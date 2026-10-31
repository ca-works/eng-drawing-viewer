ENG Drawing Viewer for Linux (x86-64)
=====================================

This is a single, self-contained AppImage. Nothing is installed and nothing is downloaded.

Start it:
  1. Make it executable:   chmod +x ENG-Drawing-Viewer-*-Linux-x86_64.AppImage
     (or File Properties > Permissions > "Allow executing file as program")
  2. Double-click it, or run it from a terminal.

Add it to your applications menu: Help > Add to Applications Menu. If you later move the
AppImage, choose it again. Help > Remove from Applications Menu removes the entry.

Requirements:
  - x86-64 Linux with glibc 2.34 or later, for example Ubuntu 22.04, Debian 12, Fedora 35,
    RHEL / Rocky Linux / AlmaLinux 9, Linux Mint 21, or later releases
  - an X11 or Wayland desktop session, with the libraries every desktop installs: graphics
    (OpenGL/EGL), fontconfig, FreeType, xkbcommon, GLib and D-Bus
  - FUSE to run the AppImage directly: the fusermount3 (or fusermount) program, from the fuse3
    (or fuse) package that desktop installs include. Without FUSE (some minimal installs and
    containers), run it with:  ./ENG-Drawing-Viewer-*.AppImage --appimage-extract-and-run

Native file dialogs come from the desktop portal (xdg-desktop-portal), which GNOME, KDE and most
other desktops run. The Office to PDF tool uses LibreOffice if it is installed.

Check the download:
  sha256sum -c SHA256SUMS.txt --ignore-missing

ENG Drawing Viewer is free software under the GNU Affero General Public License v3 or later.
Help > About lists every bundled component and its licence.
