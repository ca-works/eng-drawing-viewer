"""Compare two drawing revisions as a colour overlay, plus text changes."""
from __future__ import annotations

import shutil
import tempfile
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QListWidget, QListWidgetItem, QProgressBar, QSpinBox, QVBoxLayout, QWidget,
)

from ...core.documents import IMAGE_EXTS
from ...core.exporting import dots_per_meter
from ...core.tool_support import unique_path
from ...core.render_service import PRI_EXPORT, RenderResult
from ..page_view import PageRef, PageView
from ..widgets import SegmentedControl, label, primary_button, secondary_button
from .common import Card, file_filter, local_paths

ACCEPTS = frozenset({".pdf"}) | IMAGE_EXTS
RED = QColor(226, 32, 32)
BLUE = QColor(20, 96, 255)
ORANGE = QColor(255, 140, 0)


def compose(a: QImage, b: QImage, dpi: float, names: tuple[str, str]) -> tuple[QImage, list[QRect]]:
    """Overlay: lines only in A → red, only in B → blue, in both → black; boxes around changes."""
    a = a.convertToFormat(QImage.Format.Format_RGB32)
    b = b.convertToFormat(QImage.Format.Format_RGB32)
    if b.size() != a.size():
        b = b.scaled(a.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)

    def tint(img: QImage, color: QColor) -> QImage:
        out = img.copy()
        p = QPainter(out)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Lighten)  # dark → colour, white stays white
        p.fillRect(out.rect(), color)
        p.end()
        return out

    result = tint(a, RED)
    p = QPainter(result)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
    p.drawImage(0, 0, tint(b, BLUE))
    p.end()

    diff = a.copy()
    p = QPainter(diff)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Difference)
    p.drawImage(0, 0, b)
    p.end()
    k = max(4, max(a.width(), a.height()) // 700)
    gw, gh = max(1, a.width() // k), max(1, a.height() // k)
    small = diff.scaled(gw, gh, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    small = small.convertToFormat(QImage.Format.Format_Grayscale8)
    bpl = small.bytesPerLine()
    data = bytes(small.constBits())
    seen = bytearray(gw * gh)
    boxes: list[list[int]] = []
    for y in range(gh):
        row = y * bpl
        for x in range(gw):
            if data[row + x] < 6 or seen[y * gw + x]:
                continue
            x0 = x1 = x
            y0 = y1 = y
            q = deque([(x, y)])
            seen[y * gw + x] = 1
            while q:
                cx, cy = q.popleft()
                x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
                for nx in (cx - 2, cx - 1, cx, cx + 1, cx + 2):
                    for ny in (cy - 2, cy - 1, cy, cy + 1, cy + 2):
                        if 0 <= nx < gw and 0 <= ny < gh and not seen[ny * gw + nx] and data[ny * bpl + nx] >= 6:
                            seen[ny * gw + nx] = 1
                            q.append((nx, ny))
            boxes.append([x0, y0, x1, y1])
    # Merge boxes that are close together.
    merged = True
    while merged:
        merged = False
        out: list[list[int]] = []
        for bx in boxes:
            for ob in out:
                if bx[0] <= ob[2] + 4 and bx[2] >= ob[0] - 4 and bx[1] <= ob[3] + 4 and bx[3] >= ob[1] - 4:
                    ob[0], ob[1], ob[2], ob[3] = min(ob[0], bx[0]), min(ob[1], bx[1]), max(ob[2], bx[2]), max(ob[3], bx[3])
                    merged = True
                    break
            else:
                out.append(list(bx))
        boxes = out
    pad = 3
    rects = [QRect((x0 - pad) * k, (y0 - pad) * k, (x1 - x0 + 1 + 2 * pad) * k, (y1 - y0 + 1 + 2 * pad) * k).intersected(result.rect())
             for x0, y0, x1, y1 in boxes]
    p = QPainter(result)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(ORANGE, max(2.0, dpi / 50))
    p.setPen(pen)
    for r in rects:
        p.drawRoundedRect(QRectF(r), dpi / 20, dpi / 20)
    # Legend
    f = QFont()
    f.setPixelSize(max(12, int(dpi / 9)))
    p.setFont(f)
    lines = [(RED, f"Only in A (older): {names[0]}"), (BLUE, f"Only in B (newer): {names[1]}"),
             (QColor("black"), "In both: unchanged"), (ORANGE, f"{len(rects)} changed area{'s' if len(rects) != 1 else ''}")]
    lh = f.pixelSize() * 1.5
    box = QRectF(lh, result.height() - lh * (len(lines) + 1), max(p.fontMetrics().horizontalAdvance(t) for _c, t in lines) + lh * 2.2, lh * (len(lines) + 0.6))
    p.setPen(QPen(QColor(0, 0, 0, 60), 1))
    p.setBrush(QColor(255, 255, 255, 235))
    p.drawRoundedRect(box, lh / 3, lh / 3)
    for i, (c, t) in enumerate(lines):
        y = box.top() + lh * (i + 0.8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRect(QRectF(box.left() + lh * 0.4, y - lh * 0.35, lh * 0.5, lh * 0.5))
        p.setPen(QColor("#1C1C21"))
        p.drawText(QRectF(box.left() + lh * 1.2, y - lh * 0.6, box.width(), lh), Qt.AlignmentFlag.AlignVCenter, t)
    p.end()
    dpm = dots_per_meter(dpi / 72.0)
    result.setDotsPerMeterX(dpm)
    result.setDotsPerMeterY(dpm)
    return result, rects


class _Bridge(QObject):
    done = Signal(object, object, str)  # image, rects, png path


class _ComposeJob(QRunnable):
    def __init__(self, a, b, dpi, names, png_path, bridge):
        super().__init__()
        self.a, self.b, self.dpi, self.names, self.png, self.bridge = a, b, dpi, names, png_path, bridge

    def run(self):
        img, rects = compose(self.a, self.b, self.dpi, self.names)
        img.save(self.png, "PNG")
        self.bridge.done.emit(img, rects, self.png)


class RevisionSlot(Card):
    changed = Signal()

    def __init__(self, title: str, host, parent=None):
        super().__init__(title, parent)
        self.host = host
        self.path: str | None = None
        self.setAcceptDrops(True)
        self.name = label("Drop a PDF or image here", "secondary")
        self.lay.addWidget(self.name)
        row = QHBoxLayout()
        choose = secondary_button("Choose…")
        choose.clicked.connect(self._choose)
        row.addWidget(choose)
        cur = secondary_button("Current Drawing")
        cur.clicked.connect(lambda: host.current_document() and self.set_path(host.current_document()))
        row.addWidget(cur)
        row.addStretch(1)
        row.addWidget(label("Page", "secondary"))
        self.page = QSpinBox()
        self.page.setRange(1, 1)
        row.addWidget(self.page)
        self.lay.addLayout(row)

    def set_path(self, path: str):
        self.path = path
        self.name.setText(Path(path).name)
        self.name.setToolTip(path)
        self.host.document_info(path, lambda info: self.page.setRange(1, max(1, info.get("count", 1))))
        self.changed.emit()

    def _choose(self):
        path, _ = QFileDialog.getOpenFileName(self.window(), "Choose a revision", self.host.default_folder(), file_filter(ACCEPTS))
        if path:
            self.set_path(path)

    def dragEnterEvent(self, e):
        if local_paths(e.mimeData(), ACCEPTS):
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = local_paths(e.mimeData(), ACCEPTS)
        if paths:
            self.set_path(paths[0])
            e.acceptProposedAction()


class ComparePanel(QWidget):
    outputsCreated = Signal(list)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self._tempdir = tempfile.TemporaryDirectory(prefix="eng-compare-")
        self._tmp = Path(self._tempdir.name)
        self._images: dict[str, QImage] = {}
        self._png: str | None = None
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._composed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        slots = QHBoxLayout()
        self.a = RevisionSlot("A — older revision", host)
        self.b = RevisionSlot("B — newer revision", host)
        for s in (self.a, self.b):
            s.changed.connect(self._update)
            slots.addWidget(s, 1)
        lay.addLayout(slots)
        row = QHBoxLayout()
        row.addWidget(label("Detail", "secondary"))
        self.dpi = SegmentedControl([("100", "Fast", None, "100 DPI"), ("150", "Normal", None, "150 DPI"),
                                     ("220", "Fine", None, "220 DPI"), ("300", "Finest", None, "300 DPI")])
        self.dpi.set_current("150")
        row.addWidget(self.dpi)
        row.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedSize(140, 6)
        self.progress.setTextVisible(False)
        self.progress.hide()
        row.addWidget(self.progress)
        self.run_btn = primary_button("Compare", "compare")
        self.run_btn.clicked.connect(self.run)
        row.addWidget(self.run_btn)
        lay.addLayout(row)

        self.result = Card("Result")
        self.stats = label("", "secondary", wrap=True)
        self.result.lay.addWidget(self.stats)
        body = QHBoxLayout()
        self.view = PageView(host.render)
        self.view.setMinimumHeight(420)
        body.addWidget(self.view, 3)
        side = QVBoxLayout()
        side.addWidget(label("TEXT CHANGES", "section"))
        self.changes = QListWidget()
        self.changes.setObjectName("Queue")
        side.addWidget(self.changes, 1)
        body.addLayout(side, 1)
        self.result.lay.addLayout(body, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.open_btn = secondary_button("Open in Viewer")
        self.open_btn.clicked.connect(lambda: self._png and host.open_in_viewer(self._png))
        btns.addWidget(self.open_btn)
        self.save_btn = primary_button("Save Comparison as PDF…", "save")
        self.save_btn.clicked.connect(self.save)
        btns.addWidget(self.save_btn)
        self.result.lay.addLayout(btns)
        self.result.hide()
        lay.addWidget(self.result, 1)
        self._update()

    def _update(self):
        self.run_btn.setEnabled(bool(self.a.path and self.b.path) and not self.progress.isVisible())

    def run(self):
        if not (self.a.path and self.b.path):
            return
        dpi = int(self.dpi.current_key())
        self._dpi = dpi
        self._images = {}
        self.progress.show()
        self._update()
        for key, slot in (("a", self.a), ("b", self.b)):
            args = {"path": slot.path, "page": slot.page.value() - 1, "scale": dpi / 72.0, "min_line": 1.0,
                    "max_pixels": 60_000_000}
            self.host.render.submit("render", args, lambda r, k=key: self._rendered(k, r), priority=PRI_EXPORT,
                                    image_format="rgb888", dedupe=False)
        self.host.render.submit("tool", {"op": "text_changes", "path_a": self.a.path, "path_b": self.b.path,
                                         "page_a": self.a.page.value() - 1, "page_b": self.b.page.value() - 1},
                                self._text_done, priority=PRI_EXPORT, dedupe=False)

    def _rendered(self, key: str, res: RenderResult):
        if not res.ok or res.image is None:
            self.progress.hide()
            self._update()
            self.stats.setText(f"Could not render {'A' if key == 'a' else 'B'}: {res.error}")
            self.result.show()
            return
        self._images[key] = res.image
        self._page_size = (res.header["page_w"], res.header["page_h"]) if key == "a" else getattr(self, "_page_size", None)
        if len(self._images) == 2:
            png = str(self._tmp / f"compare {len(list(self._tmp.iterdir())) + 1}.png")
            names = (Path(self.a.path).name, Path(self.b.path).name)
            QThreadPool.globalInstance().start(_ComposeJob(self._images["a"], self._images["b"], self._dpi, names, png, self._bridge))

    def _composed(self, img: QImage, rects, png: str):
        self.progress.hide()
        self._update()
        self._png = png
        n = len(rects)
        self.stats.setText("No differences found — the two pages render identically." if n == 0 else
                           f"{n} changed area{'s' if n != 1 else ''} (orange boxes). Red: only in A · Blue: only in B · Black: unchanged.")
        w = img.width() * 72.0 / self._dpi
        h = img.height() * 72.0 / self._dpi
        self.view.set_page(PageRef(png, 0, w, h, PageRef.stamp_for(png)))
        self.view.fit_page()
        self.result.show()

    def _text_done(self, res: RenderResult):
        self.changes.clear()
        if not res.ok:
            self.changes.addItem(res.error)
            return
        changes = res.header.get("changes", [])
        if not changes:
            self.changes.addItem("No text changes.")
        for sign, line in changes:
            it = QListWidgetItem(("−  " if sign == "-" else "+  ") + line)
            it.setForeground(RED if sign == "-" else BLUE)
            self.changes.addItem(it)

    def save(self):
        if not self._png:
            return
        folder = str(Path(self.b.path).parent) if self.b.path else self.host.default_folder()
        name = f"Compare {Path(self.a.path).stem} vs {Path(self.b.path).stem}.pdf"
        path, _ = QFileDialog.getSaveFileName(self.window(), "Save comparison", str(unique_path(Path(folder) / name)), "*.pdf")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self.host.render.submit("tool", {"op": "images_to_pdf", "paths": [self._png], "output": path},
                                lambda r: (r.ok and self.outputsCreated.emit(r.header.get("outputs", [])),
                                           self.stats.setText(self.stats.text() + ("\nSaved " + Path(path).name if r.ok else "\n" + r.error))),
                                priority=PRI_EXPORT, dedupe=False)

    def cleanup(self):
        self.view.release()
        shutil.rmtree(self._tmp, ignore_errors=True)
