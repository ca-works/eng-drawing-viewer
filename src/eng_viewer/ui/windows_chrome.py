"""Windows title bars that match the app theme.

Qt already switches every window's title bar between light and dark with the app's colour scheme.
The main window and MARK windows additionally get, on Windows 11, a caption tinted with the
toolbar colour so the title bar and toolbar read as one surface, as in the Mac version (Windows 10
keeps its standard caption colours).
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QWidget

from .theme import Theme, ThemeManager


class _Chrome(QObject):
    def __init__(self):
        super().__init__()
        self._windows: list[QWidget] = []
        self._connected = False

    def track(self, w: QWidget) -> None:
        if sys.platform != "win32":
            return
        if not self._connected and ThemeManager._instance is not None:
            ThemeManager.instance().changed.connect(self._restyle_all)
            self._connected = True
        self._windows.append(w)
        w.installEventFilter(self)
        w.destroyed.connect(lambda *_: self._windows.remove(w) if w in self._windows else None)
        if w.isVisible():
            self.style(w)

    @staticmethod
    def style(w: QWidget, theme: Theme | None = None) -> None:
        from ..platform import windows

        t = theme or ThemeManager.current()
        windows.style_title_bar(w, t.dark, caption=t.toolbar, text=t.text, border=t.separator)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange):
            self.style(obj)
        return False

    def _restyle_all(self, theme: Theme):
        for w in list(self._windows):
            try:
                if w.isVisible():
                    self.style(w, theme)
            except RuntimeError:  # already deleted
                pass


_chrome: _Chrome | None = None


def track(w: QWidget) -> None:
    """Keep ``w``'s title bar styled to the current theme (Windows only; does nothing elsewhere)."""
    global _chrome
    if sys.platform != "win32":
        return
    if _chrome is None:
        _chrome = _Chrome()
    _chrome.track(w)
