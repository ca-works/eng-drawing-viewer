"""MARK annotation items.

All geometry is in PDF points (the scene unit), so annotations stay vector-
sharp at every zoom level and export resolution. Nothing here touches the PDF.
"""
from __future__ import annotations

import math
from datetime import date
from functools import lru_cache

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

    # --- geometry state (resize / clipboard) -----------------------------
    def geom_rect(self) -> QRectF:
        """Geometric bounds in scene coordinates (ignores stroke width)."""
        return self.sceneBoundingRect()

    def get_state(self) -> dict:
        return {"pos": QPointF(self.pos())}

    def set_state(self, st: dict):
        self.prepareGeometryChange()
        self.setPos(st["pos"])
        self._apply_state(st)
        self.update()

    def _apply_state(self, st: dict):
        pass

    def scaled_state(self, st: dict, old: QRectF, new: QRectF) -> dict:
        return dict(st)

    def to_dict(self) -> dict:
        d = {"type": self.kind, "x": self.pos().x(), "y": self.pos().y()}
        d.update(self._extra_dict())
        return d

    def _extra_dict(self) -> dict:
        return {}

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
        painter.restore()


def _pen(color: QColor, width: float, cap=Qt.PenCapStyle.RoundCap) -> QPen:
    pen = QPen(color, max(width, 0.05))
    pen.setCapStyle(cap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


SYMBOL_TOOLS = ("checkmark", "cross")


def symbol_path(kind: str, width: float, height: float) -> QPainterPath:
    x, y = width / 2, height / 2
    if kind == "checkmark":
        path = QPainterPath(QPointF(-x, y * 0.15))
        path.lineTo(-x * 0.3, y)
        path.lineTo(x, -y)
    else:
        path = QPainterPath(QPointF(-x, -y))
        path.lineTo(x, y)
        path.moveTo(x, -y)
        path.lineTo(-x, y)
    return path


class SymbolItem(AnnotationMixin, QGraphicsItem):
    def __init__(self, kind: str, color: QColor, size: float, width: float, height: float | None = None):
        super().__init__()
        self._init_annotation()
        self.kind, self.color, self.size, self.width = kind, QColor(color), size, width
        self.height = size if height is None else height
        self.setZValue(Z_SHAPES)

    def geom_rect(self) -> QRectF:
        return QRectF(self.pos() - QPointF(self.size / 2, self.height / 2),
                      self.pos() + QPointF(self.size / 2, self.height / 2))

    def boundingRect(self) -> QRectF:
        pad = max(6.0, self.width) / 2 + 1
        return QRectF(-self.size / 2 - pad, -self.height / 2 - pad,
                      self.size + 2 * pad, self.height + 2 * pad)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker(_pen(self.color, max(6.0, self.width)))
        return stroker.createStroke(symbol_path(self.kind, self.size, self.height))

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(_pen(self.color, self.width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(symbol_path(self.kind, self.size, self.height))
        self.paint_chrome(painter, self.boundingRect())

    def style(self) -> dict:
        return {"color": QColor(self.color), "size": self.size, "width": self.width}

    def set_style(self, style: dict):
        self.prepareGeometryChange()
        size = style.get("size", self.size)
        self.height *= size / self.size
        self.size = size
        self.width = style.get("width", self.width)
        self.color = QColor(style.get("color", self.color))
        self.update()

    def get_state(self) -> dict:
        return {**super().get_state(), "size": self.size, "height": self.height}

    def _apply_state(self, state: dict):
        self.size, self.height = state["size"], state["height"]

    def scaled_state(self, state: dict, old: QRectF, new: QRectF) -> dict:
        sx, sy = _scales(old, new)
        return {"pos": map_point(state["pos"], old, new), "size": state["size"] * sx, "height": state["height"] * sy}

    def _extra_dict(self) -> dict:
        return {"color": _col(self.color), "size": self.size, "width": self.width, "height": self.height}


STAMP_TOOLS = ("pass", "fail")
MIN_STAMP_WIDTH = 54.0
MIN_STAMP_HEIGHT = 24.0
STAMP_HEIGHT_RATIO = 0.42


def placement_date() -> str:
    return date.today().strftime("%d/%m/%Y")


def stamp_bounds(width, height, show_date):
    band = min(height * 0.44, width * 0.2) if show_date else 0
    return QRectF(-width / 2, -height / 2, width, height + band)


@lru_cache(maxsize=128)
def _stamp_lettering(text, bold):
    font = QFont()
    font.setBold(bold)
    font.setPixelSize(100)
    path = QPainterPath()
    path.addText(0, 0, font, text)
    return path


def _stamp_text(painter, text, box, bold):
    path = _stamp_lettering(text, bold)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        return
    scale = min(box.width() / bounds.width(), box.height() / bounds.height())
    painter.save()
    painter.translate(box.center())
    painter.scale(scale, scale)
    painter.translate(-bounds.center())
    painter.drawPath(path)
    painter.restore()


def paint_stamp(painter, kind, width, height, color, opacity, date_text):
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setOpacity(painter.opacity() * opacity)
    border = stamp_bounds(width, height, False)
    unit = min(height, width * STAMP_HEIGHT_RATIO)
    stroke = unit * 0.065
    radius = unit * 0.18
    painter.setPen(_pen(color, stroke))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(border.adjusted(stroke / 2, stroke / 2, -stroke / 2, -stroke / 2), radius, radius)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    _stamp_text(painter, kind.upper(), QRectF(-width * 0.36, -height * 0.24, width * 0.72, height * 0.48), True)
    if date_text:
        band = stamp_bounds(width, height, True).height() - height
        _stamp_text(painter, date_text, QRectF(-width * 0.42, height / 2 + band * 0.27, width * 0.84, band * 0.68), False)
    painter.restore()


class StampItem(AnnotationMixin, QGraphicsItem):
    """An inspection stamp with a fixed local placement date."""

    def __init__(self, kind, color, size, alpha, show_date, placed, height=None):
        super().__init__()
        self._init_annotation()
        self.kind, self.color, self.size = kind, QColor(color), size
        self.alpha, self.show_date, self.placed = alpha, show_date, placed
        self.height = max(MIN_STAMP_HEIGHT, size * STAMP_HEIGHT_RATIO) if height is None else height
        self.setZValue(Z_SHAPES)

    def boundingRect(self):
        return stamp_bounds(self.size, self.height, self.show_date)

    def geom_rect(self):
        return self.boundingRect().translated(self.pos())

    def shape(self):
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def paint(self, painter, option, widget=None):
        paint_stamp(painter, self.kind, self.size, self.height, self.color, self.alpha,
                    self.placed if self.show_date else "")
        self.paint_chrome(painter, self.boundingRect())

    def style(self):
        return {"color": QColor(self.color), "size": self.size, "alpha": self.alpha, "show_date": self.show_date}

    def set_style(self, style):
        self.prepareGeometryChange()
        size = style.get("size", self.size)
        self.height = max(MIN_STAMP_HEIGHT, self.height * size / self.size)
        self.size = size
        self.color = QColor(style.get("color", self.color))
        self.alpha = style.get("alpha", self.alpha)
        self.show_date = style.get("show_date", self.show_date)
        self.update()

    def get_state(self):
        return {**super().get_state(), "size": self.size, "height": self.height}

    def _apply_state(self, state):
        self.size, self.height = state["size"], state["height"]

    def scaled_state(self, state, old, new):
        sx, sy = _scales(old, new)
        return {"pos": map_point(state["pos"], old, new), "size": max(MIN_STAMP_WIDTH, state["size"] * sx),
                "height": max(MIN_STAMP_HEIGHT, state["height"] * sy)}

    def _extra_dict(self):
        return {**self.style(), "color": _col(self.color), "placed": self.placed, "height": self.height}


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
                  fill: bool = False, screen_px: float = 0.0, width: float = 2.0, show_date: bool = False):
        self.prepareGeometryChange()
        self.tool, self.color, self.size, self.alpha = tool, QColor(color), float(size), alpha
        self.number, self.fill, self.screen_px = number, fill, screen_px
        self.width = width
        self.show_date = show_date
        if tool == "bubble":
            self._num_path = number_path(number, size, self.font_family)
        self.update()

    def _extent(self) -> float:
        if self.tool in STAMP_TOOLS:
            return self.size + 2
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
        elif t in STAMP_TOOLS:
            paint_stamp(painter, t, self.size, max(MIN_STAMP_HEIGHT, self.size * STAMP_HEIGHT_RATIO),
                        self.color, self.alpha * 0.65, placement_date() if self.show_date else "")
        elif t in SYMBOL_TOOLS:
            painter.setOpacity(0.55)
            painter.setPen(_pen(self.color, self.width))
            painter.drawPath(symbol_path(t, self.size, self.size))
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


# ---------------------------------------------------------------- resize maths
def _scales(old: QRectF, new: QRectF) -> tuple[float, float]:
    sx = new.width() / old.width() if old.width() > 1e-6 else 1.0
    sy = new.height() / old.height() if old.height() > 1e-6 else 1.0
    return sx, sy


def map_point(p: QPointF, old: QRectF, new: QRectF) -> QPointF:
    sx, sy = _scales(old, new)
    x = new.left() + (p.x() - old.left()) * sx if old.width() > 1e-6 else p.x() + new.left() - old.left()
    y = new.top() + (p.y() - old.top()) * sy if old.height() > 1e-6 else p.y() + new.top() - old.top()
    return QPointF(x, y)


def _col(c: QColor) -> str:
    return c.name(QColor.NameFormat.HexArgb)


def _pt(p: QPointF) -> list:
    return [p.x(), p.y()]


def _unpt(v) -> QPointF:
    return QPointF(v[0], v[1])


# Stroke
StrokeItem.geom_rect = lambda self: self._path.boundingRect().translated(self.pos())
StrokeItem.get_state = lambda self: {"pos": QPointF(self.pos()), "points": [QPointF(p) for p in self.points]}


def _stroke_apply(self, st):
    self.points = [QPointF(p) for p in st["points"]]
    self._rebuild()


def _stroke_scaled(self, st, old, new):
    pos = st["pos"]
    return {"pos": pos, "points": [map_point(p + pos, old, new) - pos for p in st["points"]]}


StrokeItem._apply_state = _stroke_apply
StrokeItem.scaled_state = _stroke_scaled
StrokeItem._extra_dict = lambda self: {"color": _col(self.color), "width": self.width, "alpha": self.alpha,
                                       "highlighter": self.highlighter, "points": [_pt(p) for p in self.points]}


# Arrow
ArrowItem.geom_rect = lambda self: QRectF(self.p1, self.p2).normalized().translated(self.pos())
ArrowItem.get_state = lambda self: {"pos": QPointF(self.pos()), "p1": QPointF(self.p1), "p2": QPointF(self.p2)}


def _arrow_apply(self, st):
    self.p1, self.p2 = QPointF(st["p1"]), QPointF(st["p2"])


def _arrow_scaled(self, st, old, new):
    pos = st["pos"]
    return {"pos": pos, "p1": map_point(st["p1"] + pos, old, new) - pos, "p2": map_point(st["p2"] + pos, old, new) - pos}


ArrowItem._apply_state = _arrow_apply
ArrowItem.scaled_state = _arrow_scaled
ArrowItem._extra_dict = lambda self: {"color": _col(self.color), "width": self.width, "p1": _pt(self.p1), "p2": _pt(self.p2)}


# Ellipse
EllipseItem.geom_rect = lambda self: self.rect.translated(self.pos())
EllipseItem.get_state = lambda self: {"pos": QPointF(self.pos()), "rect": QRectF(self.rect)}


def _ell_apply(self, st):
    self.rect = QRectF(st["rect"])


def _ell_scaled(self, st, old, new):
    pos = st["pos"]
    r = st["rect"].translated(pos)
    a, b = map_point(r.topLeft(), old, new), map_point(r.bottomRight(), old, new)
    return {"pos": pos, "rect": QRectF(a, b).normalized().translated(-pos)}


EllipseItem._apply_state = _ell_apply
EllipseItem.scaled_state = _ell_scaled
EllipseItem._extra_dict = lambda self: {"color": _col(self.color), "width": self.width,
                                        "rect": [self.rect.x(), self.rect.y(), self.rect.width(), self.rect.height()]}


# Text
TextItem.get_state = lambda self: {"pos": QPointF(self.pos()), "size": self.size_pt}


def _text_apply(self, st):
    self.size_pt = float(st["size"])
    self._apply()


def _text_scaled(self, st, old, new):
    sx, sy = _scales(old, new)
    k = max((abs(sx) + abs(sy)) / 2, 0.05)
    return {"pos": map_point(st["pos"], old, new), "size": max(2.0, st["size"] * k)}


TextItem._apply_state = _text_apply
TextItem.scaled_state = _text_scaled
TextItem._extra_dict = lambda self: {"color": _col(self.color), "size": self.size_pt, "text": self.toPlainText()}


# Bubble
def _bub_geom(self):
    r = self.diameter / 2
    return QRectF(self.pos().x() - r, self.pos().y() - r, self.diameter, self.diameter)


def _bub_apply(self, st):
    self.diameter = float(st["diameter"])
    self._num_path = number_path(self.number, self.diameter, self._font_family)


def _bub_scaled(self, st, old, new):
    sx, sy = _scales(old, new)
    k = max((abs(sx) + abs(sy)) / 2, 0.05)
    return {"pos": map_point(st["pos"], old, new), "diameter": max(4.0, st["diameter"] * k)}


BubbleItem.geom_rect = _bub_geom
BubbleItem.get_state = lambda self: {"pos": QPointF(self.pos()), "diameter": self.diameter}
BubbleItem._apply_state = _bub_apply
BubbleItem.scaled_state = _bub_scaled
BubbleItem._extra_dict = lambda self: {"diameter": self.diameter, "fill": self.fill, "number": self.number,
                                       "comment": self.comment, "comment_removed": self.comment_removed}


def item_from_dict(d: dict, font_family: str = ""):
    """Rebuild an editable annotation from ``to_dict`` output (None if unknown)."""
    t = d.get("type")
    if t in ("pen", "highlighter"):
        it = StrokeItem(QColor(d["color"]), d["width"], bool(d.get("highlighter")), d.get("alpha", 1.0))
        it.points = [_unpt(p) for p in d["points"]]
        it._rebuild()
    elif t in STAMP_TOOLS:
        it = StampItem(t, QColor(d["color"]), d["size"], d["alpha"], d["show_date"], d["placed"], d.get("height"))
    elif t in SYMBOL_TOOLS:
        it = SymbolItem(t, QColor(d["color"]), d["size"], d["width"], d.get("height"))
    elif t == "arrow":
        it = ArrowItem(_unpt(d["p1"]), _unpt(d["p2"]), QColor(d["color"]), d["width"])
    elif t == "circle":
        it = EllipseItem(QRectF(*d["rect"]), QColor(d["color"]), d["width"])
    elif t == "text":
        it = TextItem(d.get("text", ""), QColor(d["color"]), d["size"])
    elif t == "bubble":
        it = BubbleItem(d.get("number", 1), d["diameter"], d.get("fill", False), font_family)
        it.comment = d.get("comment", "")
        it.comment_removed = d.get("comment_removed", False)
    else:
        return None
    it.setPos(d.get("x", 0.0), d.get("y", 0.0))
    return it


class SelectionOverlay(QGraphicsItem):
    """Bounding box with eight resize handles around the current selection.

    Purely visual: the canvas does the hit-testing so handles keep a constant
    on-screen size and grab area at every zoom level.
    """

    HANDLE_PX = 8.0
    PAD_PX = 6.0

    def __init__(self):
        super().__init__()
        self.setZValue(Z_GHOST - 1)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.box = QRectF()
        self.scale_ = 1.0
        self.setVisible(False)

    def set_box(self, box: QRectF, view_scale: float):
        self.prepareGeometryChange()
        self.box = QRectF(box)
        self.scale_ = max(view_scale, 1e-6)
        self.setVisible(not box.isNull() or box.width() > 0 or box.height() > 0)
        self.update()

    def padded(self) -> QRectF:
        pad = self.PAD_PX / self.scale_
        return self.box.adjusted(-pad, -pad, pad, pad)

    def handle_points(self) -> dict:
        r = self.padded()
        cx, cy = r.center().x(), r.center().y()
        return {
            "nw": QPointF(r.left(), r.top()), "n": QPointF(cx, r.top()), "ne": QPointF(r.right(), r.top()),
            "e": QPointF(r.right(), cy), "se": QPointF(r.right(), r.bottom()), "s": QPointF(cx, r.bottom()),
            "sw": QPointF(r.left(), r.bottom()), "w": QPointF(r.left(), cy),
        }

    def boundingRect(self) -> QRectF:
        m = (self.PAD_PX + self.HANDLE_PX + 4) / self.scale_
        return self.box.adjusted(-m, -m, m, m)

    def paint(self, painter: QPainter, option, widget=None):
        if AnnotationMixin.exporting or not self.isVisible():
            return
        accent = qc(ThemeManager.current().accent)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(accent, 1.2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.padded())
        painter.setBrush(QColor("#FFFFFF"))
        h = self.HANDLE_PX / 2 / self.scale_
        for pt in self.handle_points().values():
            painter.drawRect(QRectF(pt.x() - h, pt.y() - h, 2 * h, 2 * h))
        painter.restore()
