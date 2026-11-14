"""Regression tests for the minimal OpenCV build (core, imgproc, photo; no video I/O, no FFmpeg).

Every edition of 2.3.0 ships OpenCV built by tools/build_opencv_minimal.sh (macOS, Linux) or
tools/build_opencv_minimal_windows.py (Windows). These tests check that every OpenCV name the app
uses exists in the installed cv2 and that every feature built on OpenCV still produces correct,
exported results (files written and read back, not just return values). They run with whichever
OpenCV is installed; with ENG_REQUIRE_MINIMAL_OPENCV=1 (release builds) they also require that the
installed cv2 is the minimal build.
"""
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

cv2 = pytest.importorskip("cv2")

from eng_viewer.core.image_export import encode_image, export_file  # noqa: E402
from eng_viewer.core.image_tools import (  # noqa: E402
    color_selection, erase_pixels, heal, magic_erase, perspective, quick_selection, refine_selection,
    remove_background,
)
from eng_viewer.core.vector_trace import simplify_object, trace_image  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FFMPEG_NAMES = re.compile(r"avcodec|avformat|avutil|swscale|swresample|videoio_ffmpeg|libssl|libcrypto|opencl", re.I)


def _count(mask):
    return int(np.count_nonzero(np.asarray(mask) > 127))


def _app_cv2_names():
    names = set()
    # The application and the frozen self-test bundled with it (tools/editor_smoke.py).
    for path in [*(ROOT / "src").rglob("*.py"), ROOT / "tools" / "editor_smoke.py"]:
        if path.is_file():
            names |= set(re.findall(r"\bcv2\.([A-Za-z_][A-Za-z0-9_]*)", path.read_text(encoding="utf-8")))
    return sorted(names)


def test_every_opencv_name_the_app_uses_exists():
    names = _app_cv2_names()
    assert len(names) >= 40, names  # the scan itself works
    missing = [n for n in names if not hasattr(cv2, n)]
    assert not missing, f"the installed OpenCV lacks {missing}"


def _cv2_files():
    base = Path(cv2.__file__).resolve().parent
    out = [p for p in base.rglob("*") if p.is_file()]
    libs = base.parent / "opencv_python_headless.libs"
    if libs.is_dir():
        out += [p for p in libs.iterdir() if p.is_file()]
    return out


@pytest.mark.skipif(os.environ.get("ENG_REQUIRE_MINIMAL_OPENCV") != "1",
                    reason="set ENG_REQUIRE_MINIMAL_OPENCV=1 to require the minimal OpenCV build")
def test_installed_opencv_is_the_minimal_build():
    info = cv2.getBuildInformation()
    modules = re.search(r"To be built:\s*(.*)", info).group(1).split()
    # flann and geometry are built because OpenCV 5's photo/imgproc depend on them (no external code).
    assert {"core", "imgproc", "photo"} <= set(modules) <= {"core", "imgproc", "photo", "python3", "flann", "geometry"}, modules
    assert "videoio" not in modules and not hasattr(cv2, "VideoCapture")
    assert "imgcodecs" not in modules and not hasattr(cv2, "imread")
    assert not re.search(r"FFMPEG:\s*YES", info) and not re.search(r"Use OpenCL:\s*YES", info, re.I)
    assert not re.search(r"(?:/home/|/Users/|[A-Z]:\\Users\\)(?!runner)", info), "builder path in build information"
    bad = [p.name for p in _cv2_files() if FFMPEG_NAMES.search(p.name)]
    assert not bad, bad


def _two_tone(size=(160, 120)):
    image = Image.new("RGB", size, (235, 235, 235))
    ImageDraw.Draw(image).rectangle((40, 30, 119, 89), fill=(30, 60, 200))
    return image


def test_magic_wand_selects_exactly_the_clicked_colour_region():
    image = _two_tone()
    inside = color_selection(image, (80, 60), tolerance=10, contiguous=True)
    assert _count(inside) == 80 * 60
    outside = color_selection(image, (2, 2), tolerance=10, contiguous=True)
    assert _count(outside) == 160 * 120 - 80 * 60
    # Non-contiguous: two separate squares of one colour, both selected.
    two = Image.new("RGB", (100, 50), "white")
    d = ImageDraw.Draw(two); d.rectangle((5, 5, 24, 24), fill="red"); d.rectangle((60, 5, 79, 24), fill="red")
    assert _count(color_selection(two, (10, 10), tolerance=5, contiguous=False)) == 2 * 400
    assert _count(color_selection(two, (10, 10), tolerance=5, contiguous=True)) == 400


def test_quick_selection_follows_the_object_edge():
    image = _two_tone()
    mask = quick_selection(image, [(60, 50), (100, 70)], radius=6)
    m = np.asarray(mask) > 127
    assert m[60, 80] and m[35, 45] and m[85, 115]      # object interior and near its corners
    assert not m[5, 5] and not m[60, 10] and not m[110, 150]   # background stays unselected
    assert abs(int(m.sum()) - 80 * 60) < 0.05 * 80 * 60
    removed = quick_selection(image, [(80, 60)], radius=6, existing=mask, subtract=True)
    assert _count(removed) < _count(mask) * 0.2


def test_background_removal_keeps_the_subject_and_exports_alpha(tmp_path):
    image = Image.new("RGB", (200, 150), (250, 250, 250))
    ImageDraw.Draw(image).ellipse((60, 35, 140, 115), fill=(200, 40, 40))
    mask = remove_background(image)
    m = np.asarray(mask) > 127
    assert m[75, 100] and not m[5, 5] and not m[140, 190]
    cut = image.convert("RGBA"); cut.putalpha(mask)
    out = tmp_path / "cutout.png"
    export_file(encode_image(cut, "PNG"), out)
    back = Image.open(out); back.load()
    assert back.mode == "RGBA" and back.size == (200, 150)
    assert back.getpixel((100, 75))[3] == 255 and back.getpixel((3, 3))[3] == 0


def test_magic_eraser_and_masking_hide_only_the_matching_area(tmp_path):
    image = _two_tone().convert("RGBA")
    mask = magic_erase(image, (5, 5), tolerance=10, contiguous=True, antialias=False)
    m = np.asarray(mask)
    assert m[60, 80] == 255 and m[5, 5] == 0
    hidden = erase_pixels(image, ImageChops_invert(mask))
    out = tmp_path / "erased.webp"
    export_file(encode_image(hidden, "WEBP", quality=100), out)
    back = Image.open(out).convert("RGBA")
    assert back.getpixel((80, 60))[3] > 250 and back.getpixel((5, 5))[3] < 5


def ImageChops_invert(mask):
    from PIL import ImageChops
    return ImageChops.invert(mask)


def test_refine_grow_shrink_smooth_use_morphology():
    mask = Image.new("L", (100, 100), 0)
    ImageDraw.Draw(mask).rectangle((30, 30, 69, 69), fill=255)
    assert _count(refine_selection(mask, grow=5)) > _count(mask) + 4 * 40 * 4
    assert _count(refine_selection(mask, grow=-5)) == 30 * 30
    speck = mask.copy(); speck.putpixel((5, 5), 255)
    assert _count(refine_selection(speck, smooth=2)) == pytest.approx(_count(mask), abs=24)  # median rounds the corners
    assert np.asarray(refine_selection(speck, smooth=2))[5, 5] == 0


def test_heal_fills_a_hole_from_its_surroundings():
    image = Image.new("RGBA", (80, 80), (90, 140, 60, 255))
    ImageDraw.Draw(image).rectangle((35, 35, 44, 44), fill=(255, 0, 255, 255))
    hole = Image.new("L", (80, 80), 0)
    ImageDraw.Draw(hole).rectangle((33, 33, 46, 46), fill=255)
    healed = heal(image, hole)
    r, g, b, a = healed.getpixel((40, 40))
    assert abs(r - 90) < 12 and abs(g - 140) < 12 and abs(b - 60) < 12 and a == 255


def test_perspective_and_resize_export_have_the_expected_size(tmp_path):
    image = Image.new("RGBA", (120, 100), "white")
    ImageDraw.Draw(image).polygon([(20, 10), (100, 20), (110, 90), (10, 80)], fill="black")
    flat = perspective(image, [(20, 10), (100, 20), (110, 90), (10, 80)])
    assert 80 <= flat.width <= 105 and 70 <= flat.height <= 85
    assert flat.getpixel((flat.width // 2, flat.height // 2))[:3] == (0, 0, 0)
    small = flat.resize((flat.width // 2, flat.height // 2), Image.Resampling.LANCZOS)
    out = tmp_path / "flat.jpg"
    export_file(encode_image(small, "JPEG", quality=90), out)
    assert Image.open(out).size == small.size


def _ring():
    image = Image.new("RGBA", (200, 120), "white")
    d = ImageDraw.Draw(image)
    d.rectangle((20, 20, 179, 99), fill="black"); d.rectangle((60, 40, 139, 79), fill="white")
    return image


def test_trace_contours_export_svg_and_dxf_files(tmp_path):
    import ezdxf
    doc, info = trace_image(_ring(), threshold=128, min_area=2, simplify=.2, dpi=96)
    assert info["contours"] == 2
    svg = tmp_path / "ring.svg"; doc.export_svg(svg)
    tree = ET.parse(svg)
    paths = [e for e in tree.iter() if e.tag.endswith("path")]
    assert paths and all(p.get("d") for p in paths)
    assert tree.getroot().get("width", "").endswith("mm")
    dxf = tmp_path / "ring.dxf"; doc.export_dxf(dxf)
    entities = list(ezdxf.readfile(dxf).modelspace())
    assert len(entities) >= 2
    xs = [v[0] for e in entities if e.dxftype() == "LWPOLYLINE" for v in e.get_points("xy")]
    assert xs and max(xs) - min(xs) == pytest.approx(160 * 25.4 / 96, rel=0.03)


def test_edge_and_colour_trace_modes_and_simplify():
    doc, info = trace_image(_ring(), mode="edges", threshold=100, min_area=2)
    assert info["contours"] >= 2
    image = Image.new("RGB", (120, 80), "white")
    d = ImageDraw.Draw(image); d.rectangle((10, 10, 50, 70), fill="red"); d.ellipse((65, 10, 110, 70), fill="blue")
    doc, info = trace_image(image, mode="color", colors=3, min_area=10, remove_background=True)
    fills = sorted(o.fill for o in doc.objects)
    assert fills == ["#0000ff", "#ff0000"]
    before = sum(len(o.nodes) for o in doc.objects if hasattr(o, "nodes"))
    for o in doc.objects:
        simplify_object(o, tolerance=2)
    after = sum(len(o.nodes) for o in doc.objects if hasattr(o, "nodes"))
    assert after <= before
