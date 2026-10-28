"""Lightweight startup splash that reports real startup stages.

It only appears when startup is actually slow (cold disk caches, antivirus
scanning on Windows); a fast start goes straight to the main window.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from .theme import ThemeManager, qc


class Splash(QWidget):
    SHOW_AFTER = 0.30  # seconds of startup before the splash is worth showing

    def __init__(self, icon: QPixmap | None, stages: int, started: float):
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._icon = icon
        self._stages = max(1, stages)
        self._done = 0
        self._text = "Starting…"
        self._started = started
        self.resize(380, 236)
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.center().x() - self.width() // 2, scr.center().y() - self.height() // 2 - 40)

    def stage(self, text: str):
        """Begin a named stage; shows the splash once startup is visibly slow."""
        self._text = text
        if not self.isVisible() and time.perf_counter() - self._started >= self.SHOW_AFTER:
            self.show()
        if self.isVisible():
            self.repaint()
            QApplication.processEvents()

    def complete_stage(self):
        self._done = min(self._stages, self._done + 1)
        if self.isVisible():
            self.repaint()

    def finish(self):
        self.close()
        self.deleteLater()

    def paintEvent(self, event):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        for i, a in enumerate((18, 10, 5)):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(card.adjusted(-i * 2, -i * 2 + 2, i * 2, i * 2 + 2), 16 + i * 2, 16 + i * 2)
        p.setBrush(qc(t.elevated))
        p.setPen(QPen(qc(t.separator), 1))
        p.drawRoundedRect(card, 16, 16)
        if self._icon is not None and not self._icon.isNull():
            r = QRectF(card.center().x() - 36, card.top() + 26, 72, 72)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(r, self._icon, QRectF(self._icon.rect()))
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() + 4)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(qc(t.text))
        p.drawText(QRectF(card.left(), card.top() + 110, card.width(), 30), Qt.AlignmentFlag.AlignCenter, "ENG Drawing Viewer")
        f2 = QFont(self.font())
        f2.setPointSizeF(max(9.0, f2.pointSizeF() - 1))
        p.setFont(f2)
        p.setPen(qc(t.text_secondary))
        p.drawText(QRectF(card.left(), card.top() + 140, card.width(), 22), Qt.AlignmentFlag.AlignCenter, self._text)
        bar = QRectF(card.left() + 70, card.bottom() - 34, card.width() - 140, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(t.separator))
        p.drawRoundedRect(bar, 2, 2)
        frac = self._done / self._stages
        if frac > 0:
            p.setBrush(qc(t.accent))
            p.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * frac, bar.height()), 2, 2)
        p.end()
