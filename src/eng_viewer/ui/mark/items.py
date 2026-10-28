"""MARK annotation items.

All geometry is in PDF points (the scene unit), so annotations stay vector-
sharp at every zoom level and export resolution. Nothing here touches the PDF.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen, QTextCursor
from PySide6.QtWidgets import QGraphicsItem, QGraphicsTextItem, QStyle, QStyleOptionGraphicsItem

from ..theme import ThemeManager, qc

ANNOTATION = "eng_annotation"
BUBBLE_RED = QColor(232, 26, 30)
Z_SHAPES = 10
Z_BUBBLES = 20
Z_GHOST = 1_000_000


class AnnotationMixin:
    """Shared behaviour: identification, style snapshots, selection chrome."""

    kind = "annotation"
    exporting = False  # class-wide switch: hide selection/hover chrome when rendering output

    def _init_annotation(self):
        self.setData(0, ANNOTATION)
        self.setAcceptHoverEvents(True)
        self._hover = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def set_interactive(self, on: bool):
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, on)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, on)
        if not on:
            self.setSelected(False)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def style(self) -> dict:
        return {}

    def set_style(self, style: dict):
        pass

    def paint_chrome(self, painter: QPainter, rect: QRectF):
        if AnnotationMixin.exporting:
            return
        selected = self.isSelected()
        hover = self._hover and bool(self.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        if not (selected or hover):
            return
        t = ThemeManager.current()
        accent = qc(t.accent)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(accent, 1.6 if selected else 1.0)
        pen.setCosmetic(True)
        if not selected:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        fill = QColor(accent)
        fill.setAlphaF(0.08 if selected else 0.0)
        painter.setBrush(fill)
        scale = painter.worldTransform().m11() or 1.0
        pad = 4.0 / scale
        r = rect.adjusted(-pad, -pad, pad, pad)
        painter.drawRoundedRect(r, 3 / scale, 3 / scale)
        if selected:
            painter.setBrush(QColor("#FFFFFF"))
            hs = 3.0 / scale
            for c in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
                painter.drawEllipse(c, hs, hs)
        painter.restore()


def _pen(color: QColor, width: float, cap=Qt.PenCapStyle.RoundCap) -> QPen:
    pen = QPen(color, max(width, 0.05))
    pen.setCapStyle(cap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


class StrokeItem(AnnotationMixin, QGraphicsItem):
    """Freehand pen or highlighter stroke."""

    def __init__(self, color: QColor, width: float, highlighter: bool = False, opacity: float = 1.0):
        super().__init__()
        self._init_annotation()
        self.kind = "highlighter" if highlighter else "pen"
        self.color = QColor(color)
        self.width = float(width)
        self.highlighter = highlighter
        self.alpha = opacity
        self.points: list[QPointF] = []
        self._path = QPainterPath()
        self.setZValue(Z_SHAPES - (1 if highlighter else 0))

    def add_point(self, p: QPointF):
        self.prepareGeometryChange()
        self.points.append(QPointF(p))
        self._rebuild()

    def _rebuild(self):
        pts = self.points
        path = QPainterPath()
        if not pts:
            self._path = path
            return
        path.moveTo(pts[0])
        if len(pts) == 1:
            path.lineTo(pts[0] + QPointF(0.01, 0))
        elif len(pts) == 2:
            path.lineTo(pts[1])
        else:
            # Quadratic smoothing through midpoints: removes mouse jitter.
            for i in range(1, len(pts) - 1):
                mid = (pts[i] + pts[i + 1]) / 2
                path.quadTo(pts[i], mid)
            path.lineTo(pts[-1])
        self._path = path

    def boundingRect(self) -> QRectF:
        m = self.width / 2 + 1
        return self._path.boundingRect().adjusted(-m, -m, m, m)

    def shape(self) -> QPainterPath:
        st = QPainterPathStroker()
        st.setWidth(max(self.width, 6.0))
        st.setCapStyle(Qt.PenCapStyle.RoundCap)
        st.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return st.createStroke(self._path)

    def paint(self, painter: QPainter, option, widget=None):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self.color)
        if self.highlighter:
            color.setAlphaF(self.alpha)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            painter.setPen(_pen(color, self.width, Qt.PenCapStyle.RoundCap))
        else:
            painter.setPen(_pen(color, self.width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._path)
        painter.restore()
        self.paint_chrome(painter, self._path.boundingRect().adjusted(-self.width / 2, -self.width / 2, self.width / 2, self.width / 2))

    def style(self) -> dict:
        return {"color": QColor(self.color), "width": self.width, "alpha": self.alpha}

    def set_style(self, style: dict):
        self.prepareGeometryChange()
        self.color = QColor(style.get("color", self.color))
        self.width = float(style.get("width", self.width))
        self.alpha = float(style.get("alpha", self.alpha))
        self.update()


class ArrowItem(AnnotationMixin, QGraphicsItem):
    kind = "arrow"

    def __init__(self, p1: QPointF, p2: QPointF, color: QColor, width: float):
        super().__init__()
        self._init_annotation()
        self.p1, self.p2 = QPointF(p1), QPointF(p2)
        self.color = QColor(color)
        self.width = float(width)
        self.setZValue(Z_SHAPES)

    def set_end(self, p2: QPointF):
        self.prepareGeometryChange()
        self.p2 = QPointF(p2)
        self.update()

    def head_length(self) -> float:
        return max(9.0, self.width * 4.8)

    def _geometry(self):
        line = QLineF(self.p1, self.p2)
        length = max(line.length(), 1e-6)
        ux, uy = (self.p2.x() - self.p1.x()) / length, (self.p2.y() - self.p1.y()) / length
        head = min(self.head_length(), length * 0.6)
        wing = head * 0.5
        base = QPointF(self.p2.x() - ux * head, self.p2.y() - uy * head)
        left = QPointF(base.x() - uy * wing, base.y() + ux * wing)
        right = QPointF(base.x() + uy * wing, base.y() - ux * wing)
        return base, left, right

    def boundingRect(self) -> QRectF:
        m = self.head_length() + self.width
        return QRectF(self.p1, self.p2).normalized().adjusted(-m, -m, m, m)

    def shape(self) -> QPainterPath:
        path = QPainterPath(self.p1)
        path.lineTo(self.p2)
        st = QPainterPathStroker()
        st.setWidth(max(self.width * 2, 8.0))
        out = st.createStroke(path)
        base, left, right = self._geometry()
        head = QPainterPath(self.p2)
        head.lineTo(left)
        head.lineTo(right)
        head.closeSubpath()
        return out.united(head)

    def paint(self, painter: QPainter, option, widget=None):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base, left, right = self._geometry()
        painter.setPen(_pen(self.color, self.width, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QLineF(self.p1, base))
        head = QPainterPath(self.p2)
        head.lineTo(left)
        head.lineTo(right)
        head.closeSubpath()
        painter.setPen(_pen(self.color, max(self.width * 0.5, 0.3)))
        painter.setBrush(self.color)
        painter.drawPath(head)
        painter.restore()
        self.paint_chrome(painter, QRectF(self.p1, self.p2).normalized().adjusted(-self.width, -self.width, self.width, self.width))

    def style(self) -> dict:
        return {"color": QColor(self.color), "width": self.width}

    def set_style(self, style: dict):
        self.prepareGeometryChange()
        self.color = QColor(style.get("color", self.color))
        self.width = float(style.get("width", self.width))
        self.update()


class EllipseItem(AnnotationMixin, QGraphicsItem):
    kind = "circle"

    def __init__(self, rect: QRectF, color: QColor, width: float):
        super().__init__()
        self._init_annotation()
        self.rect = QRectF(rect)
        self.color = QColor(color)
        self.width = float(width)
        self.setZValue(Z_SHAPES)

    def set_rect(self, rect: QRectF):
        self.prepareGeometryChange()
        self.rect = QRectF(rect).normalized()
        self.update()

    def boundingRect(self) -> QRectF:
        m = self.width / 2 + 1
        return self.rect.adjusted(-m, -m, m, m)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(self.rect)
        st = QPainterPathStroker()
        st.setWidth(max(self.width, 8.0))
        return st.createStroke(path)

    def paint(self, painter: QPainter, option, widget=None):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(_pen(self.color, self.width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(self.rect)
        painter.restore()
        self.paint_chrome(painter, self.rect.adjusted(-self.width / 2, -self.width / 2, self.width / 2, self.width / 2))

    def style(self) -> dict:
        return {"color": QColor(self.color), "width": self.width}

    def set_style(self, style: dict):
        self.prepareGeometryChange()
        self.color = QColor(style.get("color", self.color))
        self.width = float(style.get("width", self.width))
        self.update()


class TextItem(AnnotationMixin, QGraphicsTextItem):
    kind = "text"

    def __init__(self, text: str, color: QColor, size_pt: float):
        super().__init__(text)
        self._init_annotation()
        self.size_pt = float(size_pt)
        self.color = QColor(color)
        self._apply()
        self.setZValue(Z_SHAPES + 1)
        self.document().setDocumentMargin(2)
        self.on_edit_finished = None  # set by the canvas

    def _apply(self):
        f = QFont(self.font())
        f.setPixelSize(max(1, round(self.size_pt)))  # 1 scene unit == 1 pt
        self.setFont(f)
        self.setDefaultTextColor(self.color)

    def start_editing(self):
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cur)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self.textInteractionFlags() != Qt.TextInteractionFlag.NoTextInteraction:
            self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            cur = self.textCursor()
            cur.clearSelection()
            self.setTextCursor(cur)
            if self.on_edit_finished:
                self.on_edit_finished(self)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget=None):
        opt = QStyleOptionGraphicsItem(option)
        opt.state &= ~QStyle.StateFlag.State_Selected
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, opt, widget)
        if self.textInteractionFlags() == Qt.TextInteractionFlag.NoTextInteraction:
            self.paint_chrome(painter, self.boundingRect())
        elif not AnnotationMixin.exporting:
            painter.save()
            pen = QPen(qc(ThemeManager.current().accent), 1)
            pen.setCosmetic(True)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(self.boundingRect())
            painter.restore()

    def style(self) -> dict:
        return {"color": QColor(self.color), "size": self.size_pt}

    def set_style(self, style: dict):
        self.color = QColor(style.get("color", self.color))
        self.size_pt = float(style.get("size", self.size_pt))
        self._apply()


def number_path(n: int, diameter: float, font_family: str) -> QPainterPath:
    """Number outline scaled and *optically* centred in a circle of ``diameter``."""
    text = str(n)
    f = QFont(font_family)
    f.setBold(True)
    f.setPixelSize(100)
    path = QPainterPath()
    path.addText(0, 0, f, text)
    br = path.boundingRect()
    if br.isEmpty():
        return path
    target_h = diameter * (0.50 if len(text) < 3 else 0.40)
    target_w = diameter * (0.62 if len(text) < 3 else 0.70)
    s = min(target_h / br.height(), target_w / br.width())
    from PySide6.QtGui import QTransform

    t = QTransform()
    t.scale(s, s)
    t.translate(-br.center().x(), -br.center().y())
    return t.map(path)


class BubbleItem(AnnotationMixin, QGraphicsItem):
    """Numbered red callout circle with a centred black number."""

    kind = "bubble"

    def __init__(self, number: int, diameter: float, fill: bool = False, font_family: str = ""):
        super().__init__()
        self._init_annotation()
        self.number = int(number)
        self.diameter = float(diameter)
        self.fill = fill
        self.comment = ""
        self.comment_removed = False
        self._font_family = font_family or QFont().defaultFamily()
        self._num_path = number_path(self.number, self.diameter, self._font_family)
        self._pulse = 0.0
        self.setZValue(Z_BUBBLES)

    @staticmethod
    def stroke_for(diameter: float) -> float:
        return min(max(diameter * 0.075, 0.8), 14.0)

    def set_number(self, n: int):
        self.number = int(n)
        self._num_path = number_path(self.number, self.diameter, self._font_family)
        self.update()

    def set_pulse(self, value: float):
        self._pulse = value
        self.update()

    def boundingRect(self) -> QRectF:
        r = self.diameter / 2 + self.stroke_for(self.diameter) + 2
        extra = self.diameter * 0.35 * self._pulse
        return QRectF(-r - extra, -r - extra, 2 * (r + extra), 2 * (r + extra))

    def shape(self) -> QPainterPath:
        p = QPainterPath()
        r = self.diameter / 2 + self.stroke_for(self.diameter) / 2
        p.addEllipse(QPointF(0, 0), r, r)
        return p

    def paint(self, painter: QPainter, option, widget=None):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.diameter / 2
        sw = self.stroke_for(self.diameter)
        if self._pulse > 0 and not AnnotationMixin.exporting:
            glow = QColor(qc(ThemeManager.current().accent))
            glow.setAlphaF(0.45 * (1 - self._pulse))
            gp = QPen(glow, sw * 2.5)
            painter.setPen(gp)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            rr = r + self.diameter * 0.35 * self._pulse
            painter.drawEllipse(QPointF(0, 0), rr, rr)
        painter.setPen(_pen(BUBBLE_RED, sw))
        painter.setBrush(QColor("#FFFFFF") if self.fill else Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(0, 0), r, r)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#000000"))
        painter.drawPath(self._num_path)
        painter.restore()
        self.paint_chrome(painter, QRectF(-r - sw / 2, -r - sw / 2, 2 * r + sw, 2 * r + sw))

    def style(self) -> dict:
        return {"size": self.diameter, "fill": self.fill}

    def set_style(self, style: dict):
        self.prepareGeometryChange()
        self.diameter = float(style.get("size", self.diameter))
        self.fill = bool(style.get("fill", self.fill))
        self._num_path = number_path(self.number, self.diameter, self._font_family)
        self.update()


class GhostItem(QGraphicsItem):
    """Live, exact-size preview of what the active tool will place."""

    def __init__(self):
        super().__init__()
        self.setZValue(Z_GHOST)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setVisible(False)
        self.tool = "pen"
        self.color = QColor("red")
        self.size = 2.0
        self.alpha = 1.0
        self.number = 1
        self.fill = False
        self.font_family = QFont().defaultFamily()
        self.screen_px = 0.0  # eraser radius is in screen pixels
        self._num_path = QPainterPath()

    def configure(self, tool: str, color: QColor, size: float, alpha: float = 1.0, number: int = 1,
                  fill: bool = False, screen_px: float = 0.0):
        self.prepareGeometryChange()
        self.tool, self.color, self.size, self.alpha = tool, QColor(color), float(size), alpha
        self.number, self.fill, self.screen_px = number, fill, screen_px
        if tool == "bubble":
            self._num_path = number_path(number, size, self.font_family)
        self.update()

    def _extent(self) -> float:
        if self.tool == "bubble":
            return self.size / 2 + BubbleItem.stroke_for(self.size) + 2
        if self.tool == "text":
            return self.size * 3
        if self.tool == "eraser":
            return 2000.0
        return self.size / 2 + 40

    def boundingRect(self) -> QRectF:
        e = self._extent()
        return QRectF(-e, -e, 2 * e, 2 * e)

    def paint(self, painter: QPainter, option, widget=None):
        scale = painter.worldTransform().m11() or 1.0
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.tool
        if t == "bubble":
            r = self.size / 2
            painter.setOpacity(0.55)
            painter.setPen(_pen(BUBBLE_RED, BubbleItem.stroke_for(self.size)))
            painter.setBrush(QColor("#FFFFFF") if self.fill else Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), r, r)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#000000"))
            painter.drawPath(self._num_path)
        elif t in ("pen", "highlighter"):
            c = QColor(self.color)
            c.setAlphaF(0.35 if t == "pen" else min(0.5, self.alpha))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawEllipse(QPointF(0, 0), self.size / 2, self.size / 2)
            ring = QPen(QColor(0, 0, 0, 140), 1)
            ring.setCosmetic(True)
            painter.setPen(ring)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), max(self.size / 2, 2 / scale), max(self.size / 2, 2 / scale))
        elif t == "eraser":
            r = self.screen_px / scale
            pen = QPen(QColor(0, 0, 0, 170), 1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QColor(255, 255, 255, 90))
            painter.drawEllipse(QPointF(0, 0), r, r)
        elif t == "text":
            f = QFont()
            f.setPixelSize(max(1, round(self.size)))
            painter.setFont(f)
            c = QColor(self.color)
            c.setAlphaF(0.5)
            painter.setPen(c)
            painter.drawText(QPointF(2, self.size * 0.9), "Text")
        elif t in ("arrow", "circle"):
            c = QColor(self.color)
            c.setAlphaF(0.6)
            painter.setPen(_pen(c, self.size))
            arm = max(6.0 / scale, self.size * 2)
            painter.drawLine(QPointF(-arm, 0), QPointF(arm, 0))
            painter.drawLine(QPointF(0, -arm), QPointF(0, arm))
        painter.restore()
