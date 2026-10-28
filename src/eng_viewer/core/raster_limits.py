"""Shared pre-allocation limits for raster output and image decoding."""
import math

# A hard ceiling on any single raster the app allocates or decodes. It is far above what the app
# produces on purpose (exports and copies cap themselves at 90 MP, the display at 80 MP per tile
# job), and leaves room for real engineering scans: an A0 sheet at 400 DPI is about 250 MP.
MAX_PIXELS = 250_000_000
MAX_SIDE = 65_535


def check_pixels(width, height):
    if (not math.isfinite(width) or not math.isfinite(height)
            or width <= 0 or height <= 0
            or width > MAX_SIDE or height > MAX_SIDE
            or math.ceil(width) * math.ceil(height) > MAX_PIXELS):
        raise ValueError("Image exceeds the safe pixel limit; use a lower DPI or smaller image.")


def max_dpi_for(page, dpi):
    """The highest DPI not above `dpi` at which the page fits the pixel ceiling."""
    w, h = page.rect.width / 72, page.rect.height / 72
    if w <= 0 or h <= 0:
        return dpi
    fit = math.floor(min((MAX_PIXELS / (w * h)) ** 0.5, (MAX_SIDE - 2) / max(w, h)))
    return max(1, min(dpi, fit))


def render_page(page, *, dpi, **kwargs):
    if not math.isfinite(dpi) or dpi <= 0:
        raise ValueError("DPI must be positive and finite.")
    check_pixels(math.ceil(page.rect.width * dpi / 72) + 2,
                 math.ceil(page.rect.height * dpi / 72) + 2)
    return page.get_pixmap(dpi=dpi, **kwargs)
