"""Focused source-side Windows 2.3.3 checks; no Windows GUI or Office required."""
from datetime import date
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock
import zipfile

import pymupdf
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QPixmap

from eng_viewer.core.reference_rtf import rtf_text
from eng_viewer.core.reference_text import decode_text
from eng_viewer.core.reference_office import passive_office_copy
from eng_viewer.ui.mark.items import StampItem, SymbolItem, item_from_dict


def test_stamp_and_symbol_roundtrip(theme):
    for kind in ('pass', 'fail'):
        item = StampItem(kind, QColor('#123456'), 120, 0.37, True, date.today().strftime('%d/%m/%Y'))
        item.setPos(QPointF(13, 27))
        restored = item_from_dict(item.to_dict())
        assert restored.to_dict() == item.to_dict()
        assert restored.placed == date.today().strftime('%d/%m/%Y')
    for kind in ('checkmark', 'cross'):
        item = SymbolItem(kind, QColor('blue'), 40, 2)
        assert item_from_dict(item.to_dict()).to_dict() == item.to_dict()


def test_rtf_passive_unicode_and_lines():
    data = br'{\rtf1\ansi A\par B\tab \u945? {\*\unknown hidden}{\field{\*\fldinst INCLUDETEXT "https://example.com"}{\fldrslt Cached}}}'
    assert rtf_text(data) == 'A\nB\tα Cached'
    assert rtf_text(br"{\rtf1 caf\'e9}") == 'café'
    with pytest.raises(ValueError):
        rtf_text(br'{\rtf1 broken')
    assert decode_text('Ω\n漢字'.encode('utf-16')) == 'Ω\n漢字'


def test_office_copy_drops_active_content_and_keeps_original(tmp_path):
    from docx import Document
    doc = Document()
    doc.add_paragraph('Reference content')
    source = tmp_path / 'source.docx'
    doc.save(source)
    with zipfile.ZipFile(source, 'a') as archive:
        archive.writestr('word/embeddings/active.bin', b'active')
        archive.writestr('word/_rels/external.xml.rels', b'<Relationships><Relationship TargetMode="External" Target="https://example.com"/></Relationships>')
    original = source.read_bytes()
    work = tmp_path / 'private'
    work.mkdir()
    safe = passive_office_copy(source, work)
    with zipfile.ZipFile(safe) as archive:
        assert 'word/embeddings/active.bin' not in archive.namelist()
        assert b'https://' not in archive.read('word/_rels/external.xml.rels')
    assert Document(safe).paragraphs[0].text == 'Reference content'
    assert source.read_bytes() == original


def test_windows_reference_scan_and_profile(tmp_path, monkeypatch):
    from eng_viewer.core import standards as s
    monkeypatch.setattr(s, 'OPEN_EXTS', {'.txt', '.md', '.rtf', '.csv', '.tsv'})
    monkeypatch.setattr(s, 'STANDARDS_EXTS', s.STANDARDS_EXTS | s.OPEN_EXTS)
    nested = tmp_path / 'standards'
    nested.mkdir()
    for name in ('a.pdf', 'b.docx', 'c.xlsx', 'Ω.csv', 'q.tsv', 'x.txt', 'r.rtf', 'm.md', 'skip.exe'):
        (nested / name).write_text('test')
    files, skipped = s.scan_folder(tmp_path, Event())
    assert len(files) == 8 and skipped == 0
    assert s.scan_folder(tmp_path, _cancelled())[0] == []
    monkeypatch.setattr(s.sys, 'platform', 'win32')
    command = ['C:/LibreOffice/soffice.exe', 'input.docx']
    assert s.restricted_office_command(command, tmp_path / 'scratch') == command
    profile = (tmp_path / 'scratch/profile/user/registrymodifications.xcu').read_text()
    assert 'DisableMacrosExecution' in profile and 'DisableActiveContent' in profile


def _cancelled():
    event = Event()
    event.set()
    return event


def test_table_preview_preserves_literal_values(qapp, tmp_path):
    from eng_viewer.ui.reference_formats import make_preview, _rows
    text = 'Name,Value\n"quoted, name","=1+2"\nΩ,"line1\nline2"\n'
    assert list(_rows(text, ','))[1:] == [['quoted, name', '=1+2'], ['Ω', 'line1\nline2']]
    source = tmp_path / 'values.csv'
    source.write_text(text, encoding='utf-8')
    output = tmp_path / 'preview.pdf'
    make_preview(source, output, Event())
    with pymupdf.open(output) as pdf:
        assert '=1+2' in ''.join(p.get_text() for p in pdf)


def test_com_background_removes_foreground_and_rolls_back():
    from eng_viewer.platform.windows_powerpoint import _insert_with_background
    pres, layout = MagicMock(), MagicMock()
    slide = pres.Slides.Item.return_value.Duplicate.return_value.Item.return_value
    slide.Shapes.Count = 3
    slide.NotesPage.Shapes.Count = 0
    assert _insert_with_background(pres, 2, layout) is slide
    assert [c.args[0] for c in slide.Shapes.Item.call_args_list] == [3, 2, 1]
    pres.Slides.AddSlide.assert_not_called()
    slide.Shapes.Item.return_value.Delete.side_effect = RuntimeError('failed')
    with pytest.raises(RuntimeError):
        _insert_with_background(pres, 2, layout)
    slide.Delete.assert_called_once()
    _insert_with_background(pres, 1, layout)
    pres.Slides.AddSlide.assert_called_once_with(1, layout)


def test_windows_shared_ui_session(qtbot, render, theme, settings, corpus, monkeypatch):
    from eng_viewer.ui import main_window as main
    from eng_viewer.ui.mark import window as mark
    from eng_viewer.ui.mark.items import StrokeItem
    monkeypatch.setattr(main, 'IS_WIN', True)
    monkeypatch.setattr(mark, 'IS_WIN', True)
    w = main.MainWindow(settings, render, QPixmap())
    qtbot.addWidget(w)
    w.show()
    path = str(corpus['multi'])
    w.open_document(path)
    qtbot.waitUntil(lambda: w.view.page_ref is not None)
    w.open_mark()
    editor = next(iter(w._mark_windows.values()))
    assert {'checkmark', 'cross', 'pass', 'fail'} <= set(editor.available_tools)
    stamp = StampItem('pass', QColor('green'), 100, 0.5, True, '12/11/2026')
    editor.canvas.attach_items([stamp])
    dock = w._standards_dock()
    dock.panel.open_paths([Path(path)])
    reference = dock.panel.tabs.widget(0)
    qtbot.waitUntil(lambda: reference.canvas.page_ref is not None)
    stroke = StrokeItem(QColor('blue'), 2)
    stroke.add_point(QPointF(20, 20))
    if reference.continuous:
        from eng_viewer.ui.reference_view import PageInk
        ink = PageInk(reference.canvas.pages[0])
        ink.attach_items([stroke])
    else:
        reference.canvas.attach_items([stroke])
        ink = reference.canvas.active[0]
    dock._dock_at(editor)
    dock._dock_at(w)
    assert ink.childItems() == [stroke]
    w.btn_annotations.setChecked(True)
    editor.close()
    assert w.mark_items(w.view.page_ref) == [stamp]
    assert dock.host is w
    w.open_mark()
    reopened = next(iter(w._mark_windows.values()))
    assert reopened.canvas.annotations() == [stamp]
    reopened.close()
    dock.panel.close_tab(0)
    assert dock.panel.tabs.count() == 0
    w.close()


def test_empty_image_canvas_recovers_and_final_tab_clears(qtbot, render, theme, settings):
    from PIL import Image
    from eng_viewer.core.image_document import ImageDocument, ImageLayer
    from eng_viewer.ui.main_window import MainWindow
    w = MainWindow(settings, render, QPixmap())
    qtbot.addWidget(w)
    w.set_mode('image')
    e = w.image_editor
    e.add_document(ImageDocument.from_image(Image.new('RGBA', (80, 60), 'red')), 'Test')
    e.remove_layer()
    qtbot.waitUntil(lambda: e._job is None)
    assert e.doc.layers == [] and e.doc.active_layer is None
    e.history(False)
    qtbot.waitUntil(lambda: e._job is None)
    assert len(e.doc.layers) == 1
    e.history(True)
    qtbot.waitUntil(lambda: e._job is None)
    assert e.doc.layers == []
    e.doc.insert_layer(ImageLayer('replacement', Image.new('RGBA', (10, 10), 'blue')))
    e.refresh()
    assert e.active_layer.name == 'replacement'
    e.doc._saved_revision = e.doc.revision
    e.close_tab(0)
    assert e.canvas.item.pixmap().isNull()
    assert e.canvas._selection.isEmpty()
    w.close()


def test_xlsx_preview_never_evaluates_formulas(tmp_path):
    from lxml import etree as ET
    source = tmp_path / 'formula.xlsx'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('xl/worksheets/sheet1.xml', b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c><f>WEBSERVICE("https://example.com")</f><v>42</v></c></row></sheetData></worksheet>')
    work = tmp_path / 'work'
    work.mkdir()
    safe = passive_office_copy(source, work)
    with zipfile.ZipFile(safe) as archive:
        tree = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        assert not tree.xpath('//*[local-name()="f"]')
        assert tree.xpath('//*[local-name()="v"]/text()') == ['42']


def test_windows_office_conversion_private_source(tmp_path, monkeypatch):
    from docx import Document
    from eng_viewer.core import pdf_tools as t
    source = tmp_path / 'Reference with spaces.docx'
    Document().save(source)
    original = source.read_bytes()
    output = tmp_path / 'out'
    output.mkdir()
    commands = []
    monkeypatch.setattr(t, 'find_office', lambda: '/local/soffice.exe')
    monkeypatch.setattr(t.sys, 'platform', 'win32')

    def convert(command, **kwargs):
        commands.append(command)
        path = Path(command[-1])
        assert path != source and path.exists()
        directory = Path(command[command.index('--outdir') + 1])
        (directory / (path.stem + '.pdf')).write_bytes(b'preview')
        return 0

    monkeypatch.setattr(t, 'run_conversion', convert)
    result = t.office_to_pdf([str(source)], str(output), reference=True)
    assert Path(result['outputs'][0]).read_bytes() == b'preview'
    assert 'sandbox-exec' not in commands[0][0]
    assert source.read_bytes() == original


@pytest.mark.parametrize('fill', [
    '<a:solidFill><a:srgbClr val="102030"/></a:solidFill>',
    '<a:solidFill><a:schemeClr val="accent2"/></a:solidFill>',
    '<a:gradFill><a:gsLst><a:gs pos="0"><a:srgbClr val="FFFFFF"/></a:gs><a:gs pos="100000"><a:srgbClr val="123456"/></a:gs></a:gsLst><a:lin ang="5400000" scaled="1"/></a:gradFill>',
])
def test_pptx_background_color_theme_gradient_survive_save(tmp_path, fill):
    from pptx import Presentation
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls
    from eng_viewer.core.pptx_export import _inherit_background
    prs = Presentation()
    source = prs.slides.add_slide(prs.slide_layouts[6])
    source._element.cSld.insert(0, parse_xml(f'<p:bg {nsdecls("p", "a")}><p:bgPr>{fill}<a:effectLst/></p:bgPr></p:bg>'))
    source.shapes.add_textbox(1, 1, 100, 100).text = 'Do not copy foreground'
    target = prs.slides.add_slide(source.slide_layout)
    _inherit_background(target, source)
    out = tmp_path / 'synthetic.pptx'
    prs.save(out)
    restored = Presentation(out)
    assert restored.slides[0]._element.cSld.bg.xml == restored.slides[1]._element.cSld.bg.xml
    assert not restored.slides[1].shapes
    assert restored.slide_width == prs.slide_width and restored.slide_height == prs.slide_height
