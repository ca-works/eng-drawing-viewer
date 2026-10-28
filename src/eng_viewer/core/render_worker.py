"""PDF rendering worker process.

Runs in a separate process for two reasons measured during the audit:

* PyMuPDF holds the GIL while rasterising, so a render thread inside the UI
  process stalls the interface for the whole render (2+ s on dense drawings).
* A malformed PDF that crashes MuPDF only takes down this worker; the viewer
  reports the file as unreadable and starts a fresh worker.

The module imports nothing from Qt so workers start quickly and stay small.
Documents are opened read-only and are never saved.
"""
from __future__ import annotations

import os
import sys
from collections import OrderedDict

from .documents import image_pixels, open_document
from .raster_limits import check_pixels

MAX_DOCS = 6
MAX_DISPLAY_LISTS = 8
DEFAULT_MAX_PIXELS = 80_000_000  # guards against absurd page sizes / zoom


class _State:
    def __init__(self, pymupdf):
        self.pymupdf = pymupdf
        self.docs: OrderedDict[str, tuple] = OrderedDict()
        self.dlists: OrderedDict[tuple, object] = OrderedDict()

    def doc(self, path: str):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            raise FileNotFoundError("File not found") from None
        except PermissionError:
            raise PermissionError("Permission denied") from None
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self.docs.get(path)
        if cached is not None and cached[1] == stamp:
            self.docs.move_to_end(path)
            return cached[0]
        if cached is not None:
            self._drop(path)
        if st.st_size == 0:
            raise ValueError("Empty file (0 bytes)")
        doc = open_document(self.pymupdf, path)
        self.docs[path] = (doc, stamp)
        while len(self.docs) > MAX_DOCS:
            self._drop(next(iter(self.docs)))
        return doc

    def _drop(self, path: str):
        for key in [k for k in self.dlists if k[0] == path]:
            del self.dlists[key]
        doc, _ = self.docs.pop(path)
        try:
            doc.close()
        except Exception:
            pass

    def display_list(self, path: str, page_index: int):
        key = (path, page_index)
        dl = self.dlists.get(key)
        if dl is not None:
            self.dlists.move_to_end(key)
            return dl
        page = self.doc(path).load_page(page_index)
        dl = page.get_displaylist()
        self.dlists[key] = dl
        while len(self.dlists) > MAX_DISPLAY_LISTS:
            self.dlists.popitem(last=False)
        return dl


def _page_info(state: _State, args: dict) -> tuple[dict, None]:
    doc = state.doc(args["path"])
    limit = int(args.get("size_limit", 400))
    sizes = []
    for i in range(min(doc.page_count, limit)):
        r = doc.load_page(i).rect
        sizes.append((float(r.width), float(r.height)))
    meta = doc.metadata or {}
    info = {"page_count": doc.page_count, "sizes": sizes, "title": meta.get("title") or "",
            "kind": "pdf" if doc.is_pdf else "image"}
    if not doc.is_pdf:
        info["pixels"] = image_pixels(args["path"])
    return info, None


def _render(state: _State, args: dict):
    pymupdf = state.pymupdf
    path = args["path"]
    index = int(args.get("page", 0))
    doc = state.doc(path)
    if not 0 <= index < doc.page_count:
        raise IndexError(f"Page {index + 1} does not exist.")
    page = doc.load_page(index)
    rect = page.rect
    pw, ph = float(rect.width), float(rect.height)

    if "fit" in args:
        fw, fh = args["fit"]
        scale = min(fw / max(pw, 1e-3), fh / max(ph, 1e-3))
    else:
        scale = float(args["scale"])
    if args.get("native_image") and not doc.is_pdf:
        # Images export at their own resolution: nothing is gained by upscaling.
        px = image_pixels(path)
        if px:
            scale = px[0] / max(pw, 1e-3)
    clip = args.get("clip")
    area_w, area_h = (clip[2] - clip[0], clip[3] - clip[1]) if clip else (pw, ph)
    max_pixels = float(args.get("max_pixels", DEFAULT_MAX_PIXELS))
    if area_w * area_h * scale * scale > max_pixels:
        scale = (max_pixels / max(area_w * area_h, 1e-3)) ** 0.5
    scale = max(scale, 1e-3)

    # The minimum line width is a process-global MuPDF setting. Each worker is
    # single threaded, so set it per job and always restore the default.
    if args.get("reinforce"):
        min_line = max(1.0, 0.90 * scale)  # same rule as core.exporting.reinforced_min_line
    else:
        min_line = float(args.get("min_line", 0.0))
    check_pixels(area_w * scale + 2, area_h * scale + 2)
    matrix = pymupdf.Matrix(scale, scale)
    try:
        pymupdf.TOOLS.set_graphics_min_line_width(min_line)
        if clip:
            dl = state.display_list(path, index)
            pix = dl.get_pixmap(matrix=matrix, clip=pymupdf.Rect(*clip), alpha=False)
        else:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
    finally:
        pymupdf.TOOLS.set_graphics_min_line_width(0.0)

    dpi = args.get("dpi")
    header = {
        "kind": "pdf" if doc.is_pdf else "image",
        "w": pix.width,
        "h": pix.height,
        "stride": pix.stride,
        "x0": pix.x,
        "y0": pix.y,
        "scale": scale,
        "page_w": pw,
        "page_h": ph,
        "page_count": doc.page_count,
    }
    if not doc.is_pdf:
        header["pixels"] = image_pixels(path)
    out_png = args.get("out_png")
    if out_png:
        if dpi:
            pix.set_dpi(int(round(dpi)), int(round(dpi)))
        pix.save(out_png)
        header["path"] = out_png
        return header, None
    cache_png = args.get("cache_png")
    if cache_png:
        # Thumbnail disk cache: write atomically so a reader never sees half a file.
        try:
            os.makedirs(os.path.dirname(cache_png), exist_ok=True)
            tmp = f"{cache_png}.{os.getpid()}.tmp"
            pix.save(tmp, output="png")
            os.replace(tmp, cache_png)
        except OSError:
            pass
    if args.get("no_return"):
        return header, None
    return header, pix  # sent by worker_main while the pixmap is still alive


def _pptx(_state: _State, args: dict):
    from . import pptx_export  # imported on first export only

    return pptx_export.build(args), None


def _tool(_state: _State, args: dict):
    from . import pdf_tools  # imported on first use only

    try:
        return pdf_tools.run(args), None
    except pdf_tools.ToolError as exc:
        raise ValueError(str(exc)) from None


HANDLERS = {"info": _page_info, "render": _render, "pptx": _pptx, "tool": _tool}


def worker_main(conn) -> None:
    from eng_viewer.core.netguard import install

    install()
    # Windowed (no console) builds have no stdout/stderr; MuPDF diagnostics
    # must never raise because of that.
    devnull = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = devnull
    if sys.stderr is None:
        sys.stderr = devnull
    import pymupdf

    try:
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
    except Exception:
        pass
    state = _State(pymupdf)
    idle_trimmed = True
    while True:
        try:
            # After a couple of idle seconds, hand MuPDF's resource store (fonts,
            # decoded images; up to 256 MB by default) back to the system.
            if not idle_trimmed and not conn.poll(2.0):
                state.dlists.clear()
                try:
                    pymupdf.TOOLS.store_shrink(100)
                except Exception:
                    pass
                import gc

                gc.collect()
                idle_trimmed = True
            msg = conn.recv()
            idle_trimmed = False
        except (EOFError, OSError):
            break
        if msg is None:
            break
        job_id, kind, args = msg
        try:
            header, payload = HANDLERS[kind](state, args)
        except MemoryError:
            conn.send((job_id, False, {"error": "Not enough memory to render this page."}))
            continue
        except Exception as exc:
            text = str(exc) or exc.__class__.__name__
            conn.send((job_id, False, {"error": text}))
            continue
        header["has_payload"] = payload is not None
        conn.send((job_id, True, header))
        if payload is not None:
            conn.send_bytes(payload.samples_mv)
        del payload
