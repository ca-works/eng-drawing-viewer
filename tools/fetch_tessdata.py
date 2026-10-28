#!/usr/bin/env python3
"""Download the Tesseract language data bundled for text recognition (OCR) and check its hashes.

    python tools/fetch_tessdata.py build/tessdata

PyMuPDF's MuPDF includes the Tesseract OCR engine; it only needs these trained models. They are
the "fast" integer models from the tessdata_fast 4.1.0 release (Apache License 2.0). Every file is
checked against the SHA-256 below; a mismatch stops the build. Files already present with the right
hash are not downloaded again.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

RELEASE = "4.1.0"
BASE = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{RELEASE}/"
FILES = {
    "eng.traineddata": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "deu.traineddata": "19d219bbb6672c869d20a9636c6816a81eb9a71796cb93ebe0cb1530e2cdb22d",
    "fra.traineddata": "ced037562e8c80c13122dece28dd477d399af80911a28791a66a63ac1e3445ca",
    "ita.traineddata": "b8f89e1e785118dac4d51ae042c029a64edb5c3ee42ef73027a6d412748d8827",
    "nld.traineddata": "ced0e5e046a84c908a6aa7accbef9a232c4a5d9a8276691b81c6ee64d02963f6",
    "por.traineddata": "c4932b937207a9514b7514d518b931a99938c02a28a5a5a553f8599ed58b7deb",
    "spa.traineddata": "6f2e04d02774a18f01bed44b1111f2cd7f3ba7ac9dc4373cd3f898a40ea6b464",
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        dest = out / name
        if dest.is_file() and sha256(dest) == expected:
            continue
        with urllib.request.urlopen(BASE + name, timeout=120) as resp:  # noqa: S310 (fixed https URL)
            data = resp.read()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            print(f"{name}: SHA-256 {actual} does not match the pinned {expected}", file=sys.stderr)
            return 1
        dest.write_bytes(data)
    print(f"tessdata_fast {RELEASE}: {len(FILES)} files verified in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
