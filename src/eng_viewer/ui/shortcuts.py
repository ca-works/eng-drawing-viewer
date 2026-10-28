"""Keyboard shortcuts that differ between macOS and Windows."""
from __future__ import annotations

from PySide6.QtGui import QKeySequence

from .theme import IS_MAC

# Shortcuts that differ by platform. On Windows, Ctrl+Alt is AltGr on many keyboard layouts
# (Ctrl+Alt+2 types "²" or "@"), so those combinations are never used there.
KEYS = {
    "viewer": "Ctrl+Alt+V" if IS_MAC else "Ctrl+Shift+V",
    "tools": "Ctrl+Alt+T" if IS_MAC else "Ctrl+Shift+T",
    "library": "Ctrl+Alt+1" if IS_MAC else "Ctrl+Shift+L",
    "inspector": "Ctrl+Alt+2" if IS_MAC else "Ctrl+Shift+I",
    "rescan": ["Ctrl+R"] if IS_MAC else ["F5", "Ctrl+R"],
    "settings": QKeySequence.StandardKey.Preferences if IS_MAC else "Ctrl+,",
    "quit": QKeySequence.StandardKey.Quit if IS_MAC else None,  # Windows: Alt+F4 closes the window
    "shortcuts": "Ctrl+/" if IS_MAC else ["F1", "Ctrl+/"],
}

MARK_PAGE = "Ctrl+Shift+M" if IS_MAC else "Ctrl+M"
