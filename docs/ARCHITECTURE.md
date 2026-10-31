# Architecture

## Decision: one PySide6 codebase, not a separate SwiftUI app

The options I evaluated were:

1. Keep PySide6/Qt for both platforms, with platform-specific polish.
2. Write a SwiftUI frontend for macOS and keep PySide6 for Windows.

I chose the first, based on these points:

| Consideration | Finding |
|---|---|
| PDF rendering quality | MuPDF (PyMuPDF) is excellent, and its *minimum line width* control is what makes thin engineering lines survive. PDFKit has no equivalent, so a SwiftUI app would either lose that or need its own MuPDF bridge. |
| Feature parity | MARK, the PowerPoint writer, the export queue, spell checking and thumbnail caching would all have to be written twice and kept in sync. |
| Native feel on macOS | Qt 6.12 provides everything needed: native menu bar and dialogs, overlay scroll bars, system light/dark mode and accent colour, Retina, trackpad pinch and smart zoom, and Finder open events. The toolbar sits in a unified title bar (an `NSToolbar` with a transparent title bar), and AppKit hit-testing confirms the toolbar receives clicks there. |
| Performance | The real bottleneck in the original was rendering on the UI process, not the toolkit. A Qt app with a process-based renderer measures faster than the original on every axis (see TEST_RESULTS). |

A SwiftUI app would only win on a few native touches, and would double the maintenance cost.

## Process model

```
UI process (Qt, never imports PyMuPDF)
 ├─ RenderService  — priority queue, per-requester cancellation, dispatcher thread
 │    └─ worker processes (spawned, 2–4)  — PyMuPDF only; render tiles, previews,
 │         thumbnails, export PNGs; build .pptx files
 └─ folder scan thread, thumbnail disk-cache loader (QImage only)
```

*Why processes?* Measured during the audit: PyMuPDF holds the GIL while rasterising. A render "in a background thread" stalled the UI thread for 2.26 s on a dense drawing. Worker processes render in true parallel, keep the UI loop free (≤ 15 ms pauses) and isolate crashes from damaged PDFs.

*Threading rules.* Only the GUI thread touches widgets and `QPixmap`. The dispatcher thread reads worker pipes, builds `QImage` objects (thread-safe) and hands results to the GUI thread through a queued signal. Results that arrive after a view or window has closed are dropped; `shiboken6.isValid` guards them.

*Scheduling.* Lower numbers run first:

| Priority | Work |
|---|---|
| 0 | visible tiles and page geometry |
| 1 | page preview |
| 2 | exports |
| 3 | visible thumbnails |
| 5 | prefetch |

Worker 0 only takes priority 0–1 jobs, so a thumbnail backlog can never delay the page in view. Identical requests are merged. Cancelling withdraws one requester's interest, and a job only stops when nobody wants it.

*Memory.* Spare workers exit after 45 s idle. Idle workers release MuPDF's resource store. Tiles and previews share a 320 MB pixmap LRU, and in-memory thumbnails are capped at 72 MB. Background thumbnail prefetch writes PNGs to the disk cache inside the worker and sends no pixels back.

## Sharp page rendering (`ui/page_view.py`)

Scene units are PDF points. For the current zoom `z` and device pixel ratio `d`, the page is split into 512 × 512 device-pixel tiles at scale `s = z·d`. Each tile is rendered from a cached MuPDF display list with a clip, then drawn at a device-pixel-aligned position with a 1:1 pixel mapping.

While a zoom or pinch is in progress, the view draws stand-ins in this order:

1. the file thumbnail
2. a quick whole-page preview
3. tiles from the nearest cached scale

Exact tiles replace them about 110 ms after the gesture settles. `tests/test_page_view.py` compares the widget's pixels with a direct MuPDF render at the same scale.

The fit modes are computed from the scroll-bar-free viewport size. Otherwise they oscillate where scroll bars take up space, as on Windows.

## MARK (`ui/mark/`)

- **Items:** annotations are `QGraphicsItem`s in point units: strokes, arrows, ellipses, text and bubbles. A ghost item previews the active tool at exact size. Selection and hover chrome is suppressed while rendering output.
- **Undo:** a `QUndoStack` with commands for add, remove, move (nudges merge), style, text edit, comment remove/restore and renumber.
- **Output:** the composite is rendered at max(quality, 300) DPI, matching the original. A high-resolution base page, rendered in bands across all workers, is painted first, then the vector markup via `QGraphicsScene.render`. The base page is prefetched when MARK opens, so copying is instant.
- **Previews:** `tool_options.paint_sample` draws exactly what the canvas will draw, scaled by the canvas zoom. The card scales itself down with a "shown at N %" label only when the true size would not fit.

## PowerPoint (`core/pptx_export.py`, `ui/export_dialog.py`)

Slide images are rendered by the workers straight to PNG with DPI metadata and reinforced hairlines. MARKed composites are saved off the UI thread. The deck itself is built inside a worker, so a large export cannot stall the UI. Files are written to a temporary name and then atomically replaced.

## Images as documents (`core/documents.py`)

`open_document` is the one way the workers open a file:

- **Native images:** MuPDF opens PNG, JPEG, TIFF, BMP and GIF directly as one-page documents and applies EXIF orientation.
- **Converted images:** WebP goes through Pillow and HEIC/HEIF through ImageIO on macOS. Both are converted in memory and the file on disk is never touched.
- **Same pipeline:** viewing, thumbnails, MARK, copy and export all reuse the PDF path. Image renders ask for `native_image`, so copies and composites use the image's own pixels instead of a DPI-derived size.

## PDF Tools (`core/pdf_tools.py`, `ui/tools/`)

- **Where tools run:** each tool is a function run inside a render worker as a `"tool"` job (priority 2, its own cancellation group).
- **Shared rules:** outputs may never be one of the inputs, every save is atomic (a temporary file, then `os.replace`), and name clashes get a numbered name.
- **Generated UI:** the panels are built from declarative `ToolSpec`s, with segmented controls for short choices, `visible_if` dependencies and batch/multi modes. Organize, Compare, Scan and Mark Up have custom panels.
- **Compare:** both revisions are rendered at the chosen DPI. The composite (tint, multiply, difference, then clustering into boxes) runs on a `QThreadPool` thread, and text differences come from a worker job.
- **MARK › Save as PDF:** the markup is painted with `QPdfWriter` at 72 dpi (one unit = one point) into an overlay PDF. A worker places it over the original page with `show_pdf_page`, and the overlay is rotated to match `/Rotate` (tested at 0/90/180/270°).
- **UI process isolation:** the UI process still never imports PyMuPDF. `core/tool_support.py` holds the cheap checks (LibreOffice, OCR engine) the UI needs.

## Scan with iPhone (`platform/continuity.py`)

This is a port of the author's independently developed Continuity Camera design, built on Apple's public Services mechanism:

1. **Anchor view.** A transparent anchor `NSView` sits over the toolbar button. It answers `validRequestorForSendType:returnType:` for PDF, HEIC, HEIF, JPEG, PNG and TIFF, and stops all other requests. Otherwise a focused text field would add AutoFill to the menu.
2. **Menu.** A contextual `NSMenu` is shown with `popUpContextMenu:withEvent:forView:`, and AppKit inserts *Take Photo*, *Scan Documents* and *Add Sketch* for each nearby device.
3. **Focus.** The anchor stays first responder until the capture arrives, or for 5 minutes.
4. **Receiving.** `readSelectionFromPasteboard:` reads the bytes synchronously: the promised type first, then every accepted type, then file URLs. The bytes are saved unchanged and the UI work runs on the next event-loop turn.

## Settings and caches

| What | Where |
|---|---|
| Settings JSON | `~/Library/Application Support/ENG Drawing Viewer` (macOS) or `%APPDATA%\ENG Drawing Viewer` (Windows). |
| Thumbnail cache | `~/Library/Caches/ENG Drawing Viewer` or `%LOCALAPPDATA%\ENG Drawing Viewer\Cache`. Keyed by path, mtime and size, pruned at 400 MB, and can be cleared in Settings. |
| Logs | `~/Library/Logs/ENG Drawing Viewer`, `%LOCALAPPDATA%\ENG Drawing Viewer\Logs`, and `logs\startup.log` next to the portable Windows app. |

## Cross-platform structure (2.3.0)

One source tree produces four editions. Everything that defines behaviour is shared; platform code
is confined to small modules with the same interfaces:

```
src/eng_viewer/
  core/        shared: rendering workers, documents, PDF tools, PowerPoint, image/vector/PDF
               editing engines, AI model packs, settings, offline guard
  ui/          shared: every window and workspace; ui/shortcuts.py maps platform key conventions
  platform/
    __init__.py      reveal_in_file_manager() dispatches per platform
    macos.py, continuity.py          spelling (NSSpellChecker), Continuity Camera
    windows*.py, wincom.py           taskbar identity, single instance, spelling, HEIC (WIC),
                                     PowerPoint COM, job objects
    linux.py                         FileManager1.ShowItems, applications-menu entry
packaging/
  macos/       spec (target arch: arm64, x86_64 or universal2), build_macos.sh, Universal inputs
  windows/     spec, Go launcher, build_windows.ps1, installer
  linux/       spec, AppRun, desktop entry, AppStream metadata, build_linux.sh, runtime key
```

Platform choices, all at run time, never by build flags in shared code:

| Concern | macOS | Windows | Linux |
|---|---|---|---|
| Qt platform plugin | cocoa | windows (style windows11) | wayland, falling back to xcb (set by AppRun) |
| File dialogs | native | native | XDG desktop portal theme |
| Window identity | bundle identifier | AppUserModelID `ca-works.ENGDrawingViewer` | desktop file name `io.github.ca_works.ENGDrawingViewer` |
| Settings / cache / logs | ~/Library/… | %APPDATA% / %LOCALAPPDATA% | XDG config / cache folders |
| Worker → UI pixel transfer | anonymous mmap wrapped by QImage | pipe + one copy | anonymous mmap wrapped by QImage |

The render workers return pixels in Qt's display format (B, G, R, 0xFF) and, on POSIX, the dispatch
thread reads them from the pipe into an anonymous memory mapping that `QImage` wraps without a copy.
Converting or copying 30–100 MB bitmaps in the interface process held the GIL long enough to freeze
the UI for hundreds of milliseconds on a busy machine (see TEST_RESULTS.md).

## Packaging invariants

- One version (`src/eng_viewer/__init__.py`) for every edition; one Python (3.13.16) everywhere.
- Exact, hash-checked dependency locks per platform; the Universal edition fuses the macOS lock's
  arm64 and x86_64 wheels and never ships a single-architecture binary (`tools/fuse_macos.py`,
  PyInstaller `target_arch=universal2`, `check_bundle.py --require-archs`).
- Every bundle passes `tools/check_bundle.py` (paths, metadata, network components, architectures,
  minimum OS / glibc) and every shipped file is attributed to a licensed component
  (`tools/collect_licenses.py`).
- The Linux AppImage keeps a build-system library only if a shipped binary needs it and it is not a
  desktop basic, so it neither depends on the build machine nor overrides the user's graphics stack.
- `tools/release_manifest.py` describes exactly four editions from the files actually built.
