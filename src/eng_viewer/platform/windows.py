"""Windows desktop integration (all local; nothing here touches the network).

* Taskbar identity: one AppUserModelID for the process, and per-window relaunch properties so a
  window pinned to the taskbar starts the launcher ("ENG Drawing Viewer.exe"), not the inner
  application executable.
* The launcher handshake: the launcher shows the only splash and passes the name of an event in
  ``ENG_VIEWER_READY_EVENT``; the application sets it once the main window is up.
* Single instance: a hidden message-only window receives file paths from later launches
  (``WM_COPYDATA``), so opening a drawing from Explorer reuses the running window.
* Title bars that follow the app's light/dark theme and are tinted to match the toolbar.
* Show in Explorer with the item selected (``SHOpenFolderAndSelectItems``).

Everything is called through ctypes and degrades to doing nothing if an API is missing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
APP_ID = "ca-works.ENGDrawingViewer"
# Shared with packaging/windows/launcher/main.go.
RECEIVER_CLASS = "ENGDrawingViewer.Receiver.1"
COPYDATA_MAGIC = 0x454E4731  # "ENG1"
MAX_FORWARDED_BYTES = 4 * 1024 * 1024
MAX_FORWARDED_PATHS = 2000

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    HWND_MESSAGE = wintypes.HWND(-3)
    WM_COPYDATA = 0x004A
    SMTO_ABORTIFHUNG = 0x0002

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

        @classmethod
        def parse(cls, text: str) -> "GUID":
            import uuid

            u = uuid.UUID(text)
            g = cls(u.time_low, u.time_mid, u.time_hi_version)
            g.Data4[:] = list(u.bytes[8:])
            return g

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.USHORT), ("r2", wintypes.USHORT), ("r3", wintypes.USHORT),
                    ("value", ctypes.c_void_p), ("extra", ctypes.c_void_p)]

    _user32.FindWindowExW.argtypes = (wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
    _user32.FindWindowExW.restype = wintypes.HWND
    _user32.SendMessageTimeoutW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
    _user32.SendMessageTimeoutW.restype = LRESULT
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.DefWindowProcW.restype = LRESULT
    _user32.RegisterClassExW.argtypes = (ctypes.POINTER(WNDCLASSEXW),)
    _user32.RegisterClassExW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                        wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = (wintypes.HWND,)
    _kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.OpenEventW.restype = wintypes.HANDLE
    _kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def _hwnd(widget) -> int:
    return int(widget.winId()) if widget is not None else 0


# ------------------------------------------------------------------ taskbar identity
def set_app_user_model_id() -> None:
    if not IS_WIN:
        return
    try:
        _shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
    except (AttributeError, OSError):
        pass


def launcher_path() -> str | None:
    """The launcher that started this process, if any (set by the launcher itself)."""
    path = os.environ.get("ENG_VIEWER_LAUNCHER")
    return path if path and os.path.isabs(path) and os.path.isfile(path) else None


_APPUSERMODEL = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
_PID_RELAUNCH_COMMAND, _PID_RELAUNCH_ICON, _PID_RELAUNCH_NAME, _PID_ID = 2, 3, 4, 5
_IID_IPROPERTYSTORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
VT_EMPTY, VT_LPWSTR = 0, 31


def _com_method(obj: int, index: int, restype, *argtypes):
    vtable = ctypes.cast(ctypes.c_void_p(obj), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(obj: int) -> None:
    if obj:
        _com_method(obj, 2, wintypes.ULONG)(obj)


def _window_property_store(widget) -> int:
    store = ctypes.c_void_p()
    iid = GUID.parse(_IID_IPROPERTYSTORE)
    hr = _shell32.SHGetPropertyStoreForWindow(wintypes.HWND(_hwnd(widget)), ctypes.byref(iid), ctypes.byref(store))
    return store.value if hr == 0 and store.value else 0


def set_relaunch_properties(widget, clear: bool = False) -> bool:
    """Tell the taskbar how to relaunch this window: through the launcher, with the app's name."""
    if not IS_WIN:
        return False
    launcher = launcher_path()
    if launcher is None and not clear:
        return False  # started without the launcher (tests, development): keep Windows' defaults
    try:
        store = _window_property_store(widget)
    except (AttributeError, OSError):
        return False
    if not store:
        return False
    set_value = _com_method(store, 6, ctypes.c_long, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    commit = _com_method(store, 7, ctypes.c_long)
    values = [(_PID_ID, APP_ID), (_PID_RELAUNCH_COMMAND, f'"{launcher}"' if launcher else None),
              (_PID_RELAUNCH_NAME, "ENG Drawing Viewer"), (_PID_RELAUNCH_ICON, f"{launcher},0" if launcher else None)]
    ok = True
    try:
        for pid, text in values:
            key = PROPERTYKEY(GUID.parse(_APPUSERMODEL), pid)
            buf = ctypes.create_unicode_buffer(text) if text and not clear else None
            var = PROPVARIANT(VT_LPWSTR if buf is not None else VT_EMPTY, 0, 0, 0,
                              ctypes.cast(buf, ctypes.c_void_p).value if buf is not None else None, None)
            ok = set_value(store, ctypes.byref(key), ctypes.byref(var)) == 0 and ok
        ok = commit(store) == 0 and ok
    finally:
        _release(store)
    return ok


# ------------------------------------------------------------------ launcher handshake
def started_by_launcher() -> bool:
    return IS_WIN and bool(os.environ.get("ENG_VIEWER_READY_EVENT"))


def signal_launcher_ready() -> None:
    """Let the launcher close its splash: the main window is on screen."""
    name = os.environ.get("ENG_VIEWER_READY_EVENT")
    if not IS_WIN or not name or not name.startswith("Local\\ENGDrawingViewer.Ready."):
        return
    EVENT_MODIFY_STATE = 0x0002
    handle = _kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if handle:
        _kernel32.SetEvent(handle)
        _kernel32.CloseHandle(handle)


# ------------------------------------------------------------------ single instance
def encode_paths(paths: list[str]) -> bytes:
    return "\0".join(paths).encode("utf-16-le")


def decode_paths(data: bytes) -> list[str]:
    """Paths from another launch: absolute, existing, and a bounded number of them."""
    if len(data) > MAX_FORWARDED_BYTES or len(data) % 2:
        return []
    out = []
    for p in data.decode("utf-16-le", errors="replace").split("\0"):
        if p and os.path.isabs(p) and os.path.exists(p):
            out.append(p)
        if len(out) >= MAX_FORWARDED_PATHS:
            break
    return out


def forward_to_running_instance(paths: list[str]) -> bool:
    """Hand paths to a running copy. False if there is none (or it did not answer)."""
    if not IS_WIN:
        return False
    hwnd = _user32.FindWindowExW(HWND_MESSAGE, None, RECEIVER_CLASS, None)
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return False
    if pid.value:
        _user32.AllowSetForegroundWindow(pid.value)
    payload = encode_paths([str(Path(p).resolve()) for p in paths])
    buf = ctypes.create_string_buffer(payload, len(payload) or 1)
    cds = COPYDATASTRUCT(COPYDATA_MAGIC, len(payload), ctypes.cast(buf, ctypes.c_void_p) if payload else None)
    result = ctypes.c_size_t()
    ok = _user32.SendMessageTimeoutW(hwnd, WM_COPYDATA, 0, ctypes.addressof(cds), SMTO_ABORTIFHUNG, 10000,
                                     ctypes.byref(result))
    return bool(ok) and result.value == 1


_receivers: dict[int, "InstanceReceiver"] = {}
_receiver_proc = None


def _dispatch(hwnd, message, wparam, lparam):
    receiver = _receivers.get(hwnd or 0)
    if receiver is not None and message == WM_COPYDATA and lparam:
        return receiver._copydata(lparam)
    return _user32.DefWindowProcW(hwnd, message, wparam, lparam)


class InstanceReceiver:
    """A message-only window that receives paths from later launches.

    It lives on the GUI thread, so Qt's event loop delivers its messages. ``handler`` is called
    with the received paths (possibly none: "bring the window to the front") from the event loop,
    never while the sender is still waiting.
    """

    def __init__(self, handler):
        global _receiver_proc
        self._handler = handler
        instance = _kernel32.GetModuleHandleW(None)
        if _receiver_proc is None:
            # One window procedure for the life of the process: Windows keeps calling the class's
            # procedure, so it must never be freed.
            _receiver_proc = WNDPROC(_dispatch)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = _receiver_proc
            wc.hInstance = instance
            wc.lpszClassName = RECEIVER_CLASS
            if not _user32.RegisterClassExW(ctypes.byref(wc)):
                raise OSError("could not register the window class")
        self.hwnd = _user32.CreateWindowExW(0, RECEIVER_CLASS, "ENG Drawing Viewer", 0, 0, 0, 0, 0, HWND_MESSAGE,
                                            None, instance, None)
        if not self.hwnd:
            raise OSError("could not create the message window")
        _receivers[self.hwnd] = self

    def _copydata(self, lparam) -> int:
        cds = COPYDATASTRUCT.from_address(lparam)
        if cds.dwData != COPYDATA_MAGIC or cds.cbData > MAX_FORWARDED_BYTES:
            return 0
        data = ctypes.string_at(cds.lpData, cds.cbData) if cds.cbData and cds.lpData else b""
        paths = decode_paths(data)
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda: self._handler(paths))
        return 1

    def close(self):
        if self.hwnd:
            _receivers.pop(self.hwnd, None)
            _user32.DestroyWindow(self.hwnd)
            self.hwnd = None


# ------------------------------------------------------------------ title bars
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19  # Windows 10 1809–1909
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34  # Windows 11
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


def _colorref(hex_colour: str) -> int:
    h = hex_colour.lstrip("#")[:6]
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


def style_title_bar(widget, dark: bool, caption: str | None = None, text: str | None = None,
                    border: str | None = None) -> bool:
    """Dark or light title bar (Windows 10 and 11); on Windows 11 also tinted to ``caption``."""
    if not IS_WIN:
        return False
    try:
        dwm = ctypes.WinDLL("dwmapi")
    except OSError:
        return False
    hwnd = wintypes.HWND(_hwnd(widget))
    value = wintypes.BOOL(bool(dark))
    ok = dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), 4) == 0
    if not ok:
        ok = dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, ctypes.byref(value), 4) == 0
    for attribute, colour in ((DWMWA_CAPTION_COLOR, caption), (DWMWA_TEXT_COLOR, text), (DWMWA_BORDER_COLOR, border)):
        if colour:
            ref = wintypes.DWORD(_colorref(colour))
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(ref), 4)  # fails quietly on Windows 10
    return ok


# ------------------------------------------------------------------ Explorer
def reveal_in_explorer(path: str) -> bool:
    """Open the containing folder in Explorer with ``path`` selected."""
    if not IS_WIN:
        return False
    target = os.path.abspath(path)
    try:
        _ole32.CoInitializeEx(None, 0x2)  # apartment-threaded; harmless if Qt already initialised COM
        _shell32.ILCreateFromPathW.argtypes = (wintypes.LPCWSTR,)
        _shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        pidl = _shell32.ILCreateFromPathW(target)
        if pidl:
            try:
                _shell32.SHOpenFolderAndSelectItems.argtypes = (ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                                                wintypes.DWORD)
                if _shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0:
                    return True
            finally:
                _shell32.ILFree.argtypes = (ctypes.c_void_p,)
                _shell32.ILFree(pidl)
    except (AttributeError, OSError):
        pass
    # Fallback: Explorer by its full path (never looked up through PATH or the current folder).
    import subprocess

    explorer = os.path.join(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows", "explorer.exe")
    try:
        subprocess.Popen(f'"{explorer}" /select,"{target}"', close_fds=True)
        return True
    except OSError:
        return False
