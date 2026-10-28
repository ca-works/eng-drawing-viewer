# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the macOS .app bundle (Apple Silicon / arm64).
# Build with: packaging/macos/build_macos.sh
import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
RES = SRC / "eng_viewer" / "resources"
# The one version number lives in src/eng_viewer/__init__.py.
VERSION = re.search(r'__version__ = "([^"]+)"', (SRC / "eng_viewer" / "__init__.py").read_text()).group(1)

datas = [(str(RES), "eng_viewer/resources")]
# Licence texts, shown by Help › About (ui/about.py looks for them in "legal").
datas += [(str(ROOT / "LICENSE"), "legal"), (str(ROOT / "licenses"), "legal/licenses")]
datas += collect_data_files("pptx")  # default slide templates used by python-pptx
datas += collect_data_files("docx")  # default document template used by python-docx (PDF → Word)

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
        # Loaded on first use (PDF Tools tab, tool jobs in the workers, iPhone scans)
        "eng_viewer.core.pdf_tools",
        "eng_viewer.ui.tools.workspace",
        "eng_viewer.ui.tools.panel",
        "eng_viewer.ui.tools.organizer",
        "eng_viewer.ui.tools.compare",
        "eng_viewer.platform.continuity",
        "docx",
        "xlsxwriter",
        "PIL.WebPImagePlugin",
        "AppKit",
        "Foundation",
        "Quartz",
        "Vision",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "ssl", "_ssl", "PySide6.QtDBus",
        "tkinter", "matplotlib", "numpy", "pandas", "IPython", "pytest", "pytestqt", "PyInstaller",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtNetwork", "PySide6.QtOpenGL", "PySide6.QtSql",
        "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtPdf",
        "win32com", "pythoncom", "pywintypes",
    ],
    noarchive=False,
    optimize=1,
)
# QtNetwork reaches the bundle only through Qt's TUIO touch plugin (touch input received over UDP),
# which the app never loads. Leaving both out means no network-capable Qt library ships at all.
# (QtDBus stays: QtGui links it; on macOS it is local inter-process messaging, not networking.)
_NETWORK_ONLY = ("QtNetwork", "libqtuiotouchplugin")
a.binaries = [b for b in a.binaries if not any(n in b[0] or n in b[1] for n in _NETWORK_ONLY)]
a.datas = [d for d in a.datas if not any(n in d[0] or n in d[1] for n in _NETWORK_ONLY)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ENG Drawing Viewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,  # file-open events are handled natively via QFileOpenEvent
    target_arch="arm64",
    codesign_identity=None,  # signed ad hoc by build_macos.sh
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ENG Drawing Viewer")

app = BUNDLE(
    coll,
    name="ENG Drawing Viewer.app",
    icon=str(ROOT / "assets" / "generated" / "AppIcon.icns"),
    bundle_identifier="com.engdrawingviewer.desktop",
    version=VERSION,
    info_plist={
        "CFBundleName": "ENG Drawing Viewer",
        "CFBundleDisplayName": "ENG Drawing Viewer",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "14.4",  # Qt 6.12 needs 14.4; tested on 14.8, 15.7, 26.6 and 27
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSHumanReadableCopyright": "Copyright © 2026 ca-works. Free software under the GNU AGPL v3 or later.",
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "PDF Document",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["com.adobe.pdf"],
            },
            {
                "CFBundleTypeName": "Image",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.png", "public.jpeg", "public.tiff", "public.heic", "public.heif",
                                       "org.webmproject.webp", "com.microsoft.bmp", "com.compuserve.gif"],
            },
            {
                "CFBundleTypeName": "Folder",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "None",
                "LSItemContentTypes": ["public.folder"],
            },
        ],
    },
)
