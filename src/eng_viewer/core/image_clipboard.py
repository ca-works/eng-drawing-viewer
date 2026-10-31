"""Encoded clipboard payloads and native raster interoperability, entirely offline."""
from __future__ import annotations

import io
import struct
import sys
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageColor

from .image_export import encode_image
from .image_document import _dimensions

FORMATS = {"PNG": ("image/png", "public.png"), "JPEG": ("image/jpeg", "public.jpeg"),
           "TIFF": ("image/tiff", "public.tiff"), "BMP": ("image/bmp", "com.microsoft.bmp")}


@dataclass
class ClipboardPayload:
    format: str
    data: bytes
    image: Image.Image
    mime: str
    uti: str


def prepare_clipboard(image, options, selection=None):
    fmt = str(options.get("format", "PNG")).upper()
    if fmt not in FORMATS:
        raise ValueError("Unsupported clipboard image format")
    image = image.convert("RGBA")
    if options.get("region", "image") == "selection":
        if selection is None or selection.size != image.size or not selection.getbbox():
            raise ValueError("Create a selection before copying a selected region.")
        image.putalpha(ImageChops.multiply(image.getchannel("A"), selection))
        image = image.crop(selection.getbbox())
    scale = float(options.get("scale", 100)) / 100
    if not .01 <= scale <= 4:
        raise ValueError("Clipboard scale must be between 1% and 400%.")
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    _dimensions(*size)
    if size != image.size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    if fmt in ("JPEG", "BMP") or not options.get("transparency", True):
        bg = ImageColor.getrgb(options.get("background", "#ffffff"))
        base = Image.new("RGBA", image.size, (*bg[:3], 255))
        image = Image.alpha_composite(base, image)
    data = encode_image(image, format=fmt, quality=int(options.get("quality", 90)),
                        dpi=int(options.get("dpi", 96)))
    return ClipboardPayload(fmt, data, image, *FORMATS[fmt])


def _dibv5(image):
    """32-bit BGRA DIBV5 with explicit alpha masks and sRGB colour space."""
    w, h = image.size
    header = bytearray(124)
    struct.pack_into("<IiiHHIIiiII", header, 0, 124, w, -h, 1, 32, 3, w * h * 4, 3780, 3780, 0, 0)
    struct.pack_into("<IIIII", header, 40, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000, 0x73524742)
    struct.pack_into("<I", header, 108, 4)  # LCS_GM_IMAGES
    return bytes(header) + image.convert("RGBA").tobytes("raw", "BGRA")


def write_clipboard(payload, owner_handle=None):
    """Write the exact selected encoding and compatible native image flavors.

    Call from the GUI thread. macOS uses NSPasteboard UTI data; Windows uses
    native registered file flavors plus CF_DIBV5 alpha and CF_DIB fallback.
    """
    if sys.platform == "darwin":
        from AppKit import NSPasteboard, NSPasteboardItem
        from Foundation import NSData
        item = NSPasteboardItem.alloc().init()
        formats = {payload.uti: payload.data,
                   "public.png": payload.data if payload.format == "PNG" else encode_image(payload.image, "PNG"),
                   "public.tiff": payload.data if payload.format == "TIFF" else encode_image(payload.image, "TIFF")}
        for uti, data in formats.items():
            if not item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), uti):
                raise RuntimeError(f"The macOS clipboard rejected {uti}.")
        pb = NSPasteboard.generalPasteboard()
        if pb is None:
            raise RuntimeError("The system clipboard is unavailable in this session.")
        pb.clearContents()
        if not pb.writeObjects_([item]):
            raise RuntimeError("Could not write the system clipboard.")
    elif sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClipboardFormatW.restype = wintypes.UINT
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        png = payload.data if payload.format == "PNG" else encode_image(payload.image, "PNG")
        bmp = io.BytesIO()
        background = Image.new("RGBA", payload.image.size, "white")
        Image.alpha_composite(background, payload.image).convert("RGB").save(bmp, "BMP")
        entries = [(user32.RegisterClipboardFormatW(payload.mime), payload.data),
                   (user32.RegisterClipboardFormatW("PNG"), png),
                   (17, _dibv5(payload.image)), (8, bmp.getvalue()[14:])]
        if payload.format == "JPEG":
            entries.append((user32.RegisterClipboardFormatW("JFIF"), payload.data))
        elif payload.format == "TIFF":
            entries.append((6, payload.data))
        if not user32.OpenClipboard(owner_handle):
            raise OSError(ctypes.get_last_error(), "The clipboard is busy; try again.")
        try:
            if not user32.EmptyClipboard():
                raise OSError(ctypes.get_last_error(), "Could not clear clipboard")
            for kind, data in entries:
                if not kind:
                    raise OSError("Could not register clipboard format")
                handle = kernel32.GlobalAlloc(0x0002, len(data))
                if not handle:
                    raise MemoryError("Could not allocate clipboard memory")
                address = kernel32.GlobalLock(handle)
                if not address:
                    kernel32.GlobalFree(handle)
                    raise MemoryError("Could not lock clipboard memory")
                ctypes.memmove(address, data, len(data))
                kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(kind, handle):
                    kernel32.GlobalFree(handle)
                    raise OSError(ctypes.get_last_error(), "Could not write clipboard format")
        finally:
            user32.CloseClipboard()
    else:
        from PySide6.QtCore import QMimeData
        from PySide6.QtWidgets import QApplication
        from ..ui.image_editor.common import to_qimage
        mime = QMimeData()
        mime.setData(payload.mime, payload.data)
        mime.setImageData(to_qimage(payload.image))
        QApplication.clipboard().setMimeData(mime)
