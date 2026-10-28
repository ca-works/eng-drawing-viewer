"""Minimal COM plumbing for ctypes (Windows only): GUIDs, interface calls, and lifetimes.

Used by the Windows integrations that talk to system components (imaging, spelling, the shell),
so the app needs no extra binary packages for them. Imports on any platform; only call it on
Windows.
"""
from __future__ import annotations

import ctypes
import sys
import uuid
from ctypes import wintypes

IS_WIN = sys.platform == "win32"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        u = uuid.UUID(text)
        g = cls(u.time_low, u.time_mid, u.time_hi_version)
        g.Data4[:] = list(u.bytes[8:])
        return g


class PROPVARIANT(ctypes.Structure):
    """Enough of PROPVARIANT for the scalar and string values used here."""
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.USHORT), ("r2", wintypes.USHORT), ("r3", wintypes.USHORT),
                ("value", ctypes.c_uint64), ("extra", ctypes.c_uint64)]


class COMError(OSError):
    def __init__(self, what: str, hr: int):
        self.hresult = hr & 0xFFFFFFFF
        super().__init__(f"{what} failed (0x{self.hresult:08X})")


def method(obj: int, index: int, restype, *argtypes):
    """The ``index``-th function of the interface pointer ``obj``'s vtable, ready to call."""
    vtable = ctypes.cast(ctypes.c_void_p(obj), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = (ctypes.WINFUNCTYPE if IS_WIN else ctypes.CFUNCTYPE)(restype, ctypes.c_void_p, *argtypes)(vtable[index])
    return lambda *args: fn(obj, *args)


def call(obj: int, index: int, what: str, *args, argtypes=()):
    """Call an HRESULT-returning method; raise COMError on failure."""
    hr = method(obj, index, ctypes.c_long, *argtypes)(*args)
    if hr < 0:
        raise COMError(what, hr)
    return hr


def release(obj: int | None) -> None:
    if obj:
        method(obj, 2, wintypes.ULONG)()


def initialize() -> None:
    """Initialise COM on this thread (any model already chosen by Qt or others is kept)."""
    ctypes.WinDLL("ole32").CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED


def create_instance(clsid: str, iid: str) -> int:
    ole32 = ctypes.WinDLL("ole32")
    out = ctypes.c_void_p()
    CLSCTX_INPROC_SERVER = 0x1
    hr = ole32.CoCreateInstance(ctypes.byref(GUID.parse(clsid)), None, CLSCTX_INPROC_SERVER,
                                ctypes.byref(GUID.parse(iid)), ctypes.byref(out))
    if hr < 0 or not out.value:
        raise COMError("CoCreateInstance", hr)
    return out.value


class Releasing:
    """Collects interface pointers and releases them all at the end of a ``with`` block."""

    def __init__(self):
        self._objs: list[int] = []

    def __enter__(self):
        return self

    def add(self, obj: int) -> int:
        self._objs.append(obj)
        return obj

    def __exit__(self, *exc):
        for obj in reversed(self._objs):
            release(obj)
        self._objs.clear()
        return False
