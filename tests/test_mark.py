"""MARK: tools, callouts and comments, undo/redo, exact-size previews, output."""
import pymupdf
import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from eng_viewer.ui.mark.canvas import MarkCanvas
from eng_viewer.ui.mark.comments import CommentsPanel
from eng_viewer.ui.mark.items import AnnotationMixin, BubbleItem, number_path
from eng_viewer.ui.mark.tool_options import RANGES, PreviewSlider, ToolSettings, paint_sample, sample_extent
from eng_viewer.ui.page_view import PageRef


@pytest.fixture()
def canvas(qtbot, render, theme, corpus):
    path = str(corpus["a3"]) if "a3" in corpus else str(corpus["multi"])
    page = pymupdf.open(path)[0]
    c = MarkCanvas(render, ToolSettings())
    c.resize(1000, 700)
    qtbot.addWidget(c)
    c.show()
    c.set_page(PageRef(path, 0, page.rect.width, page.rect.height, PageRef.stamp_for(path)))
    c.set_zoom_percent(100, animate=False)
    c.centerOn(QPointF(page.rect.width / 2, page.rect.height / 2))
    panel = CommentsPanel(c, None)
    qtbot.addWidget(panel)
    c._panel = panel
    return c


def click(c, pos: QPoint):
    QTest.mouseClick(c.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)


def drag(c, pts, mods=Qt.KeyboardModifier.NoModifier):
    QTest.mousePress(c.viewport(), Qt.MouseButton.LeftButton, mods, pts[0])
    for p in pts[1:]:
        QTest.mouseMove(c.viewport(), p)
    QTest.mouseRelease(c.viewport(), Qt.MouseButton.LeftButton, mods, pts[-1])


def test_bubbles_number_sequentially_and_reuse_last_number(canvas):
    canvas.set_tool("bubble")
    for x in (200, 300, 400):
        click(canvas, QPoint(x, 300))
    assert [b.number for b in canvas.bubbles()] == [1, 2, 3]
    canvas.delete_bubble(canvas.bubbles()[-1])
    assert canvas.next_number() == 3  # deleting the last frees its number
    canvas.delete_bubble(canvas.bubbles()[0])
    assert canvas.next_number() == 3  # a gap in the middle keeps the sequence
    canvas.renumber()
    assert [b.number for b in canvas.bubbles()] == [1]
    canvas.undo_stack.undo()
    assert [b.number for b in canvas.bubbles()] == [2]


def test_comments_link_remove_and_restore(canvas):
    canvas.set_tool("bubble")
    click(canvas, QPoint(250, 250))
    click(canvas, QPoint(350, 250))
    b1, b2 = canvas.bubbles()
    panel = canvas._panel
    panel.rows[id(b1)].edit.setPlainText("First comment")
    panel.rows[id(b2)].edit.setPlainText("Second comment")
    assert canvas.comments_text() == "1. First comment\n2. Second comment"
    # Red X removes the comment only.
    panel.rows[id(b1)].remove_btn.click()
    assert b1.scene() is canvas.scene_obj and b1.comment_removed
    assert canvas.comments_text() == "2. Second comment"
    canvas.undo_stack.undo()
    assert canvas.comments_text() == "1. First comment\n2. Second comment"
    # Deleting the bubble deletes its linked comment; undo restores both.
    canvas.delete_bubble(b2)
    assert id(b2) not in panel.rows and canvas.comments_text() == "1. First comment"
    canvas.undo_stack.undo()
    assert canvas.comments_text() == "1. First comment\n2. Second comment"
    # Selecting a comment selects (and pulses) its bubble; selecting the bubble highlights the row.
    canvas.set_tool("select")
    panel.rows[id(b2)].activated.emit(b2)
    assert b2.isSelected()
    assert panel.rows[id(b2)].selected and not panel.rows[id(b1)].selected


def test_drawing_tools_and_undo_redo(canvas):
    canvas.set_tool("pen")
    drag(canvas, [QPoint(100 + i * 5, 200 + (i % 5) * 3) for i in range(30)])
    canvas.set_tool("highlighter")
    drag(canvas, [QPoint(100, 260), QPoint(300, 260)])
    canvas.set_tool("arrow")
    drag(canvas, [QPoint(400, 400), QPoint(500, 330)])
    canvas.set_tool("circle")
    drag(canvas, [QPoint(600, 200), QPoint(680, 280)])
    canvas.set_tool("arrow")
    drag(canvas, [QPoint(400, 400), QPoint(401, 401)])  # too short: discarded
    kinds = sorted(type(i).__name__ for i in canvas.annotations())
    assert kinds == ["ArrowItem", "EllipseItem", "StrokeItem", "StrokeItem"]
    for _ in range(4):
        canvas.undo_stack.undo()
    assert canvas.annotations() == []
    for _ in range(4):
        canvas.undo_stack.redo()
    assert len(canvas.annotations()) == 4


def test_select_move_style_and_delete(canvas):
    canvas.set_tool("circle")
    drag(canvas, [QPoint(300, 300), QPoint(360, 360)])
    item = canvas.annotations()[0]
    canvas.set_tool("select")
    item.setSelected(True)
    before = item.pos()
    QTest.keyClick(canvas, Qt.Key.Key_Right)
    QTest.keyClick(canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    assert item.pos().x() == pytest.approx(before.x() + 11)
    canvas.undo_stack.undo()  # nudges merge into a single step
    assert item.pos() == before
    canvas.apply_style_to_selection({"color": QColor("#1E5ADC"), "width": 5.0}, "Style")
    assert item.width == 5.0 and item.color.name().lower() == "#1e5adc"
    canvas.undo_stack.undo()
    assert item.width != 5.0
    canvas.delete_selected()
    assert canvas.annotations() == []


def test_eraser_drag_removes_touched_items(canvas):
    canvas.set_tool("circle")
    drag(canvas, [QPoint(200, 200), QPoint(260, 260)])
    drag(canvas, [QPoint(500, 200), QPoint(560, 260)])
    canvas.set_tool("eraser")
    drag(canvas, [QPoint(200, 230), QPoint(230, 230)])  # touches only the first circle's outline
    assert len(canvas.annotations()) == 1
    canvas.undo_stack.undo()
    assert len(canvas.annotations()) == 2


def test_ghost_and_preview_match_placed_size(canvas):
    """The live previews use the same geometry as the annotation that gets placed."""
    canvas.ts.set("bubble", "size", 80.0)
    canvas.set_tool("bubble")
    canvas.show_size_preview(True)
    ghost = canvas.ghost.boundingRect()
    click(canvas, QPoint(500, 350))
    bubble = canvas.bubbles()[0]
    assert ghost == bubble.boundingRect()
    # Preview card at zoom z draws the bubble z × 80 px across, i.e. what the canvas shows.
    z = canvas.zoom
    ext = sample_extent("bubble", {"size": 80.0}, z)
    screen = canvas.mapFromScene(bubble.sceneBoundingRect()).boundingRect()
    assert abs(ext.width() - (80.0 + BubbleItem.stroke_for(80.0)) * z) < 5
    assert abs(screen.width() - bubble.boundingRect().width() * z) < 3


def test_preview_slider_log_range_and_rounding(qtbot):
    lo, hi, _unit, _ = RANGES["bubble"]
    s = PreviewSlider(lo, hi, 30)
    qtbot.addWidget(s)
    assert s.value() == 30
    s.set_value(lo)
    assert s.value() == lo
    s.set_value(hi)
    assert s.value() == hi
    s.set_value(30)
    s.step(1)
    assert s.value() > 30
    assert hi / lo >= 50  # genuinely wide bubble range


def test_number_is_centred_in_bubble():
    for n in (1, 7, 12, 88, 104):
        path = number_path(n, 40.0, "Helvetica")
        c = path.boundingRect().center()
        assert abs(c.x()) < 0.01 and abs(c.y()) < 0.01
        assert path.boundingRect().width() < 40.0 * 0.75


def test_composite_is_vector_sharp_and_hides_ui_chrome(canvas):
    canvas.set_tool("bubble")
    click(canvas, QPoint(500, 350))
    bubble = canvas.bubbles()[0]
    canvas.set_tool("select")
    bubble.setSelected(True)
    canvas.ghost.setVisible(True)
    ref = canvas.page_ref
    scale = 300 / 72
    img = QImage(round(ref.width * scale), round(ref.height * scale), QImage.Format.Format_RGB888)
    img.fill(QColor("white"))
    canvas.render_annotations(img, ref.width, ref.height)
    # Red ring at the bubble's edge, rendered at 300 DPI.
    c = bubble.scenePos() * scale
    r = bubble.diameter / 2 * scale
    edge = img.pixelColor(round(c.x() + r), round(c.y()))
    assert edge.red() > 180 and edge.green() < 90
    # No selection outline (accent blue) anywhere around it.
    pad = round((bubble.diameter / 2 + 6) * scale)
    blues = 0
    for dx in range(-pad, pad, 3):
        col = img.pixelColor(round(c.x() + dx), round(c.y() - pad + 2))
        blues += col.blue() > 200 and col.red() < 120
    assert blues == 0
    assert AnnotationMixin.exporting is False
    assert canvas.page_item.isVisible()
