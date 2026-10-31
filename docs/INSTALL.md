# Installing ENG Drawing Viewer

Every edition is a self-contained app: nothing else needs to be installed (no Python, no runtime),
and the app never connects to the internet. Download it from https://ca-works.github.io/eng-drw/
or the project's GitHub releases, and check it against the SHA-256 checksum published with it.

## Mac

Two editions with the same features:

- **Universal** (`ENG-Drawing-Viewer-macOS-Universal.dmg`) runs natively on Intel and Apple silicon Macs.
- **Apple Silicon** (`ENG-Drawing-Viewer-macOS-Apple-Silicon.dmg`) is a smaller download for M-series Macs.

Both need macOS 14.4 or later.

1. Check the download: `shasum -a 256 ~/Downloads/ENG-Drawing-Viewer-macOS-*.dmg`
2. Open the disk image and drag **ENG Drawing Viewer** onto **Applications**.
3. Open the app. It is free software from an independent developer and is not notarized by Apple,
   so the first time macOS says it could not verify the app. Click **Done**, open
   **System Settings › Privacy & Security**, click **Open Anyway** next to the message about
   ENG Drawing Viewer, and confirm. You only do this once.

Please don't turn off Gatekeeper for the whole Mac; *Open Anyway* is enough.

**Uninstall:** drag the app to the Bin. To remove what it stored, also delete
`~/Library/Application Support/ENG Drawing Viewer` (settings), `~/Library/Caches/ENG Drawing Viewer`
(thumbnails), `~/Library/Logs/ENG Drawing Viewer` (error log, only after a crash) and, if you no
longer need them, the scans in `~/Documents/ENG Drawing Viewer Scans`.

## Windows

`ENG-Drawing-Viewer-Windows-x64.zip`, for Windows 10 (version 1809 or later) or Windows 11, 64-bit.

1. Check the download (PowerShell): `Get-FileHash ENG-Drawing-Viewer-Windows-x64.zip`
2. Extract the ZIP anywhere (Desktop, Documents, a USB stick). No administrator rights are needed.
3. Start **ENG Drawing Viewer.exe** in the extracted folder. Keep the `app` and `legal` folders next to it.

The program is not code-signed, so SmartScreen may say "Windows protected your PC" the first time:
click **More info › Run anyway**. HEIC photos need Microsoft's free *HEIF Image Extensions* and *HEVC
Video Extensions* from the Microsoft Store (the app tells you if they are missing).

**Uninstall:** delete the folder. Settings are in `%APPDATA%\ENG Drawing Viewer`, the thumbnail cache
and error log in `%LOCALAPPDATA%\ENG Drawing Viewer`.

## Linux

`ENG-Drawing-Viewer-Linux-x86_64.AppImage`, for 64-bit (x86-64) Linux with glibc 2.34 or later:
Ubuntu 22.04, Debian 12, Fedora 35, RHEL / Rocky Linux / AlmaLinux 9, Linux Mint 21 or later.
It runs on X11 and Wayland desktops.

1. Check the download: `sha256sum -c SHA256SUMS.txt --ignore-missing`
2. Make it executable: `chmod +x ENG-Drawing-Viewer-Linux-x86_64.AppImage`
   (or *Properties › Permissions › Allow executing file as program*).
3. Double-click it, or run it from a terminal.
4. Optional: *Help › Add to Applications Menu* puts it in your desktop's menu (and *Remove from
   Applications Menu* takes it out again). If you move the AppImage, add it again.

AppImages need FUSE to run directly (the `fuse3` package, which desktop installations include). Where
FUSE is missing, such as in some containers, run it with `--appimage-extract-and-run`. Native file
dialogs come from the desktop portal (`xdg-desktop-portal`), which GNOME, KDE and most desktops run.

**Uninstall:** delete the AppImage (and remove the menu entry first if you added one). Settings are in
`~/.config/ENG Drawing Viewer`, the thumbnail cache and error log in `~/.cache/ENG Drawing Viewer`.

## Good to know

- Everything runs on your computer. The app has no account and makes no network connections.
- Settings, the thumbnail cache and the error log can contain file names and folder paths. Only the
  cache can be cleared from Settings; delete the folders above to remove the rest.
- MARK annotations are temporary until you copy, save or queue the page; opening MARK never changes the PDF.
- *Office to PDF* needs LibreOffice installed.
- *Redact* removes matching searchable text and the drawing underneath from pages. It does not find text
  that is drawn as lines or embedded in images. Check the result before you share it.
- Differences between the editions: [COMPATIBILITY.md](COMPATIBILITY.md).
