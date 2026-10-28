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
real minimum the app can run on, and checks Info.plist against it.

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--private-terms", type=Path,
                        default=Path(__file__).resolve().parents[1] / "audit" / "private-terms.txt",
                        help="file of owner-specific terms that must not appear (kept out of the repository)")
    args = parser.parse_args()
    if args.private_terms.is_file():
        load_private_terms(args.private_terms)
    else:
        print(f"Note: {args.private_terms} not found; only the generic checks run.")
    root = args.bundle.resolve()
    findings: list[str] = []
    info: dict = {"bundle": root.name, "files": 0, "bytes": 0, "minos": {}}
    windows = (root / "app" / "ENGDrawingViewer.exe").is_file()
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
        if is_macho(data):
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

    # PySide6/shiboken6 6.12.0 wheels are tagged for macOS 14 but their binaries declare 26.0. The
    # packaged app was run on macOS 14.8, 15.7 and 26.6 (macos-compat workflow), so this label is
    # not a real requirement; the minimum is taken from everything else (Qt itself: 14.4).
    mislabelled = re.compile(r"Frameworks/(PySide6|shiboken6)/([A-Za-z0-9]+\.abi3\.so|lib(pyside6|shiboken6)\.abi3\.[0-9.]+dylib)$")
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
    info["findings"] = sorted(set(findings))
    if args.json:
        args.json.write_text(json.dumps(info, indent=2))
    print(json.dumps({k: v for k, v in info.items() if k != "findings"}, indent=2))
    for f in info["findings"][:200]:
        print("FINDING:", f)
    print(f"{len(info['findings'])} finding(s)")
    sys.exit(1 if info["findings"] else 0)


if __name__ == "__main__":
    main()
