"""Derive every icon file from the supplied 1024×1024 artwork.

The supplied PNG is the blue rounded-square icon on an opaque white square.
This script:
  * masks the white corners to transparency (radius measured from the art),
  * writes the runtime PNGs used by the app,
  * builds the macOS .icns on Apple's icon grid (824 px body, soft shadow),
  * rebuilds the Windows .ico with all standard sizes from the masked art.

Usage: python packaging/make_icons.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets" / "app_icon_1024.png"
RES = ROOT / "src" / "eng_viewer" / "resources"
OUT = ROOT / "assets" / "generated"
RADIUS = 181  # corner radius of the supplied artwork at 1024 px


def masked(src: Image.Image) -> Image.Image:
    scale = 4
    size = src.size[0]
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * scale - 1, size * scale - 1), RADIUS * scale, fill=255)
    mask = mask.resize(src.size, Image.LANCZOS)
    out = src.convert("RGBA")
    out.putalpha(mask)
    return out


def mac_canvas(art: Image.Image) -> Image.Image:
    """Apple grid: 824 px body centred on a 1024 canvas with a subtle shadow."""
    canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    body = art.resize((824, 824), Image.LANCZOS)
    shadow = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    sh_mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(sh_mask).rounded_rectangle((100, 112, 924, 936), int(RADIUS * 824 / 1024), fill=90)
    sh_mask = sh_mask.filter(ImageFilter.GaussianBlur(14))
    shadow.putalpha(sh_mask)
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.alpha_composite(body, (100, 100))
    return canvas


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    art = masked(Image.open(SRC))
    art.save(OUT / "app_icon_masked_1024.png")
    art.resize((512, 512), Image.LANCZOS).save(RES / "app_icon_512.png")
    art.resize((256, 256), Image.LANCZOS).save(RES / "app_icon_256.png")

    ico_sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    art.save(OUT / "ENG_Drawing_Viewer.ico", sizes=[(s, s) for s in ico_sizes])
    shutil.copy(OUT / "ENG_Drawing_Viewer.ico", RES / "ENG_Drawing_Viewer.ico")

    mac = mac_canvas(art)
    mac.save(OUT / "app_icon_mac_1024.png")
    if sys.platform == "darwin":
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "AppIcon.iconset"
            iconset.mkdir()
            for base in (16, 32, 128, 256, 512):
                for mult in (1, 2):
                    px = base * mult
                    name = f"icon_{base}x{base}{'@2x' if mult == 2 else ''}.png"
                    mac.resize((px, px), Image.LANCZOS).save(iconset / name)
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT / "AppIcon.icns")], check=True)
    print(f"Icons written to {OUT} and {RES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
