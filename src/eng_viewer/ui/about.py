"""About dialog with the legal notices the AGPL asks interactive programs to show."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .. import APP_NAME, COPYRIGHT, LICENSE_NAME, SOURCE_URL, __version__

NOTICE = (
    "This program is free software: you can redistribute it and/or modify it under the terms of "
    "the GNU Affero General Public License as published by the Free Software Foundation, either "
    "version 3 of the License, or (at your option) any later version.\n\n"
    "This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; "
    "without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. "
    "See the licence for details."
)


def legal_dir() -> Path:
    """LICENSE and licenses/ ship in a 'legal' folder in builds, and at the repository root in source.

    In the Mac app it is inside the bundle; on Windows it sits next to the launcher, where people
    can also read it without starting the app (the application itself is in the "app" folder).
    """
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        for folder in (Path(frozen) / "legal", Path(sys.executable).resolve().parent.parent / "legal"):
            if folder.is_dir():
                return folder
    return Path(__file__).resolve().parents[3]


def license_text() -> str:
    try:
        return (legal_dir() / "LICENSE").read_text(encoding="utf-8")
    except OSError:
        return f"The licence text was not found. {APP_NAME} is licensed under the {LICENSE_NAME}."


def third_party_text() -> str:
    folder = legal_dir() / "licenses"
    parts = []
    for path in sorted(folder.glob("*.txt")) if folder.is_dir() else []:
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace").rstrip())
        except OSError:
            continue
    return ("\n\n" + "=" * 72 + "\n\n").join(parts) or "No third-party notices were found."


class TextViewer(QDialog):
    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 640)
        box = QPlainTextEdit(text)
        box.setReadOnly(True)
        box.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addWidget(buttons)


class AboutDialog(QDialog):
    def __init__(self, icon: QPixmap | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(520)
        head = QHBoxLayout()
        if icon is not None and not icon.isNull():
            pic = QLabel()
            pic.setPixmap(icon.scaled(88, 88, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
            head.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        title = QLabel(f"<b style='font-size:17px'>{APP_NAME}</b><br>Version {__version__}<br>{COPYRIGHT}")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(title, 1)
        about = QLabel("A free, offline viewer for engineering drawings, with markup, PDF tools and "
                       "PowerPoint export. Everything runs on this computer; PDFs are opened read-only.")
        about.setWordWrap(True)
        notice = QLabel(NOTICE)
        notice.setWordWrap(True)
        notice.setObjectName("secondary")
        # The address is plain, selectable text: the app itself never goes online.
        source = QLabel(f"Source code for this version:\n{SOURCE_URL}")
        source.setWordWrap(True)
        source.setTextFormat(Qt.TextFormat.PlainText)
        source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        lic = QPushButton("Licence…")
        third = QPushButton("Third-Party Notices…")
        buttons.addButton(lic, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(third, QDialogButtonBox.ButtonRole.ActionRole)
        lic.clicked.connect(lambda: TextViewer(LICENSE_NAME, license_text(), self).exec())
        third.clicked.connect(lambda: TextViewer("Third-Party Notices", third_party_text(), self).exec())
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.addLayout(head)
        for w in (about, notice, source):
            layout.addWidget(w)
        layout.addWidget(buttons)
