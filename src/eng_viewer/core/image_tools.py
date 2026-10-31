"""Offline selection, masking and retouching operations in source-pixel coordinates.

No Qt, network calls, model downloads or heavyweight initialization occurs on import.
Masks are Pillow ``L`` images (255 = selected/visible), pixels are straight RGBA.
Quick Selection is a conventional seeded color / GrabCut algorithm, not an AI model.
Its analysis image is bounded; all other operations retain the source resolution.
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


def color_selection(image, point, tolerance=32, contiguous=True):
    """Select colors within a per-channel RGB/alpha tolerance of the seed.

    Contiguous mode uses a native four-connected flood fill. Completely transparent
    seeds match transparency regardless of hidden RGB, avoiding invisible-color traps.
    """
    import cv2
    import numpy as np

    size = _size(image.size)
    x, y = (round(v) for v in _point(point, size))
    tolerance = round(_number(tolerance, "Tolerance", 0, 255))
    rgba = np.asarray(image.convert("RGBA"))
    sample = rgba[y, x].astype(np.int16)
    if sample[3] == 0:
        candidates = cv2.inRange(rgba[:, :, 3], 0, 0)
    else:
        lower = tuple(int(v) for v in np.maximum(sample - tolerance, 0))
        upper = tuple(int(v) for v in np.minimum(sample + tolerance, 255))
        candidates = cv2.inRange(rgba, lower, upper)
    if contiguous:
        # The seed is always a candidate. Filling with 128 distinguishes its
        # component from all disconnected 255-valued regions in a single pass.
        cv2.floodFill(candidates, None, (x, y), 128, flags=4)
        candidates = cv2.inRange(candidates, 128, 128)
    return Image.fromarray(candidates)


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


def background_erase(image, points, radius, tolerance=32, hardness=0.8, mask=None):
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


def magic_erase(image, point, tolerance=32, contiguous=True, antialias=True, opacity=1.0, mask=None):
    """One-click Magic Eraser: hide every pixel similar in colour to the clicked one.

    Matches Photoshop's Magic Eraser and GIMP's fuzzy select: a pixel matches when its
    largest RGBA channel difference from the seed is within ``tolerance``. Contiguous
    mode keeps only the area connected to the seed. Anti-aliasing fades a two-pixel
    band just past the tolerance so cut-out edges do not stair-step. Pixels that are
    already invisible (transparent or masked) count as matches, so a background split
    by earlier erasing is still removed in one click. Returns the new layer mask; the
    layer's pixels are never changed, so Restore can bring any area back.
    """
    import cv2
    import numpy as np

    size = _size(image.size)
    x, y = (round(v) for v in _point(point, size))
    tolerance = round(_number(tolerance, "Tolerance", 0, 255))
    opacity = _number(opacity, "Opacity", 0, 1)
    original = Image.new("L", size, 255) if mask is None else _mask(mask, size)
    rgba = np.ascontiguousarray(np.asarray(image.convert("RGBA")))
    hidden = (rgba[:, :, 3] == 0) | (np.asarray(original) == 0)
    if hidden[y, x]:
        return original.copy()
    seed = tuple(int(v) for v in rgba[y, x])
    diff = cv2.absdiff(rgba, np.full_like(rgba, seed)).max(axis=2)
    diff[hidden] = 0
    soft = max(1.0, tolerance * 0.5) if antialias else 0.0
    levels = np.arange(256, dtype=np.float32)
    ramp = np.clip((tolerance + soft - levels) / soft, 0, 1) if soft else (levels <= tolerance).astype(np.float32)
    ramp[: tolerance + 1] = 1
    removal = cv2.LUT(diff.astype(np.uint8), np.round(ramp * 255 * opacity).astype(np.uint8))
    if contiguous:
        core = np.where(diff <= tolerance, 255, 0).astype(np.uint8)
        cv2.floodFill(core, None, (x, y), 128, flags=4)
        core = core == 128
        band = cv2.dilate(core.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=2 if antialias else 0) > 0
        removal = np.where(core, removal, np.where(band & (diff > tolerance), removal, 0)).astype(np.uint8)
    return ImageChops.multiply(original, ImageChops.invert(Image.fromarray(removal)))


def remove_background(image, iterations=5):
    """One-click background removal without a model, returned as a visibility mask.

    The image border is treated as background: its dominant colours (k-means) mark
    every border-connected similar region as probable background and the rest as
    probable foreground, then GrabCut refines the boundary on a bounded working
    image. Suits objects on a plain or distinct backdrop; it is not semantic subject
    detection. Raises ``ValueError`` when no subject separates from the border.
    """
    import cv2
    import numpy as np

    size = _size(image.size)
    scale = min(1.0, 1024 / max(size))
    work = max(1, round(size[0] * scale)), max(1, round(size[1] * scale))
    if min(work) < 8:
        raise ValueError("The image is too small for automatic background removal.")
    rgba = np.asarray(image.convert("RGBA").resize(work, Image.Resampling.LANCZOS))
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
    similar = (distance < 14).astype(np.uint8)
    count, labels = cv2.connectedComponents(similar, connectivity=4)
    touching = np.unique(labels[edge & (similar > 0)])
    background = np.isin(labels, touching[touching > 0])
    gc = np.full((h, w), cv2.GC_PR_FGD, np.uint8)
    gc[background] = cv2.GC_PR_BGD
    gc[edge & background] = cv2.GC_BGD
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
