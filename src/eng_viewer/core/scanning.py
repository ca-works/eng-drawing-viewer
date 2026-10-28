"""Folder scanning for PDF drawings.

Runs off the UI thread. Uses ``os.scandir`` (far fewer stat calls than
``Path.rglob``), tolerates unreadable folders, ignores hidden folders and the
``._name.pdf`` AppleDouble files macOS leaves on network/FAT drives, and
guards against symlink loops.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .documents import is_document_name
from .naturalsort import sort_paths


@dataclass
class ScanResult:
    root: Path
    paths: list[Path] = field(default_factory=list)
    folders_scanned: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def is_pdf_name(name: str) -> bool:
    return name.lower().endswith(".pdf") and not name.startswith("._")


def scan_folder(
    root: Path,
    recursive: bool = True,
    should_cancel: Callable[[], bool] | None = None,
    include_images: bool = False,
) -> ScanResult:
    root = Path(root)
    result = ScanResult(root=root)
    found: list[Path] = []
    seen_dirs: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        if should_cancel is not None and should_cancel():
            result.cancelled = True
            break
        folder = stack.pop()
        try:
            st = folder.stat()
            ident = (st.st_dev, st.st_ino)
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
                    if recursive and not name.startswith("."):
                        stack.append(Path(entry.path))
                elif is_document_name(name, include_images) and entry.is_file(follow_symlinks=True):
                    found.append(Path(entry.path))
            except OSError:
                continue
    result.paths = sort_paths(found)
    return result
