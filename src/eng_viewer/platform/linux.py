"""Linux desktop integration (all local: D-Bus on the session bus and files in the home folder).

* Show in File Manager: asks the file manager to open the folder with the file selected
  (``org.freedesktop.FileManager1.ShowItems``, supported by Files/Nautilus, Dolphin, Nemo, Caja,
  Thunar and others), and falls back to opening the folder.
* Add to Applications Menu: an AppImage is a single file the user can keep anywhere, so desktop
  menus do not know about it. On request (never automatically) the app writes a desktop entry and
  its icon under ``~/.local/share`` (XDG data home), pointing at the AppImage, and can remove them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "io.github.ca_works.ENGDrawingViewer"
APP_NAME = "ENG Drawing Viewer"
MIME_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/tiff", "image/webp", "image/bmp",
              "image/gif", "image/heic", "image/heif", "image/svg+xml", "image/vnd.dxf", "inode/directory")


def appimage_path() -> Path | None:
    """The AppImage file this copy runs from (the AppImage runtime sets $APPIMAGE), if any."""
    value = os.environ.get("APPIMAGE")
    if value and Path(value).is_file():
        return Path(value).resolve()
    return None


def data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / ".local" / "share"


def file_uri(path: Path) -> str:
    return Path(path).resolve().as_uri()


def show_items_command(path: Path) -> list[str] | None:
    """A command that asks the file manager to show ``path`` selected, or None without a D-Bus tool."""
    uri = file_uri(path)
    gdbus = shutil.which("gdbus")
    if gdbus:
        return [gdbus, "call", "--session", "--dest", "org.freedesktop.FileManager1",
                "--object-path", "/org/freedesktop/FileManager1",
                "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""]
    dbus_send = shutil.which("dbus-send")
    if dbus_send:
        return [dbus_send, "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                f"array:string:{uri}", "string:"]
    return None


def reveal(path: str) -> None:
    """Show ``path`` in the file manager, selected where the file manager supports it.

    Never waits on the interface thread: the D-Bus call runs as a separate process that a timer
    checks, and the plain folder is opened only if the call fails or does not answer in time.
    """
    p = Path(path)
    command = show_items_command(p) if p.exists() else None
    if command:
        try:
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,  # noqa: S603
                                    env=_host_env())
        except OSError:
            proc = None
        if proc is not None:
            _watch(proc, p)
            return
    _open_folder(p)


def _watch(proc: subprocess.Popen, path: Path, waited_ms: int = 0) -> None:
    from PySide6.QtCore import QTimer

    code = proc.poll()
    if code == 0:
        return
    if code is not None or waited_ms >= 3000:
        if code is None:
            proc.kill()
        _open_folder(path)
        return
    QTimer.singleShot(50, lambda: _watch(proc, path, waited_ms + 50))


def _open_folder(p: Path) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent if p.is_file() else p)))


def _host_env() -> dict[str, str]:
    """Environment for host programs, without the app's bundled libraries on their search path.

    A frozen build's bootloader points LD_LIBRARY_PATH at the bundle and keeps the user's value in
    LD_LIBRARY_PATH_ORIG (PyInstaller's documented behaviour); host tools need the original.
    """
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def exec_argument(arg: str) -> str:
    """Quote one Exec= argument as the Desktop Entry Specification requires.

    Arguments with reserved characters go in double quotes with ", `, $ and backslash escaped; "%" is
    doubled (field codes); finally every backslash is escaped again for the string value itself.
    """
    arg = arg.replace("%", "%%")
    if any(c in _RESERVED for c in arg):
        arg = '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'
    return arg.replace("\\", "\\\\")


def _string_value(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")


def desktop_entry(appimage: Path, icon_name: str = APP_ID) -> str:
    exec_path = exec_argument(str(appimage))
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "GenericName=Engineering Drawing Viewer",
        "Comment=View, mark up and convert engineering drawings offline",
        f"Exec={exec_path} %F",
        f"TryExec={_string_value(str(appimage))}",
        f"Icon={icon_name}",
        "Terminal=false",
        "Categories=Graphics;Viewer;Engineering;",
        "Keywords=PDF;drawing;markup;CAD;DXF;SVG;PowerPoint;",
        "MimeType=" + ";".join(MIME_TYPES) + ";",
        f"StartupWMClass={APP_ID}",
        "X-AppImage-Integrate=false",
        "",
    ])


def menu_entry_paths() -> tuple[Path, Path]:
    base = data_home()
    return (base / "applications" / f"{APP_ID}.desktop",
            base / "icons" / "hicolor" / "512x512" / "apps" / f"{APP_ID}.png")


def menu_entry_installed() -> bool:
    return menu_entry_paths()[0].is_file()


def install_menu_entry(appimage: Path, icon_png: Path) -> Path:
    """Write the desktop entry and icon for ``appimage``; returns the desktop file's path."""
    desktop, icon = menu_entry_paths()
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(icon_png, icon)
    desktop.parent.mkdir(parents=True, exist_ok=True)
    tmp = desktop.with_name(desktop.name + ".tmp")
    tmp.write_text(desktop_entry(appimage), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, desktop)
    _refresh_menus(desktop.parent)
    return desktop


def remove_menu_entry() -> None:
    desktop, icon = menu_entry_paths()
    for path in (desktop, icon):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    _refresh_menus(desktop.parent)


def _refresh_menus(applications: Path) -> None:
    """Most desktops watch the folder; update-desktop-database refreshes the MIME cache when present."""
    tool = shutil.which("update-desktop-database")
    if tool and applications.is_dir():
        try:
            subprocess.run([tool, "-q", str(applications)], timeout=10, check=False,  # noqa: S603
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_host_env())
        except (OSError, subprocess.SubprocessError):
            pass
