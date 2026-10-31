"""Synthetic image-editor tests: no private drawings or external image fixtures."""
import math

import numpy as np
import pytest
from PIL import Image, ImageDraw

from eng_viewer.core.image_tools import (
    background_erase, brush_mask, magic_erase, remove_background, clone_stamp, color_selection, combine_selection,
    erase_pixels, heal, paint_mask, perspective, polygon_selection, quick_selection,
    rectangle_selection, refine_selection,
)


def _count(mask):
    return int(np.count_nonzero(np.asarray(mask)))


def test_rectangle_exclusive_edges_and_reverse_drag():
    mask = rectangle_selection((20, 20), (9, 12, 3, 4))
    assert mask.getbbox() == (3, 4, 9, 12)
    assert _count(mask) == 6 * 8
    assert rectangle_selection((20, 20), (-10, -10, 4, 4)).getbbox() == (0, 0, 4, 4)
    assert rectangle_selection((20, 20), (4, 4, 4, 4)).getbbox() is None


def test_ellipse_and_polygon_lasso():
    ellipse = rectangle_selection((30, 30), (5, 5, 25, 25), ellipse=True)
    assert ellipse.getpixel((5, 5)) == 0
    assert ellipse.getpixel((15, 15)) == 255
    triangle = polygon_selection((30, 30), [(2, 2), (25, 2), (2, 25)])
    assert triangle.getpixel((5, 5)) == 255
    assert triangle.getpixel((20, 20)) == 0
    assert polygon_selection((30, 30), [(5, 5)]).getbbox() is None


def test_selection_boolean_operations_keep_soft_coverage():
    a = Image.new("L", (2, 1)); a.putdata([128, 255])
    b = Image.new("L", (2, 1)); b.putdata([64, 0])
    assert list(combine_selection(a, b, "add").get_flattened_data()) == [128, 255]
    assert list(combine_selection(a, b, "subtract").get_flattened_data()) == [64, 255]
    assert list(combine_selection(a, b, "intersect").get_flattened_data()) == [64, 0]
    assert combine_selection(None, b, "subtract").getbbox() is None
    with pytest.raises(ValueError):
        combine_selection(a, b, "bogus")
    with pytest.raises(ValueError):
        combine_selection(a, Image.new("L", (4, 4)), "add")


def test_magic_wand_separates_disconnected_regions():
    image = Image.new("RGBA", (30, 15), "blue")
    painter = ImageDraw.Draw(image)
    painter.rectangle((2, 2, 9, 10), fill="red")
    painter.rectangle((20, 2, 27, 10), fill="red")
    connected = color_selection(image, (5, 5), tolerance=0)
    colors = color_selection(image, (5, 5), tolerance=0, contiguous=False)
    assert connected.getpixel((23, 5)) == 0
    assert colors.getpixel((23, 5)) == 255
    assert _count(colors) == 2 * _count(connected)
    assert connected.getpixel((15, 5)) == 0


def test_color_tolerance_and_hidden_transparent_color():
    image = Image.new("RGBA", (4, 1))
    image.putdata([(100, 100, 100, 255), (110, 95, 105, 255), (200, 0, 0, 0), (0, 200, 0, 0)])
    assert _count(color_selection(image, (0, 0), 10, contiguous=False)) == 2
    transparent = color_selection(image, (2, 0), 0, contiguous=False)
    assert list(transparent.get_flattened_data()) == [0, 0, 255, 255]


def test_brush_footprint_continuity_hardness_and_opacity():
    soft = brush_mask((80, 80), [(30, 40), (50, 40)], 12, hardness=0.3, opacity=0.5)
    assert 126 <= soft.getpixel((40, 40)) <= 128
    assert 0 < soft.getpixel((40, 49)) < soft.getpixel((40, 40))
    assert soft.getpixel((40, 55)) == 0
    assert soft.getpixel((30, 40)) == soft.getpixel((40, 40))
    hard = brush_mask((80, 80), [(30, 40), (50, 40)], 12, hardness=1)
    assert hard.getpixel((40, 49)) == 255
    assert brush_mask((80, 80), [], 12).getbbox() is None


def test_mask_erase_and_restore_preserve_original():
    original = Image.new("L", (30, 30), 255)
    erased = paint_mask(original, [(15, 15)], 6, hardness=1)
    assert original.getpixel((15, 15)) == 255
    assert erased.getpixel((15, 15)) == 0
    restored = paint_mask(erased, [(15, 15)], 6, hardness=1, restore=True)
    assert restored.getpixel((15, 15)) == 255
    partial = paint_mask(original, [(15, 15)], 6, hardness=1, opacity=0.5)
    assert 126 <= partial.getpixel((15, 15)) <= 128


def test_erase_multiplies_alpha_without_changing_rgb():
    source = Image.new("RGBA", (10, 10), (20, 100, 220, 128))
    erased = erase_pixels(source, Image.new("L", source.size, 128))
    assert erased.getpixel((5, 5)) == (20, 100, 220, 63)
    assert source.getpixel((5, 5))[-1] == 128


def test_background_eraser_fixed_sample_protects_different_foreground():
    source = Image.new("RGBA", (60, 30), "white")
    ImageDraw.Draw(source).rectangle((25, 0, 40, 29), fill="red")
    mask = background_erase(source, [(10, 15), (50, 15)], 8, tolerance=5, hardness=1)
    assert mask.getpixel((10, 15)) == 0
    assert mask.getpixel((30, 15)) == 255
    assert mask.getpixel((50, 15)) == 0


def test_quick_selection_expands_to_object_edges_and_subtracts():
    source = Image.new("RGBA", (120, 100), (235, 235, 235, 255))
    ImageDraw.Draw(source).rectangle((30, 20, 89, 79), fill=(20, 75, 145, 255))
    selection = quick_selection(source, [(50, 40), (60, 50)], radius=5)
    assert selection.getpixel((31, 21)) == 255
    assert selection.getpixel((88, 78)) == 255
    assert selection.getpixel((10, 10)) == 0
    assert 3500 <= _count(selection) <= 3700
    reduced = quick_selection(source, [(50, 40)], radius=5, existing=selection, subtract=True)
    assert reduced.getpixel((50, 40)) == 0


def test_quick_selection_preserves_dimensions_and_transparency():
    source = Image.new("RGBA", (1800, 200), (0, 0, 0, 0))
    ImageDraw.Draw(source).rectangle((100, 50, 400, 150), fill="blue")
    selection = quick_selection(source, [(200, 100)], radius=10)
    assert selection.size == source.size
    assert selection.getpixel((200, 100)) == 255
    assert selection.getpixel((1700, 100)) == 0


def test_refine_selection_grow_shrink_feather_and_smooth():
    mask = rectangle_selection((40, 40), (10, 10, 30, 30))
    assert _count(refine_selection(mask, grow=3)) > _count(mask)
    assert _count(refine_selection(mask, grow=-3)) < _count(mask)
    feather = refine_selection(mask, feather=2)
    assert 0 < feather.getpixel((10, 10)) < 255
    assert feather.getpixel((20, 20)) >= 250
    noisy = mask.copy(); noisy.putpixel((2, 2), 255)
    assert refine_selection(noisy, smooth=1).getpixel((2, 2)) == 0


def test_clone_samples_original_with_fixed_offset_and_alpha():
    source = Image.new("RGBA", (80, 30), "blue")
    ImageDraw.Draw(source).rectangle((5, 5, 20, 25), fill=(240, 20, 10, 128))
    clone = clone_stamp(source, [(50, 15), (55, 15)], source=(10, 15), radius=5, hardness=1)
    # Round-trip premultiplication can introduce one level of channel quantization.
    assert abs(clone.getpixel((50, 15))[0] - 240) <= 1
    assert clone.getpixel((50, 15))[3] == 128
    assert clone.getpixel((1, 1)) == source.getpixel((1, 1))
    assert source.getpixel((50, 15)) == (0, 0, 255, 255)


def test_clone_outside_source_does_not_erase_destination():
    source = Image.new("RGBA", (40, 40), "red")
    clone = clone_stamp(source, [(20, 20), (35, 20)], source=(35, 20), radius=3, hardness=1)
    assert clone.getpixel((35, 20)) == source.getpixel((35, 20))


def test_healing_removes_synthetic_spot_preserving_alpha_and_unselected_pixels():
    source = Image.new("RGBA", (80, 80), (175, 175, 175, 190))
    ImageDraw.Draw(source).ellipse((35, 35, 45, 45), fill=(0, 0, 0, 190))
    mask = rectangle_selection(source.size, (33, 33, 48, 48), ellipse=True)
    healed = heal(source, mask)
    assert healed.getpixel((40, 40))[0] > 150
    assert healed.getpixel((40, 40))[3] == 190
    assert healed.getpixel((4, 4)) == source.getpixel((4, 4))
    assert source.getpixel((40, 40))[0] == 0
    with pytest.raises(ValueError, match="surrounding"):
        heal(source, Image.new("L", source.size, 255))


def test_perspective_identity_and_degenerate_rejection():
    source = Image.new("RGBA", (40, 30), (100, 50, 200, 255))
    ImageDraw.Draw(source).rectangle((10, 10, 20, 20), fill="red")
    corrected = perspective(source, [(0, 0), (39, 0), (39, 29), (0, 29)])
    assert corrected.size == source.size
    assert corrected.tobytes() == source.tobytes()
    with pytest.raises(ValueError):
        perspective(source, [(5, 5)] * 4)


@pytest.mark.parametrize("call", [
    lambda: brush_mask((20, 20), [(1, 1)], math.nan),
    lambda: brush_mask((20, 20), [(math.inf, 1)], 3),
    lambda: brush_mask((20, 20), [(1, 1)], 3, opacity=2),
    lambda: rectangle_selection((20, 20), (0, 0, math.inf, 3)),
    lambda: color_selection(Image.new("RGB", (2, 2)), (0, 0), tolerance=-1),
    lambda: refine_selection(Image.new("L", (2, 2)), grow=math.nan),
])
def test_invalid_geometry_is_rejected(call):
    with pytest.raises(ValueError):
        call()


def test_magic_eraser_removes_the_clicked_background_in_one_click():
    source = Image.new("RGBA", (80, 60), (250, 250, 250, 255))
    ImageDraw.Draw(source).ellipse((20, 10, 59, 49), fill=(30, 90, 200, 255))
    ImageDraw.Draw(source).rectangle((0, 0, 3, 3), fill=(245, 248, 246, 255))  # slightly off-white noise
    mask = magic_erase(source, (2, 30), tolerance=16)
    assert mask.getpixel((0, 0)) == 0 and mask.getpixel((79, 59)) == 0
    assert mask.getpixel((40, 30)) == 255


def test_magic_eraser_contiguous_keeps_disconnected_matches():
    source = Image.new("RGBA", (60, 30), "white")
    draw = ImageDraw.Draw(source)
    draw.rectangle((20, 0, 24, 29), fill="black")
    mask = magic_erase(source, (5, 5), tolerance=10, antialias=False)
    assert mask.getpixel((10, 10)) == 0 and mask.getpixel((40, 10)) == 255
    everywhere = magic_erase(source, (5, 5), tolerance=10, contiguous=False, antialias=False)
    assert everywhere.getpixel((40, 10)) == 0 and everywhere.getpixel((22, 10)) == 255


def test_magic_eraser_respects_existing_mask_and_opacity():
    source = Image.new("RGBA", (20, 20), "white")
    existing = Image.new("L", source.size, 255)
    existing.putpixel((0, 0), 0)
    assert magic_erase(source, (0, 0), mask=existing) == existing  # clicking hidden pixels changes nothing
    half = magic_erase(source, (5, 5), opacity=0.5, antialias=False)
    assert 126 <= half.getpixel((10, 10)) <= 128


def test_automatic_background_removal_keeps_the_subject():
    source = Image.new("RGB", (300, 200), (236, 238, 240))
    ImageDraw.Draw(source).ellipse((90, 40, 210, 160), fill=(180, 40, 30))
    mask = remove_background(source)
    assert mask.size == source.size
    assert mask.getpixel((150, 100)) > 200
    assert mask.getpixel((10, 10)) < 30 and mask.getpixel((290, 190)) < 30
