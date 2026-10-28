"""Local JSON settings in the platform standard per-user location."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

APP_NAME = "ENG Drawing Viewer"

QUALITY_DPI = {"standard": 150, "high": 220, "max": 300}
QUALITY_LABELS = {"standard": "Standard (150 DPI)", "high": "High (220 DPI)", "max": "Max (300 DPI)"}

DEFAULTS: dict[str, Any] = {
    "last_folder": None,
    "recent_folders": [],
    "theme": "system",  # system | light | dark
    "recursive_scan": True,
    "quality": "high",
    "filename_placement": "title",  # none | title | subtitle
    "file_view_mode": "tree",  # tree | flat
    "file_thumb_height": 72,
    "enhance_thin_lines": True,
    "thumbnail_disk_cache": True,
    "window_geometry": None,
    "main_splitter": None,
    "main_splitter2": None,
    "viewer_splitter": None,
    "inspector_splitter": None,
    "library_visible": True,
    "inspector_visible": True,
    "mark": {},
    "mark_geometry": None,
    "export_dir": None,
    "export_mode": "new",  # new | existing | powerpoint (Windows COM)
    "existing_mode": "insert",  # insert | reuse
    "user_words": [],
    "include_images": True,  # PNG, JPEG, TIFF, HEIC... appear in the library next to PDFs
    "scans_folder": None,  # iPhone scans; default ~/Documents/ENG Drawing Viewer Scans
}


def config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def cache_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP_NAME / "Cache"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / APP_NAME


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "settings.json")
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        data: dict[str, Any] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._data:
            return self._data[key]
        if default is not None:
            return default
        value = DEFAULTS.get(key)
        # Hand out copies of mutable defaults so callers cannot alter DEFAULTS.
        return json.loads(json.dumps(value)) if isinstance(value, (dict, list)) else value

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def save(self) -> bool:
        """Write atomically so a crash never leaves a truncated settings file."""
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=1, default=str)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False
        finally:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    def add_recent_folder(self, folder: str, limit: int = 10) -> None:
        recent = [f for f in self.get("recent_folders") if f != folder]
        recent.insert(0, folder)
        self.set("recent_folders", recent[:limit])
