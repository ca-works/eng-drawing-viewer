"""Render portable annotation descriptors with the existing MARK primitives.

The descriptor remains in the editable layer; pixels are its rendering cache.
Qt graphics scenes are used only on the GUI thread.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsRectItem

from ..mark.items import AnnotationMixin, ArrowItem, EllipseItem, StrokeItem, TextItem, BubbleItem
from .common import from_qimage


def render_transformed_annotation(props):
    """Rebuild an editable annotation's pixel cache without losing transforms."""
    from PIL import Image
    from ...core.image_document import _dimensions
    image = render_annotation(tuple(props["render_size"]), props)
    if props.get("render_crop"):
        image = image.crop(tuple(props["render_crop"]))
    for op in props.get("raster_ops", []):
        if op[0] == "resize":
            _dimensions(op[1], op[2])
            image = image.resize((op[1], op[2]), Image.Resampling.LANCZOS)
        elif op[0] == "place":
            _dimensions(op[1], op[2])
            canvas = Image.new("RGBA", (op[1], op[2]))
            canvas.paste(image, (op[3], op[4]))
            image = canvas
        elif op[0] == "rotate":
            image = image.rotate(op[1], Image.Resampling.BICUBIC, expand=True)
        elif op[0] == "flip":
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT if op[1] else Image.Transpose.FLIP_TOP_BOTTOM)
        else:
            raise ValueError("Unsupported annotation transform")
    return image


def render_annotation(size, props):
    from ...core.image_document import _dimensions
    _dimensions(*size)
    raw_points = props.get("points", [])
    if not 1 <= len(raw_points) <= 100000:
        raise ValueError("Annotation has an invalid point count")
    if any(len(p) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1000000 for v in p) for p in raw_points):
        raise ValueError("Annotation coordinates are invalid")
    width = props.get("width", 6)
    if not isinstance(width, (int, float)) or not math.isfinite(width) or not 0 < width <= 10000:
        raise ValueError("Annotation size is invalid")
    if len(str(props.get("text", ""))) > 100000:
        raise ValueError("Annotation text is too long")
    scene = QGraphicsScene()
    kind = props["tool"]
    pts = [QPointF(float(x), float(y)) for x, y in props["points"]]
    color = QColor(props.get("color", "#2563eb"))
    width = float(props.get("width", 6))
    if kind in ("pen", "highlighter"):
        item = StrokeItem(color, width, kind == "highlighter", props.get("opacity", .35))
        for p in pts:
            item.add_point(p)
    elif kind == "arrow":
        item = ArrowItem(pts[0], pts[-1], color, width)
    elif kind == "circle":
        item = EllipseItem(QRectF(pts[0], pts[-1]).normalized(), color, width)
    elif kind == "shape":
        item = QGraphicsRectItem(QRectF(pts[0], pts[-1]).normalized())
        item.setPen(QPen(color, width))
    elif kind == "text":
        item = TextItem(props.get("text", "Text"), color, width)
        item.setPos(pts[0])
    elif kind == "bubble":
        item = BubbleItem(int(props.get("number", 1)), width)
        item.setPos(pts[0])
    else:
        raise ValueError("Unsupported annotation type")
    scene.addItem(item)
    scene.setSceneRect(0, 0, *size)
    image = QImage(*size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    previous = AnnotationMixin.exporting
    AnnotationMixin.exporting = True
    try:
        scene.render(painter, QRectF(0, 0, *size), QRectF(0, 0, *size))
    finally:
        painter.end()
        AnnotationMixin.exporting = previous
    return from_qimage(image)
