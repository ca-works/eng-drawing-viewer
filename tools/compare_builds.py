#!/usr/bin/env python3
"""Compare two builds of the Mac app file by file.

    python tools/compare_builds.py "a/ENG Drawing Viewer.app" "b/ENG Drawing Viewer.app"

Prints how many files are identical, and lists every file that differs or exists in only one
build. Exit status 0 means the two apps are byte-for-byte identical.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def tree(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = "link:" + str(p.readlink())
        elif p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def main():
    a, b = (Path(x) for x in sys.argv[1:3])
    ta, tb = tree(a), tree(b)
    same = [k for k in ta if tb.get(k) == ta[k]]
    differ = sorted(k for k in ta if k in tb and tb[k] != ta[k])
    only_a = sorted(set(ta) - set(tb))
    only_b = sorted(set(tb) - set(ta))
    print(f"{len(same)} of {len(set(ta) | set(tb))} entries identical")
    for label, items in (("differ", differ), (f"only in {a}", only_a), (f"only in {b}", only_b)):
        for k in items:
            print(f"  {label}: {k}")
    sys.exit(0 if not (differ or only_a or only_b) else 1)


if __name__ == "__main__":
    main()
