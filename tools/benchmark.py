r"""Reproducible performance measurements for docs/TEST_RESULTS.md.

    .venv/bin/python tools/benchmark.py [--app "release/macos/ENG Drawing Viewer.app"]
    .venv/bin/python tools/benchmark.py --app build/linux/AppDir          (or the .AppImage file)
    .venv\Scripts\python tools\benchmark.py --app "release\windows\ENG Drawing Viewer"

Measures (median of several runs where it makes sense):
  * packaged-app startup: window shown / first page rendered (smoke-test mode)
  * folder scan of 500 PDFs in 25 nested folders
  * background thumbnail throughput (disk-cache prefetch) for those 500 PDFs
  * time to first sharp content when opening an A1 / A0 sheet
  * time to finish all tiles after zooming to 800 %
  * main-thread blocking: original approach (whole-page render on the UI
    thread) vs this version (render pool)
  * resident memory of the UI process and render workers
"""
from __future__ import annotations

import argparse
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out else 0.0


def child_pids(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    return [int(x) for x in out]


def app_executable(app: Path) -> Path:
    """The program to start for a macOS .app, a Linux AppDir or AppImage, or a Windows app folder."""
    if app.suffix == ".app":
        return app / "Contents" / "MacOS" / "ENG Drawing Viewer"
    if (app / "AppRun").is_file():
        return app / "AppRun"
    if (app / "ENG Drawing Viewer.exe").is_file():
        return app / "ENG Drawing Viewer.exe"
    return app  # an AppImage file


def app_env() -> dict:
    env = dict(os.environ, ENG_VIEWER_SMOKE_TEST="1")
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        env.pop("QT_QPA_PLATFORM", None)  # the packaged app uses the real display, not offscreen
    return env


def bench_startup(app: Path, sample: Path, runs: int = 5) -> dict:
    exe = app_executable(app)
    shown, rendered = [], []
    for _ in range(runs):
        env = app_env()
        t0 = time.perf_counter()
        out = subprocess.run([str(exe), str(sample)], env=env, capture_output=True, text=True, timeout=120).stdout
        wall = time.perf_counter() - t0
        marks = {line.split("ms")[1].strip(): float(line.split()[1]) for line in out.splitlines() if line.startswith("[startup]")}
        # Marks are measured from Python start inside the bundle; add the bootloader/launch overhead
        # by comparing with the wall clock of the whole run minus the post-render shutdown (~0.2 s idle).
        shown.append(marks.get("window shown", float("nan")))
        rendered.append(marks.get("first page rendered", float("nan")))
    return {"window_shown_ms": statistics.median(shown), "first_page_ms": statistics.median(rendered), "runs": runs}


def make_big_folder(src: list[Path], root: Path, n_folders: int = 25, per_folder: int = 20) -> list[Path]:
    out = []
    k = 0
    for f in range(n_folders):
        d = root / f"Area {f // 5}" / f"Line {f}"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(per_folder):
            dst = d / f"Drawing {f}-{i + 1}.pdf"
            shutil.copyfile(src[k % len(src)], dst)
            k += 1
            out.append(dst)
    return out


def make_dense_drawing(path: Path) -> Path:
    """A1 sheet with 60k short vectors and 3k tiny labels (a heavy CAD export)."""
    import random

    import pymupdf

    random.seed(1)
    doc = pymupdf.open()
    page = doc.new_page(width=2384, height=1684)
    shape = page.new_shape()
    for _ in range(60000):
        x0, y0 = random.random() * 2384, random.random() * 1684
        shape.draw_line((x0, y0), (x0 + random.random() * 80, y0 + random.random() * 80))
    shape.finish(width=0.15, color=(0, 0, 0))
    shape.commit()
    for _ in range(3000):
        page.insert_text((random.random() * 2300, random.random() * 1650), "M12x1.5 DETAIL A-A 0.05", fontsize=3)
    doc.save(path)
    return path


def bench_app_memory(app: Path, folder: Path, pdf: Path) -> tuple[float, list[float]]:
    """Launch the real app on a folder, open a sheet, let it go idle, measure RSS."""
    exe = app_executable(app)
    env = app_env()
    env.pop("ENG_VIEWER_SMOKE_TEST")
    proc = subprocess.Popen([str(exe), str(pdf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        time.sleep(8)
        main = rss_mb(proc.pid)
        kids = [rss_mb(p) for p in child_pids(proc.pid)]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return main, kids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=str(ROOT / "release" / "macos" / "ENG Drawing Viewer.app"))
    args = ap.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from corpus import make_corpus

    work = Path(tempfile.mkdtemp(prefix="eng-bench-"))
    corpus = make_corpus(work / "corpus")
    results: list[tuple[str, str]] = []

    app = Path(args.app)
    dense = make_dense_drawing(work / "dense A1.pdf")
    if app.exists():
        s = bench_startup(app, corpus["a1"])
        results.append(("Packaged app: window shown", f"{s['window_shown_ms']:.0f} ms (median of {s['runs']})"))
        results.append(("Packaged app: first A1 page rendered", f"{s['first_page_ms']:.0f} ms"))

    import pymupdf
    from PySide6.QtCore import QElapsedTimer, QPointF, QTimer
    from PySide6.QtWidgets import QApplication

    from eng_viewer.core.render_service import RenderService
    from eng_viewer.core.scanning import scan_folder
    from eng_viewer.ui import thumbnails as thumbs_mod
    from eng_viewer.ui.page_view import PageRef, PageView
    from eng_viewer.ui.theme import ThemeManager

    qapp = QApplication(sys.argv)
    ThemeManager(qapp, "light").apply()

    big = work / "big"
    srcs = [corpus[k] for k in ("a1", "a1_2", "a1_10", "multi", "a4", "rotated", "unicode")]
    make_big_folder(srcs, big)
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        res = scan_folder(big)
        times.append(time.perf_counter() - t0)
    results.append(("Scan 500 PDFs in 25 nested folders", f"{statistics.median(times) * 1000:.0f} ms ({len(res.paths)} found)"))

    render = RenderService()
    render.warm_up(2)

    def spin_until(pred, timeout=300.0):
        t0 = time.perf_counter()
        while not pred():
            qapp.processEvents()
            time.sleep(0.002)
            if time.perf_counter() - t0 > timeout:
                raise TimeoutError
        return time.perf_counter() - t0

    cache = work / "cache"
    thumbs_mod.cache_dir = lambda: cache
    tp = thumbs_mod.ThumbnailProvider(render, disk_cache=True)
    paths = [str(p) for p in res.paths]
    tp.prefetch(paths, 144)
    elapsed = spin_until(lambda: not tp._pending)
    results.append(("Thumbnails for 500 PDFs (cold, background)", f"{elapsed:.1f} s ({500 / elapsed:.0f} files/s, {render._max_workers} workers)"))
    tp2 = thumbs_mod.ThumbnailProvider(render, disk_cache=True)
    t0 = time.perf_counter()
    for p in paths[:40]:
        tp2.get(p, 0, 144)
    spin_until(lambda: not tp2._pending)
    results.append(("40 visible thumbnails from disk cache (warm)", f"{(time.perf_counter() - t0) * 1000:.0f} ms"))

    # Opening sheets: time to first content and to all exact tiles.
    for key, label in (("a1", "A1"), ("a0", "A0")):
        path = str(corpus[key])
        pg = pymupdf.open(path)[0]
        view = PageView(render)
        view.resize(1280, 800)
        view.show()
        ready = []
        view.pageReady.connect(lambda: ready.append(time.perf_counter()))
        t0 = time.perf_counter()
        view.set_page(PageRef(path, 0, pg.rect.width, pg.rect.height, PageRef.stamp_for(path)))
        spin_until(lambda: ready)
        first = ready[0] - t0
        spin_until(lambda: not view._pending and not view._tile_timer.isActive())
        allt = time.perf_counter() - t0
        results.append((f"Open {label} sheet: first content / all sharp tiles", f"{first * 1000:.0f} ms / {allt * 1000:.0f} ms"))
        t0 = time.perf_counter()
        view.set_zoom(view.ACTUAL_SIZE * 8, QPointF(640, 400))
        spin_until(lambda: not view._pending and not view._tile_timer.isActive())
        results.append((f"Zoom {label} to 800 %: all visible tiles sharp", f"{(time.perf_counter() - t0) * 1000:.0f} ms"))
        view.release()
        view.close()

    # Main-thread blocking: original approach vs render pool, on a dense sheet.
    path = str(dense)
    page = pymupdf.open(path)[0]
    z = (1280 - 24) / page.rect.width
    t0 = time.perf_counter()
    page.get_pixmap(matrix=pymupdf.Matrix(z * 2, z * 2), alpha=False)  # what a Retina-correct full render would cost
    orig_block = time.perf_counter() - t0
    gaps = []
    timer_clock = QElapsedTimer()
    timer_clock.start()
    last = [timer_clock.elapsed()]

    def tick():
        now = timer_clock.elapsed()
        gaps.append(now - last[0])
        last[0] = now

    t = QTimer()
    t.timeout.connect(tick)
    t.start(5)
    done = []
    render.submit("render", {"path": path, "page": 0, "scale": z * 2}, done.append, dedupe=False)
    spin_until(lambda: done)
    t.stop()
    results.append(("Dense A1 sheet (60k vectors): UI frozen, original approach", f"{orig_block * 1000:.0f} ms"))
    results.append(("Dense A1 sheet: longest UI pause, render pool", f"{max(gaps[1:] or [0])} ms"))
    render.shutdown()

    if app.exists():
        main_rss, kids = bench_app_memory(app, corpus["root"], corpus["a1"])
        kids = sorted(kids, reverse=True)
        results.append(("Packaged app memory, idle with an A1 sheet open: UI process", f"{main_rss:.0f} MB"))
        results.append(("Packaged app memory: helper processes", ", ".join(f"{k:.0f}" for k in kids) + " MB"))

    print("| Measurement | Result |\n|---|---|")
    for k, v in results:
        print(f"| {k} | {v} |")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
