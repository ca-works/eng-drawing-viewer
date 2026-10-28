"""Main window behaviour: library, navigation across PDFs, queue, clipboard."""
from pathlib import Path

import pytest
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from eng_viewer.ui.main_window import MainWindow


@pytest.fixture()
def win(qtbot, render, theme, settings, corpus):
    w = MainWindow(settings, render, QPixmap())
    w.resize(1300, 820)
    qtbot.addWidget(w)
    w.show()
    w.load_folder(corpus["root"])
    qtbot.waitUntil(lambda: len(w.files) > 0, timeout=10000)
    return w


def _wait_doc(qtbot, w, path, page=None):
    qtbot.waitUntil(lambda: w.doc_path == str(path) and w.page_count > 0 and (page is None or w.page_index == page), timeout=15000)


def test_library_lists_all_pdfs_naturally(win, corpus):
    names = [p.name for p in win.files]
    assert len(names) == 15
    px = [n for n in names if n.startswith("Drawing ")]
    assert px == ["Drawing 1.pdf", "Drawing 2.pdf", "Drawing 10.pdf"]
    win._set_library_mode("flat")
    flat = [p.name for p in win.files]
    assert flat.index("Drawing 2.pdf") < flat.index("Drawing 10.pdf")
    from eng_viewer.core.naturalsort import natural_key

    assert flat == sorted(flat, key=natural_key)  # flat view is purely by name


def test_filter_matches_name_and_folder(win):
    win.library.search.setText("gearbox")
    proxy = win.library.proxy
    visible = []

    def walk(parent):
        for r in range(proxy.rowCount(parent)):
            idx = proxy.index(r, 0, parent)
            if proxy.hasChildren(idx):
                walk(idx)
            else:
                visible.append(idx.data())

    from PySide6.QtCore import QModelIndex

    walk(QModelIndex())
    assert visible == ["Gearbox set (5 sheets).pdf"]
    win.library.search.setText("sub assemblies")  # folder name matches every file inside
    visible.clear()
    walk(QModelIndex())
    assert sorted(visible) == ["Gearbox set (5 sheets).pdf", "Rotated 90.pdf"]


def test_page_navigation_crosses_documents(qtbot, win, corpus):
    files = [str(p) for p in win.files]
    multi = str(corpus["multi"])
    win.open_document(multi, 0)
    _wait_doc(qtbot, win, multi, 0)
    assert win.page_count == 5
    for i in range(1, 5):
        win.act_next.trigger()  # same action as the → key
        assert win.page_index == i
    win.act_next.trigger()  # past the last page: next PDF, first page
    nxt = files[files.index(multi) + 1]
    _wait_doc(qtbot, win, nxt, 0)
    win.act_prev.trigger()  # back: previous PDF, *last* page
    _wait_doc(qtbot, win, multi, 4)
    win.act_first.trigger()
    assert win.page_index == 0


def test_unreadable_pdf_shows_message_not_crash(qtbot, win, corpus):
    bad = str(corpus["root"] / "Problems" / "truncated.pdf")
    win.open_document(bad, 0)
    qtbot.waitUntil(lambda: "can’t be displayed" in win.msg_title.text(), timeout=10000)
    assert win.viewer_stack.currentWidget() is win.message_page
    assert "Damaged" in win.msg_body.text()
    assert not win.btn_mark.isEnabled()


def test_queue_add_current_selected_all(qtbot, win, corpus):
    win.open_document(str(corpus["multi"]), 2)
    _wait_doc(qtbot, win, corpus["multi"], 2)
    win.add_current_to_queue()
    entries = win.queue.model.entries
    assert len(entries) == 1 and entries[0].page_index == 2
    win.library.select_path(str(corpus["multi"]))
    win.add_selected_to_queue()
    qtbot.waitUntil(lambda: len(win.queue.model.entries) == 6, timeout=15000)
    assert [e.page_index for e in win.queue.model.entries[1:]] == [0, 1, 2, 3, 4]
    rows = win.queue.model.move_rows([5], 0)
    assert rows == [0] and win.queue.model.entries[0].page_index == 4
    win.queue.model.clear()
    win._add_paths_to_queue([str(p) for p in win.files])
    # 4 problem files are skipped; 11 readable PDFs with 1 page each except the 5-page set.
    qtbot.waitUntil(lambda: len(win.queue.model.entries) == 15, timeout=30000)


def test_copy_filename_and_page_image(qtbot, win, corpus):
    win.open_document(str(corpus["a4"]), 0)
    _wait_doc(qtbot, win, corpus["a4"])
    win.copy_filename()
    assert QApplication.clipboard().text() == "a4 portrait spec.pdf"
    win.copy_page_image()
    qtbot.waitUntil(lambda: win._copy_job is None, timeout=20000)
    img = QApplication.clipboard().image()
    assert abs(img.width() - 595.3 * 220 / 72) <= 2
    assert round(img.dotsPerMeterX() * 0.0254) == 220


def test_open_single_pdf_path_loads_its_folder(qtbot, win, corpus, tmp_path):
    # A file already in the library opens in place; the library stays as it is.
    root = win.source_root
    target = corpus["unicode"]
    win.open_paths([target])
    _wait_doc(qtbot, win, target)
    assert Path(win.source_root) == Path(root)
    # A file from elsewhere loads its own folder.
    outside = tmp_path / "Elsewhere" / "Loose drawing.pdf"
    outside.parent.mkdir()
    outside.write_bytes(corpus["a4"].read_bytes())
    win.open_paths([outside])
    _wait_doc(qtbot, win, outside)
    assert Path(win.source_root) == outside.parent


def test_theme_switching_at_runtime(qtbot, win):
    from eng_viewer.ui.theme import ThemeManager

    for mode in ("dark", "light", "system"):
        win.set_theme(mode)
        assert ThemeManager.instance().mode == mode
        assert win.theme_actions[mode].isChecked()
    assert ThemeManager.current().dark in (True, False)


def test_mark_window_opens_and_closes_cleanly(qtbot, win, corpus):
    win.open_document(str(corpus["a4"]), 0)
    _wait_doc(qtbot, win, corpus["a4"])
    win.act_mark.trigger()
    qtbot.waitUntil(lambda: len(win._mark_windows) == 1, timeout=5000)
    mark = next(iter(win._mark_windows.values()))
    assert mark.canvas.page_ref.path == str(corpus["a4"])
    win.act_mark.trigger()  # same page again: reuses the open window
    assert len(win._mark_windows) == 1
    mark.close()  # no markup -> closes without asking
    qtbot.waitUntil(lambda: not win._mark_windows, timeout=5000)


def test_about_shows_legal_notices(qtbot):
    from eng_viewer import SOURCE_URL, __version__
    from eng_viewer.ui.about import AboutDialog, license_text, third_party_text

    dlg = AboutDialog()
    qtbot.addWidget(dlg)
    text = " ".join(w.text() for w in dlg.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel))
    assert __version__ in text and "WITHOUT ANY WARRANTY" in text and SOURCE_URL in text
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_text()
    assert "SCOWL" in third_party_text()


def test_specs_bundle_licence_texts():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "packaging/macos/ENG_Drawing_Viewer_mac.spec").read_text()
    assert '(str(ROOT / "LICENSE"), "legal")' in text and '"legal/licenses"' in text
    # Windows: the build script puts "legal" next to the launcher (ui/about.py looks there too).
    script = root / "packaging/windows/build_windows.ps1"
    if script.exists():
        text = script.read_text(encoding="utf-8")
        assert 'Copy-Item LICENSE "$Dist\\legal\\LICENSE"' in text
        assert '--out "$Dist\\legal\\licenses"' in text
