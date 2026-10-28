"""HEIC/HEIF photos on Windows, decoded by Windows itself.

Windows decodes HEIF through the Windows Imaging Component (the codec layer that WinRT's
``BitmapDecoder`` also uses) once Microsoft's free "HEIF Image Extensions" (and, for HEVC-coded
photos such as iPhone pictures, "HEVC Video Extensions") are installed from the Microsoft Store.
The app ships no codec of its own. Without the extensions, opening a HEIC file shows a message
that says what to install. Runs in render workers (no Qt).
"""
from __future__ import annotations

import ctypes
import io
from ctypes import wintypes

from ..core.raster_limits import MAX_PIXELS, check_pixels
from . import wincom

CLSID_WICImagingFactory = "{CACAF262-9370-4615-A13B-9F5539DA4C0A}"
IID_IWICImagingFactory = "{EC5EC8A9-C395-4314-9C77-54D7A935FF70}"
GUID_WICPixelFormat32bppBGRA = "{6FDDC324-4E03-4BFE-B185-3D77768DC90F}"
GENERIC_READ = 0x80000000
# IWICImagingFactory, IWICBitmapDecoder, IWICBitmapSource and IWICMetadataQueryReader methods.
FACTORY_CREATE_DECODER_FROM_FILENAME, FACTORY_CREATE_FORMAT_CONVERTER = 3, 10
DECODER_GET_FRAME = 13
SOURCE_GET_SIZE, SOURCE_COPY_PIXELS = 3, 7
FRAME_GET_METADATA_QUERY_READER = 8
CONVERTER_INITIALIZE = 8
QUERY_GET_METADATA_BY_NAME = 5
VT_UI2 = 18

# Codec missing or unable to decode this kind of HEIF.
NO_CODEC = {0x88982F50, 0x88982F07, 0x88982F8B, 0xC00D5212, 0x80004005}

MISSING_EXTENSION = ("Windows can’t decode this HEIC photo yet. Install the free “HEIF Image Extensions” and "
                     "“HEVC Video Extensions” from the Microsoft Store, then open it again.")

EXIF_TRANSPOSE = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE", 6: "ROTATE_270",
                  7: "TRANSVERSE", 8: "ROTATE_90"}


def _orientation(frame: int, scope: wincom.Releasing) -> int:
    reader = ctypes.c_void_p()
    try:
        wincom.call(frame, FRAME_GET_METADATA_QUERY_READER, "GetMetadataQueryReader", ctypes.byref(reader),
                    argtypes=(ctypes.POINTER(ctypes.c_void_p),))
    except OSError:
        return 1
    scope.add(reader.value)
    for name in ("System.Photo.Orientation", "/ifd/{ushort=274}", "/app1/ifd/{ushort=274}"):
        var = wincom.PROPVARIANT()
        try:
            wincom.call(reader.value, QUERY_GET_METADATA_BY_NAME, "GetMetadataByName", name, ctypes.byref(var),
                        argtypes=(wintypes.LPCWSTR, ctypes.POINTER(wincom.PROPVARIANT)))
        except OSError:
            continue
        if var.vt == VT_UI2 and 1 <= (var.value & 0xFFFF) <= 8:
            return var.value & 0xFFFF
    return 1


def _open(path: str, scope: wincom.Releasing) -> tuple[int, int]:
    wincom.initialize()
    factory = scope.add(wincom.create_instance(CLSID_WICImagingFactory, IID_IWICImagingFactory))
    decoder = ctypes.c_void_p()
    try:
        wincom.call(factory, FACTORY_CREATE_DECODER_FROM_FILENAME, "CreateDecoderFromFilename", path, None,
                    GENERIC_READ, 0, ctypes.byref(decoder),
                    argtypes=(wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int,
                              ctypes.POINTER(ctypes.c_void_p)))
    except wincom.COMError as exc:
        if exc.hresult in NO_CODEC:
            raise ValueError(MISSING_EXTENSION) from None
        raise ValueError("Unsupported or damaged image") from None
    scope.add(decoder.value)
    frame = ctypes.c_void_p()
    try:
        wincom.call(decoder.value, DECODER_GET_FRAME, "GetFrame", 0, ctypes.byref(frame),
                    argtypes=(wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)))
    except wincom.COMError as exc:
        raise ValueError(MISSING_EXTENSION if exc.hresult in NO_CODEC else "Unsupported or damaged image") from None
    return factory, scope.add(frame.value)


def _size(source: int) -> tuple[int, int]:
    w, h = wintypes.UINT(), wintypes.UINT()
    wincom.call(source, SOURCE_GET_SIZE, "GetSize", ctypes.byref(w), ctypes.byref(h),
                argtypes=(ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)))
    return w.value, h.value


def heic_size(path: str) -> tuple[int, int]:
    """Upright pixel size without decoding the pixels."""
    with wincom.Releasing() as scope:
        _factory, frame = _open(path, scope)
        w, h = _size(frame)
        return (h, w) if _orientation(frame, scope) in (5, 6, 7, 8) else (w, h)


def heic_to_png(path: str) -> bytes:
    """Decode the first image upright (EXIF orientation applied) and return it as PNG."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with wincom.Releasing() as scope:
        factory, frame = _open(path, scope)
        w, h = _size(frame)
        check_pixels(w, h)
        orientation = _orientation(frame, scope)
        converter = ctypes.c_void_p()
        wincom.call(factory, FACTORY_CREATE_FORMAT_CONVERTER, "CreateFormatConverter", ctypes.byref(converter),
                    argtypes=(ctypes.POINTER(ctypes.c_void_p),))
        scope.add(converter.value)
        fmt = wincom.GUID.parse(GUID_WICPixelFormat32bppBGRA)
        try:
            wincom.call(converter.value, CONVERTER_INITIALIZE, "FormatConverter.Initialize", frame, ctypes.byref(fmt),
                        0, None, ctypes.c_double(0.0), 0,
                        argtypes=(ctypes.c_void_p, ctypes.POINTER(wincom.GUID), ctypes.c_int, ctypes.c_void_p,
                                  ctypes.c_double, ctypes.c_int))
            stride = w * 4
            buf = (ctypes.c_ubyte * (stride * h))()
            wincom.call(converter.value, SOURCE_COPY_PIXELS, "CopyPixels", None, stride, len(buf), buf,
                        argtypes=(ctypes.c_void_p, wintypes.UINT, wintypes.UINT, ctypes.c_void_p))
        except wincom.COMError as exc:
            raise ValueError(MISSING_EXTENSION if exc.hresult in NO_CODEC else "Could not decode the image") from None
    im = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", stride, 1)
    if orientation in EXIF_TRANSPOSE:
        im = im.transpose(getattr(Image.Transpose, EXIF_TRANSPOSE[orientation]))
    if im.getextrema()[3] == (255, 255):
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()
