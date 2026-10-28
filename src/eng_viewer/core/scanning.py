"""Folder scanning for PDF drawings.

Runs off the UI thread. Uses ``os.scandir`` (far fewer stat calls than
``Path.rglob``), tolerates unreadable folders, ignores hidden folders (dot
folders, and folders marked hidden or system on Windows, such as
``$RECYCLE.BIN``) and the ``._name.pdf`` AppleDouble files macOS leaves on
network/FAT drives, and guards against symlink and junction loops, also on
drives that report no file IDs (inode 0: FAT, exFAT, some network shares).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .documents import is_document_name
from .naturalsort import sort_paths

# FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
_HIDDEN_ATTRIBUTES = 0x2 | 0x4


@dataclass
class ScanResult:
    root: Path
    paths: list[Path] = field(default_factory=list)
    folders_scanned: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def is_pdf_name(name: str) -> bool:
    return name.lower().endswith(".pdf") and not name.startswith("._")


def is_hidden_dir(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if os.name == "nt":
        try:
            # On Windows, scandir already has the attributes: no extra disk access.
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & _HIDDEN_ATTRIBUTES)
        except OSError:
            return False
    return False


def _folder_id(folder: Path, st: os.stat_result):
    """Identity for loop detection. Drives without file IDs report inode 0 for everything; use the
    resolved path there, or every folder after the first would look already visited."""
    if st.st_ino:
        return (st.st_dev, st.st_ino)
    try:
        return ("path", os.path.normcase(os.path.realpath(folder)))
    except OSError:
        return ("path", os.path.normcase(os.path.abspath(folder)))


def scan_folder(
    root: Path,
    recursive: bool = True,
    should_cancel: Callable[[], bool] | None = None,
    include_images: bool = False,
) -> ScanResult:
    root = Path(root)
    result = ScanResult(root=root)
    found: list[Path] = []
    seen_dirs: set[tuple] = set()
    stack = [root]
    while stack:
        if should_cancel is not None and should_cancel():
            result.cancelled = True
            break
        folder = stack.pop()
        try:
            st = folder.stat()
            ident = _folder_id(folder, st)
            if ident in seen_dirs:
                continue
            seen_dirs.add(ident)
            entries = list(os.scandir(folder))
        except OSError as exc:
            result.errors.append(f"{folder}: {exc.strerror or exc}")
            continue
        result.folders_scanned += 1
        for entry in entries:
            name = entry.name
            try:
                if entry.is_dir(follow_symlinks=True):
                    if recursive and not is_hidden_dir(entry):
                        stack.append(Path(entry.path))
                elif is_document_name(name, include_images) and entry.is_file(follow_symlinks=True):
                    found.append(Path(entry.path))
            except OSError:
                continue
    result.paths = sort_paths(found)
    return result
