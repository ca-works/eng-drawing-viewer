"""Render pool: correctness, sharpness, robustness and responsiveness."""
import io
import time

import pymupdf
import pytest
from PIL import Image
from PySide6.QtCore import QElapsedTimer, QPointF, QTimer
from PySide6.QtGui import QImage

from conftest import run_job
from eng_viewer.core.exporting import export_scale, reinforced_min_line
from eng_viewer.core.render_service import PRI_PREFETCH


def _qimage_rgb(img: QImage) -> bytes:
    """Tightly packed RGB bytes (rows may be padded in the QImage)."""
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    bpl, row = img.bytesPerLine(), img.width() * 3
    data = bytes(img.constBits())
    return b"".join(data[y * bpl: y * bpl + row] for y in range(img.height()))


def test_info_reports_pages_and_rotated_size(qtbot, render, corpus):
    res = run_job(qtbot, render, "info", {"path": str(corpus["multi"])})
    assert res.ok and res.header["page_count"] == 5
    w, h = res.header["sizes"][0]
    assert w > h  # A3 landscape
    res = run_job(qtbot, render, "info", {"path": str(corpus["rotated"])})
    w, h = res.header["sizes"][0]
    assert h > w  # /Rotate 90 swaps the displayed size


@pytest.mark.parametrize("name,expected", [
    ("empty.pdf", "Empty file"),
    ("not really a pdf.pdf", "Damaged or not a valid PDF"),
    ("truncated.pdf", "Damaged or not a valid PDF"),
    ("password protected.pdf", "Password protected"),
])
def test_problem_files_fail_gracefully(qtbot, render, corpus, name, expected):
    res = run_job(qtbot, render, "info", {"path": str(corpus["root"] / "Problems" / name)})
    assert not res.ok
    assert expected in res.error


def test_missing_file(qtbot, render, tmp_path):
    res = run_job(qtbot, render, "info", {"path": str(tmp_path / "gone.pdf")})
    assert not res.ok and "not found" in res.error


def test_tile_equals_full_render(qtbot, render, corpus):
    """A clipped tile is the same pixels as the matching region of a full render."""
    path = str(corpus["a1"])
    s = 1.6
    full = run_job(qtbot, render, "render", {"path": path, "page": 0, "scale": s}).image
    clip = (512 / s, 512 / s, 1024 / s, 1024 / s)
    tile = run_job(qtbot, render, "render", {"path": path, "page": 0, "scale": s, "clip": clip}).image
    assert tile.width() == 512 and tile.height() == 512
    region = full.copy(512, 512, 512, 512)
    a, b = _qimage_rgb(region), _qimage_rgb(tile)
    diff = sum(1 for i in range(0, len(a), 7) if abs(a[i] - b[i]) > 40)
    assert diff / (len(a) / 7) < 0.002  # only anti-aliasing noise may differ


def test_thin_line_enhancement_darkens_hairlines(qtbot, render, corpus):
    path = str(corpus["a1"])
    doc = pymupdf.open(path)
    w, h = doc[0].rect.width, doc[0].rect.height
    # The construction grid of 0.1 pt lines sits right of centre.
    clip = (w * 0.38 + 450 * w / 2384, h * 0.45 - 100, w * 0.38 + 870 * w / 2384, h * 0.45 + 100)
    plain = run_job(qtbot, render, "render", {"path": path, "page": 0, "scale": 0.5, "clip": clip, "min_line": 0}).image
    thick = run_job(qtbot, render, "render", {"path": path, "page": 0, "scale": 0.5, "clip": clip, "min_line": 1.0}).image
    darkest = lambda img: min(_qimage_rgb(img))
    assert darkest(thick) < darkest(plain) - 40


def test_export_png_has_dpi_and_reinforced_lines(qtbot, render, corpus, tmp_path):
    out = tmp_path / "page.png"
    res = run_job(qtbot, render, "render", {"path": str(corpus["a4"]), "page": 0, "scale": 220 / 72, "reinforce": True,
                                            "out_png": str(out), "dpi": 220})
    assert res.ok and out.exists()
    with Image.open(out) as im:
        dpi = im.info.get("dpi")
        assert dpi and round(dpi[0]) == 220
        assert im.size == (round(595.3 * 220 / 72), round(841.9 * 220 / 72)) or abs(im.size[0] - 595.3 * 220 / 72) <= 1


def test_export_scale_caps_huge_pages():
    s = export_scale(300, 3370.4, 2383.9)  # A0 at 300 DPI would be ~140 MP
    assert 3370.4 * 2383.9 * s * s <= 90_000_001
    assert reinforced_min_line(220 / 72) == pytest.approx(0.9 * 220 / 72)


def test_cancelled_jobs_never_call_back(qtbot, render, corpus):
    called = []
    ids = [render.submit("render", {"path": str(corpus["a0"]), "page": 0, "scale": 0.2 + i * 0.01}, called.append,
                         priority=PRI_PREFETCH, group=("test-cancel",)) for i in range(20)]
    render.cancel_group(("test-cancel",))
    qtbot.wait(1500)
    # Jobs already running may finish but are not delivered.
    assert called == []
    assert len(ids) == 20


def test_shared_job_survives_one_requester_cancelling(qtbot, render, corpus):
    got = {}
    args = {"path": str(corpus["a1_2"]), "page": 0, "scale": 0.123}
    render.submit("render", args, lambda r: got.setdefault("a", r), group="viewA")
    render.submit("render", args, lambda r: got.setdefault("b", r), group="viewB")
    render.cancel_group("viewA")
    qtbot.waitUntil(lambda: "b" in got, timeout=20000)
    assert "a" not in got and got["b"].ok


def test_worker_crash_is_contained(qtbot, render, corpus):
    """Kill workers mid-render: the job fails cleanly and the pool recovers."""
    killed = False
    for scale in (2.5, 3.5, 4.5):  # a fast machine can finish before the kill; retry with more work
        box = {}
        render.submit("render", {"path": str(corpus["a0"]), "page": 0, "scale": scale, "max_pixels": 400_000_000},
                      lambda r, box=box: box.setdefault("res", r), dedupe=False)
        # Kill as soon as a worker has the job, not after a fixed delay.
        qtbot.waitUntil(lambda: "res" in box or any(w.job is not None for w in render._workers), timeout=10000)
        for w in list(render._workers):
            if w.job is not None:
                w.process.kill()
                killed = True
        qtbot.waitUntil(lambda: "res" in box, timeout=20000)
        if killed:
            break
    assert killed
    assert not box["res"].ok
    render._crashes.clear()
    after = run_job(qtbot, render, "info", {"path": str(corpus["a4"])})
    assert after.ok


def test_ui_thread_stays_responsive_while_rendering(qtbot, render, corpus):
    """The original rendered in threads that held the GIL; this must not stall."""
    gaps = []
    t = QElapsedTimer()
    t.start()
    last = [t.elapsed()]

    def tick():
        now = t.elapsed()
        gaps.append(now - last[0])
        last[0] = now

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(5)
    done = []
    for i in range(6):
        render.submit("render", {"path": str(corpus["a0"]), "page": 0, "scale": 1.0 + i * 0.05},
                      lambda r: done.append(r), dedupe=False)
    qtbot.waitUntil(lambda: len(done) == 6, timeout=60000)
    timer.stop()
    assert all(r.ok for r in done)
    assert max(gaps[2:]) < 150, f"UI stalled for {max(gaps)} ms"
