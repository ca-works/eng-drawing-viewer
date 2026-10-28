"""Windows-specific integration.

* A stable AppUserModelID so the taskbar groups windows under the app icon.
* Signals the native launcher (``launcher.go``) that the main window is up so
  it can close its splash immediately, via a named event it passes in the
  ``ENG_DRAWING_VIEWER_READY_EVENT`` environment variable.
"""
from __future__ import annotations

import os
import sys

APP_ID = "ENGDrawingViewer.Desktop.2"


def set_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def signal_launcher_ready() -> None:
    if sys.platform != "win32":
        return
    name = os.environ.get("ENG_DRAWING_VIEWER_READY_EVENT")
    if not name:
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenEventW.restype = wintypes.HANDLE
        kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        EVENT_MODIFY_STATE = 0x0002
        handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
        if handle:
            kernel32.SetEvent(handle)
            kernel32.CloseHandle(handle)
    except Exception:
        pass
