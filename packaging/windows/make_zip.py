# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 ca-works
"""Pack a folder into a ZIP whose bytes depend only on the files: sorted, fixed timestamps.

    python packaging/windows/make_zip.py "release/windows/ENG Drawing Viewer" OUTPUT.zip

The folder becomes the single top-level folder in the archive, so "Extract All" gives one folder.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    folder, output = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    tmp = output.with_suffix(".tmp")
    # SOURCE_DATE_EPOCH (set by the build script from the source's commit time) or 1980-01-01.
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "315532800"))
    stamp = max(epoch, 315532800)
    import time

    date_time = time.gmtime(stamp)[:6]
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            name = f"{folder.name}/{p.relative_to(folder).as_posix()}"
            info = zipfile.ZipInfo(name, date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0  # MS-DOS/Windows
            info.external_attr = 0x20  # FILE_ATTRIBUTE_ARCHIVE
            zf.writestr(info, p.read_bytes(), compresslevel=9)
    tmp.replace(output)
    print(f"wrote {output} ({output.stat().st_size / 1e6:.1f} MB, {len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
