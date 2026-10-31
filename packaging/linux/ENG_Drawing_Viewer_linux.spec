# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Linux (x86-64) application folder, packed into an AppImage.
# Build with: packaging/linux/build_linux.sh
#
# Produces <distpath>/eng-drawing-viewer/ (the executable and its _internal folder); the build script
# turns it into an AppDir (AppRun, desktop entry, AppStream metadata, icon) and the AppImage.
import os
import re
import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
RES = SRC / "eng_viewer" / "resources"
BUILD = ROOT / "build" / "linux"

TESSDATA = Path(os.environ.get("ENG_VIEWER_TESSDATA") or BUILD / "tessdata")
if not (TESSDATA / "eng.traineddata").is_file():
    raise SystemExit(f"OCR language data missing in {TESSDATA}; run tools/fetch_tessdata.py first")

datas = [(str(RES), "eng_viewer/resources")]
# Licence texts, shown by Help › About (ui/about.py looks for them in "legal").
datas += [(str(ROOT / "LICENSE"), "legal"), (str(ROOT / "licenses"), "legal/licenses")]
datas += [(str(ROOT / "build" / "legal-notices"), "legal/dependencies")]
datas += [(str(ROOT / "packaging" / "licenses"), "legal/codec-licenses")]
# Tesseract language data: PyMuPDF's MuPDF includes the Tesseract engine (Apache 2.0 models).
datas += [(str(p), "tessdata") for p in sorted(TESSDATA.glob("*.traineddata")) + [TESSDATA / "LICENSE"]]
datas += collect_data_files("pptx")  # default slide templates used by python-pptx
datas += collect_data_files("docx")  # default document template used by python-docx (PDF → Word)

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(SRC), str(ROOT / "tools")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "eng_viewer.core.render_worker",
        "eng_viewer.core.pptx_export",
        "eng_viewer.ui.mark.window",
        "eng_viewer.ui.export_dialog",
        "eng_viewer.ui.preferences",
        # Image, Vector and Edit PDF workspaces (imported when their tab first opens)
        "eng_viewer.ui.image_editor",
        "eng_viewer.ui.vector_editor",
        "eng_viewer.ui.pdf_editor",
        "eng_viewer.core.image_document",
        "eng_viewer.core.image_tools",
        "eng_viewer.core.image_export",
        "eng_viewer.core.image_ai",
        "eng_viewer.core.image_clipboard",
        "eng_viewer.core.vector_document",
        "eng_viewer.core.vector_trace",
        "eng_viewer.core.pdf_edit",
        "editor_smoke",
        "numpy", "cv2", "pillow_heif", "onnx", "onnxruntime",
        "ezdxf", "defusedxml",
        # Loaded on first use (PDF Tools tab, tool jobs in the workers)
        "eng_viewer.core.pdf_tools",
        "eng_viewer.ui.tools.workspace",
        "eng_viewer.ui.tools.panel",
        "eng_viewer.ui.tools.organizer",
        "eng_viewer.ui.tools.compare",
        "eng_viewer.platform.linux",
        "docx",
        "xlsxwriter",
        "PIL.WebPImagePlugin",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "ssl", "_ssl", "PySide6.QtDBus",
        "tkinter", "matplotlib", "pandas", "IPython", "pytest", "pytestqt", "PyInstaller",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtNetwork", "PySide6.QtOpenGL", "PySide6.QtSql",
        "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtPdf",
        "AppKit", "Foundation", "Quartz", "Vision", "objc",
        "eng_viewer.platform.macos", "eng_viewer.platform.continuity",
        "win32com", "pythoncom", "pywintypes",
    ],
    noarchive=False,
    optimize=1,
)

# Nothing network-capable ships: QtNetwork and the TLS / network-information plugins that load it,
# the TUIO touch plugin (touch input over UDP) and the VNC platform plugin (a network server).
# Also left out: Qt plugins for setups other than a desktop session (bare framebuffers, KMS,
# Wayland compositors) and the GTK theme plugin, which would bundle the build machine's GTK stack.
# Native file dialogs come from the XDG desktop portal theme (see AppRun) instead.
_LEFT_OUT = (
    "QtNetwork", "libQt6Network", "libqtuiotouchplugin", "/tls/", "/networkinformation/",
    "libqvnc", "libqvkkhrdisplay", "libqeglfs", "libqlinuxfb", "/egldeviceintegrations/",
    "/wayland-graphics-integration-server/", "libQt6WaylandCompositor", "libqgtk3",
    "/sqldrivers/", "/designer/", "/qmltooling/", "libqtvirtualkeyboardplugin",
    "/printsupport/", "libQt6PrintSupport",  # no printing in the app; CUPS would pull in Kerberos
)
a.binaries = [b for b in a.binaries if not any(n in b[0] or n in b[1] for n in _LEFT_OUT)]
a.datas = [d for d in a.datas if not any(n in d[0] or n in d[1] for n in _LEFT_OUT)]


def _host_closure(binaries):
    """Keep a library copied from the build system only if something we ship needs it, and never
    one every desktop provides (tools/check_bundle.HOST_PROVIDED). PyInstaller collects the
    dependencies of every plugin before the filter above runs, so without this the GTK stack,
    Kerberos and systemd libraries of the build machine would ride along."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_bundle", ROOT / "tools" / "check_bundle.py")
    cb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cb)
    prefix = Path(sysconfig.get_paths()["data"]).resolve()
    base_prefix = Path(sysconfig.get_config_var("prefix") or "/nonexistent").resolve()

    def from_host(src):
        p = Path(src).resolve()
        return not (p.is_relative_to(prefix) or p.is_relative_to(base_prefix))

    needed = {}
    for dest, src, _kind in binaries:
        try:
            with open(src, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        info = cb.elf_info(data) if cb.is_elf(data) else None
        needed[dest] = info["needed"] if info else []
    by_name = {Path(dest).name: dest for dest, _src, _kind in binaries}
    host = {dest for dest, src, _kind in binaries if from_host(src)}
    keep = set()
    todo = [dest for dest, _src, _kind in binaries if dest not in host]
    while todo:
        dest = todo.pop()
        for lib in needed.get(dest, []):
            target = by_name.get(lib)
            if target in host and target not in keep and not cb.HOST_PROVIDED.match(lib):
                keep.add(target)
                todo.append(target)
    dropped = sorted(Path(d).name for d in host - keep)
    print(f"Linux bundle: {len(keep)} build-system libraries kept, {len(dropped)} left to the host system")
    return [b for b in binaries if b[0] not in host or b[0] in keep]


a.binaries = _host_closure(a.binaries)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="eng-drawing-viewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # Linux: no separate windowed bootloader; the desktop entry sets Terminal=false
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="eng-drawing-viewer")
