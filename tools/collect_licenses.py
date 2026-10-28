#!/usr/bin/env python3
r"""Account for every file in a built app and gather the licences that apply to it.

    .venv-build/bin/python tools/collect_licenses.py "release/macos/ENG Drawing Viewer.app" [--sbom out.cdx.json]
    .venv-build\Scripts\python tools\collect_licenses.py "release\windows\ENG Drawing Viewer" ^
        --out "release\windows\ENG Drawing Viewer\legal\licenses" [--sbom out.cdx.json]

Run it with the build environment's Python, so it sees the exact packages that were bundled.

Each shipped file is matched to the Python package it came from, using the package's own list of
installed files (or, for libraries PyInstaller moves, the file name), or to one of the components
in CURATED below (the Python runtime, the PyInstaller bootloader, this project). The run fails if
any shipped file can't be accounted for, so a new dependency can't slip in without its licence.

A Windows build is the application folder (launcher, app\ and legal\). Its licences are written
with --out into the build itself, because the Windows build ships different native components
(the Microsoft C++ runtime, pywin32, the Go runtime in the launcher, Tesseract language data).

Output (rewritten each run; for the Mac, commit it, because the app and the source archive ship it):
  licenses/THIRD_PARTY_NOTICES.txt   a readable summary, then every licence text in full
  licenses/components.csv            component, version, licence, files shipped
  licenses/third_party/<component>/  licence files exactly as each package provides them
"""
from __future__ import annotations

import csv
import importlib.metadata as md
import io
import re
import shutil
import sys
import sysconfig
from collections import defaultdict
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
LIC = ROOT / "licenses"
OUT = LIC / "third_party"

# Licences of native libraries inside wheels that don't describe them in their own metadata.
# Each entry: (component, licence, where the full texts come from).
CURATED_NATIVE = {
    "pyside6-essentials": [
        ("Qt 6.12.0 (qtbase, qtsvg, qtimageformats)", "LGPL-3.0-only",
         "licenses/curated/Qt/ (LGPL-3.0, GPL-3.0 and Qt's third-party attributions)"),
    ],
    "pymupdf": [
        ("MuPDF 1.28.2 and its bundled libraries", "AGPL-3.0-or-later and others",
         "licenses/curated/MuPDF/ (from the MuPDF source release)"),
    ],
}


# Packages that ship no licence file of their own, and where their licence text is.
SHARED_LICENCE = {
    "pyobjc-core": "MIT licence, the same text as pyobjc-framework-Cocoa's LICENSE.txt in this folder.",
    "pyobjc-framework-coreml": "MIT licence, the same text as pyobjc-framework-Cocoa's LICENSE.txt in this folder.",
    "pyobjc-framework-vision": "MIT licence, the same text as pyobjc-framework-Cocoa's LICENSE.txt in this folder.",
    "pyside6-essentials": "Qt for Python 6.12.0, under the GNU LGPL v3 (or GPL v2/v3). The licence texts are in licenses/curated/Qt/.",
    "shiboken6": "Shiboken 6.12.0 (part of Qt for Python), under the GNU LGPL v3 (or GPL v2/v3). The licence texts are in licenses/curated/Qt/.",
}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def licence_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    lic = (meta.get("License") or "").strip().splitlines()
    return lic[0][:80] if lic else "see licence files"


def licence_files(dist: md.Distribution) -> list[Path]:
    found = []
    for f in dist.files or []:
        p = PurePosixPath(str(f))
        name = p.name.upper()
        in_meta = any(part.endswith((".dist-info", ".egg-info")) for part in p.parts)
        looks = re.match(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS|COPYRIGHT)", name) or "licenses" in p.parts
        if looks and (in_meta or len(p.parts) <= 2):
            path = Path(dist.locate_file(f))
            if path.is_file():
                found.append(path)
    return sorted(set(found))


# Native files on Windows that come from Microsoft's C++ runtime, whichever package carried them.
MSVC_RUNTIME = re.compile(r"^(vcruntime140(_1)?|msvcp140(_1|_2|_atomic_wait|_codecvt_ids)?|concrt140|vcomp140|"
                          r"vccorlib140|ucrtbase|api-ms-win-[a-z0-9-]+)\.dll$", re.I)
MSVC_NOTE = ("Microsoft Visual C++ Runtime (vcruntime/msvcp DLLs). Redistributed unmodified, as permitted by the "
             "Microsoft Visual Studio licence terms for redistributable code; Python and Qt for Python ship these "
             "same files with their Windows releases.")
GO_LICENSE_FALLBACK = "The Go standard library and runtime are under a BSD-3-Clause licence: https://go.dev/LICENSE"


def go_license() -> str:
    """The licence of the Go runtime compiled into the Windows launcher, from the Go toolchain."""
    import subprocess

    try:
        goroot = subprocess.run(["go", "env", "GOROOT"], capture_output=True, text=True, timeout=60).stdout.strip()
        return (Path(goroot) / "LICENSE").read_text(encoding="utf-8")
    except (OSError, subprocess.SubprocessError):
        return GO_LICENSE_FALLBACK


def app_version() -> str:
    init = (ROOT / "src" / "eng_viewer" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', init).group(1)


def write_sbom(path: Path, rows: list, app_version: str):
    """A CycloneDX 1.5 software bill of materials listing every bundled component."""
    import json
    import uuid

    components = []
    for name, version, lic, _files in rows:
        name = name.strip()
        if name.startswith("ENG Drawing Viewer"):
            continue
        comp = {"type": "library", "name": name}
        if version:
            comp["version"] = version
        if lic:
            comp["licenses"] = [{"license": {"name": lic}}]
        if version and not name.startswith(("CPython", "PyInstaller", "SCOWL", "Qt ", "MuPDF", "Go ", "Microsoft",
                                            "Tesseract")):
            comp["purl"] = f"pkg:pypi/{norm(name)}@{version}"
        components.append(comp)
    doc = {
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'eng-drawing-viewer/' + app_version)}",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "ENG Drawing Viewer", "version": app_version,
                                   "licenses": [{"license": {"id": "AGPL-3.0-or-later"}}]}},
        "components": components,
    }
    path.write_text(json.dumps(doc, indent=2) + "\n")


def main():
    global LIC, OUT
    args = sys.argv[1:]
    sbom = None
    if "--sbom" in args:
        i = args.index("--sbom")
        sbom = Path(args[i + 1])
        del args[i:i + 2]
    out_dir = None
    if "--out" in args:
        i = args.index("--out")
        out_dir = Path(args[i + 1]).resolve()
        del args[i:i + 2]
    if len(args) != 1:
        raise SystemExit(__doc__)
    bundle = Path(args[0]).resolve()
    windows = not (bundle / "Contents").is_dir()
    contents = bundle if windows else bundle / "Contents"
    curated = LIC / "curated"
    scowl = LIC / "SCOWL.txt"
    if out_dir is not None:
        LIC, OUT = out_dir, out_dir / "third_party"
        LIC.mkdir(parents=True, exist_ok=True)

    # Map installed file paths (and bare file names, for relocated libraries) to their package.
    by_path: dict[str, md.Distribution] = {}
    by_name: dict[str, md.Distribution] = {}
    dists = {norm(d.metadata["Name"]): d for d in md.distributions()}
    for dist in dists.values():
        for f in dist.files or []:
            key = str(PurePosixPath(str(f)))
            by_path[key] = dist
            by_name.setdefault(PurePosixPath(key).name, dist)

    shipped: dict[str, list[str]] = defaultdict(list)
    unknown: list[str] = []
    stdlib_dynload = Path(sysconfig.get_path("platstdlib")) / "lib-dynload"
    dynload = {p.name for p in stdlib_dynload.glob("*")} if stdlib_dynload.is_dir() else set()
    if windows:  # CPython's own DLLs and extension modules on Windows
        base = Path(sys.base_prefix)
        dynload |= {p.name for p in (base / "DLLs").glob("*")} | {p.name for p in base.glob("*.dll")}
    extra_texts: list[tuple[str, str]] = []

    for path in sorted(p for p in contents.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(contents).as_posix()
        if windows:
            inner = rel[len("app/_internal/"):] if rel.startswith("app/_internal/") else rel
        else:
            inner = rel.split("/", 1)[1] if rel.startswith(("Frameworks/", "Resources/")) else rel
        name = path.name
        comp = None
        if windows and (rel in ("ENG Drawing Viewer.exe", "Read Me.txt", "Licence.txt") or rel.startswith("legal/")):
            comp = "ENG Drawing Viewer (this project)"
            if rel == "ENG Drawing Viewer.exe":
                shipped["Go standard library and runtime (in the launcher)"].append(rel)
        elif windows and rel == "app/ENGDrawingViewer.exe":
            comp = "PyInstaller bootloader"
        elif windows and inner.startswith("tessdata/"):
            comp = "Tesseract language data (tessdata_fast 4.1.0)"
        elif windows and MSVC_RUNTIME.match(name):
            comp = "Microsoft Visual C++ Runtime"
        elif inner.startswith(("eng_viewer/", "legal/")) or rel in ("Info.plist", "Resources/AppIcon.icns") \
                or rel.startswith("_CodeSignature/") or name == "CodeResources":
            comp = "ENG Drawing Viewer (this project)"
        elif rel == "MacOS/ENG Drawing Viewer":
            comp = "PyInstaller bootloader"
        elif name in ("libpython3.13.dylib", "base_library.zip", "python313.dll", "python3.dll") \
                or inner.startswith("python3.13/") or name in dynload:
            comp = "CPython 3.13"
        else:
            dist = by_path.get(inner)
            if dist is None and "/" in inner:
                # PyObjC and PyInstaller hooks sometimes relocate top-level folders.
                dist = next((d for k, d in by_path.items() if k.endswith("/" + inner)), None)
            if dist is None:
                dist = by_name.get(name)
            if dist is not None:
                comp = dist.metadata["Name"]
        if comp is None:
            unknown.append(rel)
        else:
            shipped[comp].append(rel)

    # Pure-Python modules are compressed inside the executable rather than shipped as files.
    from PyInstaller.archive.readers import CArchiveReader
    owners = md.packages_distributions()
    # Packages such as pywin32 put their modules on the path through a .pth file, so
    # packages_distributions() doesn't know them; match them by the files the package installed.
    by_module: dict[str, md.Distribution] = {}
    for dist in dists.values():
        for f in dist.files or []:
            p = PurePosixPath(str(f))
            if any(x.endswith((".dist-info", ".egg-info")) or x == ".." for x in p.parts):
                continue
            if p.suffix in (".py", ".pyd", ".so"):
                by_module.setdefault(p.name.split(".")[0], dist)  # module files, e.g. win32/lib/win32con.py
            for folder in p.parts[:-1]:
                by_module.setdefault(folder, dist)  # packages and data folders, e.g. pywin32_system32
    exe = contents / "app" / "ENGDrawingViewer.exe" if windows else contents / "MacOS" / "ENG Drawing Viewer"
    carchive = CArchiveReader(str(exe))
    for name, entry in carchive.toc.items():
        if entry[-1] != "z":
            continue
        for mod in carchive.open_embedded_archive(name).toc:
            top = mod.split(".")[0]
            if top == "eng_viewer":
                comp = "ENG Drawing Viewer (this project)"
            elif top in owners:
                comp = dists[norm(owners[top][0])].metadata["Name"]
            elif top in by_module and top not in sys.stdlib_module_names:
                comp = by_module[top].metadata["Name"]
            elif top in sys.stdlib_module_names or top.startswith(("_", "pyimod", "pyi_")):
                comp = "CPython 3.13" if not top.startswith(("pyimod", "pyi_")) else "PyInstaller bootloader"
            else:
                unknown.append(f"(Python module) {mod}")
                continue
            shipped[comp].append(f"(module) {mod}")

    if unknown:
        print("These shipped files could not be matched to a component:")
        for u in unknown:
            print("  ", u)
        raise SystemExit(1)

    # Write licence files per Python package, plus the curated folders.
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    rows = []
    texts = []
    for comp in sorted(shipped, key=str.lower):
        files = shipped[comp]
        dist = dists.get(norm(comp))
        if dist is not None:
            version, lic = dist.version, licence_of(dist)
            lic_files = licence_files(dist)
            target = OUT / comp
            target.mkdir()
            for lf in lic_files:
                shutil.copy2(lf, target / lf.name)
                texts.append((f"{comp} {version}: {lf.name}", lf.read_text(encoding="utf-8", errors="replace")))
            if not lic_files:
                note = SHARED_LICENCE.get(norm(comp), f"The package ships no licence file; its licence is {lic}.")
                (target / "NOTE.txt").write_text(note + "\n")
                texts.append((f"{comp} {version}", note))
            rows.append([comp, version, lic, len(files)])
            for native, nlic, where in CURATED_NATIVE.get(norm(dist.metadata["Name"]), []):
                rows.append([f"  {native}", "", nlic, f"(in {comp})"])
        elif comp == "CPython 3.13":
            v = sys.version.split()[0]
            rows.append([f"CPython {v}", v, "PSF-2.0 and others (see LICENSE)", len(files)])
            src = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
            target = OUT / "CPython"
            target.mkdir()
            if src.is_file():
                shutil.copy2(src, target / "LICENSE.txt")
                texts.append((f"CPython {v}: LICENSE.txt", src.read_text(encoding="utf-8", errors="replace")))
        elif comp == "PyInstaller bootloader":
            pyi = dists.get("pyinstaller")
            rows.append(["PyInstaller bootloader", pyi.version if pyi else "", "GPL-2.0-or-later WITH Bootloader-exception", 1])
            if pyi is not None:
                for lf in licence_files(pyi):
                    if lf.name.upper().startswith("COPYING"):
                        extra_texts.append((f"PyInstaller bootloader: {lf.name}", lf.read_text(encoding="utf-8", errors="replace")))
        elif comp.startswith("Go standard library"):
            text = go_license()
            rows.append([comp, "", "BSD-3-Clause", len(files)])
            target = OUT / "Go"
            target.mkdir()
            (target / "LICENSE").write_text(text, encoding="utf-8")
            texts.append(("Go standard library and runtime: LICENSE", text))
        elif comp == "Microsoft Visual C++ Runtime":
            rows.append([comp, "14.x", "Microsoft Visual C++ redistributable", len(files)])
            target = OUT / "Microsoft Visual C++ Runtime"
            target.mkdir()
            (target / "NOTE.txt").write_text(MSVC_NOTE + "\n", encoding="utf-8")
            texts.append((comp, MSVC_NOTE))
        elif comp.startswith("Tesseract language data"):
            rows.append([comp, "4.1.0", "Apache-2.0", len(files)])
            lic = next((contents / "app" / "_internal" / "tessdata").glob("LICENSE*"), None)
            if lic is not None:
                target = OUT / "tessdata_fast"
                target.mkdir()
                shutil.copy2(lic, target / "LICENSE")
                texts.append((f"{comp}: LICENSE", lic.read_text(encoding="utf-8", errors="replace")))
        else:
            rows.append([comp, "", "AGPL-3.0-or-later", len(files)])

    texts.extend(extra_texts)
    if out_dir is not None and curated.is_dir():
        shutil.rmtree(LIC / "curated", ignore_errors=True)
        shutil.copytree(curated, LIC / "curated")
    for folder in sorted(p for p in curated.iterdir() if p.is_dir()) if curated.is_dir() else []:
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                texts.append((f"{folder.name}: {f.relative_to(folder)}", f.read_text(encoding="utf-8", errors="replace")))
    if scowl.is_file():
        if out_dir is not None:
            shutil.copy2(scowl, LIC / "SCOWL.txt")
        rows.append(["SCOWL word list", "2020.12.07", "SCOWL licence (permissive)", 1])
        texts.append(("SCOWL word list: SCOWL.txt", scowl.read_text(encoding="utf-8", errors="replace")))

    with (LIC / "components.csv").open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["component", "version", "licence", "files shipped"])
        w.writerows(rows)

    buf = io.StringIO()
    buf.write("ENG Drawing Viewer — third-party software\n")
    buf.write("=" * 72 + "\n\n")
    buf.write("ENG Drawing Viewer is free software under the GNU Affero General Public License v3 or\n"
              "later (see LICENSE). It includes the components below, each under its own licence.\n"
              "The complete corresponding source code of the app and of these components is\n"
              "published with each release.\n\n")
    width = max(len(r[0]) for r in rows) + 2
    for r in rows:
        buf.write(f"{r[0]:<{width}}{r[1]:<12}{r[2]}\n")
    for title, text in texts:
        buf.write("\n\n" + "=" * 72 + f"\n{title}\n" + "=" * 72 + "\n\n" + text.strip() + "\n")
    (LIC / "THIRD_PARTY_NOTICES.txt").write_text(buf.getvalue(), encoding="utf-8")
    if sbom:
        if windows:
            version = app_version()
        else:
            import plistlib
            version = plistlib.loads((contents / "Info.plist").read_bytes()).get("CFBundleShortVersionString", "")
        write_sbom(sbom, rows, version)
    total = sum(len(v) for v in shipped.values())
    print(f"Accounted for {total} shipped files in {len(shipped)} components; wrote licenses/.")


if __name__ == "__main__":
    main()
