"""PDF Tools tab, drag and drop, image documents, iPhone scans and the queue as one PDF."""
import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QColor, QDropEvent, QImage, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from eng_viewer.ui.main_window import MainWindow


@pytest.fixture()
def win(qtbot, render, theme, settings, corpus, tmp_path):
    settings.set("scans_folder", str(tmp_path / "Scans"))
    w = MainWindow(settings, render, QPixmap())
    w.resize(1300, 820)
    qtbot.addWidget(w)
    w.show()
    w.load_folder(corpus["root"])
    qtbot.waitUntil(lambda: len(w.files) > 0, timeout=10000)
    yield w
    if w.tools is not None:
        for wrap in w.tools.panels.values():
            if hasattr(wrap.panel, "cleanup"):
                wrap.panel.cleanup()


@pytest.fixture()
def photo(tmp_path) -> Path:
    path = tmp_path / "Site photos" / "site photo.png"
    path.parent.mkdir()
    im = Image.new("RGB", (1200, 800), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((100, 100, 500, 400), outline="black", width=8)
    im.save(path, dpi=(150, 150))
    return path


def _wait_doc(qtbot, w, path):
    """Until ``path`` is shown with its own page info (not the previous document's)."""
    p = str(path)
    qtbot.waitUntil(lambda: w.doc_path == p and "count" in w._doc_info.get(p, {}) and w.page_sizes is w._doc_info[p]["sizes"],
                    timeout=15000)


def _pages(path) -> int:
    with pymupdf.open(path) as d:
        return d.page_count


def _drop(widget, paths):
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    ev = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction, md, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(ev)
    return ev


def _save_to(monkeypatch, path: Path):
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))


def _folder_to(monkeypatch, path: Path):
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(path)))


# ------------------------------------------------------------------ tools tab
def test_tools_mode_switches_and_keeps_the_open_drawing(qtbot, win, corpus):
    from eng_viewer.ui.tools.specs import SPECS

    win.open_document(str(corpus["a4"]), 0)
    _wait_doc(qtbot, win, corpus["a4"])
    win.set_mode("tools")
    assert win.content_stack.currentWidget() is win.tools
    assert win.act_mode_tools.isChecked() and win.viewer_controls.isHidden()
    assert not win.act_next.isEnabled() and not win.act_mark.isEnabled()
    assert len(win.tools.home.cards) == len(SPECS)
    win.set_mode("viewer")
    assert win.content_stack.currentWidget() is win.viewer_split
    assert win.doc_path == str(corpus["a4"]) and win.act_mark.isEnabled()


def test_merge_tool_writes_a_new_pdf(qtbot, win, corpus, tmp_path, monkeypatch):
    win.open_tool("merge", [str(corpus["a1"]), str(corpus["multi"])])
    panel = win.tools.panels["merge"].panel
    assert panel.inputs.paths() == [str(corpus["a1"]), str(corpus["multi"])]
    out = tmp_path / "merged.pdf"
    _save_to(monkeypatch, out)
    panel.run()
    qtbot.waitUntil(lambda: len(panel._results) == 1 and not panel._jobs, timeout=20000)
    assert panel._results[0].ok, panel._results[0].error
    assert _pages(out) == 6


def test_batch_tool_writes_one_result_per_file(qtbot, win, corpus, tmp_path, monkeypatch):
    win.open_tool("rotate", [str(corpus["a1"]), str(corpus["a4"])])
    panel = win.tools.panels["rotate"].panel
    _folder_to(monkeypatch, tmp_path / "rotated")
    panel.run()
    qtbot.waitUntil(lambda: len(panel._results) == 2 and not panel._jobs, timeout=20000)
    made = sorted(p.name for p in (tmp_path / "rotated").iterdir())
    assert len(made) == 2 and all(r.ok for r in panel._results)


def test_organizer_reorders_rotates_and_deletes(qtbot, win, corpus, tmp_path, monkeypatch):
    win.open_tool("organize", [str(corpus["multi"])])
    org = win.tools.panels["organize"].panel
    qtbot.waitUntil(lambda: org.model.rowCount() == 5, timeout=10000)
    sel = org.view.selectionModel()
    sel.select(org.model.index(0), sel.SelectionFlag.ClearAndSelect)
    org.rotate(90)
    sel.select(org.model.index(4), sel.SelectionFlag.ClearAndSelect)
    org.delete_selected()
    assert org.model.rowCount() == 4
    org.undo.undo()
    assert org.model.rowCount() == 5
    org.undo.redo()
    out = tmp_path / "organized.pdf"
    _save_to(monkeypatch, out)
    org.save()
    qtbot.waitUntil(out.exists, timeout=20000)
    with pymupdf.open(out) as d, pymupdf.open(corpus["multi"]) as src:
        assert d.page_count == 4
        assert d[0].rotation == (src[0].rotation + 90) % 360


def test_compare_finds_the_changed_area():
    from eng_viewer.ui.tools.compare import compose

    a = QImage(400, 300, QImage.Format.Format_RGB888)
    a.fill(QColor("white"))
    p = QPainter(a)
    p.fillRect(QRect(50, 50, 100, 40), QColor("black"))
    p.end()
    same, none = compose(a, a.copy(), 72, ("A", "B"))
    assert none == []
    b = a.copy()
    p = QPainter(b)
    p.fillRect(QRect(250, 200, 60, 30), QColor("black"))
    p.end()
    img, rects = compose(a, b, 72, ("A", "B"))
    assert len(rects) == 1 and rects[0].contains(QPoint(280, 215))
    assert img.width() >= 400


def test_compare_panel_end_to_end(qtbot, win, corpus):
    win.open_tool("compare", [str(corpus["a1"]), str(corpus["a1_2"])])
    panel = win.tools.panels["compare"].panel
    assert panel.a.path == str(corpus["a1"]) and panel.b.path == str(corpus["a1_2"])
    panel.run()
    qtbot.waitUntil(lambda: bool(panel._png) and not panel.progress.isVisible(), timeout=30000)
    assert "changed area" in panel.stats.text()
    qtbot.waitUntil(lambda: panel.changes.count() > 0, timeout=10000)


# --------------------------------------------------------------- drag & drop
def test_dropped_files_open_in_the_viewer(qtbot, win, corpus, photo):
    _drop(win, [corpus["a1_10"]])
    _wait_doc(qtbot, win, corpus["a1_10"])
    _drop(win, [photo])
    _wait_doc(qtbot, win, photo)
    assert Path(win.source_root) == photo.parent
    meta = win.doc_info.meta.text()
    assert "PNG image" in meta and "1200 × 800 px" in meta


def test_drops_feed_the_open_tool_and_office_files_convert(qtbot, win, corpus, tmp_path):
    win.open_tool("merge")
    _drop(win, [corpus["a1"], corpus["a4"]])
    assert win.tools.panels["merge"].panel.inputs.paths() == [str(corpus["a1"]), str(corpus["a4"])]
    win.set_mode("viewer")
    doc = tmp_path / "Report.docx"
    doc.write_bytes(b"PK\x03\x04 not a real document")
    _drop(win, [doc])
    assert win._mode == "tools" and win.tools.current_tool() == "office_to_pdf"
    assert win.tools.panels["office_to_pdf"].panel.inputs.paths() == [str(doc)]


def test_queue_accepts_dropped_files_and_exports_one_pdf(qtbot, win, corpus, photo, tmp_path, monkeypatch):
    win.queue.model.clear()
    _drop(win.queue.view, [corpus["multi"], photo])
    qtbot.waitUntil(lambda: len(win.queue.model.entries) == 6, timeout=15000)
    marked = QImage(1240, 1754, QImage.Format.Format_RGB888)
    marked.fill(QColor("white"))
    marked.setDotsPerMeterX(round(150 / 0.0254))
    marked.setDotsPerMeterY(round(150 / 0.0254))
    win.add_marked_page(str(corpus["a4"]), 0, marked, "1. Check this", 1)
    out = tmp_path / "queue.pdf"
    _save_to(monkeypatch, out)
    win.export_queue_pdf()
    qtbot.waitUntil(out.exists, timeout=30000)
    with pymupdf.open(out) as d:
        assert d.page_count == 7
        assert abs(d[6].rect.width - 1240 * 72 / 150) < 1  # the marked page keeps its size


def test_include_images_setting_is_restored_and_applied(qtbot, render, theme, settings, corpus, photo):
    settings.set("include_images", False)
    w = MainWindow(settings, render, QPixmap())
    qtbot.addWidget(w)
    assert not w.act_images.isChecked() and not w.library.act_images.isChecked()
    w.load_folder(photo.parent)
    qtbot.waitUntil(lambda: w.library.count_label.text() != "Scanning…", timeout=10000)
    assert w.files == []
    w.library.act_images.trigger()  # the library's own menu
    qtbot.waitUntil(lambda: len(w.files) == 1, timeout=10000)
    assert settings.get("include_images") is True and w.act_images.isChecked()


# ----------------------------------------------------------- images and MARK
def test_image_pages_render_at_native_resolution(qtbot, win, photo):
    win.open_paths([photo])
    _wait_doc(qtbot, win, photo)
    win.copy_page_image()
    qtbot.waitUntil(lambda: win._copy_job is None, timeout=20000)
    assert QApplication.clipboard().image().width() == 1200


def test_mark_saves_vector_markup_as_pdf_on_rotated_page(qtbot, win, corpus, tmp_path, monkeypatch):
    path = corpus["rotated"]
    win.open_document(str(path), 0)
    _wait_doc(qtbot, win, path)
    win.open_mark()
    qtbot.waitUntil(lambda: len(win._mark_windows) == 1, timeout=5000)
    mw = next(iter(win._mark_windows.values()))
    c = mw.canvas
    c.set_zoom_percent(100, animate=False)
    target = QPointF(160, 120)  # top-left area of the page as displayed
    c.centerOn(target)
    c.set_tool("bubble")
    QTest.mouseClick(c.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c.mapFromScene(target))
    bubble = c.bubbles()[0]
    out = tmp_path / "marked.pdf"
    _save_to(monkeypatch, out)
    mw.save_pdf()
    qtbot.waitUntil(lambda: out.exists() and c.undo_stack.isClean(), timeout=20000)
    with pymupdf.open(out) as d, pymupdf.open(path) as src:
        assert d.page_count == 1
        assert d[0].rect == src[0].rect and d[0].rotation == src[0].rotation
        pix = d[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    ring = bubble.scenePos() * 2 + QPointF(bubble.diameter, 0)
    r, g, b = pix.pixel(round(ring.x()) - 2, round(ring.y()))
    assert r > 170 and g < 110, (r, g, b)
    mw.close()


def test_mark_on_an_image_uses_its_own_pixels(qtbot, win, photo, tmp_path, monkeypatch):
    win.open_paths([photo])
    _wait_doc(qtbot, win, photo)
    win.open_mark()
    qtbot.waitUntil(lambda: len(win._mark_windows) == 1, timeout=5000)
    mw = next(iter(win._mark_windows.values()))
    assert mw.is_image and "page" not in mw.windowTitle()
    qtbot.waitUntil(lambda: mw._base is not None, timeout=20000)
    assert mw._base.width() == 1200
    out = tmp_path / "photo marked.pdf"
    _save_to(monkeypatch, out)
    mw.save_pdf()
    qtbot.waitUntil(out.exists, timeout=20000)
    assert _pages(out) == 1
    mw.close()


# ---------------------------------------------------------------- iPhone scan
@pytest.mark.skipif(sys.platform != "darwin", reason="Continuity Camera is macOS only")
def test_iphone_scan_is_saved_unchanged_and_opened(qtbot, win, corpus):
    pytest.importorskip("AppKit")
    from AppKit import NSPasteboard, NSPasteboardItem
    from Foundation import NSData

    from eng_viewer.platform.continuity import ContinuityCamera

    cam = ContinuityCamera(win._receive_scan, lambda: None, lambda: None, lambda: None)
    pb = NSPasteboard.pasteboardWithUniqueName()
    try:
        pb.clearContents()
        item = NSPasteboardItem.alloc().init()
        data = corpus["multi"].read_bytes()
        item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), "com.adobe.pdf")
        assert pb.writeObjects_([item])
        assert cam.deliver_for_test(pb)
    finally:
        pb.releaseGlobally()
    saved = list(Path(win.settings.get("scans_folder")).glob("Scan *.pdf"))
    assert len(saved) == 1 and saved[0].read_bytes() == data
    _wait_doc(qtbot, win, saved[0])
    assert win.page_count == 5 and Path(win.source_root) == saved[0].parent
