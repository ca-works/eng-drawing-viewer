"""Tool settings, live-size preview sliders and colour swatches for MARK."""
from __future__ import annotations

import copy
import math

from PySide6.QtCore import QEvent, QLineF, QPoint, QPointF, QRectF, QSize, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QConicalGradient, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QColorDialog, QHBoxLayout, QSizePolicy, QToolButton, QWidget

from ..theme import ThemeManager, qc
from .items import BUBBLE_RED, BubbleItem, number_path

PRESET_COLORS = [
    ("Black", "#141414"), ("Red", "#E5202A"), ("Blue", "#1E5ADC"),
    ("Green", "#1E9646"), ("Orange", "#F08214"), ("Yellow", "#FFD21E"),
]

DEFAULTS = {
    "pen": {"color": "#E5202A", "size": 2.0},
    "highlighter": {"color": "#FFD21E", "size": 12.0, "alpha": 0.42},
    "text": {"color": "#E5202A", "size": 18.0},
    "arrow": {"color": "#E5202A", "size": 2.0},
    "circle": {"color": "#E5202A", "size": 2.0},
    "bubble": {"size": 30.0, "fill": False},
    "eraser": {"size": 14.0},
    "select": {},
}

# (min, max, unit, label) — sizes are PDF points except the eraser (screen px).
RANGES = {
    "pen": (0.25, 24.0, "pt", "Thickness"),
    "highlighter": (2.0, 72.0, "pt", "Thickness"),
    "text": (4.0, 144.0, "pt", "Text size"),
    "arrow": (0.25, 24.0, "pt", "Thickness"),
    "circle": (0.25, 24.0, "pt", "Thickness"),
    "bubble": (8.0, 600.0, "pt", "Bubble size"),
    "eraser": (4.0, 80.0, "px", "Eraser size"),
}


class ToolSettings:
    def __init__(self, stored: dict | None = None):
        self._data = copy.deepcopy(DEFAULTS)
        for tool, values in (stored or {}).items():
            if tool in self._data and isinstance(values, dict):
                self._data[tool].update({k: v for k, v in values.items() if k in DEFAULTS[tool]})
        for tool, (lo, hi, _u, _l) in RANGES.items():
            self._data[tool]["size"] = min(max(float(self._data[tool]["size"]), lo), hi)

    def get(self, tool: str) -> dict:
        return self._data.get(tool, {})

    def set(self, tool: str, key: str, value):
        self._data.setdefault(tool, {})[key] = value

    def to_dict(self) -> dict:
        return copy.deepcopy(self._data)


def format_size(value: float, unit: str) -> str:
    if unit == "px":
        return f"{value:.0f} px"
    if value < 10:
        return f"{value:.2f}".rstrip("0").rstrip(".") + " pt"
    return f"{value:.0f} pt"


# --------------------------------------------------------------------------
# Sample drawing shared by the preview card
# --------------------------------------------------------------------------
def sample_extent(tool: str, st: dict, ppp: float, number: int = 1) -> QSizeF:
    size = st.get("size", 1.0)
    if tool == "pen":
        w = size * ppp
        return QSizeF(max(140.0, w * 6), max(44.0, w * 3))
    if tool == "highlighter":
        h = size * ppp
        return QSizeF(max(150.0, h * 4), max(36.0, h + 12))
    if tool in ("arrow",):
        w = size * ppp
        head = max(9.0, size * 4.8) * ppp
        return QSizeF(max(140.0, head * 3), max(52.0, head * 2 + w))
    if tool == "circle":
        w = size * ppp
        return QSizeF(max(70.0, 56 + w * 2), max(70.0, 56 + w * 2))
    if tool == "text":
        f = QFont()
        f.setPixelSize(max(1, round(size * ppp)))
        fm = QFontMetrics(f)
        return QSizeF(fm.horizontalAdvance("Aa Text 12") + 8, fm.height() + 4)
    if tool == "bubble":
        d = size * ppp + BubbleItem.stroke_for(size) * ppp + 4
        return QSizeF(d, d)
    if tool == "eraser":
        return QSizeF(size * 2 + 6, size * 2 + 6)
    return QSizeF(100, 40)


def paint_sample(p: QPainter, rect: QRectF, tool: str, st: dict, ppp: float, number: int = 1):
    """Draw ``tool`` exactly as it will appear at ``ppp`` screen px per point."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = rect.center()
    size = st.get("size", 1.0)
    color = QColor(st.get("color", "#E5202A"))
    if tool == "pen":
        w = size * ppp
        path = QPainterPath(QPointF(rect.left() + w, c.y() + rect.height() * 0.18))
        path.cubicTo(QPointF(rect.left() + rect.width() * 0.3, c.y() - rect.height() * 0.5),
                     QPointF(rect.left() + rect.width() * 0.6, c.y() + rect.height() * 0.5),
                     QPointF(rect.right() - w, c.y() - rect.height() * 0.18))
        pen = QPen(color, max(w, 0.3))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPath(path)
    elif tool == "highlighter":
        h = size * ppp
        f = QFont()
        f.setPixelSize(13)
        p.setFont(f)
        p.setPen(QColor("#3A3A40"))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Highlighted note text")
        col = QColor(color)
        col.setAlphaF(st.get("alpha", 0.35))
        pen = QPen(col, max(h, 0.5))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        p.setPen(pen)
        p.drawLine(QPointF(rect.left() + h / 2 + 4, c.y()), QPointF(rect.right() - h / 2 - 4, c.y()))
    elif tool == "arrow":
        w = size * ppp
        head = max(9.0, size * 4.8) * ppp
        a = QPointF(rect.left() + 6, rect.bottom() - head * 0.6 - 4)
        b = QPointF(rect.right() - 6, rect.top() + head * 0.6 + 4)
        line = QLineF(a, b)
        ux, uy = line.dx() / line.length(), line.dy() / line.length()
        hl = min(head, line.length() * 0.6)
        base = QPointF(b.x() - ux * hl, b.y() - uy * hl)
        left = QPointF(base.x() - uy * hl * 0.5, base.y() + ux * hl * 0.5)
        right = QPointF(base.x() + uy * hl * 0.5, base.y() - ux * hl * 0.5)
        pen = QPen(color, max(w, 0.3))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QLineF(a, base))
        hp = QPainterPath(b)
        hp.lineTo(left)
        hp.lineTo(right)
        hp.closeSubpath()
        p.setPen(QPen(color, max(w * 0.5, 0.3)))
        p.setBrush(color)
        p.drawPath(hp)
    elif tool == "circle":
        w = size * ppp
        p.setPen(QPen(color, max(w, 0.3)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, 28, 28)
    elif tool == "text":
        f = QFont()
        f.setPixelSize(max(1, round(size * ppp)))
        p.setFont(f)
        p.setPen(color)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Aa Text 12")
    elif tool == "bubble":
        d = size * ppp
        sw = BubbleItem.stroke_for(size) * ppp
        pen = QPen(BUBBLE_RED, max(sw, 0.5))
        p.setPen(pen)
        p.setBrush(QColor("#FFFFFF") if st.get("fill") else Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, d / 2, d / 2)
        path = number_path(number, d, QFont().defaultFamily())
        p.translate(c)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#000000"))
        p.drawPath(path)
    elif tool == "eraser":
        p.setPen(QPen(QColor(0, 0, 0, 160), 1))
        p.setBrush(QColor(255, 255, 255, 140))
        p.drawEllipse(c, size, size)
    p.restore()


class PreviewPopover(QWidget):
    """Floating card that shows the selected size at true on-screen scale."""

    MAX_W, MAX_H = 300.0, 220.0

    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.tool = "pen"
        self.st: dict = {}
        self.ppp = 1.0
        self.zoom_pct = 100.0
        self.number = 1
        self.unit = "pt"
        self._scale = 1.0
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def present(self, anchor: QWidget, tool: str, st: dict, ppp: float, zoom_pct: float, number: int, unit: str):
        self.tool, self.st, self.ppp, self.zoom_pct, self.number, self.unit = tool, dict(st), ppp, zoom_pct, number, unit
        ext = sample_extent(tool, st, ppp, number)
        self._scale = min(1.0, self.MAX_W / max(ext.width(), 1), self.MAX_H / max(ext.height(), 1))
        left, right = self._labels()
        fm = QFontMetrics(self._label_font())
        label_w = fm.horizontalAdvance(left) + fm.horizontalAdvance(right) + 56
        content_w = max(220.0, ext.width() * self._scale + 32, label_w)
        content_h = ext.height() * self._scale + 64
        self.resize(int(content_w), int(content_h))
        g = anchor.mapToGlobal(QPoint(anchor.width() // 2, anchor.height() + 6))
        screen = anchor.screen().availableGeometry() if anchor.screen() else None
        x = g.x() - self.width() // 2
        y = g.y()
        if screen is not None:
            x = max(screen.left() + 4, min(x, screen.right() - self.width() - 4))
            if y + self.height() > screen.bottom():
                y = anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 6
        self.move(x, y)
        self._hide_timer.stop()
        if not self.isVisible():
            self.show()
        self.update()

    def linger(self, msec: int = 900):
        self._hide_timer.start(msec)

    def paintEvent(self, event):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawRoundedRect(card.adjusted(-2, 0, 2, 3), 12, 12)
        p.setBrush(qc(t.elevated))
        p.setPen(QPen(qc(t.separator), 1))
        p.drawRoundedRect(card, 10, 10)
        # Sample area on a paper-white panel, like the drawing itself.
        area = card.adjusted(10, 10, -10, -34)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(area, 6, 6)
        ext = sample_extent(self.tool, self.st, self.ppp, self.number)
        p.save()
        p.setClipRect(area)
        p.translate(area.center())
        p.scale(self._scale, self._scale)
        sample = QRectF(-ext.width() / 2, -ext.height() / 2, ext.width(), ext.height())
        paint_sample(p, sample, self.tool, self.st, self.ppp, self.number)
        p.restore()
        p.setFont(self._label_font())
        left, right = self._labels()
        text_rect = QRectF(card.left() + 12, card.bottom() - 28, card.width() - 24, 22)
        p.setPen(qc(t.text))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, left)
        p.setPen(qc(t.text_secondary))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, right)
        p.end()

    def _label_font(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(9.0, f.pointSizeF() - 1.5))
        return f

    def _labels(self) -> tuple[str, str]:
        size = self.st.get("size", 0)
        left = format_size(size, self.unit)
        if self.unit == "pt":
            left += f" · {size * 25.4 / 72:.2f} mm"
        if self.unit == "px":
            right = "on-screen size"
        elif self._scale < 0.999:
            right = f"shown at {self._scale * 100:.0f}% · true size on canvas"
        else:
            right = f"actual size at {self.zoom_pct:.0f}% zoom"
        return left, right


class PreviewSlider(QWidget):
    """Logarithmic slider with a value label; drives the preview popover."""

    valueChanged = Signal(float)
    pressed = Signal()
    released = Signal()
    hovered = Signal(bool)

    STEPS = 1000

    def __init__(self, lo: float, hi: float, value: float, width: int = 150, log: bool = True, parent=None):
        super().__init__(parent)
        self.lo, self.hi, self.log = lo, hi, log
        self._pos = self._to_pos(value)
        self._drag = False
        self._hover = False
        self.setFixedSize(width, 22)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Size")

    def set_range(self, lo: float, hi: float, value: float):
        self.lo, self.hi = lo, hi
        self._pos = self._to_pos(value)
        self.update()

    def _to_pos(self, v: float) -> float:
        v = min(max(v, self.lo), self.hi)
        if self.log:
            return math.log(v / self.lo) / math.log(self.hi / self.lo) * self.STEPS
        return (v - self.lo) / (self.hi - self.lo) * self.STEPS

    def _to_value(self, pos: float) -> float:
        f = min(max(pos / self.STEPS, 0.0), 1.0)
        if self.log:
            v = self.lo * (self.hi / self.lo) ** f
        else:
            v = self.lo + (self.hi - self.lo) * f
        # Friendly rounding: quarter points when small, whole points when large.
        if v < 3:
            return round(v * 4) / 4
        if v < 20:
            return round(v * 2) / 2
        return float(round(v))

    def value(self) -> float:
        return self._to_value(self._pos)

    def set_value(self, v: float, emit: bool = False):
        self._pos = self._to_pos(v)
        self.update()
        if emit:
            self.valueChanged.emit(self.value())

    def step(self, direction: int, big: bool = False):
        old = self.value()
        delta = (self.STEPS * (0.08 if big else 0.035)) * direction
        self._pos = min(max(self._pos + delta, 0), self.STEPS)
        # Always move by at least one rounding step.
        if self.value() == old and 0 < self._pos < self.STEPS:
            self._pos = min(max(self._pos + delta, 0), self.STEPS)
        self.update()
        self.valueChanged.emit(self.value())

    def _track(self) -> QRectF:
        return QRectF(8, self.height() / 2 - 2, self.width() - 16, 4)

    def _set_from_x(self, x: float):
        tr = self._track()
        self._pos = min(max((x - tr.left()) / tr.width(), 0.0), 1.0) * self.STEPS
        self.update()
        self.valueChanged.emit(self.value())

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self.pressed.emit()
            self._set_from_x(e.position().x())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._set_from_x(e.position().x())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.released.emit()

    def enterEvent(self, e):
        self._hover = True
        self.hovered.emit(True)
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.hovered.emit(False)
        self.update()

    def wheelEvent(self, e):
        d = e.angleDelta().y() or e.angleDelta().x()
        if d:
            self.pressed.emit()
            self.step(1 if d > 0 else -1)
            self.released.emit()
        e.accept()

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_BracketRight):
            self.pressed.emit()
            self.step(1)
            self.released.emit()
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Down, Qt.Key.Key_BracketLeft):
            self.pressed.emit()
            self.step(-1)
            self.released.emit()
        elif k in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.pressed.emit()
            self.step(1 if k == Qt.Key.Key_PageUp else -1, big=True)
            self.released.emit()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, event):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tr = self._track()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(t.separator))
        p.drawRoundedRect(tr, 2, 2)
        f = self._pos / self.STEPS
        filled = QRectF(tr.left(), tr.top(), tr.width() * f, tr.height())
        p.setBrush(qc(t.accent))
        p.drawRoundedRect(filled, 2, 2)
        kx = tr.left() + tr.width() * f
        r = 7.5 if (self._hover or self._drag) else 7.0
        p.setBrush(QColor(0, 0, 0, 50))
        p.drawEllipse(QPointF(kx, self.height() / 2 + 1), r, r)
        p.setBrush(QColor("#FFFFFF"))
        p.setPen(QPen(QColor(0, 0, 0, 40), 0.8))
        p.drawEllipse(QPointF(kx, self.height() / 2), r, r)
        if self.hasFocus():
            p.setPen(QPen(qc(t.accent), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(kx, self.height() / 2), r + 2.5, r + 2.5)
        p.end()


class ColorSwatches(QWidget):
    colorChosen = Signal(QColor)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        self._buttons: list[tuple[QToolButton, QColor]] = []
        for name, hexv in PRESET_COLORS:
            b = _Swatch(QColor(hexv), name)
            b.clicked.connect(lambda _=False, c=QColor(hexv): self._choose(c))
            lay.addWidget(b)
            self._buttons.append((b, QColor(hexv)))
        self.custom = _Swatch(None, "Custom colour…")
        self.custom.clicked.connect(self._pick)
        lay.addWidget(self.custom)
        self._current = QColor(PRESET_COLORS[1][1])

    def set_current(self, color: QColor):
        self._current = QColor(color)
        matched = False
        for b, c in self._buttons:
            on = c.rgb() == self._current.rgb()
            b.set_selected(on)
            matched = matched or on
        self.custom.set_selected(not matched)
        self.custom.custom_color = None if matched else QColor(color)
        self.custom.update()

    def _choose(self, c: QColor):
        self.set_current(c)
        self.colorChosen.emit(QColor(c))

    def _pick(self):
        c = QColorDialog.getColor(self._current, self.window(), "Annotation colour")
        if c.isValid():
            self._choose(c)


class _Swatch(QToolButton):
    def __init__(self, color: QColor | None, name: str):
        super().__init__()
        self.color = color
        self.custom_color = None
        self._selected = False
        self.setFixedSize(22, 22)
        self.setToolTip(name)
        self.setAccessibleName(f"Colour {name}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def set_selected(self, on: bool):
        self._selected = on
        self.update()

    def paintEvent(self, event):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        if self._selected:
            p.setPen(QPen(qc(t.accent), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        if self.color is None:
            if self.custom_color is not None:
                p.setBrush(self.custom_color)
            else:
                g = QConicalGradient(r.center(), 90)
                for i, h in enumerate((0, 60, 120, 180, 240, 300, 360)):
                    g.setColorAt(i / 6, QColor.fromHsv(h % 360, 200, 240))
                p.setBrush(g)
        else:
            p.setBrush(self.color)
        p.setPen(QPen(QColor(0, 0, 0, 60) if not t.dark else QColor(255, 255, 255, 60), 1))
        p.drawEllipse(r)
        if self.underMouse() and not self._selected:
            p.setPen(QPen(qc(t.text_tertiary), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        p.end()

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()
