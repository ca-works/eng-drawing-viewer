"""Shared macOS Send To actions and Launch Services handoff feedback."""
from __future__ import annotations

import shiboken6
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QMessageBox

from ..platform.macos_iwork import application_url, open_in_app

EXTENSIONS = {"Keynote": ".pptx", "Pages": ".docx", "Numbers": ".xlsx"}


def app_icon(url):
    from AppKit import NSWorkspace

    image = NSWorkspace.sharedWorkspace().iconForFile_(url.path())
    pixels = QPixmap()
    data = image.TIFFRepresentation()
    if data is not None:
        pixels.loadFromData(bytes(data))
    return QIcon(pixels)


def send_menu(parent_menu, send, *, apps=("Keynote", "Pages", "Numbers"), suitable=None, title="Send To"):
    menu = parent_menu.addMenu(title)
    actions = {}
    for name in apps:
        actions[name] = menu.addAction(name + "…", lambda _=False, app=name: send(app))

    def sync():
        for name, action in actions.items():
            url = application_url(name)
            usable = suitable(name) if suitable else True
            action.setEnabled(url is not None and usable)
            if url is not None:
                action.setIcon(app_icon(url))
            if url is None:
                action.setToolTip(f"Install {name} to send files to it.")
            elif not usable and name == "Numbers":
                action.setToolTip("Send a CSV or TSV reference to Numbers from Standards.")
            else:
                action.setToolTip(f"Send to {name}")

    menu.setToolTipsVisible(True)
    menu.aboutToShow.connect(sync)
    return menu


class _OpenResult(QObject):
    finished = Signal(str)

    def __init__(self, parent, app, path):
        super().__init__(parent)
        self.app = app
        self.path = path
        self.finished.connect(self._done)

    def start(self):
        def completed(message):
            if shiboken6.isValid(self):
                self.finished.emit(message)

        open_in_app(self.app, self.path, completed)

    @Slot(str)
    def _done(self, error):
        if error:
            QMessageBox.warning(self.parent(), f"Could not open in {self.app}",
                                f"The exported file is available at:\n{self.path}\n\n{error}")
        self.deleteLater()


def open_result(parent, app, path):
    result = _OpenResult(parent, app, path)
    result.start()
