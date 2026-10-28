"""Export the queue to PowerPoint: options, progress and result in one sheet."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QRadioButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from ..core.config import QUALITY_DPI, Settings
from ..core.exporting import EXPORT_MAX_PIXELS
from ..core.render_service import PRI_EXPORT, RenderResult, RenderService
from ..platform import reveal_in_file_manager, reveal_label
from . import icons
from .queue_panel import QueueEntry
from .widgets import ElidedLabel, SegmentedControl, label, primary_button, secondary_button

IS_WIN = sys.platform == "win32"


class _SaveImage(QRunnable):
    def __init__(self, image: QImage, path: str, done):
        super().__init__()
        self.image, self.path, self.done = image, path, done
        self.setAutoDelete(True)

    def run(self):
        ok = self.image.save(self.path, "PNG")
        self.done.emit(self.path, ok)


class _SaveBridge(QObject):
    saved = Signal(str, bool)


class ExportController(QObject):
    """Renders slide images on the worker pool, then builds the deck."""

    progress = Signal(int, int, str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, service: RenderService, entries: list[QueueEntry], options: dict, parent=None):
        super().__init__(parent)
        self.service = service
        self.entries = list(entries)
        self.options = options
        self.tmp = tempfile.mkdtemp(prefix="eng-drawing-export-")
        self.images: dict[int, tuple[str, int, int]] = {}
        self.cancelled = False
        self.group = ("pptx-export", id(self))
        self._bridge = _SaveBridge(self)
        self._bridge.saved.connect(self._on_saved)
        self._pending_marked: dict[str, tuple[int, int, int]] = {}
        self._thread = None

    def start(self):
        dpi = QUALITY_DPI.get(self.options.get("quality", "high"), 220)
        total = len(self.entries)
        self.progress.emit(0, total, "Preparing slides…")
        for i, e in enumerate(self.entries):
            out = os.path.join(self.tmp, f"slide_{i + 1:04d}.png")
            if e.marked:
                self._pending_marked[out] = (i, e.marked_image.width(), e.marked_image.height())
                QThreadPool.globalInstance().start(_SaveImage(e.marked_image, out, self._bridge.saved))
            else:
                args = {"path": e.doc_path, "page": e.page_index, "scale": dpi / 72.0, "reinforce": True,
                        "max_pixels": EXPORT_MAX_PIXELS, "out_png": out, "dpi": dpi, "native_image": True}
                self.service.submit("render", args, lambda res, i=i: self._on_rendered(i, res),
                                    priority=PRI_EXPORT, group=self.group, dedupe=False)

    def cancel(self):
        self.cancelled = True
        self.service.cancel_group(self.group)
        self._cleanup()

    def _cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _on_rendered(self, i: int, res: RenderResult):
        if self.cancelled:
            return
        if not res.ok:
            e = self.entries[i]
            self.cancel()
            self.failed.emit(f"{e.display_name}, page {e.page_index + 1}: {res.error}")
            return
        self.images[i] = (res.header["path"], res.header["w"], res.header["h"])
        self._advance()

    def _on_saved(self, path: str, ok: bool):
        if self.cancelled:
            return
        i, w, h = self._pending_marked.pop(path)
        if not ok:
            self.cancel()
            self.failed.emit("Could not write a temporary image for a MARKed page.")
            return
        self.images[i] = (path, w, h)
        self._advance()

    def _advance(self):
        done, total = len(self.images), len(self.entries)
        self.progress.emit(done, total, f"Rendered {done} of {total} slide images")
        if done < total:
            return
        slides = []
        title_mode = self.options.get("title_mode", "title")
        for i, e in enumerate(self.entries):
            path, w, h = self.images[i]
            comments = [ln for ln in (e.comments or "").splitlines() if ln.strip()]
            slides.append({"image": path, "img_w": w, "img_h": h, "title": e.display_name if title_mode != "none" else "",
                           "marked": e.marked, "comments": comments})
        if self.options.get("mode") == "powerpoint":
            self._send_com(slides)
            return
        spec = {
            "output": self.options["output"],
            "mode": self.options.get("mode", "new"),
            "template": self.options.get("template"),
            "existing_mode": self.options.get("existing_mode", "insert"),
            "start_slide": self.options.get("start_slide", 1),
            "slide_size": self.options.get("slide_size", "16:9"),
            "title_mode": title_mode,
            "slides": slides,
        }
        self.progress.emit(total, total, "Building presentation…")
        self.service.submit("pptx", spec, self._on_built, priority=PRI_EXPORT, group=self.group, dedupe=False)

    def _on_built(self, res: RenderResult):
        self._cleanup()
        if self.cancelled:
            return
        if res.ok:
            self.finished.emit(res.header)
        else:
            self.failed.emit(res.error)

    def _send_com(self, slides):
        from ..platform.windows_powerpoint import ComExportThread

        self.progress.emit(len(slides), len(slides), "Sending slides to PowerPoint…")
        self._thread = ComExportThread(slides, self.options["target"], self.options.get("start_slide", 1),
                                       self.options.get("title_mode", "title"), self)
        self._thread.done.connect(lambda n: (self._cleanup(), self.finished.emit({"slides": n, "path": self.options["target"], "com": True})))
        self._thread.error.connect(lambda msg: (self._cleanup(), self.failed.emit(msg)))
        self._thread.start()


class ExportDialog(QDialog):
    def __init__(self, parent, service: RenderService, settings: Settings, entries: list[QueueEntry]):
        super().__init__(parent)
        self.setObjectName("Sheet")
        self.setWindowTitle("Export to PowerPoint")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.service = service
        self.settings = settings
        self.entries = entries
        self.controller: ExportController | None = None
        self.result_path: str | None = None
        self.template_path: str | None = None

        self.stack = QStackedWidget()
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.addWidget(self.stack)
        self.stack.addWidget(self._options_page())
        self.stack.addWidget(self._progress_page())
        self.stack.addWidget(self._done_page())

    # -------------------------------------------------------------- pages
    def _options_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        head = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icons.pixmap("presentation", 28, icons._resolve("accent")))
        head.addWidget(ic)
        tbox = QVBoxLayout()
        tbox.setSpacing(2)
        tbox.addWidget(label("Export to PowerPoint", "heading"))
        docs = len({e.doc_path for e in self.entries})
        marked = sum(1 for e in self.entries if e.marked)
        summary = f"{len(self.entries)} slide{'s' if len(self.entries) != 1 else ''} from {docs} drawing{'s' if docs != 1 else ''}"
        if marked:
            summary += f" · {marked} MARKed"
        tbox.addWidget(label(summary, "secondary"))
        head.addLayout(tbox, 1)
        lay.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        row = 0
        grid.addWidget(label("Destination", "secondary"), row, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        dest = QVBoxLayout()
        dest.setSpacing(6)
        self.rb_new = QRadioButton("New presentation")
        self.rb_existing = QRadioButton("Add to an existing presentation")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_new)
        self.mode_group.addButton(self.rb_existing)
        dest.addWidget(self.rb_new)
        dest.addWidget(self.rb_existing)
        ex_row = QHBoxLayout()
        ex_row.setContentsMargins(24, 0, 0, 0)
        self.choose_btn = secondary_button("Choose…")
        self.choose_btn.clicked.connect(self._choose_template)
        ex_row.addWidget(self.choose_btn)
        self.template_label = ElidedLabel("No file chosen")
        self.template_label.setProperty("role", "secondary")
        ex_row.addWidget(self.template_label, 1)
        dest.addLayout(ex_row)
        self.existing_opts = QWidget()
        eo = QHBoxLayout(self.existing_opts)
        eo.setContentsMargins(24, 0, 0, 0)
        eo.addWidget(label("Starting at slide", "secondary"))
        self.start_spin = QSpinBox()
        self.start_spin.setRange(1, 9999)
        eo.addWidget(self.start_spin)
        self.existing_seg = SegmentedControl([("insert", "Insert new slides", None, "Insert new slides at this position"),
                                              ("reuse", "Use existing slides", None, "Place drawings onto the existing slides from this position; add slides when they run out")])
        eo.addWidget(self.existing_seg)
        eo.addStretch(1)
        dest.addWidget(self.existing_opts)
        self.com_combo = None
        self.rb_com = None
        if IS_WIN:
            # Presentations open in PowerPoint are looked up on a worker thread (PowerPoint can take
            # a while to answer); the option appears only if there are any.
            from ..platform.windows_powerpoint import PresentationLookup

            self.rb_com = QRadioButton("Send to a presentation open in PowerPoint")
            self.mode_group.addButton(self.rb_com)
            dest.addWidget(self.rb_com)
            self.com_combo = QComboBox()
            c = QHBoxLayout()
            c.setContentsMargins(24, 0, 0, 0)
            c.addWidget(self.com_combo, 1)
            dest.addLayout(c)
            self.rb_com.hide()
            self.com_combo.hide()
            self._lookup = PresentationLookup()
            self._lookup.found.connect(self._on_presentations)
            self._lookup.start()
        grid.addLayout(dest, row, 1)
        row += 1

        grid.addWidget(label("Slide title", "secondary"), row, 0, Qt.AlignmentFlag.AlignRight)
        self.title_seg = SegmentedControl([("none", "None", None, "No filename on the slide"),
                                           ("title", "Filename as title", None, "Editable slide title"),
                                           ("subtitle", "As subtitle", None, "Editable subtitle")])
        grid.addWidget(self.title_seg, row, 1, Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid.addWidget(label("Image quality", "secondary"), row, 0, Qt.AlignmentFlag.AlignRight)
        self.quality_seg = SegmentedControl([("standard", "150 DPI", None, "Standard — smaller files"),
                                             ("high", "220 DPI", None, "High — recommended"),
                                             ("max", "300 DPI", None, "Maximum — largest files")])
        grid.addWidget(self.quality_seg, row, 1, Qt.AlignmentFlag.AlignLeft)
        row += 1
        self.size_label = label("Slide size", "secondary")
        grid.addWidget(self.size_label, row, 0, Qt.AlignmentFlag.AlignRight)
        self.size_seg = SegmentedControl([("16:9", "Widescreen 16:9", None, ""), ("4:3", "Standard 4:3", None, "")])
        grid.addWidget(self.size_seg, row, 1, Qt.AlignmentFlag.AlignLeft)
        row += 1
        self.open_when_done = QCheckBox("Open the presentation when finished")
        self.open_when_done.setChecked(bool(self.settings.get("export_open_after", True)))
        grid.addWidget(self.open_when_done, row, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        note = label("Filenames and comments stay editable in PowerPoint. Drawings are embedded as high-resolution images "
                     "with thin-line reinforcement. Microsoft PowerPoint does not need to be installed.", "tertiary", wrap=True)
        lay.addWidget(note)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = secondary_button("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.export_btn = primary_button("Export…")
        self.export_btn.setDefault(True)
        self.export_btn.clicked.connect(self._start)
        btns.addWidget(self.export_btn)
        lay.addLayout(btns)

        # Restore choices
        mode = self.settings.get("export_mode")
        (self.rb_existing if mode == "existing" else self.rb_new).setChecked(True)
        self.existing_seg.set_current(self.settings.get("existing_mode"))
        self.title_seg.set_current(self.settings.get("filename_placement"))
        self.quality_seg.set_current(self.settings.get("quality"))
        self.size_seg.set_current(self.settings.get("slide_size", "16:9"))
        self.mode_group.buttonToggled.connect(lambda *_: self._sync())
        self._sync()
        return page

    def _progress_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(label("Exporting to PowerPoint…", "heading"))
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        lay.addWidget(self.progress)
        self.progress_text = label("", "secondary")
        lay.addWidget(self.progress_text)
        lay.addStretch(1)
        b = QHBoxLayout()
        b.addStretch(1)
        self.cancel_export_btn = secondary_button("Cancel")
        self.cancel_export_btn.clicked.connect(self._cancel_export)
        b.addWidget(self.cancel_export_btn)
        lay.addLayout(b)
        return page

    def _done_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(10)
        h = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icons.pixmap("check", 28, icons._resolve("success"), stroke=2.2))
        h.addWidget(ic)
        self.done_title = label("", "heading")
        h.addWidget(self.done_title, 1)
        lay.addLayout(h)
        self.done_path = ElidedLabel("")
        self.done_path.setProperty("role", "secondary")
        lay.addWidget(self.done_path)
        lay.addStretch(1)
        b = QHBoxLayout()
        self.reveal_btn = secondary_button(reveal_label())
        self.reveal_btn.clicked.connect(lambda: reveal_in_file_manager(self.result_path))
        b.addWidget(self.reveal_btn)
        b.addStretch(1)
        self.open_btn = secondary_button("Open")
        self.open_btn.clicked.connect(self._open_result)
        b.addWidget(self.open_btn)
        done = primary_button("Done")
        done.clicked.connect(self.accept)
        b.addWidget(done)
        lay.addLayout(b)
        return page

    def _on_presentations(self, opened: list):
        if not opened or self.com_combo is None:
            return
        self.com_combo.clear()
        for name, full in opened:
            self.com_combo.addItem(name, full)
            self.com_combo.setItemData(self.com_combo.count() - 1, full, Qt.ItemDataRole.ToolTipRole)
        self.rb_com.show()
        self.com_combo.show()
        self._sync()

    # ------------------------------------------------------------ actions
    def _mode(self) -> str:
        if self.rb_existing.isChecked():
            return "existing"
        if self.com_combo is not None and self.rb_com is not None and self.rb_com.isChecked():
            return "powerpoint"
        return "new"

    def _sync(self):
        mode = self._mode()
        existing = mode == "existing"
        self.choose_btn.setEnabled(existing)
        self.template_label.setEnabled(existing)
        self.existing_opts.setVisible(existing or mode == "powerpoint")
        self.existing_seg.setVisible(existing)
        self.size_seg.setVisible(mode == "new")
        self.size_label.setVisible(mode == "new")
        if self.com_combo is not None:
            self.com_combo.setEnabled(mode == "powerpoint")
        self.export_btn.setText("Export…" if mode == "new" else "Export")
        self.export_btn.setEnabled(mode != "existing" or self.template_path is not None)

    def _choose_template(self):
        start = self.settings.get("export_dir") or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose a PowerPoint presentation", start, "PowerPoint presentation (*.pptx)")
        if path:
            self.template_path = path
            self.template_label.setText(Path(path).name)
            self.template_label.setToolTip(path)
        self._sync()

    def _start(self):
        mode = self._mode()
        opts = {
            "mode": mode,
            "title_mode": self.title_seg.current_key(),
            "quality": self.quality_seg.current_key(),
            "slide_size": self.size_seg.current_key(),
            "existing_mode": self.existing_seg.current_key(),
            "start_slide": self.start_spin.value(),
        }
        if mode == "new":
            folder = self.settings.get("export_dir") or str(Path.home() / "Documents")
            default = Path(folder) / f"Drawings {date.today().isoformat()}.pptx"
            path, _ = QFileDialog.getSaveFileName(self, "Save presentation", str(default), "PowerPoint presentation (*.pptx)")
            if not path:
                return
            if not path.lower().endswith(".pptx"):
                path += ".pptx"
            opts["output"] = path
        elif mode == "existing":
            # Ask where to save: defaulting to the same file means replacing it goes
            # through the system's own "Replace?" confirmation, and a copy is one click away.
            path, _ = QFileDialog.getSaveFileName(self, "Save the updated presentation", self.template_path,
                                                  "PowerPoint presentation (*.pptx)")
            if not path:
                return
            if not path.lower().endswith(".pptx"):
                path += ".pptx"
            opts["template"] = self.template_path
            opts["output"] = path
        else:
            opts["target"] = self.com_combo.currentData()
        self.settings.set("export_mode", "existing" if mode == "existing" else "new")
        self.settings.set("existing_mode", opts["existing_mode"])
        self.settings.set("filename_placement", opts["title_mode"])
        self.settings.set("quality", opts["quality"])
        self.settings.set("slide_size", opts["slide_size"])
        self.settings.set("export_open_after", self.open_when_done.isChecked())
        if opts.get("output"):
            self.settings.set("export_dir", str(Path(opts["output"]).parent))
        self.settings.save()

        self.stack.setCurrentIndex(1)
        self.progress.setRange(0, len(self.entries))
        self.controller = ExportController(self.service, self.entries, opts, self)
        self.controller.progress.connect(self._on_progress)
        self.controller.finished.connect(self._on_finished)
        self.controller.failed.connect(self._on_failed)
        self.controller.start()

    def _on_progress(self, done: int, total: int, text: str):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        if text.startswith("Building") or text.startswith("Sending"):
            self.progress.setRange(0, 0)
        self.progress_text.setText(text)

    def _on_finished(self, info: dict):
        self.result_path = info.get("path")
        n = info.get("slides", 0)
        self.done_title.setText(f"Exported {n} slide{'s' if n != 1 else ''}")
        self.done_path.setText(self.result_path or "")
        self.stack.setCurrentIndex(2)
        com = info.get("com")
        self.reveal_btn.setVisible(not com)
        self.open_btn.setVisible(not com)
        if self.open_when_done.isChecked() and not com and self.result_path:
            self._open_result()

    def _on_failed(self, msg: str):
        from PySide6.QtWidgets import QMessageBox

        self.stack.setCurrentIndex(0)
        QMessageBox.warning(self, "Export failed", msg)

    def _cancel_export(self):
        if self.controller is not None:
            self.controller.cancel()
        self.stack.setCurrentIndex(0)

    def _open_result(self):
        if self.result_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.result_path))

    def reject(self):
        if self.controller is not None and self.stack.currentIndex() == 1:
            self.controller.cancel()
        super().reject()
