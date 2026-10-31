"""Keyboard shortcuts that differ between macOS and Windows/Linux."""
from __future__ import annotations

import sys

from PySide6.QtGui import QKeySequence

from .theme import IS_MAC

IS_LINUX = sys.platform.startswith("linux")

# Shortcuts that differ by platform. On Windows, Ctrl+Alt is AltGr on many keyboard layouts
# (Ctrl+Alt+2 types "²" or "@"), so those combinations are never used there; Linux follows Windows
# (Ctrl+Alt+arrows and Ctrl+Alt+T are also taken by many Linux desktops).
KEYS = {
    "viewer": "Ctrl+Alt+V" if IS_MAC else "Ctrl+Shift+V",
    "tools": "Ctrl+Alt+T" if IS_MAC else "Ctrl+Shift+T",
    "image_editor": "Ctrl+Alt+I" if IS_MAC else "Ctrl+Shift+J",
    "vector_editor": "Ctrl+Alt+G" if IS_MAC else "Ctrl+Shift+G",
    "pdf_editor": "Ctrl+Alt+E" if IS_MAC else "Ctrl+Shift+P",
    "library": "Ctrl+Alt+1" if IS_MAC else "Ctrl+Shift+L",
    "inspector": "Ctrl+Alt+2" if IS_MAC else "Ctrl+Shift+I",
    "rescan": ["Ctrl+R"] if IS_MAC else ["F5", "Ctrl+R"],
    "settings": QKeySequence.StandardKey.Preferences if IS_MAC else "Ctrl+,",
    # Windows: Alt+F4 closes the window. Linux desktops (GNOME, KDE) use Ctrl+Q to quit.
    "quit": QKeySequence.StandardKey.Quit if IS_MAC else "Ctrl+Q" if IS_LINUX else None,
    "shortcuts": "Ctrl+/" if IS_MAC else ["F1", "Ctrl+/"],
}

MARK_PAGE = "Ctrl+Shift+M" if IS_MAC else "Ctrl+M"
