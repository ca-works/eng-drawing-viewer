"""Read-only view of the MARK items for the current drawing page."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QStyleOptionGraphicsItem

from ..page_view import PageView
from .items import AnnotationMixin


class MarkOverlayView(PageView):
    def __init__(self, render, owner):
        super().__init__(render)
        self.owner = owner
        self.show_annotations = False

    def drawForeground(self, painter, rect):
        ref = self.page_ref
        if self.show_annotations and ref is not None:
            self._paint_markup(painter, rect, ref)
        super().drawForeground(painter, rect)

    def render_markup(self, image, ref):
        painter = QPainter(image)
        try:
            painter.scale(image.width() / ref.width, image.height() / ref.height)
            self._paint_markup(painter, QRectF(0, 0, ref.width, ref.height), ref)
        finally:
            painter.end()

    def _paint_markup(self, painter, exposed, ref):
        items = self.owner.mark_items(ref)
        if not items:
            return
        page = QRectF(0, 0, ref.width, ref.height)
        option = QStyleOptionGraphicsItem()
        was_exporting = AnnotationMixin.exporting
        AnnotationMixin.exporting = True
        painter.save()
        try:
            painter.setClipRect(page, Qt.ClipOperation.IntersectClip)
            painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
            for item in items:
                if not exposed.intersects(item.sceneBoundingRect()):
                    continue
                painter.save()
                try:
                    option.exposedRect = item.mapRectFromScene(exposed)
                    painter.setWorldTransform(item.sceneTransform(), True)
                    item.paint(painter, option)
                finally:
                    painter.restore()
        finally:
            painter.restore()
            AnnotationMixin.exporting = was_exporting
