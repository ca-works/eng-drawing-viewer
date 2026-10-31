"""Validated PDF content editing executed exclusively in render workers.

The UI replays a command journal against a private immutable source copy. Save
As writes a new PDF with native text/vector content, never a rasterized page.
Original text replacement is deliberately limited to isolated horizontal spans:
actual text is redacted, then a clearly selected fallback font is inserted.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

MAX_COMMANDS = 500
MAX_FILE_BYTES = 512 * 1024 * 1024
FONT_NAMES = {"helv": "Helvetica", "hebo": "Helvetica Bold", "heit": "Helvetica Italic",
              "hebi": "Helvetica Bold Italic", "tiro": "Times Roman", "tibo": "Times Bold",
              "co": "Courier", "cobo": "Courier Bold"}


def _finite(value, minimum=-100000, maximum=100000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("PDF coordinates and sizes must be finite numbers.")
    if not minimum <= value <= maximum:
        raise ValueError("PDF coordinate or size is out of range.")
    return float(value)


def _color(value):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError("Choose an RGB color.")
    return tuple(_finite(v, 0, 1) for v in value)


def _rect(pymupdf, value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("A PDF rectangle needs four coordinates.")
    rect = pymupdf.Rect([_finite(v) for v in value])
    if rect.is_empty or rect.is_infinite:
        raise ValueError("Draw a nonempty rectangle.")
    return rect


def _page(doc, value):
    if type(value) is not int or not 0 <= value < doc.page_count:
        raise ValueError("The selected PDF page does not exist.")
    return doc[value]


def _span_id(span):
    data = [span["text"], [round(v, 3) for v in span["bbox"]], span.get("font", "")]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()[:24]


def _annotation_id(annotation):
    identity = [annotation.type[1], [round(v, 3) for v in annotation.rect],
                annotation.info.get("content", "")]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


def _spans(page):
    spans = []
    # Exclude image byte payloads when inspecting text: photos can be enormous.
    for block in page.get_text("dict", flags=0)["blocks"]:
        for line in block.get("lines", []):
            direction = line.get("dir", (1, 0))
            for span in line.get("spans", []):
                if not span.get("text", "").strip():
                    continue
                color = span.get("color", 0)
                item = {"kind": "text", "text": span["text"], "bbox": list(span["bbox"]),
                        "origin": list(span["origin"]), "font": span["font"], "size": span["size"],
                        "color": [(color >> 16 & 255) / 255, (color >> 8 & 255) / 255, (color & 255) / 255],
                        "horizontal": abs(direction[0] - 1) < .001 and abs(direction[1]) < .001}
                item["id"] = _span_id(item)
                spans.append(item)
    return spans


def inspect_document(doc, page_index=0):
    page = _page(doc, page_index)
    spans = _spans(page)
    images = [{"kind": "image", "bbox": list(item["bbox"]), "xref": item.get("xref", 0),
               "width": item["width"], "height": item["height"], "id": f"image-{index}"}
              for index, item in enumerate(page.get_image_info(xrefs=True))]
    annotations = [{"kind": "annotation", "type": annotation.type[1], "xref": annotation.xref,
                    "id": _annotation_id(annotation),
                    "bbox": list(annotation.rect), "text": annotation.info.get("content", "")}
                   for annotation in page.annots() or []]
    forms = [{"kind": "form", "name": widget.field_name or "", "value": str(widget.field_value or ""),
              "type": widget.field_type_string, "bbox": list(widget.rect), "xref": widget.xref}
             for widget in page.widgets() or []]
    return {"page_count": doc.page_count, "page": page_index, "width": page.rect.width,
            "height": page.rect.height, "rotation": page.rotation,
            "rotation_matrix": list(page.rotation_matrix), "derotation_matrix": list(page.derotation_matrix),
            "cropbox": list(page.cropbox), "mediabox": list(page.mediabox),
            "texts": spans, "images": images, "annotations": annotations, "forms": forms,
            "drawing_count": len(page.get_drawings()), "is_encrypted": bool(doc.is_encrypted)}


def _text_style(command):
    text = command.get("text", "")
    if not isinstance(text, str) or not text or len(text) > 10000:
        raise ValueError("Enter between 1 and 10,000 characters.")
    if any(ord(c) > 255 or (ord(c) < 32 and c not in "\n\t") for c in text):
        raise ValueError("This editor's built-in fonts support Latin-1 text only; an embedded font is required for these characters.")
    font = command.get("font", "helv")
    if font not in FONT_NAMES:
        raise ValueError("Choose one of the supported PDF fonts.")
    return text, font, _finite(command.get("size", 12), 1, 1000), _color(command.get("color", [0, 0, 0]))


def _isolated_span(pymupdf, page, command):
    spans = _spans(page)
    selected = next((s for s in spans if s["id"] == command.get("span_id")), None)
    if selected is None or selected["text"] != command.get("expected_text"):
        raise ValueError("The selected original text has changed; select it again.")
    if not selected["horizontal"]:
        raise ValueError("Rotated or transformed original text cannot be replaced reliably by this editor.")
    rect = pymupdf.Rect(selected["bbox"])
    for other in spans:
        if other is selected:
            continue
        overlap = rect & pymupdf.Rect(other["bbox"])
        if not overlap.is_empty and overlap.width > .1 and overlap.height > .1:
            raise ValueError("This text overlaps another text span; use an explicit region redaction or leave it unchanged.")
    return selected, rect


def apply_command(pymupdf, doc, command):
    if not isinstance(command, dict):
        raise ValueError("Invalid PDF edit command.")
    kind = command.get("kind")
    page = _page(doc, command.get("page", 0))
    if kind in ("replace_text", "delete_text", "redact"):
        if any(annotation.type[0] == pymupdf.PDF_ANNOT_REDACT for annotation in page.annots() or []):
            raise ValueError("This page already contains pending redaction annotations. Resolve those before editing its content.")
    if kind == "text":
        text, font, size, color = _text_style(command)
        rect = _rect(pymupdf, command.get("rect"))
        shape = page.new_shape()
        remaining = shape.insert_textbox(rect, text, fontname=font, fontsize=size, color=color,
                                         align=int(command.get("align", 0)))
        if remaining < 0:
            raise ValueError("The text does not fit. Enlarge its box or reduce the font size.")
        shape.commit()
    elif kind in ("replace_text", "delete_text"):
        selected, rect = _isolated_span(pymupdf, page, command)
        if kind == "replace_text":
            text, font, size, color = _text_style(command)
            if "\n" in text:
                raise ValueError("Replace one original line at a time; use Add Text for multiple lines.")
            origin = command.get("origin", selected["origin"])
            if not isinstance(origin, (list, tuple)) or len(origin) != 2:
                raise ValueError("Invalid text baseline.")
            origin = [_finite(v) for v in origin]
            length = pymupdf.get_text_length(text, fontname=font, fontsize=size)
            bounds = page.rect * page.derotation_matrix
            if origin[0] < bounds.x0 or origin[0] + length > bounds.x1 or not bounds.y0 < origin[1] < bounds.y1:
                raise ValueError("The replacement text would extend outside this page.")
        # fill=False is essential: no white paint disguising the old content.
        page.add_redact_annot(rect, fill=False, cross_out=False)
        page.apply_redactions(images=0, graphics=0, text=0)
        if kind == "replace_text":
            page.insert_text(origin, text, fontname=font, fontsize=size, color=color)
    elif kind in ("rectangle", "ellipse", "line"):
        rect = _rect(pymupdf, command.get("rect"))
        color = _color(command.get("color", [0.1, 0.35, 0.8]))
        fill = _color(command["fill"]) if command.get("fill") is not None else None
        width = _finite(command.get("width", 1.5), .1, 100)
        if kind == "rectangle":
            page.draw_rect(rect, color=color, fill=fill, width=width)
        elif kind == "ellipse":
            page.draw_oval(rect, color=color, fill=fill, width=width)
        else:
            page.draw_line(rect.top_left, rect.bottom_right, color=color, width=width)
    elif kind == "image":
        from .image_document import ImageDocument
        from .image_export import encode_image
        image_path = Path(command.get("image_path", ""))
        if not image_path.is_file() or image_path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("The inserted image is missing or too large.")
        image = ImageDocument.from_file(image_path).composite()
        page.insert_image(_rect(pymupdf, command.get("rect")), stream=encode_image(image), keep_proportion=True)
    elif kind == "note":
        text = command.get("text", "")
        if not isinstance(text, str) or not 1 <= len(text) <= 10000:
            raise ValueError("Enter a note up to 10,000 characters.")
        rect = _rect(pymupdf, command.get("rect"))
        annotation = page.add_text_annot(rect.top_left, text)
        annotation.set_info(title="ENG Drawing Viewer")
        annotation.update()
    elif kind == "delete_annotation":
        found = next((a for a in page.annots() or [] if _annotation_id(a) == command.get("annotation_id")), None)
        if found is None:
            raise ValueError("The selected annotation no longer exists.")
        page.delete_annot(found)
    elif kind == "redact":
        rect = _rect(pymupdf, command.get("rect"))
        page.add_redact_annot(rect, fill=False, cross_out=False)
        page.apply_redactions(images=2, graphics=1, text=0)
    elif kind == "rotate_page":
        degrees = command.get("degrees", 90)
        if type(degrees) is not int or degrees % 90:
            raise ValueError("Page rotation must use 90-degree increments.")
        page.set_rotation((page.rotation + degrees) % 360)
    elif kind == "crop_page":
        rect = _rect(pymupdf, command.get("rect"))
        # UI coordinates are relative to the current unrotated crop. CropBox is
        # relative to MediaBox, so add the existing crop's top-left offset.
        rect += (page.cropbox.x0, page.cropbox.y0, page.cropbox.x0, page.cropbox.y0)
        if not page.mediabox.contains(rect):
            raise ValueError("Crop must be inside the PDF page.")
        page.set_cropbox(rect)
    else:
        raise ValueError("Unsupported PDF edit command.")


def _protect_output(path, sources, overwrite):
    output = Path(path)
    for source in sources:
        source = Path(source)
        if output.resolve() == source.resolve() or (output.exists() and source.exists() and os.path.samefile(output, source)):
            raise ValueError("Save As must use a different file from the original PDF.")
    if output.exists() and not overwrite:
        raise FileExistsError("The output already exists; explicit overwrite confirmation is required.")
    return output


def run(args):
    """Worker entry point. Results contain JSON-safe inspection data only."""
    import pymupdf

    source = Path(args["path"])
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("PDF editing currently supports files up to 512 MiB.")
    with pymupdf.open(source) as doc:
        if not doc.is_pdf or doc.needs_pass:
            raise ValueError("Open an unlocked PDF to edit its contents.")
        if doc.get_sigflags() > 0:
            raise ValueError("This PDF contains signature fields. Edit an unsigned copy to preserve signature expectations.")
        action = args.get("action", "inspect")
        if action not in ("inspect", "replay", "save"):
            raise ValueError("Unsupported PDF editor operation.")
        commands = args.get("commands", [])
        if not isinstance(commands, list) or len(commands) > MAX_COMMANDS:
            raise ValueError("This editing session supports up to 500 operations.")
        if action == "replay":
            for command in commands:
                apply_command(pymupdf, doc, command)
        result = inspect_document(doc, args.get("page", 0))
        if action in ("replay", "save"):
            sources = [str(source)] + list(args.get("source_paths", []))
            output = _protect_output(args["output"], sources, args.get("overwrite", False))
            fd, temporary = tempfile.mkstemp(prefix=".eng-pdf-edit-", suffix=".pdf", dir=output.parent)
            os.close(fd)
            try:
                doc.save(temporary, garbage=4, clean=True, deflate=True)
                # Atomic installation; no source PDFs or aliases are replaced.
                _protect_output(output, sources, args.get("overwrite", False))
                if args.get("overwrite", False):
                    os.replace(temporary, output)
                else:
                    os.link(temporary, output)
                    os.unlink(temporary)
            finally:
                Path(temporary).unlink(missing_ok=True)
            result["path"] = str(output)
        return result
