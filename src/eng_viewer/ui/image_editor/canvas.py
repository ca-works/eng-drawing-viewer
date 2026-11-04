"""Source-resolution image viewport; geometry is always in source pixels.

With the Move tool the active layer shows a transform frame: drag inside to move it,
drag a handle to scale (corners keep proportions; Shift frees them, Option/Alt scales
from the centre) and drag the knob above the frame, or just outside a corner, to rotate
(Shift: 15° steps). While dragging, the layers below and above are shown separately so
the layer moves live; the change is applied as one undoable step on release.
"""
from __future__ import annotations

import math

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal, QEvent
from PySide6.QtGui import QColor, QBrush, QCursor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from .. import icons
from ..rulers import RULER, grid_step, paint_grid, paint_rulers, ruler_steps
from ..theme import IS_MAC
from . import transform as TF
from .common import to_qimage

SNAP_COLOR = QColor("#ff2fd0")
FRAME_COLOR = QColor("#2f8cff")

BRUSH_TOOLS = {"pen", "highlighter", "eraser", "background", "restore", "quick", "clone", "heal"}
DRAG_TOOLS = BRUSH_TOOLS | {"rect", "ellipse", "lasso", "crop", "move", "arrow", "circle", "shape"}
CLICK_TOOLS = ("wand", "color", "text", "bubble", "magic", "bucket")
TOOL_KEYS = {Qt.Key.Key_V: "move", Qt.Key.Key_M: "rect", Qt.Key.Key_L: "lasso", Qt.Key.Key_W: "wand",
             Qt.Key.Key_Q: "quick", Qt.Key.Key_C: "crop", Qt.Key.Key_B: "pen", Qt.Key.Key_E: "eraser",
             Qt.Key.Key_S: "clone", Qt.Key.Key_J: "heal", Qt.Key.Key_T: "text", Qt.Key.Key_U: "shape",
             Qt.Key.Key_X: "magic", Qt.Key.Key_G: "bucket"}


def qtransform(matrix):
    a, b, c, d, e, f = matrix
    return QTransform(a, d, b, e, c, f)


def _rotate_cursor():
    pixmap = icons.pixmap("refresh", 20, QColor("#111111"), dpr=2.0, stroke=2.2)
    halo = QPixmap(pixmap.size())
    halo.setDevicePixelRatio(pixmap.devicePixelRatio())
    halo.fill(Qt.GlobalColor.transparent)
    p = QPainter(halo)
    white = icons.pixmap("refresh", 20, QColor("#ffffff"), dpr=2.0, stroke=4.2)
    p.drawPixmap(0, 0, white)
    p.drawPixmap(0, 0, pixmap)
    p.end()
    return QCursor(halo, 10, 10)


class ImageCanvas(QGraphicsView):
    gesture = Signal(str, object, object)
    zoomChanged = Signal(float)
    cropChanged = Signal(object)
    toolRequested = Signal(str)
    actionRequested = Signal(str)
    guidesChanged = Signal(object)
    transformFinished = Signal(str, object)  # ("move", (dx, dy)) or ("affine", matrix)
    layerPickRequested = Signal(float, float)  # handled synchronously: may change layer_frame
    nudgeRequested = Signal(int, int)

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
        self.snap = True
        self.busy = False
        # Move tool: the active layer's frame (cx, cy, w, h, degrees) and live preview.
        self.layer_frame = None
        self.layer_editable = True
        self.auto_select = True
        self.preview_provider = None  # () -> (below QImage|None, layer QImage, (x, y), above QImage|None)
        self.snap_provider = None  # () -> (x targets, y targets)
        self._drag = None
        self._snap_lines = []
        self._rotate_cursor = None
        self.preview_items = []
        for z in (1, 2, 3):
            item = self.scene_.addPixmap(QPixmap())
            item.setZValue(z)
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            item.setVisible(False)
            self.preview_items.append(item)
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
        self.end_preview()
        if fit or old == (0, 0):
            self.fit_image()
        self.viewport().update()

    def clear_image(self):
        self.end_preview()
        self.item.setPixmap(QPixmap())
        self._image_size = (0, 0)
        self.layer_frame = None
        self.viewport().update()

    def set_layer_frame(self, frame, editable=True):
        self.layer_frame = frame
        self.layer_editable = editable
        self.viewport().update()

    # ------------------------------------------------------- live layer preview
    def begin_preview(self):
        """Split the picture into layers below / the active layer / layers above."""
        if self.preview_provider is None:
            return False
        parts = self.preview_provider()
        if parts is None:
            return False
        below, layer, (x, y), above = parts
        for item, image in zip(self.preview_items, (below, layer, above)):
            item.setPixmap(QPixmap.fromImage(image) if image is not None else QPixmap())
            item.setTransform(QTransform())
            item.setVisible(image is not None)
        self.preview_items[1].setOffset(x, y)
        self.item.setVisible(False)
        return True

    def end_preview(self):
        if self._drag is not None:
            self._drag = None
        self._snap_lines = []
        for item in self.preview_items:
            item.setVisible(False)
            item.setPixmap(QPixmap())
        self.item.setVisible(True)

    def _preview_transform(self, matrix):
        self.preview_items[1].setTransform(qtransform(matrix))

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
        if self._drag is not None:
            self.end_preview()
        self.tool = tool
        self._points = []
        self._polygon = []
        self._down = False
        self.setCursor(Qt.CursorShape.ArrowCursor if tool == "move" else Qt.CursorShape.CrossCursor)
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

    def _zoom(self):
        return max(self.transform().m11(), .01)

    def _free_point(self, event):
        """Scene point without clamping (layers may extend past the canvas)."""
        return self.mapToScene(event.position().toPoint())

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
        if self.tool in CLICK_TOOLS:
            self.gesture.emit(self.tool, [(p.x(), p.y())], event.modifiers())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.tool == "move":
            self._start_layer_drag(event)
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

    # ------------------------------------------------------------ Move tool
    def _snap_enabled(self, modifiers):
        held = modifiers & (Qt.KeyboardModifier.MetaModifier if IS_MAC else Qt.KeyboardModifier.ControlModifier)
        return self.snap and not held

    def _start_layer_drag(self, event):
        point = self._free_point(event)
        zoom = self._zoom()
        hit = TF.hit_test(self.layer_frame, point.x(), point.y(), zoom)
        if self.auto_select and hit not in ("rotate", "rotate-zone") and not isinstance(hit, tuple):
            self.layerPickRequested.emit(point.x(), point.y())
            hit = TF.hit_test(self.layer_frame, point.x(), point.y(), zoom)
            if hit is None:
                return
        if self.layer_frame is None:
            return
        if not self.layer_editable:
            self.actionRequested.emit("locked")
            return
        kind = "rotate" if hit in ("rotate", "rotate-zone") else "scale" if isinstance(hit, tuple) else "move"
        if not self.begin_preview():
            return
        targets = self.snap_provider() if self.snap_provider else ([], [])
        self._drag = {"kind": kind, "handle": hit, "start": (point.x(), point.y()), "frame": self.layer_frame,
                      "current": self.layer_frame, "matrix": (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), "targets": targets,
                      "moved": False}
        self.setCursor(Qt.CursorShape.ClosedHandCursor if kind == "move" else self.cursor())
        event.accept()

    def _update_layer_drag(self, event):
        drag = self._drag
        point = self._free_point(event)
        mods = event.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        start, frame = drag["start"], drag["frame"]
        self._snap_lines = []
        if drag["kind"] == "move":
            dx, dy = point.x() - start[0], point.y() - start[1]
            if shift:  # constrain to the main axis
                if abs(dx) > abs(dy):
                    dy = 0
                else:
                    dx = 0
            if self._snap_enabled(mods):
                tx, ty = drag["targets"]
                dx, dy, self._snap_lines = TF.snap_move(frame, dx, dy, [] if shift and not dx else tx,
                                                        [] if shift and not dy else ty, 8 / self._zoom())
            dx, dy = round(dx), round(dy)
            new = (frame[0] + dx, frame[1] + dy, *frame[2:])
            matrix = (1.0, 0.0, float(dx), 0.0, 1.0, float(dy))
            drag["offset"] = (dx, dy)
        elif drag["kind"] == "scale":
            new = TF.scale_frame(frame, drag["handle"], point.x(), point.y(),
                                 keep_aspect=not shift, from_center=alt)
            matrix = TF.frame_delta(frame, new)
        else:
            new = TF.rotate_frame(frame, start, (point.x(), point.y()), snap=shift)
            matrix = TF.frame_delta(frame, new)
        drag["current"], drag["matrix"] = new, matrix
        drag["moved"] = drag["moved"] or math.hypot(point.x() - start[0], point.y() - start[1]) * self._zoom() > 2
        self._preview_transform(matrix)
        self.viewport().update()

    def _finish_layer_drag(self, cancel=False):
        drag, self._drag = self._drag, None
        self._snap_lines = []
        if drag is None:
            return
        changed = not cancel and drag["moved"]
        if drag["kind"] == "move":
            changed = changed and drag.get("offset", (0, 0)) != (0, 0)
        if not changed:
            self.end_preview()
            self.set_tool(self.tool)
            return
        # The preview stays until the workspace shows the applied result.
        self.layer_frame = drag["current"]
        self.set_tool(self.tool)
        for item in self.preview_items:
            item.setVisible(bool(item.pixmap() and not item.pixmap().isNull()))
        self.item.setVisible(False)
        if drag["kind"] == "move":
            self.transformFinished.emit("move", drag["offset"])
        else:
            self.transformFinished.emit("affine", drag["matrix"])

    def _hover_cursor(self, point):
        if self.tool != "move" or self.layer_frame is None or self.busy:
            return
        hit = TF.hit_test(self.layer_frame, point.x(), point.y(), self._zoom())
        if hit in ("rotate", "rotate-zone"):
            if self._rotate_cursor is None:
                self._rotate_cursor = _rotate_cursor()
            self.setCursor(self._rotate_cursor)
        elif isinstance(hit, tuple):
            angle = TF.handle_cursor_angle(self.layer_frame, hit)
            shapes = [Qt.CursorShape.SizeHorCursor, Qt.CursorShape.SizeFDiagCursor,
                      Qt.CursorShape.SizeVerCursor, Qt.CursorShape.SizeBDiagCursor]
            self.setCursor(shapes[round(angle / 45) % 4])
        elif hit == "inside":
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self._cursor = self._point(event)
            self._update_layer_drag(event)
            return
        if not self._down and self._pan is None and not event.buttons():
            self._hover_cursor(self._free_point(event))
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
        if self._drag is not None:
            self._update_layer_drag(event)
            self._finish_layer_drag()
            return
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
        elif key == Qt.Key.Key_Escape and self._drag is not None:
            self._finish_layer_drag(cancel=True)
        elif (key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down) and self.tool == "move"
              and not event.modifiers() & ~(Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.KeypadModifier)):
            step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            dx = {Qt.Key.Key_Left: -step, Qt.Key.Key_Right: step}.get(key, 0)
            dy = {Qt.Key.Key_Up: -step, Qt.Key.Key_Down: step}.get(key, 0)
            self.nudgeRequested.emit(dx, dy)
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
            if key in TOOL_KEYS:
                self.toolRequested.emit(TOOL_KEYS[key])
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
        if self.tool == "move" and self.layer_frame is not None and not self.busy:
            self._paint_frame(painter, zoom)
        if self._snap_lines:
            painter.setPen(QPen(SNAP_COLOR, 0))
            big = 1e6
            for axis, value in self._snap_lines:
                if axis == "x":
                    painter.drawLine(QPointF(value, -big), QPointF(value, big))
                else:
                    painter.drawLine(QPointF(-big, value), QPointF(big, value))
        if self.rulers:
            paint_rulers(self, painter, self._unit_px(), QColor("#303641"), QColor("#d7dde6"), QColor("#62b8ff"))

    def _paint_frame(self, painter, zoom):
        frame = self._drag["current"] if self._drag else self.layer_frame
        polygon = QPolygonF([QPointF(*p) for p in TF.corners(frame)])
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 170), 3 / zoom))
        painter.drawPolygon(polygon)
        painter.setPen(QPen(FRAME_COLOR if self.layer_editable else QColor("#9aa3af"), 0))
        painter.drawPolygon(polygon)
        if not self.layer_editable:
            return
        top = TF.frame_point(frame, 0.0, -0.5 if frame[3] >= 0 else 0.5)
        knob = TF.rotate_handle(frame, zoom)
        painter.drawLine(QPointF(*top), QPointF(*knob))
        size = 7 / zoom
        painter.setBrush(Qt.GlobalColor.white)
        for handle in TF.HANDLES:
            x, y = TF.frame_point(frame, *handle)
            painter.drawRect(QRectF(x - size / 2, y - size / 2, size, size))
        painter.drawEllipse(QPointF(*knob), 4.5 / zoom, 4.5 / zoom)
        if self._drag and self._drag["kind"] != "move":
            text = (f"{frame[4]:.1f}°" if self._drag["kind"] == "rotate"
                    else f"{abs(frame[2]):.0f} × {abs(frame[3]):.0f} px")
            painter.save()
            painter.resetTransform()
            pos = self.mapFromScene(self._cursor) + QPointF(14, 18).toPoint()
            font = QFont(self.font())
            font.setPointSizeF(max(9.0, font.pointSizeF() - 1))
            painter.setFont(font)
            metrics = painter.fontMetrics()
            box = QRectF(pos.x(), pos.y(), metrics.horizontalAdvance(text) + 12, metrics.height() + 6)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(20, 22, 28, 215))
            painter.drawRoundedRect(box, 5, 5)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()

    def _unit_px(self):
        return {"px": 1, "in": self.dpi, "cm": self.dpi / 2.54, "mm": self.dpi / 25.4}[self.unit]
