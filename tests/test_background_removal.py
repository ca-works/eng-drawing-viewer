"""Automatic background removal (model-free) and edge refinement, against ground-truth masks."""
import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

import image_scenes as S
from eng_viewer.core import image_tools as T


def edge_error(mask, truth):
    """Mean alpha error (%) in a 6-pixel band around the true boundary."""
    t = np.asarray(truth, np.float32) / 255
    m = np.asarray(mask, np.float32) / 255
    inside = (t > .5).astype(np.uint8)
    band = (cv2.dilate(inside, np.ones((7, 7), np.uint8)) > 0) & (cv2.erode(inside, np.ones((7, 7), np.uint8)) == 0)
    return float(np.abs(m - t)[band].mean() * 100)


# (scene, minimum IoU %, maximum edge error %). 2.3.0 scored person 55 %, product 83 %
# (its white cap lost), light-on-light edge error 38 %.
@pytest.mark.parametrize("name,min_iou,max_edge", [
    ("person_on_beige", 97, 12), ("white_object_on_beige", 98.5, 6), ("reflective_metal", 99, 3),
    ("mechanical_part", 98, 6), ("light_on_light", 98.5, 9), ("product_on_white", 88, 15)])
def test_scenes(name, min_iou, max_edge):
    image, truth = S.SCENES[name]()
    mask = T.remove_background(image)
    lost, left, iou = S.score(mask, truth)
    assert iou >= min_iou and edge_error(mask, truth) <= max_edge, (iou, lost, left)


def test_light_cap_on_white_sweep_is_kept():
    image, truth = S.product_on_white()
    lost, _left, _iou = S.score(T.remove_background(image), truth)
    assert lost < 2


@pytest.mark.slow
def test_edges_stay_sharp_on_large_images():
    image, truth = S.white_object_on_beige((1920, 1440))
    mask = T.remove_background(image)
    assert edge_error(mask, truth) < 6
    unrefined = T.remove_background(image, refine=False)
    assert edge_error(mask, truth) < edge_error(unrefined, truth) * 0.6


def test_refine_mask_sharpens_an_upsampled_model_mask():
    image, truth = S.mechanical_part((1280, 960))
    coarse = truth.resize((160, 120), Image.Resampling.BILINEAR).resize(image.size, Image.Resampling.LANCZOS)
    refined = T.refine_mask(image, coarse, band=10)
    assert edge_error(refined, truth) < edge_error(coarse, truth) * 0.75  # measured: 31.7 % -> 19.8 %
    assert S.score(refined, truth)[2] > S.score(coarse, truth)[2]


def test_decontaminate_removes_backdrop_tint_from_edges():
    big = Image.new("RGB", (400, 400), (255, 140, 0))
    ImageDraw.Draw(big).ellipse((60, 60, 340, 340), fill=(20, 60, 200))
    image = big.resize((100, 100), Image.Resampling.BOX).convert("RGBA")
    truth = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(truth).ellipse((60, 60, 340, 340), fill=255)
    mask = truth.resize((100, 100), Image.Resampling.BOX)
    clean = T.decontaminate(image, mask)
    m = np.asarray(mask)
    edge = (m > 40) & (m < 215)
    before = np.asarray(image)[..., 0][edge].mean()
    after = np.asarray(clean)[..., 0][edge].mean()
    assert after < before - 40  # the orange cast is gone from the soft edge
    assert np.array_equal(np.asarray(clean)[..., 3], m)
    assert image.getpixel((50, 50)) == clean.getpixel((50, 50))


def test_no_subject_raises():
    with pytest.raises(ValueError):
        T.remove_background(Image.new("RGBA", (200, 150), (240, 240, 240, 255)))
