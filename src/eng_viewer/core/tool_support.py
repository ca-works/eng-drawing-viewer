"""Cheap capability checks for the UI process (never imports PyMuPDF)."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

OFFICE_EXTS = frozenset({".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp", ".txt"})


def find_office() -> str | None:
    """Path of a local LibreOffice ``soffice`` executable, if installed."""
    candidates = [shutil.which("soffice"), shutil.which("libreoffice")]
    if sys.platform == "darwin":
        candidates.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    elif os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            candidates.append(os.path.join(base, "LibreOffice", "program", "soffice.exe"))
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def ocr_available() -> str | None:
    """'vision' (macOS), 'tesseract' (if installed) or None."""
    if sys.platform == "darwin" and importlib.util.find_spec("Vision") is not None:
        return "vision"
    if shutil.which("tesseract"):
        return "tesseract"
    return None


def scanning_available() -> bool:
    """iPhone scanning uses Continuity Camera, which only exists on macOS."""
    return sys.platform == "darwin" and importlib.util.find_spec("AppKit") is not None


def unique_path(path: Path) -> Path:
    """``path`` or ``name 2.ext``, ``name 3.ext`` … so nothing is overwritten."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 100_000):
        cand = path.with_name(f"{stem} {i}{suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError("Could not find a free file name.")
