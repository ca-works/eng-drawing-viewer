#!/usr/bin/env python3
r"""Inspect a built ENG Drawing Viewer bundle before release.

    python tools/check_bundle.py "release/macos/ENG Drawing Viewer.app" [--json report.json]
                                [--private-terms terms.txt]
    python tools\check_bundle.py "release\windows\ENG Drawing Viewer" [--json report.json]

Run it with the build environment's Python (it needs PyInstaller to read the bundled archive).
It fails (exit status 1) if it finds any of these:

- personal or machine-specific text, in any file or inside the compressed Python archive;
- absolute source paths recorded in the bundled Python code objects;
- install metadata that records where a package was installed from (direct_url.json);
- modules that could reach the network (ssl, QtNetwork) or that the app excludes on purpose.

It also reports the highest minimum macOS version declared by any bundled binary, which is the
real minimum the app can run on, and checks Info.plist against it. With --require-archs (the
Universal edition: arm64,x86_64) every Mach-O file must contain each listed architecture.

For a Linux AppDir it reads every ELF file: x86-64 only, the highest glibc symbol version any of
them needs (fails above --max-glibc), RPATHs pointing outside the bundle, the Qt platform plugins
for X11 and Wayland, and every library the bundle expects from the host, which must be one of the
libraries all supported desktops provide.

For a Windows build (the application folder) it also checks every PE file (EXE, DLL, PYD): x64
only; the security flags of each binary (ASLR, high-entropy ASLR, DEP, Control Flow Guard; missing
flags in third-party files are reported, missing flags in this project's executables fail); the
launcher's imports (only kernel32.dll); both executables' manifests (Common Controls v6,
PerMonitorV2, longPathAware, asInvoker) and version information; and text in UTF-16 as well,
which is how Windows binaries store most strings.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import types
import zipfile
import marshal
from pathlib import Path

# Owner-specific terms (names, former workplaces) are read from a private file passed with
# --private-terms, so they never appear in this public code. Generic checks below always run.
PRIVATE_TERMS: list[bytes] = []
PRIVATE_WORDS: list[re.Pattern] = []
TEXT_ONLY_WORDS: list[re.Pattern] = []


def load_private_terms(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("word:"):
            word = line[5:].encode()
            rx = re.compile(rb"(?<![A-Za-z0-9])" + re.escape(word) + rb"(?![A-Za-z0-9])", re.I)
            # Short words match random bytes in compiled code, so check them in text only.
            (TEXT_ONLY_WORDS if len(word) <= 3 else PRIVATE_WORDS).append(rx)
        else:
            PRIVATE_TERMS.append(line.encode())


_user = os.environ.get("USER", "")
if len(_user) >= 4:
    PRIVATE_TERMS.append(f"/Users/{_user}/".encode())
# Absolute build paths are normal in upstream binaries; a path from *this* machine is not.
HOME_PATH = re.compile(rb"/(?:Users|home)/([A-Za-z0-9_.-]+)/")
WIN_HOME_PATH = re.compile(rb"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]([^\\/\x00\"<>|:*?\r\n]{1,64})[\\/]", re.I)
UPSTREAM_USERS = {b"runner", b"qt", b"sysadmin", b"ronald", b"Barney", b"distiller", b"admin",
                  b"builder", b"vsts", b"jenkins", b"user", b"me", b"you", b"Shared",
                  # Windows build machines of upstream projects (GitHub Actions, Azure Pipelines, Qt CI)
                  b"runneradmin", b"VssAdministrator", b"cloudtest", b"ContainerAdministrator",
                  b"Administrator", b"Public", b"Default", b"All Users", b"buildbot", b"appveyor"}


def macho_minos(path: Path) -> str | None:
    try:
        out = subprocess.run(["otool", "-l", str(path)], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"cmd LC_BUILD_VERSION.*?minos (\S+)", out, re.S) or \
        re.search(r"cmd LC_VERSION_MIN_MACOSX.*?version (\S+)", out, re.S)
    return m.group(1) if m else None


def is_macho(data: bytes) -> bool:
    return data[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


MACHO_CPU = {0x0100000C: "arm64", 0x01000007: "x86_64", 0x0000000C: "arm", 0x00000007: "i386"}


def macho_archs(data: bytes) -> set[str]:
    """Architectures in a thin or fat Mach-O file."""
    import struct

    if data[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        return {MACHO_CPU.get(struct.unpack_from("<I", data, 4)[0], "other")}
    order = "<" if data[:4] == b"\xbe\xba\xfe\xca" else ">"  # fat headers are big-endian
    n = struct.unpack_from(order + "I", data, 4)[0]
    if n > 30 or len(data) < 8 + n * 20:
        return {"other"}
    return {MACHO_CPU.get(struct.unpack_from(order + "I", data, 8 + i * 20)[0], "other") for i in range(n)}


def is_pe(data: bytes) -> bool:
    if data[:2] != b"MZ" or len(data) < 0x40:
        return False
    off = int.from_bytes(data[0x3C:0x40], "little")
    return data[off:off + 4] == b"PE\0\0"


IMAGE_FILE_MACHINE_AMD64 = 0x8664
DLL_FLAGS = {"high-entropy ASLR": 0x0020, "ASLR": 0x0040, "DEP": 0x0100, "CFG": 0x4000}
OWN_EXES = {"ENG Drawing Viewer.exe", "app/ENGDrawingViewer.exe"}


def pe_report(rel: str, path: Path, findings: list, info: dict):
    """Architecture, security flags, imports, manifest and version of one PE file (needs pefile,
    which PyInstaller installs on Windows)."""
    try:
        import pefile
    except ImportError:
        info.setdefault("notes", []).append("pefile is not installed; PE files were only scanned for text")
        return
    try:
        pe = pefile.PE(str(path), fast_load=True)
    except pefile.PEFormatError as exc:
        findings.append(f"{rel}: not a valid PE file ({exc})")
        return
    try:
        if pe.FILE_HEADER.Machine != IMAGE_FILE_MACHINE_AMD64:
            findings.append(f"{rel}: not an x64 binary (machine 0x{pe.FILE_HEADER.Machine:04X})")
        flags = pe.OPTIONAL_HEADER.DllCharacteristics
        missing = [n for n, bit in DLL_FLAGS.items() if not flags & bit and not (n == "CFG" and rel not in OWN_EXES)]
        if missing:
            if rel in OWN_EXES and [m for m in missing if m != "CFG"]:
                findings.append(f"{rel}: missing {', '.join(missing)}")
            else:
                info.setdefault("pe_without_flags", {})[rel] = missing
        if rel not in OWN_EXES:
            return
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        imports = sorted({e.dll.decode().lower() for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])})
        info.setdefault("executables", {})[rel] = {"imports": imports}
        if rel == "ENG Drawing Viewer.exe" and imports != ["kernel32.dll"]:
            findings.append(f"{rel}: the launcher should import only kernel32.dll, not {imports}")
        manifest = b""
        for entry in getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None).entries if hasattr(pe, "DIRECTORY_ENTRY_RESOURCE") else []:
            if entry.id == 24:  # RT_MANIFEST
                data = entry.directory.entries[0].directory.entries[0].data.struct
                manifest = pe.get_data(data.OffsetToData, data.Size)
        for needed in (b"Microsoft.Windows.Common-Controls", b"PerMonitorV2", b"<longPathAware", b'level="asInvoker"'):
            if needed not in manifest:
                findings.append(f"{rel}: manifest lacks {needed.decode()}")
        if b"longPathAware" in manifest and not re.search(rb"<longPathAware[^>]*>\s*true", manifest):
            findings.append(f"{rel}: manifest does not enable long paths")
        pe.parse_data_directories()
        strings = {}
        for fi in getattr(pe, "FileInfo", []) or []:
            for block in fi:
                for table in getattr(block, "StringTable", []):
                    strings.update({k.decode(): v.decode(errors="replace") for k, v in table.entries.items()})
        info["executables"][rel]["version"] = strings.get("ProductVersion")
        want = info.get("app_version")
        if want and strings.get("ProductVersion") != want:
            findings.append(f"{rel}: ProductVersion {strings.get('ProductVersion')!r} is not {want!r}")
    finally:
        pe.close()


EM_X86_64 = 62
SHT_DYNAMIC, SHT_GNU_VERNEED = 6, 0x6FFFFFFE
DT_NEEDED, DT_RPATH, DT_RUNPATH, DT_SONAME = 1, 15, 29, 14


def is_elf(data: bytes) -> bool:
    return data[:4] == b"\x7fELF"


def elf_info(data: bytes) -> dict | None:
    """Machine, DT_NEEDED/SONAME/RPATH/RUNPATH and versioned symbol requirements of a 64-bit
    little-endian ELF file, read directly (no binutils needed)."""
    import struct

    if len(data) < 64 or data[4] != 2 or data[5] != 1:
        return None
    machine = struct.unpack_from("<H", data, 18)[0]
    shoff, = struct.unpack_from("<Q", data, 40)
    shentsize, shnum = struct.unpack_from("<HH", data, 58)
    sections = []
    for i in range(shnum):
        off = shoff + i * shentsize
        if off + 64 > len(data):
            return None
        _name, typ, _flags, _addr, offset, size, link = struct.unpack_from("<IIQQQQI", data, off)
        sections.append((typ, offset, size, link))

    def cstr(table: int, at: int) -> str:
        base = sections[table][1] + at
        end = data.find(b"\0", base)
        return data[base:end].decode(errors="replace")

    out = {"machine": machine, "needed": [], "rpath": [], "soname": None, "versions": set()}
    for typ, offset, size, link in sections:
        if typ == SHT_DYNAMIC:
            for j in range(0, size, 16):
                tag, val = struct.unpack_from("<qQ", data, offset + j)
                if tag == 0:
                    break
                if tag == DT_NEEDED:
                    out["needed"].append(cstr(link, val))
                elif tag in (DT_RPATH, DT_RUNPATH):
                    out["rpath"].append(cstr(link, val))
                elif tag == DT_SONAME:
                    out["soname"] = cstr(link, val)
        elif typ == SHT_GNU_VERNEED:
            pos = offset
            while True:
                _ver, cnt, _file, aux, nxt = struct.unpack_from("<HHIII", data, pos)
                apos = pos + aux
                for _ in range(cnt):
                    _hash, _flags, _other, name, anext = struct.unpack_from("<IHHII", data, apos)
                    out["versions"].add(cstr(link, name))
                    apos += anext
                if not nxt:
                    break
                pos += nxt
    return out


# Libraries every supported Linux desktop provides: the graphics driver stack, the C and C++
# runtimes, X11/Wayland client libraries, fontconfig/FreeType, GLib and D-Bus. The AppImage uses the
# system's copies (bundled ones break against newer drivers and configuration files, which is why
# AppImage's own exclude list leaves them out); the build drops them and this check accepts them.
HOST_PROVIDED = re.compile(
    r"^(ld-linux-x86-64|libc|libm|libmvec|libdl|libpthread|librt|libutil|libresolv|libgcc_s|libstdc\+\+"
    r"|libGL|libEGL|libOpenGL|libGLX|libGLdispatch|libgbm|libdrm|libvulkan"
    r"|libX11|libX11-xcb|libXau|libXdmcp|libXext|libXrender|libXi|libXfixes|libXcursor|libXrandr|libSM|libICE"
    r"|libxcb|libxcb-(glx|randr|render|shape|shm|sync|xfixes|xkb|xinerama|dri2|dri3|present)"
    r"|libxkbcommon|libxkbcommon-x11|libwayland-(client|cursor|egl)"
    r"|libfontconfig|libfreetype|libglib-2\.0|libgobject-2\.0|libgthread-2\.0|libgio-2\.0|libgmodule-2\.0"
    r"|libdbus-1|libz|libexpat|libuuid)\.so(\.\d+)*$")


def highest(versions: set, prefix: str) -> str | None:
    found = [v[len(prefix):] for v in versions if v.startswith(prefix) and re.fullmatch(r"[\d.]+", v[len(prefix):])]
    return max(found, key=vkey) if found else None


def vkey(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def code_filenames(code: types.CodeType):
    yield code.co_filename
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from code_filenames(const)


def scan_bytes(label: str, data: bytes, findings: list, narrowed: bool = False):
    low = data.lower()
    if not narrowed and is_pe(data):
        # Most strings in Windows binaries are UTF-16: scan a narrowed copy of those too.
        wide = re.sub(rb"([\x20-\x7e])\x00", rb"\1", data)
        if wide != data:
            scan_bytes(label + " (UTF-16 text)", wide, findings, narrowed=True)
    if not is_macho(data) and b"\0" not in data[:4096]:
        for rx in TEXT_ONLY_WORDS:
            if rx.search(data):
                findings.append(f"{label}: contains a private term")
    for term in PRIVATE_TERMS:
        if term.lower() in low:
            findings.append(f"{label}: contains {term.decode()!r}")
    for rx in PRIVATE_WORDS:
        m = rx.search(data)
        if m:
            findings.append(f"{label}: contains {m.group(0).decode(errors='replace')!r}")
    for m in HOME_PATH.finditer(data):
        if m.group(1) not in UPSTREAM_USERS:
            findings.append(f"{label}: home-folder path {m.group(0).decode(errors='replace')!r}")
    for m in WIN_HOME_PATH.finditer(data):
        if m.group(1) not in UPSTREAM_USERS and m.group(1).lower() not in {u.lower() for u in UPSTREAM_USERS}:
            findings.append(f"{label}: home-folder path {m.group(0).decode(errors='replace')!r}")


def scan_pyinstaller(executable: Path, findings: list, info: dict):
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except ImportError:
        findings.append("PyInstaller is not installed in this Python; the bundled archive was not checked")
        return
    try:
        carchive = CArchiveReader(str(executable))
    except Exception as exc:  # not a PyInstaller executable
        info.setdefault("notes", []).append(f"{executable.name}: no PyInstaller archive ({exc})")
        return
    modules = 0
    archives = []
    for name in carchive.toc:
        typecode = carchive.toc[name][-1] if isinstance(carchive.toc[name], tuple) else None
        if typecode == "z" or name.startswith("PYZ"):
            archives.append(carchive.open_embedded_archive(name))
            continue
        data = carchive.extract(name)
        if data:
            scan_bytes(f"{executable.name}:{name}", data, findings)
    for reader in archives:
        for mod in reader.toc:
            modules += 1
            if mod.split(".")[0] in ("ssl", "_ssl") or "QtNetwork" in mod:
                findings.append(f"network-capable module bundled: {mod}")
            try:
                obj = reader.extract(mod, raw=False)
            except Exception:
                obj = None
            if isinstance(obj, types.CodeType):
                for fn in code_filenames(obj):
                    if fn.startswith("/") or re.match(r"[A-Za-z]:\\", fn):
                        findings.append(f"{mod}: absolute source path {fn!r}")
                        break
                    scan_bytes(f"{mod} (code)", fn.encode(), findings)
                for const in obj.co_consts:
                    if isinstance(const, (str, bytes)):
                        scan_bytes(f"{mod} (strings)", const if isinstance(const, bytes) else const.encode(errors="ignore"), findings)
    info["python_modules"] = modules


def linux_report(root: Path, elf: dict, findings: list, info: dict, max_glibc: str):
    """Architecture, minimum glibc, build paths and the host libraries an AppDir relies on."""
    bundled = {Path(rel).name for rel in elf} | {e["soname"] for e in elf.values() if e["soname"]}
    host: dict[str, list[str]] = {}
    versions: set = set()
    for rel, e in elf.items():
        if e["machine"] != EM_X86_64:
            findings.append(f"{rel}: not an x86-64 binary (machine {e['machine']})")
        versions |= e["versions"]
        for rp in e["rpath"]:
            for part in rp.split(":"):
                if part and not part.startswith("$ORIGIN"):
                    findings.append(f"{rel}: RPATH/RUNPATH outside the bundle: {part}")
        for lib in e["needed"]:
            if lib not in bundled:
                host.setdefault(lib, []).append(rel)
    glibc = highest(versions, "GLIBC_")
    info["glibc_required"] = glibc
    info["glibcxx_required"] = highest(versions, "GLIBCXX_")
    info["glibc_required_by"] = sorted(rel for rel, e in elf.items() if glibc and f"GLIBC_{glibc}" in e["versions"])[:12]
    if glibc and vkey(glibc) > vkey(max_glibc):
        findings.append(f"bundled binaries need glibc {glibc}, newer than the supported minimum {max_glibc}")
    info["host_libraries"] = sorted(host)
    for lib in sorted(host):
        if not HOST_PROVIDED.match(lib):
            findings.append(f"needs {lib} from the host system (used by {host[lib][0]}); bundle it or document it")
    for plugin in ("platforms/libqxcb.so", "platforms/libqwayland.so", "platformthemes/libqxdgdesktopportal.so",
                   "wayland-shell-integration/libxdg-shell.so", "wayland-decoration-client/libbradient.so"):
        if not list(root.glob(f"usr/lib/eng-drawing-viewer/_internal/PySide6/Qt/plugins/{plugin}")):
            findings.append(f"Qt plugin {plugin} is missing")
    internal = root / "usr" / "lib" / "eng-drawing-viewer" / "_internal"
    if not (internal / "tessdata" / "eng.traineddata").is_file():
        findings.append("OCR language data (tessdata/eng.traineddata) is missing")
    for legal in ("legal/LICENSE", "legal/licenses/THIRD_PARTY_NOTICES.txt"):
        if not (internal / legal).is_file():
            findings.append(f"{legal}: missing")
    for name in ("AppRun", "usr/lib/eng-drawing-viewer/eng-drawing-viewer"):
        if not os.access(root / name, os.X_OK):
            findings.append(f"{name}: not executable")
    if not list(root.glob("*.desktop")) or not (root / ".DirIcon").exists():
        findings.append("AppDir needs a top-level .desktop file and .DirIcon")


def load_reviewed(path: Path) -> list[tuple[re.Pattern, str]]:
    """Exact finding texts (with "*" wildcards) that were reviewed; see packaging/reviewed-upstream-findings.txt."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        text, _, why = line.partition(" | ")
        pattern = "".join("[^ ]*" if c == "*" else re.escape(c) for c in text.strip())
        out.append((re.compile(pattern), why.strip() or "reviewed"))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--private-terms", type=Path,
                        default=Path(__file__).resolve().parents[1] / "audit" / "private-terms.txt",
                        help="file of owner-specific terms that must not appear (kept out of the repository)")
    parser.add_argument("--require-archs", default="",
                        help="macOS: every Mach-O file must contain these architectures (arm64,x86_64 for Universal)")
    parser.add_argument("--max-glibc", default="2.35",
                        help="Linux: the oldest glibc the build must run on (Ubuntu 22.04 has 2.35)")
    parser.add_argument("--reviewed", type=Path,
                        help="reviewed upstream findings that are reported but do not fail "
                             "(packaging/reviewed-upstream-findings.txt)")
    args = parser.parse_args()
    if args.private_terms.is_file():
        load_private_terms(args.private_terms)
    else:
        print(f"Note: {args.private_terms} not found; only the generic checks run.")
    root = args.bundle.resolve()
    findings: list[str] = []
    info: dict = {"bundle": root.name, "files": 0, "bytes": 0, "minos": {}}
    windows = (root / "app" / "ENGDrawingViewer.exe").is_file()
    linux = (root / "AppRun").is_file() and (root / "usr" / "lib" / "eng-drawing-viewer").is_dir()
    elf: dict[str, dict] = {}
    if windows:
        init = (Path(__file__).resolve().parents[1] / "src" / "eng_viewer" / "__init__.py").read_text(encoding="utf-8")
        info["app_version"] = re.search(r'__version__ = "([^"]+)"', init).group(1)

    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        info["files"] += 1
        data = path.read_bytes()
        info["bytes"] += len(data)
        if path.name == "direct_url.json":
            findings.append(f"{rel}: install-source metadata (direct_url.json)")
        if any(m in rel for m in ("QtNetwork", "Qt6Network", "tuiotouch", "/_ssl", "libssl", "libcrypto",
                                  "qschannelbackend", "qopensslbackend", "qcertonlybackend", "qnetworklistmanager",
                                  "win32net", "win32wnet", "win32inet")):
            findings.append(f"{rel}: excluded component is bundled")
        scan_bytes(rel, data, findings)
        if windows and is_pe(data):
            pe_report(rel, path, findings, info)
        if linux and is_elf(data):
            parsed = elf_info(data)
            if parsed is None:
                findings.append(f"{rel}: not a 64-bit little-endian ELF file")
            else:
                elf[rel] = parsed
        if is_macho(data):
            if args.require_archs:
                archs = macho_archs(data)
                info.setdefault("architectures", {}).setdefault(",".join(sorted(archs)), 0)
                info["architectures"][",".join(sorted(archs))] += 1
                missing = set(args.require_archs.split(",")) - archs
                if missing:
                    findings.append(f"{rel}: lacks {', '.join(sorted(missing))} (has {', '.join(sorted(archs))})")
            v = macho_minos(path)
            if v:
                info["minos"][rel] = v

    for zpath in sorted(root.rglob("base_library.zip")):
        with zipfile.ZipFile(zpath) as zf:
            for member in zf.namelist():
                data = zf.read(member)
                scan_bytes(f"{zpath.name}:{member}", data, findings)
                if member.endswith(".pyc") and len(data) > 16:
                    try:
                        code = marshal.loads(data[16:])
                    except Exception:
                        continue
                    for fn in code_filenames(code):
                        if (fn.startswith("/") and not fn.startswith("/Users/" + "runner/")) \
                                or re.match(r"[A-Za-z]:[\\/]", fn):
                            findings.append(f"{zpath.name}:{member}: absolute source path {fn!r}")
                            break

    if linux:
        exe = root / "usr" / "lib" / "eng-drawing-viewer"
        scan_pyinstaller(exe / "eng-drawing-viewer", findings, info)
        linux_report(root, elf, findings, info, args.max_glibc)
    else:
        exe = root / "app" if windows else root / "Contents" / "MacOS"
        for binary in sorted(exe.iterdir()) if exe.exists() else []:
            if binary.is_file() and (not windows or binary.suffix.lower() == ".exe"):
                scan_pyinstaller(binary, findings, info)
    if windows:
        for own in OWN_EXES:
            if not (root / own).is_file():
                findings.append(f"{own}: missing")
        for plugin in ("platforms/qwindows.dll", "styles/qmodernwindowsstyle.dll"):
            if not list(root.glob(f"app/_internal/PySide6/plugins/{plugin}")):
                findings.append(f"Qt plugin {plugin} is missing (needed for the native Windows look)")
        if not list(root.glob("app/_internal/tessdata/eng.traineddata")):
            findings.append("OCR language data (tessdata/eng.traineddata) is missing")
        for legal in ("legal/LICENSE", "legal/licenses/THIRD_PARTY_NOTICES.txt", "Licence.txt", "Read Me.txt"):
            if not (root / legal).is_file():
                findings.append(f"{legal}: missing")
        info["pe_without_flags"] = dict(sorted(info.get("pe_without_flags", {}).items()))

    # PySide6/shiboken6 6.12.0 wheels are tagged for macOS 14 but their binding binaries declare 26.0.
    # Qt's own frameworks, and every other bundled library (OpenCV, NumPy, ONNX Runtime, codecs),
    # declare at most 14.4. Packaged candidates ran on macOS 14.8, 15.7, 26.6 and 27 (macos-compat
    # CI job and local runs), so this label is not a runtime requirement. The minimum is taken from
    # everything else, and the ignored files are still listed in the report.
    mislabelled = re.compile(r"Frameworks/((PySide6|shiboken6)/)?([A-Za-z0-9]+\.abi3\.so|lib(pyside6|shiboken6)\.abi3\.[0-9.]+dylib)$")
    info["minos_ignored"] = sorted(k for k in info["minos"] if mislabelled.search(k))
    for k in info["minos_ignored"]:
        info["minos"].pop(k)
    if info["minos"]:
        highest = max(info["minos"].values(), key=vkey)
        info["highest_minos"] = highest
        info["highest_minos_files"] = sorted(k for k, v in info["minos"].items() if v == highest)[:12]
        plist = root / "Contents" / "Info.plist"
        if plist.exists():
            declared = plistlib.loads(plist.read_bytes()).get("LSMinimumSystemVersion", "")
            info["LSMinimumSystemVersion"] = declared
            if not declared or vkey(declared) < vkey(highest):
                findings.append(f"Info.plist LSMinimumSystemVersion {declared or '(none)'} is lower than "
                                f"the bundled binaries' minimum {highest}")
    info.pop("minos")
    findings = sorted(set(findings))
    accepted = load_reviewed(args.reviewed) if args.reviewed else []
    info["reviewed_findings"], info["findings"] = [], []
    for f in findings:
        why = next((reason for rx, reason in accepted if rx.fullmatch(f)), None)
        if why is None:
            info["findings"].append(f)
        else:
            info["reviewed_findings"].append({"finding": f, "reason": why})
    if args.json:
        args.json.write_text(json.dumps(info, indent=2))
    print(json.dumps({k: v for k, v in info.items() if k != "findings"}, indent=2))
    for r in info["reviewed_findings"]:
        print("REVIEWED:", r["finding"], "--", r["reason"])
    for f in info["findings"][:200]:
        print("FINDING:", f)
    print(f"{len(info['findings'])} finding(s), {len(info['reviewed_findings'])} reviewed upstream finding(s)")
    sys.exit(1 if info["findings"] else 0)


if __name__ == "__main__":
    main()
