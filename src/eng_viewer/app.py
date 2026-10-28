"""Application entry point."""
from __future__ import annotations

import os
import sys
import time

_T0 = time.perf_counter()

from pathlib import Path  # noqa: E402

from . import APP_NAME, __version__  # noqa: E402


def resource_path(*parts: str) -> Path:
    """Resources live inside the package in source and frozen builds alike."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    candidate = base / "eng_viewer" / "resources"
    if not candidate.exists():
        candidate = Path(__file__).resolve().parent / "resources"
    return candidate.joinpath(*parts)


def log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME / "Logs"
    return Path.home() / ".cache" / APP_NAME / "logs"


def _install_excepthook():
    import traceback

    path = log_dir() / "errors.log"

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} v{__version__}\n{text}")
        except OSError:
            pass
        if sys.stderr:
            sys.stderr.write(text)

    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    from eng_viewer.core.netguard import install

    install()
    argv = list(sys.argv if argv is None else argv)
    profile = os.environ.get("ENG_VIEWER_PROFILE") == "1"
    marks: list[tuple[str, float]] = []

    def mark(name: str):
        marks.append((name, time.perf_counter() - _T0))

    _install_excepthook()
    from PySide6.QtCore import QEvent, Qt, QTimer
    from PySide6.QtGui import QIcon, QPixmap
    from PySide6.QtWidgets import QApplication

    mark("qt imported")

    class EngApplication(QApplication):
        def __init__(self, args):
            super().__init__(args)
            self.window = None
            self.pending_paths: list[Path] = []

        def event(self, e):
            if e.type() == QEvent.Type.FileOpen:
                path = Path(e.file())
                if self.window is not None:
                    self.window.route_dropped([path])
                else:
                    self.pending_paths.append(path)
                return True
            return super().event(e)

    smoke = os.environ.get("ENG_VIEWER_SMOKE_TEST") == "1"
    cli_paths = [Path(a) for a in argv[1:] if not a.startswith("-") and Path(a).exists()]
    if sys.platform == "win32":
        from .platform import windows

        windows.set_app_user_model_id()
        # Started directly rather than through the launcher (which checks this itself): hand the
        # files to a running copy instead of opening a second one.
        if not smoke and not windows.started_by_launcher() and windows.forward_to_running_instance([str(p) for p in cli_paths]):
            return 0
    app = EngApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(__version__)
    if sys.platform != "darwin":
        app.setApplicationDisplayName(APP_NAME)
        # The app's stylesheet draws most of the interface. Underneath it, Windows uses Qt's
        # "windows11" style (native Fluent check boxes, menus and scroll bars; compared with Fusion
        # on Windows Server 2022 and 2025 screenshots), and other systems Fusion.
        # ENG_VIEWER_STYLE picks another Qt style, for side-by-side comparisons.
        from PySide6.QtWidgets import QStyleFactory

        default = "windows11" if sys.platform == "win32" and "windows11" in QStyleFactory.keys() else "Fusion"
        app.setStyle(os.environ.get("ENG_VIEWER_STYLE") or default)
    mark("QApplication")

    from .core.config import Settings
    from .ui.theme import ThemeManager, configure_app_font

    settings = Settings()
    configure_app_font(app)
    theme = ThemeManager(app, settings.get("theme"))
    theme.apply()
    icon_path = resource_path("app_icon_512.png")
    app_icon = QPixmap(str(icon_path)) if icon_path.exists() else None
    if sys.platform != "darwin":
        ico = resource_path("ENG_Drawing_Viewer.ico")
        app.setWindowIcon(QIcon(str(ico)) if ico.exists() else QIcon(app_icon))
    mark("theme")

    from .ui.splash import Splash

    # The Windows launcher already shows a splash; never show a second one.
    splash = Splash(app_icon, stages=3, started=_T0,
                    enabled=not (sys.platform == "win32" and windows.started_by_launcher()))
    splash.stage("Starting the PDF renderer…")
    from .core.render_service import RenderService

    render = RenderService()
    render.warm_up(1)
    splash.complete_stage()
    mark("render service")

    splash.stage("Building the interface…")
    from .ui.main_window import MainWindow

    window = MainWindow(settings, render, app_icon)
    app.window = window
    splash.complete_stage()
    mark("main window built")

    splash.stage("Opening your last folder…")
    window.show()
    splash.complete_stage()
    splash.finish()
    mark("window shown")

    receiver = None
    if sys.platform == "win32":
        from .ui import windows_chrome

        windows_chrome.track(window)
        windows.set_relaunch_properties(window)

        def bring_forward(paths: list[str]):
            if window.isMinimized():
                window.showNormal()
            window.raise_()
            window.activateWindow()
            if paths:
                window.route_dropped([Path(p) for p in paths])

        if not smoke:
            try:
                receiver = windows.InstanceReceiver(bring_forward)
            except OSError:
                receiver = None
        windows.signal_launcher_ready()

    exit_code = {"value": 0}

    def say(text: str):
        # A windowed Windows program may have no console; smoke tests can also ask for a file.
        print(text, flush=True)
        log = os.environ.get("ENG_VIEWER_SMOKE_LOG")
        if smoke and log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")

    def report():
        for name, t in marks:
            say(f"[startup] {t * 1000:7.1f} ms  {name}")

    if smoke:
        # Packaging check: open the PDF given on the command line, wait until the
        # page has really been rendered by a worker process, run PDF tool jobs in
        # the (frozen) workers so their bundled libraries are proven, then quit.
        def rendered():
            mark("first page rendered")
            report()
            smoke_tools()

        def smoke_tools():
            import shutil
            import tempfile

            if not window.doc_path:
                QTimer.singleShot(200, app.quit)
                return
            tmp = Path(tempfile.mkdtemp(prefix="eng-smoke-"))
            import hashlib
            import secrets

            # Hashing and random passwords must work without OpenSSL (the Windows build has none).
            hashlib.sha256(b"smoke").hexdigest()
            secrets.token_hex(8)
            jobs = [{"op": "to_word", "path": window.doc_path, "output": str(tmp / "out.docx")},
                    {"op": "to_excel", "path": window.doc_path, "output": str(tmp / "out.xlsx")},
                    {"op": "compress", "path": window.doc_path, "output": str(tmp / "out.pdf")}]
            results = []

            def done(res):
                results.append(res)
                if len(results) < len(jobs):
                    return
                failed = [r.error for r in results if not r.ok]
                shutil.rmtree(tmp, ignore_errors=True)
                if failed:
                    say(f"[smoke] FAILED: PDF tool job: {failed[0]}")
                    exit_code["value"] = 4
                else:
                    say(f"[smoke] {len(jobs)} PDF tool jobs OK (Word, Excel, compress)")
                QTimer.singleShot(200, app.quit)

            for job in jobs:
                render.submit("tool", job, done, dedupe=False)

        def timeout():
            say("[smoke] FAILED: no page rendered within 30 s")
            exit_code["value"] = 3
            app.quit()

        window.view.pageReady.connect(rendered, Qt.ConnectionType.SingleShotConnection)
        QTimer.singleShot(30000, timeout)

    def after_show():
        paths = cli_paths + app.pending_paths
        app.pending_paths = []
        if paths:
            window.route_dropped([Path(p) for p in paths])
        elif not smoke:
            window.restore_last_session()
        render.warm_up(2)
        mark("session restored")
        if profile and not smoke:
            report()

    QTimer.singleShot(0, after_show)
    try:
        code = app.exec()
    finally:
        if receiver is not None:
            receiver.close()
        render.shutdown()
    return exit_code["value"] or code


if __name__ == "__main__":
    sys.exit(main())
