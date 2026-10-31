"""Ruler strips and grid spacing shared by the image and vector canvases.

Rulers are painted in viewport pixels over a ``QGraphicsView`` with major,
half and minor tick marks, labels in the chosen unit, and a marker that follows
the pointer, like the rulers in Photoshop, Illustrator, GIMP and Inkscape.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QCursor, QFont, QPen

RULER = 20  # strip thickness in viewport pixels


def nice_step(minimum: float) -> float:
    """The smallest 1, 2 or 5 × 10ⁿ that is at least ``minimum``."""
    minimum = max(minimum, 1e-9)
    power = 10 ** math.floor(math.log10(minimum))
    for factor in (1, 2, 5, 10):
        if factor * power >= minimum * (1 - 1e-9):
            return factor * power
    return 10 * power


def ruler_steps(pixels_per_unit: float, major_px: float = 64, minor_px: float = 6) -> tuple[float, int]:
    """Major step in units and how many minor divisions fit between major ticks."""
    major = nice_step(major_px / max(pixels_per_unit, 1e-9))
    lead = round(major / 10 ** math.floor(math.log10(major) + 1e-9))
    for divisions in {1: (10, 5, 2), 2: (4, 2), 5: (5,)}.get(lead, (10, 5, 2)):
        if major / divisions * pixels_per_unit >= minor_px:
            return major, divisions
    return major, 1


def grid_step(pixels_per_unit: float, minimum_px: float = 14) -> float:
    """Grid spacing in units that stays at least ``minimum_px`` apart on screen."""
    return nice_step(minimum_px / max(pixels_per_unit, 1e-9))


def _label(value: float) -> str:
    return f"{0 if abs(value) < 1e-9 else value:g}"


def paint_rulers(view, painter, unit_scene: float, background: QColor, foreground: QColor, marker: QColor) -> None:
    """Paint both rulers. ``unit_scene`` is the size of one ruler unit in scene units."""
    viewport = view.viewport()
    width, height = viewport.width(), viewport.height()
    zoom = max(view.transform().m11(), 1e-6)
    major, divisions = ruler_steps(unit_scene * zoom)
    minor = major / divisions
    origin = view.mapFromScene(QPointF(0, 0))
    scale = unit_scene * zoom  # viewport pixels per unit
    painter.save()
    painter.resetTransform()
    painter.setRenderHint(painter.RenderHint.Antialiasing, False)
    painter.fillRect(0, 0, width, RULER, background)
    painter.fillRect(0, 0, RULER, height, background)
    font = QFont(painter.font())
    font.setPixelSize(9)
    painter.setFont(font)
    edge = QColor(foreground)
    edge.setAlpha(110)
    tick = QPen(foreground, 1)
    for axis, length, offset in (("x", width, origin.x()), ("y", height, origin.y())):
        first = math.floor((RULER - offset) / scale / minor)
        last = math.ceil((length - offset) / scale / minor)
        for i in range(first, min(last, first + 2000) + 1):
            value = i * minor
            pos = round(offset + value * scale)
            if pos < RULER or pos > length:
                continue
            is_major = i % divisions == 0
            half = divisions % 2 == 0 and i % (divisions // 2) == 0
            size = RULER if is_major else 9 if half else 5
            painter.setPen(tick)
            if axis == "x":
                painter.drawLine(pos, RULER - size, pos, RULER - 1)
                if is_major:
                    painter.drawText(pos + 3, 10, _label(value))
            else:
                painter.drawLine(RULER - size, pos, RULER - 1, pos)
                if is_major:
                    text = _label(value)
                    painter.save()
                    # Rotated text runs upward, so start it below the tick by its own length.
                    painter.translate(10, pos + 3 + painter.fontMetrics().horizontalAdvance(text))
                    painter.rotate(-90)
                    painter.drawText(0, 0, text)
                    painter.restore()
    painter.setPen(QPen(edge, 1))
    painter.drawLine(RULER, RULER - 1, width, RULER - 1)
    painter.drawLine(RULER - 1, RULER, RULER - 1, height)
    painter.fillRect(0, 0, RULER, RULER, background)
    if view.underMouse():
        cursor = viewport.mapFromGlobal(QCursor.pos())
        painter.setPen(QPen(marker, 1))
        if cursor.x() >= RULER:
            painter.drawLine(cursor.x(), 0, cursor.x(), RULER - 1)
        if cursor.y() >= RULER:
            painter.drawLine(0, cursor.y(), RULER - 1, cursor.y())
    painter.restore()


def paint_grid(painter, area: QRectF, exposed: QRectF, step: float, zoom: float,
               color: QColor, major_every: int = 5) -> None:
    """Grid lines every ``step`` scene units inside ``area``; every ``major_every``th line is stronger."""
    visible = area.intersected(exposed)
    if visible.isEmpty() or step <= 0:
        return
    minor_pen = QPen(QColor(color.red(), color.green(), color.blue(), max(25, color.alpha() // 2)), 0)
    major_pen = QPen(color, 0)
    painter.save()
    painter.setRenderHint(painter.RenderHint.Antialiasing, False)
    for axis in ("x", "y"):
        lo, hi = (visible.left(), visible.right()) if axis == "x" else (visible.top(), visible.bottom())
        start = math.floor((lo - (area.left() if axis == "x" else area.top())) / step)
        stop = math.ceil((hi - (area.left() if axis == "x" else area.top())) / step)
        for i in range(start, min(stop, start + 4000) + 1):
            v = (area.left() if axis == "x" else area.top()) + i * step
            if not lo <= v <= hi:
                continue
            painter.setPen(major_pen if major_every and i % major_every == 0 else minor_pen)
            if axis == "x":
                painter.drawLine(QPointF(v, visible.top()), QPointF(v, visible.bottom()))
            else:
                painter.drawLine(QPointF(visible.left(), v), QPointF(visible.right(), v))
    painter.restore()

