"""Linux integrations: platform-neutral logic everywhere, desktop behaviour on Linux only."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

LINUX = sys.platform.startswith("linux")
linux_only = pytest.mark.skipif(not LINUX, reason="Linux only")
# Desktop entries hold POSIX paths and permission bits; Windows has neither.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths and file modes")


def _exec_split(value: str) -> list[str]:
    """Parse an Exec= value as the Desktop Entry Specification describes (not as a shell would).

    First the string-level escapes, then arguments: inside double quotes a backslash makes the next
    ", `, $ or backslash literal; "%%" is a literal percent sign.
    """
    value = value.replace("\\\\", "\\")
    args, cur, quoted, i, started = [], [], False, 0, False
    while i < len(value):
        c = value[i]
        if quoted and c == "\\" and i + 1 < len(value) and value[i + 1] in '"`$\\':
            cur.append(value[i + 1]); i += 2; continue
        if c == '"':
            quoted, started = not quoted, True
        elif c == " " and not quoted:
            if started or cur:
                args.append("".join(cur)); cur, started = [], False
        else:
            cur.append(c); started = True
        i += 1
    if started or cur:
        args.append("".join(cur))
    return [a.replace("%%", "%") for a in args]


@pytest.mark.parametrize("name", ["ENG Drawing Viewer.AppImage", "plain", "it's $HOME `x` \"q\" back\\slash",
                                  "100% sure (draft).AppImage", "Ünïcødé – 图纸.AppImage"])
def test_exec_argument_round_trips(name):
    from eng_viewer.platform.linux import exec_argument

    path = f"/home/user/Apps/{name}"
    assert _exec_split(exec_argument(path) + " %F")[:1] == [path]


@posix_only
def test_desktop_entry_is_valid_and_points_at_the_appimage(tmp_path):
    from eng_viewer.platform.linux import APP_ID, desktop_entry

    entry = desktop_entry(Path("/opt/My Apps/ENG-Drawing-Viewer.AppImage"))
    lines = entry.splitlines()
    assert lines[0] == "[Desktop Entry]"
    keys = dict(line.split("=", 1) for line in lines[1:] if line)
    assert keys["Type"] == "Application" and keys["Icon"] == APP_ID
    assert keys["StartupWMClass"] == APP_ID
    assert _exec_split(keys["Exec"]) == ["/opt/My Apps/ENG-Drawing-Viewer.AppImage", "%F"]
    assert keys["TryExec"] == "/opt/My Apps/ENG-Drawing-Viewer.AppImage"
    assert "application/pdf" in keys["MimeType"].split(";")
    assert keys["Categories"].endswith(";") and keys["MimeType"].endswith(";")


@posix_only
def test_menu_entry_install_and_remove_stay_in_xdg_data_home(tmp_path, monkeypatch):
    from eng_viewer.app import resource_path
    from eng_viewer.platform import linux

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setattr(linux.shutil, "which", lambda _name: None)  # no update-desktop-database
    appimage = tmp_path / "ENG-Drawing-Viewer.AppImage"
    appimage.write_bytes(b"\x7fELF")
    assert not linux.menu_entry_installed()
    desktop = linux.install_menu_entry(appimage, resource_path("app_icon_512.png"))
    icon = tmp_path / "share/icons/hicolor/512x512/apps" / f"{linux.APP_ID}.png"
    assert desktop == tmp_path / "share/applications" / f"{linux.APP_ID}.desktop"
    assert linux.menu_entry_installed() and icon.is_file()
    assert (desktop.stat().st_mode & 0o777) == 0o644
    assert str(appimage) in desktop.read_text(encoding="utf-8")
    linux.remove_menu_entry()
    assert not desktop.exists() and not icon.exists()
    linux.remove_menu_entry()  # removing twice is harmless
    assert sorted(p.name for p in (tmp_path / "share").rglob("*") if p.is_file()) == []


def test_relative_xdg_data_home_is_ignored(monkeypatch):
    from eng_viewer.platform import linux

    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    assert linux.data_home() == Path.home() / ".local" / "share"


def test_show_items_command_prefers_gdbus_and_encodes_the_uri(tmp_path, monkeypatch):
    from eng_viewer.platform import linux

    target = tmp_path / "Sheet 1 #A.pdf"
    target.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    cmd = linux.show_items_command(target)
    assert cmd[0] == "/usr/bin/gdbus" and "org.freedesktop.FileManager1.ShowItems" in cmd
    assert f"['{target.as_uri()}']" in cmd and "%20" in target.as_uri() and "%23" in target.as_uri()
    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/dbus-send" if name == "dbus-send" else None)
    assert linux.show_items_command(target)[-2] == f"array:string:{target.as_uri()}"
    monkeypatch.setattr(linux.shutil, "which", lambda name: None)
    assert linux.show_items_command(target) is None


def test_host_programs_get_the_users_library_path(monkeypatch):
    from eng_viewer.platform import linux

    monkeypatch.setattr(linux.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIbundle")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    assert linux._host_env()["LD_LIBRARY_PATH"] == "/usr/local/lib"
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in linux._host_env()


def test_appimage_path_requires_an_existing_file(tmp_path, monkeypatch):
    from eng_viewer.platform import linux

    monkeypatch.delenv("APPIMAGE", raising=False)
    assert linux.appimage_path() is None
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "missing.AppImage"))
    assert linux.appimage_path() is None
    (tmp_path / "real.AppImage").write_bytes(b"x")
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "real.AppImage"))
    assert linux.appimage_path() == (tmp_path / "real.AppImage").resolve()


@linux_only
def test_reveal_falls_back_to_the_folder_when_dbus_fails(qapp, qtbot, tmp_path, monkeypatch):
    from eng_viewer.platform import linux

    target = tmp_path / "a.pdf"
    target.write_bytes(b"%PDF-1.4")
    opened = []
    monkeypatch.setattr(linux, "show_items_command", lambda _p: [sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(linux, "_open_folder", lambda p: opened.append(p))
    linux.reveal(str(target))
    assert opened == []  # the interface thread did not wait for the command
    qtbot.waitUntil(lambda: opened == [target], timeout=5000)


@linux_only
def test_reveal_does_not_open_the_folder_when_the_file_manager_answers(qapp, qtbot, tmp_path, monkeypatch):
    from eng_viewer.platform import linux

    target = tmp_path / "a.pdf"
    target.write_bytes(b"%PDF-1.4")
    opened = []
    monkeypatch.setattr(linux, "show_items_command", lambda _p: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(linux, "_open_folder", lambda p: opened.append(p))
    linux.reveal(str(target))
    qtbot.wait(600)
    assert opened == []


@linux_only
def test_linux_quit_shortcut(qapp):
    from PySide6.QtGui import QKeySequence

    from eng_viewer.ui.shortcuts import KEYS

    assert QKeySequence(KEYS["quit"]).toString() == "Ctrl+Q"


@linux_only
def test_heic_photos_open_with_the_bundled_decoder(tmp_path):
    """Linux has no system HEIF decoder to rely on; the viewer uses the bundled libheif."""
    pillow_heif = pytest.importorskip("pillow_heif")
    from PIL import Image

    from eng_viewer.core.documents import heic_to_png, image_pixels

    pillow_heif.register_heif_opener()
    photo = tmp_path / "site photo.heic"
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways: shown rotated 90°
    Image.new("RGB", (40, 20), "blue").save(photo, format="HEIF", quality=90, exif=exif.tobytes())
    assert image_pixels(str(photo)) == (20, 40)
    with Image.open(io.BytesIO(heic_to_png(str(photo)))) as png:
        assert png.format == "PNG" and png.size == (20, 40)


@linux_only
def test_window_identity_matches_the_desktop_entry(qapp):
    from PySide6.QtGui import QGuiApplication

    from eng_viewer.platform.linux import APP_ID

    QGuiApplication.setDesktopFileName(APP_ID)
    assert QGuiApplication.desktopFileName() == APP_ID


def _check_bundle():
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_bundle", Path(__file__).parents[1] / "tools" / "check_bundle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@linux_only
def test_elf_reader_finds_needed_libraries_and_glibc_versions():
    import PySide6

    cb = _check_bundle()
    core = next(Path(PySide6.__file__).parent.rglob("QtCore.abi3.so"))
    info = cb.elf_info(core.read_bytes())
    assert info["machine"] == cb.EM_X86_64
    assert any(n.startswith("libQt6Core.so") for n in info["needed"])
    assert cb.highest(info["versions"], "GLIBC_") is not None
    assert cb.elf_info(b"\x7fELF" + b"\0" * 10) is None  # truncated files are rejected, not misread


def test_host_provided_libraries_are_the_desktop_basics():
    cb = _check_bundle()
    for lib in ("libc.so.6", "libGL.so.1", "libEGL.so.1", "libxkbcommon.so.0", "libfontconfig.so.1",
                "libwayland-client.so.0", "libX11.so.6", "libxcb.so.1", "libstdc++.so.6"):
        assert cb.HOST_PROVIDED.match(lib), lib
    # Not on every desktop: these must be bundled.
    for lib in ("libxcb-cursor.so.0", "libxcb-icccm.so.4", "libgtk-3.so.0", "libssl.so.3", "libicuuc.so.73"):
        assert not cb.HOST_PROVIDED.match(lib), lib


def test_reviewed_findings_match_exactly(tmp_path):
    cb = _check_bundle()
    listed = tmp_path / "reviewed.txt"
    listed.write_text("# comment\n*cv2/cv2.abi3.so: home-folder path '/home/ci/' | OpenCV build path\n", encoding="utf-8")
    [(rx, why)] = cb.load_reviewed(listed)
    assert why == "OpenCV build path"
    assert rx.fullmatch("usr/lib/eng-drawing-viewer/_internal/cv2/cv2.abi3.so: home-folder path '/home/ci/'")
    assert not rx.fullmatch("usr/lib/x/cv2/cv2.abi3.so: home-folder path '/home/someone/'")
    assert not rx.fullmatch("evil path/cv2/cv2.abi3.so: home-folder path '/home/ci/'")  # "*" never spans a space


def test_shipped_reviewed_findings_file_parses():
    cb = _check_bundle()
    entries = cb.load_reviewed(Path(__file__).parents[1] / "packaging" / "reviewed-upstream-findings.txt")
    assert entries and all(why for _rx, why in entries)
