"""Send slides straight into a presentation that is open in Microsoft PowerPoint (Windows only).

PowerPoint is driven through its documented COM automation interface (pywin32, late-bound, so no
type-library cache is written). Nothing starts PowerPoint: only presentations already open in a
running PowerPoint are offered. Slides follow the same layout as the .pptx export
(``core/pptx_export.py``): plain pages centred, marked pages with the drawing on the right and the
numbered comments in an editable text box on the left, the filename as an editable title or
subtitle. All COM calls run on worker threads, never on the UI thread.
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QThread, Signal

# PowerPoint and Office constants (from the PowerPoint object model reference).
PP_PLACEHOLDER_TITLE, PP_PLACEHOLDER_BODY, PP_PLACEHOLDER_CENTER_TITLE, PP_PLACEHOLDER_SUBTITLE = 1, 2, 3, 4
PP_PLACEHOLDER_OBJECT, PP_PLACEHOLDER_SLIDE_NUMBER, PP_PLACEHOLDER_FOOTER, PP_PLACEHOLDER_DATE = 7, 13, 15, 16
PP_PLACEHOLDER_PICTURE = 18
TITLE_TYPES = (PP_PLACEHOLDER_TITLE, PP_PLACEHOLDER_CENTER_TITLE)
IGNORED_TYPES = (PP_PLACEHOLDER_DATE, PP_PLACEHOLDER_FOOTER, PP_PLACEHOLDER_SLIDE_NUMBER)
REMOVABLE_TYPES = (PP_PLACEHOLDER_PICTURE, PP_PLACEHOLDER_BODY, PP_PLACEHOLDER_OBJECT, PP_PLACEHOLDER_SUBTITLE,
                   PP_PLACEHOLDER_TITLE, PP_PLACEHOLDER_CENTER_TITLE)
MSO_TRUE, MSO_FALSE = -1, 0
MSO_TEXT_HORIZONTAL = 1
MSO_AUTOSIZE_TEXT_TO_FIT = 2
RPC_E_CALL_REJECTED = -2147418111  # PowerPoint is busy (a dialog is open, it is still starting …)
RPC_E_SERVERCALL_RETRYLATER = -2147417846


def _com():
    import pythoncom
    import win32com.client.dynamic

    return pythoncom, win32com.client.dynamic


def _retry(fn, attempts: int = 20):
    """Call ``fn``; while PowerPoint is busy, wait briefly and try again."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # pywintypes.com_error carries the HRESULT
            if getattr(exc, "hresult", None) not in (RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER) \
                    or i == attempts - 1:
                raise
            time.sleep(0.25)


def _running_powerpoint():
    """The running PowerPoint, or None. Never starts it."""
    pythoncom, dynamic = _com()
    try:
        clsid = pythoncom.MakeIID("PowerPoint.Application")  # ProgID to CLSID, from the registry
        unknown = pythoncom.GetActiveObject(clsid)
    except pythoncom.com_error:
        return None
    return dynamic.Dispatch(unknown.QueryInterface(pythoncom.IID_IDispatch))


def list_open_presentations() -> list[tuple[str, str]]:
    """(name, full name) of each presentation open in a running PowerPoint. Blocking: call it
    from a worker thread (PresentationLookup does)."""
    pythoncom, _ = _com()
    pythoncom.CoInitialize()
    try:
        app = _running_powerpoint()
        if app is None:
            return []
        out = []
        for i in range(1, _retry(lambda: app.Presentations.Count) + 1):
            pres = _retry(lambda i=i: app.Presentations.Item(i))
            try:
                if pres.ReadOnly:
                    continue
            except Exception:
                pass
            out.append((str(pres.Name), str(pres.FullName)))
        return out
    except Exception:
        return []
    finally:
        pythoncom.CoUninitialize()


# Worker threads are kept here until they finish, so closing a dialog never destroys a running one.
_RUNNING: set[QThread] = set()


def _keep_until_finished(thread: QThread):
    _RUNNING.add(thread)
    thread.finished.connect(lambda: _RUNNING.discard(thread))


class PresentationLookup(QThread):
    """Finds open presentations without blocking the window; emits ``found`` once."""

    found = Signal(list)

    def __init__(self):
        super().__init__()
        _keep_until_finished(self)

    def run(self):
        self.found.emit(list_open_presentations())


def _layout(pres, want_title: bool):
    """The 'Title Only' style layout when a title is wanted, otherwise the emptiest one."""
    best, best_score = None, None
    layouts = pres.SlideMaster.CustomLayouts
    for i in range(1, layouts.Count + 1):
        layout = layouts.Item(i)
        types = []
        placeholders = layout.Shapes.Placeholders
        for j in range(1, placeholders.Count + 1):
            try:
                types.append(placeholders.Item(j).PlaceholderFormat.Type)
            except Exception:
                pass
        has_title = any(t in TITLE_TYPES for t in types)
        others = [t for t in types if t not in TITLE_TYPES and t not in IGNORED_TYPES]
        score = ((0 if has_title else 1) if want_title else (1 if has_title else 0), len(others))
        if best_score is None or score < best_score:
            best, best_score = layout, score
    return best


def _clear_placeholders(slide, keep_title: bool):
    shapes = slide.Shapes
    for j in range(shapes.Count, 0, -1):
        shape = shapes.Item(j)
        try:
            if shape.Type != 14:  # msoPlaceholder
                continue
            kind = shape.PlaceholderFormat.Type
        except Exception:
            continue
        if keep_title and kind in TITLE_TYPES:
            continue
        try:
            if shape.HasTextFrame and shape.TextFrame.TextRange.Text.strip():
                continue
        except Exception:
            pass
        if kind in REMOVABLE_TYPES:
            shape.Delete()


def _text_box(slide, left, top, width, height, text: str, size: float, bold=False):
    box = slide.Shapes.AddTextbox(MSO_TEXT_HORIZONTAL, left, top, width, height)
    box.TextFrame.WordWrap = MSO_TRUE
    box.TextFrame.TextRange.Text = text
    box.TextFrame.TextRange.Font.Size = size
    box.TextFrame.TextRange.Font.Bold = MSO_TRUE if bold else MSO_FALSE
    return box


def _place_filename(slide, text: str, mode: str, sw: float, sh: float):
    if not text or mode == "none":
        return
    if mode == "title":
        if slide.Shapes.HasTitle:
            title = slide.Shapes.Title
            title.TextFrame.TextRange.Text = text
            title.Left, title.Top, title.Width, title.Height = sw * 0.05, sh * 0.03, sw * 0.90, sh * 0.10
            title.TextFrame.TextRange.Font.Size = 26
        else:
            _text_box(slide, sw * 0.05, sh * 0.035, sw * 0.90, sh * 0.08, text, 24, bold=True)
        return
    _text_box(slide, sw * 0.05, sh * 0.055, sw * 0.90, sh * 0.055, text, 18)


def _comments(slide, comments: list[str], left, top, width, height):
    from ..core.pptx_export import _comment_font_size

    size = _comment_font_size(comments, width, height)
    box = slide.Shapes.AddTextbox(MSO_TEXT_HORIZONTAL, left, top, width, height)
    frame = box.TextFrame
    frame.WordWrap = MSO_TRUE
    frame.TextRange.Text = "\r".join(comments)  # one paragraph per comment
    frame.TextRange.Font.Size = size
    frame.TextRange.ParagraphFormat.SpaceAfter = size * 0.35
    for i, line in enumerate(comments, 1):
        num, sep, _rest = line.partition(". ")
        if sep and num.isdigit():
            frame.TextRange.Paragraphs(i).Characters(1, len(num) + 1).Font.Bold = MSO_TRUE
    try:
        box.TextFrame2.AutoSize = MSO_AUTOSIZE_TEXT_TO_FIT
    except Exception:
        pass


def _fit(iw, ih, max_w, max_h):
    if iw <= 0 or ih <= 0:
        return max_w, max_h
    s = min(max_w / iw, max_h / ih)
    return iw * s, ih * s


def send_slides(slides: list[dict], target: str, start_slide: int, title_mode: str) -> int:
    """Insert one slide per item into the open presentation ``target`` (its FullName)."""
    pythoncom, _ = _com()
    pythoncom.CoInitialize()
    try:
        app = _running_powerpoint()
        if app is None:
            raise RuntimeError("PowerPoint is no longer running.")
        pres = None
        for i in range(1, _retry(lambda: app.Presentations.Count) + 1):
            p = app.Presentations.Item(i)
            if str(p.FullName) == target:
                pres = p
                break
        if pres is None:
            raise RuntimeError("The presentation is no longer open in PowerPoint.")
        sw, sh = _retry(lambda: (float(pres.PageSetup.SlideWidth), float(pres.PageSetup.SlideHeight)))
        layout = _retry(lambda: _layout(pres, want_title=(title_mode == "title")))
        index = max(1, min(int(start_slide), pres.Slides.Count + 1))
        first = index
        for item in slides:
            slide = _retry(lambda: pres.Slides.AddSlide(index, layout))
            _clear_placeholders(slide, keep_title=(title_mode == "title"))
            _place_filename(slide, item.get("title", ""), title_mode, sw, sh)
            top = sh * (0.145 if title_mode == "title" else 0.125 if title_mode == "subtitle" else 0.05)
            avail_h = sh - top - sh * 0.05
            iw, ih = item.get("img_w", 0), item.get("img_h", 0)
            image = os.path.abspath(item["image"])
            if item.get("marked"):
                text_left, text_w, gap = sw * 0.04, sw * 0.36, sw * 0.035
                image_left = text_left + text_w + gap
                image_w = sw - image_left - sw * 0.04
                pw, ph = _fit(iw, ih, image_w, avail_h)
                slide.Shapes.AddPicture(image, MSO_FALSE, MSO_TRUE, image_left + (image_w - pw) / 2,
                                        top + (avail_h - ph) / 2, pw, ph)
                _comments(slide, item.get("comments") or [], text_left, top, text_w, avail_h)
            else:
                pw, ph = _fit(iw, ih, sw * 0.9, avail_h)
                slide.Shapes.AddPicture(image, MSO_FALSE, MSO_TRUE, (sw - pw) / 2, top + max(avail_h - ph, 0) / 2, pw, ph)
            index += 1
        try:  # show the first new slide
            pres.Windows.Item(1).View.GotoSlide(first)
        except Exception:
            pass
        return len(slides)
    finally:
        pythoncom.CoUninitialize()


class ComExportThread(QThread):
    done = Signal(int)
    error = Signal(str)

    def __init__(self, slides: list[dict], target: str, start_slide: int, title_mode: str, parent=None):
        super().__init__()  # deliberately without a parent: see _keep_until_finished
        _keep_until_finished(self)
        self._args = (slides, target, start_slide, title_mode)

    def run(self):
        try:
            self.done.emit(send_slides(*self._args))
        except Exception as exc:
            text = str(exc)
            if hasattr(exc, "excepinfo") and exc.excepinfo and exc.excepinfo[2]:
                text = str(exc.excepinfo[2])
            self.error.emit(f"Could not add the slides in PowerPoint: {text}")
