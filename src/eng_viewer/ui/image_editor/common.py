"""Thread boundary and owned pixel conversions for the image editor."""
from __future__ import annotations

import sys

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox


def to_qimage(image: Image.Image) -> QImage:
    rgba = image.convert("RGBA")
    return QImage(rgba.tobytes(), rgba.width, rgba.height, rgba.width * 4,
                  QImage.Format.Format_RGBA8888).copy()


def from_qimage(image: QImage) -> Image.Image:
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes("RGBA", (rgba.width(), rgba.height()), bytes(rgba.constBits()),
                           "raw", "RGBA", rgba.bytesPerLine()).copy()


class JobSignals(QObject):
    finished = Signal(object, object)
    progress = Signal(int, str)


class Job(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = JobSignals()

    def run(self):
        try:
            result = self.fn()
        except Exception as exc:
            self.signals.finished.emit(None, str(exc))
        else:
            self.signals.finished.emit(result, None)


def _configure_number_field(field):
    if sys.platform not in {"darwin", "win32"}:
        return
    # Qt's stylesheet masks collapse the edit area with NoButtons. Give the
    # stepper subcontrols zero geometry so the numeric editor keeps its width.
    field.setStyleSheet("""
        QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
            width: 0; height: 0; padding: 0; border: 0;
        }
    """)
    field.lineEdit().setTextMargins(6, 0, 6, 0)


class SpinBox(QSpinBox):
    def __init__(self):
        super().__init__()
        _configure_number_field(self)


class DoubleSpinBox(QDoubleSpinBox):
    def __init__(self):
        super().__init__()
        _configure_number_field(self)
