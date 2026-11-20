"""Regression coverage for the macOS Reference dock's real input paths."""
import sys

import pymupdf
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QLineEdit

from eng_viewer.ui.main_window import MainWindow

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Reference viewer")


def test_reference_continuous_scroll_and_document_history(qtbot, render, theme, settings, tmp_path):
    def pdf(name, count):
        path = tmp_path / name
        with pymupdf.open() as document:
            for number in range(count):
                page = document.new_page(width=360 + number % 2 * 40, height=450)
                page.insert_text((20, 30), str(number + 1))
            document.save(path)
        return path

    long_pdf, single_pdf = pdf("Long.pdf", 420), pdf("Single.pdf", 1)
    window = MainWindow(settings, render, QPixmap())
    qtbot.addWidget(window)
    window.resize(880, 560)
    window.show()
    window.activateWindow()
    dock = window._standards_dock()
    dock.toggle_at(window)
    panel = dock.panel
    panel.open_paths([long_pdf, single_pdf])
    first, second = panel.tabs.widget(0), panel.tabs.widget(1)
    qtbot.waitUntil(lambda: first.count == 420 and second.count == 1, timeout=15000)
    assert dock.windowTitle() == "Reference"
    assert isinstance(first.width, QLineEdit) and not first.findChildren(QAbstractSpinBox)
    assert not hasattr(first, "page")

    qtbot.mouseClick(panel.back, Qt.MouseButton.LeftButton)
    view = first.canvas
    qtbot.waitUntil(lambda: view.hasFocus())
    qtbot.waitUntil(lambda: bool(view.requested))
    assert len(view.requested) < 10
    assert second.total.text() == "1 / 1"
    bar = view.verticalScrollBar()
    before = bar.value()
    pos = QPointF(50, 50)
    touchpad = QWheelEvent(pos, QPointF(view.viewport().mapToGlobal(pos.toPoint())),
                           QPoint(0, -42), QPoint(), Qt.MouseButton.NoButton,
                           Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
    QApplication.sendEvent(view.viewport(), touchpad)
    assert touchpad.isAccepted() and bar.value() == before + 42
    bar.setValue(round(view.bottoms[0] * view.zoom) - view.viewport().height() // 2)
    qtbot.waitUntil(lambda: {0, 1}.issubset(view.requested))

    def point(page, x, y):
        return view.mapFromScene(view.pages[page].mapToScene(QPointF(x, y)))

    def key(key, modifiers=Qt.KeyboardModifier.NoModifier):
        qtbot.keyClick(view, key, modifiers)

    ctrl = Qt.KeyboardModifier.ControlModifier
    left = Qt.MouseButton.LeftButton
    qtbot.mouseClick(first.tools["pen"], left)
    first.width.setFocus()
    first.width.selectAll()
    qtbot.keyClicks(first.width, "7")
    qtbot.keyClick(first.width, Qt.Key.Key_Return)
    assert first.style.get("pen")["size"] == 7
    qtbot.mousePress(view.viewport(), left, pos=point(0, 80, 400))
    qtbot.mouseMove(view.viewport(), point(0, 100, 410))
    qtbot.mouseRelease(view.viewport(), left, pos=point(0, 100, 410))
    stroke, = view.ink[0].childItems()
    qtbot.mouseClick(first.tools["text"], left)
    qtbot.mouseClick(view.viewport(), left, pos=point(1, 65, 40))
    assert view.editing is not None
    qtbot.keyClicks(view, "Test annotation")
    key(Qt.Key.Key_Z, ctrl)
    assert view.stack.count() == 1 and stroke.scene() is view.scene()
    key(Qt.Key.Key_Z, ctrl | Qt.KeyboardModifier.ShiftModifier)
    assert view.editing.toPlainText() == "Test annotation"
    key(Qt.Key.Key_Escape)
    annotation, = view.ink[1].childItems()
    key(Qt.Key.Key_Z, ctrl)
    assert annotation.scene() is None and stroke.scene() is view.scene()
    key(Qt.Key.Key_Z, ctrl | Qt.KeyboardModifier.ShiftModifier)
    assert annotation.parentItem() is view.ink[1]
    undo, redo = window.image_actions[:2]
    assert undo.isEnabled() and redo.isEnabled()
    undo.trigger()
    assert annotation.scene() is None
    redo.trigger()
    assert annotation.parentItem() is view.ink[1]

    qtbot.mouseClick(first.tools["eraser"], left)
    qtbot.mouseClick(view.viewport(), left, pos=point(1, 85, 49))
    assert annotation.scene() is None
    key(Qt.Key.Key_Z, ctrl)
    assert annotation.parentItem() is view.ink[1]
    qtbot.mouseClick(panel.forward, left)
    qtbot.keyClick(second.canvas, Qt.Key.Key_Z, ctrl)
    assert annotation.parentItem() is view.ink[1]
    qtbot.mouseClick(panel.back, left)
    key(Qt.Key.Key_Z, ctrl | Qt.KeyboardModifier.ShiftModifier)
    assert annotation.scene() is None
    key(Qt.Key.Key_Z, ctrl)
    assert annotation.parentItem() is view.ink[1]
    pos = annotation.pos()
    view.set_zoom(view.zoom * 1.5)
    window.resize(950, 640)
    assert annotation.pos() == pos

    bar.setValue(bar.maximum())
    qtbot.waitUntil(lambda: 419 in view.requested)
    assert first.total.text() == "420 / 420"
    index = panel.tabs.currentIndex()
    key(Qt.Key.Key_Right)
    assert panel.tabs.currentIndex() == index and bar.value() == bar.maximum()
    panel.dock.toggle_floating()
    panel.dock.resize(panel.minimumWidth(), 500)
    qtbot.waitUntil(panel.dock.isFloating)
    for widget in (*first.tools.values(), first.width, first.undo, first.redo, first.zoom):
        assert widget.geometry().right() < first.rect().right()
    panel.dock.toggle_floating()
    assert len(view.requested) < 10
    # Reopening uses the same local settings and page coordinates.
    panel.close_tab(0)
    panel.open_paths([long_pdf])
    reopened = panel.tabs.currentWidget()
    qtbot.waitUntil(lambda: reopened.count == 420)
    restored, = reopened.canvas.ink[1].childItems()
    assert restored.toPlainText() == "Test annotation"
    assert restored.pos() == pos
    window.close()


def workbook_fixture(path):
    """Write a two-sheet XLSX with both formula cache states, without Office."""
    from zipfile import ZIP_DEFLATED, ZipFile

    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with ZipFile(path, "w", ZIP_DEFLATED) as out:
        types = "application/vnd.openxmlformats-officedocument.spreadsheetml"
        out.writestr("[Content_Types].xml", f"""<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
            <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
            <Default Extension="xml" ContentType="application/xml"/>
            <Override PartName="/xl/workbook.xml" ContentType="{types}.sheet.main+xml"/>
            <Override PartName="/xl/worksheets/one.xml" ContentType="{types}.worksheet+xml"/>
            <Override PartName="/xl/worksheets/two.xml" ContentType="{types}.worksheet+xml"/>
            <Override PartName="/xl/styles.xml" ContentType="{types}.styles+xml"/>
            <Override PartName="/xl/sharedStrings.xml" ContentType="{types}.sharedStrings+xml"/>
            </Types>""")
        out.writestr("_rels/.rels", f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        links = [("worksheet", "worksheets/one.xml"), ("worksheet", "worksheets/two.xml"),
                 ("styles", "styles.xml"), ("sharedStrings", "sharedStrings.xml")]
        relationships = "".join(f'<Relationship Id="rId{i}" Type="{rel}/{kind}" Target="{target}"/>'
                                for i, (kind, target) in enumerate(links, 1))
        out.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{pkg}">{relationships}</Relationships>')
        out.writestr("xl/workbook.xml", f"""<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>
            <sheet name="Inspect" sheetId="1" r:id="rId1"/><sheet name="Notes" sheetId="2" r:id="rId2"/>
            </sheets></workbook>""")
        out.writestr("xl/styles.xml", f"""<styleSheet xmlns="{ns}"><cellXfs count="4">
            <xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="10"/><xf numFmtId="4"/>
            </cellXfs></styleSheet>""")
        out.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}"><si><r><t>Batch </t></r><r><t>Ω</t></r></si></sst>')
        out.writestr("xl/worksheets/one.xml", f"""<worksheet xmlns="{ns}"><sheetData><row r="1">
            <c r="A1" t="s"><v>0</v></c><c r="B1" s="1"><v>46000</v></c>
            <c r="C1" s="2"><v>0.125</v></c><c r="D1" s="3"><v>1234.5</v></c>
            <c r="E1"><f>2+3</f><v>5</v></c><c r="F1"><f>4+3</f><v/></c>
            </row><row r="60000"><c r="AB60000" t="inlineStr"><is><t>Scroll target</t></is></c></row>
            </sheetData></worksheet>""")
        out.writestr("xl/worksheets/two.xml", f"""<worksheet xmlns="{ns}"><sheetData><row r="1">
            <c r="A1" t="inlineStr"><is><t>Second worksheet</t></is></c></row></sheetData></worksheet>""")
    return path


def test_xlsx_reference_grid_and_pdf(qtbot, render, theme, settings, tmp_path, monkeypatch):
    from eng_viewer.core import pdf_tools

    monkeypatch.setattr(pdf_tools, "find_office", lambda: None)
    workbook = workbook_fixture(tmp_path / "Quality checks.xlsx")
    original = workbook.read_bytes()
    pdf = tmp_path / "Reference.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.save(pdf)
    window = MainWindow(settings, render, QPixmap())
    qtbot.addWidget(window)
    window.resize(880, 560)
    window.show()
    dock = window._standards_dock()
    dock.toggle_at(window)
    panel = dock.panel
    panel.open_paths([workbook, pdf])
    book, drawing = panel.tabs.widget(0), panel.tabs.widget(1)
    qtbot.waitUntil(lambda: 0 in book.grids and drawing.count == 1, timeout=15000)
    qtbot.mouseClick(panel.back, Qt.MouseButton.LeftButton)
    grid = book.grids[0]
    model = grid.model()
    assert (model.rowCount(), model.columnCount()) == (60000, 28)
    assert model.headerData(27, Qt.Orientation.Horizontal) == "AB"
    assert [model.index(0, col).data() for col in range(6)] == [
        "Batch Ω", "2025-12-09", "12.50%", "1,234.50", "5", "Result unavailable",
    ]
    grid.setCurrentIndex(model.index(0, 0))
    qtbot.keyClick(grid, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    qtbot.keyClick(grid, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "Batch Ω\t2025-12-09"
    qtbot.keyClick(grid, Qt.Key.Key_PageDown)
    assert grid.currentIndex().row() > 0
    distant = model.index(59999, 27)
    qtbot.keyClick(grid, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    assert grid.currentIndex() == distant
    assert grid.visualRect(distant).intersects(grid.viewport().rect())
    assert distant.data() == "Scroll target"
    assert 1 not in book.grids
    book.canvas.setCurrentIndex(1)
    qtbot.waitUntil(lambda: 1 in book.grids, timeout=15000)
    assert book.grids[1].model().index(0, 0).data() == "Second worksheet"
    qtbot.mouseClick(panel.forward, Qt.MouseButton.LeftButton)
    assert panel.tabs.currentWidget() is drawing
    qtbot.mouseClick(panel.back, Qt.MouseButton.LeftButton)
    assert panel.tabs.currentWidget() is book and book.canvas.currentIndex() == 1
    panel.close_tab(0)
    assert workbook.read_bytes() == original
    window.close()
