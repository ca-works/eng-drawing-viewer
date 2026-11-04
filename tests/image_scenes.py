"""Deterministic synthetic photographs with ground-truth subject masks.

Each scene returns ``(image, truth)``: an RGBA photograph-like image (supersampled edges,
lighting falloff, sensor noise and JPEG compression where a camera would add them) and an
``L`` mask of the subject's true coverage. They reproduce the cases the Magic Eraser and
automatic background removal are judged on without shipping anyone's photographs.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SS = 4  # supersampling factor for anti-aliased edges


def _finish(rgb, truth, size, noise=2.2, jpeg=88, seed=1):
    """Downsample, add sensor noise and JPEG compression like a camera would."""
    rgb = rgb.resize(size, Image.Resampling.BOX)
    truth = truth.resize(size, Image.Resampling.BOX)
    pixels = np.asarray(rgb).astype(np.float32)
    if noise:
        pixels += np.random.default_rng(seed).normal(0, noise, pixels.shape)
    rgb = Image.fromarray(np.clip(np.round(pixels), 0, 255).astype(np.uint8))
    if jpeg:
        buffer = io.BytesIO()
        rgb.save(buffer, "JPEG", quality=jpeg)
        buffer.seek(0)
        rgb = Image.open(buffer).convert("RGB")
    return rgb.convert("RGBA"), truth


def _backdrop(size, color, falloff=0.16, floor=None):
    """Plain studio backdrop with lighting falloff towards the corners (and an optional floor)."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h * 0.42) / (h / 2)) ** 2)
    shade = 1 - falloff * np.clip(r, 0, 1.6) ** 2 / 2.56
    pixels = np.asarray(color, np.float32)[None, None, :] * shade[..., None]
    if floor:
        start, darker = floor
        t = np.clip((yy - h * start) / (h * (1 - start)), 0, 1)[..., None]
        pixels = pixels * (1 - darker * t)
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def _shaded(draw_fn, size, base, shadow, bands):
    """A garment-like fill: base colour with soft fold shadows (vertical bands)."""
    w, h = size
    layer = Image.new("RGB", size, base)
    shade = Image.new("L", size, 0)
    d = ImageDraw.Draw(shade)
    for x0, x1 in bands:
        d.rectangle((x0, 0, x1, h), fill=255)
    shade = shade.filter(ImageFilter.GaussianBlur(10 * SS))
    layer = Image.composite(Image.new("RGB", size, shadow), layer, shade)
    mask = Image.new("L", size, 0)
    draw_fn(ImageDraw.Draw(mask))
    return layer, mask


def person_on_beige(size=(600, 800)):
    """A figure in a white shirt with shaded folds, a cream scarf and light trousers on a beige backdrop."""
    W, H = size[0] * SS, size[1] * SS
    bg = _backdrop((W, H), (222, 212, 196), floor=(0.8, 0.12))
    truth = Image.new("L", (W, H), 0)

    def put(fill, mask):
        bg.paste(fill, (0, 0), mask)
        truth.paste(255, (0, 0), mask)

    def shirt(d):
        d.polygon([(W * .30, H * .30), (W * .70, H * .30), (W * .80, H * .62), (W * .68, H * .64),
                   (W * .66, H * .58), (W * .34, H * .58), (W * .32, H * .64), (W * .20, H * .62)], fill=255)
    put(*_shaded(shirt, (W, H), (240, 240, 238), (206, 204, 199),
                 [(W * .38, W * .41), (W * .55, W * .58), (W * .70, W * .74)]))

    def trousers(d):
        d.polygon([(W * .34, H * .58), (W * .66, H * .58), (W * .64, H * .98), (W * .53, H * .98),
                   (W * .50, H * .70), (W * .47, H * .98), (W * .36, H * .98)], fill=255)
    put(*_shaded(trousers, (W, H), (200, 199, 196), (176, 175, 172), [(W * .49, W * .51)]))

    def scarf(d):
        d.polygon([(W * .40, H * .28), (W * .60, H * .28), (W * .58, H * .36), (W * .52, H * .37),
                   (W * .54, H * .50), (W * .49, H * .50), (W * .47, H * .37), (W * .42, H * .36)], fill=255)
    put(*_shaded(scarf, (W, H), (236, 226, 204), (222, 211, 190), []))

    def neck(d):
        d.rectangle((W * .46, H * .22, W * .54, H * .29), fill=255)
    put(*_shaded(neck, (W, H), (226, 187, 160), (205, 166, 140), []))

    def head(d):
        d.ellipse((W * .40, H * .07, W * .60, H * .25), fill=255)
    put(*_shaded(head, (W, H), (228, 190, 163), (208, 170, 145), [(W * .40, W * .43)]))

    def hair(d):
        d.chord((W * .39, H * .05, W * .61, H * .21), 180, 360, fill=255)
    put(*_shaded(hair, (W, H), (62, 44, 34), (40, 28, 22), []))
    return _finish(bg, truth, size)


def white_object_on_beige(size=(640, 480)):
    """A white box and mug on beige paper: the light subject must survive a backdrop click."""
    W, H = size[0] * SS, size[1] * SS
    bg = _backdrop((W, H), (226, 216, 198))
    truth = Image.new("L", (W, H), 0)
    d, t = ImageDraw.Draw(bg), ImageDraw.Draw(truth)
    box = (W * .18, H * .30, W * .48, H * .78)
    d.rectangle(box, fill=(246, 246, 244))
    d.rectangle((box[2] - W * .05, box[1], box[2], box[3]), fill=(228, 228, 226))  # shaded side
    t.rectangle(box, fill=255)
    mug = (W * .58, H * .38, W * .80, H * .78)
    d.rounded_rectangle(mug, radius=W * .02, fill=(244, 243, 240))
    d.ellipse((mug[2] - W * .03, H * .48, mug[2] + W * .07, H * .64), outline=(244, 243, 240), width=int(W * .018))
    t.rounded_rectangle(mug, radius=W * .02, fill=255)
    t.ellipse((mug[2] - W * .03, H * .48, mug[2] + W * .07, H * .64), outline=255, width=int(W * .018))
    return _finish(bg, truth, size)


def reflective_metal(size=(640, 480)):
    """A polished cylinder whose highlights and reflections match the light grey backdrop."""
    W, H = size[0] * SS, size[1] * SS
    bg = _backdrop((W, H), (214, 214, 216), falloff=0.10)
    truth = Image.new("L", (W, H), 0)
    x0, x1, y0, y1 = W * .30, W * .70, H * .18, H * .84
    xs = np.linspace(0, 1, int(x1 - x0))
    profile = (150 + 75 * np.exp(-((xs - .28) / .07) ** 2) + 40 * np.exp(-((xs - .75) / .12) ** 2)
               - 70 * np.exp(-((xs - .55) / .10) ** 2) - 40 * xs ** 6)
    band = np.repeat(np.clip(profile, 0, 255)[None, :], int(y1 - y0), axis=0)
    metal = Image.fromarray(np.stack([band, band, band * 1.01], -1).clip(0, 255).astype(np.uint8))
    shape = Image.new("L", metal.size, 0)
    ImageDraw.Draw(shape).rounded_rectangle((0, 0, metal.width - 1, metal.height - 1), radius=W * .03, fill=255)
    bg.paste(metal, (int(x0), int(y0)), shape)
    truth.paste(255, (int(x0), int(y0)), shape)
    return _finish(bg, truth, size, noise=1.6)


def separated_regions(size=(400, 300)):
    """Two identical pale panels split by a dark divider: contiguous erasing must keep the far one."""
    image = Image.new("RGBA", size, (232, 226, 214, 255))
    d = ImageDraw.Draw(image)
    d.rectangle((size[0] // 2 - 6, 0, size[0] // 2 + 6, size[1]), fill=(40, 40, 46, 255))
    truth = Image.new("L", size, 0)
    ImageDraw.Draw(truth).rectangle((size[0] // 2 - 6, 0, size[0] // 2 + 6, size[1]), fill=255)
    return image, truth


def product_on_white(size=(640, 480)):
    """A coloured product with a soft contact shadow on a white sweep."""
    W, H = size[0] * SS, size[1] * SS
    bg = _backdrop((W, H), (250, 250, 250), falloff=0.06)
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).ellipse((W * .28, H * .74, W * .72, H * .86), fill=120)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14 * SS))
    bg = Image.composite(Image.new("RGB", (W, H), (150, 150, 150)), bg, shadow)
    truth = Image.new("L", (W, H), 0)
    d, t = ImageDraw.Draw(bg), ImageDraw.Draw(truth)
    body = (W * .36, H * .16, W * .64, H * .80)
    d.rounded_rectangle(body, radius=W * .05, fill=(34, 110, 196))
    d.rounded_rectangle((W * .40, H * .08, W * .60, H * .18), radius=W * .02, fill=(236, 236, 236))  # white cap
    t.rounded_rectangle(body, radius=W * .05, fill=255)
    t.rounded_rectangle((W * .40, H * .08, W * .60, H * .18), radius=W * .02, fill=255)
    return _finish(bg, truth, size)


def mechanical_part(size=(640, 480)):
    """A grey bracket with bolt holes (backdrop visible through them) and thin ribs."""
    W, H = size[0] * SS, size[1] * SS
    bg = _backdrop((W, H), (232, 232, 228), falloff=0.08)
    part = Image.new("L", (W, H), 0)
    p = ImageDraw.Draw(part)
    p.rounded_rectangle((W * .15, H * .30, W * .85, H * .62), radius=W * .03, fill=255)
    p.rectangle((W * .45, H * .12, W * .55, H * .30), fill=255)
    for i in range(5):
        x = W * (.22 + .14 * i)
        p.line((x, H * .62, x + W * .03, H * .86), fill=255, width=int(3 * SS))  # thin ribs, 3 px wide
    for cx in (.25, .5, .75):
        p.ellipse((W * cx - W * .04, H * .40, W * cx + W * .04, H * .40 + W * .08), fill=0)
    metal = Image.new("RGB", (W, H), (128, 132, 138))
    edge = part.filter(ImageFilter.GaussianBlur(6 * SS))
    metal = Image.composite(metal, Image.new("RGB", (W, H), (182, 186, 190)), edge)
    bg.paste(metal, (0, 0), part)
    return _finish(bg, part, size)


def light_on_light(size=(640, 480)):
    """An off-white ceramic tile on a slightly warmer white backdrop: a low-contrast boundary."""
    W, H = size[0] * SS, size[1] * SS
    bg = _backdrop((W, H), (240, 236, 228), falloff=0.08)
    truth = Image.new("L", (W, H), 0)
    tile = (W * .25, H * .22, W * .75, H * .78)
    ImageDraw.Draw(bg).rounded_rectangle(tile, radius=W * .02, fill=(250, 250, 252))
    ImageDraw.Draw(bg).rounded_rectangle(tile, radius=W * .02, outline=(214, 212, 210), width=SS * 2)
    ImageDraw.Draw(truth).rounded_rectangle(tile, radius=W * .02, fill=255)
    return _finish(bg, truth, size, noise=1.5)


SCENES = {
    "person_on_beige": person_on_beige,
    "white_object_on_beige": white_object_on_beige,
    "reflective_metal": reflective_metal,
    "product_on_white": product_on_white,
    "mechanical_part": mechanical_part,
    "light_on_light": light_on_light,
}


def score(visible, truth):
    """(subject lost %, backdrop left %, IoU %) for a visibility mask against the truth."""
    v = np.asarray(visible, np.float32) / 255
    t = np.asarray(truth, np.float32) / 255
    lost = float(((1 - v) * t).sum() / max(1, t.sum()) * 100)
    left = float((v * (1 - t)).sum() / max(1, (1 - t).sum()) * 100)
    iou = float(np.minimum(v, t).sum() / max(1, np.maximum(v, t).sum()) * 100)
    return lost, left, iou
