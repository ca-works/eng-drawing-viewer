"""Thread boundary and owned pixel conversions for the image editor."""
from __future__ import annotations

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage


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
