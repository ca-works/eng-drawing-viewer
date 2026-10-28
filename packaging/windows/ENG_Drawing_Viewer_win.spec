# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows (x64) application folder.
# Build with: packaging/windows/build_windows.ps1
#
# Produces <distpath>/app/ENGDrawingViewer.exe and its _internal folder. The build script puts the
# launcher ("ENG Drawing Viewer.exe"), the licence texts and the Read Me next to the "app" folder.
import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32 import versioninfo as vi

ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
RES = SRC / "eng_viewer" / "resources"
BUILD = ROOT / "build" / "windows"
VERSION = re.search(r'__version__ = "([^"]+)"', (SRC / "eng_viewer" / "__init__.py").read_text()).group(1)
V4 = tuple(([int(p) for p in VERSION.split(".") if p.isdigit()] + [0, 0, 0, 0])[:4])

TESSDATA = Path(os.environ.get("ENG_VIEWER_TESSDATA") or BUILD / "tessdata")
if not (TESSDATA / "eng.traineddata").is_file():
    raise SystemExit(f"OCR language data missing in {TESSDATA}; run tools/fetch_tessdata.py first")

datas = [(str(RES), "eng_viewer/resources")]
datas += [(str(p), "tessdata") for p in sorted(TESSDATA.glob("*.traineddata")) + [TESSDATA / "LICENSE"]]
datas += collect_data_files("pptx")  # default slide templates used by python-pptx
datas += collect_data_files("docx")  # default document template used by python-docx (PDF → Word)

BUILD.mkdir(parents=True, exist_ok=True)
version_file = BUILD / "version_info.txt"
version_file.write_text(str(vi.VSVersionInfo(
    ffi=vi.FixedFileInfo(filevers=V4, prodvers=V4, mask=0x3F, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[
        vi.StringFileInfo([vi.StringTable("040904B0", [
            vi.StringStruct("CompanyName", "ca-works"),
            vi.StringStruct("FileDescription", "ENG Drawing Viewer"),
            vi.StringStruct("FileVersion", VERSION),
            vi.StringStruct("InternalName", "ENGDrawingViewer"),
            vi.StringStruct("LegalCopyright", "Copyright © 2026 ca-works. GNU AGPL v3 or later."),
            vi.StringStruct("OriginalFilename", "ENGDrawingViewer.exe"),
            vi.StringStruct("ProductName", "ENG Drawing Viewer"),
            vi.StringStruct("ProductVersion", VERSION),
        ])]),
        vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])]),
    ],
)), encoding="utf-8")
manifest = (ROOT / "packaging" / "windows" / "app.manifest").read_text(encoding="utf-8").replace(
    "@VERSION4@", ".".join(map(str, V4)))

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "eng_viewer.core.render_worker",
        "eng_viewer.core.pptx_export",
        "eng_viewer.ui.mark.window",
        "eng_viewer.ui.export_dialog",
        "eng_viewer.ui.preferences",
        "eng_viewer.ui.windows_chrome",
        # Loaded on first use (PDF Tools tab, tool jobs in the workers)
        "eng_viewer.core.pdf_tools",
        "eng_viewer.ui.tools.workspace",
        "eng_viewer.ui.tools.panel",
        "eng_viewer.ui.tools.organizer",
        "eng_viewer.ui.tools.compare",
        # Windows integrations
        "eng_viewer.platform.windows",
        "eng_viewer.platform.wincom",
        "eng_viewer.platform.windows_heic",
        "eng_viewer.platform.windows_spell",
        "eng_viewer.platform.windows_powerpoint",
        "win32com.client.dynamic",
        "pythoncom",
        "pywintypes",
        "win32job",
        "win32process",
        "win32event",
        "win32api",
        "win32con",
        "docx",
        "xlsxwriter",
        "PIL.WebPImagePlugin",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # No OpenSSL: _hashlib would bring libcrypto; hashlib's built-in SHA-2/BLAKE2 cover the app.
        "ssl", "_ssl", "_hashlib", "PySide6.QtDBus",
        "tkinter", "matplotlib", "numpy", "pandas", "IPython", "pytest", "pytestqt", "PyInstaller",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtNetwork", "PySide6.QtOpenGL", "PySide6.QtSql",
        "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtPdf",
        "AppKit", "Foundation", "Quartz", "Vision", "objc",
        "eng_viewer.platform.macos", "eng_viewer.platform.continuity",
        # pywin32 pieces the app never uses (the Pythonwin GUI, and modules that talk to other machines)
        "win32com.demos", "win32comext", "win32net", "win32wnet", "win32inet",
        "win32ui", "win32uiole", "dde", "pythonwin", "pywin",
    ],
    noarchive=False,
    optimize=1,
)
# No network-capable Qt library ships: QtNetwork itself, the TLS backends and network-information
# plugins (which load it), and the TUIO touch plugin (touch input received over UDP).
_NETWORK_ONLY = ("QtNetwork", "Qt6Network", "qtuiotouchplugin", "qschannelbackend", "qopensslbackend",
                 "qcertonlybackend", "qnetworklistmanager", "networkinformation", "/tls/", "\\tls\\")
a.binaries = [b for b in a.binaries if not any(n in b[0] or n in b[1] for n in _NETWORK_ONLY)]
a.datas = [d for d in a.datas if not any(n in d[0] or n in d[1] for n in _NETWORK_ONLY)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ENGDrawingViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(RES / "ENG_Drawing_Viewer.ico"),
    version=str(version_file),
    manifest=manifest,
    uac_admin=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="app")
