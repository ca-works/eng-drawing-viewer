"""Offline selection, masking and retouching operations in source-pixel coordinates.

No Qt, network calls, model downloads or heavyweight initialization occurs on import.
Masks are Pillow ``L`` images (255 = selected/visible), pixels are straight RGBA.
Quick Selection is a conventional seeded color / GrabCut algorithm, not an AI model.
Its analysis image is bounded; all other operations retain the source resolution.

Colour tolerance (Magic Wand, Magic Eraser, Paint Bucket, Background Eraser) is perceptual:
a CIE ΔE*94 colour difference in CIELAB from a robust sample of the clicked colour, on a
0–100 scale where 2–3 is barely visible, 10 is a clearly different shade and 50 a different
colour. Every pixel is compared with the clicked colour, never with its neighbour, so a
region cannot drift step by step into unrelated colours.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .raster_limits import check_pixels

MAX_STROKE_POINTS = 50_000
MAX_DABS = 100_000
MAX_ANALYSIS_SIDE = 1600


def _number(value, name, low, high):
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    return value


def _size(size):
    if len(size) != 2:
        raise ValueError("Expected image width and height.")
    width, height = size
    check_pixels(width, height)
    if int(width) != width or int(height) != height:
        raise ValueError("Image dimensions must be integers.")
    return int(width), int(height)


def _point(point, size):
    if len(point) != 2:
        raise ValueError("A point needs two coordinates.")
    x, y = (float(p) for p in point)
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("Point coordinates must be finite.")
    return max(0.0, min(size[0] - 1.0, x)), max(0.0, min(size[1] - 1.0, y))


def _points(points, size):
    if not isinstance(points, Sequence) or len(points) > MAX_STROKE_POINTS:
        raise ValueError("Too many stroke points; use shorter strokes.")
    return [_point(p, size) for p in points]


def _mask(mask, size=None):
    _size(mask.size)
    if size is not None and mask.size != size:
        raise ValueError("Mask dimensions must match the image.")
    return mask if mask.mode == "L" else mask.convert("L")


def rectangle_selection(size, box, ellipse=False):
    """A rectangle/ellipse with an exclusive right and bottom edge, like Pillow crop."""
    width, height = _size(size)
    if len(box) != 4 or not all(math.isfinite(float(v)) for v in box):
        raise ValueError("A selection requires four finite coordinates.")
    x0, x1 = sorted((round(float(box[0])), round(float(box[2]))))
    y0, y1 = sorted((round(float(box[1])), round(float(box[3]))))
    x0, x1 = max(0, min(width, x0)), max(0, min(width, x1))
    y0, y1 = max(0, min(height, y0)), max(0, min(height, y1))
    result = Image.new("L", (width, height), 0)
    if x1 > x0 and y1 > y0:
        painter = ImageDraw.Draw(result)
        draw = painter.ellipse if ellipse else painter.rectangle
        draw((x0, y0, x1 - 1, y1 - 1), fill=255)
    return result


def polygon_selection(size, points):
    """Closed lasso/polygon selection. Fewer than three vertices selects nothing."""
    size = _size(size)
    vertices = _points(points, size)
    result = Image.new("L", size, 0)
    if len(vertices) >= 3:
        ImageDraw.Draw(result).polygon(vertices, fill=255)
    return result


def combine_selection(old, new, mode="replace"):
    """Combine coverage masks; a missing old selection is an empty selection."""
    new = _mask(new)
    if mode not in {"replace", "add", "subtract", "intersect"}:
        raise ValueError("Unknown selection mode.")
    if mode == "replace":
        return new.copy()
    old = Image.new("L", new.size, 0) if old is None else _mask(old, new.size)
    if mode == "add":
        return ImageChops.lighter(old, new)
    if mode == "subtract":
        return ImageChops.subtract(old, new)
    return ImageChops.darker(old, new)


DEFAULT_TOLERANCE = 16
_STRIP = 512          # rows analysed at a time, bounding temporary memory on very large images
_D_SCALE = 2.5        # distance map stores ΔE × 2.5 in a byte (0.4 ΔE steps up to ΔE 102)
_EDGE_PIXELS = 24_000_000  # larger images find edges on a reduced copy
EDGE_FLOOR = 0.8      # ΔE per pixel: the weakest boundary that may become a wall
NOISE_FACTOR = 2.5    # walls start this many times above the backdrop's own gradient noise (95th percentile)


def _lab(rgb):
    """CIELAB (L 0–100) of an sRGB uint8 array, as float32."""
    import cv2
    import numpy as np

    return cv2.cvtColor(rgb.astype(np.float32) * (1 / 255), cv2.COLOR_RGB2Lab)


def _seed(rgba, visible, x, y):
    """Robust clicked colour: the median of the visible pixels in a 5 × 5 patch.

    One noisy or JPEG-ringing pixel no longer decides what the whole click selects.
    Returns (Lab, alpha 0–1)."""
    import numpy as np

    h, w = visible.shape
    y0, y1, x0, x1 = max(0, y - 2), min(h, y + 3), max(0, x - 2), min(w, x + 3)
    patch, seen = rgba[y0:y1, x0:x1].reshape(-1, 4), visible[y0:y1, x0:x1].reshape(-1)
    if seen[(y - y0) * (x1 - x0) + (x - x0)]:
        patch = patch[seen]
    else:
        patch = rgba[y:y + 1, x:x + 1].reshape(-1, 4)
    median = np.median(patch.astype(np.float32), axis=0)
    rgb = np.clip(np.round(median[:3]), 0, 255).astype(np.uint8).reshape(1, 1, 3)
    return _lab(rgb)[0, 0], float(median[3]) / 255


def _delta_e94(lab, seed):
    """CIE ΔE*94 (graphic arts weights) from the seed colour to every pixel."""
    import numpy as np

    l1, a1, b1 = (float(v) for v in seed)
    c1 = math.hypot(a1, b1)
    dl = lab[..., 0] - l1
    da = lab[..., 1] - a1
    db = lab[..., 2] - b1
    c2 = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    dc = c1 - c2
    dh2 = np.maximum(da * da + db * db - dc * dc, 0)
    sc, sh = 1 + 0.045 * c1, 1 + 0.015 * c1
    return np.sqrt(dl * dl + (dc / sc) ** 2 + dh2 / (sh * sh))


def color_distance_map(image, point, hidden=None):
    """Quantised perceptual distance of every pixel from the clicked colour (uint8, ΔE × 2.5).

    ``hidden`` marks pixels to treat as a perfect match (the Magic Eraser passes pixels
    that are already invisible). Distance includes alpha, so a transparent click selects
    transparency regardless of the hidden RGB beneath it, and an opaque click does not
    take in transparent pixels. Returns (distance, (x, y), seed Lab or None for a
    transparent click, invisible pixels).
    """
    import numpy as np

    size = _size(image.size)
    x, y = (round(v) for v in _point(point, size))
    rgba = np.asarray(image.convert("RGBA"))
    alpha = rgba[:, :, 3]
    invisible = alpha == 0 if hidden is None else (hidden | (alpha == 0))
    seed_lab, seed_alpha = _seed(rgba, ~invisible, x, y)
    transparent_seed = seed_alpha < 0.5 / 255 or invisible[y, x]
    height = alpha.shape[0]
    distance = np.empty(alpha.shape, np.uint8)
    for top in range(0, height, _STRIP):
        rows = slice(top, min(height, top + _STRIP))
        alpha_part = alpha[rows].astype(np.float32) * (100 / 255)
        if transparent_seed:
            d = alpha_part
        else:
            d = _delta_e94(_lab(rgba[rows, :, :3]), seed_lab)
            d = np.sqrt(d * d + (alpha_part - seed_alpha * 100) ** 2)
        if hidden is not None:
            d[hidden[rows]] = 0
        distance[rows] = np.clip(np.round(d * _D_SCALE), 0, 255).astype(np.uint8)
    return distance, (x, y), None if transparent_seed else seed_lab, invisible


def edge_walls(image, invisible=None, fill_lab=None, near=None):
    """Closed one-pixel walls along visible boundaries (bool array).

    Boundaries are found in CIELAB, so a change of hue or saturation counts as much as a
    change of brightness: Canny's thin, hysteresis-linked edges on the steepest of the L,
    a and b gradients after light smoothing, plus every pixel above the strong threshold
    (Canny alone drops the weaker edge where two meet). The thresholds rise with the
    image's own noise near ``near`` (x, y), so grain and JPEG noise do not wall off a
    backdrop; a 5 × 5 closing and a one-pixel thickening bridge the small breaks left.
    Invisible pixels take ``fill_lab`` first, so earlier erasing adds no walls of its own.
    """
    import cv2
    import numpy as np

    rgba = np.asarray(image.convert("RGBA"))
    height, width = rgba.shape[:2]
    scale = min(1.0, math.sqrt(_EDGE_PIXELS / (width * height)))
    if scale < 1:
        work = (max(1, round(width * scale)), max(1, round(height * scale)))
        rgba = np.asarray(image.convert("RGBA").resize(work, Image.Resampling.BOX))
        if invisible is not None:
            invisible = cv2.resize(invisible.astype(np.uint8), work, interpolation=cv2.INTER_NEAREST) > 0
    h, w = rgba.shape[:2]
    dx = np.empty((h, w), np.int16)
    dy = np.empty((h, w), np.int16)
    pad = 6
    for top in range(0, h, _STRIP):
        a, b = max(0, top - pad), min(h, top + _STRIP + pad)
        lab = _lab(rgba[a:b, :, :3])
        if invisible is not None and fill_lab is not None:
            lab[invisible[a:b]] = fill_lab
        lab = cv2.GaussianBlur(lab, (0, 0), 1.0)
        gx = cv2.Sobel(lab, cv2.CV_32F, 1, 0, ksize=3) * 0.125
        gy = cv2.Sobel(lab, cv2.CV_32F, 0, 1, ksize=3) * 0.125
        strongest = (gx * gx + gy * gy).argmax(axis=2)[..., None]
        rows = slice(top - a, top - a + min(_STRIP, h - top))
        dx[top:top + rows.stop - rows.start] = np.clip(np.round(np.take_along_axis(gx, strongest, 2)[rows, :, 0] * 100), -32767, 32767)
        dy[top:top + rows.stop - rows.start] = np.clip(np.round(np.take_along_axis(gy, strongest, 2)[rows, :, 0] * 100), -32767, 32767)
    high = EDGE_FLOOR
    if near is not None:
        x, y = round(near[0] * scale), round(near[1] * scale)
        window = (slice(max(0, y - 40), y + 41), slice(max(0, x - 40), x + 41))
        local = np.hypot(dx[window].astype(np.float32), dy[window].astype(np.float32)) / 100
        if invisible is not None:
            local = local[~invisible[window]]
        if local.size:
            high = max(EDGE_FLOOR, NOISE_FACTOR * float(np.percentile(local, 95)))
    edges = cv2.Canny(dx, dy, high * 50, high * 100, L2gradient=True)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    # Canny thins edges to one line and drops the weaker one where two meet at a junction;
    # every pixel whose gradient clears the strong threshold is a wall as well, which keeps
    # junctions closed. One pixel thicker, a line that steps diagonally by two pixels holds too.
    strong = (dx.astype(np.float32) ** 2 + dy.astype(np.float32) ** 2) >= (high * 100) ** 2
    edges = cv2.dilate(edges | (strong.astype(np.uint8) * 255), np.ones((3, 3), np.uint8))
    if scale < 1:
        edges = cv2.dilate(cv2.resize(edges, (width, height), interpolation=cv2.INTER_NEAREST), np.ones((3, 3), np.uint8))
    return edges > 0


def _soft_boundary(rgba, core, band, distance, limit):
    """Coverage (0–1, 1 = belongs with the clicked area) of boundary-band pixels.

    Each band pixel is treated as a mix of the nearby area colour B (mean of region
    pixels in its 7 × 7 neighbourhood) and the nearby other colour F (mean of pixels
    outside the region and band): coverage = 1 − projection of (pixel − B) on (F − B).
    Local colours make this right under uneven lighting, where the clicked colour itself
    is not the backdrop's colour at the edge.
    """
    import numpy as np

    ys, xs = np.nonzero(band)
    if not len(ys):
        return ys, xs, np.zeros(0, np.float32)
    h, w = core.shape
    outside = ~(core | band)
    sum_b = np.zeros((len(ys), 3), np.float32)
    sum_f = np.zeros((len(ys), 3), np.float32)
    n_b = np.zeros(len(ys), np.float32)
    n_f = np.zeros(len(ys), np.float32)
    offsets = [(dy, dx) for dy in range(-3, 4) for dx in range(-3, 4)]
    yy = np.clip(ys[:, None] + np.array([o[0] for o in offsets])[None, :], 0, h - 1)
    xx = np.clip(xs[:, None] + np.array([o[1] for o in offsets])[None, :], 0, w - 1)
    lab = _lab(rgba[yy, xx, :3].reshape(-1, 1, 3)).reshape(len(ys), len(offsets), 3)
    in_b = core[yy, xx]
    in_f = outside[yy, xx]
    sum_b = (lab * in_b[..., None]).sum(axis=1)
    sum_f = (lab * in_f[..., None]).sum(axis=1)
    n_b, n_f = in_b.sum(axis=1), in_f.sum(axis=1)
    pixel = lab[:, len(offsets) // 2]
    # Fallback where a side has no samples: the distance ramp past the tolerance.
    fallback = np.clip(1 - (distance[ys, xs].astype(np.float32) - limit) / max(limit, 1.0), 0, 1)
    coverage = fallback.copy()
    ok = (n_b > 0) & (n_f > 0)
    b = sum_b[ok] / n_b[ok, None]
    f = sum_f[ok] / n_f[ok, None]
    v = f - b
    length2 = (v * v).sum(axis=1)
    alpha_f = np.clip(((pixel[ok] - b) * v).sum(axis=1) / np.maximum(length2, 1e-6), 0, 1)
    # Colours within ~1.5 ΔE of each other are one colour, not a boundary.
    coverage[ok] = np.where(length2 > 2.25, 1 - alpha_f, fallback[ok])
    return ys, xs, coverage


def _absorb_cells(flooded, within, walls):
    """Add small cells enclosed by the region and its walls whose colours are within tolerance.

    Texture and noise occasionally close a ring of walls inside a backdrop; the cell inside
    is backdrop, not a subject (a subject is surrounded by its own colours, not only by the
    region). Cells are the pieces left between the region and the walls, so a higher
    tolerance (a larger region) only ever adds cells: the result stays monotonic.
    """
    import cv2
    import numpy as np

    limit = max(64, int(flooded.size * 2e-5))
    count, labels, stats, _ = cv2.connectedComponentsWithStats((~(flooded | walls)).astype(np.uint8), connectivity=4)
    h, w = flooded.shape
    left, top = stats[:, cv2.CC_STAT_LEFT], stats[:, cv2.CC_STAT_TOP]
    right, bottom = left + stats[:, cv2.CC_STAT_WIDTH], top + stats[:, cv2.CC_STAT_HEIGHT]
    area = stats[:, cv2.CC_STAT_AREA]
    inside = np.bincount(labels.ravel(), weights=within.ravel(), minlength=count)
    keep = (area <= limit) & (left > 0) & (top > 0) & (right < w) & (bottom < h) & (inside >= 0.9 * area)
    keep[0] = False
    return flooded | keep[labels]


def color_region(image, point, tolerance=DEFAULT_TOLERANCE, contiguous=True, antialias=True,
                 stop_at_edges=True, hidden=None):
    """Soft coverage (uint8, 255 = fully in) of the colour area at ``point``.

    A pixel is a candidate when its perceptual difference from the clicked colour is
    within ``tolerance``. Contiguous mode keeps the candidates 4-connected to the click,
    and with ``stop_at_edges`` a visible boundary (``edge_walls``) also stops it, so a light
    garment beside a light backdrop is not flooded through a soft gap, while gradual
    lighting across the backdrop is still followed. Anti-aliasing gives a three-pixel band
    around the area partial coverage from the local colour mix (``_soft_boundary``).
    """
    import cv2
    import numpy as np

    tolerance = _number(tolerance, "Tolerance", 0, 100)
    distance, (x, y), seed_lab, invisible = color_distance_map(image, point, hidden)
    limit = round(tolerance * _D_SCALE)
    core = distance <= limit
    if contiguous and stop_at_edges and seed_lab is not None:
        walls = edge_walls(image, invisible, seed_lab, (x, y))
        if walls[y, x]:
            # A click right on a boundary starts from the nearest candidate beside it.
            near = np.argwhere(core[max(0, y - 3):y + 4, max(0, x - 3):x + 4] & ~walls[max(0, y - 3):y + 4, max(0, x - 3):x + 4])
            if len(near):
                y, x = (int(v) for v in near[np.argmin(np.abs(near - [min(y, 3), min(x, 3)]).sum(axis=1))] + [max(0, y - 3), max(0, x - 3)])
        core &= ~walls
    if contiguous:
        mask = core.astype(np.uint8)
        mask[y, x] = 1  # the clicked pixel always belongs to its own region
        cv2.floodFill(mask, None, (x, y), 2, flags=4)
        flooded = mask == 2
        if contiguous and stop_at_edges and seed_lab is not None:
            flooded = _absorb_cells(flooded, distance <= limit, walls)
        core = flooded
    coverage = np.where(core, 255, 0).astype(np.uint8)
    if antialias and core.any():
        band = (cv2.dilate(core.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=3) > 0) & ~core
        rgba = np.asarray(image.convert("RGBA"))
        if seed_lab is None:
            # Filling transparency: an anti-aliased outline's soft edge is partly covered.
            coverage[band] = 255 - rgba[:, :, 3][band]
        else:
            ys, xs, soft = _soft_boundary(rgba, core, band & ~invisible, distance, limit)
            coverage[ys, xs] = np.round(soft * 255).astype(np.uint8)
            coverage[band & invisible] = 255
    return coverage


def color_selection(image, point, tolerance=DEFAULT_TOLERANCE, contiguous=True, antialias=False):
    """Magic Wand: select the colour area at ``point`` (see ``color_region``).

    Completely transparent clicks select transparency regardless of hidden RGB.
    """
    return Image.fromarray(color_region(image, point, tolerance, contiguous, antialias, stop_at_edges=False))


def _sample_stroke(points, spacing):
    if not points:
        return
    yield points[0]
    count = 1
    for start, end in zip(points, points[1:]):
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(1, math.ceil(distance / spacing))
        count += steps
        if count > MAX_DABS:
            raise ValueError("Stroke is too long; apply it in shorter sections.")
        for step in range(1, steps + 1):
            f = step / steps
            yield start[0] + (end[0] - start[0]) * f, start[1] + (end[1] - start[1]) * f


def brush_mask(size, points, radius, hardness=0.8, opacity=1):
    """Continuous, round, maximum-coverage brush stroke, without opacity buildup.

    Radius is in source pixels; hardness is the solid inner fraction of the radius.
    A smoothstep falloff reaches zero at the outer radius. Large brush kernels are
    generated at a bounded resolution before scaling, limiting temporary memory.
    """
    import numpy as np

    size = _size(size)
    points = _points(points, size)
    radius = _number(radius, "Brush radius", 0.5, 4096)
    hardness = _number(hardness, "Brush hardness", 0, 1)
    opacity = _number(opacity, "Brush opacity", 0, 1)
    result = Image.new("L", size, 0)
    if not points or opacity == 0:
        return result
    if hardness == 1:
        # Native raster drawing is especially efficient for solid strokes.
        draw = ImageDraw.Draw(result)
        fill = round(opacity * 255)
        if len(points) > 1:
            draw.line(points, fill=fill, width=max(1, round(radius * 2)), joint="curve")
        for x, y in (points[0], points[-1]):
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
        return result
    kernel_radius = min(512, math.ceil(radius))
    yy, xx = np.ogrid[-kernel_radius:kernel_radius + 1, -kernel_radius:kernel_radius + 1]
    distance = np.sqrt(xx * xx + yy * yy) / kernel_radius
    falloff = np.clip((1 - distance) / max(1e-6, 1 - hardness), 0, 1)
    falloff = falloff * falloff * (3 - 2 * falloff)
    kernel = Image.fromarray(np.rint(falloff * opacity * 255).astype("uint8"))
    support = math.ceil(radius)
    if support != kernel_radius:
        kernel = kernel.resize((support * 2 + 1, support * 2 + 1), Image.Resampling.BICUBIC)
    stamp = np.asarray(kernel)
    pixels = np.zeros((size[1], size[0]), dtype="uint8")
    for x, y in _sample_stroke(points, max(1, radius / 4)):
        left, top = round(x) - support, round(y) - support
        x0, y0 = max(0, left), max(0, top)
        x1, y1 = min(size[0], left + stamp.shape[1]), min(size[1], top + stamp.shape[0])
        if x1 > x0 and y1 > y0:
            destination = pixels[y0:y1, x0:x1]
            np.maximum(destination, stamp[y0 - top:y1 - top, x0 - left:x1 - left], out=destination)
    return Image.fromarray(pixels)


def paint_mask(mask, points, radius, hardness=0.8, opacity=1, restore=False):
    """Nondestructively hide/restore coverage. Does not modify the caller's mask."""
    mask = _mask(mask)
    stroke = brush_mask(mask.size, points, radius, hardness, opacity)
    if restore:
        return ImageChops.screen(mask, stroke)
    return ImageChops.multiply(mask, ImageChops.invert(stroke))


def erase_pixels(image, mask):
    """Remove selected coverage to transparency; preserve RGB and existing alpha."""
    _size(image.size)
    mask = _mask(mask, image.size)
    result = image.convert("RGBA")
    result.putalpha(ImageChops.multiply(result.getchannel("A"), ImageChops.invert(mask)))
    return result


def background_erase(image, points, radius, tolerance=DEFAULT_TOLERANCE, hardness=0.8, mask=None):
    """Return a layer visibility mask, erasing the initially sampled background color.

    Sampling is fixed at the first stroke point, so dragging across a differently
    colored foreground does not continually resample and erase that foreground.
    """
    size = _size(image.size)
    points = _points(points, size)
    original = Image.new("L", size, 255) if mask is None else _mask(mask, size)
    if not points:
        return original.copy()
    colors = color_selection(image, points[0], tolerance, contiguous=False)
    stroke = brush_mask(size, points, radius, hardness)
    removal = ImageChops.multiply(colors, stroke)
    return ImageChops.multiply(original, ImageChops.invert(removal))


def magic_erase(image, point, tolerance=DEFAULT_TOLERANCE, contiguous=True, antialias=True, opacity=1.0, mask=None,
                stop_at_edges=True):
    """One-click Magic Eraser: hide the colour area at ``point`` through the layer mask.

    Uses ``color_region``: a perceptual colour difference from a robust sample of the
    clicked colour, the connected area only when ``contiguous`` (stopping at visible edges
    when ``stop_at_edges``), and a soft anti-aliased boundary. Pixels that are already
    invisible (transparent or masked) count as matches, so a background split by earlier
    erasing is still removed in one click. Returns the new layer mask; the layer's pixels
    never change, so Restore or Undo brings any area back.
    """
    import numpy as np

    size = _size(image.size)
    x, y = (round(v) for v in _point(point, size))
    opacity = _number(opacity, "Opacity", 0, 1)
    original = Image.new("L", size, 255) if mask is None else _mask(mask, size)
    keep = np.asarray(original)
    alpha = np.asarray(image.getchannel("A"))
    if keep[y, x] == 0 or alpha[y, x] == 0:
        return original.copy()
    removal = color_region(image, point, tolerance, contiguous, antialias, stop_at_edges, hidden=(keep == 0) | (alpha == 0))
    if opacity < 1:
        removal = np.round(removal.astype(np.float32) * opacity).astype(np.uint8)
    return ImageChops.multiply(original, ImageChops.invert(Image.fromarray(removal)))


def bucket_fill(image, point, color, tolerance=DEFAULT_TOLERANCE, contiguous=True, antialias=True, opacity=1.0,
                sample=None, selection=None, mask=None):
    """Paint Bucket: fill the colour area at ``point`` with ``color``.

    The area is found in ``sample`` (default: the layer itself, as seen through ``mask``)
    with the same perceptual tolerance as the Magic Wand, so a transparent click fills a
    transparent area and anti-aliased outlines are filled up to their soft edge.
    ``selection`` (layer coordinates) limits the fill. Returns ``(filled image, coverage)``;
    coverage is the fill's own alpha (``L``), useful for filling on a new layer.
    """
    import numpy as np
    from PIL import ImageColor

    size = _size(image.size)
    opacity = _number(opacity, "Opacity", 0, 1)
    rgba = ImageColor.getrgb(color) if isinstance(color, str) else tuple(color)
    if len(rgba) == 3:
        rgba = (*rgba, 255)
    base = image.convert("RGBA")
    analysed = base if sample is None else sample.convert("RGBA")
    if analysed.size != size:
        raise ValueError("The sampled image must match the layer.")
    if sample is None and mask is not None:
        analysed = base.copy()
        analysed.putalpha(ImageChops.multiply(base.getchannel("A"), _mask(mask, size)))
    coverage = color_region(analysed, point, tolerance, contiguous, antialias, stop_at_edges=False)
    coverage = coverage.astype(np.float32) * (opacity * rgba[3] / 255)
    if selection is not None:
        coverage *= np.asarray(_mask(selection, size), np.float32) / 255
    alpha = Image.fromarray(np.round(coverage).astype(np.uint8))
    paint = Image.new("RGBA", size, rgba[:3] + (255,))
    paint.putalpha(alpha)
    return Image.alpha_composite(base, paint), alpha


def _local_mix(rgba, side_b, side_f, pixels, radius=3, fallback=None, third_colour_is_f=False):
    """Two-colour coverage of ``pixels`` (bool) between nearby ``side_b`` and ``side_f`` colours.

    For each pixel, B and F are the mean CIELAB colours of side-b and side-f pixels
    sampled within ``radius`` (on a grid of at most 9 × 9 samples), and the result is how
    much of the pixel is F: the projection of (pixel − B) on (F − B), 0–1. Pixels without
    samples on both sides, or whose two sides are one colour, take ``fallback`` (0–1 per
    pixel, default 0.5). With ``third_colour_is_f`` a pixel far from the line between B
    and F (a dark outline or shading at an object's rim) counts as F. Returns (ys, xs, coverage).
    """
    import numpy as np

    ys, xs = np.nonzero(pixels)
    if not len(ys):
        return ys, xs, np.zeros(0, np.float32)
    h, w = side_b.shape
    stride = max(1, math.ceil((2 * radius + 1) / 9))
    steps = range(-radius, radius + 1, stride)
    offsets = [(dy, dx) for dy in steps for dx in steps]
    out = np.full(len(ys), 0.5, np.float32) if fallback is None else np.asarray(fallback, np.float32).copy()
    chunk = 200_000
    for start in range(0, len(ys), chunk):
        sy, sx = ys[start:start + chunk], xs[start:start + chunk]
        yy = np.clip(sy[:, None] + np.array([o[0] for o in offsets])[None, :], 0, h - 1)
        xx = np.clip(sx[:, None] + np.array([o[1] for o in offsets])[None, :], 0, w - 1)
        lab = _lab(rgba[yy, xx, :3].reshape(-1, 1, 3)).reshape(len(sy), len(offsets), 3)
        in_b, in_f = side_b[yy, xx], side_f[yy, xx]
        n_b, n_f = in_b.sum(axis=1), in_f.sum(axis=1)
        ok = (n_b > 0) & (n_f > 0)
        b = (lab * in_b[..., None]).sum(axis=1)[ok] / n_b[ok, None]
        f = (lab * in_f[..., None]).sum(axis=1)[ok] / n_f[ok, None]
        pixel = _lab(rgba[sy, sx, :3].reshape(-1, 1, 3)).reshape(-1, 3)[ok]
        v = f - b
        length2 = (v * v).sum(axis=1)
        alpha = np.clip(((pixel - b) * v).sum(axis=1) / np.maximum(length2, 1e-6), 0, 1)
        if third_colour_is_f:
            residual = np.linalg.norm(pixel - (b + alpha[:, None] * v), axis=1)
            alpha = np.where(residual > np.maximum(4.0, 0.5 * np.sqrt(length2)), 1.0, alpha)
        part = out[start:start + chunk]
        # Colours within ~1.5 ΔE of each other are one colour, not a boundary.
        part[ok] = np.where(length2 > 2.25, alpha, part[ok])
        out[start:start + chunk] = part
    return ys, xs, out


def refine_mask(image, mask, band=None):
    """Full-resolution, edge-true soft edges for a coarse or upsampled subject mask.

    Pixels within ``band`` pixels of the mask's 50 % contour are re-estimated from the
    image itself: each is treated as a mix of the nearby backdrop and subject colours
    (``_local_mix``), so the edge follows the real boundary at full resolution and partly
    covered pixels (hair, soft fabric, anti-aliasing) get partial alpha. Elsewhere the mask
    is made definite. Used for model-free removal and for offline model output alike.
    """
    import cv2
    import numpy as np

    size = _size(image.size)
    mask = _mask(mask, size)
    m = np.asarray(mask)
    if band is None:
        band = 3
    band = max(1, int(band))
    inside = m >= 128
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * band + 1, 2 * band + 1))
    grown = cv2.dilate(inside.astype(np.uint8), kernel) > 0
    shrunk = cv2.erode(inside.astype(np.uint8), kernel) > 0
    unsure = grown & ~shrunk
    rgba = np.asarray(image.convert("RGBA"))
    fallback = m[unsure].astype(np.float32) / 255
    ys, xs, alpha = _local_mix(rgba, ~grown, shrunk, unsure, radius=band + 2, fallback=fallback, third_colour_is_f=True)
    result = np.where(shrunk, 255, 0).astype(np.uint8)
    result[ys, xs] = np.round(alpha * 255).astype(np.uint8)
    # Isolated soft specks far from the subject are noise.
    result[(result < 40) & ~shrunk] = 0
    return ImageChops.multiply(Image.fromarray(result), image.convert("RGBA").getchannel("A"))


def decontaminate(image, mask, band=4):
    """Pixels whose edge colours no longer carry the old backdrop (for a new layer).

    Where the mask is partial, the pixel I = α·F + (1 − α)·B is solved for the subject
    colour F with B the nearby backdrop colour, then the pixel takes F. Returns a new RGBA
    image with the mask as its alpha; the source image is not changed.
    """
    import cv2
    import numpy as np

    size = _size(image.size)
    mask = _mask(mask, size)
    a = np.asarray(mask).astype(np.float32) / 255
    rgb = np.asarray(image.convert("RGB")).astype(np.float32)
    background = a < 0.02
    soft = (a > 0.02) & (a < 0.98)
    weights = background.astype(np.float32)
    k = 2 * (band * 2) + 1
    total = cv2.boxFilter(weights, -1, (k, k), normalize=False)
    bg = cv2.boxFilter(rgb * weights[..., None], -1, (k, k), normalize=False) / np.maximum(total, 1e-6)[..., None]
    alpha = np.maximum(a, 0.15)[..., None]
    solved = np.clip((rgb - (1 - alpha) * bg) / alpha, 0, 255)
    usable = soft & (total > 0)
    out = rgb.copy()
    out[usable] = solved[usable]
    result = Image.fromarray(np.round(out).astype(np.uint8)).convert("RGBA")
    result.putalpha(ImageChops.multiply(mask, image.convert("RGBA").getchannel("A")))
    return result


def remove_background(image, iterations=5, refine=True):
    """One-click background removal without a model, returned as a visibility mask.

    The image border is treated as background: its dominant colours (k-means) mark
    border-connected similar regions as probable background and the rest as probable
    foreground, then GrabCut refines the boundary on a bounded working image. The
    backdrop seeding stops at visible edges (``edge_walls``), so a light subject on a
    light backdrop is not seeded as backdrop through a soft gap. ``refine`` re-estimates
    the boundary at full resolution with partial alpha (``refine_mask``). Suits objects on
    a plain or distinct backdrop; it is not semantic subject detection. Raises
    ``ValueError`` when no subject separates from the border.
    """
    import cv2
    import numpy as np

    size = _size(image.size)
    scale = min(1.0, 1024 / max(size))
    work = max(1, round(size[0] * scale)), max(1, round(size[1] * scale))
    if min(work) < 8:
        raise ValueError("The image is too small for automatic background removal.")
    work_image = image.convert("RGBA").resize(work, Image.Resampling.LANCZOS)
    rgba = np.asarray(work_image)
    bgr = np.ascontiguousarray(cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR))
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    h, w = lab.shape[:2]
    edge = np.zeros((h, w), bool)
    band = max(1, round(min(h, w) * 0.01))
    edge[:band, :] = edge[-band:, :] = True
    edge[:, :band] = edge[:, -band:] = True
    samples = lab[edge]
    k = min(4, len(samples))
    _, _, centres = cv2.kmeans(samples, k, None, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5),
                               2, cv2.KMEANS_PP_CENTERS)
    distance = np.min(np.linalg.norm(lab[:, :, None, :] - centres[None, None, :, :], axis=3), axis=2)
    walls = edge_walls(work_image, rgba[:, :, 3] == 0, None, (band, band))
    similar = ((distance < 14) & ~walls).astype(np.uint8)
    count, labels = cv2.connectedComponents(similar, connectivity=4)
    touching = np.unique(labels[edge & (similar > 0)])
    background = np.isin(labels, touching[touching > 0])
    gc = np.full((h, w), cv2.GC_PR_FGD, np.uint8)
    gc[background] = cv2.GC_PR_BGD
    gc[edge & background] = cv2.GC_BGD
    # A backdrop-coloured region walled off from the backdrop but touching it across a
    # wall is part of the subject's outline (a white cap on a white sweep): GrabCut's
    # colour model alone would hand it to the backdrop, so it becomes definite subject.
    # Regions enclosed by the subject (gaps between arms) stay for GrabCut to decide.
    pale = similar.astype(bool) & ~background
    count, parts, stats, _ = cv2.connectedComponentsWithStats(pale.astype(np.uint8), connectivity=4)
    # Walls are a few pixels thick; reach across one.
    near_backdrop = cv2.dilate(background.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] >= 0.001 * h * w:
            part = parts == i
            if (part & near_backdrop).any():
                gc[part] = cv2.GC_FGD
    gc[rgba[:, :, 3] == 0] = cv2.GC_BGD
    if not np.any(gc == cv2.GC_PR_FGD):
        raise ValueError("No subject stands out from the background; use the Magic Eraser instead.")
    try:
        cv2.grabCut(bgr, gc, None, np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64),
                    int(iterations), cv2.GC_INIT_WITH_MASK)
    except cv2.error as exc:
        raise ValueError("Automatic background removal could not separate this image.") from exc
    keep = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    # Drop specks: components under 0.2 % of the image.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(keep, connectivity=8)
    small = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] < 0.002 * h * w]
    keep[np.isin(labels, small)] = 0
    if not keep.any() or keep.mean() > 254:
        raise ValueError("No subject stands out from the background; use the Magic Eraser instead.")
    result = Image.fromarray(keep).resize(size, Image.Resampling.BILINEAR)
    if refine:
        return refine_mask(image, result, band=max(2, math.ceil(1.5 / scale)))
    if scale == 1:
        result = result.filter(ImageFilter.GaussianBlur(0.6))
    return ImageChops.multiply(result, image.convert("RGBA").getchannel("A"))


def quick_selection(image, points, radius=20, existing=None, subtract=False):
    """Seeded, edge-aware selection on a bounded working image.

    Connected fixed-range color regions initialize GrabCut; the painted stroke is
    a definite foreground hint. The graph cut refines boundaries using RGB models.
    This is a useful traditional baseline, not semantic subject detection or hair
    matting. Returned masks have the original dimensions and honor source alpha.
    """
    import cv2
    import numpy as np

    size = _size(image.size)
    points = _points(points, size)
    radius = _number(radius, "Brush radius", 0.5, 4096)
    if existing is not None:
        existing = _mask(existing, size)
    if not points:
        return Image.new("L", size, 0) if existing is None else existing.copy()
    scale = min(1.0, MAX_ANALYSIS_SIDE / max(size))
    work_size = max(1, round(size[0] * scale)), max(1, round(size[1] * scale))
    rgba_image = image.convert("RGBA").resize(work_size, Image.Resampling.LANCZOS)
    rgba = np.asarray(rgba_image)
    bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    work_points = [(min(work_size[0] - 1, x * scale), min(work_size[1] - 1, y * scale)) for x, y in points]
    stroke = brush_mask(work_size, work_points, max(0.5, radius * scale), hardness=1)
    stroke_array = np.asarray(stroke)
    regions = np.zeros((work_size[1], work_size[0]), dtype="uint8")
    # Up to 24 seeds bounds graph initialization even for a long pointer path.
    stride = max(1, math.ceil(len(work_points) / 24))
    for x, y in work_points[::stride]:
        # Reuse a one-channel flood mask without copying the three-channel image.
        flood = np.zeros((work_size[1] + 2, work_size[0] + 2), dtype="uint8")
        cv2.floodFill(bgr, flood, (round(x), round(y)), (0, 0, 0),
                     (28, 28, 28), (28, 28, 28),
                     flags=4 | cv2.FLOODFILL_FIXED_RANGE | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
        np.maximum(regions, flood[1:-1, 1:-1], out=regions)
    regions[stroke_array > 0] = 255
    labels = np.full(regions.shape, cv2.GC_PR_BGD, dtype="uint8")
    labels[regions > 0] = cv2.GC_PR_FGD
    labels[stroke_array > 0] = cv2.GC_FGD
    labels[rgba[:, :, 3] == 0] = cv2.GC_BGD
    # Give GrabCut definite background only outside the color-connected region.
    # Objects touching an image edge are therefore not forcibly cut off.
    border = np.zeros(regions.shape, dtype=bool)
    border[[0, -1], :] = True
    border[:, [0, -1]] = True
    labels[border & (regions == 0)] = cv2.GC_BGD
    foreground_count = int(np.count_nonzero(labels == cv2.GC_FGD))
    background_count = int(np.count_nonzero((labels == cv2.GC_BGD) | (labels == cv2.GC_PR_BGD)))
    if foreground_count >= 5 and background_count >= 5:
        try:
            cv2.grabCut(bgr, labels, None, np.zeros((1, 65), "float64"),
                        np.zeros((1, 65), "float64"), 2, cv2.GC_INIT_WITH_MASK)
            regions = np.where((labels == cv2.GC_FGD) | (labels == cv2.GC_PR_FGD), 255, 0).astype("uint8")
        except cv2.error:
            # Degenerate flat/tiny inputs still have a valid connected-color result.
            pass
    selection = Image.fromarray(regions).resize(size, Image.Resampling.BILINEAR)
    selection = ImageChops.multiply(selection, image.convert("RGBA").getchannel("A"))
    return combine_selection(existing, selection, "subtract" if subtract else "add")


def refine_selection(mask, feather=0, grow=0, smooth=0):
    """Grow/shrink by circular morphology, remove small jaggies, then feather."""
    import cv2
    import numpy as np

    mask = _mask(mask)
    feather = _number(feather, "Feather", 0, 256)
    grow = round(_number(grow, "Grow/shrink", -256, 256))
    smooth = round(_number(smooth, "Smoothing", 0, 32))
    result = mask.copy()
    if grow or smooth:
        pixels = np.asarray(result)
        if grow:
            radius = abs(grow)
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
            operation = cv2.dilate if grow > 0 else cv2.erode
            pixels = operation(pixels, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0)
        if smooth:
            pixels = cv2.medianBlur(pixels, smooth * 2 + 1)
        result = Image.fromarray(pixels)
    return result.filter(ImageFilter.GaussianBlur(feather)) if feather else result


def clone_stamp(image, points, source, radius, hardness=0.8, opacity=1):
    """Aligned clone, sampled from the original at a fixed source/destination offset.

    ``source`` is the sampled source point; ``points[0]`` anchors the destination.
    Samples outside the source leave the destination unchanged. Premultiplied color
    interpolation avoids dark fringes when copying partially transparent pixels.
    """
    size = _size(image.size)
    points = _points(points, size)
    source = _point(source, size)
    original = image.convert("RGBA")
    if not points:
        return original.copy()
    dx, dy = round(points[0][0] - source[0]), round(points[0][1] - source[1])
    shifted = Image.new("RGBA", size)
    shifted.paste(original, (dx, dy))
    valid = Image.new("L", size)
    valid.paste(Image.new("L", size, 255), (dx, dy))
    coverage = ImageChops.multiply(brush_mask(size, points, radius, hardness, opacity), valid)
    return Image.composite(shifted.convert("RGBa"), original.convert("RGBa"), coverage).convert("RGBA")


def heal(image, mask):
    """Traditional Telea inpainting, limited to the affected bounding box.

    Reconstructs color while retaining the original alpha, and composites with the
    provided soft mask. It does not remove pixels to transparency or invent texture
    semantically; large objects in complex scenes may show obvious artifacts.
    """
    import cv2
    import numpy as np

    size = _size(image.size)
    mask = _mask(mask, size)
    original = image.convert("RGBA")
    bounds = mask.getbbox()
    if bounds is None:
        return original.copy()
    if mask.getextrema()[0] > 0:
        raise ValueError("Healing needs some unselected surrounding pixels.")
    padding = 32
    bounds = (max(0, bounds[0] - padding), max(0, bounds[1] - padding),
              min(size[0], bounds[2] + padding), min(size[1], bounds[3] + padding))
    crop = original.crop(bounds)
    coverage = mask.crop(bounds)
    pixels = np.asarray(crop)
    hole = np.where(np.asarray(coverage) > 0, 255, 0).astype("uint8")
    bgr = cv2.cvtColor(pixels, cv2.COLOR_RGBA2BGR)
    filled = cv2.inpaint(bgr, hole, 3, cv2.INPAINT_TELEA)
    corrected = Image.fromarray(cv2.cvtColor(filled, cv2.COLOR_BGR2RGB)).convert("RGBA")
    corrected.putalpha(crop.getchannel("A"))
    result = original.copy()
    result.paste(Image.composite(corrected, crop, coverage), bounds[:2])
    return result


def perspective(image, points):
    """Rectify a convex source quadrilateral supplied in TL, TR, BR, BL order."""
    import cv2
    import numpy as np

    size = _size(image.size)
    if len(points) != 4:
        raise ValueError("Perspective correction needs four corners.")
    corners = np.array(_points(points, size), dtype="float32")
    if not cv2.isContourConvex(corners) or abs(cv2.contourArea(corners)) < 1:
        raise ValueError("Perspective corners must form a nonempty convex quadrilateral.")
    width = round(max(np.linalg.norm(corners[1] - corners[0]), np.linalg.norm(corners[2] - corners[3]))) + 1
    height = round(max(np.linalg.norm(corners[3] - corners[0]), np.linalg.norm(corners[2] - corners[1]))) + 1
    _size((width, height))
    destination = np.array([(0, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1)], dtype="float32")
    transform = cv2.getPerspectiveTransform(corners, destination)
    premultiplied = np.asarray(image.convert("RGBA").convert("RGBa"))
    output = cv2.warpPerspective(premultiplied, transform, (width, height),
                                 flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT)
    return Image.frombytes("RGBa", (width, height), output.tobytes()).convert("RGBA")
