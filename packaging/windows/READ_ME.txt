ENG Drawing Viewer for Windows (64-bit)
=======================================

Requirements: Windows 10 (version 1809 or later) or Windows 11, on a 64-bit
(x64) PC. Windows on ARM PCs run it through Windows' built-in x64 emulation.

Portable version (ZIP)
----------------------
1. Right-click the ZIP file, choose "Extract All...", and extract it to a folder
   you can write to, for example your Documents folder or a USB drive.
   (Do not run it from inside the ZIP.)
2. Open the extracted folder and double-click "ENG Drawing Viewer".

Nothing is installed and nothing is written to the folder. Settings are kept in
%APPDATA%\ENG Drawing Viewer and the thumbnail cache and logs in
%LOCALAPPDATA%\ENG Drawing Viewer. To remove the app, delete the folder (and
those two folders if you want to remove your settings too).

Installer
---------
The Setup program installs the app for your user account only. It does not
need administrator rights, adds ENG Drawing Viewer to the Start menu and to
"Open with" for PDFs and images, and can be removed from Settings > Apps.

Open it the first time
----------------------
ENG Drawing Viewer is free software from an independent developer and is not
code-signed, so Microsoft Defender SmartScreen may say "Windows protected your
PC" the first time. Click "More info", check that the app is "ENG Drawing
Viewer", then click "Run anyway". You only do this once per download.

Please download the app only from https://ca-works.github.io/eng-drw/ and check
the SHA-256 checksum listed there. In PowerShell:

    Get-FileHash .\ENG-Drawing-Viewer-*-Windows-x64-Portable.zip

Optional extras
---------------
* HEIC photos (from iPhones): install the free "HEIF Image Extensions" and
  "HEVC Video Extensions" from the Microsoft Store. ENG Drawing Viewer uses the
  decoder Windows provides; it does not include one.
* Office to PDF: install LibreOffice (free) from https://www.libreoffice.org.
* "Scan with iPhone" is available only in the Mac version.

Privacy
-------
Everything runs on your PC. The app has no account, no telemetry and makes no
network connections. Your PDFs are opened read-only and are never changed.

Licence and source code
-----------------------
Copyright (C) 2026 ca-works.

ENG Drawing Viewer is free software: you can redistribute it and/or modify it
under the terms of the GNU Affero General Public License as published by the
Free Software Foundation, either version 3 of the License, or (at your option)
any later version. It comes WITHOUT ANY WARRANTY. See "Licence.txt".

The complete source code for this version is published with the release:
https://github.com/ca-works/eng-drawing-viewer/releases

Third-party notices are in the "legal" folder and inside the app, under
Help > About ENG Drawing Viewer.
