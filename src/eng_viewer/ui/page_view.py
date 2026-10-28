"""Resolution-independent PDF page view shared by the viewer and MARK.

Scene units are PDF points. The page is drawn from 512×512 device-pixel tiles
rendered by MuPDF at exactly ``zoom × devicePixelRatio`` and blitted at
device-pixel-aligned positions, so text and hairlines are never produced by
enlarging a bitmap. While a zoom gesture is in progress, tiles from the nearest
cached scale (or a quick whole-page preview) stand in until the exact tiles
arrive a moment later.
"""
from __future__ import annotations

import math
import os
import sys

import shiboken6
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import QColor, QCursor, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QStyle, QStyleOptionGraphicsItem, QWidget

from ..core.render_service import PRI_INTERACTIVE, PRI_PREFETCH, RenderResult, RenderService
from .theme import ThemeManager, qc

TILE = 512
MIN_ZOOM = 0.02
MAX_ZOOM = 64.0
PAGE_MARGIN_PX = 18
IS_MAC = sys.platform == "darwin"


# ---------------------------------------------------------------------------
# Page identity and the shared pixmap cache
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PageRef:
    path: str
    page: int
    width: float  # points, rotation applied
    height: float
    stamp: tuple = ()

    @staticmethod
    def stamp_for(path: str) -> tuple:
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return ()

    @property
    def key(self) -> tuple:
        return (self.path, self.stamp, self.page)


class PixmapCache:
    """Byte-budgeted LRU shared by every view (tiles and page previews)."""

    def __init__(self, budget_bytes: int = 320 * 1024 * 1024):
        self.budget = budget_bytes
        self.used = 0
        self._items: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._scales: dict[tuple, set[float]] = {}  # (page key, thin) -> cached tile scales

    @staticmethod
    def _cost(pm: QPixmap) -> int:
        return pm.width() * pm.height() * 4

    def get(self, key: tuple) -> QPixmap | None:
        pm = self._items.get(key)
        if pm is not None:
            self._items.move_to_end(key)
        return pm

    def put(self, key: tuple, pm: QPixmap) -> None:
        old = self._items.pop(key, None)
        if old is not None:
            self.used -= self._cost(old)
        self._items[key] = pm
        self.used += self._cost(pm)
        if key[0] == "tile":
            self._scales.setdefault((key[1], key[2]), set()).add(key[3])
        while self.used > self.budget and len(self._items) > 1:
            k, evicted = self._items.popitem(last=False)
            self.used -= self._cost(evicted)
            if k[0] == "tile":
                remaining = any(x[0] == "tile" and x[1] == k[1] and x[2] == k[2] and x[3] == k[3] for x in self._items)
                if not remaining:
                    self._scales.get((k[1], k[2]), set()).discard(k[3])

    def scales(self, page_key: tuple, thin: bool) -> set[float]:
        return self._scales.get((page_key, thin), set())


_CACHE: PixmapCache | None = None


def pixmap_cache() -> PixmapCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = PixmapCache()
    return _CACHE


def quantize_scale(s: float) -> float:
    return float(f"{s:.5g}")


def min_line_px(thin_lines: bool) -> float:
    return 1.0 if thin_lines else 0.0


# ---------------------------------------------------------------------------
# Graphics item that paints the PDF page
# ---------------------------------------------------------------------------
class PdfPageItem(QGraphicsItem):
    def __init__(self, view: "PageView"):
        super().__init__()
        self._view = view
        self.ref: PageRef | None = None
        self.placeholder: QPixmap | None = None  # e.g. the file-list thumbnail
        self.setZValue(-1000)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)

    def boundingRect(self) -> QRectF:
        if self.ref is None:
            return QRectF()
        return QRectF(0, 0, self.ref.width, self.ref.height)

    def set_ref(self, ref: PageRef | None):
        self.prepareGeometryChange()
        self.ref = ref
        self.update()

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None):
        ref = self.ref
        if ref is None:
            return
        page_rect = QRectF(0, 0, ref.width, ref.height)
        exposed = option.exposedRect.intersected(page_rect)
        if exposed.isEmpty():
            return
        painter.fillRect(exposed, QColor("#FFFFFF"))
        world = painter.worldTransform()
        dpr = widget.devicePixelRatioF() if widget is not None else 1.0
        s_dev = abs(world.m11()) * dpr
        if s_dev <= 0:
            return
        cache = pixmap_cache()
        thin = self._view.thin_lines
        s_q = quantize_scale(s_dev)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        # 1) Lowest layer: placeholder thumbnail and page preview, scaled.
        preview = cache.get(("preview", ref.key, thin))
        drew_base = False
        if preview is not None:
            painter.drawPixmap(page_rect, preview, QRectF(preview.rect()))
            drew_base = True
        elif self.placeholder is not None and not self.placeholder.isNull():
            painter.drawPixmap(page_rect, self.placeholder, QRectF(self.placeholder.rect()))
            drew_base = True
        # 2) Stand-in tiles from the nearest other cached scale (during zoom).
        exact_cover = self._exact_tiles_cover(exposed, s_q, ref, thin, cache)
        if not exact_cover:
            others = [x for x in cache.scales(ref.key, thin) if x != s_q]
            if others:
                best = min(others, key=lambda x: (abs(math.log(x / s_q)) - (0.01 if x > s_q else 0)))
                self._draw_tiles_scaled(painter, exposed, best, ref, thin, cache)
        painter.restore()

        # 3) Exact tiles, blitted 1:1 at device-pixel-aligned positions.
        painter.save()
        origin = world.map(QPointF(0, 0))
        ox = round(origin.x() * dpr)
        oy = round(origin.y() * dpr)
        painter.resetTransform()
        x0, y0, x1, y1 = self._tile_range(exposed, s_q)
        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                pm = cache.get(("tile", ref.key, thin, s_q, tx, ty))
                if pm is None:
                    continue
                gx, gy = pm.property_origin
                # Target rectangle expressed in device pixels / dpr: exactly one
                # tile pixel per screen pixel, whatever the pixmap's own ratio.
                target = QRectF((ox + gx) / dpr, (oy + gy) / dpr, pm.width() / dpr, pm.height() / dpr)
                painter.drawPixmap(target, pm, QRectF(pm.rect()))
        painter.restore()
        if not drew_base and not exact_cover:
            self._view.note_missing()

    @staticmethod
    def _tile_range(rect: QRectF, s: float) -> tuple[int, int, int, int]:
        x0 = max(0, int(math.floor(rect.left() * s / TILE)))
        y0 = max(0, int(math.floor(rect.top() * s / TILE)))
        x1 = max(x0, int(math.floor((rect.right() * s - 1e-6) / TILE)))
        y1 = max(y0, int(math.floor((rect.bottom() * s - 1e-6) / TILE)))
        return x0, y0, x1, y1

    def _exact_tiles_cover(self, rect, s, ref, thin, cache) -> bool:
        x0, y0, x1, y1 = self._tile_range(rect, s)
        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                if cache.get(("tile", ref.key, thin, s, tx, ty)) is None:
                    return False
        return True

    def _draw_tiles_scaled(self, painter, rect, s, ref, thin, cache):
        x0, y0, x1, y1 = self._tile_range(rect, s)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 400:
            return
        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                pm = cache.get(("tile", ref.key, thin, s, tx, ty))
                if pm is None:
                    continue
                gx, gy = pm.property_origin
                target = QRectF(gx / s, gy / s, pm.width() / s, pm.height() / s)
                painter.drawPixmap(target, pm, QRectF(pm.rect()))


class _TilePixmap(QPixmap):
    """QPixmap that remembers where its tile starts, in device pixels."""

    def __init__(self, pm: QPixmap, origin: tuple[int, int]):
        super().__init__(pm)
        self.property_origin = origin


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------
class PageView(QGraphicsView):
    zoomChanged = Signal(float)  # zoom (1.0 == 100 %)
    fitModeChanged = Signal(object)  # "width" | "page" | None
    pageReady = Signal()
    renderError = Signal(str)

    # 100 % shows a page at physical size on a typical 96-DPI display, matching
    # the original app (96/72 logical pixels per point).
    ACTUAL_SIZE = 96.0 / 72.0

    def __init__(self, render: RenderService, parent: QWidget | None = None):
        super().__init__(parent)
        self.render = render
        self.thin_lines = True
        self.left_drag_pans = True
        self._zoom = 1.0
        self._fit_mode: str | None = "width"
        self._ref: PageRef | None = None
        self._pending: dict[tuple, int] = {}
        self._req_scale: float | None = None
        self._panning = False
        self._pan_last = QPoint()
        self._space_down = False
        self._gesture_active = False
        self._missing_noted = False
        self._smooth_target: QPointF | None = None

        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)
        self.page_item = PdfPageItem(self)
        self.scene_obj.addItem(self.page_item)

        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.TextAntialiasing)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, False)
        if not IS_MAC:
            self.viewport().grabGesture(Qt.GestureType.PinchGesture)

        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.timeout.connect(self._request_tiles)
        self._zoom_anim = QVariantAnimation(self)
        self._zoom_anim.setDuration(160)
        self._zoom_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._zoom_anim.valueChanged.connect(self._on_zoom_anim)
        self._zoom_anim.finished.connect(lambda: self._schedule_tiles(settle=True))
        self._anim_anchor = QPointF()
        self._scroll_anim = QVariantAnimation(self)
        self._scroll_anim.setDuration(140)
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.valueChanged.connect(self._on_scroll_anim)
        self.horizontalScrollBar().valueChanged.connect(lambda _v: self._schedule_tiles())
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._schedule_tiles())
        if ThemeManager._instance is not None:
            # A bound method (not a lambda) so Qt disconnects it when the view is destroyed.
            ThemeManager.instance().changed.connect(self._on_theme_changed)
        self._update_cursor()

    def _on_theme_changed(self, _theme):
        self.viewport().update()

    # ------------------------------------------------------------ properties
    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def fit_mode(self) -> str | None:
        return self._fit_mode

    @property
    def page_ref(self) -> PageRef | None:
        return self._ref

    def zoom_percent(self) -> float:
        return self._zoom / self.ACTUAL_SIZE * 100.0

    # ------------------------------------------------------------- page setup
    def set_page(self, ref: PageRef | None, placeholder: QPixmap | None = None, keep_view: bool = False):
        old = self._ref
        if old is not None and (ref is None or old.key != ref.key):
            self.render.cancel_group(self._group(old))
            self._pending.clear()
        self._ref = ref
        self.page_item.placeholder = placeholder
        self.page_item.set_ref(ref)
        self._update_scene_rect()
        if ref is None:
            self.viewport().update()
            return
        if self._fit_mode is not None:
            self._apply_fit(self._fit_mode, animate=False)
        elif not keep_view:
            self._center_on_page_top()
        self._request_preview(ref, PRI_INTERACTIVE)
        self._schedule_tiles(settle=True, immediate=True)

    def refresh(self):
        """Drop pending requests and re-request (e.g. thin-line setting changed)."""
        if self._ref is not None:
            self.render.cancel_group(self._group(self._ref))
            self._pending.clear()
            self._request_preview(self._ref, PRI_INTERACTIVE)
            self._schedule_tiles(settle=True, immediate=True)
        self.viewport().update()

    def prefetch(self, ref: PageRef):
        """Warm the preview cache for a page the user is likely to open next."""
        self._request_preview(ref, PRI_PREFETCH)

    def _preview_scale(self, ref: PageRef) -> float:
        dpr = self.devicePixelRatioF()
        long_edge = max(ref.width, ref.height, 1.0)
        return min(1100.0 * dpr / 2.0, 2048.0) / long_edge

    def _request_preview(self, ref: PageRef, priority: int):
        key = ("preview", ref.key, self.thin_lines)
        if pixmap_cache().get(key) is not None:
            return
        scale = self._preview_scale(ref)
        args = {"path": ref.path, "page": ref.page, "scale": scale, "min_line": min_line_px(self.thin_lines)}
        thin = self.thin_lines

        def done(res: RenderResult, key=key, ref=ref):
            if getattr(self, "_dead", False) or not shiboken6.isValid(self):
                return
            if not res.ok or res.image is None:
                if self._ref is not None and ref.key == self._ref.key:
                    self.renderError.emit(res.error)
                return
            pixmap_cache().put(key, QPixmap.fromImage(res.image))
            if self._ref is not None and self._ref.key == ref.key and self.thin_lines == thin:
                self.viewport().update()
                self.pageReady.emit()

        self.render.submit("render", args, done, priority=priority, group=self._group(ref))

    # ---------------------------------------------------------------- tiles
    def _group(self, ref: PageRef) -> tuple:
        """Render-job group for this view's interest in ``ref``."""
        return ("page", ref.key, id(self))

    def release(self):
        """Withdraw all outstanding requests (call before the view goes away)."""
        self._dead = True
        if self._ref is not None:
            self.render.cancel_group(self._group(self._ref))
        self._pending.clear()

    def note_missing(self):
        if not self._missing_noted:
            self._missing_noted = True
            self._schedule_tiles()

    def _schedule_tiles(self, settle: bool = False, immediate: bool = False):
        if self._ref is None:
            return
        if immediate:
            self._tile_timer.start(0)
        elif settle or self._gesture_active or self._zoom_anim.state() == QVariantAnimation.State.Running:
            self._tile_timer.start(110)
        elif not self._tile_timer.isActive():
            self._tile_timer.start(16)

    def _visible_scene_rect(self) -> QRectF:
        return self.viewportTransform().inverted()[0].mapRect(QRectF(self.viewport().rect()))

    def _request_tiles(self):
        self._missing_noted = False
        ref = self._ref
        if ref is None or self._zoom_anim.state() == QVariantAnimation.State.Running or self._gesture_active:
            return
        dpr = self.devicePixelRatioF()
        s = quantize_scale(self._zoom * dpr)
        cache = pixmap_cache()
        thin = self.thin_lines
        group = self._group(ref)
        if self._req_scale is not None and self._req_scale != s:
            # Abandon queued tiles for the previous zoom level.
            for k, job in list(self._pending.items()):
                if k[3] != s:
                    self.render.cancel(job, group)
                    del self._pending[k]
        self._req_scale = s
        page_rect = QRectF(0, 0, ref.width, ref.height)
        visible = self._visible_scene_rect()
        # Request a ring of one tile around the viewport so small pans stay sharp.
        margin = TILE / s
        wanted_rect = visible.adjusted(-margin, -margin, margin, margin).intersected(page_rect)
        if wanted_rect.isEmpty():
            return
        x0, y0, x1, y1 = PdfPageItem._tile_range(wanted_rect, s)
        # If the whole page at this scale is about the size of the preview, the
        # preview already is the right resolution: render it as tiles anyway
        # (in parallel) since that is what makes it pixel-exact.
        center = visible.center() * s
        order = []
        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                key = ("tile", ref.key, thin, s, tx, ty)
                if cache.get(key) is not None or key in self._pending:
                    continue
                cx, cy = (tx + 0.5) * TILE, (ty + 0.5) * TILE
                in_view = visible.intersects(QRectF(tx * TILE / s, ty * TILE / s, TILE / s, TILE / s))
                order.append((0 if in_view else 1, (cx - center.x()) ** 2 + (cy - center.y()) ** 2, tx, ty, key))
        order.sort()
        for vis, _d, tx, ty, key in order:
            clip = (
                tx * TILE / s,
                ty * TILE / s,
                min((tx + 1) * TILE / s, ref.width),
                min((ty + 1) * TILE / s, ref.height),
            )
            args = {"path": ref.path, "page": ref.page, "scale": s, "clip": clip, "min_line": min_line_px(thin)}
            self._pending[key] = self.render.submit(
                "render", args, lambda res, key=key: self._on_tile(key, res),
                priority=PRI_INTERACTIVE if vis == 0 else PRI_INTERACTIVE + 1, group=group,
            )

    def _on_tile(self, key: tuple, res: RenderResult):
        if getattr(self, "_dead", False) or not shiboken6.isValid(self):
            return
        self._pending.pop(key, None)
        if not res.ok or res.image is None:
            if self._ref is not None and key[1] == self._ref.key and res.error:
                self.renderError.emit(res.error)
            return
        pm = _TilePixmap(QPixmap.fromImage(res.image), (res.header.get("x0", key[4] * TILE), res.header.get("y0", key[5] * TILE)))
        pixmap_cache().put(key, pm)
        if self._ref is not None and key[1] == self._ref.key:
            s = key[3]
            r = QRectF(pm.property_origin[0] / s, pm.property_origin[1] / s, pm.width() / s, pm.height() / s)
            self.page_item.update(r)
            if not self._pending:
                self.pageReady.emit()

    # ----------------------------------------------------------------- zoom
    def _update_scene_rect(self):
        if self._ref is None:
            self.scene_obj.setSceneRect(QRectF(0, 0, 1, 1))
            return
        m = PAGE_MARGIN_PX / max(self._zoom, 1e-6)
        self.scene_obj.setSceneRect(QRectF(-m, -m, self._ref.width + 2 * m, self._ref.height + 2 * m))

    def _set_zoom_raw(self, z: float, anchor: QPointF):
        z = min(max(z, MIN_ZOOM), MAX_ZOOM)
        inv, _ok = self.viewportTransform().inverted()
        scene_pt = inv.map(anchor)
        self._zoom = z
        self.setTransform(QTransform.fromScale(z, z))
        self._update_scene_rect()
        new_vp = self.viewportTransform().map(scene_pt)
        d = new_vp - anchor
        h, v = self.horizontalScrollBar(), self.verticalScrollBar()
        h.setValue(h.value() + round(d.x()))
        v.setValue(v.value() + round(d.y()))
        self.zoomChanged.emit(self.zoom_percent())

    def set_zoom(self, z: float, anchor: QPointF | None = None, animate: bool = False):
        if self._ref is None:
            return
        self._set_fit_mode(None)
        if anchor is None:
            anchor = QPointF(self.viewport().width() / 2.0, self.viewport().height() / 2.0)
        if animate:
            self._anim_anchor = anchor
            self._zoom_anim.stop()
            self._zoom_anim.setStartValue(math.log(self._zoom))
            self._zoom_anim.setEndValue(math.log(min(max(z, MIN_ZOOM), MAX_ZOOM)))
            self._zoom_anim.start()
        else:
            self._set_zoom_raw(z, anchor)
            self._schedule_tiles(settle=True)

    def _on_zoom_anim(self, value):
        self._set_zoom_raw(math.exp(float(value)), self._anim_anchor)

    def set_zoom_percent(self, pct: float, animate: bool = True):
        self.set_zoom(pct / 100.0 * self.ACTUAL_SIZE, animate=animate)

    def zoom_by(self, factor: float, anchor: QPointF | None = None, animate: bool = False):
        target = self._zoom_anim.endValue() if self._zoom_anim.state() == QVariantAnimation.State.Running else None
        base = math.exp(float(target)) if target is not None else self._zoom
        self.set_zoom(base * factor, anchor, animate)

    def zoom_in(self):
        self.zoom_by(1.25, animate=True)

    def zoom_out(self):
        self.zoom_by(1 / 1.25, animate=True)

    def actual_size(self):
        self.set_zoom(self.ACTUAL_SIZE, animate=True)

    def fit_width(self):
        self._apply_fit("width", animate=True)

    def fit_page(self):
        self._apply_fit("page", animate=True)

    def _set_fit_mode(self, mode):
        if mode != self._fit_mode:
            self._fit_mode = mode
            self.fitModeChanged.emit(mode)

    def _fit_zoom(self, mode: str) -> float:
        """Zoom for a fit mode, independent of whether scroll bars are showing.

        Using the current viewport size would oscillate on platforms with
        non-overlay scroll bars (Windows): fitting the width makes a vertical
        bar appear, which narrows the viewport, which refits, and so on.
        """
        ref = self._ref
        full = self.maximumViewportSize()
        vw = max(1.0, full.width() - 2 * PAGE_MARGIN_PX)
        vh = max(1.0, full.height() - 2 * PAGE_MARGIN_PX)
        pw, ph = max(ref.width, 1e-3), max(ref.height, 1e-3)
        if mode == "width":
            z = vw / pw
            transient = self.style().styleHint(QStyle.StyleHint.SH_ScrollBar_Transient, None, self)
            if not transient and ph * z > vh:
                bar = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, None, self)
                z = max(1.0, vw - bar) / pw
            return z
        return min(vw / pw, vh / ph)

    def _apply_fit(self, mode: str, animate: bool = False):
        if self._ref is None:
            self._set_fit_mode(mode)
            return
        z = self._fit_zoom(mode)
        self._zoom_anim.stop()
        self._zoom = min(max(z, MIN_ZOOM), MAX_ZOOM)
        self.setTransform(QTransform.fromScale(self._zoom, self._zoom))
        self._update_scene_rect()
        self._set_fit_mode(mode)
        if mode == "width":
            self.horizontalScrollBar().setValue((self.horizontalScrollBar().minimum() + self.horizontalScrollBar().maximum()) // 2)
            self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
        else:
            self.centerOn(QPointF(self._ref.width / 2, self._ref.height / 2))
        self.zoomChanged.emit(self.zoom_percent())
        self._schedule_tiles(settle=True)

    def _center_on_page_top(self):
        if self._ref is None:
            return
        self.horizontalScrollBar().setValue((self.horizontalScrollBar().minimum() + self.horizontalScrollBar().maximum()) // 2)
        self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())

    def toggle_smart_zoom(self, anchor: QPointF):
        if self._fit_mode == "page" or self._zoom <= self._fit_zoom("page") * 1.05:
            self.set_zoom(self._fit_zoom("page") * 2.5, anchor, animate=True)
        else:
            self.fit_page()

    # --------------------------------------------------------------- events
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._ref is None:
            return
        if self._fit_mode is not None and abs(self._fit_zoom(self._fit_mode) / self._zoom - 1.0) > 1e-4:
            self._apply_fit(self._fit_mode, animate=False)
        else:
            self._schedule_tiles(settle=True)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self._schedule_tiles(settle=True, immediate=True)

    def drawBackground(self, painter: QPainter, rect: QRectF):
        theme = ThemeManager.current()
        painter.fillRect(rect, qc(theme.canvas))
        if self._ref is None:
            return
        # Soft page shadow drawn in screen pixels, independent of zoom.
        painter.save()
        page = self.mapFromScene(QRectF(0, 0, self._ref.width, self._ref.height)).boundingRect()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        shadow = qc(theme.page_shadow)
        base_alpha = shadow.alphaF()
        for i, spread in enumerate((1, 3, 6, 10)):
            c = QColor(shadow)
            c.setAlphaF(base_alpha * (0.32, 0.18, 0.09, 0.04)[i])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawRoundedRect(QRectF(page).adjusted(-spread, -spread + 2, spread, spread + 2), spread + 1, spread + 1)
        painter.restore()

    def drawForeground(self, painter: QPainter, rect: QRectF):
        if self._ref is None:
            return
        painter.save()
        pen = QPen(QColor(ThemeManager.current().page_border))
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(0, 0, self._ref.width, self._ref.height))
        painter.restore()

    def wheelEvent(self, event):
        if self._ref is None:
            return
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            pd = event.pixelDelta()
            if not pd.isNull() and IS_MAC:
                factor = 2.0 ** (pd.y() / 260.0)
            else:
                factor = 2.0 ** (event.angleDelta().y() / 120.0 / 3.5)
            self._zoom_anim.stop()
            self._set_fit_mode(None)
            self._set_zoom_raw(self._zoom * factor, event.position())
            self._schedule_tiles(settle=True)
            event.accept()
            return
        pd = event.pixelDelta()
        ad = event.angleDelta()
        if pd.isNull() and not IS_MAC and not ad.isNull():
            # Discrete mouse wheel: animate the scroll so it glides instead of jumping.
            dx, dy = ad.x(), ad.y()
            if mods & Qt.KeyboardModifier.ShiftModifier and dx == 0:
                dx, dy = dy, 0
            step = 1.0  # pixels per angle unit (120 units = one notch = 120 px)
            h, v = self.horizontalScrollBar(), self.verticalScrollBar()
            base = self._smooth_target if (self._smooth_target is not None and self._scroll_anim.state() == QVariantAnimation.State.Running) else QPointF(h.value(), v.value())
            target = QPointF(
                min(max(base.x() - dx * step, h.minimum()), h.maximum()),
                min(max(base.y() - dy * step, v.minimum()), v.maximum()),
            )
            self._smooth_target = target
            self._scroll_anim.stop()
            self._scroll_anim.setStartValue(QPointF(h.value(), v.value()))
            self._scroll_anim.setEndValue(target)
            self._scroll_anim.start()
            event.accept()
            return
        super().wheelEvent(event)

    def _on_scroll_anim(self, value):
        self.horizontalScrollBar().setValue(round(value.x()))
        self.verticalScrollBar().setValue(round(value.y()))

    def viewportEvent(self, event):
        et = event.type()
        if et == QEvent.Type.NativeGesture:
            gt = event.gestureType()
            if gt == Qt.NativeGestureType.BeginNativeGesture:
                self._gesture_active = True
            elif gt == Qt.NativeGestureType.EndNativeGesture:
                self._gesture_active = False
                self._schedule_tiles(settle=True)
            elif gt == Qt.NativeGestureType.ZoomNativeGesture and self._ref is not None:
                self._set_fit_mode(None)
                self._set_zoom_raw(self._zoom * (1.0 + event.value()), event.position())
                self._schedule_tiles(settle=True)
            elif gt == Qt.NativeGestureType.SmartZoomNativeGesture and self._ref is not None:
                self.toggle_smart_zoom(event.position())
            return True
        if et == QEvent.Type.Gesture:
            pinch = event.gesture(Qt.GestureType.PinchGesture)
            if pinch is not None and self._ref is not None:
                self._gesture_active = pinch.state() not in (Qt.GestureState.GestureFinished, Qt.GestureState.GestureCanceled)
                anchor = self.viewport().mapFromGlobal(pinch.centerPoint().toPoint())
                self._set_fit_mode(None)
                self._set_zoom_raw(self._zoom * pinch.scaleFactor(), QPointF(anchor))
                self._schedule_tiles(settle=True)
                return True
        return super().viewportEvent(event)

    def _start_pan(self, pos: QPoint):
        self._panning = True
        self._pan_last = pos
        self._scroll_anim.stop()
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)

    def mousePressEvent(self, event):
        btn = event.button()
        if btn == Qt.MouseButton.MiddleButton or (
            btn == Qt.MouseButton.LeftButton and (self._space_down or self.left_drag_pans)
        ):
            self._start_pan(event.position().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            now = event.position().toPoint()
            d = now - self._pan_last
            self._pan_last = now
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._panning and event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            self._panning = False
            self._update_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.left_drag_pans and event.button() == Qt.MouseButton.LeftButton and self._ref is not None:
            self.toggle_smart_zoom(event.position())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = True
            self._update_cursor()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = False
            self._update_cursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._space_down = False
        self._update_cursor()
        super().focusOutEvent(event)

    def _update_cursor(self):
        if self._panning:
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._space_down or self.left_drag_pans:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.viewport().setCursor(self.tool_cursor())

    def tool_cursor(self) -> QCursor:
        return QCursor(Qt.CursorShape.ArrowCursor)
