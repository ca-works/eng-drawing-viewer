The first public release of **ENG Drawing Viewer**: a free, offline workspace for engineering drawings on the Mac. View folders of PDF drawings and images sharply at any zoom, mark them up with numbered review comments, convert them with 32 PDF tools and turn them into PowerPoint slides, all on your own computer.

ENG Drawing Viewer is free software under the GNU Affero General Public License v3 or later.

## Download

| File | For |
|---|---|
| `ENG-Drawing-Viewer-{version}-macOS-AppleSilicon.dmg` | **Mac with Apple silicon** (M1 or newer), **macOS 14.4 or later**. Open it and drag the app to Applications. |
| `ENG-Drawing-Viewer-{version}-macOS-AppleSilicon.zip` | The same app as a ZIP. |
| `eng-drawing-viewer-{version}-source.zip` | The complete source code of this version, with build instructions. |
| `SHA256SUMS.txt` | Checksums of every file. |
| Upstream source archives | The unmodified source code of the copyleft libraries inside the app (Qt, Qt for Python, MuPDF, PyMuPDF, lxml and GNU libiconv, PyInstaller). |

A Windows version is being rebuilt and will follow.

**First launch:** the app is free software from an independent developer and is not notarized by Apple. The first time you open it, macOS says it could not verify the app: click **Done**, open **System Settings › Privacy & Security**, click **Open Anyway** next to the message about ENG Drawing Viewer, and confirm. You only do this once.

**Check your download:**

```bash
shasum -a 256 -c SHA256SUMS.txt --ignore-missing
```

## What's in it

- **View:** folders and nested subfolders of PDFs and images, thumbnails, sheet sizes, natural sorting, sharp tiled rendering from 1.5 % to 4800 %.
- **MARK:** pen, highlighter, arrow, circle, text, eraser and numbered callouts with linked, spell-checked comments; undo and redo; copy the marked page or save it as a PDF with vector markup. The original PDF is never changed.
- **PowerPoint:** queue pages and export editable `.pptx` slides without PowerPoint; marked pages put the drawing on the right and the numbered comments on the left.
- **PDF Tools:** 32 offline tools to organize, convert, optimize, edit, secure and compare drawings. *Office to PDF* needs LibreOffice installed.
- **Scan with iPhone:** Continuity Camera scans arrive as PDFs.
- **Private:** no account, no telemetry, no network connections.

## Built and checked

- Built from the published source archive with Python 3.13 and hash-checked dependencies; two independent builds produce byte-identical apps.
- Tested on macOS 14, 15, 26 and 27 (Apple silicon) with no network activity.
- Every bundled component and its licence is listed in the app under **Help › About › Third-Party Notices**.

Changes in detail: [CHANGELOG.md](https://github.com/ca-works/eng-drawing-viewer/blob/main/CHANGELOG.md)

*GitHub also attaches "Source code (zip/tar.gz)" archives of this repository automatically; they contain the same source as `eng-drawing-viewer-{version}-source.zip`.*
