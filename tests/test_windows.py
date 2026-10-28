"""Windows integrations: platform-neutral logic everywhere, real Windows APIs on Windows only."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

WIN = sys.platform == "win32"
win_only = pytest.mark.skipif(not WIN, reason="Windows only")


def needs_windows_qpa(qapp):
    """Title bars and system fonts exist only with Qt's real Windows platform plugin."""
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.platformName() != "windows":
        pytest.skip("needs QT_QPA_PLATFORM=windows")


# ------------------------------------------------------------------ shortcuts and menus
def test_key_sequences_drop_duplicates(qapp):
    from PySide6.QtGui import QKeySequence

    from eng_viewer.ui.widgets import key_sequences

    seqs = key_sequences([QKeySequence.StandardKey.Redo, "Ctrl+Y", "Ctrl+Shift+Z", "Ctrl+Y"])
    texts = [s.toString(QKeySequence.SequenceFormat.PortableText) for s in seqs]
    assert len(texts) == len(set(texts)) and "Ctrl+Y" in texts


def _all_actions(menu_owner):
    for a in menu_owner.actions():
        yield a
        if a.menu() is not None:
            yield from _all_actions(a.menu())


def _shortcut_owners(window):
    from PySide6.QtGui import QKeySequence

    owners: dict[str, list[str]] = {}
    for a in _all_actions(window.menuBar()):
        for ks in a.shortcuts():
            owners.setdefault(ks.toString(QKeySequence.SequenceFormat.PortableText), []).append(a.text())
    return owners


@pytest.fixture()
def main_window(qtbot, settings, render, theme):
    from eng_viewer.ui.main_window import MainWindow

    w = MainWindow(settings, render, None)
    qtbot.addWidget(w)
    return w


def test_main_window_shortcuts_are_unambiguous(main_window):
    dupes = {k: v for k, v in _shortcut_owners(main_window).items() if len(v) > 1}
    assert not dupes, dupes


def test_windows_shortcuts(main_window):
    owners = _shortcut_owners(main_window)
    if WIN or sys.platform.startswith("linux"):
        # AltGr is Ctrl+Alt on Windows keyboards: no Ctrl+Alt shortcuts at all.
        assert not [k for k in owners if "Ctrl+Alt" in k], owners
        for key in ("Ctrl+,", "F5", "F1", "F11", "Ctrl+Shift+L", "Ctrl+Shift+I"):
            assert key in owners, key
        texts = [a.text() for a in _all_actions(main_window.menuBar())]
        assert "E&xit" in texts


def test_menus_get_unique_mnemonics(main_window):
    from eng_viewer.ui.widgets import assign_mnemonics

    assign_mnemonics(main_window.menuBar())  # what Windows builds do; idempotent

    def check(menu):
        letters, missing = [], []
        for a in menu.actions():
            if a.isSeparator() or not a.text():
                continue
            t = a.text().replace("&&", "")
            if "&" in t:
                letters.append(t[t.index("&") + 1].lower())
            else:  # only when every letter of the item is taken (long menus such as PDF Tools)
                assert set(c.lower() for c in t if c.isalnum()) <= set(letters), a.text()
                missing.append(t)
            if a.menu() is not None and a.menu().actions():
                check(a.menu())
        assert len(letters) == len(set(letters)), [a.text() for a in menu.actions()]
        assert len(missing) <= 2, missing

    check(main_window.menuBar())


def test_mark_window_redo_keys_are_unique(qtbot, main_window, settings, render, corpus):
    from PySide6.QtGui import QKeySequence

    from eng_viewer.ui.mark.window import MarkWindow
    from eng_viewer.ui.page_view import PageRef

    pdf = str(next(Path(corpus["root"]).rglob("*.pdf")))
    w = MarkWindow(main_window, render, settings, PageRef(pdf, 0, 842.0, 595.0, PageRef.stamp_for(pdf)), 220)
    qtbot.addWidget(w)
    redo = [s.toString(QKeySequence.SequenceFormat.PortableText) for s in w.act_redo.shortcuts()]
    assert len(redo) == len(set(redo))
    assert "Ctrl+Shift+Z" in redo
    if WIN:
        assert "Ctrl+Y" in redo
    dupes = {k: v for k, v in _shortcut_owners(w).items() if len(v) > 1}
    assert not dupes, dupes
    w.close()


def test_organizer_undo_tooltip_shows_this_platforms_keys(qapp):
    from PySide6.QtGui import QKeySequence

    from eng_viewer.ui.widgets import tip

    text = tip("Undo", QKeySequence.StandardKey.Undo)
    assert ("⌘" in text) == (sys.platform == "darwin")
    src = (Path(__file__).resolve().parents[1] / "src/eng_viewer/ui/tools/organizer.py").read_text(encoding="utf-8")
    assert "⌘" not in src


# ------------------------------------------------------------------ scanning
def test_scan_handles_drives_without_file_ids(tmp_path, monkeypatch):
    """FAT/exFAT and some network drives report inode 0 for every folder."""
    from eng_viewer.core import scanning

    for sub in ("A", "A/B", "C"):
        (tmp_path / sub).mkdir(parents=True)
        (tmp_path / sub / "d.pdf").write_bytes(b"%PDF-1.4\n")
    real_stat = Path.stat

    def no_inode(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        return SimpleNamespace(st_dev=st.st_dev, st_ino=0)

    monkeypatch.setattr(Path, "stat", no_inode)
    res = scanning.scan_folder(tmp_path, recursive=True)
    assert len(res.paths) == 3, res.paths


@win_only
def test_scan_skips_hidden_and_system_folders(tmp_path):
    import ctypes

    from eng_viewer.core.scanning import scan_folder

    for sub in ("visible", "hidden", "$RECYCLE.BIN"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "d.pdf").write_bytes(b"%PDF-1.4\n")
    ctypes.windll.kernel32.SetFileAttributesW(str(tmp_path / "hidden"), 0x2)
    ctypes.windll.kernel32.SetFileAttributesW(str(tmp_path / "$RECYCLE.BIN"), 0x2 | 0x4)
    res = scan_folder(tmp_path, recursive=True)
    assert [p.parent.name for p in res.paths] == ["visible"]


# ------------------------------------------------------------------ open documents
@win_only
def test_open_drawing_does_not_lock_the_file(tmp_path):
    import pymupdf

    from eng_viewer.core.documents import open_document

    src = tmp_path / "drawing.pdf"
    d = pymupdf.open()
    d.new_page()
    d.save(src)
    d.close()
    doc = open_document(pymupdf, str(src))
    try:
        src.rename(tmp_path / "renamed.pdf")  # fails with a sharing violation if the file is held open
        (tmp_path / "renamed.pdf").unlink()
        assert doc.page_count == 1
    finally:
        doc.close()


# ------------------------------------------------------------------ OCR
def test_tesseract_language_names(tmp_path):
    from eng_viewer.core.pdf_tools import ToolError, tesseract_languages

    for name in ("eng", "deu"):
        (tmp_path / f"{name}.traineddata").write_bytes(b"x")
    assert tesseract_languages(["en-US", "de-DE", "English"[:2]], str(tmp_path)) == "eng+deu"
    assert tesseract_languages(None, str(tmp_path)).startswith("eng")
    with pytest.raises(ToolError, match="Available"):
        tesseract_languages(["fr-FR"], str(tmp_path))


@pytest.mark.skipif(not os.environ.get("ENG_VIEWER_TESSDATA"), reason="bundled OCR data not downloaded")
def test_ocr_with_bundled_tesseract_data(tmp_path, monkeypatch):
    import pymupdf

    from eng_viewer.core import pdf_tools as T

    monkeypatch.setattr(T, "ocr_engine", lambda: "tesseract")
    src = pymupdf.open()
    page = src.new_page(width=595, height=842)
    page.insert_text((72, 120), "FLANGE ASSEMBLY 42", fontsize=28)
    pix = page.get_pixmap(dpi=200)
    scan = pymupdf.open()
    scan.new_page(width=595, height=842).insert_image(scan[0].rect, pixmap=pix)
    scan.save(tmp_path / "scan.pdf")
    T.ocr(str(tmp_path / "scan.pdf"), str(tmp_path / "ocr.pdf"), languages="en-GB")
    with pymupdf.open(tmp_path / "ocr.pdf") as d:
        assert "FLANGE" in d[0].get_text().upper()


# ------------------------------------------------------------------ PowerPoint (logic, any platform)
def _fake_powerpoint(slide_count=3):
    app, pres = MagicMock(), MagicMock()
    pres.FullName, pres.Name, pres.ReadOnly = r"C:\Decks\Review.pptx", "Review.pptx", False
    pres.PageSetup.SlideWidth, pres.PageSetup.SlideHeight = 960.0, 540.0
    app.Presentations.Count = 1
    app.Presentations.Item.return_value = pres

    def layout(types):
        lay = MagicMock()
        lay.Shapes.Placeholders.Count = len(types)
        lay.Shapes.Placeholders.Item.side_effect = lambda j: SimpleNamespace(
            PlaceholderFormat=SimpleNamespace(Type=types[j - 1]))
        return lay

    layouts = [layout([1, 2]), layout([1]), layout([])]  # title+content, title only, blank
    pres.SlideMaster.CustomLayouts.Count = len(layouts)
    pres.SlideMaster.CustomLayouts.Item.side_effect = lambda i: layouts[i - 1]
    pres.Slides.Count = slide_count
    added = []

    def add_slide(index, lay):
        slide = MagicMock()
        slide.Shapes.Count = 0
        slide.Shapes.HasTitle = lay is layouts[1]
        added.append((index, lay, slide))
        return slide

    pres.Slides.AddSlide.side_effect = add_slide
    return app, pres, layouts, added


def test_powerpoint_send_follows_the_pptx_layout(monkeypatch, tmp_path):
    from eng_viewer.platform import windows_powerpoint as wp

    app, pres, layouts, added = _fake_powerpoint()
    com = SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
    monkeypatch.setattr(wp, "_com", lambda: (com, None))
    monkeypatch.setattr(wp, "_running_powerpoint", lambda: app)
    slides = [
        {"image": str(tmp_path / "a.png"), "img_w": 3000, "img_h": 2000, "title": "A-100.pdf", "marked": False},
        {"image": str(tmp_path / "b.png"), "img_w": 3000, "img_h": 2000, "title": "B-200.pdf", "marked": True,
         "comments": ["1. Check the weld", "2. Add a datum"]},
    ]
    assert wp.send_slides(slides, r"C:\Decks\Review.pptx", 2, "title") == 2
    assert [i for i, _, _ in added] == [2, 3]
    assert all(lay is layouts[1] for _, lay, _ in added)  # "Title Only"
    plain, marked = added[0][2], added[1][2]
    _, _, _, left, top, w, h = plain.Shapes.AddPicture.call_args.args
    assert abs(left + w / 2 - 480) < 1  # centred
    _, _, _, left, top, w, h = marked.Shapes.AddPicture.call_args.args
    assert left >= 960 * (0.04 + 0.36 + 0.035) - 1  # drawing to the right of the comments
    box = marked.Shapes.AddTextbox.call_args.args
    assert abs(box[1] - 960 * 0.04) < 1 and abs(box[3] - 960 * 0.36) < 1
    assert pres.Windows.Item.return_value.View.GotoSlide.call_args.args == (2,)


def test_powerpoint_send_reports_a_closed_presentation(monkeypatch):
    from eng_viewer.platform import windows_powerpoint as wp

    app, *_ = _fake_powerpoint()
    com = SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
    monkeypatch.setattr(wp, "_com", lambda: (com, None))
    monkeypatch.setattr(wp, "_running_powerpoint", lambda: app)
    with pytest.raises(RuntimeError, match="no longer open"):
        wp.send_slides([], r"C:\Other.pptx", 1, "none")


@win_only
def test_powerpoint_lookup_without_powerpoint_is_quiet(qtbot):
    from eng_viewer.platform.windows_powerpoint import PresentationLookup, list_open_presentations

    assert list_open_presentations() == []  # the runners have no PowerPoint
    lookup = PresentationLookup()
    with qtbot.waitSignal(lookup.found, timeout=20000) as blocker:
        lookup.start()
    assert blocker.args == [[]]


# ------------------------------------------------------------------ Windows APIs
@win_only
def test_single_instance_forwarding(qtbot, tmp_path):
    from eng_viewer.platform import windows

    received = []
    receiver = windows.InstanceReceiver(received.append)
    target = tmp_path / "drawing one.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    try:
        code = textwrap.dedent(f"""
            import sys
            from eng_viewer.platform import windows
            sys.exit(0 if windows.forward_to_running_instance([{str(target)!r}]) else 1)
        """)
        proc = subprocess.Popen([sys.executable, "-c", code])
        qtbot.waitUntil(lambda: proc.poll() is not None, timeout=20000)
        assert proc.returncode == 0
        qtbot.waitUntil(lambda: bool(received), timeout=5000)
        assert received == [[str(target)]]
    finally:
        receiver.close()
    assert windows.decode_paths(windows.encode_paths(["relative.pdf", str(target)])) == [str(target)]


@win_only
def test_taskbar_and_title_bar(qtbot, qapp, monkeypatch):
    needs_windows_qpa(qapp)
    from PySide6.QtWidgets import QMainWindow

    from eng_viewer.platform import windows

    w = QMainWindow()
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    assert windows.style_title_bar(w, True, caption="#232327", text="#F2F2F7", border="#3A3A3D")
    assert windows.style_title_bar(w, False)
    monkeypatch.setenv("ENG_VIEWER_LAUNCHER", sys.executable)
    assert windows.set_relaunch_properties(w)
    assert windows.set_relaunch_properties(w, clear=True)


@win_only
def test_interface_font_is_segoe(qapp):
    needs_windows_qpa(qapp)
    from PySide6.QtGui import QFontDatabase, QFontInfo

    from eng_viewer.ui.theme import windows_ui_font

    font = windows_ui_font()
    print("interface font:", font.family(), "->", QFontInfo(font).family(),
          "| Segoe families:", sorted(f for f in QFontDatabase.families() if f.startswith("Segoe UI")))
    assert QFontInfo(font).family().startswith("Segoe UI")


@win_only
def test_windows_imaging_decoder_reads_upright(tmp_path):
    """The HEIC path, exercised with a JPEG (Windows decodes JPEG without extra extensions)."""
    from PIL import Image

    from eng_viewer.platform import windows_heic

    im = Image.new("RGB", (40, 20), (200, 30, 30))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to view
    im.save(tmp_path / "photo.jpg", exif=exif)
    assert windows_heic.heic_size(str(tmp_path / "photo.jpg")) == (20, 40)
    import io

    out = Image.open(io.BytesIO(windows_heic.heic_to_png(str(tmp_path / "photo.jpg"))))
    assert out.size == (20, 40)
    assert out.getpixel((10, 20))[0] > 150


@win_only
def test_heic_without_codec_explains_what_to_install(tmp_path):
    from eng_viewer.core.documents import image_pixels

    fake = tmp_path / "IMG_0001.HEIC"
    fake.write_bytes(b"\0\0\0\x18ftypheic" + b"\0" * 64)
    try:
        image_pixels(str(fake))
    except ValueError as exc:
        assert "HEIF Image Extensions" in str(exc) or "damaged" in str(exc)
    else:
        pytest.skip("this Windows can decode the sample (HEIF extension installed)")


@win_only
def test_windows_spelling():
    from eng_viewer.platform.windows_spell import make_speller

    speller = make_speller()
    if speller is None:
        pytest.skip("no Windows spelling dictionary on this machine")
    try:
        assert speller.check("drawing")
        assert not speller.check("drawnig")
        assert "drawing" in [s.lower() for s in speller.suggest("drawnig")]
    finally:
        speller.close()


def test_builtin_dictionary_remembers_learned_words(settings):
    import eng_viewer.ui.mark.window as mw

    settings.set("spell_backend", "builtin")
    old = mw._SPELLER
    mw._SPELLER = None
    try:
        speller = mw.get_speller(settings)
        speller.learn("Weldolet")
        assert "weldolet" in settings.get("user_words")
        from eng_viewer.core.config import Settings

        assert "weldolet" in Settings(settings.path).get("user_words")
    finally:
        mw._SPELLER = old
