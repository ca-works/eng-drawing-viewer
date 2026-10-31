# Platform compatibility

ENG Drawing Viewer 2.3.0 ships as four editions built from one source tree. Every workspace
(Viewer, MARK, PowerPoint export, PDF Tools, Image, Vector, Edit PDF) is the same code on every
edition. This page lists the places where a platform does something differently, and why.

## Editions

| | macOS Universal | macOS Apple Silicon | Windows | Linux |
|---|---|---|---|---|
| Package | `ENG-Drawing-Viewer-macOS-Universal.dmg` | `ENG-Drawing-Viewer-macOS-Apple-Silicon.dmg` | `ENG-Drawing-Viewer-Windows-x64.zip` | `ENG-Drawing-Viewer-Linux-x86_64.AppImage` |
| CPU | x86_64 and arm64 in every binary | arm64 | x86-64 | x86-64 |
| Minimum system | macOS 14.4 | macOS 14.4 | Windows 10 1809 / Windows 11 (declared) | glibc 2.34 (measured from the binaries) |
| How the minimum is known | `LSMinimumSystemVersion`, checked against every bundled binary | same | application manifest and Read Me | `check_bundle.py` reads every ELF file's symbol versions |
| Systems run on so far | see TEST_RESULTS.md | macOS 14.8, 15.7, 26.6, 27 (2.2.x) | Windows Server 2022 and 2025 (CI) | Ubuntu 22.04 and 24.04 (local); Debian 12, Fedora 42, Rocky 9 in CI |
| Python runtime | CPython 3.13.16 (both architectures fused) | CPython 3.13.16 | CPython 3.13.16 | CPython 3.13.16 |
| Installation | drag to Applications | drag to Applications | extract anywhere (optional per-user installer) | single file, `chmod +x` |
| Signing | ad hoc (not notarized) | ad hoc (not notarized) | none | none (SHA-256 checksums) |

## Feature differences

| Feature | macOS | Windows | Linux | Why |
|---|---|---|---|---|
| Spelling in MARK comments | macOS spelling (system languages, learned words) or built-in English | Windows spelling (system dictionaries) or built-in English | built-in English (SCOWL, about 140,000 words) | Linux desktops have no common, always-present spelling service; Hunspell dictionaries vary by distribution |
| HEIC/HEIF photos in the Viewer | Apple ImageIO | Windows Imaging Component; needs Microsoft's free HEIF and HEVC extensions | bundled libheif + libde265 (pillow-heif) | each uses the system decoder where one is guaranteed |
| HEIC/HEIF in the Image Editor | bundled libheif | bundled libheif | bundled libheif | same everywhere |
| Text recognition (Make Searchable) | Apple Vision | Tesseract (bundled language data: eng, deu, fra, ita, nld, por, spa) | Tesseract (same bundled data) | Vision is better and built into macOS |
| Scan with iPhone | Continuity Camera | — | — | an Apple service |
| PowerPoint export (.pptx) | yes | yes | yes | written locally; PowerPoint is not needed |
| Send slides to a presentation open in PowerPoint | — | yes (COM) | — | needs PowerPoint's Windows automation interface |
| Office to PDF | if LibreOffice is installed | if LibreOffice is installed | if LibreOffice is installed | LibreOffice is not bundled (size, licensing) |
| AI acceleration (optional model packs) | Core ML, else CPU | CPU | CPU | ONNX Runtime's macOS wheels include Core ML; DirectML needs a different ONNX Runtime build that is not bundled |
| Clipboard image formats | PNG, TIFF and the chosen format on the pasteboard | PNG, CF_DIBV5 (alpha), CF_DIB, JFIF/TIFF | MIME data of the chosen format plus Qt image data | each system's own clipboard conventions |
| File dialogs | native (Cocoa) | native | native through the XDG desktop portal (GNOME, KDE, …); Qt's own dialog without a portal | the portal works the same on every desktop and in sandboxes |
| Show in file manager | Finder, file selected | Explorer, file selected | file manager via `FileManager1.ShowItems` (file selected), else the folder | |
| Display server | Cocoa | Win32 | Wayland (native) or X11 / XWayland | `AppRun` asks Qt for Wayland first, X11 if that fails |
| Menu integration | menu bar | in-window menus with Alt mnemonics | in-window menus with Alt mnemonics; *Help › Add to Applications Menu* | AppImages are not installed, so the menu entry is offered on request |
| Quit shortcut | ⌘Q | Alt+F4 | Ctrl+Q | each platform's convention |
| Shortcuts with Ctrl+Alt | used (⌃⌥ on Mac keyboards) | never (AltGr) | never (AltGr and desktop shortcuts) | |
| Trackpad pinch / smart zoom | yes | precision touchpads through Qt | through Qt where the compositor sends gestures (Wayland) | not physically tested on Windows or Linux |
| Open files from the desktop | Finder "Open With", drag and drop | file associations from the installer; drag and drop | the desktop entry's MIME types; drag and drop | |

## Shared code and platform modules

- Shared, identical everywhere: `src/eng_viewer/core/` (rendering, documents, PDF tools, PowerPoint,
  image/vector/PDF editing engines, AI model packs, offline guard), `src/eng_viewer/ui/` (all
  windows and workspaces) and `tools/editor_smoke.py`.
- Platform modules, each with tests that run on every platform where possible:
  `platform/macos.py` and `platform/continuity.py` (spelling, Continuity Camera),
  `platform/windows*.py` and `platform/wincom.py` (taskbar identity, single instance, spelling,
  HEIC, PowerPoint COM, job objects), `platform/linux.py` (file manager, applications menu).
- Packaging per platform: `packaging/macos/`, `packaging/windows/`, `packaging/linux/`.

## Known limitations

- **Signing.** No edition is signed with a publisher certificate. macOS asks once to *Open Anyway*;
  Windows SmartScreen may warn on first start; Linux has no signing convention for AppImages (the
  release lists SHA-256 checksums). See DISTRIBUTION.md for what signing would need.
- **Universal under Rosetta.** The CI smoke test runs the Universal app's Intel code on Apple silicon
  under Rosetta 2 and on a real Intel runner. Under Rosetta the render worker processes may start
  as arm64 (macOS chooses the native half of a universal binary for new processes); the Intel
  runner covers a fully Intel run.
- **OpenCV in the Universal edition** is built from source with only the modules the app uses (core,
  imgproc, photo). The other editions use the published OpenCV wheels, which also contain video
  I/O through FFmpeg (and OpenSSL); the app never uses those parts. Same functions, same results.
- **Linux desktops.** The portal file dialog needs `xdg-desktop-portal` and a desktop backend
  (installed by GNOME, KDE and most others). Running the AppImage directly needs FUSE (`fusermount3`);
  without it use `--appimage-extract-and-run`. Physical trackpad gestures, screen readers and
  fractional scaling on Linux have not been tested by hand.
- **Windows.** The Windows edition has run in CI (tests, packaged-app smoke tests, scripted UI pass)
  but not yet in an interactive session with a person: real clipboard pasting into Office, long
  paths and DirectML are unverified.
- **AI models.** No trained model is bundled; optional model packs are installed from local files
  only (docs/image-ai-model-packs.md). The stock ONNX Runtime lists an Azure execution provider; the
  app only ever requests Core ML, DirectML or CPU and blocks network access.
