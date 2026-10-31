**ENG Drawing Viewer {version}** for Mac, Windows and Linux: a free, offline workspace for engineering drawings. View folders of PDF drawings and images sharply at any zoom, mark them up with numbered review comments, edit images, vectors and PDFs, convert them with 32 PDF tools and turn them into PowerPoint slides, all on your own computer.

ENG Drawing Viewer is free software under the GNU Affero General Public License v3 or later.

## Download

| File | For |
|---|---|
| `ENG-Drawing-Viewer-macOS-Universal.dmg` | **Any Mac**, Intel or Apple silicon, **macOS 14.4 or later** |
| `ENG-Drawing-Viewer-macOS-Apple-Silicon.dmg` | **Mac with Apple silicon** (M1 or newer), macOS 14.4 or later: a smaller download |
| `ENG-Drawing-Viewer-Windows-x64.zip` | **Windows 10 (1809 or later) or 11**, 64-bit: extract and start `ENG Drawing Viewer.exe` |
| `ENG-Drawing-Viewer-Linux-x86_64.AppImage` | **Linux**, 64-bit (x86-64), glibc 2.34 or later (Ubuntu 22.04, Debian 12, Fedora 35, RHEL/Rocky 9 or later): `chmod +x` and start it |
| `eng-drawing-viewer-{version}-source.zip` | The complete source code of this version, with build instructions |
| `SHA256SUMS.txt` | Checksums of every file |
| Upstream source archives | The unmodified source code of the copyleft libraries inside the apps |

All four editions have the same features; [COMPATIBILITY.md](https://github.com/ca-works/eng-drawing-viewer/blob/main/docs/COMPATIBILITY.md) lists the few platform differences.

**First launch:** the apps are free software from an independent developer and are not signed with a publisher certificate.
- *Mac:* macOS says it could not verify the app: click **Done**, open **System Settings › Privacy & Security**, click **Open Anyway** and confirm. Once only.
- *Windows:* SmartScreen may say it protected your PC: click **More info › Run anyway**.
- *Linux:* make the AppImage executable first; *Help › Add to Applications Menu* adds it to your desktop's menu.

**Check your download:**

```bash
shasum -a 256 -c SHA256SUMS.txt --ignore-missing      # Mac
sha256sum -c SHA256SUMS.txt --ignore-missing          # Linux
```

```powershell
Get-FileHash ENG-Drawing-Viewer-Windows-x64.zip       # Windows: compare with SHA256SUMS.txt
```

## What's in it

- **View:** folders and nested subfolders of PDFs and images, thumbnails, sheet sizes, natural sorting, sharp tiled rendering from 1.5 % to 4800 %.
- **MARK:** pen, highlighter, arrow, circle, text, eraser and numbered callouts with linked, spell-checked comments; undo and redo; copy the marked page or save it as a PDF with vector markup. The original PDF is never changed.
- **PowerPoint:** queue pages and export editable `.pptx` slides without PowerPoint; marked pages put the drawing on the right and the numbered comments on the left.
- **Image, Vector and Edit PDF:** layered image editing, real SVG/DXF vector editing with raster tracing, and PDF editing with genuine redaction.
- **PDF Tools:** 32 offline tools to organize, convert, optimize, edit, secure and compare drawings. *Office to PDF* needs LibreOffice installed.
- **Private:** no account, no telemetry, no network connections.

## Built and checked

- Built from the published source archive with Python 3.13 and hash-checked dependencies.
- The Universal Mac app contains Intel and Apple silicon code in every binary, and was tested on both.
- Every bundled component and its licence is listed in the app under **Help › About › Third-Party Notices**.

Changes in detail: [CHANGELOG.md](https://github.com/ca-works/eng-drawing-viewer/blob/main/CHANGELOG.md)

*GitHub also attaches "Source code (zip/tar.gz)" archives of this repository automatically; they contain the same source as `eng-drawing-viewer-{version}-source.zip`.*
