"""High-resolution whole-page renders split into bands across all workers."""
from __future__ import annotations

import math

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtGui import QColor, QImage, QPainter

from ..core.exporting import dots_per_meter, export_scale, reinforced_min_line
from ..core.render_service import PRI_EXPORT, RenderResult, RenderService


class FullPageRender(QObject):
    """Render a page at export DPI. Emits ``finished(QImage)`` or ``failed(str)``."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, service: RenderService, path: str, page: int, page_w: float, page_h: float,
                 dpi: float, reinforce: bool = True, priority: int = PRI_EXPORT, parent=None, native: bool = False):
        super().__init__(parent)
        self.native = native  # image documents render at their own pixel size
        self.service = service
        self.scale = export_scale(dpi, page_w, page_h)
        self.path, self.page = path, page
        self.page_w, self.page_h = page_w, page_h
        self.reinforce = reinforce
        self.priority = priority
        self._parts: list[tuple[int, int, QImage]] = []
        self._expected = 0
        self._failed = False
        self._jobs: list[int] = []
        self.group = ("export", id(self))

    @property
    def effective_dpi(self) -> float:
        return self.scale * 72.0

    def start(self):
        if self.native:
            # One band: the worker picks the image's own scale (images are never banded).
            self._expected = 1
            args = {"path": self.path, "page": self.page, "scale": self.scale, "native_image": True,
                    "max_pixels": 200_000_000}
            self._jobs.append(self.service.submit("render", args, self._on_part, priority=self.priority,
                                                  group=self.group, image_format="rgb888", dedupe=False))
            return
        s = self.scale
        total_h = math.ceil(self.page_h * s)
        total_px = math.ceil(self.page_w * s) * total_h
        bands = max(1, min(8, math.ceil(total_px / 5_000_000)))
        band_h = math.ceil(total_h / bands)
        min_line = reinforced_min_line(s) if self.reinforce else 0.0
        self._expected = bands
        for k in range(bands):
            y0 = k * band_h / s
            y1 = min((k + 1) * band_h / s, self.page_h)
            if y1 <= y0:
                self._expected -= 1
                continue
            args = {"path": self.path, "page": self.page, "scale": s, "clip": (0.0, y0, self.page_w, y1),
                    "min_line": min_line, "max_pixels": 200_000_000}
            self._jobs.append(self.service.submit("render", args, self._on_part, priority=self.priority,
                                                  group=self.group, image_format="rgb888", dedupe=False))

    def cancel(self):
        self.service.cancel_group(self.group)
        self._failed = True

    def _on_part(self, res: RenderResult):
        if self._failed:
            return
        if not res.ok or res.image is None:
            self._failed = True
            self.service.cancel_group(self.group)
            self.failed.emit(res.error or "Rendering failed.")
            return
        if self.native and res.header.get("scale"):
            self.scale = float(res.header["scale"])
        self._parts.append((res.header.get("x0", 0), res.header.get("y0", 0), res.image))
        if len(self._parts) == self._expected:
            self._assemble()

    def _assemble(self):
        w = max(x + img.width() for x, y, img in self._parts)
        h = max(y + img.height() for x, y, img in self._parts)
        if len(self._parts) == 1:
            out = self._parts[0][2]
        else:
            out = QImage(w, h, QImage.Format.Format_RGB888)
            out.fill(QColor("#FFFFFF"))
            p = QPainter(out)
            for x, y, img in sorted(self._parts, key=lambda t: t[1]):
                p.drawImage(QPoint(x, y), img)
            p.end()
        dpm = dots_per_meter(self.scale)
        out.setDotsPerMeterX(dpm)
        out.setDotsPerMeterY(dpm)
        self._parts.clear()
        self.finished.emit(out)
