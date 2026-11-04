"""Magic Eraser, Magic Wand and Paint Bucket colour regions, judged on scenes with ground truth."""
import numpy as np
import pytest
from PIL import Image, ImageDraw

import image_scenes as S
from eng_viewer.core import image_tools as T


@pytest.fixture(scope="module")
def scenes():
    return {name: make() for name, make in S.SCENES.items()}


def test_light_clothing_survives_a_click_on_a_beige_backdrop(scenes):
    """The reported case: a figure in light clothing on a beige/grey backdrop."""
    image, truth = scenes["person_on_beige"]
    lost, left, iou = S.score(T.magic_erase(image, (20, 20)), truth)
    # 2.3.0 lost 47 % of the figure at its default tolerance (best IoU over all tolerances: 74 %).
    assert lost < 1 and left < 2 and iou > 97


@pytest.mark.parametrize("name,max_lost", [("white_object_on_beige", 2), ("light_on_light", 2), ("product_on_white", 2),
                                            ("reflective_metal", 2), ("mechanical_part", 2)])
def test_subjects_are_kept_across_the_useful_tolerance_range(scenes, name, max_lost):
    image, truth = scenes[name]
    for tolerance in (12, 16, 25, 40):
        lost, _left, _iou = S.score(T.magic_erase(image, (20, 20), tolerance), truth)
        assert lost < max_lost, (name, tolerance, lost)


def test_tolerance_is_monotonic_and_predictable(scenes):
    image, _truth = scenes["person_on_beige"]
    for edges in (True, False):
        areas = [int(np.count_nonzero(np.asarray(T.magic_erase(image, (20, 20), t, stop_at_edges=edges)) < 128))
                 for t in (0, 3, 8, 16, 30, 60, 100)]
        assert areas == sorted(areas), (edges, areas)
    # Low tolerance takes only the clicked shade; very high tolerance with edges off takes nearly everything.
    assert areas[0] < image.width * image.height * 0.05 and areas[-1] > image.width * image.height * 0.95


def test_contiguous_keeps_separated_regions_noncontiguous_takes_all():
    image, _truth = S.separated_regions()
    contiguous = np.asarray(T.magic_erase(image, (20, 20), contiguous=True))
    everywhere = np.asarray(T.magic_erase(image, (20, 20), contiguous=False))
    assert contiguous[150, 20] == 0 and contiguous[150, 380] == 255 and contiguous[150, 200] == 255
    assert everywhere[150, 20] == 0 and everywhere[150, 380] == 0 and everywhere[150, 200] == 255


def test_antialiased_edges_are_partial_and_interior_untouched():
    big = Image.new("RGBA", (400, 400), (230, 225, 215, 255))
    ImageDraw.Draw(big).ellipse((80, 80, 320, 320), fill=(40, 90, 160, 255))
    image = big.resize((100, 100), Image.Resampling.BOX)  # anti-aliased disc
    mask = np.asarray(T.magic_erase(image, (2, 2)))
    assert mask[50, 50] == 255 and mask[2, 2] == 0
    edge = mask[(mask > 0) & (mask < 255)]
    assert edge.size > 20  # a soft band, not a stair-stepped cut
    hard = np.asarray(T.magic_erase(image, (2, 2), antialias=False))
    assert set(np.unique(hard)) <= {0, 255}


def test_edges_can_be_turned_off_and_hidden_pixels_still_join():
    image, truth = S.person_on_beige()
    lost_with, *_ = S.score(T.magic_erase(image, (20, 20), 30), truth)
    lost_without, *_ = S.score(T.magic_erase(image, (20, 20), 30, stop_at_edges=False), truth)
    assert lost_with < 2 < lost_without
    # A backdrop split by earlier erasing is still removed in one click.
    split = Image.new("RGBA", (60, 20), (200, 200, 200, 255))
    mask = Image.new("L", (60, 20), 255)
    ImageDraw.Draw(mask).rectangle((28, 0, 31, 19), fill=0)
    result = np.asarray(T.magic_erase(split, (5, 5), mask=mask))
    assert result[10, 55] == 0


def test_erasing_is_restorable_and_never_changes_pixels(scenes):
    image, _truth = scenes["white_object_on_beige"]
    before = image.tobytes()
    mask = T.magic_erase(image, (20, 20))
    assert image.tobytes() == before
    restored = T.paint_mask(mask, [(20, 20), (200, 20)], radius=30, hardness=1, restore=True)
    assert np.asarray(restored)[20, 100] == 255 and np.asarray(mask)[20, 100] == 0


def test_transparent_click_and_wand_transparency_rules():
    image = Image.new("RGBA", (4, 1))
    image.putdata([(100, 100, 100, 255), (101, 99, 100, 255), (200, 0, 0, 0), (0, 200, 0, 0)])
    assert np.count_nonzero(np.asarray(T.color_selection(image, (0, 0), 10, contiguous=False))) == 2
    assert np.count_nonzero(np.asarray(T.color_selection(image, (3, 0), 10, contiguous=False))) == 2
    assert T.magic_erase(image, (2, 0)).tobytes() == Image.new("L", (4, 1), 255).tobytes()


def test_bucket_fills_up_to_antialiased_outline_on_transparent_layer():
    big = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(big).ellipse((40, 40, 360, 360), outline=(0, 0, 0, 255), width=12)
    layer = big.resize((100, 100), Image.Resampling.BOX)
    filled, coverage = T.bucket_fill(layer, (50, 50), "#ffcc00")
    assert filled.getpixel((50, 50)) == (255, 204, 0, 255) and filled.getpixel((2, 2))[3] == 0
    # Soft outline pixels are partly filled beneath their own anti-aliasing: no transparent gap.
    ring = np.asarray(coverage)[(np.asarray(layer)[..., 3] > 0) & (np.asarray(layer)[..., 3] < 255)]
    assert ring.max() > 0


@pytest.mark.slow
def test_large_photo_keeps_quality_in_bounded_time():
    """Edge walls must hold at camera resolutions too (junctions and diagonal steps leaked at 2-5x)."""
    import time
    for size in ((1200, 1600), (3000, 4000)):  # 2 MP and 12 MP
        image, truth = S.person_on_beige(size)
        start = time.perf_counter()
        lost, left, iou = S.score(T.magic_erase(image, (20, 20)), truth)
        assert time.perf_counter() - start < 6
        assert lost < 1 and iou > 98, (size, lost, left, iou)
