"""Measured offline encoding, target-size optimization and safe file writes."""
from __future__ import annotations

import io
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .image_document import ImageDocument, _dimensions, _number

FORMAT_ALIASES = {"JPG": "JPEG", "TIF": "TIFF", "HEIC": "HEIF"}
QUALITY_FORMATS = {"JPEG", "WEBP", "AVIF", "HEIF"}
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "TIFF": ".tif", "WEBP": ".webp",
              "BMP": ".bmp", "GIF": ".gif", "AVIF": ".avif", "HEIF": ".heic"}


def _format(value):
    value = str(value).upper().lstrip(".")
    value = FORMAT_ALIASES.get(value, value)
    if value not in EXTENSIONS:
        raise ValueError("Unsupported image export format.")
    return value


def encode_image(image, format="PNG", quality=90, dpi=96, metadata=None):
    """Return actual encoded bytes. JPEG/BMP use white behind transparency.

    Metadata is stripped unless explicitly supplied. ICC and normalized EXIF are
    retained only in formats whose Pillow encoders support those fields.
    """
    format = _format(format)
    if format == "HEIF":
        try:
            from pillow_heif import register_heif_opener
        except ImportError as exc:
            raise ValueError("HEIC/HEIF encoding is not installed in this build.") from exc
        register_heif_opener()
    _dimensions(*image.size)
    _number(quality, "quality", 1, 100)
    _number(dpi, "DPI", 1, 100000)
    Image.init()
    if format not in Image.SAVE:
        raise ValueError(f"The {format} encoder is not installed in this build.")
    image = image.convert("RGBA")
    options = {}
    if format in QUALITY_FORMATS:
        options["quality"] = round(quality)
    if format in ("JPEG", "BMP"):
        opaque = Image.new("RGB", image.size, "white")
        opaque.paste(image, mask=image.getchannel("A"))
        image = opaque
    if format == "JPEG":
        options.update(optimize=True, subsampling=0)
    elif format == "PNG":
        options.update(optimize=True, compress_level=9)
    elif format == "TIFF":
        options["compression"] = "tiff_deflate"
    elif format == "WEBP":
        options["method"] = 4
    elif format == "GIF":
        # GIF has one-bit transparency. Reserve palette entry 255 for it.
        alpha = image.getchannel("A")
        image = image.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT)
        image.paste(255, mask=alpha.point(lambda a: 255 if a < 128 else 0))
        options.update(transparency=255, disposal=2)
    if format in ("PNG", "JPEG", "TIFF", "BMP"):
        options["dpi"] = (float(dpi), float(dpi))
    metadata = metadata or {}
    if format in ("PNG", "JPEG", "TIFF", "WEBP", "AVIF", "HEIF"):
        profile = metadata.get("icc_profile")
        if isinstance(profile, bytes):
            options["icc_profile"] = profile
        raw_exif = metadata.get("exif")
        if isinstance(raw_exif, bytes):
            exif = Image.Exif()
            exif.load(raw_exif)
            exif.pop(274, None)  # Pixel orientation was already normalized at import.
            for tag, value in ((256, image.width), (257, image.height),
                               (40962, image.width), (40963, image.height)):
                if tag in exif:
                    exif[tag] = value
            options["exif"] = exif.tobytes()
    output = io.BytesIO()
    image.save(output, format=format, **options)
    return output.getvalue()


@dataclass(frozen=True)
class ExportResult:
    data: bytes
    width: int
    height: int
    quality: int
    target_met: bool
    reason: str = ""
    format: str = "PNG"

    @property
    def size_bytes(self):
        return len(self.data)


def optimize_export(image, format="JPEG", target_bytes=None, quality=90,
                    min_quality=35, allow_resize=False, dpi=96, metadata=None):
    """Find a fitting encoding without silently changing permitted constraints.

    Quality search is bounded. Actual encoded length, never an estimate, decides
    success. Resizing occurs only when explicitly allowed and uses Lanczos.
    """
    format = _format(format)
    _number(quality, "quality", 1, 100)
    _number(min_quality, "minimum quality", 1, 100)
    quality, min_quality = round(quality), round(min_quality)
    if min_quality > quality:
        raise ValueError("Minimum quality cannot exceed the selected quality.")
    if target_bytes is not None and (type(target_bytes) is not int or target_bytes <= 0):
        raise ValueError("The target file size must be a positive number of bytes.")
    _dimensions(*image.size)
    current = image
    best = None
    for resize_attempt in range(18):
        cache = {}

        def encode(q):
            if q not in cache:
                cache[q] = encode_image(current, format, q, dpi, metadata)
            return cache[q]

        data = encode(quality)
        if target_bytes is None or len(data) <= target_bytes:
            return ExportResult(data, *current.size, quality, True, format=format)
        chosen_quality = quality
        if format in QUALITY_FORMATS:
            low_data = encode(min_quality)
            if len(low_data) <= target_bytes:
                lo, hi = min_quality, quality - 1
                chosen_quality, data = min_quality, low_data
                while lo <= hi:
                    mid = (lo + hi) // 2
                    candidate = encode(mid)
                    if len(candidate) <= target_bytes:
                        chosen_quality, data, lo = mid, candidate, mid + 1
                    else:
                        hi = mid - 1
                return ExportResult(data, *current.size, chosen_quality, True, format=format)
            data, chosen_quality = min(cache.values(), key=len), min_quality
            chosen_quality = next(q for q, encoded in cache.items() if encoded is data)
        candidate = ExportResult(data, *current.size, chosen_quality, False, format=format)
        if best is None or len(candidate.data) < len(best.data):
            best = candidate
        if not allow_resize or current.size == (1, 1):
            break
        factor = min(.85, math.sqrt(target_bytes / max(1, len(data))) * .94)
        width, height = max(1, int(current.width * factor)), max(1, int(current.height * factor))
        if (width, height) == current.size:
            break
        # Always resample the original, avoiding cumulative resize degradation.
        current = image.resize((width, height), Image.Resampling.LANCZOS)
    reason = (f"Target not met: the smallest tested output is {len(best.data):,} bytes; "
              f"the target is {target_bytes:,} bytes. "
              + ("Try a different format or a larger target." if allow_resize
                 else "Allow dimension reduction, select another format, or increase the target."))
    return ExportResult(best.data, best.width, best.height, best.quality, False, reason, format)


def _same_file(a, b):
    a, b = Path(a), Path(b)
    if a.resolve() == b.resolve():
        return True
    try:
        return os.path.samefile(a, b)
    except (FileNotFoundError, OSError):
        return False


def export_file(result, path, source_paths=(), overwrite=False):
    """Write an export atomically; never replace an original source or alias.

    Without overwrite authorization an atomic hard-link installation prevents
    races between the existence check and another writer creating the target.
    """
    path = Path(path)
    if any(_same_file(path, source) for source in source_paths if source):
        raise ValueError("Choose an output path different from every original source.")
    if path.exists() and not overwrite:
        raise FileExistsError("Output exists; explicit overwrite confirmation is required.")
    payload = result.data if isinstance(result, ExportResult) else result
    if not isinstance(payload, bytes):
        raise TypeError("Expected encoded image bytes or an ExportResult.")
    fd, temporary = tempfile.mkstemp(prefix=".eng-image-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        if overwrite:
            # Recheck aliases after encoding/writing, before replacement.
            if any(_same_file(path, source) for source in source_paths if source):
                raise ValueError("The output resolves to an original source.")
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            os.unlink(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def batch_convert(paths, output_dir, **options):
    """Convert to unique outputs without overwrites; return written paths.

    Raises on a failed input, retaining already completed outputs. Callers can
    invoke per input to report individual failures and offer cancellation.
    """
    sources = [Path(path) for path in paths]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    format = _format(options.get("format", "JPEG"))
    options["format"] = format
    keep_metadata = options.pop("preserve_metadata", False)
    outputs = []
    for source in sources:
        document = ImageDocument.from_file(source)
        encode_options = dict(options)
        encode_options.setdefault("dpi", document.dpi)
        if keep_metadata:
            encode_options["metadata"] = document.metadata
        result = optimize_export(document.composite(), **encode_options)
        if not result.target_met:
            raise ValueError(f"{source.name}: {result.reason}")
        index = 1
        while True:
            suffix = "_converted" if index == 1 else f"_converted_{index}"
            path = output_dir / (source.stem + suffix + EXTENSIONS[format])
            try:
                outputs.append(export_file(result, path, source_paths=sources))
                break
            except FileExistsError:
                index += 1
    return outputs


def supported_export_formats():
    """Probe this installed build's codecs instead of promising unavailable ones."""
    image = Image.new("RGBA", (2, 2), (20, 50, 90, 180))
    supported = []
    for format in EXTENSIONS:
        try:
            data = encode_image(image, format)
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
                if decoded.size == image.size:
                    supported.append(format)
        except (OSError, ValueError, KeyError):
            continue
    return supported
