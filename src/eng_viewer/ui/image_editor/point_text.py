"""Editable point text in image coordinates and its persistent pixel cache."""
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QGraphicsScene, QGraphicsTextItem

from ...core.image_document import _dimensions, affine, affine_about, affine_multiply, affine_translate
from .canvas import qtransform
from .common import from_qimage


class PointText(QGraphicsTextItem):
    def __init__(self, props):
        super().__init__(props.get("text", ""))
        self.document().setDocumentMargin(props.get("margin", 2))
        self.set_size(props["width"])
        self.setDefaultTextColor(QColor(props["color"]))

    def set_size(self, pixels):
        font = QFont(self.font())
        font.setPixelSize(max(1, round(pixels)))
        self.setFont(font)


def text_to_canvas(layer):
    """Map the text document to the canvas, including transforms in older projects."""
    props = layer.properties
    if "text_matrix" in props:
        matrix = affine(props["text_matrix"])
        size = props["text_cache_size"]
    else:
        x, y = props["points"][0]
        size = props["render_size"]
        crop = props.get("render_crop")
        if crop:
            x, y = x - crop[0], y - crop[1]
            size = crop[2] - crop[0], crop[3] - crop[1]
        matrix = affine_translate(x, y)
    for op in props.get("raster_ops", []):
        width, height = size
        if op[0] == "affine":
            delta, size = affine(op[1:7]), op[7:9]
        elif op[0] == "resize":
            size = op[1:3]
            delta = (size[0] / width, 0, 0, 0, size[1] / height, 0)
        elif op[0] == "place":
            size = op[1:3]
            delta = affine_translate(*op[3:5])
        elif op[0] == "flip":
            delta = (-1, 0, width, 0, 1, 0) if op[1] else (1, 0, 0, 0, -1, height)
        elif op[0] == "rotate":
            rotation = affine_about((width / 2, height / 2), degrees=-op[1])
            size = (width, height) if op[1] % 180 == 0 else (height, width) if op[1] % 90 == 0 else None
            if size is None:
                rect = qtransform(rotation).mapRect(QRectF(0, 0, width, height))
                size = (math.ceil(rect.right()) - math.floor(rect.left()),
                        math.ceil(rect.bottom()) - math.floor(rect.top()))
            delta = affine_multiply(rotation, affine_translate((size[0] - width) / 2, (size[1] - height) / 2))
        else:
            raise ValueError("Unsupported text transform")
        matrix = affine_multiply(matrix, delta)
    return affine_multiply(matrix, affine_translate(layer.x, layer.y))


def render_point_text(props, matrix):
    """Rasterize at the layer's current scale, while retaining source text and geometry."""
    if len(props["text"]) > 100000:
        raise ValueError("Text is limited to 100,000 characters.")
    item = PointText(props)
    item.setTransform(qtransform(matrix))
    bounds = item.sceneBoundingRect().adjusted(-1, -1, 1, 1).toAlignedRect()
    _dimensions(bounds.width(), bounds.height())
    scene = QGraphicsScene()
    scene.addItem(item)
    image = QImage(bounds.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    try:
        scene.render(painter, QRectF(image.rect()), QRectF(bounds))
    finally:
        painter.end()
    props["text_matrix"] = list(affine_multiply(matrix, affine_translate(-bounds.x(), -bounds.y())))
    props["text_cache_size"] = [bounds.width(), bounds.height()]
    return from_qimage(image), bounds.x(), bounds.y()
