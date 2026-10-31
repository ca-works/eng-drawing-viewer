"""Settings and keyboard-shortcut reference dialogs."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from .shortcuts import KEYS, MARK_PAGE
from .widgets import SegmentedControl, label, primary_button, secondary_button, shortcut_text

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"


def _keys(spec) -> str:
    items = spec if isinstance(spec, list) else [spec]
    return " / ".join(shortcut_text(k) if isinstance(k, QKeySequence.StandardKey) else k for k in items)


class PreferencesDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.settings = window.settings
        self.setWindowTitle("Settings")
        self.setObjectName("Sheet")
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(14)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(12)
        r = 0

        grid.addWidget(label("Appearance", "secondary"), r, 0, Qt.AlignmentFlag.AlignRight)
        self.theme = SegmentedControl([("system", "System", "monitor", "Follow the system setting"),
                                       ("light", "Light", "sun", ""), ("dark", "Dark", "moon", "")])
        self.theme.set_current(self.settings.get("theme"))
        self.theme.changed.connect(window.set_theme)
        grid.addWidget(self.theme, r, 1, Qt.AlignmentFlag.AlignLeft)
        r += 1

        grid.addWidget(label("Drawings", "secondary"), r, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        box = QVBoxLayout()
        box.setSpacing(2)
        self.thin = QCheckBox("Enhance thin lines")
        self.thin.setChecked(bool(self.settings.get("enhance_thin_lines")))
        self.thin.toggled.connect(window.set_thin_lines)
        box.addWidget(self.thin)
        box.addWidget(label("Draws hairlines at least one screen pixel wide so fine drawing lines never look washed out. "
                            "Exports always reinforce hairlines.", "tertiary", wrap=True))
        grid.addLayout(box, r, 1)
        r += 1

        grid.addWidget(label("Copy & export", "secondary"), r, 0, Qt.AlignmentFlag.AlignRight)
        self.quality = SegmentedControl([("standard", "150 DPI", None, "Standard"), ("high", "220 DPI", None, "High (recommended)"),
                                         ("max", "300 DPI", None, "Maximum")])
        self.quality.set_current(self.settings.get("quality"))
        self.quality.changed.connect(lambda k: (self.settings.set("quality", k), self.settings.save()))
        grid.addWidget(self.quality, r, 1, Qt.AlignmentFlag.AlignLeft)
        r += 1

        grid.addWidget(label("Image clipboard", "secondary"), r, 0, Qt.AlignmentFlag.AlignRight)
        clipboard = secondary_button("Format and options…", "copy-image")
        clipboard.clicked.connect(self._clipboard_options)
        grid.addWidget(clipboard, r, 1, Qt.AlignmentFlag.AlignLeft)
        r += 1

        grid.addWidget(label("Library", "secondary"), r, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        lib = QVBoxLayout()
        lib.setSpacing(2)
        self.recursive = QCheckBox("Include subfolders when opening a folder")
        self.recursive.setChecked(bool(self.settings.get("recursive_scan")))
        self.recursive.toggled.connect(window._set_recursive)
        lib.addWidget(self.recursive)
        self.cache = QCheckBox("Keep a thumbnail cache on this computer")
        self.cache.setChecked(bool(self.settings.get("thumbnail_disk_cache")))
        self.cache.toggled.connect(self._cache_toggled)
        lib.addWidget(self.cache)
        row = QHBoxLayout()
        self.cache_info = label("", "tertiary")
        row.addWidget(self.cache_info, 1)
        clear = secondary_button("Clear Cache")
        clear.clicked.connect(self._clear_cache)
        row.addWidget(clear)
        lib.addLayout(row)
        grid.addLayout(lib, r, 1)
        r += 1

        if IS_MAC or IS_WIN:
            grid.addWidget(label("Spelling", "secondary"), r, 0, Qt.AlignmentFlag.AlignRight)
            system = (("system", "macOS spelling", None, "Uses your Mac's languages and learned words") if IS_MAC else
                      ("system", "Windows spelling", None, "Uses Windows' spelling dictionaries and the words you add"))
            self.spell = SegmentedControl([system,
                                           ("builtin", "Built-in English", None, "Bundled offline word list")])
            self.spell.set_current(self.settings.get("spell_backend", "system"))
            self.spell.changed.connect(lambda k: (self.settings.set("spell_backend", k), self.settings.save()))
            grid.addWidget(self.spell, r, 1, Qt.AlignmentFlag.AlignLeft)
            r += 1
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        lay.addWidget(label("ENG Drawing Viewer works entirely offline. Nothing is uploaded. Editing saves to a separate file.", "tertiary", wrap=True))
        b = QHBoxLayout()
        b.addStretch(1)
        done = primary_button("Done")
        done.clicked.connect(self.accept)
        b.addWidget(done)
        lay.addLayout(b)
        self._refresh_cache_info()

    def _refresh_cache_info(self):
        mb = self.win.thumbs.cache_size_bytes() / (1024 * 1024)
        self.cache_info.setText(f"Thumbnail cache: {mb:.1f} MB")

    def _clipboard_options(self):
        from .image_editor.clipboard_dialog import ClipboardDialog
        dialog = ClipboardDialog(self.settings.get("image_clipboard", {}), True, self, preferences_only=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings.set("image_clipboard", dialog.options())
            self.settings.save()

    def _cache_toggled(self, on: bool):
        self.settings.set("thumbnail_disk_cache", on)
        self.settings.save()
        self.win.thumbs.disk_enabled = on

    def _clear_cache(self):
        self.win.thumbs.clear_disk_cache()
        self._refresh_cache_info()


SHORTCUTS = [
    ("Navigation", [
        ("Previous / next page (continues into the previous / next PDF)", "Left / Right"),
        ("Previous / next page", "PgUp / PgDown"),
        ("First / last page", "Home / End"),
        ("Previous / next drawing", "Ctrl+Shift+Up / Ctrl+Shift+Down"),
        ("Go to page", "Ctrl+G"),
        ("Filter the library", "Ctrl+F"),
    ]),
    ("Viewing", [
        ("Zoom in / out (centred)", "Ctrl+= / Ctrl+-"),
        ("Zoom at the pointer", "Ctrl+scroll wheel" if not IS_MAC else "⌘+scroll or pinch"),
        ("Actual size / fit width / fit page", "Ctrl+0 / Ctrl+1 / Ctrl+2"),
        ("Pan", "Drag · middle-drag · Space+drag"),
        ("Smart zoom", "Double-click" if not IS_MAC else "Double-click or two-finger double-tap"),
    ]),
    ("Clipboard & export", [
        ("Copy page as image", "Ctrl+C"),
        ("Copy filename", "Ctrl+Shift+C"),
        ("Save page as PNG", "Ctrl+Shift+S"),
        ("Add page to PowerPoint queue", "Ctrl+D"),
        ("Export queue to PowerPoint", "Ctrl+E"),
        ("MARK this page", MARK_PAGE),
    ]),
    ("Window", [
        ("Viewer / PDF Tools", f"{KEYS['viewer']} / {KEYS['tools']}"),
        ("Show or hide the library / inspector", f"{KEYS['library']} / {KEYS['inspector']}"),
        ("Rescan the folder", _keys(KEYS["rescan"])),
        ("Settings", _keys(KEYS["settings"])),
        ("Keyboard shortcuts", _keys(KEYS["shortcuts"])),
    ] + ([] if IS_MAC else [("Full screen", "F11")])),
    ("MARK", [
        ("Select · Pen · Highlighter · Text", "V · P · H · T"),
        ("Arrow · Circle · Callout bubble · Eraser", "A · O · B · E"),
        ("Smaller / larger tool size (with live preview)", "[ / ]"),
        ("Undo / redo", "Ctrl+Z / Ctrl+Shift+Z" if IS_MAC else "Ctrl+Z / Ctrl+Y"),
        ("Delete selection", "Delete"),
        ("Copy marked page / numbered comments", "Ctrl+C / Ctrl+Shift+C"),
    ]),
]


def _native(seq: str) -> str:
    parts = []
    for piece in seq.split(" / "):
        if any(k in piece for k in ("Ctrl", "Shift", "Alt", "PgUp", "PgDown", "Home", "End", "Left", "Right", "Up", "Down")) and "+scroll" not in piece and "drag" not in piece:
            parts.append(shortcut_text(QKeySequence(piece)) or piece)
        else:
            parts.append(piece)
    return " / ".join(parts)


class ShortcutsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.setObjectName("Sheet")
        self.resize(560, 620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        g = QGridLayout(inner)
        g.setVerticalSpacing(6)
        g.setHorizontalSpacing(18)
        r = 0
        for section, rows in SHORTCUTS:
            g.addWidget(label(section.upper(), "section"), r, 0, 1, 2)
            r += 1
            for desc, keys in rows:
                g.addWidget(label(desc), r, 0)
                k = label(_native(keys), "secondary")
                k.setAlignment(Qt.AlignmentFlag.AlignRight)
                g.addWidget(k, r, 1)
                r += 1
            g.setRowMinimumHeight(r, 10)
            r += 1
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        b = QHBoxLayout()
        b.addStretch(1)
        ok = primary_button("Done")
        ok.clicked.connect(self.accept)
        b.addWidget(ok)
        lay.addLayout(b)
