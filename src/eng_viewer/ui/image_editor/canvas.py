"""Source-resolution image viewport; geometry is always in source pixels."""
from __future__ import annotations

import math

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal, QEvent
from PySide6.QtGui import QColor, QBrush, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from ..rulers import RULER, grid_step, paint_grid, paint_rulers, ruler_steps
from .common import to_qimage

BRUSH_TOOLS = {"pen", "highlighter", "eraser", "background", "restore", "quick", "clone", "heal"}
DRAG_TOOLS = BRUSH_TOOLS | {"rect", "ellipse", "lasso", "crop", "move", "arrow", "circle", "shape"}


class ImageCanvas(QGraphicsView):
    gesture = Signal(str, object, object)
    zoomChanged = Signal(float)
    cropChanged = Signal(object)
    toolRequested = Signal(str)
    actionRequested = Signal(str)
    guidesChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.item = self.scene_.addPixmap(QPixmap())
        self.item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.tool = "move"
        self.diameter = 40
        self.hardness = .8
        self.color = "#2563eb"
        self.crop_ratio = 0.0
        self.crop_rect = None
        self._crop_handle = None
        self._points = []
        self._cursor = QPointF()
        self._down = False
        self._pan = None
        self._space = False
        self._polygon = []
        self._selection = QPainterPath()
        self._phase = 0
        self._image_size = (0, 0)
        self.grid = False
        self.rulers = True
        self.unit = "px"
        self.dpi = 96
        self.guides = []
        self._guide_drag = None
        self.snap = False
        self.busy = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(110)
        tile = QPixmap(24, 24)
        tile.fill(QColor("#d3d6dc"))
        p = QPainter(tile)
        p.fillRect(0, 0, 12, 12, QColor("#eef0f3"))
        p.fillRect(12, 12, 12, 12, QColor("#eef0f3"))
        p.end()
        self._checker = QBrush(tile)

    def set_image(self, image: Image.Image, fit=False):
        old = self._image_size
        self._image_size = image.size
        self.item.setPixmap(QPixmap.fromImage(to_qimage(image)))
        self.scene_.setSceneRect(0, 0, *image.size)
        if fit or old == (0, 0):
            self.fit_image()
        self.viewport().update()

    def set_selection(self, mask):
        self._selection = QPainterPath()
        self._selection.setFillRule(Qt.FillRule.OddEvenFill)
        if mask is not None and mask.getbbox():
            import cv2
            import numpy as np
            scale = min(1, 2048 / max(mask.size))
            small = mask.resize((max(1, round(mask.width * scale)), max(1, round(mask.height * scale))))
            contours, _ = cv2.findContours((np.array(small) > 127).astype('uint8'), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                if len(contour) < 2:
                    continue
                xy = contour[:, 0, :] / scale
                self._selection.moveTo(float(xy[0][0]), float(xy[0][1]))
                for x, y in xy[1:]:
                    self._selection.lineTo(float(x), float(y))
                self._selection.closeSubpath()
        self.viewport().update()

    def _animate(self):
        if self.isVisible() and not self._selection.isEmpty():
            self._phase = (self._phase + 1) % 12
            self.viewport().update()

    def set_tool(self, tool):
        self.tool = tool
        self._points = []
        self._polygon = []
        self._down = False
        self.setCursor(Qt.CursorShape.OpenHandCursor if tool == "move" else Qt.CursorShape.CrossCursor)
        self.viewport().update()

    def fit_image(self):
        if self._image_size[0]:
            self.fitInView(self.item.boundingRect().adjusted(-24, -24, 24, 24), Qt.AspectRatioMode.KeepAspectRatio)
            self.zoomChanged.emit(self.transform().m11() * 100)

    def fit_width(self):
        if self._image_size[0]:
            self.set_zoom(100 * max(1, self.viewport().width() - 48) / self._image_size[0])

    def set_zoom(self, percent, anchor=None):
        percent = max(1, min(6400, percent))
        if anchor is None:
            anchor = self.viewport().rect().center()
        before = self.mapToScene(anchor)
        self.resetTransform()
        self.scale(percent / 100, percent / 100)
        after = self.mapToScene(anchor)
        delta = after - before
        self.translate(delta.x(), delta.y())
        self.item.setTransformationMode(Qt.TransformationMode.FastTransformation if percent >= 800
                                        else Qt.TransformationMode.SmoothTransformation)
        self.zoomChanged.emit(percent)

    def wheelEvent(self, event):
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            delta = event.angleDelta().y() or event.pixelDelta().y()
            self.set_zoom(self.transform().m11() * 100 * math.exp(delta / 600), event.position().toPoint())
            event.accept()
        else:
            super().wheelEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.NativeGesture and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            self.set_zoom(self.transform().m11() * 100 * (1 + event.value()), event.position().toPoint())
            return True
        return super().event(event)

    def _point(self, event):
        p = self.mapToScene(event.position().toPoint())
        w, h = self._image_size
        x, y = min(max(p.x(), 0), w), min(max(p.y(), 0), h)
        if self.snap:
            threshold = 8 / max(self.transform().m11(), .01)
            for axis, value in self.guides:
                if axis == "x" and abs(x - value) < threshold:
                    x = value
                elif axis == "y" and abs(y - value) < threshold:
                    y = value
        return QPointF(x, y)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton or self._space:
            self._pan = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if self.busy or not self._image_size[0]:
            return
        self.setFocus()
        p = self._point(event)
        if self.rulers and (event.position().x() < RULER or event.position().y() < RULER):
            axis = "x" if event.position().x() < RULER else "y"
            self.guides.append((axis, p.x() if axis == "x" else p.y()))
            self._guide_drag = len(self.guides) - 1
            return
        if self.tool == "clone" and event.modifiers() & Qt.KeyboardModifier.AltModifier:
            self.gesture.emit("clone_source", [(p.x(), p.y())], event.modifiers())
            return
        if self.tool == "polygon":
            self._polygon.append(p)
            self.viewport().update()
            return
        if self.tool in ("wand", "color", "text", "bubble", "magic"):
            self.gesture.emit(self.tool, [(p.x(), p.y())], event.modifiers())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._down = True
        self._points = [p]
        self._mods = event.modifiers()
        self._crop_handle = None
        if self.tool == "crop" and self.crop_rect is not None:
            corners = [self.crop_rect.topLeft(), self.crop_rect.topRight(), self.crop_rect.bottomRight(), self.crop_rect.bottomLeft()]
            radius = 12 / max(self.transform().m11(), .01)
            for i, corner in enumerate(corners):
                if (p - corner).manhattanLength() < radius:
                    self._crop_handle = i
                    self._points = [corners[(i + 2) % 4]]
                    break
        event.accept()

    def mouseMoveEvent(self, event):
        self._cursor = self._point(event)
        if self._pan is not None:
            pos = event.position().toPoint()
            delta = pos - self._pan
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._pan = pos
            return
        if self._guide_drag is not None:
            axis, _ = self.guides[self._guide_drag]
            self.guides[self._guide_drag] = (axis, self._cursor.x() if axis == "x" else self._cursor.y())
            self.viewport().update()
            return
        if self._down:
            self._points.append(self._cursor)
            if self.tool == "crop":
                self.crop_rect = self._drag_rect()
                self.cropChanged.emit(self.crop_rect)
        self.viewport().update()

    def _drag_rect(self):
        rect = QRectF(self._points[0], self._points[-1]).normalized()
        if self.tool == "crop" and self.crop_ratio > 0:
            height = min(rect.height(), rect.width() / self.crop_ratio)
            rect.setSize(type(rect.size())(height * self.crop_ratio, height))
        return rect

    def mouseReleaseEvent(self, event):
        if self._pan is not None:
            self._pan = None
            self.set_tool(self.tool)
            return
        if self._guide_drag is not None:
            self._guide_drag = None
            self.guidesChanged.emit(list(self.guides))
            return
        if not self._down:
            return
        self._points.append(self._point(event))
        self._down = False
        if self.tool == "crop":
            self.crop_rect = self._drag_rect()
            self.cropChanged.emit(self.crop_rect)
        else:
            self.gesture.emit(self.tool, [(p.x(), p.y()) for p in self._points], self._mods)
        self._points = []
        self.viewport().update()

    def mouseDoubleClickEvent(self, event):
        if self.tool == "polygon":
            self._finish_polygon(event.modifiers())
        else:
            super().mouseDoubleClickEvent(event)

    def _finish_polygon(self, modifiers=Qt.KeyboardModifier.NoModifier):
        if len(self._polygon) >= 3:
            self.gesture.emit("polygon", [(p.x(), p.y()) for p in self._polygon], modifiers)
        self._polygon = []
        self.viewport().update()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Space:
            self._space = True
            event.accept()
        elif key == Qt.Key.Key_Escape:
            self.crop_rect = None
            self._points = []
            self._polygon = []
            self._down = False
            self.viewport().update()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.tool == "polygon":
                self._finish_polygon(event.modifiers())
            elif self.tool == "crop":
                self.actionRequested.emit("crop")
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.actionRequested.emit("delete")
        elif key in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight):
            self.actionRequested.emit("smaller" if key == Qt.Key.Key_BracketLeft else "larger")
        elif not event.modifiers():
            tools = {Qt.Key.Key_V: "move", Qt.Key.Key_M: "rect", Qt.Key.Key_L: "lasso", Qt.Key.Key_W: "wand",
                     Qt.Key.Key_Q: "quick", Qt.Key.Key_C: "crop", Qt.Key.Key_B: "pen", Qt.Key.Key_E: "eraser",
                     Qt.Key.Key_S: "clone", Qt.Key.Key_J: "heal", Qt.Key.Key_T: "text", Qt.Key.Key_U: "shape",
                     Qt.Key.Key_X: "magic"}
            if key in tools:
                self.toolRequested.emit(tools[key])
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self._space = False
        super().keyReleaseEvent(event)

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, QColor("#272c35"))
        painter.fillRect(self.sceneRect(), self._checker)

    def drawForeground(self, painter, rect):
        zoom = max(self.transform().m11(), .01)
        pen = QPen(QColor("#62b8ff"), 0)
        painter.setPen(pen)
        if self.grid:
            area = QRectF(0, 0, *self._image_size)
            if zoom >= 12:
                paint_grid(painter, area, rect, 1, zoom, QColor(98, 184, 255, 70), 0)
            unit = self._unit_px()
            major, _ = ruler_steps(unit * zoom)
            step = grid_step(unit * zoom, 16)
            ratio = major / step
            paint_grid(painter, area, rect, step * unit, zoom, QColor(98, 184, 255, 150),
                       round(ratio) if abs(ratio - round(ratio)) < 1e-6 else 0)
            painter.setPen(pen)
        for axis, value in self.guides:
            painter.drawLine(QPointF(value, 0), QPointF(value, self._image_size[1])) if axis == "x" else painter.drawLine(QPointF(0, value), QPointF(self._image_size[0], value))
        if not self._selection.isEmpty():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(Qt.GlobalColor.white, 0))
            painter.drawPath(self._selection)
            ants = QPen(Qt.GlobalColor.black, 0, Qt.PenStyle.DashLine)
            ants.setDashPattern([5, 5])
            ants.setDashOffset(self._phase)
            painter.setPen(ants)
            painter.drawPath(self._selection)
        if self.crop_rect is not None and self.tool == "crop":
            crop = self.crop_rect
            shade = QPainterPath()
            shade.addRect(self.sceneRect())
            shade.addRect(crop)
            painter.fillPath(shade, QColor(0, 0, 0, 130))
            painter.setPen(QPen(Qt.GlobalColor.white, 0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(crop)
            for f in (1 / 3, 2 / 3):
                painter.drawLine(QPointF(crop.left() + crop.width() * f, crop.top()), QPointF(crop.left() + crop.width() * f, crop.bottom()))
                painter.drawLine(QPointF(crop.left(), crop.top() + crop.height() * f), QPointF(crop.right(), crop.top() + crop.height() * f))
            painter.setBrush(Qt.GlobalColor.white)
            for p in (crop.topLeft(), crop.topRight(), crop.bottomLeft(), crop.bottomRight()):
                painter.drawRect(QRectF(p.x() - 3 / zoom, p.y() - 3 / zoom, 6 / zoom, 6 / zoom))
        points = self._points or self._polygon
        if points:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            preview = QPainterPath(points[0])
            for p in points[1:]:
                preview.lineTo(p)
            if self._polygon:
                preview.lineTo(self._cursor)
            if self.tool in ("rect", "ellipse", "circle", "shape") and len(points) > 1:
                painter.setPen(QPen(QColor(self.color), 0))
                r = QRectF(points[0], points[-1]).normalized()
                painter.drawEllipse(r) if self.tool in ("ellipse", "circle") else painter.drawRect(r)
            else:
                color = QColor(self.color)
                color.setAlpha(110 if self.tool in BRUSH_TOOLS else 255)
                painter.setPen(QPen(color, self.diameter if self.tool in BRUSH_TOOLS else 1 / zoom,
                                     Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.drawPath(preview)
        if self.tool in BRUSH_TOOLS and self.underMouse():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(Qt.GlobalColor.black, 2 / zoom))
            painter.drawEllipse(self._cursor, self.diameter / 2, self.diameter / 2)
            painter.setPen(QPen(Qt.GlobalColor.white, 0))
            painter.drawEllipse(self._cursor, self.diameter / 2, self.diameter / 2)
        if self.rulers:
            paint_rulers(self, painter, self._unit_px(), QColor("#303641"), QColor("#d7dde6"), QColor("#62b8ff"))

    def _unit_px(self):
        return {"px": 1, "in": self.dpi, "cm": self.dpi / 2.54, "mm": self.dpi / 25.4}[self.unit]
