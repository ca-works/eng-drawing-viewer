"""Natural ("human") ordering for drawing filenames.

``Sheet 2.pdf`` sorts before ``Sheet 10.pdf``, case is ignored, and the
decomposed Unicode that macOS uses for filenames compares equal to the
composed form typed on Windows.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_DIGITS = re.compile(r"(\d+)")


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def natural_key(text: str) -> tuple:
    """Key that orders embedded numbers numerically.

    ``re.split`` with a capturing group always alternates text/number/text, so
    every position holds the same type across keys and tuples never compare an
    int against a str. Leading zeros only break ties at the very end, so
    ``Sheet 01b`` still sorts before ``Sheet 1c``.
    """
    norm = _normalise(text)
    parts = _DIGITS.split(norm)
    key: list = []
    widths: list[int] = []
    for i, part in enumerate(parts):
        if i % 2:
            key.append(int(part))
            widths.append(len(part))
        else:
            key.append(part)
    return (tuple(key), tuple(widths), norm)


def path_sort_key(path: Path) -> tuple:
    """Order by filename first, then by the containing folder (flat view)."""
    return natural_key(path.name), natural_key(str(path.parent)), str(path)


def sort_paths(paths) -> list[Path]:
    return sorted(set(paths), key=path_sort_key)
