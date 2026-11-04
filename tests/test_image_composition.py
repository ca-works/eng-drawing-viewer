"""Layer composition: transforms, clipboard, drag and drop, the Move tool and the Paint Bucket."""
import io
import math
import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent, QPixmap
from PySide6.QtWidgets import QMessageBox

from eng_viewer.core import image_clipboard as clip
from eng_viewer.core.image_document import (ImageDocument, ImageLayer, affine_about, affine_translate)
from eng_viewer.ui.image_editor import transform as TF
from eng_viewer.ui.main_window import MainWindow


# ---------------------------------------------------------------- document model
def _two_tone(size=(200, 100)):
    image = Image.new("RGBA", size, (220, 30, 30, 255))
    ImageDraw.Draw(image).rectangle((0, 0, size[0] // 2 - 1, size[1] - 1), fill=(30, 60, 220, 255))
    return image


def test_repeated_transforms_resample_once_from_original_pixels():
    doc = ImageDocument(800, 600)
    doc.insert_layer(ImageLayer("photo", _two_tone(), x=300, y=250))
    index = doc.active_index
    cx, cy, *_ = doc.active_layer.frame
    for _ in range(12):
        doc.transform_layer(index, affine_about((cx, cy), 1.05, 1.05, 7.5))
    layer = doc.active_layer
    assert layer.original is not None and layer.original.image.size == (200, 100)
    fx, fy, w, h, angle = layer.frame
    assert (round(fx), round(fy)) == (400, 300)
    assert w == pytest.approx(200 * 1.05 ** 12, rel=1e-6) and angle == pytest.approx(90)
    # One combined transform gives the same pixels: nothing compounds.
    single = ImageDocument(800, 600)
    single.insert_layer(ImageLayer("photo", _two_tone(), x=300, y=250))
    single.transform_layer(single.active_index, affine_about((cx, cy), 1.05 ** 12, 1.05 ** 12, 90))
    a = np.asarray(layer.image, np.int16)
    b = np.asarray(single.active_layer.image, np.int16)
    assert a.shape == b.shape and np.abs(a - b).max() <= 2
    doc.reset_layer_transform(index)
    assert doc.active_layer.image.tobytes() == _two_tone().tobytes()
    assert doc.active_layer.frame[:2] == pytest.approx((400, 300))


def test_transform_keeps_mask_aligned_and_undo_restores():
    doc = ImageDocument(400, 400)
    mask = Image.new("L", (100, 100), 255)
    ImageDraw.Draw(mask).rectangle((0, 0, 49, 99), fill=0)  # left half hidden
    doc.insert_layer(ImageLayer("masked", Image.new("RGBA", (100, 100), (0, 200, 0, 255)), mask=mask, x=150, y=150))
    doc.transform_layer(doc.active_index, affine_about((200, 200), 2, 2))
    composite = doc.composite()
    assert composite.getpixel((140, 200))[3] == 0      # still hidden, now twice as wide
    assert composite.getpixel((260, 200))[3] == 255
    assert doc.undo() and doc.composite().getpixel((260, 200))[3] == 0


def test_offset_place_reveal_fit_center_and_copy():
    doc = ImageDocument(300, 200)
    big = ImageLayer("big", Image.new("RGBA", (600, 400), (10, 20, 30, 255)), x=-150, y=-100)
    doc.insert_layer(big)
    index = doc.active_index
    assert doc.layers[index].image.size == (600, 400)  # never downscaled when placed
    assert doc.reveal_all() and doc.size == (600, 400) and doc.layers[index].x == 0
    doc.undo()
    doc.fit_layer_to_canvas(index)
    cx, cy, w, h, _ = doc.layers[index].frame
    assert (w, h) == pytest.approx((300, 200)) and (cx, cy) == pytest.approx((150, 100))
    doc.offset_layer(index, 7, -3)
    assert doc.center_layer(index) and doc.layers[index].frame[:2] == pytest.approx((150, 100))
    selection = Image.new("L", doc.size, 0)
    ImageDraw.Draw(selection).rectangle((10, 20, 59, 69), fill=255)
    piece = doc.copy_layer(index, selection)
    assert (piece.x, piece.y, piece.image.size) == (10, 20, (50, 50))
    assert doc.clear_selection_pixels(index, selection, "Cut")
    assert doc.composite().getpixel((30, 40))[3] == 0 and doc.composite().getpixel((100, 100))[3] == 255
    whole = doc.copy_layer(index)
    assert whole.id != doc.layers[index].id and whole.image is doc.layers[index].image


def test_multilayer_project_round_trip_keeps_transforms_and_v1_stays_v1(tmp_path):
    doc = ImageDocument(320, 240)
    doc.layers[0].image = Image.new("RGBA", (320, 240), (240, 240, 240, 255))
    doc.insert_layer(ImageLayer("subject", _two_tone((80, 40)), mask=Image.new("L", (80, 40), 200), x=40, y=50,
                                blend="multiply", opacity=0.8))
    doc.transform_layer(1, affine_about((80, 70), 1.5, 1.5, 30))
    doc.insert_layer(ImageLayer("note", Image.new("RGBA", (30, 30), (255, 200, 0, 255)), x=250, y=180, blend="soft-light"))
    path = tmp_path / "composition.engimg"
    doc.save_project(path)
    again = ImageDocument.load_project(path)
    assert [l.name for l in again.layers] == ["Background", "subject", "note"]
    assert again.layers[1].original is not None and again.layers[1].frame == pytest.approx(doc.layers[1].frame)
    assert (again.layers[1].blend, again.layers[1].opacity, again.layers[2].blend) == ("multiply", 0.8, "soft-light")
    assert again.composite().tobytes() == doc.composite().tobytes()
    # Reopened layers still transform from their original pixels.
    again.reset_layer_transform(1)
    assert again.layers[1].image.tobytes() == _two_tone((80, 40)).tobytes()
    import json
    import zipfile
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read("manifest.json"))["version"] == 2
    plain = ImageDocument(10, 10)
    plain.save_project(tmp_path / "plain.engimg")
    with zipfile.ZipFile(tmp_path / "plain.engimg") as archive:
        assert json.loads(archive.read("manifest.json"))["version"] == 1


def test_frame_geometry_handles_and_snapping():
    frame = (100.0, 50.0, 80.0, 40.0, 0.0)
    # Dragging the right edge doubles the width, the left edge stays.
    new = TF.scale_frame(frame, (0.5, 0.0), 220, 50)
    assert new[2] == pytest.approx(160) and new[0] - new[2] / 2 == pytest.approx(60)
    # A corner keeps proportions unless asked not to.
    keep = TF.scale_frame(frame, (0.5, 0.5), 220, 200)
    assert keep[2] / keep[3] == pytest.approx(2)
    free = TF.scale_frame(frame, (0.5, 0.5), 220, 200, keep_aspect=False)
    assert free[2:4] == pytest.approx((160, 170))  # anchored at the top-left corner (60, 30)
    turned = TF.rotate_frame(frame, (200, 50), (100, 150))
    assert turned[4] == pytest.approx(90)
    delta = TF.frame_delta(frame, keep)
    from eng_viewer.core.image_document import affine_point
    assert affine_point(delta, *TF.frame_point(frame, 0.5, 0.5)) == pytest.approx(TF.frame_point(keep, 0.5, 0.5))
    assert TF.hit_test(frame, 140, 70, 1) == (0.5, 0.5)
    assert TF.hit_test(frame, 100, 50, 1) == "inside"
    assert TF.hit_test(frame, *TF.rotate_handle(frame, 1), 1) == "rotate"
    dx, dy, lines = TF.snap_move(frame, 96, 3, [200], [50], 6)
    assert (dx, dy) == (100, 0) and ("x", 200) in lines


# ------------------------------------------------------------------- workspace
@pytest.fixture
def editor(qtbot, render, theme, settings, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda parent, title, msg: errors.append(msg))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **kw: QMessageBox.StandardButton.Discard)
    w = MainWindow(settings, render, QPixmap())
    w.resize(1400, 940)
    qtbot.addWidget(w)
    w.show()
    w.set_mode('image')
    e = w.image_editor
    yield e, errors
    qtbot.waitUntil(lambda: e._job is None, timeout=20000)
    for state in e.states:
        state.doc._saved_revision = state.doc.revision
    w.close()


def wait(qtbot, e):
    qtbot.waitUntil(lambda: e._job is None, timeout=20000)


@pytest.fixture
def pasteboard(monkeypatch):
    """A private clipboard: the native one on macOS (a uniquely named pasteboard), Qt's elsewhere."""
    if sys.platform == "darwin":
        from AppKit import NSPasteboard
        pb = NSPasteboard.pasteboardWithUniqueName()
        real_write, real_read = clip.write_clipboard, clip.read_clipboard
        monkeypatch.setattr(clip, "write_clipboard", lambda payload, owner=None, layers=None: real_write(payload, owner, layers, pasteboard=pb))
        monkeypatch.setattr(clip, "read_clipboard", lambda pasteboard=None: real_read(pb))
        yield pb
        pb.releaseGlobally()
    else:
        yield None


def _put_png(pb, image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    if pb is not None:
        from Foundation import NSData
        pb.clearContents()
        pb.setData_forType_(NSData.dataWithBytes_length_(buffer.getvalue(), len(buffer.getvalue())), "public.png")
    else:
        from PySide6.QtWidgets import QApplication
        mime = QMimeData()
        mime.setData("image/png", buffer.getvalue())
        QApplication.clipboard().setMimeData(mime)


def test_paste_image_with_alpha_as_new_layer_and_as_document(editor, pasteboard, qtbot):
    e, errors = editor
    e.paste()  # no document yet: the clipboard is empty, nothing breaks
    image = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((0, 0, 59, 39), fill=(10, 200, 90, 255))
    _put_png(pasteboard, image)
    e.paste()  # no document open: becomes one
    wait(qtbot, e)
    assert e.doc.size == (60, 40) and e.doc.composite().getpixel((0, 0))[3] == 0
    e.add_document(ImageDocument.from_image(Image.new("RGBA", (400, 300), (255, 255, 255, 255))), "Base")
    e.paste()
    wait(qtbot, e)
    layer = e.doc.active_layer
    assert len(e.doc.layers) == 2 and e.doc.active_index == 1 and layer.image.size == (60, 40)
    assert layer.image.getpixel((0, 0))[3] == 0 and layer.image.getpixel((30, 20)) == (10, 200, 90, 255)
    assert e.canvas.tool == "move" and e.canvas.layer_frame is not None
    assert not errors


def test_copy_layer_selection_merged_cut_and_cross_document(editor, pasteboard, qtbot):
    e, errors = editor
    base = Image.new("RGBA", (300, 200), (255, 255, 255, 255))
    e.add_document(ImageDocument.from_image(base), "A")
    top = Image.new("RGBA", (80, 60), (200, 40, 40, 255))
    e.doc.insert_layer(ImageLayer("red", top, x=50, y=40, opacity=0.5, blend="multiply"))
    e.refresh()
    # Whole layer: pasting back keeps its look and position (same canvas size).
    assert e.copy()
    wait(qtbot, e)
    e.paste()
    wait(qtbot, e)
    pasted = e.doc.active_layer
    assert (pasted.x, pasted.y, pasted.opacity, pasted.blend) == (50, 40, 0.5, "multiply") and len(e.doc.layers) == 3
    # Selected pixels only, transparent outside the selection.
    e.doc.active_index = 1
    from eng_viewer.core.image_tools import rectangle_selection
    e.set_selection(rectangle_selection(e.doc.size, (60, 50, 90, 70)))
    e.copy()
    wait(qtbot, e)
    e.paste()
    wait(qtbot, e)
    piece = e.doc.active_layer
    assert piece.image.size == (30, 20) and (piece.x, piece.y) == (60, 50)
    # Cut removes the selected pixels from the layer it came from.
    e.doc.active_index = 1
    e.cut()
    wait(qtbot, e)
    assert e.doc.layers[1].image.getpixel((20, 15))[3] == 0 and e.doc.layers[1].image.getpixel((5, 5))[3] == 255
    # Merged copy into a second document of another size: centred, flattened.
    e.set_selection(None)
    e.copy(merged=True)
    wait(qtbot, e)
    e.add_document(ImageDocument.from_image(Image.new("RGBA", (900, 700), (0, 0, 0, 255))), "B")
    e.paste()
    wait(qtbot, e)
    merged = e.doc.active_layer
    assert merged.image.size == (300, 200)
    cx, cy, *_ = merged.frame
    assert abs(cx - 450) <= 1 and abs(cy - 350) <= 1
    assert not errors


def test_external_apps_get_png_and_tiff(editor, pasteboard, qtbot):
    e, _ = editor
    if pasteboard is None:
        pytest.skip("native pasteboard check is macOS only")
    e.add_document(ImageDocument.from_image(Image.new("RGBA", (40, 30), (1, 2, 3, 128))), "Alpha")
    e.copy()
    wait(qtbot, e)
    types = [str(t) for t in pasteboard.types()]
    assert "public.png" in types and "public.tiff" in types and clip.LAYER_UTI in types
    png = Image.open(io.BytesIO(bytes(pasteboard.dataForType_("public.png"))))
    assert png.mode == "RGBA" and png.getpixel((5, 5))[3] == 128


def test_paste_copied_image_file(editor, pasteboard, qtbot, tmp_path):
    e, _ = editor
    if pasteboard is None:
        pytest.skip("Finder file copy is a macOS pasteboard type")
    from AppKit import NSURL
    path = tmp_path / "photo.png"
    Image.new("RGB", (50, 30), "orange").save(path)
    pasteboard.clearContents()
    pasteboard.writeObjects_([NSURL.fileURLWithPath_(str(path))])
    e.add_document(ImageDocument.from_image(Image.new("RGBA", (200, 200), "white")), "Base")
    e.paste()
    wait(qtbot, e)
    assert e.doc.active_layer.name == "photo" and e.doc.active_layer.image.size == (50, 30)


def test_duplicate_layer_via_copy_and_via_cut(editor, qtbot):
    e, _ = editor
    e.add_document(ImageDocument.from_image(Image.new("RGBA", (100, 100), (90, 90, 90, 255))), "Dup")
    e.duplicate_layer()
    wait(qtbot, e)
    assert len(e.doc.layers) == 2 and e.doc.active_layer.name.endswith("copy")
    from eng_viewer.core.image_tools import rectangle_selection
    e.doc.active_index = 0
    e.set_selection(rectangle_selection(e.doc.size, (0, 0, 50, 50)))
    e.layer_via_cut()
    wait(qtbot, e)
    assert len(e.doc.layers) == 3 and e.doc.layers[0].image.getpixel((10, 10))[3] == 0
    assert e.doc.layers[1].image.size == (50, 50)
    e.history(False)
    wait(qtbot, e)
    assert len(e.doc.layers) == 2 and e.doc.layers[0].image.getpixel((10, 10))[3] == 255


def _drop(e, paths, widget, pos, modifiers=Qt.KeyboardModifier.NoModifier):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    point = QPointF(widget.mapTo(e, pos))
    event = QDropEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.NoButton, modifiers)
    e.dragEnterEvent(event)
    hint = e._drop_overlay.title
    e.dropEvent(event)
    return hint


def test_drop_on_canvas_places_layer_option_or_tabs_open_document(editor, qtbot, tmp_path):
    e, _ = editor
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    Image.new("RGB", (400, 300), "white").save(first)
    Image.new("RGBA", (120, 80), (255, 0, 0, 200)).save(second)
    assert _drop(e, [first], e.canvas, QPoint(200, 200)) in ("Open as New Document",)
    qtbot.waitUntil(lambda: e.doc is not None and e._job is None, timeout=20000)
    assert len(e.states) == 1
    hint = _drop(e, [second], e.canvas, e.canvas.rect().center())
    wait(qtbot, e)
    assert hint == "Place as New Layer"
    assert len(e.states) == 1 and len(e.doc.layers) == 2 and e.doc.active_layer.image.size == (120, 80)
    assert e.doc.active_layer.image.getpixel((5, 5))[3] == 200
    hint = _drop(e, [second], e.layers.viewport(), QPoint(20, 20))
    wait(qtbot, e)
    assert hint == "Place as New Layer" and len(e.doc.layers) == 3
    hint = _drop(e, [second], e.canvas, QPoint(100, 100), Qt.KeyboardModifier.AltModifier)
    qtbot.waitUntil(lambda: len(e.states) == 2 and e._job is None, timeout=20000)
    assert hint == "Open as New Document"
    e.tabs.setCurrentIndex(0)
    hint = _drop(e, [second], e.tabs, QPoint(30, 5))
    qtbot.waitUntil(lambda: e._job is None, timeout=20000)
    assert hint == "Open as New Document"


def _press_drag(qtbot, e, start, end, modifiers=Qt.KeyboardModifier.NoModifier):
    viewport = e.canvas.viewport()
    a = e.canvas.mapFromScene(QPointF(*start))
    b = e.canvas.mapFromScene(QPointF(*end))
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, modifiers, a)
    for f in (0.3, 0.6, 1.0):
        qtbot.mouseMove(viewport, a + (b - a) * f)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, modifiers, b)
    qtbot.waitUntil(lambda: e._job is None, timeout=20000)


def test_move_tool_moves_scales_rotates_and_auto_selects(editor, qtbot):
    e, _ = editor
    doc = ImageDocument.from_image(Image.new("RGBA", (400, 300), (250, 250, 250, 255)))
    doc.insert_layer(ImageLayer("blue", Image.new("RGBA", (100, 60), (0, 0, 255, 255)), x=40, y=40))
    doc.insert_layer(ImageLayer("red", Image.new("RGBA", (80, 80), (255, 0, 0, 255)), x=250, y=150))
    e.add_document(doc, "Move")
    e.set_tool("move")
    e.canvas.set_zoom(100)
    e.canvas.snap = False
    # Auto-select: dragging the blue layer picks it although red is active.
    _press_drag(qtbot, e, (90, 70), (120, 90))
    assert e.doc.active_layer.name == "blue" and (e.doc.active_layer.x, e.doc.active_layer.y) == (70, 60)
    # Scale from the bottom-right handle (proportional by default).
    frame = e.doc.active_layer.frame
    _press_drag(qtbot, e, TF.frame_point(frame, 0.5, 0.5), (frame[0] + 100, frame[1] + 60))
    cx, cy, w, h, _ = e.doc.active_layer.frame
    # Anchored at the top-left corner (70, 60): the corner moves to (220, 150).
    assert w == pytest.approx(150, abs=2) and h == pytest.approx(90, abs=2)
    assert cx - w / 2 == pytest.approx(70, abs=1) and cy - h / 2 == pytest.approx(60, abs=1)
    # Rotate with the knob, Shift snaps to 15° steps.
    frame = e.doc.active_layer.frame
    knob = TF.rotate_handle(frame, e.canvas._zoom())
    _press_drag(qtbot, e, knob, (frame[0] + 200, frame[1] - 40), Qt.KeyboardModifier.ShiftModifier)
    assert e.doc.active_layer.frame[4] % 15 == pytest.approx(0, abs=1e-6) and e.doc.active_layer.frame[4] != 0
    # Locked layers stay put.
    e.doc.active_layer.locked = True
    e.refresh()
    before = (e.doc.active_layer.x, e.doc.active_layer.y)
    _press_drag(qtbot, e, e.doc.active_layer.frame[:2], (10, 10))
    assert (e.doc.active_layer.x, e.doc.active_layer.y) == before
    # Undo walks back through the transforms.
    e.doc.active_layer.locked = False
    for _ in range(3):
        e.history(False)
        wait(qtbot, e)
    assert e.doc.layers[1].frame == pytest.approx((90, 70, 100, 60, 0))


def test_move_snaps_to_canvas_centre_and_arrow_keys_nudge(editor, qtbot):
    e, _ = editor
    doc = ImageDocument.from_image(Image.new("RGBA", (400, 300), "white"))
    doc.insert_layer(ImageLayer("box", Image.new("RGBA", (50, 50), "black"), x=10, y=10))
    e.add_document(doc, "Snap")
    e.set_tool("move")
    e.canvas.set_zoom(100)
    e.canvas.snap = True
    _press_drag(qtbot, e, (35, 35), (35 + 162, 35 + 113))  # within 8 px of centring
    assert e.doc.active_layer.frame[:2] == pytest.approx((200, 150))
    e.canvas.setFocus()
    qtbot.keyClick(e.canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    wait(qtbot, e)
    assert e.doc.active_layer.x == 185


def test_paint_bucket_fills_contiguous_area_transparency_selection_and_new_layer(editor, qtbot):
    e, _ = editor
    image = Image.new("RGBA", (120, 80), (255, 255, 255, 255))
    d = ImageDraw.Draw(image)
    d.rectangle((0, 0, 119, 79), outline=(0, 0, 0, 255), width=3)
    d.line((60, 0, 60, 79), fill=(0, 0, 0, 255), width=3)
    e.add_document(ImageDocument.from_image(image), "Bucket")
    e.set_tool("bucket")
    e.color = "#ff0000"
    e.on_gesture("bucket", [(20, 40)], Qt.KeyboardModifier.NoModifier)
    wait(qtbot, e)
    filled = e.doc.composite()
    assert filled.getpixel((20, 40))[:3] == (255, 0, 0) and filled.getpixel((90, 40))[:3] == (255, 255, 255)
    assert filled.getpixel((60, 40))[:3] == (0, 0, 0)
    e.history(False)
    wait(qtbot, e)
    assert e.doc.composite().getpixel((20, 40))[:3] == (255, 255, 255)
    e.history(True)
    wait(qtbot, e)
    assert e.doc.composite().getpixel((20, 40))[:3] == (255, 0, 0)
    # Non-contiguous: the other side too.
    e.contiguous.setChecked(False)
    e.color = "#0000ff"
    e.on_gesture("bucket", [(90, 40)], Qt.KeyboardModifier.NoModifier)
    wait(qtbot, e)
    assert e.doc.composite().getpixel((90, 40))[:3] == (0, 0, 255)
    e.contiguous.setChecked(True)
    # Transparent layer, inside a selection, on a new layer.
    e.add_document(ImageDocument(100, 100), "Clear")
    from eng_viewer.core.image_tools import rectangle_selection
    e.set_selection(rectangle_selection(e.doc.size, (0, 0, 50, 100)))
    e.fill_new_layer.setChecked(True)
    e.on_gesture("bucket", [(10, 10)], Qt.KeyboardModifier.NoModifier)
    wait(qtbot, e)
    assert len(e.doc.layers) == 2 and e.doc.layers[0].image.getpixel((10, 10))[3] == 0
    fill = e.doc.active_layer
    assert fill.image.size == (50, 100) and fill.image.getpixel((10, 10)) == (0, 0, 255, 255)
    assert e.doc.composite().getpixel((75, 10))[3] == 0
    # A locked layer is never filled directly.
    e.fill_new_layer.setChecked(False)
    e.doc.layers[0].locked = True
    e.doc.active_index = 0
    e.set_selection(None)
    e.on_gesture("bucket", [(80, 10)], Qt.KeyboardModifier.NoModifier)
    wait(qtbot, e)
    assert e.doc.layers[0].image.getpixel((80, 10))[3] == 0


def test_bucket_tool_shortcut_and_panel(editor):
    e, _ = editor
    e.add_document(ImageDocument(50, 50), "Keys")
    e.canvas.setFocus()
    from PySide6.QtTest import QTest
    QTest.keyClick(e.canvas, Qt.Key.Key_G)
    assert e.canvas.tool == "bucket" and e.tool_buttons["bucket"].isChecked()
    assert e.fill_new_layer.isVisibleTo(e) and e.color_button.isVisibleTo(e) and not e.stop_edges.isVisibleTo(e)
    QTest.keyClick(e.canvas, Qt.Key.Key_X)
    assert e.canvas.tool == "magic" and e.stop_edges.isVisibleTo(e) and not e.fill_new_layer.isVisibleTo(e)


def test_edit_menu_commands_belong_to_the_image_workspace(editor):
    e, _ = editor
    w = e.owner
    assert all(a.isEnabled() for a in w.image_actions)
    w.set_mode("viewer")
    assert not any(a.isEnabled() for a in w.image_actions)
    w.set_mode("image")
    keys = {a.text(): a.shortcut().toString() for a in w.image_actions}
    assert keys["Paste as New Layer"] and keys["Duplicate Layer / Layer via Copy"] == "Ctrl+J"
    assert w.act_copy.text() == "Copy Layer or Selection" and w.act_copy.isEnabled()
    assert w.act_copy_name.text() == "Copy Merged" and w.act_copy_name.isEnabled()
    w.set_mode("viewer")
    assert w.act_copy.text() == "Copy Page as Image" and not w.act_copy.isEnabled()  # no drawing open
    w.set_mode("image")


def test_remove_background_mask_or_clean_copy_on_new_layer(editor, qtbot):
    import image_scenes as scenes
    e, errors = editor
    image, truth = scenes.white_object_on_beige()
    e.add_document(ImageDocument.from_image(image), "Product")
    e.remove_background()
    wait(qtbot, e)
    assert len(e.doc.layers) == 1 and e.doc.active_layer.mask is not None
    assert scenes.score(e.doc.active_layer.mask, truth)[2] > 98
    e.history(False)
    wait(qtbot, e)
    e.clean_edges.setChecked(True)
    e.remove_background()
    wait(qtbot, e)
    original, cut = e.doc.layers
    assert not original.visible and original.mask is None and original.image.tobytes() == image.tobytes()
    assert cut.name.endswith("(cut out)") and cut.visible and cut.image.getpixel((5, 5))[3] == 0
    e.history(False)
    wait(qtbot, e)
    assert len(e.doc.layers) == 1 and e.doc.layers[0].visible
    assert not errors
