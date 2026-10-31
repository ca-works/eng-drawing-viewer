"""Pure-Python core: natural sort, folder scanning, settings, spelling."""
import json
import os
import unicodedata
from pathlib import Path

import pytest

from eng_viewer.core.config import Settings
from eng_viewer.core.naturalsort import natural_key, sort_paths
from eng_viewer.core.scanning import scan_folder
from eng_viewer.core.spell import WordListSpeller, checkable

RES = Path(__file__).resolve().parents[1] / "src" / "eng_viewer" / "resources"


# ----------------------------------------------------------------- sorting
def test_natural_order_numbers_and_case():
    names = ["Sheet 10.pdf", "sheet 2.pdf", "Sheet 1c.pdf", "Sheet 1.pdf", "Sheet 1a.pdf", "Sheet 01b.pdf", "Assembly.pdf", "Sheet 007.pdf", "Sheet 7.pdf"]
    ordered = sorted(names, key=natural_key)
    assert ordered == ["Assembly.pdf", "Sheet 1.pdf", "Sheet 1a.pdf", "Sheet 01b.pdf", "Sheet 1c.pdf", "sheet 2.pdf",
                       "Sheet 7.pdf", "Sheet 007.pdf", "Sheet 10.pdf"]


def test_natural_order_unicode_normalisation():
    composed = unicodedata.normalize("NFC", "Çhâssis 2.pdf")
    decomposed = unicodedata.normalize("NFD", "Çhâssis 2.pdf")  # how macOS stores filenames
    assert natural_key(composed) == natural_key(decomposed)


def test_natural_order_mixed_types_never_raise():
    names = ["10", "a", "1a", "a1", "", "007", "7", "_x", "Ü2", "u10"]
    sorted(names, key=natural_key)  # must not raise TypeError


def test_sort_paths_dedupes_and_orders_by_name_then_folder(tmp_path):
    a = tmp_path / "b" / "Drawing 2.pdf"
    b = tmp_path / "a" / "Drawing 2.pdf"
    c = tmp_path / "Drawing 10.pdf"
    assert sort_paths([c, a, b, a]) == [b, a, c]


# ---------------------------------------------------------------- scanning
def test_scan_finds_nested_and_skips_junk(corpus):
    res = scan_folder(corpus["root"], recursive=True)
    names = {p.name for p in res.paths}
    assert "Deep nested drawing.pdf" in names
    assert "UPPERCASE.PDF" in names
    assert "Çhâssis — rév B (final) #12 [ä].pdf" in names
    assert "._Drawing 1.pdf" not in names  # AppleDouble resource fork
    assert "should not appear.pdf" not in names  # hidden folder
    assert "notes.txt" not in names
    assert not res.errors


def test_scan_non_recursive(corpus):
    res = scan_folder(corpus["root"] / "Project X", recursive=False)
    assert sorted(p.name for p in res.paths) == ["Drawing 1.pdf", "Drawing 10.pdf", "Drawing 2.pdf"]
    assert [p.name for p in res.paths] == ["Drawing 1.pdf", "Drawing 2.pdf", "Drawing 10.pdf"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink/permission semantics")
def test_scan_survives_symlink_loop_and_unreadable_folder(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.pdf").write_bytes(b"%PDF-1.4")
    os.symlink(tmp_path, tmp_path / "a" / "loop")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "y.pdf").write_bytes(b"%PDF-1.4")
    os.chmod(locked, 0)
    try:
        res = scan_folder(tmp_path, recursive=True)
    finally:
        os.chmod(locked, 0o755)
    if os.geteuid() != 0:
        assert [p.name for p in res.paths] == ["x.pdf"]
        assert res.errors  # reported, not raised
    else:  # root (e.g. a CI container) can read a folder with no permissions
        assert sorted(p.name for p in res.paths) == ["x.pdf", "y.pdf"]


def test_scan_can_be_cancelled(corpus):
    res = scan_folder(corpus["root"], recursive=True, should_cancel=lambda: True)
    assert res.cancelled


# ---------------------------------------------------------------- settings
def test_settings_roundtrip_and_defaults(tmp_path):
    s = Settings(tmp_path / "s.json")
    assert s.get("quality") == "high"
    assert s.get("recent_folders") == []
    s.get("recent_folders").append("mutating a copy must not leak")
    assert s.get("recent_folders") == []
    s.set("quality", "max")
    s.add_recent_folder("/a")
    s.add_recent_folder("/b")
    s.add_recent_folder("/a")
    assert s.save()
    s2 = Settings(tmp_path / "s.json")
    assert s2.get("quality") == "max"
    assert s2.get("recent_folders") == ["/a", "/b"]


def test_settings_tolerates_corrupt_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert Settings(p).get("theme") == "system"


# ---------------------------------------------------------------- spelling
def test_checkable_skips_codes_and_acronyms():
    assert not checkable("GD")
    assert not checkable("ISO")
    assert not checkable("x")
    assert not checkable("McMaster")
    assert checkable("chamfer")
    assert checkable("Chamfer")


def test_builtin_speller():
    sp = WordListSpeller(RES / "english_words.txt")
    assert sp.check("tolerance")
    assert sp.check("Tolerances")
    assert sp.check("drawing's")
    assert sp.check("callout")  # engineering supplement
    assert sp.check("chamfered")
    assert not sp.check("tolerence")
    assert "tolerance" in sp.suggest("tolerence")
    assert sp.suggest("Degres")[0][0].isupper()
    sp.learn("SampleWidget")
    assert sp.check("samplewidget")
    sp.ignore("zzqx")
    assert sp.check("zzqx")


def test_version_is_consistent_everywhere():
    import re
    from pathlib import Path
    import eng_viewer
    root = Path(__file__).resolve().parents[1]
    v = eng_viewer.__version__
    win_file = root / "packaging/windows/version_info.txt"
    if win_file.exists():  # not in the Mac source release
        win = win_file.read_text()
        assert f"'{v}'" in win and "(%s, 0)" % ", ".join(v.split(".")) in win
    spec = (root / "packaging/macos/ENG_Drawing_Viewer_mac.spec").read_text()
    assert "2.1.0" not in spec and "__init__.py" in spec
