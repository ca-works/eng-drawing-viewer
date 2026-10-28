"""Prioritised, cancellable PDF rendering on a small pool of worker processes.

Threading model
---------------
* The UI thread submits jobs and receives results through a queued Qt signal.
* One dispatcher thread hands jobs to idle workers and reads their replies.
  It only builds ``QImage`` objects (safe off the GUI thread) and never touches
  widgets or ``QPixmap``.
* Worker processes (``render_worker.py``) do all PyMuPDF work.

Worker 0 is reserved for interactive work (page tiles, page geometry) so a
backlog of thumbnails can never delay the page the user is looking at.
"""
from __future__ import annotations

import heapq
import itertools
import multiprocessing as mp
import os
import threading
import time
from dataclasses import dataclass, field
from multiprocessing.connection import wait as mp_wait
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

from . import render_worker

# Priorities: lower runs first.
PRI_INTERACTIVE = 0  # visible tiles of the current page, page geometry
PRI_PREVIEW = 1  # whole-page preview of the current page
PRI_EXPORT = 2  # clipboard / PowerPoint / MARK export renders
PRI_VISIBLE_THUMB = 3  # thumbnails currently on screen
PRI_PREFETCH = 5  # neighbouring pages, off-screen thumbnails

STUCK_SECONDS = 25.0
MAX_CRASHES_PER_FILE = 2


@dataclass
class RenderResult:
    ok: bool
    header: dict = field(default_factory=dict)
    image: QImage | None = None
    error: str = ""


@dataclass(eq=False)
class _Job:
    job_id: int
    kind: str
    args: dict
    priority: int
    key: tuple | None
    groups: set = field(default_factory=set)  # requesters still interested
    callbacks: list = field(default_factory=list)  # (group, callback)
    cancelled: bool = False
    image_format: str = "rgb32"
    started: float = 0.0


@dataclass(eq=False)
class _Worker:
    process: Any
    conn: Any
    interactive_only: bool
    job: _Job | None = None
    idle_since: float = field(default_factory=time.monotonic)


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def default_worker_count() -> int:
    cpus = os.cpu_count() or 4
    return max(2, min(4, cpus // 2))


class RenderService(QObject):
    """Owns the worker pool. Create once, on the GUI thread."""

    _delivered = Signal(object, object)  # (_Job, RenderResult)
    activityChanged = Signal(int)  # number of queued + running jobs

    def __init__(self, max_workers: int | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._ctx = mp.get_context("spawn")
        self._max_workers = max(1, max_workers or default_worker_count())
        self._lock = threading.Lock()
        self._heap: list[tuple[int, int, _Job]] = []
        self._pending_by_key: dict[tuple, _Job] = {}
        self._jobs: dict[int, _Job] = {}
        self._workers: list[_Worker] = []
        self._seq = itertools.count(1)
        self._crashes: dict[str, int] = {}
        self._wake_r, self._wake_w = mp.Pipe(duplex=False)
        self._running = True
        self._delivered.connect(self._on_delivered)
        self._thread = threading.Thread(target=self._dispatch_loop, name="render-dispatch", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ API
    def submit(
        self,
        kind: str,
        args: dict,
        callback: Callable[[RenderResult], None] | None,
        priority: int = PRI_INTERACTIVE,
        group: Any = None,
        image_format: str = "rgb32",
        dedupe: bool = True,
    ) -> int:
        """Queue a job. ``callback`` runs on the GUI thread."""
        key = (kind, _freeze(args), image_format) if dedupe else None
        with self._lock:
            if key is not None:
                existing = self._pending_by_key.get(key)
                if existing is not None and not existing.cancelled:
                    existing.groups.add(group)
                    if callback is not None:
                        existing.callbacks.append((group, callback))
                    if priority < existing.priority and existing.started == 0.0:
                        existing.priority = priority
                        heapq.heappush(self._heap, (priority, next(self._seq), existing))
                    return existing.job_id
            job = _Job(next(self._seq), kind, dict(args), priority, key, {group}, [(group, callback)] if callback else [],
                       image_format=image_format)
            self._jobs[job.job_id] = job
            if key is not None:
                self._pending_by_key[key] = job
            heapq.heappush(self._heap, (priority, job.job_id, job))
        self._wake()
        self._emit_activity()
        return job.job_id

    _ALL = object()

    def cancel(self, job_id: int, group: Any = _ALL) -> None:
        """Withdraw ``group``'s interest in a job (or cancel it outright).

        Identical requests from different views share one job, so a job is
        only dropped once no requester wants its result any more.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if group is RenderService._ALL:
                self._cancel_locked(job)
            else:
                self._withdraw_locked(job, group)

    def cancel_group(self, group: Any) -> None:
        with self._lock:
            for job in list(self._jobs.values()):
                if group in job.groups:
                    self._withdraw_locked(job, group)

    def _withdraw_locked(self, job: _Job, group: Any):
        job.groups.discard(group)
        job.callbacks = [(g, c) for g, c in job.callbacks if g != group]
        if not job.groups:
            self._cancel_locked(job)

    def reprioritize_group(self, group: Any, priority: int) -> None:
        with self._lock:
            for job in self._jobs.values():
                if group in job.groups and job.started == 0.0 and not job.cancelled and priority < job.priority:
                    job.priority = priority
                    heapq.heappush(self._heap, (priority, next(self._seq), job))
        self._wake()

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if not j.cancelled)

    def warm_up(self, count: int = 1) -> None:
        """Start workers ahead of need (call after the window is shown)."""
        with self._lock:
            while len(self._workers) < min(count, self._max_workers):
                self._spawn_locked()

    def shutdown(self) -> None:
        self._running = False
        self._wake()
        self._thread.join(timeout=2.0)
        with self._lock:
            workers, self._workers = self._workers, []
        for w in workers:
            try:
                w.conn.send(None)
            except Exception:
                pass
        deadline = time.monotonic() + 1.5
        for w in workers:
            w.process.join(timeout=max(0.0, deadline - time.monotonic()))
            if w.process.is_alive():
                w.process.terminate()

    # ------------------------------------------------------------ internals
    def _emit_activity(self):
        self.activityChanged.emit(self.pending_count())

    def _wake(self):
        try:
            self._wake_w.send_bytes(b"!")
        except Exception:
            pass

    def _cancel_locked(self, job: _Job):
        job.cancelled = True
        job.callbacks.clear()
        if job.key is not None and self._pending_by_key.get(job.key) is job:
            del self._pending_by_key[job.key]
        if job.started == 0.0:
            self._jobs.pop(job.job_id, None)

    def _spawn_locked(self) -> _Worker:
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(target=render_worker.worker_main, args=(child_conn,), daemon=True, name="eng-render")
        proc.start()
        child_conn.close()
        worker = _Worker(proc, parent_conn, interactive_only=not self._workers and self._max_workers > 1)
        self._workers.append(worker)
        return worker

    def _next_job_locked(self, interactive_only: bool) -> _Job | None:
        while self._heap:
            pri, seq, job = heapq.heappop(self._heap)
            if job.cancelled or job.started or pri != job.priority:
                continue  # stale heap entry
            if interactive_only and pri > PRI_PREVIEW:
                # The heap is ordered, so nothing interactive remains.
                heapq.heappush(self._heap, (pri, seq, job))
                return None
            return job
        return None

    def _has_waiting_locked(self, interactive_only: bool) -> bool:
        for pri, _seq, job in self._heap:
            if not job.cancelled and not job.started and pri == job.priority:
                if not interactive_only or pri <= PRI_PREVIEW:
                    return True
        return False

    def _assign_locked(self):
        now = time.monotonic()
        for w in self._workers:
            if w.job is None:
                job = self._next_job_locked(w.interactive_only)
                if job is not None:
                    self._start_job_locked(w, job, now)
        # Grow the pool while general work is waiting.
        while len(self._workers) < self._max_workers and self._has_waiting_locked(False):
            w = self._spawn_locked()
            job = self._next_job_locked(w.interactive_only)
            if job is None:
                break
            self._start_job_locked(w, job, now)

    def _start_job_locked(self, w: _Worker, job: _Job, now: float):
        path = job.args.get("path", "")
        if self._crashes.get(path, 0) >= MAX_CRASHES_PER_FILE:
            job.started = now
            self._finish_locked(job, RenderResult(False, error="Skipped: this PDF repeatedly crashed the renderer"))
            return
        job.started = now
        if job.key is not None and self._pending_by_key.get(job.key) is job:
            del self._pending_by_key[job.key]
        w.job = job
        try:
            w.conn.send((job.job_id, job.kind, job.args))
        except Exception:
            self._worker_died_locked(w)

    def _finish_locked(self, job: _Job, result: RenderResult):
        self._jobs.pop(job.job_id, None)
        if not job.cancelled:
            self._delivered.emit(job, result)

    def _worker_died_locked(self, w: _Worker):
        if w in self._workers:
            self._workers.remove(w)
        try:
            w.conn.close()
        except Exception:
            pass
        if w.process.is_alive():
            w.process.terminate()
        job, w.job = w.job, None
        if job is not None:
            path = job.args.get("path", "")
            self._crashes[path] = self._crashes.get(path, 0) + 1
            self._finish_locked(job, RenderResult(False, error="Renderer stopped while reading this PDF (possibly damaged)"))
        # Keep the interactive slot filled.
        if self._workers and not any(x.interactive_only for x in self._workers) and self._max_workers > 1:
            self._workers[0].interactive_only = True

    def _read_result(self, w: _Worker):
        job = w.job
        job_id, ok, header = w.conn.recv()
        payload = w.conn.recv_bytes() if ok and header.get("has_payload") else None
        image = None
        if payload is not None:
            fmt_rgb = QImage.Format.Format_RGB888
            raw = QImage(payload, header["w"], header["h"], header["stride"], fmt_rgb)
            if job is not None and job.image_format == "rgb888":
                image = raw.copy()
            else:
                image = raw.convertToFormat(QImage.Format.Format_RGB32)
            del raw
        result = RenderResult(ok, header, image, "" if ok else header.get("error", "Unknown error"))
        with self._lock:
            w.job = None
            w.idle_since = time.monotonic()
            if job is not None and job.job_id == job_id:
                self._finish_locked(job, result)

    def _housekeeping_locked(self):
        now = time.monotonic()
        for w in list(self._workers):
            if not w.process.is_alive():
                self._worker_died_locked(w)
            elif w.job is not None and w.job.cancelled and now - w.job.started > STUCK_SECONDS:
                # A discarded render is still running: recycle the worker.
                w.job = None
                self._worker_died_locked(w)
        # Let surplus workers go after a quiet period to keep memory low.
        idle = [w for w in self._workers if w.job is None and not w.interactive_only]
        for w in idle:
            if len(self._workers) > 2 and now - w.idle_since > 45.0:
                self._workers.remove(w)
                try:
                    w.conn.send(None)
                except Exception:
                    pass

    def _dispatch_loop(self):
        last_housekeeping = time.monotonic()
        while self._running:
            with self._lock:
                self._assign_locked()
                busy = {w.conn: w for w in self._workers if w.job is not None}
            try:
                ready = mp_wait([self._wake_r, *busy.keys()], timeout=0.5)
            except OSError:
                ready = []
            for conn in ready:
                if conn is self._wake_r:
                    try:
                        while self._wake_r.poll():
                            self._wake_r.recv_bytes()
                    except Exception:
                        pass
                    continue
                w = busy.get(conn)
                if w is None:
                    continue
                try:
                    self._read_result(w)
                except (EOFError, OSError, ValueError):
                    with self._lock:
                        self._worker_died_locked(w)
            if time.monotonic() - last_housekeeping > 1.0:
                last_housekeeping = time.monotonic()
                with self._lock:
                    self._housekeeping_locked()

    @Slot(object, object)
    def _on_delivered(self, job: _Job, result: RenderResult):
        if job.cancelled:
            return
        for _group, cb in list(job.callbacks):
            try:
                cb(result)
            except Exception:  # a broken consumer must not stop delivery to others
                import traceback

                traceback.print_exc()
        self._emit_activity()
