"""Local contour tracing with explicit resolution, noise and simplification controls.

This produces editable geometry, not an SVG that embeds the raster. Edge tracing
outlines edge bands; it is not engineering centreline reconstruction.
"""
from __future__ import annotations

import math

from PIL import Image

from .raster_limits import check_pixels
from .vector_document import MAX_NODES, VectorDocument, VectorObject, finite


def trace_image(image, *, mode="threshold", threshold=128, colors=4, invert=False,
                min_area=8, simplify=.2, smoothing=0, dpi=96, max_side=2400, remove_background=False):
    """Trace at a bounded analysis resolution, restoring physical source dimensions.

    ``simplify`` is in source pixels; ``min_area`` is in source square pixels.
    ``smoothing`` is Gaussian sigma in source pixels. Alpha below 128 is ignored.
    Returned metadata reports the actual analysis scale so limits are inspectable.
    """
    import cv2
    import numpy as np

    check_pixels(*image.size)
    if mode not in {"threshold", "color", "edges"}: raise ValueError("Unknown tracing mode.")
    threshold = int(finite(threshold, 0, 255)); colors = int(finite(colors, 2, 32))
    min_area = finite(min_area, 0, 1e8); simplify = finite(simplify, 0, 1000)
    smoothing = finite(smoothing, 0, 30); dpi = finite(dpi, 1, 9600)
    max_side = int(finite(max_side, 128, 4096))
    width, height = image.size; scale = min(1.0, max_side / max(width, height))
    working = image.convert("RGBA").resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
    pixels = np.asarray(working); visible = pixels[:, :, 3] >= 128
    rgb = pixels[:, :, :3]
    if remove_background:
        from .image_tools import color_selection
        corners = [(0, 0), (working.width - 1, 0), (0, working.height - 1), (working.width - 1, working.height - 1)]
        background = np.zeros(visible.shape, dtype=bool)
        for point in corners:
            background |= np.asarray(color_selection(working, point, tolerance=24, contiguous=True)) > 127
        visible &= ~background
    if smoothing:
        rgb = cv2.GaussianBlur(rgb, (0, 0), max(.1, smoothing * scale))
    regions = []
    if mode == "color":
        quantized = Image.fromarray(rgb).quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
        labels = np.asarray(quantized); palette = quantized.getpalette()
        for index in sorted(np.unique(labels)):
            selected = (labels == index) & visible
            color = "#%02x%02x%02x" % tuple(palette[int(index) * 3:int(index) * 3 + 3])
            regions.append((np.where(selected, 255, 0).astype("uint8"), color))
    else:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        if mode == "threshold": selected = gray > threshold if invert else gray <= threshold
        else: selected = cv2.Canny(gray, max(0, threshold // 2), threshold) > 0
        regions.append((np.where(selected & visible, 255, 0).astype("uint8"), "#202936"))
    mm_per_pixel = 25.4 / dpi / scale
    doc = VectorDocument(width * 25.4 / dpi, height * 25.4 / dpi)
    contour_count, removed, nodes = 0, 0, 0
    for mask, color in regions:
        contours, hierarchy = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None: continue
        commands = []
        for contour in contours:
            area = abs(cv2.contourArea(contour))
            if area < min_area * scale * scale or len(contour) < 3:
                removed += 1; continue
            if simplify: contour = cv2.approxPolyDP(contour, simplify * scale, True)
            points = contour.reshape(-1, 2)
            if len(points) < 3: removed += 1; continue
            contour_count += 1
            commands.append(["M", float(points[0, 0] + .5) * mm_per_pixel, float(points[0, 1] + .5) * mm_per_pixel])
            for x, y in points[1:]: commands.append(["L", float(x + .5) * mm_per_pixel, float(y + .5) * mm_per_pixel])
            commands.append(["Z"]); nodes += len(points)
            if nodes > MAX_NODES: raise ValueError("Trace exceeds the node limit. Increase simplification or noise removal.")
        if commands:
            doc.objects.append(VectorObject(f"Trace {color}", commands, fill=color,
                                             stroke="none", layer="Trace"))
    doc.layers = [{"name": "Trace", "visible": True, "locked": False}]
    doc.dirty = True
    info = {"contours": contour_count, "nodes": nodes, "removed": removed,
            "analysis_size": working.size, "analysis_scale": scale, "dpi": dpi,
            "pixel_uncertainty_mm": mm_per_pixel}
    doc.warnings.append(f"Trace analysis {working.width} × {working.height} px; approximately one analysis pixel ({mm_per_pixel:.4g} mm) boundary uncertainty. Verify dimensions before cutting.")
    if mode == "edges": doc.warnings.append("Edge mode outlines edge bands and may create double contours; it is not a centreline trace.")
    return doc, info


def simplify_object(obj, tolerance=.1):
    """Reduce path nodes with Douglas–Peucker. Curves become polygon geometry."""
    import cv2
    import numpy as np
    from PySide6.QtGui import QTransform
    tolerance = finite(tolerance, .001, 100)
    commands = []
    for polygon in obj.path().toSubpathPolygons(QTransform.fromScale(10 / tolerance, 10 / tolerance)):
        points = np.array([(point.x() * tolerance / 10, point.y() * tolerance / 10) for point in polygon], dtype="float32")
        closed = len(points) > 2 and float(np.linalg.norm(points[0] - points[-1])) < 1e-6
        simplified = cv2.approxPolyDP(points, tolerance, closed).reshape(-1, 2)
        for index, (x, y) in enumerate(simplified): commands.append(["M" if index == 0 else "L", float(x), float(y)])
        if closed: commands.append(["Z"])
    obj.commands = commands; obj.text = None
