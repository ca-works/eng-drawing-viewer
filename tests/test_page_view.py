"""The page view shows MuPDF's pixels 1:1 (no bitmap enlargement) at any zoom."""
import pymupdf
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QImage

from eng_viewer.ui.page_view import PageRef, PageView, min_line_px, pixmap_cache


def _view(qtbot, render, theme, path):
    view = PageView(render)
    view.resize(900, 640)
    qtbot.addWidget(view)
    view.show()
    doc = pymupdf.open(path)
    r = doc[0].rect
    view.set_page(PageRef(str(path), 0, r.width, r.height, PageRef.stamp_for(str(path))))
    return view


def _settle(qtbot, view):
    qtbot.wait(150)
    qtbot.waitUntil(lambda: not view._pending and not view._tile_timer.isActive(), timeout=30000)
    qtbot.wait(100)


def _compare(view, path) -> float:
    img = view.grab().toImage().convertToFormat(QImage.Format.Format_RGB888)
    dpr = view.devicePixelRatioF()
    s = view.zoom * dpr
    origin = view.viewportTransform().map(QPointF(0, 0))
    ox, oy = round(origin.x() * dpr), round(origin.y() * dpr)
    page = pymupdf.open(path)[0]
    clip = pymupdf.Rect(max(0, -ox) / s, max(0, -oy) / s, (max(0, -ox) + img.width()) / s, (max(0, -oy) + img.height()) / s)
    clip &= page.rect
    pymupdf.TOOLS.set_graphics_min_line_width(min_line_px(True))
    ref = page.get_pixmap(matrix=pymupdf.Matrix(s, s), clip=clip, alpha=False)
    pymupdf.TOOLS.set_graphics_min_line_width(0)
    samples = ref.samples
    data, bpl = bytes(img.constBits()), img.bytesPerLine()
    bad = total = 0
    for y in range(4, ref.height - 4, 5):
        vy = ref.y + oy + y
        if not 0 <= vy < img.height():
            continue
        line = data[vy * bpl: (vy + 1) * bpl]
        for x in range(4, ref.width - 4, 5):
            vx = ref.x + ox + x
            if not 0 <= vx < img.width():
                continue
            i = (y * ref.width + x) * 3
            j = vx * 3
            total += 1
            if max(abs(line[j + k] - samples[i + k]) for k in range(3)) > 60:
                bad += 1
    return bad / max(total, 1)


@pytest.mark.parametrize("pct", [600, 2400])
def test_zoomed_view_is_pixel_exact(qtbot, render, theme, corpus, pct):
    path = str(corpus["a1"])
    view = _view(qtbot, render, theme, path)
    _settle(qtbot, view)
    view.set_zoom(view.ACTUAL_SIZE * pct / 100, QPointF(420, 300))
    _settle(qtbot, view)
    assert _compare(view, path) < 0.001


def test_fit_modes_and_anchored_zoom(qtbot, render, theme, corpus):
    path = str(corpus["a3"]) if "a3" in corpus else str(corpus["multi"])
    view = _view(qtbot, render, theme, path)
    _settle(qtbot, view)
    assert view.fit_mode == "width"
    view.fit_page()
    qtbot.wait(50)
    assert view.fit_mode == "page"
    anchor = QPointF(300, 200)
    before = view.viewportTransform().inverted()[0].map(anchor)
    view.set_zoom(view.zoom * 3, anchor)
    after = view.viewportTransform().inverted()[0].map(anchor)
    assert view.fit_mode is None
    assert abs(before.x() - after.x()) < 1.0 and abs(before.y() - after.y()) < 1.0  # point under cursor stays put


def test_tiles_are_cached_and_reused(qtbot, render, theme, corpus):
    path = str(corpus["a1_10"])
    view = _view(qtbot, render, theme, path)
    _settle(qtbot, view)
    cache = pixmap_cache()
    n = len(cache._items)
    view.set_page(view.page_ref)  # re-show the same page
    qtbot.wait(100)
    assert not view._pending  # everything came from the cache
    assert len(cache._items) == n
