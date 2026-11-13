"""Build PowerPoint decks locally with python-pptx (no PowerPoint required).

Runs inside a render worker process. Layout follows the original COM export:
plain pages are centred; MARKed pages put the drawing on the right and the
numbered comments in an editable text box on the left. The PDF filename can be
an editable slide title or subtitle.
"""
from __future__ import annotations

import math
import os
import tempfile
from copy import deepcopy

from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.oxml.ns import qn
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.util import Emu, Inches, Pt

SLIDE_SIZES = {"16:9": (Inches(13.333), Inches(7.5)), "4:3": (Inches(10), Inches(7.5))}


def _layout(prs, want_title: bool):
    """Pick a 'Title Only' layout when a title is wanted, else the emptiest one."""
    best, best_score = None, None
    for layout in prs.slide_layouts:
        types = [ph.placeholder_format.type for ph in layout.placeholders]
        has_title = any(t in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE) for t in types)
        others = [t for t in types if t not in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE,
                                                 PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER)]
        if want_title:
            score = (0 if has_title else 1, len(others))
        else:
            score = (1 if has_title else 0, len(others))
        if best_score is None or score < best_score:
            best, best_score = layout, score
    return best


def _remove_empty_placeholders(slide, keep_title: bool):
    for ph in list(slide.placeholders):
        t = ph.placeholder_format.type
        if keep_title and t in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
            continue
        if ph.has_text_frame and ph.text_frame.text.strip():
            continue
        if t in (PP_PLACEHOLDER.PICTURE, PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.SUBTITLE,
                 PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
            ph._element.getparent().remove(ph._element)


def _inherit_background(slide, preceding):
    """Reuse the layout/master chain and copy slide-local background overrides."""
    source, target = preceding._element, slide._element
    if "showMasterSp" in source.attrib:
        target.set("showMasterSp", source.get("showMasterSp"))
    color_map = source.find(qn("p:clrMapOvr"))
    if color_map is not None:
        default_map = target.find(qn("p:clrMapOvr"))
        if default_map is not None:
            target.remove(default_map)
        target.insert(1, deepcopy(color_map))
    for rel in preceding.part.rels.values():
        if rel.reltype == RT.THEME_OVERRIDE and not rel.is_external:
            slide.part.relate_to(rel.target_part, rel.reltype)

    background = source.cSld.find(qn("p:bg"))
    if background is not None:
        background = deepcopy(background)
        rel_prefix = qn("r:id").split("}")[0] + "}"
        rel_ids = {}
        for element in background.iter():
            for name, rid in element.attrib.items():
                if not name.startswith(rel_prefix):
                    continue
                if rid not in rel_ids:
                    rel = preceding.part.rels.get(rid)
                    if rel is None:
                        return  # leave the inherited layout background if the override has a broken reference
                    rel_ids[rid] = slide.part.relate_to(
                        rel.target_ref if rel.is_external else rel.target_part, rel.reltype, rel.is_external)
                element.set(name, rel_ids[rid])
        target.cSld.insert(0, background)


def _remove_inherited_placeholders(slide, title_mode):
    keep = {"title": (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE),
            "subtitle": (PP_PLACEHOLDER.SUBTITLE,)}.get(title_mode, ())
    for placeholder in list(slide.placeholders):
        if placeholder.placeholder_format.type not in keep:
            placeholder._element.getparent().remove(placeholder._element)


def _place_filename(slide, text: str, mode: str, sw: int, sh: int, new_slide: bool):
    if not text or mode == "none":
        return
    if mode == "title":
        title = slide.shapes.title
        if title is not None:
            title.text_frame.text = text
            if new_slide:
                title.left, title.top, title.width, title.height = int(sw * 0.05), int(sh * 0.03), int(sw * 0.90), int(sh * 0.10)
                for p in title.text_frame.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(26)
            return
        box = slide.shapes.add_textbox(int(sw * 0.05), int(sh * 0.035), int(sw * 0.90), int(sh * 0.08))
        box.text_frame.text = text
        run = box.text_frame.paragraphs[0].runs[0]
        run.font.size = Pt(24)
        run.font.bold = True
        return
    for ph in slide.placeholders:
        if ph.placeholder_format.type == PP_PLACEHOLDER.SUBTITLE:
            ph.text_frame.text = text
            return
    box = slide.shapes.add_textbox(int(sw * 0.05), int(sh * 0.055), int(sw * 0.90), int(sh * 0.055))
    box.text_frame.text = text
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(18)


def _comment_font_size(comments: list[str], box_w_pt: float, box_h_pt: float) -> int:
    for size in (18, 16, 14, 13, 12, 11, 10, 9):
        chars_per_line = max(8, int(box_w_pt / (size * 0.5)))
        lines = sum(max(1, math.ceil(len(c) / chars_per_line)) + c.count("\n") for c in comments) or 1
        if lines * size * 1.25 <= box_h_pt:
            return size
    return 9


def _add_comments(slide, comments: list[str], left, top, width, height):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    size = _comment_font_size(comments, Emu(width).pt, Emu(height).pt)
    first = True
    for line in comments:
        para = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        num, sep, rest = line.partition(". ")
        if sep and num.isdigit():
            r1 = para.add_run()
            r1.text = f"{num}. "
            r1.font.bold = True
            r1.font.size = Pt(size)
            r2 = para.add_run()
            r2.text = rest
            r2.font.size = Pt(size)
        else:
            r = para.add_run()
            r.text = line
            r.font.size = Pt(size)
        para.space_after = Pt(size * 0.35)
    return box


def _fit(iw, ih, max_w, max_h):
    if iw <= 0 or ih <= 0:
        return max_w, max_h
    s = min(max_w / iw, max_h / ih)
    return int(iw * s), int(ih * s)


def build(spec: dict) -> dict:
    mode = spec.get("mode", "new")
    if mode == "existing":
        prs = Presentation(spec["template"])
    else:
        prs = Presentation()
        w, h = SLIDE_SIZES.get(spec.get("slide_size", "16:9"), SLIDE_SIZES["16:9"])
        prs.slide_width, prs.slide_height = w, h
    sw, sh = prs.slide_width, prs.slide_height
    title_mode = spec.get("title_mode", "title")
    existing_mode = spec.get("existing_mode", "insert")
    start = max(1, int(spec.get("start_slide", 1)))
    layout = _layout(prs, want_title=(title_mode == "title"))
    sld_list = prs.slides._sldIdLst
    insert_at = min(start - 1, len(prs.slides)) if mode == "existing" else len(prs.slides)

    preceding = None
    if mode == "existing" and insert_at:
        preceding = prs.slides[insert_at - 1]
    if mode == "existing" and existing_mode == "reuse" and len(prs.slides):
        preceding = prs.slides[-1]  # only the overflow slides are generated

    count = 0
    for i, item in enumerate(spec["slides"]):
        new_slide = True
        if mode == "existing" and existing_mode == "reuse" and start - 1 + i < len(prs.slides):
            slide = prs.slides[start - 1 + i]
            new_slide = False
        else:
            if preceding is not None:
                slide = prs.slides.add_slide(preceding.slide_layout)
                _remove_inherited_placeholders(slide, title_mode)
                _inherit_background(slide, preceding)
            else:
                slide = prs.slides.add_slide(layout)
                _remove_empty_placeholders(slide, keep_title=(title_mode == "title"))
            if mode == "existing" and existing_mode == "insert":
                el = sld_list[-1]
                sld_list.remove(el)
                sld_list.insert(insert_at, el)
                insert_at += 1
        _place_filename(slide, item.get("title", ""), title_mode, sw, sh, new_slide)
        top = int(sh * (0.145 if title_mode == "title" else 0.125 if title_mode == "subtitle" else 0.05))
        avail_h = int(sh - top - sh * 0.05)
        iw, ih = item.get("img_w", 0), item.get("img_h", 0)
        if item.get("marked"):
            text_left = int(sw * 0.04)
            text_w = int(sw * 0.36)
            gap = int(sw * 0.035)
            image_left = text_left + text_w + gap
            image_w = int(sw - image_left - sw * 0.04)
            pw, ph = _fit(iw, ih, image_w, avail_h)
            slide.shapes.add_picture(item["image"], image_left + (image_w - pw) // 2, top + (avail_h - ph) // 2, pw, ph)
            _add_comments(slide, item.get("comments") or [], text_left, top, text_w, avail_h)
        else:
            max_w = int(sw * 0.9)
            pw, ph = _fit(iw, ih, max_w, avail_h)
            slide.shapes.add_picture(item["image"], (sw - pw) // 2, top + max(avail_h - ph, 0) // 2, pw, ph)
        count += 1

    out = spec["output"]
    folder = os.path.dirname(os.path.abspath(out)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".eng-export-", suffix=".pptx", dir=folder)
    os.close(fd)
    try:
        prs.save(tmp)
        try:
            os.replace(tmp, out)
        except PermissionError as exc:
            raise PermissionError(
                f"Could not write {os.path.basename(out)}. If it is open in PowerPoint, close it and try again."
            ) from exc
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"slides": count, "path": out, "total_slides": len(prs.slides)}
