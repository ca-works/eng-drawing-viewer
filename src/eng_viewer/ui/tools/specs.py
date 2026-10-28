"""Declarative definitions of the PDF tools shown in the Tools workspace.

Each ToolSpec describes its inputs, options and output; the generic panel
builds the UI from it and the worker-side engine (core/pdf_tools.py) does the
work. Custom panels (Organize, Compare, Scan) only need an id and a title.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...core.documents import IMAGE_EXTS
from ...core.tool_support import OFFICE_EXTS, find_office, ocr_available, scanning_available

PDF = frozenset({".pdf"})
PDF_OR_IMAGE = PDF | IMAGE_EXTS
PAPERS = ["A4", "A3", "A2", "A1", "A0", "Letter", "Tabloid"]

CATEGORIES = [
    ("organize", "Organize", "#2F7CF6"),
    ("to_pdf", "Convert to PDF", "#20A060"),
    ("from_pdf", "Convert from PDF", "#0FA3A3"),
    ("optimize", "Optimize", "#E58A12"),
    ("edit", "Edit", "#8B5CF6"),
    ("security", "Security", "#E5484D"),
    ("compare", "Review", "#5B6B7F"),
]


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"  # choice | text | multiline | password | int | float | bool | color | pages | file | combo
    default: Any = None
    options: list = field(default_factory=list)  # choice: [(value, label)]; combo: [str]
    suffix: str = ""
    minimum: float = 0
    maximum: float = 9999
    placeholder: str = ""
    help: str = ""
    visible_if: tuple | None = None  # (other key, allowed value or tuple of values)
    file_exts: frozenset = PDF


@dataclass
class ToolSpec:
    id: str
    title: str
    category: str
    icon: str
    blurb: str
    accepts: frozenset = PDF
    multi: bool = False  # several inputs combined into one result (merge, images→PDF)
    batch: bool = True  # run once per input when several are given
    reorder: bool = False
    min_inputs: int = 1
    fields: list[Field] = field(default_factory=list)
    output: str = "pdf"  # pdf | folder | docx | xlsx | pptx | text
    suffix: str = ""
    op: str = ""
    custom: str = ""  # name of a custom panel
    available: Callable[[], str] | None = None  # returns "" when usable, else the reason
    build: Callable[[dict, list[str], str], list[dict]] | None = None  # (values, inputs, output) -> job args


def _one(op: str, **extra):
    """Build one job per input: op(path=..., output=..., **values)."""
    def build(values: dict, inputs: list[str], output: str) -> list[dict]:
        return [{"op": op, "path": inputs[0], "output": output, **{k: values[k] for k in values}, **extra}]
    return build


def _avail_office() -> str:
    return "" if find_office() else "Needs LibreOffice (free) installed on this computer — it converts Office files offline."


def _avail_ocr() -> str:
    return "" if ocr_available() else "Needs macOS (built-in text recognition) or Tesseract OCR installed."


def _avail_scan() -> str:
    return "" if scanning_available() else "Scanning with an iPhone uses Apple’s Continuity Camera and is only available on macOS."


ORIENTATION = Field("orientation", "Orientation", "choice", "auto",
                    [("auto", "Automatic"), ("portrait", "Portrait"), ("landscape", "Landscape")])
RANGES = Field("ranges", "Pages", "pages", "", placeholder="All pages — or e.g. 1-3, 5, 8-end")


SPECS: list[ToolSpec] = [
    # ---------------------------------------------------------- organize
    ToolSpec("organize", "Organize Pages", "organize", "pages",
             "Reorder, rotate, delete, duplicate and insert pages from several PDFs and images, then save one PDF.",
             custom="organizer"),
    ToolSpec("merge", "Merge", "organize", "merge", "Combine PDFs and images into one PDF, in the order you choose.",
             accepts=PDF_OR_IMAGE, multi=True, batch=False, reorder=True, min_inputs=2,
             fields=[Field("bookmarks", "Add a bookmark for each file", "bool", True)], suffix=" (merged)",
             build=lambda v, ins, out: [{"op": "merge", "paths": ins, "output": out, "bookmarks": v["bookmarks"]}]),
    ToolSpec("split", "Split", "organize", "split", "Split a PDF into single pages, every N pages or custom ranges.",
             fields=[
                 Field("mode", "Split into", "choice", "each", [("each", "Single pages"), ("every", "Every N pages"), ("ranges", "Custom ranges")]),
                 Field("every", "Pages per file", "int", 2, minimum=1, maximum=999, visible_if=("mode", "every")),
                 Field("ranges", "Ranges", "text", "", placeholder="e.g. 1-3; 4-6; 7-end", visible_if=("mode", "ranges")),
             ], output="folder", suffix=" (split)",
             build=lambda v, ins, out: [{"op": "split", "path": ins[0], "out_dir": out, "mode": v["mode"],
                                         "ranges": v["ranges"], "every": v["every"]}]),
    ToolSpec("extract", "Extract Pages", "organize", "extract", "Save chosen pages as a new PDF.",
             fields=[Field("ranges", "Pages to keep", "pages", "", placeholder="e.g. 1-3, 5, 8-end")], suffix=" (extract)",
             build=lambda v, ins, out: [{"op": "select_pages", "path": ins[0], "output": out, "ranges": v["ranges"], "keep": True}]),
    ToolSpec("remove", "Remove Pages", "organize", "remove-pages", "Save a copy without the pages you choose.",
             fields=[Field("ranges", "Pages to remove", "pages", "", placeholder="e.g. 2, 4-6")], suffix=" (pages removed)",
             build=lambda v, ins, out: [{"op": "select_pages", "path": ins[0], "output": out, "ranges": v["ranges"], "keep": False}]),
    ToolSpec("rotate", "Rotate", "organize", "rotate-page", "Turn pages by 90°, 180° or 270°.",
             fields=[Field("angle", "Rotate by", "choice", 90, [(90, "90° right"), (270, "90° left"), (180, "180°")]), RANGES],
             suffix=" (rotated)", build=_one("rotate")),
    # ---------------------------------------------------------- to PDF
    ToolSpec("images_to_pdf", "Images to PDF", "to_pdf", "image", "Turn photos, screenshots and scans into a PDF.",
             accepts=PDF_OR_IMAGE, multi=True, batch=False, reorder=True,
             fields=[
                 Field("page_size", "Page size", "choice", "fit", [("fit", "Same as image"), ("A4", "A4"), ("A3", "A3"), ("Letter", "Letter")]),
                 Field("landscape", "Orientation", "choice", "auto", [("auto", "Automatic"), ("portrait", "Portrait"), ("landscape", "Landscape")],
                       visible_if=("page_size", ("A4", "A3", "Letter"))),
                 Field("margin_mm", "Margin", "float", 0, suffix=" mm", maximum=50, visible_if=("page_size", ("A4", "A3", "Letter"))),
             ], suffix=" (images)",
             build=lambda v, ins, out: [{"op": "images_to_pdf", "paths": ins, "output": out, "page_size": v["page_size"],
                                         "landscape": v["landscape"], "margin_mm": v["margin_mm"]}]),
    ToolSpec("scan", "Scan with iPhone", "to_pdf", "iphone",
             "Use your iPhone’s camera to scan paper drawings straight into a PDF (Continuity Camera).",
             custom="scan", available=_avail_scan),
    ToolSpec("office_to_pdf", "Office to PDF", "to_pdf", "office", "Convert Word, Excel, PowerPoint and OpenDocument files to PDF.",
             accepts=OFFICE_EXTS, multi=True, batch=False, output="folder", suffix=" (PDF)", available=_avail_office,
             build=lambda v, ins, out: [{"op": "office_to_pdf", "paths": ins, "out_dir": out}]),
    # ---------------------------------------------------------- from PDF
    ToolSpec("to_images", "PDF to Images", "from_pdf", "to-image", "Save pages as PNG, JPEG or TIFF images.",
             fields=[
                 Field("fmt", "Format", "choice", "png", [("png", "PNG"), ("jpg", "JPEG"), ("tiff", "TIFF")]),
                 Field("dpi", "Resolution", "choice", 200, [(100, "100 DPI"), (150, "150"), (200, "200"), (300, "300"), (600, "600")]),
                 RANGES,
             ], output="folder", suffix=" images",
             build=lambda v, ins, out: [{"op": "to_images", "path": ins[0], "out_dir": out, **v}]),
    ToolSpec("to_word", "PDF to Word", "from_pdf", "word", "Create an editable Word document (.docx).",
             fields=[Field("mode", "Content", "choice", "text", [("text", "Editable text"), ("images", "Page pictures"), ("both", "Both")])],
             output="docx", build=_one("to_word")),
    ToolSpec("to_excel", "PDF to Excel", "from_pdf", "excel", "Pull tables — BOMs, title blocks, schedules — into Excel (.xlsx).",
             fields=[Field("include_text", "Also add a sheet with all text lines", "bool", False)], output="xlsx", build=_one("to_excel")),
    ToolSpec("to_powerpoint", "PDF to PowerPoint", "from_pdf", "presentation", "One slide per page, with the filename as an editable title.",
             fields=[
                 Field("dpi", "Image quality", "choice", 200, [(150, "150 DPI"), (200, "200"), (300, "300")]),
                 Field("title_mode", "Slide title", "choice", "title", [("none", "None"), ("title", "Filename"), ("subtitle", "As subtitle")]),
             ], output="pptx", build=_one("to_powerpoint")),
    ToolSpec("to_text", "PDF to Text", "from_pdf", "text-file", "Extract all text as plain text or Markdown.",
             fields=[Field("markdown", "Markdown with page headings", "bool", False)], output="text", build=_one("to_text")),
    ToolSpec("extract_images", "Extract Images", "from_pdf", "extract-images", "Save the pictures embedded in a PDF.",
             output="folder", suffix=" images", build=lambda v, ins, out: [{"op": "extract_images", "path": ins[0], "out_dir": out}]),
    # ---------------------------------------------------------- optimize
    ToolSpec("compress", "Compress", "optimize", "compress", "Make PDFs smaller for email. Drawings stay sharp.",
             fields=[Field("level", "Compression", "choice", "balanced",
                           [("lossless", "Lossless"), ("balanced", "Balanced"), ("strong", "Strong")],
                           help="Lossless keeps every pixel. Balanced and Strong recompress photos and scans; vector lines are never changed.")],
             suffix=" (compressed)", build=_one("compress")),
    ToolSpec("ocr", "Make Searchable (OCR)", "optimize", "ocr", "Recognise text in scans so you can search and copy it.",
             accepts=PDF_OR_IMAGE,
             fields=[
                 Field("languages", "Languages", "text", "", placeholder="Automatic — or e.g. en-US, de-DE"),
                 Field("skip_text_pages", "Skip pages that already have text", "bool", True),
             ], suffix=" (searchable)", available=_avail_ocr, build=_one("ocr")),
    ToolSpec("repair", "Repair", "optimize", "repair", "Rebuild damaged PDFs that will not open properly.",
             suffix=" (repaired)", build=_one("repair")),
    ToolSpec("flatten", "Flatten", "optimize", "flatten", "Burn comments, markups and form fields into the page.",
             suffix=" (flattened)", build=_one("flatten")),
    # ---------------------------------------------------------- edit
    ToolSpec("page_numbers", "Page Numbers", "edit", "page-number", "Number pages in a corner of your choice.",
             fields=[
                 Field("position", "Position", "choice", "bottom-right",
                       [("bottom-right", "Bottom right"), ("bottom-center", "Bottom centre"), ("bottom-left", "Bottom left"),
                        ("top-right", "Top right"), ("top-center", "Top centre"), ("top-left", "Top left")]),
                 Field("fmt", "Format", "combo", "Page {n} of {total}", ["{n}", "Page {n}", "Page {n} of {total}", "{n} / {total}", "{file} – {n}"],
                       help="{n} page number · {total} page count · {file} file name"),
                 Field("start", "First number", "int", 1, minimum=0, maximum=99999),
                 Field("size", "Text size", "float", 10, suffix=" pt", minimum=4, maximum=72),
                 Field("margin_mm", "Distance from edge", "float", 8, suffix=" mm", maximum=100),
                 Field("color", "Colour", "color", "#000000"),
                 RANGES,
             ], suffix=" (numbered)", build=_one("page_numbers")),
    ToolSpec("watermark", "Watermark & Stamp", "edit", "stamp", "Add DRAFT, APPROVED, SUPERSEDED… or your own text or logo.",
             fields=[
                 Field("style", "Style", "choice", "diagonal", [("diagonal", "Diagonal"), ("stamp", "Stamp box"), ("image", "Image")]),
                 Field("text", "Text", "combo", "DRAFT", ["DRAFT", "PRELIMINARY", "FOR REVIEW", "APPROVED", "REJECTED", "SUPERSEDED",
                                                         "VOID", "CONFIDENTIAL", "FOR INFORMATION ONLY", "AS BUILT"],
                       visible_if=("style", ("diagonal", "stamp"))),
                 Field("add_date", "Add today’s date", "bool", False, visible_if=("style", ("diagonal", "stamp"))),
                 Field("image", "Image", "file", "", file_exts=IMAGE_EXTS, visible_if=("style", "image")),
                 Field("color", "Colour", "color", "#D21E1E", visible_if=("style", ("diagonal", "stamp"))),
                 Field("opacity", "Opacity", "float", 0.3, minimum=0.05, maximum=1.0),
                 Field("behind", "Place behind the drawing", "bool", False),
                 RANGES,
             ], suffix=" (stamped)",
             build=lambda v, ins, out: [{"op": "watermark", "path": ins[0], "output": out, **{k: v[k] for k in v if k != "style"},
                                         "style": "stamp" if v["style"] == "stamp" else "diagonal",
                                         "image": v["image"] if v["style"] == "image" else ""}]),
    ToolSpec("crop", "Crop", "edit", "crop", "Trim white margins automatically, or cut fixed margins.",
             fields=[
                 Field("mode", "Crop", "choice", "auto", [("auto", "Trim to drawing"), ("margins", "Fixed margins")]),
                 Field("margin_mm", "Keep around drawing", "float", 5, suffix=" mm", maximum=100, visible_if=("mode", "auto")),
                 Field("top", "Top", "float", 10, suffix=" mm", maximum=500, visible_if=("mode", "margins")),
                 Field("right", "Right", "float", 10, suffix=" mm", maximum=500, visible_if=("mode", "margins")),
                 Field("bottom", "Bottom", "float", 10, suffix=" mm", maximum=500, visible_if=("mode", "margins")),
                 Field("left", "Left", "float", 10, suffix=" mm", maximum=500, visible_if=("mode", "margins")),
             ], suffix=" (cropped)", build=_one("crop")),
    ToolSpec("resize", "Resize to Sheet", "edit", "resize", "Scale pages onto a standard sheet such as A3 or Letter. Lines stay vector.",
             fields=[Field("paper", "Sheet", "choice", "A3", [(p, p) for p in PAPERS]), ORIENTATION,
                     Field("margin_mm", "Margin", "float", 0, suffix=" mm", maximum=50)],
             suffix=" (resized)", build=_one("resize")),
    ToolSpec("tile", "Tile for Printing", "edit", "tile", "Print a large sheet on A4/A3 paper as overlapping tiles with cut marks.",
             fields=[
                 Field("paper", "Paper", "choice", "A4", [("A4", "A4"), ("A3", "A3"), ("Letter", "Letter"), ("Tabloid", "Tabloid")]),
                 ORIENTATION,
                 Field("scale_pct", "Scale", "float", 100, suffix=" %", minimum=5, maximum=400),
                 Field("overlap_mm", "Overlap", "float", 10, suffix=" mm", maximum=50),
                 Field("labels", "Cut marks and labels", "bool", True),
                 RANGES,
             ], suffix=" (tiled)", build=_one("tile")),
    ToolSpec("nup", "Pages per Sheet", "edit", "nup", "Put 2, 4, 6, 9 or 16 pages on one sheet for overviews.",
             fields=[Field("per_sheet", "Pages per sheet", "choice", 4, [(2, "2"), (4, "4"), (6, "6"), (9, "9"), (16, "16")]),
                     Field("paper", "Sheet", "choice", "A3", [(p, p) for p in PAPERS]), ORIENTATION],
             suffix=" (n-up)", build=_one("nup")),
    ToolSpec("overlay", "Overlay PDF", "edit", "overlay", "Stamp another PDF — e.g. a title block or approval stamp — onto every page.",
             fields=[Field("overlay_path", "Overlay PDF", "file", "", file_exts=PDF),
                     Field("mode", "Use", "choice", "first", [("first", "Its first page on every page"), ("match", "Page by page")]),
                     Field("behind", "Place behind the drawing", "bool", False)],
             suffix=" (overlay)", build=_one("overlay")),
    ToolSpec("metadata", "Edit Metadata", "edit", "tag", "Edit document metadata, or remove metadata, bookmarks, comments, attachments, links and form fields. Page content remains.",
             fields=[Field("strip", "Remove metadata, bookmarks, comments, attachments, links and forms", "bool", False),
                     Field("title", "Title", "text", "", visible_if=("strip", False)),
                     Field("author", "Author", "text", "", visible_if=("strip", False)),
                     Field("subject", "Subject", "text", "", visible_if=("strip", False)),
                     Field("keywords", "Keywords", "text", "", visible_if=("strip", False))],
             suffix=" (metadata)", build=_one("metadata")),
    ToolSpec("markup", "Mark Up", "edit", "markup", "Draw, highlight, add callouts and text with MARK, then save as PDF.",
             custom="markup"),
    # ---------------------------------------------------------- security
    ToolSpec("protect", "Protect", "security", "lock", "Encrypt with a password (AES-256) and limit printing, copying or editing.",
             fields=[Field("user_password", "Password", "password", ""),
                     Field("owner_password", "Permissions password", "password", "", placeholder="Optional"),
                     Field("allow_print", "Allow printing", "bool", True),
                     Field("allow_copy", "Allow copying text", "bool", False),
                     Field("allow_edit", "Allow editing", "bool", False)],
             suffix=" (protected)", build=_one("protect")),
    ToolSpec("unlock", "Unlock", "security", "unlock", "Remove the password from a PDF you are allowed to open.",
             fields=[Field("password", "Password", "password", "")], suffix=" (unlocked)", build=_one("unlock")),
    ToolSpec("redact", "Redact", "security", "redact", "Remove matching searchable text and overlapping graphics. Also removes all metadata, bookmarks, comments, attachments, links and forms. Review images and remaining content.",
             fields=[Field("terms", "Words to remove", "multiline", "", placeholder="One per line, e.g. a customer name or part number"),
                     Field("match_case", "Match case", "bool", False), Field("whole_words", "Whole words only", "bool", False)],
             suffix=" (redacted)", build=_one("redact")),
    # ---------------------------------------------------------- review
    ToolSpec("compare", "Compare Drawings", "compare", "compare",
             "Overlay two revisions: removed lines in red, added in blue, changed areas boxed, plus text changes.",
             custom="compare"),
]

BY_ID = {s.id: s for s in SPECS}


def default_output_name(spec: ToolSpec, first_input: str) -> str:
    stem = Path(first_input).stem
    ext = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "text": ".txt"}.get(spec.output, "")
    return f"{stem}{spec.suffix}{ext}"
