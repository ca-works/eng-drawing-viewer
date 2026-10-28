"""Small platform integrations (all local; nothing here touches the network)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"


def reveal_in_file_manager(path: str | None) -> None:
    if not path:
        return
    p = Path(path)
    try:
        if IS_MAC:
            subprocess.Popen(["/usr/bin/open", "-R", str(p)])
        elif IS_WIN:
            from .windows import reveal_in_explorer

            reveal_in_explorer(str(p))
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent if p.is_file() else p)))
    except OSError:
        pass


def file_manager_name() -> str:
    return "Finder" if IS_MAC else "Explorer" if IS_WIN else "File Manager"


def reveal_label() -> str:
    return f"Show in {file_manager_name()}"
