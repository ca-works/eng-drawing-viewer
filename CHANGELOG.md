# Changelog

## 2.3.0 — four editions: macOS Universal, macOS Apple Silicon, Windows x64, Linux x86-64

One version for every platform, built from the same source. Includes the Windows rebuild that was
published as the 2.2.1 Windows Preview (below) and brings its shared fixes to the other platforms.

### New
- **Image, Vector and Edit PDF workspaces**, each on its own tab next to the Viewer and PDF Tools:
  layered image editing with selections, masks, Magic Eraser, healing and offline AI model packs;
  editable SVG/DXF vector geometry with raster tracing; PDF text boxes, shapes, images, real
  redaction, page crop/rotate and Save As (see docs/IMAGE_EDITOR.md, VECTOR_EDITOR.md, PDF_EDITOR.md).
- **Linux edition** (x86-64 AppImage): native Wayland or X11, desktop-portal file dialogs, Show in
  File Manager selects the file, *Help › Add to Applications Menu*, HEIC/HEIF photos through the
  bundled libheif, OCR with bundled Tesseract language data, Ctrl+Q to quit.
- **macOS Universal edition** (Intel and Apple Silicon in one app) next to the Apple Silicon edition.

### Faster
- The interface stays responsive while pages render on busy machines: the longest pause with three
  render workers at full load dropped from 190–640 ms to 10–22 ms (measured on Linux; the change
  applies to macOS too). Page pixels now arrive in display format and are shown without a copy.

### Fixed
- The licence collector counts the editors' self-test module as part of the app (Windows and macOS
  release builds would have stopped on it).

### Security and privacy
- Every Mac edition ships a minimal OpenCV (core, imgproc, photo) built from the official source
  release, without FFmpeg, OpenSSL, libssh or image codecs.

## 2.2.1 — Windows release

ENG Drawing Viewer for Windows 10 and 11 (x64), rebuilt as a standalone app: a portable ZIP and a per-user installer that needs no administrator rights. The Mac app gets the shared fixes below.

### New on Windows
- A small native launcher shows the splash at once, opens files in the window that is already running, and is what the taskbar pins.
- Title bars follow the app's light or dark appearance and, on Windows 11, match the toolbar.
- **Make Searchable (OCR)** works out of the box with bundled Tesseract language data (English, German, French, Italian, Dutch, Portuguese, Spanish).
- **HEIC photos** open through Windows' own decoder once the free HEIF and HEVC extensions from the Microsoft Store are installed; the app says so if they're missing.
- **Spelling** uses Windows' spelling dictionaries and remembers the words you add (or the built-in English dictionary).
- **Send to an open presentation**: slides can go straight into a presentation open in PowerPoint.
- Windows shortcuts: Ctrl+, (Settings), F5 (Rescan), F1 (Keyboard Shortcuts), Exit, Alt-key menu mnemonics, and no Ctrl+Alt shortcuts (they clash with AltGr).
- Open drawings are no longer locked: you can rename, move or replace a PDF while it's shown.
- Show in Explorer selects the file. Folders on FAT/exFAT and network drives, hidden and system folders are scanned correctly.

### Fixed (all platforms)
- Words added with *Learn Spelling* in the built-in dictionary are kept after restarting.
- Redo no longer lists the same key twice (which could disable it).
- The Organizer's Undo/Redo tooltips show this computer's keys.
- Closing a MARK window twice in quick succession is harmless.

## 2.2.0 — first public release

ENG Drawing Viewer is now free and open-source software under the GNU AGPL v3 or later.

### New
- **About** shows the copyright, the licence and where to get the source code, with the full licence and third-party notices one click away.
- **Spelling dictionary** rebuilt from SCOWL (about 140,000 English words, American and British spellings).
- **PDF to Images** lowers the resolution for very large pages instead of failing, and says so in the result.

### Security and privacy
- The app now refuses network connections in its own code, in the main process and in every render worker; tests check this.
- PDF to Excel stores text as text: formulas and links in a drawing's text can no longer become live spreadsheet formulas.
- Protect: when you leave the permissions password empty, a random one is generated instead of one derived from the open password.
- Tools refuse to write over an input file even through a hard link, symbolic link or different capitalisation.
- Remove Metadata and Redact now also remove bookmarks, comments, attachments, links and form fields. Redaction still only finds searchable text, as the tool explains.
- Image and page sizes are checked before memory is allocated, so damaged or extreme files can't exhaust memory.
- LibreOffice conversions run in a contained process group that is stopped completely on timeout.
- Saves are atomic and temporary files are always cleaned up, including Compare's.
- Thumbnail cache names use SHA-256.

### Changed
- Built with Python 3.13 and hash-checked dependencies.
- The one-time import of settings from the app's earlier configuration file has been removed.

## 2.1.0 — 2026-10-24

### New
- **PDF Tools tab.** A second workspace next to the viewer (*Viewer | PDF Tools*) with 32 offline tools, chosen after surveying iLovePDF, PDF24 and Stirling-PDF and adding the ones engineering drawings need. See [docs/PDF_TOOLS.md](docs/PDF_TOOLS.md).
  - **Organize:** Organize Pages (a visual grid to reorder, rotate, duplicate, insert blanks, delete and undo, mixing PDFs and images), Merge, Split, Extract, Remove, Rotate.
  - **Convert to PDF:** Images to PDF, Scan with iPhone, Office to PDF (uses LibreOffice if installed).
  - **Convert from PDF:** PDF to Images, Word, Excel (tables), PowerPoint, Text/Markdown; Extract Images.
  - **Optimize:** Compress (vector linework stays vector), Make Searchable (OCR; Apple Vision on macOS, Tesseract on Windows if installed), Repair, Flatten.
  - **Edit:** Page Numbers, Watermark & Stamp, Crop (auto-trim), Resize to Sheet, Tile for Printing, Pages per Sheet, Overlay PDF, Edit/Strip Metadata, Mark Up.
  - **Security:** Protect (AES-256 with permissions), Unlock, Redact (text is removed, not covered).
  - **Review:** Compare Drawings (removed in red, added in blue, changed areas boxed, text changes listed; save as PDF).
  - **Batch and safety:** batch tools process many files at once. Results are always new files: tools refuse to write over an input, and saves are atomic.
- **Scan with iPhone (macOS).** A toolbar button and *File › Scan with iPhone…* (⌘⇧I) open Apple's Continuity Camera menu (Take Photo / Scan Documents / Add Sketch). Scans are saved unchanged as PDFs in `~/Documents/ENG Drawing Viewer Scans` and open immediately. This is ported from the author's own device-tested implementation.
- **Images are documents.** PNG, JPEG, TIFF, HEIC/HEIF, WebP, BMP and GIF open, list, MARK, copy, queue and export like one-page drawings.
  - Photos are shown upright using their EXIF orientation.
  - Copies and composites use the image's own resolution.
  - The library shows the image type and pixel size.
  - *View › Include Images* switches them off.
- **Drag and drop everywhere.**
  - **Window:** drop files or folders onto the window to open them.
  - **Tools:** drop onto a tool or the Organize grid to add inputs.
  - **Queue:** drop onto the PowerPoint queue to queue every page.
  - **Office files:** Word, Excel and PowerPoint files open *Office to PDF*.
- **MARK › Save Marked Page as PDF** (⌘S / Ctrl+S): the page plus the markup as vectors, correct on rotated pages.
- **Export the queue as one PDF**, with MARKed pages included: the queue's *PDF* button, or *File › Export Queue as PDF…*.
- **Library context menu:** Open, MARK… and *PDF Tools ›* shortcuts for the selected files.

### Changed
- Opening a file that is already in the library now opens it in place instead of reloading the library from that file's folder.

## 2.0.0 — 2026-10-24

A rebuild of the original single-file app into a structured, tested application for macOS and Windows. All original features are kept.

### Viewing and performance
- **Sharp at every zoom.** Pages are drawn from 512 px tiles rendered at the exact screen pixel density (Retina, 4K, fractional Windows scaling), with no enlarged bitmaps. Tests show the on-screen pixels match MuPDF's own output exactly at 600 % and 2400 %. Previously MARK magnified a single raster, and the main viewer rendered at half resolution on Retina.
- **No more freezes.** PDF rendering moved into separate worker processes. PyMuPDF holds Python's global lock while rendering, so the original's "background" thread rendering blocked the UI. On a dense A1 sheet the UI used to freeze for about 1.9 s and now pauses for at most 15 ms.
- **Crash-safe.** A damaged PDF that crashes the PDF engine only takes down a worker. The file is reported as unreadable and the app carries on.
- **Fast start.** The packaged Mac app shows its window in about 0.35 s and the first page about 30 ms later.
- **Fast with big folders.** Folder scanning runs off the UI thread, and 500 PDFs in nested folders scan in about 6 ms. Thumbnails are prioritised so visible rows come first. A background disk cache fills without holding pixels in memory: 500 cold thumbnails took 0.7 s, and warm rows load in about 0.3 ms each.
- **Zoom and panning.** Zoom is anchored at the pointer for Ctrl/⌘+scroll and pinch, and at the viewport centre for toolbar and keyboard zoom, with a short animation. Mouse-wheel scrolling on Windows is smoothed. Pan by dragging, middle-dragging or Space-dragging, and double-click to smart-zoom. The zoom range is 1.5 %–4800 %.
- **Instant flipping.** Neighbouring pages and PDFs are prefetched.
- **Enhance thin lines.** Hairlines can be shown at least one device pixel wide. Exports still use the original's 0.9 pt reinforcement.

### Interface
- **New layout:** a library sidebar, page viewer and inspector (document info, page grid, PowerPoint queue), all resizable and collapsible.
- **Icons:** a consistent, hand-drawn SVG set replaces the emoji and Unicode symbols. Icons render crisp at any scale and recolour with the theme.
- **Light and dark themes** follow the system setting, or can be forced in View › Appearance or Settings.
- **macOS:** the toolbar sits in the title bar like Finder and Preview, with native menus, dialogs, overlay scroll bars, file-open events (Finder "Open With", drop on the Dock icon) and a properly shaped icon.
- **Windows:** a modern flat style with slim scroll bars, Segoe UI Variable, high-DPI support and a taskbar identity.
- **Feedback and empty states:** transient confirmations ("Page 3 copied at 220 DPI") appear as toasts. Empty and error states are clear, and unreadable PDFs show the reason instead of a dialog.
- **Keyboard:** full menus with shortcuts, a keyboard-shortcut reference, and accessible names on icon buttons.

### Library
- **Natural sorting** handles case, accents and decomposed filenames (macOS) and leading zeros. Folders sort before files.
- **Rows** show the page count and sheet size (A0–A5, ANSI), and the folder in the flat view.
- **Filtering** matches file and folder names, so typing a folder name shows everything inside it.
- **Skipped entries:** hidden folders and macOS `._` resource-fork files are ignored, symlink loops are handled, and unreadable folders are reported.
- **Opening:** recent folders, opening individual PDFs, and drag and drop of folders or PDFs.
- **Session restore:** the last folder, drawing and page are reopened.

### MARK
- **Annotations are vector, in PDF units,** so they stay sharp on screen and in 300 DPI output.
- **Live size previews:** while you adjust any slider, a floating card shows the stroke, highlighter, arrow, circle, text or numbered bubble at true on-screen size for the current zoom. An exact-size ghost also appears on the drawing, and `[` / `]` resize with the same preview.
- **Wide ranges:** sliders are logarithmic. Callout bubbles range from 8 to 600 pt (originally 20–300), pens and arrows from 0.25 to 24 pt, and text from 4 to 144 pt.
- **Callout bubbles:** the black number is optically centred and auto-sized for 1–3 digits, the outline scales with the bubble, and white fill is optional.
- **Comments:** bubble and comment highlight each other, and selecting a comment pulses its bubble.
  - The red ✕ removes only the comment, and you can add it back.
  - Deleting a bubble removes its comment, and undo restores both, text included.
  - Callouts can be renumbered 1…n.
  - Comments copy as numbered text.
- **Spell checking:** on macOS it uses the system checker with your language and learned words. Elsewhere it uses the bundled offline dictionary plus drafting terms, with suggestions, Add to Dictionary and Ignore.
- **Text and shapes:** text is typed directly on the drawing (no dialog) and can be edited by double-clicking. Shift snaps arrows to 15° and makes perfect circles, and Alt draws circles from the centre.
- **Editing:** select, move, nudge with the arrow keys, restyle selected items, and drag the eraser across items.
- **Undo/redo** covers every action, including comment removal and renumbering.
- **Window behaviour:** MARK has its own window and menus and doesn't block the main window. It warns before discarding markup that hasn't been copied or queued.

### PowerPoint
- **No PowerPoint required:** export writes `.pptx` files directly with python-pptx, on macOS and Windows.
- **Destinations:**
  - a new presentation (16:9 or 4:3)
  - inserted into an existing deck at a slide number
  - placed onto existing slides, as the original did
  - on Windows with PowerPoint running, the original "send to open presentation" option
- **Slide layout:**
  - the filename is an editable title or subtitle, and unused placeholders are removed
  - marked slides put the drawing on the right and numbered comments, with bold numbers and auto-sized text, on the left
- **Queue:** reorder by drag and drop, see thumbnails and MARKED badges, use the context menu (show page, move to top/bottom, remove) and remove with Delete.
- **Progress:** exporting shows a progress view with cancel, then Open and Show in Finder/Explorer.
- **Quality:** images render in parallel on the worker pool at 150/220/300 DPI with DPI metadata. Images are capped at 90 MP so A0 sheets can't exhaust memory.

### Reliability and packaging
- **No runtime installs:** the app never downloads anything, and the source build has no Conda or `pip install` at runtime.
- **macOS:** a `.app` bundle (Apple Silicon), DMG and ZIP, ad-hoc signed and smoke-tested at build time.
- **Windows:** a reproducible build with pinned versions, isolated venv, tests, smoke test and portable ZIP. The improved native launcher is DPI-aware, keeps its splash animating, gets a ready signal from the app, forwards file arguments and reports early exits.
- **Settings and logs:** settings were migrated from the original application’s local configuration. Errors are logged to the user's Logs folder.
- **Tests:** an automated suite of 55 tests in about 8 s, plus scripted UI sessions and a benchmark.

### Fixed (found during the rebuild)
- The fit-width zoom could oscillate forever where scroll bars take up space (Windows).
- Background thumbnail threads could run PyMuPDF calls concurrently, which PyMuPDF does not support.
- Middle-mouse panning, arrow-key navigation and zoom anchoring were reimplemented consistently for the viewer and MARK.
