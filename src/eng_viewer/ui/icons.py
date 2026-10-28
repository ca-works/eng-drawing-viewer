"""Theme-aware SVG icons rendered at the exact device pixel ratio.

Icons are drawn on demand by a ``QIconEngine`` so they stay crisp at 100 %,
125 %, 150 % (Windows) and Retina scales, and recolour instantly when the theme
changes without rebuilding any widgets.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .icon_data import ICONS
from .theme import ThemeManager, qc

_renderers: dict[tuple[str, str, float], QSvgRenderer] = {}
_pixmaps: dict[tuple, QPixmap] = {}


def clear_cache() -> None:
    _renderers.clear()
    _pixmaps.clear()


def _svg(name: str, color: str, stroke: float) -> bytes:
    body = ICONS.get(name) or ICONS["file"]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">'
        f"{body.replace('currentColor', color)}</svg>"
    ).encode()


def _renderer(name: str, color: str, stroke: float) -> QSvgRenderer:
    key = (name, color, stroke)
    r = _renderers.get(key)
    if r is None:
        r = QSvgRenderer(QByteArray(_svg(name, color, stroke)))
        _renderers[key] = r
    return r


def _resolve(role: str) -> QColor:
    if role.startswith("#"):
        return QColor(role)
    theme = ThemeManager.current()
    return qc(getattr(theme, role, theme.text_secondary))


class SvgIconEngine(QIconEngine):
    def __init__(self, name: str, role: str = "text_secondary", on_role: str | None = "accent",
                 active_role: str | None = "text", stroke: float = 1.7):
        super().__init__()
        self.name = name
        self.role = role
        self.on_role = on_role
        self.active_role = active_role
        self.stroke = stroke

    def _color(self, mode: QIcon.Mode, state: QIcon.State) -> QColor:
        role = self.role
        if state == QIcon.State.On and self.on_role:
            role = self.on_role
        elif mode in (QIcon.Mode.Active, QIcon.Mode.Selected) and self.active_role:
            role = self.active_role
        color = _resolve(role)
        if mode == QIcon.Mode.Disabled:
            color.setAlphaF(0.38)
        return color

    def _draw(self, painter: QPainter, rect: QRectF, mode, state):
        color = self._color(mode, state)
        hex_rgb = color.name(QColor.NameFormat.HexRgb)
        renderer = _renderer(self.name, hex_rgb, self.stroke)
        painter.save()
        painter.setOpacity(painter.opacity() * color.alphaF())
        renderer.render(painter, rect)
        painter.restore()

    def paint(self, painter: QPainter, rect: QRect, mode, state):
        self._draw(painter, QRectF(rect), mode, state)

    def scaledPixmap(self, size: QSize, mode, state, scale: float) -> QPixmap:
        color = self._color(mode, state)
        key = (self.name, color.rgba(), size.width(), size.height(), round(scale, 3), self.stroke)
        cached = _pixmaps.get(key)
        if cached is not None:
            return cached
        pm = QPixmap(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw(p, QRectF(0, 0, pm.width(), pm.height()), mode, state)
        p.end()
        pm.setDevicePixelRatio(scale)
        _pixmaps[key] = pm
        return pm

    def pixmap(self, size: QSize, mode, state) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def clone(self):
        return SvgIconEngine(self.name, self.role, self.on_role, self.active_role, self.stroke)


def icon(name: str, role: str = "text_secondary", on_role: str | None = "accent",
         active_role: str | None = "text", stroke: float = 1.7) -> QIcon:
    return QIcon(SvgIconEngine(name, role, on_role, active_role, stroke))


def pixmap(name: str, size: int, color: str | QColor, dpr: float = 2.0, stroke: float = 1.7) -> QPixmap:
    c = QColor(color) if not isinstance(color, QColor) else color
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setOpacity(c.alphaF())
    _renderer(name, c.name(QColor.NameFormat.HexRgb), stroke).render(p, QRectF(0, 0, pm.width(), pm.height()))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def paint_icon(painter: QPainter, name: str, rect: QRectF, color: QColor, stroke: float = 1.7) -> None:
    painter.save()
    painter.setOpacity(painter.opacity() * color.alphaF())
    _renderer(name, color.name(QColor.NameFormat.HexRgb), stroke).render(painter, rect)
    painter.restore()
