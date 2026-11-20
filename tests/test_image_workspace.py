from pathlib import Path
import sys

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from PySide6.QtGui import QPixmap

from eng_viewer.core.image_document import ImageDocument
from eng_viewer.ui.main_window import MainWindow


@pytest.fixture
def workspace(qtbot, render, theme, settings, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda parent, title, msg: errors.append(msg))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **kw: QMessageBox.StandardButton.Discard)
    w = MainWindow(settings, render, QPixmap())
    w.resize(1400, 940)
    qtbot.addWidget(w)
    w.show()
    w.set_mode('image')
    editor = w.image_editor
    editor.add_document(ImageDocument.from_image(Image.new('RGBA', (320, 240), (200, 100, 50, 255))), 'Test')
    yield editor, errors
    qtbot.waitUntil(lambda: editor._job is None, timeout=20000)
    for state in editor.states:
        state.doc._saved_revision = state.doc.revision
    w.close()
    assert not errors


def wait(qtbot, editor):
    qtbot.waitUntil(lambda: editor._job is None, timeout=20000)


def test_source_resolution_selection_mask_undo(workspace, qtbot):
    e, _ = workspace
    assert e.canvas.item.pixmap().width() == 320
    e.canvas.set_zoom(1600)
    assert e.canvas.item.pixmap().width() == 320
    e.on_gesture('rect', [(30, 30), (120, 100)], Qt.KeyboardModifier.NoModifier)
    wait(qtbot, e)
    assert e.selection.getbbox() == (30, 30, 120, 100)
    e.delete_selection()
    wait(qtbot, e)
    assert e.doc.composite().getpixel((40, 40))[3] == 0
    e.history(False)
    wait(qtbot, e)
    assert e.doc.composite().getpixel((40, 40))[3] == 255


def test_annotations_reuse_mark_and_survive_project(workspace, qtbot, tmp_path, monkeypatch):
    e, _ = workspace
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, 'getMultiLineText', lambda *a, **kw: ('Inspect edge', True))
    e.size.setValue(26)
    e.on_gesture('bubble', [(50, 50)], Qt.KeyboardModifier.NoModifier)
    wait(qtbot, e)
    assert e.doc.active_layer.properties['comment'] == 'Inspect edge'
    assert e.doc.active_layer.properties['number'] == 1
    path = tmp_path / 'project.engimg'
    e.doc.save_project(path)
    reopened = ImageDocument.load_project(path)
    assert reopened.active_layer.properties['comment'] == 'Inspect edge'
    e.send_to_queue()
    entry = e.owner.queue.model.entries[-1]
    assert entry.comments == '1. Inspect edge'
    assert entry.marked_image.width() == 320
    assert entry.comment_count == 1


def test_crop_and_layer_movement(workspace, qtbot):
    e, _ = workspace
    from PySide6.QtCore import QRectF
    e.canvas.crop_rect = QRectF(20, 10, 100, 80)
    e.apply_crop()
    wait(qtbot, e)
    assert e.doc.size == (100, 80)
    assert e.doc.active_layer.x == -20
    e.on_gesture('move', [(0, 0), (12, 8)], Qt.KeyboardModifier.NoModifier)
    wait(qtbot, e)
    assert (e.doc.active_layer.x, e.doc.active_layer.y) == (-8, -2)


def test_export_dialog_actual_bytes(workspace, qtbot):
    e, _ = workspace
    from eng_viewer.ui.image_editor.dialogs import ExportDialog
    d = ExportDialog(e.doc, e)
    qtbot.addWidget(d)
    d.show()
    d.format.setCurrentText('JPEG')
    d.target.setCurrentIndex(1)
    qtbot.waitUntil(lambda: d.result is not None and d._job is None, timeout=20000)
    assert d.result.data.startswith(b'\xff\xd8')
    assert len(d.result.data) <= 100000
    d.reject()


def test_open_image_from_main_preserves_viewer(workspace, qtbot, tmp_path):
    e, _ = workspace
    path = tmp_path / 'open.png'
    Image.new('RGB', (120, 90), 'red').save(path)
    e.owner.open_paths([path])
    qtbot.waitUntil(lambda: e.owner.doc_path == str(path) and e.owner.page_count == 1, timeout=20000)
    wait(qtbot, e)
    assert e.owner._mode == 'image'
    assert e.doc.size == (120, 90)
    e.owner.set_mode('viewer')
    assert e.owner.view._ref.path == str(path)


def test_magic_eraser_click_hides_background_and_restore_is_possible(workspace, qtbot):
    e, _ = workspace
    from PIL import ImageDraw
    image = Image.new('RGBA', (200, 150), (255, 255, 255, 255))
    ImageDraw.Draw(image).rectangle((60, 40, 139, 109), fill=(10, 120, 60, 255))
    e.add_document(ImageDocument.from_image(image), 'Magic')
    e.set_tool('magic')
    assert e.magic_panel.isVisibleTo(e) and not e.brush_panel.layout().isRowVisible(0)
    assert e.tool_buttons['rect'].icon().cacheKey() != e.tool_buttons['move'].icon().cacheKey()
    e.on_gesture('magic', [(5, 5)], Qt.KeyboardModifier.NoModifier)
    wait(qtbot, e)
    composite = e.doc.composite()
    assert composite.getpixel((5, 5))[3] == 0 and composite.getpixel((100, 75))[3] == 255
    assert e.doc.active_layer.image.getpixel((5, 5)) == (255, 255, 255, 255)  # pixels kept, only masked
    e.history(False)
    wait(qtbot, e)
    assert e.doc.composite().getpixel((5, 5))[3] == 255


def test_grid_and_rulers_toggle_and_paint(workspace, qtbot):
    e, _ = workspace
    assert not e.canvas.grid and e.canvas.rulers
    e.grid_box.setChecked(True)
    assert e.canvas.grid
    e._toggle_view('rulers')
    assert not e.canvas.rulers and not e.rulers_box.isChecked()
    e._toggle_view('rulers')
    e.canvas.set_zoom(1600)
    e.canvas.viewport().grab()  # paints pixel grid, unit grid and ruler ticks without error


def test_on_canvas_text_edit_and_transform(workspace, qtbot, tmp_path, qapp):
    from PySide6.QtCore import QPointF
    from eng_viewer.ui.image_editor.transform import frame_point

    e, _ = workspace
    e.canvas.rulers = False
    e.canvas.snap = False
    e.canvas.set_zoom(200)
    e.canvas.centerOn(155, 115)
    e.set_tool("text")
    canvas = e.canvas
    viewport = canvas.viewport()
    ctrl = Qt.KeyboardModifier.ControlModifier
    left = Qt.MouseButton.LeftButton
    position = canvas.mapFromScene(QPointF(50, 65))
    qtbot.mouseClick(viewport, left, pos=position)
    editor = canvas.text_editor
    assert editor is not None and editor.hasFocus()
    assert canvas.mapFromScene(editor.mapToScene(QPointF())) == position
    qtbot.keyClicks(canvas, "Hello world")
    qtbot.keyClick(canvas, Qt.Key.Key_Left)
    qtbot.keyClick(canvas, Qt.Key.Key_Backspace)
    qapp.clipboard().setText("L")
    qtbot.keyClick(canvas, Qt.Key.Key_V, ctrl)
    qtbot.keyClick(canvas, Qt.Key.Key_Z, ctrl)
    assert editor.toPlainText() == "Hello word"
    qtbot.keyClick(canvas, Qt.Key.Key_Z, ctrl | Qt.KeyboardModifier.ShiftModifier)
    assert editor.toPlainText() == "Hello worLd"
    qtbot.keyClick(canvas, Qt.Key.Key_End)
    qtbot.keyClick(canvas, Qt.Key.Key_Return)
    qtbot.keyClicks(canvas, "Second line")
    assert editor.toPlainText() == "Hello worLd\nSecond line"
    qtbot.mouseClick(e.font_size.lineEdit(), left)
    e.font_size.lineEdit().selectAll()
    qtbot.keyClicks(e.font_size.lineEdit(), "32")
    assert canvas.text_editor is editor and editor.font().pixelSize() == 32
    qtbot.mouseClick(e.commit_text_button, left)
    wait(qtbot, e)
    assert e.doc.active_layer.properties["text"] == "Hello worLd\nSecond line"
    assert e.doc.active_layer.properties["width"] == 32
    assert len(e.doc.layers) == 2

    e.set_tool("move")
    frame = canvas.layer_frame
    corner = canvas.mapFromScene(QPointF(*frame_point(frame, .5, .5)))
    target = canvas.mapFromScene(QPointF(*frame_point(frame, .8, .8)))
    qtbot.mousePress(viewport, left, pos=corner)
    assert canvas._drag is not None
    qtbot.mouseMove(viewport, target)
    assert not canvas.preview_items[1].transform().isIdentity()
    qtbot.mouseRelease(viewport, left, pos=target)
    wait(qtbot, e)
    assert e.doc.active_layer.image.width > frame[2]
    path = tmp_path / "point text.engimg"
    e.doc.save_project(path)
    reopened = ImageDocument.load_project(path)
    assert reopened.composite().tobytes() == e.doc.composite().tobytes()
    e.add_document(reopened, "Reopened")
    e.set_tool("text")
    layer = e.doc.active_layer
    position = canvas.mapFromScene(QPointF(layer.x + 8, layer.y + 8))
    qtbot.mouseClick(viewport, left, pos=position)
    assert canvas.text_editor.toPlainText() == layer.properties["text"]
    qtbot.keyClick(canvas, Qt.Key.Key_A, ctrl)
    qtbot.keyClicks(canvas, "Canceled")
    qtbot.keyClick(canvas, Qt.Key.Key_Escape)
    assert e.doc.active_layer.properties["text"] == "Hello worLd\nSecond line"

    qtbot.mouseClick(viewport, left, pos=position)
    qtbot.keyClick(canvas, Qt.Key.Key_A, ctrl)
    qtbot.keyClicks(canvas, "Replacement text")
    qtbot.keyClick(canvas, Qt.Key.Key_Return, ctrl)
    wait(qtbot, e)
    assert e.doc.active_layer.properties["text"] == "Replacement text"
    assert e.doc.active_layer.image.getbbox() is not None
    qtbot.keyClick(canvas, Qt.Key.Key_Z, ctrl)
    wait(qtbot, e)
    assert e.doc.active_layer.properties["text"] == "Hello worLd\nSecond line"
    qtbot.keyClick(canvas, Qt.Key.Key_Y, ctrl)
    wait(qtbot, e)
    assert e.doc.active_layer.properties["text"] == "Replacement text"

    qtbot.mouseClick(viewport, left, pos=canvas.mapFromScene(QPointF(10, 10)))
    qtbot.keyClick(canvas, Qt.Key.Key_Return, ctrl)
    assert len(e.doc.layers) == 2

@pytest.mark.skipif(sys.platform != "win32", reason="Windows number fields")
def test_numeric_fields_fit_without_stepper_buttons(workspace, qtbot, qapp):
    if qapp.platformName() != "windows":
        pytest.skip("Requires the native Windows fonts and Qt platform")
    from PySide6.QtWidgets import QStyle, QStyleOptionSpinBox

    e, _ = workspace
    e.owner.resize(880, 590)
    e.set_tool("move")
    qtbot.waitUntil(lambda: e.transform_fields["x"].isVisible())
    scroll_view = e.side_scroll.viewport()
    for field, value in zip(e.transform_fields.values(), (-12345, -6789, 1536, 1024, -123.4)):
        field.setValue(value)
        option = QStyleOptionSpinBox()
        field.initStyleOption(option)
        for stepper in (QStyle.SubControl.SC_SpinBoxUp, QStyle.SubControl.SC_SpinBoxDown):
            assert field.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, stepper, field).isEmpty()
        entry = field.lineEdit()
        margins = entry.textMargins()
        assert entry.width() >= entry.fontMetrics().horizontalAdvance(entry.text()) + margins.left() + margins.right()
        assert field.mapTo(scroll_view, field.rect().topRight()).x() < scroll_view.width()
