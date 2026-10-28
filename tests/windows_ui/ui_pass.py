"""Scripted user-interface pass over the packaged Windows app (CI only; not part of pytest).

    python tests/windows_ui/ui_pass.py "release/windows/ENG Drawing Viewer" OUTPUT_FOLDER

Starts the app through its launcher the way a user would, drives it with the keyboard (pywinauto),
and saves screenshots for review: light and dark, Fusion and Qt's windows11 style, and 125/150/175 %
scaling (emulated with QT_SCALE_FACTOR, since a CI machine's display scale can't be changed). It also
checks single-instance file forwarding, Show in Explorer, the absence of a second splash, and the
app's menus and dialogs. Writes OUTPUT_FOLDER/ui_pass.json and exits non-zero if a check failed.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab
from pywinauto import Desktop, keyboard

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from corpus import make_corpus  # noqa: E402

APP_DIR = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
LAUNCHER = APP_DIR / "ENG Drawing Viewer.exe"
CORE = APP_DIR / "app" / "ENGDrawingViewer.exe"
SETTINGS = Path(os.environ["APPDATA"]) / "ENG Drawing Viewer" / "settings.json"
results: dict = {"checks": {}, "screenshots": [], "startup_windows": []}
user32 = ctypes.WinDLL("user32")


def check(name: str, ok: bool, detail=""):
    results["checks"][name] = {"ok": bool(ok), "detail": detail}
    print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)


def clean_env(extra: dict | None = None) -> dict:
    """The environment of an ordinary PC: no Python on PATH, nothing Python- or Qt-specific."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("PYTHON", "QT_", "VIRTUAL_ENV", "PIP_", "UV_", "CONDA"))}
    env["PATH"] = ";".join(p for p in env.get("PATH", "").split(";")
                           if p and not any(x in p.lower() for x in ("python", "pipx", "\\.venv", "hostedtoolcache", "uv")))
    env.update(extra or {})
    return env


def app_pids() -> set[int]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"Get-Process | Where-Object {{ $_.Path -eq '{CORE}' }} | ForEach-Object {{ $_.Id }}"],
                         capture_output=True, text=True).stdout
    return {int(x) for x in out.split()}


def app_windows():
    pids = app_pids()
    return [w for w in Desktop(backend="win32").windows(visible_only=True) if w.process_id() in pids]


def is_main_title(title: str) -> bool:
    # "ENG Drawing Viewer" or "<drawing> — ENG Drawing Viewer"; dialogs are "<name> - ENG Drawing Viewer"
    # and MARK windows start with "MARK".
    return (title == "ENG Drawing Viewer" or title.endswith(" — ENG Drawing Viewer")) and not title.startswith("MARK")


def main_window(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        for w in app_windows():
            if is_main_title(w.window_text()) and w.rectangle().width() > 400:
                return w
        time.sleep(0.2)
    raise RuntimeError("the main window did not appear")


def shot(name: str, settle: float = 1.2):
    time.sleep(settle)
    path = OUT / f"{name}.png"
    ImageGrab.grab(all_screens=True).save(path)
    results["screenshots"].append(path.name)


def kill_app():
    subprocess.run(["taskkill", "/F", "/T", "/IM", CORE.name], capture_output=True)
    time.sleep(1.5)


def write_settings(**values):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    data.update(values)
    SETTINGS.write_text(json.dumps(data), encoding="utf-8")


def launch(args: list[str], extra_env: dict | None = None, watch_startup=False):
    proc = subprocess.Popen([str(LAUNCHER), *args], env=clean_env(extra_env))
    seen = set()
    if watch_startup:
        # Record every visible window of the launcher and the app while starting: there must be one
        # splash (the launcher's) and then the main window, never a second splash. Startup takes well
        # under a second, so windows are listed directly (EnumWindows) every 20 ms.
        end = time.time() + 60
        exe_of: dict[int, str] = {}
        while time.time() < end:
            for pid, cls, title, w, h in visible_windows():
                if pid not in exe_of:
                    exe_of[pid] = process_path(pid)
                owner = "launcher" if pid == proc.pid else "app" if exe_of[pid].lower() == str(CORE).lower() else None
                if owner:
                    seen.add((cls, title, owner, w, h))
            if any(s[2] == "app" and s[3] > 500 for s in seen) and proc.poll() is not None:
                break
            time.sleep(0.02)
        results["startup_windows"] = sorted(seen)
    proc.wait(timeout=120)
    return proc.returncode, seen


def visible_windows():
    out = []
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            out.append((pid.value, cls.value, title.value, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(PROC(cb), 0)
    return out


def process_path(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32")
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def close_dialog(title_start: str, timeout=8):
    """Close a dialog by its window (more dependable on CI than a keypress racing its focus)."""
    end = time.time() + timeout
    while time.time() < end:
        found = [w for w in app_windows() if w.window_text().startswith(title_start)]
        if not found:
            return
        for w in found:
            try:
                w.close()
            except Exception:
                pass
        time.sleep(0.4)
    raise RuntimeError(f"dialog {title_start!r} did not close")


def press(keys: str, appeared, tries: int = 3, wait: float = 6.0) -> bool:
    """Send ``keys`` to the main window until ``appeared()`` is true. A CI desktop sometimes keeps
    the foreground elsewhere for a moment (Windows' foreground lock), and the keys are lost."""
    for attempt in range(tries):
        focus(main_window())
        keyboard.send_keys(keys)
        end = time.time() + wait
        while time.time() < end:
            if appeared():
                if attempt:
                    results.setdefault("retried_keys", []).append(f"{keys} (attempt {attempt + 1})")
                return True
            time.sleep(0.2)
    return False


def focus(w):
    try:
        w.set_focus()
    except Exception:
        pass
    time.sleep(0.3)


def explorer_windows():
    return [w for w in Desktop(backend="win32").windows(class_name="CabinetWClass", visible_only=True)]


def run_full(corpus_root: Path, pdf: Path, other_pdf: Path):
    kill_app()
    write_settings(theme="light")
    code, seen = launch([str(corpus_root)], watch_startup=True)
    check("launcher exits 0 after the window is up", code == 0, f"exit {code}")
    win = main_window()
    # The app's own splash is a small frameless window (380 × 236 at 100 %); the main window is large.
    splashes = [s for s in seen if s[2] == "app" and s[3] <= 520 and s[4] <= 340]
    check("no second splash from the app", not splashes, str(splashes))
    check("launcher showed its splash", any(s[0] == "ENGDrawingViewer.Splash.1" for s in seen), str(sorted(seen)))
    focus(win)
    win.maximize()
    shot("01-main-light")
    keyboard.send_keys("%f")
    shot("02-file-menu-mnemonics", 0.8)
    keyboard.send_keys("{ESC}{ESC}")
    ok = press("{F1}", lambda: any(w.window_text().startswith("Keyboard Shortcuts") for w in app_windows()))
    shot("03-shortcuts-dialog")
    check("F1 opens Keyboard Shortcuts", ok)
    close_dialog("Keyboard Shortcuts")
    focus(main_window())
    ok = press("^,", lambda: any(w.window_text().startswith("Settings") for w in app_windows()))
    shot("04-settings-dialog")
    check("Ctrl+, opens Settings", ok)
    close_dialog("Settings")
    focus(main_window())
    keyboard.send_keys("^+t")
    shot("05-pdf-tools")
    keyboard.send_keys("^+v")
    time.sleep(0.5)

    # Single instance: a second launch hands its file to the running window.
    def main_windows():
        return [w for w in app_windows() if is_main_title(w.window_text()) and w.rectangle().width() > 400]

    t0 = time.time()
    code, _ = launch([str(other_pdf)])
    deadline = time.time() + 15
    while time.time() < deadline and other_pdf.stem not in main_window().window_text():
        time.sleep(0.2)
    title = main_window().window_text()
    check("second launch is forwarded to the running copy", code == 0 and other_pdf.stem in title,
          f"exit {code}, title {title!r}, {time.time() - t0:.1f} s")
    check("still one main window", len(main_windows()) == 1, [w.window_text() for w in main_windows()])
    shot("06-forwarded-file")

    # Show in Explorer with the file selected.
    for w in explorer_windows():
        w.close()
    press("^+r", lambda: bool(explorer_windows()), wait=10)
    ex = explorer_windows()
    check("Show in Explorer opens the folder", bool(ex), ex[0].window_text() if ex else "no Explorer window")
    shot("07-show-in-explorer")
    for w in explorer_windows():
        w.close()

    press("^m", lambda: any(w.window_text().startswith("MARK") for w in app_windows()), wait=8)
    time.sleep(1.5)
    marks = [w for w in app_windows() if w.window_text().startswith("MARK")]
    check("MARK opens with Ctrl+M", bool(marks))
    if marks:
        focus(marks[0])
        marks[0].maximize()
        keyboard.send_keys("b")
        shot("08-mark-window")
        keyboard.send_keys("%e")
        shot("09-mark-edit-menu", 0.8)
        keyboard.send_keys("{ESC}{ESC}")
        keyboard.send_keys("^w")
        time.sleep(1)
    focus(main_window())
    keyboard.send_keys("^e")
    shot("10-export-dialog", 2.5)
    close_dialog("Export to PowerPoint")
    kill_app()


def run_variant(name: str, theme: str, env: dict, folder: Path):
    kill_app()
    write_settings(theme=theme)
    launch([str(folder)], env)
    win = main_window()
    focus(win)
    win.maximize()
    shot(f"{name}-main")
    keyboard.send_keys("^,")
    shot(f"{name}-settings")
    close_dialog("Settings")
    focus(main_window())
    keyboard.send_keys("^m")
    time.sleep(3)
    marks = [w for w in app_windows() if w.window_text().startswith("MARK")]
    if marks:
        focus(marks[0])
        marks[0].maximize()
        shot(f"{name}-mark")
    kill_app()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    corpus = make_corpus(Path(os.environ.get("RUNNER_TEMP", OUT)) / "ui-drawings")
    root = Path(corpus["root"])
    pdfs = sorted(root.rglob("*.pdf"))
    other = next(p for p in pdfs if "Gearbox" in p.name) if any("Gearbox" in p.name for p in pdfs) else pdfs[-1]
    try:
        run_full(root, pdfs[0], other)
    except Exception as exc:
        check("full interaction pass", False, repr(exc))
        shot("error-full")
    for name, theme, env in (
        ("11-dark", "dark", {}),
        ("12-light-fusion", "light", {"ENG_VIEWER_STYLE": "Fusion"}),
        ("13-dark-fusion", "dark", {"ENG_VIEWER_STYLE": "Fusion"}),
        ("14-scale-125", "light", {"QT_SCALE_FACTOR": "1.25"}),
        ("15-scale-150", "light", {"QT_SCALE_FACTOR": "1.5"}),
        ("16-scale-175", "light", {"QT_SCALE_FACTOR": "1.75"}),
    ):
        try:
            run_variant(name, theme, env, root)
        except Exception as exc:
            check(f"variant {name}", False, repr(exc))
            shot(f"error-{name}")
    kill_app()
    (OUT / "ui_pass.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    failed = [k for k, v in results["checks"].items() if not v["ok"]]
    print(f"{len(results['checks']) - len(failed)} checks passed, {len(failed)} failed; "
          f"{len(results['screenshots'])} screenshots in {OUT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
