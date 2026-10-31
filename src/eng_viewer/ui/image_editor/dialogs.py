"""Measured export preview and precise geometry dialogs."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt, QTimer, QThreadPool, QRectF
from PySide6.QtGui import QPainter, QColor, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSlider, QSpinBox,
    QVBoxLayout, QWidget)

from ...core.image_export import optimize_export, export_file
from .common import Job, to_qimage


class Comparison(QWidget):
    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.before = QPixmap.fromImage(to_qimage(image))
        self.after = self.before
        self.fraction = .5
        self.setMinimumSize(400, 250)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#252b35"))
        target = QRectF(self.rect()).adjusted(12, 12, -12, -12)
        size = self.before.size().scaled(target.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2, size.width(), size.height())
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(target, self.before, QRectF(self.before.rect()))
        p.save()
        p.setClipRect(QRectF(target.left() + target.width() * self.fraction, target.top(), target.width() * (1 - self.fraction), target.height()))
        p.drawPixmap(target, self.after, QRectF(self.after.rect()))
        p.restore()
        p.setPen(Qt.GlobalColor.white)
        x = target.left() + target.width() * self.fraction
        p.drawLine(int(x), int(target.top()), int(x), int(target.bottom()))
        p.drawText(22, 30, "Original")
        p.drawText(self.width() - 95, 30, "Export")


class ExportDialog(QDialog):
    def __init__(self, document, parent=None):
        super().__init__(parent)
        self.document = document
        self.image = document.composite()
        self.result = None
        self._job = None
        self._generation = 0
        self._pending = False
        self._cache = {}
        self.setWindowTitle("Export image")
        self.resize(800, 660)
        layout = QVBoxLayout(self)
        preview = self.image.copy()
        preview.thumbnail((1100, 650))
        self.compare = Comparison(preview)
        layout.addWidget(self.compare, 1)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(50)
        slider.setToolTip("Slide to compare original and encoded output")
        slider.valueChanged.connect(lambda n: (setattr(self.compare, "fraction", n / 100), self.compare.update()))
        layout.addWidget(slider)
        form = QFormLayout()
        self.format = QComboBox()
        # Entries are limited to codecs exercised by the editor test corpus.
        self.format.addItems(["PNG", "JPEG", "WEBP", "TIFF", "BMP", "GIF", "AVIF", "HEIF"])
        form.addRow("Format", self.format)
        self.preset = QComboBox()
        self.preset.addItems(["Maximum quality", "Balanced", "Small file", "Custom target"])
        self.preset.setCurrentIndex(1)
        form.addRow("Preset", self.preset)
        quality_row = QWidget()
        ql = QHBoxLayout(quality_row)
        ql.setContentsMargins(0, 0, 0, 0)
        self.quality = QSlider(Qt.Orientation.Horizontal)
        self.quality.setRange(1, 100)
        self.quality.setValue(90)
        self.quality_number = QSpinBox()
        self.quality_number.setRange(1, 100)
        self.quality_number.setValue(90)
        self.quality.valueChanged.connect(self.quality_number.setValue)
        self.quality_number.valueChanged.connect(self.quality.setValue)
        ql.addWidget(self.quality)
        ql.addWidget(self.quality_number)
        form.addRow("Quality", quality_row)
        target_row = QWidget()
        tl = QHBoxLayout(target_row)
        tl.setContentsMargins(0, 0, 0, 0)
        self.target = QComboBox()
        for name, value in [("No size target", 0), ("Under 100 KB", 100_000), ("Under 500 KB", 500_000),
                            ("Under 1 MB", 1_000_000), ("Under 5 MB", 5_000_000), ("Custom KB", -1)]:
            self.target.addItem(name, value)
        self.custom = QSpinBox()
        self.custom.setRange(1, 2_000_000)
        self.custom.setValue(500)
        self.custom.setSuffix(" KB")
        self.custom.setEnabled(False)
        tl.addWidget(self.target)
        tl.addWidget(self.custom)
        form.addRow("File size", target_row)
        self.allow_resize = QCheckBox("Allow smaller dimensions to meet the target")
        form.addRow("Resolution", self.allow_resize)
        self.profile = QCheckBox("Keep embedded colour profile")
        self.profile.setChecked(True)
        form.addRow("Colour", self.profile)
        self.metadata = QCheckBox("Preserve source metadata (may contain location)")
        form.addRow("Metadata", self.metadata)
        self.dpi = QSpinBox()
        self.dpi.setRange(1, 2400)
        self.dpi.setValue(round(document.dpi))
        form.addRow("DPI", self.dpi)
        layout.addLayout(form)
        self.info = QLabel("Encoding preview…")
        self.info.setWordWrap(True)
        self.info.setMinimumHeight(52)
        layout.addWidget(self.info)
        self.transparency = QLabel("JPEG and BMP flatten transparency onto white; GIF uses palette transparency.")
        self.transparency.setProperty("role", "small")
        layout.addWidget(self.transparency)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        self.save_button = buttons.addButton("Export…", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.clicked.connect(self.save)
        self.save_button.setEnabled(False)
        layout.addWidget(buttons)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._start)
        for signal in (self.format.currentIndexChanged, self.quality.valueChanged, self.target.currentIndexChanged,
                       self.custom.valueChanged, self.allow_resize.toggled, self.profile.toggled,
                       self.metadata.toggled, self.dpi.valueChanged):
            signal.connect(self.schedule)
        self.preset.currentIndexChanged.connect(self._preset)
        self.schedule()

    def _preset(self, index):
        if index < 3:
            self.quality.setValue([100, 90, 65][index])
            self.target.setCurrentIndex(0)
        else:
            self.target.setCurrentIndex(2)

    def schedule(self, *_):
        self._generation += 1
        self.result = None
        self.save_button.setEnabled(False)
        self.custom.setEnabled(self.target.currentData() == -1)
        self.info.setText("Encoding preview…")
        self.timer.start(180)

    def options(self):
        meta = dict(self.document.metadata) if self.metadata.isChecked() else {}
        if self.profile.isChecked() and self.document.metadata.get("icc_profile"):
            meta["icc_profile"] = self.document.metadata["icc_profile"]
        if not self.profile.isChecked():
            meta.pop("icc_profile", None)
        target = self.target.currentData()
        if target == -1:
            target = self.custom.value() * 1000
        return dict(format=self.format.currentText(), quality=self.quality.value(), target_bytes=target or None,
                    min_quality=min(35, self.quality.value()), allow_resize=self.allow_resize.isChecked(),
                    dpi=self.dpi.value(), metadata=meta)

    def _start(self):
        if self._job is not None:
            self._pending = True
            return
        options = self.options()
        key = (options['format'], options['quality'], options['target_bytes'], options['min_quality'],
               options['allow_resize'], options['dpi'], self.profile.isChecked(), self.metadata.isChecked())
        generation = self._generation
        if key in self._cache:
            self._ready(self._cache[key], None, generation, key)
            return
        self._job = Job(lambda: optimize_export(self.image, **options))
        self._job.signals.finished.connect(lambda result, error: self._ready(result, error, generation, key))
        QThreadPool.globalInstance().start(self._job)

    def _ready(self, result, error, generation, key):
        self._job = None
        if result is not None and len(result.data) <= 32_000_000:
            if len(self._cache) >= 5:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = result
        if generation == self._generation:
            if error:
                self.info.setText(error)
            else:
                self.result = result
                decoded = Image.open(io.BytesIO(result.data))
                decoded.thumbnail((1100, 650))
                self.compare.after = QPixmap.fromImage(to_qimage(decoded))
                self.compare.update()
                original = Path(self.document.source_path).stat().st_size if self.document.source_path and Path(self.document.source_path).is_file() else 0
                reduction = f" · {100 * (1 - len(result.data) / original):.1f}% smaller" if original else ""
                before = f"Original {original / 1000:,.1f} KB → " if original else ""
                self.info.setText(f"{before}{len(result.data) / 1000:,.1f} KB{reduction}\n"
                                  f"{result.width} × {result.height} px · {self.format.currentText()} · quality {result.quality}\n{result.reason}")
                self.save_button.setEnabled(True)
        if self._pending or generation != self._generation:
            self._pending = False
            self.timer.start(0)

    def save(self):
        if self.result is None:
            return
        fmt = self.format.currentText().lower()
        ext = {'jpeg': 'jpg', 'tiff': 'tif', 'heif': 'heic'}.get(fmt, fmt)
        default = Path(self.document.source_path).stem if self.document.source_path else "Image"
        path, _ = QFileDialog.getSaveFileName(self, "Export a new image", f"{default}-edited.{ext}", f"{fmt.upper()} (*.{ext})")
        if not path:
            return
        try:
            export_file(self.result, path, source_paths=[self.document.source_path] if self.document.source_path else (), overwrite=True)
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.accept()

    def reject(self):
        # Worker owns only immutable image data; it may finish harmlessly after closing.
        self.timer.stop()
        super().reject()


class ResizeDialog(QDialog):
    def __init__(self, document, canvas=False, parent=None):
        super().__init__(parent)
        self.document = document
        self.canvas = canvas
        self.setWindowTitle("Canvas size" if canvas else "Resize image")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.unit = QComboBox()
        self.unit.addItems(["Pixels", "Percent", "Inches", "Centimeters"])
        self.w = QDoubleSpinBox()
        self.h = QDoubleSpinBox()
        for spin in (self.w, self.h):
            spin.setRange(.01, 65535)
            spin.setDecimals(2)
        self.dpi = QSpinBox()
        self.dpi.setRange(1, 2400)
        self.dpi.setValue(round(document.dpi))
        self.lock = QCheckBox("Maintain aspect ratio")
        self.lock.setChecked(not canvas)
        form.addRow("Units", self.unit)
        form.addRow("Width", self.w)
        form.addRow("Height", self.h)
        form.addRow("DPI", self.dpi)
        form.addRow("", self.lock)
        lay.addLayout(form)
        self.summary = QLabel()
        lay.addWidget(self.summary)
        self.unit.currentIndexChanged.connect(self._units)
        self.dpi.valueChanged.connect(self._summary)
        self.w.valueChanged.connect(lambda _: self._changed(True))
        self.h.valueChanged.connect(lambda _: self._changed(False))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._units()

    def _factor(self):
        return [1, self.document.width / 100, self.dpi.value(), self.dpi.value() / 2.54][self.unit.currentIndex()]

    def _units(self, *_):
        self.w.blockSignals(True)
        self.h.blockSignals(True)
        if self.unit.currentIndex() == 1:
            self.w.setValue(100)
            self.h.setValue(100)
        else:
            factor = self._factor()
            self.w.setValue(self.document.width / factor)
            self.h.setValue(self.document.height / factor)
        self.w.blockSignals(False)
        self.h.blockSignals(False)
        self._summary()

    def _changed(self, width):
        if self.lock.isChecked():
            ratio = 1 if self.unit.currentIndex() == 1 else self.document.width / self.document.height
            other = self.h if width else self.w
            other.blockSignals(True)
            other.setValue(self.w.value() / ratio if width else self.h.value() * ratio)
            other.blockSignals(False)
        self._summary()

    def dimensions(self):
        if self.unit.currentIndex() == 1:
            return max(1, round(self.w.value() * self.document.width / 100)), max(1, round(self.h.value() * self.document.height / 100))
        return max(1, round(self.w.value() * self._factor())), max(1, round(self.h.value() * self._factor()))

    def _summary(self, *_):
        w, h = self.dimensions()
        self.summary.setText(f"{self.document.width} × {self.document.height} → {w} × {h} px\n"
                             + ("Canvas extends with transparency around the centre." if self.canvas else "High-quality Lanczos resampling. Layers and masks are preserved."))
