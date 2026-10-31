#!/usr/bin/env python3
"""Make the universal2 (Apple Silicon + Intel) inputs of the macOS Universal edition.

    python tools/fuse_macos.py python ARM64.tar.gz X86_64.tar.gz OUT_DIR
    python tools/fuse_macos.py wheels packaging/requirements-macos.txt OUT_DIR [--cache DIR] [--skip NAME]
    python tools/fuse_macos.py pair ARM64.whl X86_64.whl OUT_DIR

``python`` fuses the two official python-build-standalone builds of the same CPython release into
one universal2 interpreter tree. ``wheels`` fills a wheelhouse for every package in the lock: the
package's universal2 or pure-Python wheel where PyPI has one, otherwise its arm64 and x86_64 wheels
fused into a universal2 wheel. Every downloaded file must match a SHA-256 in the lock. ``--skip``
leaves a package out (OpenCV: its published arm64 and x86_64 wheels bundle different libraries, so
tools/build_opencv_minimal.sh builds both from source and ``pair`` fuses them).

Fusing is strict, so that "universal" means every binary really has both architectures:

- each pair of Mach-O files (or static archives) is merged with lipo after checking that the arm64
  input contains arm64 code and the x86_64 input x86_64 code;
- identical files are kept;
- a file that exists in only one input, or a non-binary file that differs, stops the run, unless it
  is listed in ALLOWED_DIFFERENCES below with the reason it is harmless.

lipo comes from $LIPO, or ``lipo`` (macOS), or LLVM's ``llvm-lipo`` (Linux), so this also runs on a
Linux machine or CI runner. A report of every input and output hash is written to OUT_DIR.
Needs network access only for downloads; the app never runs this.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# The macOS minimum of the app (LSMinimumSystemVersion): no wheel may need a newer macOS.
DEPLOYMENT_TARGET = (14, 4)
CPU_ARM64, CPU_X86_64 = 0x0100000C, 0x01000007
ARCH_NAMES = {CPU_ARM64: "arm64", CPU_X86_64: "x86_64"}

# Non-binary files allowed to differ between the two architectures (the arm64 copy is kept), and
# files allowed in only one input. Paths are relative to the tree (interpreter) or wheel root.
# Build-only parts of the interpreter, removed from both inputs before fusing: headers, static
# libraries and objects (LTO bitcode, which no program loads), and the compiler configuration
# folder. The universal build compiles nothing; every dependency is a prebuilt wheel.
PYTHON_PRUNE = (r"include/.*", r".*\.a", r".*\.o", r"lib/python3\.13/config-3\.13-darwin/.*",
                r"lib/[^/]+/[A-Za-z]+Config\.sh", r"lib/pkgconfig/.*", r"bin/python3[.0-9]*-config")

ALLOWED_DIFFERENCES = {
    "python": {
        # Compiler flags of each slice (-arch, deployment target, JIT options). Python reads them
        # only to build C extensions; the values that matter at run time are compiled into each
        # architecture's own binary.
        "lib/python3.13/_sysconfigdata__darwin_darwin.py": "compiler flags of each slice (only for building extensions)",
        # pip as installed by the interpreter's own build (not shipped; only the temp folder differs).
        "lib/python3.13/site-packages/pip-26.2.1.dist-info/RECORD": "pip install record (pip is not shipped)",
        "lib/python3.13/site-packages/pip-26.2.1.dist-info/direct_url.json": "pip install record (pip is not shipped)",
    },
    "python-one-sided": {
        # Bytecode caches the arm64 build happened to leave; bytecode is architecture independent.
        "lib/python3.13/encodings/__pycache__/__init__.cpython-313.pyc": "bytecode cache",
        "lib/python3.13/encodings/__pycache__/aliases.cpython-313.pyc": "bytecode cache",
        "lib/python3.13/encodings/__pycache__/utf_8.cpython-313.pyc": "bytecode cache",
    },
}
# Per package: {path regex: reason} for differing non-binary files or one-sided files.
WHEEL_ALLOWED: dict[str, dict[str, str]] = {
    "numpy": {
        r"numpy/__config__\.py": "build information shown by numpy.show_config() (the arm64 copy is kept)",
    },
}
# C headers that differ by architecture are merged into one header that picks each architecture's
# version with the preprocessor, as universal2 headers do, so compiling for either target is correct.
MERGED_HEADERS = {
    "numpy": (r"numpy/_core/include/numpy/_numpyconfig\.h",),
}


# ----------------------------------------------------------------------------- Mach-O helpers
def lipo_tool() -> str:
    for candidate in (os.environ.get("LIPO"), "lipo", "llvm-lipo", "llvm-lipo-20", "llvm-lipo-19", "llvm-lipo-18"):
        if candidate and shutil.which(candidate):
            return shutil.which(candidate)
    raise SystemExit("lipo is required (Xcode command line tools on macOS, or llvm-lipo from LLVM)")


def macho_archs(data: bytes) -> set[str] | None:
    """Architectures of a Mach-O file (thin or fat), or None if it is not one."""
    import struct

    magic = data[:4]
    if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        return {ARCH_NAMES.get(struct.unpack_from("<I", data, 4)[0], "other")}
    if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        n = struct.unpack_from(">I", data, 4)[0]
        if n > 30:  # Java class files share the magic number
            return None
        size = 20 if magic == b"\xca\xfe\xba\xbe" else 32
        return {ARCH_NAMES.get(struct.unpack_from(">I", data, 8 + i * size)[0], "other") for i in range(n)}
    return None


def is_archive(data: bytes) -> bool:
    return data[:8] == b"!<arch>\n"


def archive_archs(path: Path) -> set[str]:
    out = subprocess.run([lipo_tool(), "-archs", str(path)], capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def lipo_merge(arm: Path, x86: Path, out: Path) -> None:
    tmp = out.with_name(out.name + ".lipo")
    subprocess.run([lipo_tool(), "-create", str(arm), str(x86), "-output", str(tmp)], check=True,
                   capture_output=True, text=True)
    os.replace(tmp, out)


def merged_header(arm: bytes, x86: bytes) -> bytes:
    return (b"/* universal2: arm64 and x86_64 versions of this header (tools/fuse_macos.py) */\n"
            b"#if defined(__arm64__) || defined(__aarch64__)\n" + arm.rstrip(b"\n") +
            b"\n#elif defined(__x86_64__)\n" + x86.rstrip(b"\n") +
            b"\n#else\n#error \"unsupported architecture\"\n#endif\n")


def fuse_tree(arm_root: Path, x86_root: Path, out_root: Path, allowed: dict[str, str],
              one_sided: dict[str, str], headers: tuple[str, ...] = ()) -> dict:
    """Fuse two extracted trees into out_root (a copy of the arm64 tree, updated in place)."""
    if out_root.exists():
        shutil.rmtree(out_root)
    shutil.copytree(arm_root, out_root, symlinks=True)
    report = {"merged": [], "identical": 0, "allowed_differences": {}, "one_sided": {}, "problems": []}
    arm_files = {p.relative_to(arm_root).as_posix() for p in arm_root.rglob("*") if p.is_file() or p.is_symlink()}
    x86_files = {p.relative_to(x86_root).as_posix() for p in x86_root.rglob("*") if p.is_file() or p.is_symlink()}
    for rel in sorted(arm_files ^ x86_files):
        reason = next((why for rx, why in one_sided.items() if re.fullmatch(rx, rel)), None)
        if reason is None:
            report["problems"].append(f"only in the {'arm64' if rel in arm_files else 'x86_64'} input: {rel}")
        else:
            report["one_sided"][rel] = reason
            if rel not in arm_files:
                (out_root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(x86_root / rel, out_root / rel, follow_symlinks=False)
    for rel in sorted(arm_files & x86_files):
        a, x = arm_root / rel, x86_root / rel
        if a.is_symlink() or x.is_symlink():
            if not (a.is_symlink() and x.is_symlink() and os.readlink(a) == os.readlink(x)):
                report["problems"].append(f"symbolic links differ: {rel}")
            continue
        da, dx = a.read_bytes(), x.read_bytes()
        if da == dx:
            report["identical"] += 1
            continue
        aa, xa = macho_archs(da), macho_archs(dx)
        if aa is not None and xa is not None or is_archive(da) and is_archive(dx):
            if aa is None:
                try:
                    aa, xa = archive_archs(a), archive_archs(x)
                except subprocess.CalledProcessError:
                    report["problems"].append(f"{rel}: lipo cannot read this static library")
                    continue
            if "arm64" not in aa or "x86_64" not in xa:
                report["problems"].append(f"{rel}: arm64 input has {sorted(aa)}, x86_64 input has {sorted(xa)}")
                continue
            if aa >= {"arm64", "x86_64"}:
                continue  # already universal in the arm64 input; keep it
            lipo_merge(a, x, out_root / rel)
            report["merged"].append(rel)
            continue
        if any(re.fullmatch(rx, rel) for rx in headers):
            (out_root / rel).write_bytes(merged_header(da, dx))
            report.setdefault("merged_headers", []).append(rel)
            continue
        reason = next((why for rx, why in allowed.items() if re.fullmatch(rx, rel)), None)
        if reason is None:
            report["problems"].append(f"differs between the architectures and is not a binary: {rel}")
        else:
            report["allowed_differences"][rel] = reason
    # Every Mach-O file in the result must hold both architectures.
    for p in sorted(out_root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            data = p.read_bytes()[:4096]
            archs = macho_archs(data)
            if archs is None and is_archive(data):
                try:
                    archs = archive_archs(p)
                except subprocess.CalledProcessError:
                    report["problems"].append(f"lipo cannot read the static library {p.relative_to(out_root)}")
                    continue
            if archs is not None and not archs >= {"arm64", "x86_64"}:
                report["problems"].append(f"not universal after fusing: {p.relative_to(out_root)} {sorted(archs)}")
    return report


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- interpreter
def fuse_python(arm_tar: Path, x86_tar: Path, out: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        roots = []
        for name, tar in (("arm64", arm_tar), ("x86_64", x86_tar)):
            dest = Path(tmp) / name
            dest.mkdir()
            with tarfile.open(tar) as tf:
                tf.extractall(dest, filter="tar")
            root = dest / "python"
            for p in sorted(root.rglob("*"), reverse=True):
                rel = p.relative_to(root).as_posix()
                if (p.is_file() or p.is_symlink()) and any(re.fullmatch(rx, rel) for rx in PYTHON_PRUNE):
                    p.unlink()
            for d in sorted((d for d in root.rglob("*") if d.is_dir()), reverse=True):
                if not any(d.iterdir()):
                    d.rmdir()
            roots.append(root)
        out.mkdir(parents=True, exist_ok=True)
        target = out / "python"
        allowed = {re.escape(k): v for k, v in ALLOWED_DIFFERENCES["python"].items()}
        one_sided = {re.escape(k): v for k, v in ALLOWED_DIFFERENCES["python-one-sided"].items()}
        report = fuse_tree(roots[0], roots[1], target, allowed, one_sided)
    report["inputs"] = {"arm64": {"file": arm_tar.name, "sha256": sha256(arm_tar)},
                        "x86_64": {"file": x86_tar.name, "sha256": sha256(x86_tar)}}
    (out / "fuse-python-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"interpreter: {len(report['merged'])} binaries merged, {report['identical']} identical files, "
          f"{len(report['allowed_differences'])} allowed text differences, {len(report['problems'])} problem(s)")
    for p in report["problems"]:
        print("PROBLEM:", p)
    return 1 if report["problems"] else 0


# ----------------------------------------------------------------------------- wheels
def parse_lock(lock: Path) -> dict[str, tuple[str, set[str]]]:
    """name -> (version, allowed sha256 hashes) from a uv/pip-compile lock with hashes."""
    out, current = {}, None
    for line in lock.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", line)
        if m:
            current = re.sub(r"[-_.]+", "-", m.group(1)).lower()
            out[current] = (m.group(2), set())
        for h in re.findall(r"--hash=sha256:([0-9a-f]{64})", line):
            if current:
                out[current][1].add(h)
    return out


WHEEL_RE = re.compile(r"^(?P<name>.+?)-(?P<ver>[^-]+)(-\d[^-]*)?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$")


def compatible_python(py: str, abi: str) -> bool:
    if py.startswith(("py3", "py2.py3")) and abi == "none":
        return True
    if py == "cp313" and abi == "cp313":
        return True
    m = re.fullmatch(r"cp3(\d+)", py)
    return abi == "abi3" and m is not None and int(m.group(1)) <= 13


def mac_tag(plat: str, arch: str) -> tuple[int, int] | None:
    """The macOS version of a platform tag for this architecture, if it fits the deployment target."""
    best = None
    for tag in plat.split("."):
        m = re.fullmatch(r"macosx_(\d+)_(\d+)_(\w+)", tag)
        if m and m.group(3) == arch:
            v = (int(m.group(1)), int(m.group(2)))
            if v <= DEPLOYMENT_TARGET and (best is None or v > best):
                best = v
    return best


def choose(files: list[dict]) -> dict:
    """{'universal2' | 'pure' | 'arm64' and 'x86_64': file} for one package version."""
    picks: dict[str, tuple] = {}
    for f in files:
        m = WHEEL_RE.match(f["filename"])
        if not m or not compatible_python(m["py"], m["abi"]):
            continue
        if m["plat"] == "any":
            picks.setdefault("pure", ((0, 0), f))
            continue
        for arch in ("universal2", "arm64", "x86_64"):
            v = mac_tag(m["plat"], arch)
            if v is not None and (arch not in picks or v > picks[arch][0]):
                picks[arch] = (v, f)
    if "universal2" in picks:
        return {"universal2": picks["universal2"][1]}
    if "arm64" in picks and "x86_64" in picks:
        return {"arm64": picks["arm64"][1], "x86_64": picks["x86_64"][1]}
    if "pure" in picks:
        return {"pure": picks["pure"][1]}
    raise SystemExit(f"no universal2, pure, or arm64 + x86_64 wheels: {[f['filename'] for f in files]}")


def download(f: dict, cache: Path, allowed: set[str]) -> Path:
    dest = cache / f["filename"]
    want = f["digests"]["sha256"]
    if want not in allowed:
        raise SystemExit(f"{f['filename']}: its SHA-256 is not in the lock")
    if not (dest.is_file() and sha256(dest) == want):
        with urllib.request.urlopen(f["url"], timeout=300) as resp:  # noqa: S310 (PyPI https URLs)
            data = resp.read()
        if hashlib.sha256(data).hexdigest() != want:
            raise SystemExit(f"{f['filename']}: download does not match its SHA-256")
        dest.write_bytes(data)
    return dest


def record_line(root: Path, rel: str) -> list[str]:
    data = (root / rel).read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return [rel, f"sha256={digest}", str(len(data))]


def fuse_wheel(arm: Path, x86: Path, out_dir: Path, name: str) -> tuple[Path, dict]:
    ma, mx = WHEEL_RE.match(arm.name), WHEEL_RE.match(x86.name)
    va, vx = mac_tag(ma["plat"], "arm64"), mac_tag(mx["plat"], "x86_64")
    plat = "macosx_{}_{}_universal2".format(*max(va, vx))
    with tempfile.TemporaryDirectory() as tmp:
        ra, rx, ro = Path(tmp, "arm64"), Path(tmp, "x86_64"), Path(tmp, "out")
        for whl, root in ((arm, ra), (x86, rx)):
            with zipfile.ZipFile(whl) as zf:
                zf.extractall(root)
        # Static libraries in wheels (numpy's libnpymath.a and libnpyrandom.a) are for compiling
        # other extensions against the package; nothing loads them, and they are LTO bitcode
        # that lipo cannot always read. They are left out of the fused wheel and listed.
        pruned = sorted({p.relative_to(root).as_posix() for root in (ra, rx) for p in root.rglob("*.a")})
        for root in (ra, rx):
            for rel in pruned:
                (root / rel).unlink(missing_ok=True)
        dist_info = next(p.name for p in ra.iterdir() if p.name.endswith(".dist-info"))
        allowed = {re.escape(f"{dist_info}/RECORD"): "regenerated", re.escape(f"{dist_info}/WHEEL"): "rewritten"}
        allowed.update(WHEEL_ALLOWED.get(name, {}))
        report = fuse_tree(ra, rx, ro, allowed, WHEEL_ALLOWED.get(name, {}), MERGED_HEADERS.get(name, ()))
        report["pruned"] = pruned
        report["allowed_differences"].pop(f"{dist_info}/RECORD", None)
        report["allowed_differences"].pop(f"{dist_info}/WHEEL", None)
        wheel_meta = (ro / dist_info / "WHEEL").read_text(encoding="utf-8")
        tags = sorted({f"{ma['py']}-{ma['abi']}-{plat}"})
        wheel_meta = re.sub(r"^Tag: .*\n?", "", wheel_meta, flags=re.M).rstrip("\n") + "\n"
        wheel_meta += "".join(f"Tag: {t}\n" for t in tags)
        (ro / dist_info / "WHEEL").write_text(wheel_meta, encoding="utf-8")
        files = sorted(p.relative_to(ro).as_posix() for p in ro.rglob("*") if p.is_file())
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        for rel in files:
            w.writerow([rel, "", ""] if rel == f"{dist_info}/RECORD" else record_line(ro, rel))
        (ro / dist_info / "RECORD").write_text(buf.getvalue(), encoding="utf-8")
        files = sorted(set(files) | {f"{dist_info}/RECORD"})
        out = out_dir / f"{ma['name']}-{ma['ver']}-{ma['py']}-{ma['abi']}-{plat}.whl"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in files:
                info = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))  # fixed time: reproducible
                info.external_attr = ((ro / rel).stat().st_mode & 0o777 | 0o100000) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, (ro / rel).read_bytes())
    return out, report


def pair(arm: Path, x86: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[-_.]+", "-", WHEEL_RE.match(arm.name)["name"]).lower()
    fused, report = fuse_wheel(arm, x86, out, name)
    report["inputs"] = {"arm64": [arm.name, sha256(arm)], "x86_64": [x86.name, sha256(x86)]}
    report["wheel"], report["sha256"] = fused.name, sha256(fused)
    (out / f"{name}-fuse-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{fused.name}: {len(report['merged'])} binaries merged, {len(report['problems'])} problem(s)")
    for p in report["problems"]:
        print("PROBLEM:", p)
    return 1 if report["problems"] else 0


def wheels(lock: Path, out: Path, cache: Path, skip: set[str] = frozenset()) -> int:
    pins = {k: v for k, v in parse_lock(lock).items() if k not in skip}
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    summary, problems = [], []
    for name, (version, hashes) in sorted(pins.items()):
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as resp:  # noqa: S310
            files = json.load(resp)["urls"]
        pick = choose(files)
        entry = {"package": name, "version": version}
        if "arm64" in pick:
            arm = download(pick["arm64"], cache, hashes)
            x86 = download(pick["x86_64"], cache, hashes)
            fused, report = fuse_wheel(arm, x86, out, name)
            entry.update(kind="fused", inputs={"arm64": [arm.name, sha256(arm)], "x86_64": [x86.name, sha256(x86)]},
                         wheel=fused.name, sha256=sha256(fused), merged_binaries=len(report["merged"]),
                         allowed_differences=report["allowed_differences"], one_sided=report["one_sided"],
                         pruned_static_libraries=report["pruned"])
            problems += [f"{name}: {p}" for p in report["problems"]]
        else:
            kind, f = next(iter(pick.items()))
            src = download(f, cache, hashes)
            shutil.copy2(src, out / src.name)
            entry.update(kind=kind, wheel=src.name, sha256=sha256(src))
        summary.append(entry)
        print(f"{name} {version}: {entry['kind']} -> {entry['wheel']}")
    (out / "wheelhouse-report.json").write_text(json.dumps({"lock": lock.name, "packages": summary,
                                                            "problems": problems}, indent=2), encoding="utf-8")
    for p in problems:
        print("PROBLEM:", p)
    fused = sum(1 for e in summary if e["kind"] == "fused")
    print(f"{len(summary)} packages: {fused} fused, {len(summary) - fused} used as published; {len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[0] == "python":
        return fuse_python(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    if len(argv) >= 3 and argv[0] == "wheels":
        cache = Path(argv[argv.index("--cache") + 1]) if "--cache" in argv else Path(argv[2]) / ".downloads"
        skip = {argv[i + 1] for i, a in enumerate(argv) if a == "--skip"}
        return wheels(Path(argv[1]), Path(argv[2]), cache, skip)
    if len(argv) >= 4 and argv[0] == "pair":
        return pair(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
