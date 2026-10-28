#!/usr/bin/env python3
"""Build the built-in spelling dictionary from SCOWL (Spell Checker Oriented Word Lists).

    python tools/build_wordlist.py scowl-2020.12.07.tar.gz

Download the archive from https://sourceforge.net/projects/wordlist/files/SCOWL/2020.12.07/
The script checks its SHA-256, then writes:

- src/eng_viewer/resources/english_words.txt: lower-case, sorted, one word per line
- licenses/SCOWL.txt: SCOWL's copyright and licence notice, copied verbatim

It uses SCOWL size 70 and smaller ("large", the size recommended for spell checkers),
American and British spellings, and leaves out abbreviations, proper names, contractions
and special-purpose lists. The output is the same every time for the same archive.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import tarfile
from pathlib import Path

SCOWL_VERSION = "2020.12.07"
SCOWL_SHA256 = "5587667caa20c4891390c2d42dbb4d5c4c3f41bee77af1457ece3ba23fb859cc"
SIZES = (10, 20, 35, 40, 50, 55, 60, 70)
SPELLINGS = ("english", "american", "british")
CATEGORIES = ("words",)  # not: upper (names), abbreviations, contractions, proper-names
WORD = re.compile(r"^[a-z]+(?:'[a-z]+)?$")

ROOT = Path(__file__).resolve().parents[1]


def build(archive: Path) -> tuple[list[str], bytes]:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != SCOWL_SHA256:
        raise SystemExit(f"Unexpected SCOWL archive (sha256 {digest}); expected {SCOWL_SHA256}.")
    words: set[str] = set()
    copyright_text = b""
    wanted = {f"final/{s}-{c}.{n}" for s in SPELLINGS for c in CATEGORIES for n in SIZES}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            # Read members as data only; nothing is extracted to disk.
            if not member.isfile():
                continue
            name = member.name.split("/", 1)[-1]
            if name in wanted:
                text = tar.extractfile(member).read().decode("iso-8859-1")
                for line in text.splitlines():
                    w = line.strip().lower()
                    if WORD.match(w):
                        words.add(w)
            elif name == "Copyright":
                copyright_text = tar.extractfile(member).read()
    if not copyright_text or len(words) < 50_000:
        raise SystemExit("The archive did not contain the expected SCOWL files.")
    return sorted(words), copyright_text


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    words, copyright_text = build(args.archive)
    out = ROOT / "src" / "eng_viewer" / "resources" / "english_words.txt"
    out.write_text("\n".join(words) + "\n", encoding="utf-8", newline="\n")
    notice = (
        f"The built-in spelling dictionary (src/eng_viewer/resources/english_words.txt) is generated\n"
        f"by tools/build_wordlist.py from SCOWL {SCOWL_VERSION} (http://wordlist.aspell.net/),\n"
        f"sizes up to 70, American and British spellings. SCOWL's copyright notice follows.\n\n"
    ).encode() + copyright_text
    (ROOT / "licenses" / "SCOWL.txt").write_bytes(notice)
    print(f"Wrote {len(words)} words to {out.relative_to(ROOT)} and licenses/SCOWL.txt")


if __name__ == "__main__":
    main()
