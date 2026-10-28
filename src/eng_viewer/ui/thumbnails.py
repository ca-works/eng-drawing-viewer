"""Thumbnails for the file list, page grid and export queue.

* Visible thumbnails are requested on demand (from item delegates).
* Background prefetch only renders into the on-disk cache (workers write the
  PNG; no pixels travel back), so warming hundreds of PDFs costs no memory.
* Page count and paper size come back with every render and are remembered.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections import OrderedDict
from pathlib import Path

import shiboken6
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage, QPixmap

from ..core.config import cache_dir
from ..core.render_service import PRI_PREFETCH, PRI_VISIBLE_THUMB, RenderResult, RenderService

BUCKETS = (96, 160, 256, 400)  # device-pixel heights
ASPECT = 1.42  # thumbnail box width/height (landscape drawing sheets)
CACHE_VERSION = "v2"
MEM_BUDGET = 72 * 1024 * 1024


def bucket_for(device_height: float) -> int:
    for b in BUCKETS:
        if b >= device_height:
            return b
    return BUCKETS[-1]


class _Bridge(QObject):
    loaded = Signal(object, object)  # key, QImage | None


class _DiskLoad(QRunnable):
    def __init__(self, key, path: str, bridge: _Bridge):
        super().__init__()
        self.key, self.path, self.bridge = key, path, bridge
        self.setAutoDelete(True)

    def run(self):
        img = QImage(self.path)
        self.bridge.loaded.emit(self.key, None if img.isNull() else img)


class ThumbnailProvider(QObject):
    thumbReady = Signal(object)  # (path, page)
    infoReady = Signal(str)  # path

    def __init__(self, render: RenderService, disk_cache: bool = True, thin_lines: bool = True, parent=None):
        super().__init__(parent)
        self.render = render
        self.disk_enabled = disk_cache
        self.thin_lines = thin_lines
        self._mem: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._mem_used = 0
        self._pending: set[tuple] = set()
        self._failed: set[tuple] = set()  # (path, page) that could not be rendered
        self._stamps: dict[str, tuple] = {}
        self._info: dict[str, dict] = {}
        self._dir = cache_dir() / "thumbs"
        self._info_path = cache_dir() / "page-info.json"
        self._persisted_info: dict[str, dict] = {}
        self._info_dirty = False
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._bridge = _Bridge(self)
        self._bridge.loaded.connect(self._on_disk_loaded)
        self._load_info()

    # -------------------------------------------------------------- helpers
    def _stamp(self, path: str) -> tuple:
        st = self._stamps.get(path)
        if st is None:
            try:
                s = os.stat(path)
                st = (s.st_mtime_ns, s.st_size)
            except OSError:
                st = (0, 0)
            self._stamps[path] = st
        return st

    def forget_stamps(self):
        """Call after a rescan so edited files get fresh thumbnails."""
        self._stamps.clear()
        self._failed.clear()

    def _id(self, path: str) -> str:
        m, s = self._stamp(path)
        return hashlib.sha256(f"{path}|{m}|{s}".encode("utf-8", "surrogatepass")).hexdigest()

    def _disk_path(self, path: str, page: int, bucket: int) -> Path:
        h = hashlib.sha256(f"{self._id(path)}|{page}|{bucket}|{int(self.thin_lines)}|{CACHE_VERSION}".encode()).hexdigest()
        return self._dir / h[:2] / f"{h[2:26]}.png"

    def _load_info(self):
        try:
            data = json.loads(self._info_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._persisted_info = data
        except (OSError, ValueError):
            pass

    def save_info(self):
        if not self._info_dirty or not self.disk_enabled:
            return
        try:
            self._info_path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the file bounded.
            items = list(self._persisted_info.items())[-5000:]
            tmp = self._info_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(dict(items)), encoding="utf-8")
            os.replace(tmp, self._info_path)
            self._info_dirty = False
        except OSError:
            pass

    # ----------------------------------------------------------------- info
    def failed(self, path: str, page: int = 0) -> bool:
        return (path, page) in self._failed

    def info(self, path: str) -> dict | None:
        info = self._info.get(path)
        if info is None and self.disk_enabled:
            info = self._persisted_info.get(self._id(path))
            if info is not None:
                self._info[path] = info
        return info

    def _record_info(self, path: str, header: dict | None, error: str = ""):
        if error:
            info = {"error": error}
        else:
            info = {"pages": header.get("page_count", 0), "w": round(header.get("page_w", 0), 2), "h": round(header.get("page_h", 0), 2)}
            if header.get("kind") == "image":
                info["pixels"] = list(header.get("pixels") or [])
        if self._info.get(path) != info:
            self._info[path] = info
            if not error:
                self._persisted_info[self._id(path)] = info
                self._info_dirty = True
            self.infoReady.emit(path)

    # ----------------------------------------------------------- thumbnails
    def get(self, path: str, page: int, device_height: float, visible: bool = True) -> QPixmap | None:
        """Best available pixmap now; requests a sharper one if needed."""
        bucket = bucket_for(device_height)
        key = (path, page, bucket)
        pm = self._mem.get(key)
        if pm is not None:
            self._mem.move_to_end(key)
            return pm
        best = None
        for b in reversed(BUCKETS):
            if b == bucket:
                continue
            alt = self._mem.get((path, page, b))
            if alt is not None and (best is None or b > bucket):
                best = alt
                if b > bucket:
                    break
        if (path, page) in self._failed:
            return best
        if key not in self._pending:
            self._request(key, PRI_VISIBLE_THUMB if visible else PRI_PREFETCH)
        return best

    def _request(self, key, priority: int):
        self._pending.add(key)
        path, page, bucket = key
        disk = self._disk_path(path, page, bucket) if self.disk_enabled else None
        if disk is not None and disk.exists():
            self._pool.start(_DiskLoad(key, str(disk), self._bridge))
            return
        self._submit_render(key, priority, disk, return_pixels=True)

    def _submit_render(self, key, priority: int, disk: Path | None, return_pixels: bool):
        path, page, bucket = key
        args = {
            "path": path,
            "page": page,
            "fit": (round(bucket * ASPECT), bucket),
            "min_line": 1.0 if self.thin_lines else 0.0,
        }
        if disk is not None:
            args["cache_png"] = str(disk)
        if not return_pixels:
            args["no_return"] = True
        self.render.submit("render", args, lambda res, key=key, rp=return_pixels: self._on_rendered(key, res, rp),
                           priority=priority, group=("thumb", path))

    def _on_rendered(self, key, res: RenderResult, returned: bool):
        if not shiboken6.isValid(self):
            return  # result arrived after the window closed
        path, page, _bucket = key
        self._pending.discard(key)
        if not res.ok:
            self._failed.add((path, page))
            if page == 0:
                self._record_info(path, None, res.error)
            self.thumbReady.emit((path, page))
            return
        if page == 0:
            self._record_info(path, res.header)
        if returned and res.image is not None:
            self._store(key, QPixmap.fromImage(res.image))
            self.thumbReady.emit((path, page))

    @Slot(object, object)
    def _on_disk_loaded(self, key, img):
        if not shiboken6.isValid(self):
            return
        if img is None:
            # Corrupt cache entry: render it instead.
            path, page, bucket = key
            self._submit_render(key, PRI_VISIBLE_THUMB, self._disk_path(path, page, bucket), True)
            return
        self._pending.discard(key)
        self._store(key, QPixmap.fromImage(img))
        self.thumbReady.emit((key[0], key[1]))
        if key[1] == 0 and self.info(key[0]) is None:
            # Page info was not persisted (e.g. cache from older session): fetch cheaply.
            self.render.submit("info", {"path": key[0], "size_limit": 1},
                               lambda res, p=key[0]: self._on_info(p, res), priority=PRI_PREFETCH)

    def _on_info(self, path, res: RenderResult):
        if not shiboken6.isValid(self):
            return
        if res.ok:
            sizes = res.header.get("sizes") or [(0, 0)]
            self._record_info(path, {"page_count": res.header.get("page_count", 0), "page_w": sizes[0][0], "page_h": sizes[0][1]})
        else:
            self._record_info(path, None, res.error)

    def _store(self, key, pm: QPixmap):
        old = self._mem.pop(key, None)
        if old is not None:
            self._mem_used -= old.width() * old.height() * 4
        self._mem[key] = pm
        self._mem_used += pm.width() * pm.height() * 4
        while self._mem_used > MEM_BUDGET and len(self._mem) > 1:
            _k, ev = self._mem.popitem(last=False)
            self._mem_used -= ev.width() * ev.height() * 4

    def prefetch(self, paths: list[str], device_height: float):
        """Warm the disk cache (and page info) for files not yet on screen."""
        bucket = bucket_for(device_height)
        for path in paths:
            key = (path, 0, bucket)
            if key in self._pending or key in self._mem:
                continue
            if self.disk_enabled:
                disk = self._disk_path(path, 0, bucket)
                if disk.exists() and self.info(path) is not None:
                    continue
                self._pending.add(key)
                self._submit_render(key, PRI_PREFETCH, disk, return_pixels=False)
            elif self.info(path) is None:
                self.render.submit("info", {"path": path, "size_limit": 1},
                                   lambda res, p=path: self._on_info(p, res), priority=PRI_PREFETCH)

    def cancel_prefetch(self):
        for key in list(self._pending):
            self.render.cancel_group(("thumb", key[0]))
        self._pending.clear()

    def set_thin_lines(self, on: bool):
        if on != self.thin_lines:
            self.thin_lines = on
            self._mem.clear()
            self._mem_used = 0
            self._pending.clear()

    # ------------------------------------------------------------ disk cache
    def cache_size_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self._dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def clear_disk_cache(self):
        shutil.rmtree(self._dir, ignore_errors=True)
        try:
            self._info_path.unlink()
        except OSError:
            pass
        self._persisted_info.clear()

    def prune_async(self, limit_bytes: int = 400 * 1024 * 1024):
        def prune():
            entries = []
            for root, _dirs, files in os.walk(self._dir):
                for f in files:
                    p = os.path.join(root, f)
                    try:
                        st = os.stat(p)
                        entries.append((st.st_mtime, st.st_size, p))
                    except OSError:
                        pass
            total = sum(e[1] for e in entries)
            if total <= limit_bytes:
                return
            entries.sort()
            for _m, size, p in entries:
                if total <= limit_bytes * 0.8:
                    break
                try:
                    os.remove(p)
                    total -= size
                except OSError:
                    pass

        threading.Thread(target=prune, name="thumb-prune", daemon=True).start()
