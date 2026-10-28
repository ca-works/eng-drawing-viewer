"""Shared export parameters (clipboard, PNG, PowerPoint, MARK composites)."""
from __future__ import annotations

# Upper bound for one exported raster. A1 at 300 DPI is ~70 MP; A0 at 300 DPI
# would be ~140 MP (420 MB RGB) and is reduced to this ceiling instead.
EXPORT_MAX_PIXELS = 90_000_000


def export_scale(dpi: float, page_w: float, page_h: float, max_pixels: float = EXPORT_MAX_PIXELS) -> float:
    s = dpi / 72.0
    area = max(page_w * page_h, 1e-3)
    if area * s * s > max_pixels:
        s = (max_pixels / area) ** 0.5
    return s


def reinforced_min_line(scale: float) -> float:
    """Minimum stroke width (device px) for exported rasters.

    Preserved from the original application: PDF hairlines otherwise rasterise
    as very pale grey, especially once PowerPoint scales a high-DPI image down.
    0.90 pt is slightly above a 96-DPI hairline (0.75 pt) and only affects
    extremely thin strokes; ordinary lines are unchanged.
    """
    return max(1.0, 0.90 * scale)


def dots_per_meter(scale: float) -> int:
    return int(round(max(1.0, 72.0 * scale) / 0.0254))
