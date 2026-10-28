#!/usr/bin/env python3
"""Copy the licence texts of bundled native libraries out of their official source archives.

    python tools/curate_licenses.py release/sources

Python packages describe their own licences, but the Qt libraries inside PySide6, the libraries
inside MuPDF, and GNU libiconv inside lxml don't. This reads the archives downloaded by
tools/fetch_third_party_sources.py and writes, under licenses/curated/:

  Qt/        the LGPL-3.0 and GPL licence texts, and the attribution of every third-party library
             compiled into the Qt modules and plugins the app ships (from qt_attribution.json)
  MuPDF/     MuPDF's licence and the licence of each library in its thirdparty/ folder
  libiconv/  the LGPL-2.1 text from GNU libiconv 1.18

tools/collect_licenses.py then includes these in THIRD_PARTY_NOTICES.txt.
"""
from __future__ import annotations

import json
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "licenses" / "curated"

# Qt source folders whose code ends up in the shipped modules and plugins.
QT_SHIPPED = re.compile(
    r"/src/(corelib|gui|widgets|dbus|plugins/(platforms/cocoa|platforms/minimal|platforms/offscreen|styles/mac"
    r"|imageformats|iconengines)|3rdparty|svg|imageformats)/"
)
LICENCE_NAME = re.compile(r"(^|/)(LICEN[CS]E|COPYING|COPYRIGHT|NOTICE)([._-][A-Za-z0-9.+-]*)?(\.txt|\.md)?$", re.I)


def write(rel: str, data: bytes):
    dest = OUT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def qt(sources: Path):
    attributions = []
    for archive in ("qtbase-everywhere-src-6.12.0.tar.xz", "qtsvg-everywhere-src-6.12.0.tar.xz",
                    "qtimageformats-everywhere-src-6.12.0.tar.xz"):
        with tarfile.open(sources / archive) as tar:
            module = archive.split("-")[0]
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                name = m.name.split("/", 1)[1]
                if module == "qtbase" and re.match(r"LICENSES/(LGPL-3\.0-only|GPL-2\.0-only|GPL-3\.0-only|"
                                                   r"Qt-GPL-exception-1\.0)\.txt$", name):
                    write(f"Qt/{PurePosixPath(name).name}", tar.extractfile(m).read())
                if name.endswith("qt_attribution.json") and QT_SHIPPED.search("/" + name):
                    data = json.loads(tar.extractfile(m).read(), strict=False)
                    for entry in data if isinstance(data, list) else [data]:
                        attributions.append((module, name, entry))
    lines = ["Third-party code in the Qt 6.12.0 source modules qtbase, qtsvg and qtimageformats, as declared",
             "by Qt's own qt_attribution.json files. The Mac app ships QtCore, QtGui, QtWidgets, QtDBus and",
             "QtSvg with the Cocoa platform, macOS style, image-format and SVG icon plugins. Some components",
             "below serve other platforms (for example Wayland, Android or Direct3D) and are not compiled",
             "into the Mac app; they are listed so that nothing is left out.", ""]
    for module, path, e in sorted(attributions, key=lambda x: (x[2].get("Name", ""), x[1])):
        lines.append(f"{e.get('Name', '?')} {e.get('Version', '')}".rstrip())
        lines.append(f"  in {module}/{PurePosixPath(path).parent}")
        if e.get("License"):
            lines.append(f"  Licence: {e['License']}")
        if e.get("Copyright"):
            copyright_text = e["Copyright"] if isinstance(e["Copyright"], str) else "\n".join(e["Copyright"])
            lines += ["  " + c for c in copyright_text.splitlines()]
        lines.append("")
    write("Qt/ATTRIBUTIONS.txt", "\n".join(lines).encode())
    print(f"Qt: {len(attributions)} attributions")


# MuPDF's third-party libraries that are compiled into the shipped libmupdf (curl and freeglut only
# serve MuPDF's own viewer programs and are not included), and each one's licence file.
MUPDF_LIBS = ("brotli", "cmark-gfm", "extract", "freetype", "gumbo-parser", "harfbuzz", "jbig2dec", "lcms2",
              "leptonica", "libjpeg", "mujs", "openjpeg", "tesseract", "zint", "zlib", "zxing-cpp")
MUPDF_EXTRA = {"thirdparty/libjpeg/README", "thirdparty/leptonica/leptonica-license.txt",
               # Fonts built into MuPDF
               "resources/fonts/han/LICENSE.txt", "resources/fonts/noto/COPYING",
               "resources/fonts/sil/OFL.txt", "resources/fonts/urw/OFL.txt"}


def mupdf(sources: Path):
    count = 0
    with tarfile.open(sources / "mupdf-1.28.2-source.tar.gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            name = m.name.split("/", 1)[1]
            parts = name.split("/")
            third = (len(parts) == 3 and parts[0] == "thirdparty" and parts[1] in MUPDF_LIBS
                     and LICENCE_NAME.search(name)) or name in MUPDF_EXTRA
            if name == "COPYING" or third:
                write(f"MuPDF/{name.replace('/', '__')}", tar.extractfile(m).read())
                count += 1
    print(f"MuPDF: {count} licence files")


def libiconv(sources: Path):
    with tarfile.open(sources / "libiconv-1.18.tar.gz") as tar:
        for m in tar.getmembers():
            if m.isfile() and m.name.split("/", 1)[1] in ("COPYING.LIB", "COPYING"):
                write(f"libiconv/{PurePosixPath(m.name).name}", tar.extractfile(m).read())
    print("libiconv: done")


def main():
    sources = Path(sys.argv[1] if len(sys.argv) > 1 else "release/sources")
    if OUT.exists():
        import shutil
        shutil.rmtree(OUT)
    qt(sources)
    mupdf(sources)
    libiconv(sources)


if __name__ == "__main__":
    main()
