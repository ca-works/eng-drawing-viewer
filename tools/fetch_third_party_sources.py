#!/usr/bin/env python3
"""Download the exact source code of the copyleft components inside the apps.

    python tools/fetch_third_party_sources.py release/sources

The apps bundle libraries under the GNU LGPL, GPL and AGPL (Qt and Qt for Python, MuPDF and PyMuPDF,
GNU libiconv inside lxml, libheif, libde265 and x265 inside pillow-heif, and on Linux libfuse inside
the AppImage runtime). Each release publishes these upstream source archives, or points to the
earlier release that already carries the identical archive, so anyone can rebuild or modify them.
The Windows-only libiconv build inputs (lxml's Windows packages) are listed in the 2.2.1 release. Downloads resume after interruptions and are checked against
the checksum each project publishes (Qt publishes MD5 sums; the others SHA-256).
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

QT = "https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules"
PYSIDE = "https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src"
PYPI = "https://files.pythonhosted.org/packages/source"

# (file name, URL, algorithm, expected digest, what it is)
SOURCES = [
    ("qtbase-everywhere-src-6.12.0.tar.xz", f"{QT}/qtbase-everywhere-src-6.12.0.tar.xz", "md5", None,
     "Qt 6.12.0 base modules: QtCore, QtGui, QtWidgets, QtDBus, Cocoa platform and style"),
    ("qtsvg-everywhere-src-6.12.0.tar.xz", f"{QT}/qtsvg-everywhere-src-6.12.0.tar.xz", "md5", None,
     "Qt 6.12.0 SVG module and plugins"),
    ("qtimageformats-everywhere-src-6.12.0.tar.xz", f"{QT}/qtimageformats-everywhere-src-6.12.0.tar.xz", "md5", None,
     "Qt 6.12.0 image format plugins (TIFF, WebP, HEIF, JPEG 2000, ICNS, TGA, WBMP)"),
    # Qt for Python publishes its checksum on the download's .mirrorlist page.
    ("pyside-setup-everywhere-src-6.12.0.tar.xz", f"{PYSIDE}/pyside-setup-everywhere-src-6.12.0.tar.xz", "sha256",
     "099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751",
     "Qt for Python 6.12.0 (PySide6 and Shiboken6)"),
    ("mupdf-1.28.2-source.tar.gz", "https://mupdf.com/downloads/archive/mupdf-1.28.2-source.tar.gz", "sha256", None,
     "MuPDF 1.28.2 with its third-party libraries"),
    ("pymupdf-1.28.2.tar.gz", f"{PYPI}/p/pymupdf/pymupdf-1.28.2.tar.gz", "sha256",
     "5e0be7908a715aa20333caddd73f1d6f01e4cd0c26e869fa2dd0b7f344da2249", "PyMuPDF 1.28.2"),
    ("lxml-6.1.3.tar.gz", f"{PYPI}/l/lxml/lxml-6.1.3.tar.gz", "sha256",
     "45222d94ddd511536f3b2f7d9deae3b2339b4ce0f075f1ca25703b07cad9dd21", "lxml 6.1.3 (statically links libiconv)"),
    ("libiconv-1.18.tar.gz", "https://ftp.gnu.org/pub/gnu/libiconv/libiconv-1.18.tar.gz", "sha256",
     "3b08f5f4f9b4eb82f151a7040bfd6fe6c6fb922efe4b1659c66ea933276965e8", "GNU libiconv 1.18 (inside lxml)"),
    ("pyinstaller-6.22.3.tar.gz", f"{PYPI}/p/pyinstaller/pyinstaller-6.22.3.tar.gz", "sha256",
     "05eb2f5615503e72939a7224d68b4aff572c6b0438ee4a17d0a4b481f399362d", "PyInstaller 6.22.3, including the bootloader"),
    # 2.3.0: the HEIF stack inside pillow-heif (all editions), the AppImage runtime (Linux) and the
    # OpenCV source release the Mac editions are built from (Apache-2.0; with packaging/opencv/).
    ("pillow_heif-1.8.1.tar.gz", f"{PYPI}/p/pillow_heif/pillow_heif-1.8.1.tar.gz", "sha256",
     "521ebffb8a181d56c3904e5a61f20903edee0d9d3275967b8fb345f866215c06", "pillow-heif 1.8.1 and its library build recipes"),
    ("libheif-1.23.6.tar.gz", "https://github.com/strukturag/libheif/releases/download/v1.23.6/libheif-1.23.6.tar.gz",
     "sha256", None, "libheif 1.23.6 (LGPL-3.0), inside pillow-heif"),
    ("libde265-1.1.3.tar.gz", "https://github.com/strukturag/libde265/releases/download/v1.1.3/libde265-1.1.3.tar.gz",
     "sha256", None, "libde265 1.1.3 (LGPL-3.0), inside pillow-heif"),
    ("x265-4.3.tar.gz", "https://github.com/Multicorewareinc/x265/archive/refs/tags/4.3.tar.gz", "sha256", None,
     "x265 4.3 (GPL-2.0-or-later), inside pillow-heif"),
    ("appimage-type2-runtime-20251108.tar.gz", "https://github.com/AppImage/type2-runtime/archive/refs/tags/20251108.tar.gz",
     "sha256", None, "AppImage type 2 runtime 20251108 (MIT; builds the statically linked libfuse 3, LGPL-2.1)"),
    ("opencv_python_headless-5.0.0.93.tar.gz", f"{PYPI}/o/opencv-python-headless/opencv_python_headless-5.0.0.93.tar.gz",
     "sha256", "b82f9831daab90b725c7c1ee1b36cb5732c367096ac76d119e64e14eb70d5f3c",
     "OpenCV 5.0.0.93 source release (Apache-2.0) of the Mac editions' minimal build"),
]


def fetch(url: str, dest: Path):
    subprocess.run(["curl", "-fL", "--retry", "10", "--retry-all-errors", "--retry-delay", "3",
                    "-C", "-", "-sS", "-o", str(dest), url], check=True)


def digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def qt_md5(dest: Path, base: str, name: str) -> str:
    sums = dest / f"md5sums-{abs(hash(base)) % 10_000}.txt"
    if not sums.exists():
        fetch(f"{base}/md5sums.txt", sums)
    for line in sums.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0]
    raise SystemExit(f"{name} is not listed in {base}/md5sums.txt")


def main():
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "release/sources")
    dest.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, url, algo, expected, what in SOURCES:
        path = dest / name
        if algo == "md5":
            expected = qt_md5(dest, url.rsplit("/", 1)[0], name)
        if not path.exists() or (expected and digest(path, algo) != expected):
            print(f"Downloading {name} …", flush=True)
            fetch(url, path)
        actual = digest(path, algo)
        if expected and actual != expected:
            path.unlink()
            raise SystemExit(f"{name}: {algo} {actual} does not match the published {expected}; deleted.")
        note = f"matches the published {algo}" if expected else "no published checksum; recorded below"
        sha = digest(path, "sha256")
        print(f"{name}: {note}")
        lines.append(f"{sha}  {name}  # {what}; {url}")
    (dest / "THIRD_PARTY_SOURCES.txt").write_text(
        "Source code of the copyleft components bundled in ENG Drawing Viewer,\n"
        "exactly as published by their projects. SHA-256, file, description and origin:\n\n"
        + "\n".join(lines) + "\n")
    for f in dest.glob("md5sums-*.txt"):
        f.unlink()


if __name__ == "__main__":
    main()
