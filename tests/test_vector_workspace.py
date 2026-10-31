from PIL import Image
import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QApplication, QWidget

from eng_viewer.core.vector_document import VectorDocument, VectorObject, commands_from_path
from eng_viewer.ui.vector_editor import VectorWorkspace


def workspace(qtbot, theme):
    owner = QWidget(); owner.resize(1200, 800)
    editor = VectorWorkspace(owner); editor.resize(1200, 800)
    qtbot.addWidget(owner); editor.show(); owner.show()
    QApplication.processEvents(); editor.fit()
    return editor


def test_workspace_draw_shape_edit_node_undo_and_group(qtbot, theme):
    editor = workspace(qtbot, theme)
    editor.set_tool("rectangle")
    start = editor.canvas.mapFromScene(QPointF(20, 20)); end = editor.canvas.mapFromScene(QPointF(60, 50))
    qtbot.mousePress(editor.canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(editor.canvas.viewport(), end)
    qtbot.mouseRelease(editor.canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert len(editor.document.objects) == 1
    original = editor.document.objects[0].path().boundingRect()
    assert abs(original.width() - 40) < 1
    editor.set_tool("nodes")
    assert editor.nodes
    node = editor.nodes[1]
    before = editor.document.objects[0].commands[1][1]
    point = editor.canvas.mapFromScene(node.pos())
    qtbot.mousePress(editor.canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.mouseMove(editor.canvas.viewport(), point + QPoint(15, 0))
    qtbot.mouseRelease(editor.canvas.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(15, 0))
    assert editor.document.objects[0].commands[1][1] > before
    editor.undo()
    assert editor.document.objects[0].commands[1][1] == before
    editor.set_tool("select")
    editor.items[editor.document.objects[0].id].setSelected(True)
    editor.duplicate()
    assert len(editor.document.objects) == 2
    for item in editor.items.values(): item.setSelected(True)
    editor.group()
    assert editor.document.objects[0].group == editor.document.objects[1].group


def test_workspace_import_svg_and_image_reference(qtbot, theme, tmp_path):
    editor = workspace(qtbot, theme)
    source = tmp_path / "shape.svg"
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="30mm" viewBox="0 0 50 30"><rect x="5" y="5" width="20" height="10"/></svg>')
    assert editor.open_file(source)
    assert editor.document.objects[0].path().boundingRect() == QRectF(5, 5, 20, 10)
    editor.import_image(Image.new("RGBA", (96, 48), "red"))
    assert editor.document.width == pytest.approx(25.4)
    assert editor.raster.size == (96, 48)


def test_workspace_properties_and_visibility(qtbot, theme):
    editor = workspace(qtbot, theme)
    path = QPainterPath(); path.addEllipse(QRectF(10, 10, 20, 20)); editor.add_path(path, "Ellipse")
    editor.fill.setText("#ff0000"); editor.stroke_width.setValue(.8); editor.name.setText("Cut boundary")
    editor.apply_properties()
    assert editor.document.objects[0].fill == "#ff0000"
    assert editor.document.objects[0].stroke_width == .8
    assert editor.document.objects[0].name == "Cut boundary"
    editor.object_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert not editor.document.objects[0].visible
    editor.undo()
    assert editor.document.objects[0].visible


def test_trace_preview_presets_and_units_undo(qtbot, theme):
    from PIL import ImageDraw
    from eng_viewer.ui.vector_editor.trace_dialog import TraceDialog
    source = Image.new("RGB", (200, 120), "white")
    ImageDraw.Draw(source).rectangle((30, 20, 160, 95), fill="black")
    dialog = TraceDialog(source)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: "contours" in dialog.status.text(), timeout=10000)
    assert dialog.after.pixmap() is not None
    dialog.preset.setCurrentText("General vector illustration")
    assert dialog.options()["mode"] == "color"
    dialog.reject()
    editor = workspace(qtbot, theme)
    editor.units.setCurrentText("in")
    assert editor.document.units == "in" and editor.document.dirty
    editor.undo()
    assert editor.document.units == editor.units.currentText() == "mm"


def test_ruler_steps_are_round_numbers_with_subdivisions():
    from eng_viewer.ui.rulers import grid_step, nice_step, ruler_steps
    assert [nice_step(v) for v in (0.7, 1, 1.3, 3, 7, 40)] == [1, 1, 2, 5, 10, 50]
    assert ruler_steps(1) == (100, 10)  # 1 px per unit: labels every 100, ticks every 10
    major, divisions = ruler_steps(3.78)  # mm at 100 %
    assert major == 20 and divisions == 4
    assert grid_step(10, 14) == 2
