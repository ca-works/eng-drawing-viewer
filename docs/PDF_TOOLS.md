# PDF Tools

The **PDF Tools** tab (toolbar switch *Viewer | PDF Tools*, or ⌘⌥T on macOS, Ctrl+Alt+T on Windows) holds the editing and conversion tools. Every tool runs on this computer. Results are always written to **new files**, and the originals are never changed.

![PDF Tools](images/tools-home-light.png)

## How the tool list was chosen

Before building, I surveyed the tool sets of the three best-known PDF toolboxes:

- [iLovePDF](https://www.ilovepdf.com/), a cloud service
- [PDF24 Tools](https://tools.pdf24.org/en/), a cloud service with an offline Windows app
- [Stirling-PDF](https://docs.stirlingpdf.com/), self-hosted and open source

Their common core is organize, merge, split, compress, convert both ways, OCR, page numbers, watermark, protect, unlock and redact. All of it is here, rebuilt for offline use. Some tools were added because engineering drawings need them:

- **Compare Drawings:** a revision overlay.
- **Tile for Printing:** print an A0 sheet on A4/A3 paper.
- **Resize to Sheet:** scale a page onto a standard sheet.
- **Pages per Sheet:** several pages on one sheet.
- **Overlay PDF:** stamp a title block onto pages.
- **PDF to Excel:** pull out BOMs and schedules.
- **Scan with iPhone.**

Some tools were left out because they need a server or an account, which the offline rule excludes:

- e-signature workflows
- cloud storage pickers
- "chat with PDF" and AI summaries
- HTML/URL to PDF, which fetches web pages

## The tools

| Group | Tools |
|---|---|
| Organize | **Organize Pages** (visual: drag to reorder, rotate, duplicate, insert blank pages, delete, undo/redo, mix PDFs and images), Merge, Split (every page, every N pages, ranges), Extract Pages, Remove Pages, Rotate |
| Convert to PDF | Images to PDF (paper size, orientation, margins, one PDF or one per image), **Scan with iPhone**, Office to PDF |
| Convert from PDF | PDF to Images (PNG/JPEG/TIFF at chosen DPI), PDF to Word (editable text, page pictures or both), PDF to Excel (detected tables, optionally all text), PDF to PowerPoint, PDF to Text / Markdown, Extract Images |
| Optimize | Compress (lossless / balanced / strong; vector linework is never rasterised), Make Searchable (OCR), Repair, Flatten |
| Edit | Page Numbers, Watermark & Stamp (diagonal text, a stamp box such as APPROVED with today's date, or an image), Crop (auto-trim white margins, or fixed), Resize to Sheet, Tile for Printing (overlap, cut marks, labels), Pages per Sheet, Overlay PDF, Edit Metadata (or strip it), Mark Up (MARK, then *Save as PDF*) |
| Security | Protect (AES-256, with print/copy/edit permissions), Unlock, Redact (search terms; the text is removed, not just covered) |
| Review | **Compare Drawings**: removed linework in red, added in blue, unchanged in black. Changed areas are boxed in orange and text changes are listed. The result can be saved as a PDF. |

Page ranges accept `1-3, 5`, `odd`, `even`, `last`, `3-end` and reversed ranges such as `9-1`.

## Getting files in

- Drag files or folders onto the tool, the window, the Organize grid or the PowerPoint queue.
- Use **Add Files…**, **From Library** (the current selection) or **Current Drawing**.
- Right-click files in the library and choose *PDF Tools ›* (Merge, Organize, Compress, Images to PDF and more).
- Drop Word, Excel or PowerPoint files anywhere and the app opens *Office to PDF* for them.

## Images

PNG, JPEG, TIFF, BMP, GIF, WebP and HEIC/HEIF (iPhone photos) open like one-page drawings:

- They appear in the library next to PDFs. Turn this off with *View › Include Images*.
- They can be MARKed, copied, queued for PowerPoint and used in every tool.
- Photos are shown upright using their EXIF orientation.
- Copies and exports use the image's own pixels, never upscaled.

## Scan with iPhone (macOS)

The iPhone button in the toolbar (or *File › Scan with iPhone…*, ⌘⇧I) opens Apple's Continuity Camera menu, with **Take Photo**, **Scan Documents** and **Add Sketch** for each nearby iPhone or iPad.

- **Saving:** a scan arrives as a PDF and is saved unchanged in `~/Documents/ENG Drawing Viewer Scans`, then opened in the viewer. From there you can MARK it, OCR it, merge it or export it.
- **Requirements:** the iPhone must be signed in to the same Apple Account, with Wi-Fi and Bluetooth on.
- **Privacy:** the app never uses a camera itself and opens no network connection. The transfer is Apple's own link between your devices.

The implementation is a port of the author’s independently developed and device-tested approach:

- An anchor view answers AppKit's `validRequestorForSendType:returnType:`.
- The menu is a contextual `NSMenu`, so AppKit inserts its own device items.
- The scan is read synchronously in `readSelectionFromPasteboard:`.

## Save marked pages as PDF

**MARK › File › Save Marked Page as PDF…** (⌘S / Ctrl+S) writes the page with the markup on top **as vectors**. Lines and text stay sharp at any zoom, and the drawing underneath stays vector too. **Export as PDF** in the PowerPoint queue (or *File › Export Queue as PDF…*) combines queued pages and MARKed pages into one PDF.

## Optional helpers (installed separately, still offline)

| Tool | Needs |
|---|---|
| Office to PDF | [LibreOffice](https://www.libreoffice.org/) installed in its standard location (it runs headless and locally). The tool is greyed out with a note when LibreOffice is missing. |
| OCR on Windows | [Tesseract](https://github.com/tesseract-ocr/tesseract) on `PATH`. On macOS OCR uses Apple's on-device Vision framework, so nothing needs installing. |

Nothing is ever downloaded by the app.

## How it works

The tools are plain functions in `core/pdf_tools.py`. They run inside the existing render worker processes as `"tool"` jobs, so the UI never blocks and a crash in a damaged file cannot take the app down. The rules:

- **Inputs are never outputs.** Every tool refuses to write over any of its inputs.
- **Atomic saves.** Each save goes to a temporary file in the target folder, then `os.replace`.
- **Never silently overwrite.** Name clashes get "name 2.pdf", "name 3.pdf" and so on.

The UI is generated from declarative `ToolSpec`s (`ui/tools/specs.py`). The custom panels are Organize, Compare, Scan and Mark Up.
