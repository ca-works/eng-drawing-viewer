"""Which files the app opens, and how they are opened (no Qt; used by workers).

PDFs and images are both "documents": an image is a one-page document (a
multi-page TIFF has several pages). MuPDF reads PNG, JPEG, TIFF, BMP and GIF
directly; WebP is decoded with Pillow, and HEIC/HEIF (iPhone photos) with
Apple's ImageIO on macOS. Files are only ever read.
"""
from __future__ import annotations

import io
import os
import sys

from .raster_limits import MAX_PIXELS, check_pixels

PDF_EXTS = frozenset({".pdf"})
NATIVE_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".bmp", ".gif"})
CONVERTED_IMAGE_EXTS = frozenset({".webp", ".heic", ".heif"})
IMAGE_EXTS = NATIVE_IMAGE_EXTS | CONVERTED_IMAGE_EXTS


def extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def kind_for(name: str) -> str | None:
    ext = extension(name)
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def is_document_name(name: str, include_images: bool = True) -> bool:
    if name.startswith("._"):
        return False  # AppleDouble resource forks left on network/FAT drives
    kind = kind_for(name)
    return kind == "pdf" or (include_images and kind == "image")


def heic_to_png(path: str) -> bytes:
    """Decode HEIC/HEIF with ImageIO, upright (EXIF orientation applied)."""
    if sys.platform != "darwin":
        raise ValueError("HEIC images can only be opened on macOS")
    import Quartz
    from Foundation import NSURL, NSMutableData

    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    if src is None:
        raise ValueError("Unsupported or damaged image")
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, 0, None) or {}
    check_pixels(int(props.get("PixelWidth", 0)), int(props.get("PixelHeight", 0)))
    longest = max(int(props.get("PixelWidth", 0) or 0), int(props.get("PixelHeight", 0) or 0), 1)
    opts = {
        Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
        Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
        Quartz.kCGImageSourceThumbnailMaxPixelSize: longest,
    }
    image = Quartz.CGImageSourceCreateThumbnailAtIndex(src, 0, opts)
    if image is None:
        raise ValueError("Unsupported or damaged image")
    data = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(data, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, image, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise ValueError("Could not decode the image")
    return bytes(data)


def _webp_to_png(path: str) -> bytes:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with Image.open(path) as im:
        check_pixels(*im.size)
        im = ImageOps.exif_transpose(im)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()


def image_pixels(path: str) -> tuple[int, int] | None:
    """Pixel size of an image file without decoding all of it (upright)."""
    if extension(path) in (".heic", ".heif") and sys.platform == "darwin":
        try:
            import Quartz
            from Foundation import NSURL

            src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
            props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, 0, None) or {}
            w, h = int(props.get("PixelWidth", 0)), int(props.get("PixelHeight", 0))
            return (h, w) if int(props.get("Orientation", 1)) in (5, 6, 7, 8) else (w, h)
        except Exception:
            return None
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        with Image.open(path) as im:
            w, h = im.size
            check_pixels(w, h)
            orientation = im.getexif().get(0x0112, 1)
            return (h, w) if orientation in (5, 6, 7, 8) else (w, h)
    except ValueError:
        raise
    except Exception as exc:
        if type(exc).__name__ == "DecompressionBombError":
            raise ValueError("Image exceeds the safe pixel limit.") from None
        return None


def open_document(pymupdf, path: str):
    """Open a PDF or image read-only, with short, friendly error messages."""
    kind = kind_for(path) or "pdf"
    if kind == "image":
        pixels = image_pixels(path)
        if pixels:
            check_pixels(*pixels)
    try:
        if kind == "pdf":
            doc = pymupdf.open(path, filetype="pdf")
        elif extension(path) in (".heic", ".heif"):
            doc = pymupdf.open(stream=heic_to_png(path), filetype="png")
        elif extension(path) == ".webp":
            doc = pymupdf.open(stream=_webp_to_png(path), filetype="png")
        else:
            doc = pymupdf.open(path)
    except ValueError as exc:
        raise ValueError(str(exc) or "Unsupported or damaged image") from None
    except Exception:
        raise ValueError("Damaged or not a valid PDF" if kind == "pdf" else "Unsupported or damaged image") from None
    if doc.needs_pass:
        doc.close()
        raise PermissionError("Password protected")
    if doc.page_count < 1:
        doc.close()
        raise ValueError("PDF has no pages" if kind == "pdf" else "Image has no frames")
    return doc
