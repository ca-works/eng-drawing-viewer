import hashlib
import io

import pymupdf
import pytest
from PIL import Image

from eng_viewer.core.pdf_edit import run


@pytest.fixture
def editable_pdf(tmp_path):
    path = tmp_path / "source.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=300)
        page.draw_rect((25, 35, 260, 90), color=(0, 0, 1), fill=(.8, .9, 1), width=2)
        page.insert_text((40, 65), "PRIVATE ORIGINAL", fontsize=16, fontname="helv")
        page.insert_text((40, 125), "Keep this vector text", fontsize=12)
        image = io.BytesIO()
        Image.new("RGB", (20, 20), "green").save(image, "PNG")
        page.insert_image((270, 35, 310, 75), stream=image.getvalue())
        page.add_text_annot((330, 150), "Separate annotation")
        field = pymupdf.Widget()
        field.field_name = "TestForm"
        field.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        field.rect = pymupdf.Rect(30, 220, 130, 240)
        field.field_value = "Editable form value"
        page.add_widget(field)
        doc.new_page(width=400, height=300).insert_text((40, 60), "Second page")
        doc.save(path)
    return path


def _target(path):
    info = run({"path": str(path), "action": "inspect"})
    return next(span for span in info["texts"] if span["text"] == "PRIVATE ORIGINAL")


def test_inspection_distinguishes_pdf_content_annotations_and_forms(editable_pdf):
    info = run({"path": str(editable_pdf)})
    assert info["page_count"] == 2
    assert len(info["texts"]) >= 2
    assert info["images"][0]["width"] == 20
    assert info["annotations"][0]["text"] == "Separate annotation"
    assert info["forms"][0]["name"] == "TestForm"
    assert info["drawing_count"] >= 1


def test_replace_original_removes_text_keeps_vectors_images_and_source(editable_pdf, tmp_path):
    original = editable_pdf.read_bytes()
    target = _target(editable_pdf)
    output = tmp_path / "replaced.pdf"
    command = {"kind": "replace_text", "page": 0, "span_id": target["id"],
               "expected_text": target["text"], "text": "REPLACED", "font": "helv",
               "size": 16, "origin": target["origin"], "color": [0, 0, 0]}
    run({"path": str(editable_pdf), "action": "replay", "commands": [command], "output": str(output)})
    assert editable_pdf.read_bytes() == original
    with pymupdf.open(output) as doc:
        page = doc[0]
        assert "PRIVATE ORIGINAL" not in page.get_text()
        assert "REPLACED" in page.get_text()
        assert "Keep this vector text" in page.get_text()
        assert len(page.get_images()) == 1
        assert len(page.get_drawings()) >= 1
        # New text is searchable vector content, not a flattened page bitmap.
        assert len(page.get_text("dict")["blocks"]) >= 2
        content = b"".join(doc.xref_stream(xref) or b"" for xref in range(1, doc.xref_length()) if doc.xref_is_stream(xref))
        assert b"PRIVATE ORIGINAL" not in content
        assert b"50524956415445204f524947494e414c" not in content.lower()


def test_delete_original_and_region_redaction_reconstruct_no_fake_white_box(editable_pdf, tmp_path):
    target = _target(editable_pdf)
    output = tmp_path / "deleted.pdf"
    run({"path": str(editable_pdf), "action": "replay", "output": str(output), "commands": [
        {"kind": "delete_text", "page": 0, "span_id": target["id"], "expected_text": target["text"]}]})
    with pymupdf.open(output) as doc:
        assert "PRIVATE ORIGINAL" not in doc[0].get_text()
        pixel = doc[0].get_pixmap().pixel(45, 55)
        assert pixel[2] > pixel[0]  # Underlying blue vector fill remains visible.
    redacted = tmp_path / "redacted.pdf"
    run({"path": str(editable_pdf), "action": "replay", "output": str(redacted), "commands": [
        {"kind": "redact", "page": 0, "rect": [30, 40, 315, 80]}]})
    with pymupdf.open(redacted) as doc:
        assert "PRIVATE ORIGINAL" not in doc[0].get_text()
        assert "Keep this vector text" in doc[0].get_text()
        assert not list(doc[0].annots() or []) or all(a.type[0] != pymupdf.PDF_ANNOT_REDACT for a in doc[0].annots())


def test_added_native_objects_are_editable_through_command_replay(editable_pdf, tmp_path):
    image = tmp_path / "insert.png"
    Image.new("RGBA", (8, 8), "red").save(image)
    commands = [
        {"kind": "text", "page": 1, "rect": [30, 100, 250, 140], "text": "Added vector text", "size": 14},
        {"kind": "rectangle", "page": 1, "rect": [30, 150, 100, 190], "color": [1, 0, 0]},
        {"kind": "ellipse", "page": 1, "rect": [130, 150, 200, 190], "fill": [0, 1, 0]},
        {"kind": "line", "page": 1, "rect": [30, 210, 100, 240]},
        {"kind": "image", "page": 1, "rect": [270, 150, 310, 190], "image_path": str(image)},
        {"kind": "note", "page": 1, "rect": [270, 210, 300, 240], "text": "A separate note"},
    ]
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    run({"path": str(editable_pdf), "action": "replay", "output": str(first), "commands": commands, "page": 1})
    commands[0]["text"] = "Revised object"
    commands[1]["rect"] = [40, 160, 110, 200]
    run({"path": str(editable_pdf), "action": "replay", "output": str(second), "commands": commands, "page": 1})
    with pymupdf.open(second) as doc:
        page = doc[1]
        assert "Revised object" in page.get_text()
        assert "Added vector text" not in page.get_text()
        # The note's appearance is also vector graphics; verify the actual
        # three inserted shapes rather than counting annotation appearance paths.
        assert len([drawing for drawing in page.get_drawings() if drawing["rect"].y0 < 200]) == 2
        assert any(drawing["rect"] == pymupdf.Rect(30, 210, 100, 240) for drawing in page.get_drawings())
        assert len(page.get_images()) == 1
        assert next(page.annots()).info["content"] == "A separate note"


def test_page_rotate_crop_and_empty_journal_undo(editable_pdf, tmp_path):
    output, undo = tmp_path / "geometry.pdf", tmp_path / "undo.pdf"
    run({"path": str(editable_pdf), "action": "replay", "output": str(output), "commands": [
        {"kind": "crop_page", "page": 0, "rect": [10, 20, 350, 280]},
        {"kind": "rotate_page", "page": 0, "degrees": 90}]})
    with pymupdf.open(output) as doc:
        assert doc[0].rotation == 90
        assert doc[0].rect.width == 260
        assert doc[0].rect.height == 340
    run({"path": str(editable_pdf), "action": "replay", "output": str(undo), "commands": []})
    with pymupdf.open(undo) as doc:
        assert doc[0].rotation == 0
        assert doc[0].rect.width == 400


def test_annotation_deletion_by_stable_identity(editable_pdf, tmp_path):
    info = run({"path": str(editable_pdf)})
    output = tmp_path / "without-note.pdf"
    run({"path": str(editable_pdf), "action": "replay", "output": str(output), "commands": [
        {"kind": "delete_annotation", "page": 0, "annotation_id": info["annotations"][0]["id"]}]})
    with pymupdf.open(output) as doc:
        assert list(doc[0].annots() or []) == []
        assert len(list(doc[0].widgets())) == 1


def test_inspected_saved_revision_can_be_edited_and_text_moved(editable_pdf, tmp_path):
    first, second = tmp_path / "first.pdf", tmp_path / "moved.pdf"
    commands = [{"kind": "rectangle", "page": 0, "rect": [20, 150, 250, 180]}]
    run({"path": str(editable_pdf), "action": "replay", "commands": commands, "output": str(first)})
    target = _target(first)
    commands.append({"kind": "replace_text", "page": 0, "span_id": target["id"],
                     "expected_text": target["text"], "text": "MOVED", "origin": [80, 205], "size": 16})
    run({"path": str(editable_pdf), "action": "replay", "commands": commands, "output": str(second)})
    with pymupdf.open(second) as doc:
        found = doc[0].search_for("MOVED")
        assert len(found) == 1 and found[0].y0 > 180
        assert "PRIVATE ORIGINAL" not in doc[0].get_text()


def test_overlapping_original_text_is_rejected(tmp_path):
    source = tmp_path / "overlap.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 60), "First text")
        page.insert_text((40, 62), "Overlapping text")
        doc.save(source)
    target = run({"path": str(source)})["texts"][0]
    with pytest.raises(ValueError, match="overlaps"):
        run({"path": str(source), "action": "replay", "output": str(tmp_path / "no.pdf"), "commands": [
            {"kind": "delete_text", "span_id": target["id"], "expected_text": target["text"]}]})


def test_no_source_overwrite_or_output_overwrite_without_confirmation(editable_pdf, tmp_path):
    alias = tmp_path / "alias.pdf"
    alias.hardlink_to(editable_pdf)
    for output in (editable_pdf, alias):
        with pytest.raises(ValueError, match="original"):
            run({"path": str(editable_pdf), "action": "save", "output": str(output), "overwrite": True})
    existing = tmp_path / "existing.pdf"
    existing.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        run({"path": str(editable_pdf), "action": "save", "output": str(existing)})
    assert existing.read_bytes() == b"keep"


def test_pending_redactions_never_apply_implicitly(tmp_path):
    path = tmp_path / "pending.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 60), "First")
        page.insert_text((40, 100), "Second")
        page.add_redact_annot((30, 80, 100, 110))
        doc.save(path)
    selected = run({"path": str(path)})["texts"][0]
    with pytest.raises(ValueError, match="pending redaction"):
        run({"path": str(path), "action": "replay", "output": str(tmp_path / "no.pdf"), "commands": [
            {"kind": "delete_text", "page": 0, "span_id": selected["id"], "expected_text": selected["text"]}]})
    assert not (tmp_path / "no.pdf").exists()


@pytest.mark.parametrize("command", [
    {"kind": "unsupported"},
    {"kind": "text", "rect": [0, 0, 2, 2], "text": "This cannot fit", "size": 30},
    {"kind": "text", "rect": [0, 0, 200, 80], "text": "漢字"},
    {"kind": "rectangle", "rect": [0, 0, float("nan"), 80]},
    {"kind": "rotate_page", "degrees": 23},
    {"kind": "delete_text", "span_id": "changed", "expected_text": "unknown"},
])
def test_invalid_or_unreliable_operations_fail_without_output(editable_pdf, tmp_path, command):
    output = tmp_path / "must-not-exist.pdf"
    with pytest.raises(ValueError):
        run({"path": str(editable_pdf), "action": "replay", "output": str(output), "commands": [command]})
    assert not output.exists()


def test_pdf_edit_worker_dispatch(qtbot, render, editable_pdf, tmp_path):
    results = []
    render.submit("pdf_edit", {"action": "replay", "path": str(editable_pdf),
        "output": str(tmp_path / "worker.pdf"), "commands": [
            {"kind": "text", "page": 0, "rect": [30, 150, 220, 190], "text": "Worker text"}]}, results.append)
    qtbot.waitUntil(lambda: bool(results), timeout=20000)
    assert results[0].ok, results[0].error
    with pymupdf.open(tmp_path / "worker.pdf") as doc:
        assert "Worker text" in doc[0].get_text()


def test_native_workspace_undo_redo_and_original_preserved(qtbot, render, editable_pdf, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QWidget, QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Discard)
    from eng_viewer.ui.pdf_editor import PdfEditWorkspace

    owner = QWidget()
    owner.render = render
    qtbot.addWidget(owner)
    widget = PdfEditWorkspace(owner)
    qtbot.addWidget(widget)
    original = hashlib.sha256(editable_pdf.read_bytes()).hexdigest()
    errors = []
    monkeypatch.setattr(widget, "_error", errors.append)
    widget.resize(1100, 750)
    widget.show()
    assert widget.open_file(str(editable_pdf))
    qtbot.waitUntil(lambda: bool(widget.info) and not widget._busy, timeout=20000)
    widget._add({"kind": "text", "page": 0, "rect": [30, 150, 220, 190], "text": "Session text"})
    qtbot.waitUntil(lambda: len(widget.commands) == 1 and not widget._busy, timeout=20000)
    assert widget.dirty and widget.undo_button.isEnabled()
    widget.undo()
    qtbot.waitUntil(lambda: not widget.commands and not widget._busy, timeout=20000)
    assert not widget.dirty
    widget.redo()
    qtbot.waitUntil(lambda: len(widget.commands) == 1 and not widget._busy, timeout=20000)
    assert widget.dirty
    assert hashlib.sha256(editable_pdf.read_bytes()).hexdigest() == original
    output = tmp_path / "ui-saved.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(output), "PDF (*.pdf)"))
    widget.save_as()
    qtbot.waitUntil(lambda: output.exists() and not widget._busy, timeout=20000)
    assert not widget.dirty
    with pymupdf.open(output) as doc:
        assert "Session text" in doc[0].get_text()
    # A rejected worker edit must not modify either the journal or saved state.
    widget._add({"kind": "text", "page": 0, "rect": [0, 0, 1, 1], "text": "Does not fit", "size": 80})
    qtbot.waitUntil(lambda: bool(errors) and not widget._busy, timeout=20000)
    assert "fit" in errors[-1]
    assert len(widget.commands) == 1 and not widget.dirty
    widget.close()
