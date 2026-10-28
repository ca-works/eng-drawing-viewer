# Installing ENG Drawing Viewer

## Mac (Apple silicon)

1. Download `ENG-Drawing-Viewer-<version>-macOS-AppleSilicon.dmg` from https://ca-works.github.io/eng-drw/ or from the project's GitHub releases.
2. Optional but recommended: check the download against the SHA-256 checksum published with the release.

   ```bash
   shasum -a 256 ~/Downloads/ENG-Drawing-Viewer-*-macOS-AppleSilicon.dmg
   ```

3. Open the disk image and drag **ENG Drawing Viewer** onto **Applications**.
4. Open the app. ENG Drawing Viewer is free software from an independent developer and is not notarized by Apple, so the first time macOS says it could not verify the app. Click **Done**, open **System Settings › Privacy & Security**, scroll down and click **Open Anyway** next to the message about ENG Drawing Viewer, then confirm. You only need to do this once.

Please don't turn off Gatekeeper for your whole Mac to open the app; *Open Anyway* is enough.

The requirements for each release (macOS version, Apple silicon) are listed on the download page. A ZIP of the app is also published for people who prefer it to the disk image.

## Windows

A new Windows version is being built and is not part of this release.

## Uninstall

Drag the app from Applications to the Bin. To remove everything it stored, also delete:

- `~/Library/Application Support/ENG Drawing Viewer` (settings)
- `~/Library/Caches/ENG Drawing Viewer` (thumbnail cache)
- `~/Library/Logs/ENG Drawing Viewer` (error log, only written after a crash)
- `~/Documents/ENG Drawing Viewer Scans` (scans made with your iPhone; keep these if you still need them)

## Good to know

- Everything runs on your computer. The app has no account and makes no network connections.
- Settings, the thumbnail cache, the error log and saved scans can contain file names and folder paths. Only the cache can be cleared from Settings; delete the folders above to remove the rest.
- MARK annotations are temporary until you copy, save or queue the page.
- *Office to PDF* needs LibreOffice installed.
- *Redact* removes matching searchable text from pages. It does not find text that is drawn as lines or embedded in images. Check the result before you share it.
