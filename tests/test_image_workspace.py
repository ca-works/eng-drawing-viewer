from pathlib import Path

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
