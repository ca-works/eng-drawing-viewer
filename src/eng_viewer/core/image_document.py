"""Offline, Qt-independent layered image documents.

Pixel buffers are immutable by convention: after ``snapshot`` replace a layer's
image or mask instead of editing it in place. History then shares unchanged
buffers and drops old states to stay within its 256 MiB pixel budget.

ENGIMG is a ZIP containing manifest.json and embedded PNG assets. It never contains
executable objects or references to external image paths. Version 2 adds, for layers that
were moved, scaled or rotated, the layer's original pixels and the affine transform from
them (``sources/<i>.png``), so a reopened project can still be transformed without
resampling an already resampled copy. Projects without such layers are saved as version 1.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import io
import json
import math
import os
import tempfile
import uuid
import warnings
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

from .raster_limits import check_pixels

MAX_EDITOR_PIXELS = 80_000_000
MAX_PROJECT_PIXELS = 160_000_000
MAX_PROJECT_BYTES = 512 * 1024 * 1024
MAX_LAYERS = 64
BLEND_MODES = ("normal", "multiply", "screen", "overlay", "darken", "lighten", "difference", "soft-light")


def _dimensions(width, height):
    if (isinstance(width, bool) or isinstance(height, bool)
            or not isinstance(width, int) or not isinstance(height, int)):
        raise ValueError("Pixel dimensions must be whole numbers.")
    check_pixels(width, height)
    if width * height > MAX_EDITOR_PIXELS:
        raise ValueError("The editor supports at most 80 million pixels per layer/canvas.")


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid {name}.")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Invalid {name}.")
    return value


def _text(value, name, limit=1024):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"Invalid {name}.")
    return value


def _decode_image(path):
    """Validate dimensions before decoding; preserve normalized metadata.

    ``path`` may also be a binary file object (clipboard data), which is decoded with the
    same limits and normalisation as a file.
    """
    is_heif = (isinstance(path, (str, os.PathLike)) and Path(path).suffix.lower() in (".heic", ".heif", ".hif")) or (
        hasattr(path, "getbuffer") and bytes(path.getbuffer()[4:12]) in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1", b"ftypheif"))
    if is_heif:
        try:
            from pillow_heif import register_heif_opener
        except ImportError as exc:
            raise ValueError("HEIC/HEIF decoding is not installed in this build.") from exc
        register_heif_opener()
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as opened:
            _dimensions(*opened.size)
            info = dict(opened.info)
            fmt = opened.format or ""
            frame_count = getattr(opened, "n_frames", 1)
            oriented = ImageOps.exif_transpose(opened)
            exif = oriented.getexif()
            exif.pop(274, None)
            if oriented.mode in ("CMYK", "LAB") and info.get("icc_profile"):
                from PIL import ImageCms
                source_profile = ImageCms.ImageCmsProfile(io.BytesIO(info["icc_profile"]))
                target_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
                oriented = ImageCms.profileToProfile(oriented, source_profile, target_profile, outputMode="RGB")
                info["icc_profile"] = target_profile.tobytes()
            image = oriented.convert("RGBA")
            image.load()
    metadata = {"format": fmt, "frame_count": frame_count}
    for key in ("icc_profile",):
        if isinstance(info.get(key), bytes):
            metadata[key] = info[key]
    if exif:
        metadata["exif"] = exif.tobytes()
    dpi = info.get("dpi", (96, 96))
    if isinstance(dpi, (list, tuple)):
        dpi = dpi[0] if dpi else 96
    try:
        dpi = float(dpi)
    except (TypeError, ValueError):
        dpi = 96
    if not math.isfinite(dpi) or not 1 <= dpi <= 100000:
        dpi = 96
    return image, metadata, dpi


IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def affine(matrix):
    """Validate a 2 × 3 affine matrix ``(a, b, c, d, e, f)``: x' = a·x + b·y + c, y' = d·x + e·y + f."""
    if not isinstance(matrix, (list, tuple)) or len(matrix) != 6:
        raise ValueError("A transform needs six numbers.")
    values = tuple(float(_number(v, "transform", -1e7, 1e7)) for v in matrix)
    if abs(values[0] * values[4] - values[1] * values[3]) < 1e-9:
        raise ValueError("A transform cannot flatten a layer to nothing.")
    return values


def affine_multiply(first, then):
    """The transform that applies ``first`` and then ``then``."""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = then
    return (a2 * a1 + b2 * d1, a2 * b1 + b2 * e1, a2 * c1 + b2 * f1 + c2,
            d2 * a1 + e2 * d1, d2 * b1 + e2 * e1, d2 * c1 + e2 * f1 + f2)


def affine_point(matrix, x, y):
    a, b, c, d, e, f = matrix
    return a * x + b * y + c, d * x + e * y + f


def affine_invert(matrix):
    a, b, c, d, e, f = matrix
    det = a * e - b * d
    return (e / det, -b / det, (b * f - c * e) / det, -d / det, a / det, (c * d - a * f) / det)


def affine_translate(dx, dy):
    return (1.0, 0.0, float(dx), 0.0, 1.0, float(dy))


def affine_about(center, scale_x=1.0, scale_y=1.0, degrees=0.0, axis_degrees=0.0):
    """Scale along axes turned by ``axis_degrees``, then rotate by ``degrees``, about ``center``.

    Positive degrees turn clockwise on screen (image y points down)."""
    cx, cy = center
    t, r = math.radians(axis_degrees), math.radians(degrees)
    ct, st = math.cos(t), math.sin(t)
    to_axes = (ct, st, 0.0, -st, ct, 0.0)
    from_axes = (ct, -st, 0.0, st, ct, 0.0)
    m = affine_multiply(affine_translate(-cx, -cy), to_axes)
    m = affine_multiply(m, (float(scale_x), 0.0, 0.0, 0.0, float(scale_y), 0.0))
    m = affine_multiply(m, from_axes)
    m = affine_multiply(m, (math.cos(r), -math.sin(r), 0.0, math.sin(r), math.cos(r), 0.0))
    return affine_multiply(m, affine_translate(cx, cy))


def warp(image, matrix, size, fill=0):
    """Resample ``image`` through ``matrix`` (image pixels → output pixels) into ``size``.

    RGBA is resampled premultiplied, so transparent surroundings never darken edges. Strong
    reductions are first area-averaged, which keeps fine detail from aliasing.
    """
    import cv2
    import numpy as np

    _dimensions(*size)
    matrix = affine(matrix)
    sx = math.hypot(matrix[0], matrix[3])
    sy = math.hypot(matrix[1], matrix[4])
    rgba = image.mode == "RGBA"
    source = image.convert("RGBa") if rgba else image.convert("L")
    if min(sx, sy) < 0.75:
        fx, fy = min(1.0, sx * 1.5), min(1.0, sy * 1.5)
        reduced = (max(1, round(source.width * fx)), max(1, round(source.height * fy)))
        if reduced != source.size:
            fx, fy = reduced[0] / source.width, reduced[1] / source.height
            array = cv2.resize(np.asarray(source), reduced, interpolation=cv2.INTER_AREA)
            source = Image.frombuffer(source.mode, reduced, np.ascontiguousarray(array).tobytes(), "raw", source.mode, 0, 1)
            matrix = affine_multiply((1 / fx, 0.0, 0.0, 0.0, 1 / fy, 0.0), matrix)
    a, b, c, d, e, f = matrix
    out = cv2.warpAffine(np.asarray(source), np.array([[a, b, c], [d, e, f]], dtype=np.float64), tuple(size),
                         flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                         borderValue=(0, 0, 0, 0) if rgba else int(fill))
    result = Image.frombuffer(source.mode, tuple(size), np.ascontiguousarray(out).tobytes(), "raw", source.mode, 0, 1)
    return result.convert("RGBA") if rgba else result.copy()


def transformed_bounds(matrix, width, height):
    """Whole-pixel bounds (left, top, right, bottom) of a ``width`` × ``height`` image under ``matrix``."""
    corners = [affine_point(matrix, x, y) for x, y in ((0, 0), (width, 0), (width, height), (0, height))]
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    # A hair of tolerance keeps exact integer edges from growing an empty row or column.
    return (math.floor(min(xs) + 1e-6), math.floor(min(ys) + 1e-6),
            math.ceil(max(xs) - 1e-6), math.ceil(max(ys) - 1e-6))


@dataclass(frozen=True)
class LayerSource:
    """The original pixels of a transformed layer.

    ``matrix`` maps them to the layer's pixels; ``rendered`` is the layer image they
    produced. Assigning any other image to the layer (painting, healing, a fill) makes the
    source stale, so it is used only while ``layer.image is rendered``.
    """
    image: Image.Image
    matrix: tuple
    rendered: Image.Image


@dataclass
class ImageLayer:
    name: str
    image: Image.Image
    mask: Image.Image | None = None
    visible: bool = True
    opacity: float = 1.0
    blend: str = "normal"
    locked: bool = False
    x: int = 0
    y: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = "image"
    properties: dict = field(default_factory=dict)
    source: LayerSource | None = None

    def __post_init__(self):
        _dimensions(*self.image.size)
        _text(self.name, "layer name")
        _text(self.id, "layer ID", 128)
        _text(self.kind, "layer kind", 64)
        if not self.id:
            raise ValueError("A layer ID cannot be empty.")
        if type(self.visible) is not bool or type(self.locked) is not bool:
            raise ValueError("Layer visibility and lock state must be boolean.")
        if any(type(value) is not int or abs(value) > 1_000_000 for value in (self.x, self.y)):
            raise ValueError("Layer offsets must be bounded whole pixel coordinates.")
        _number(self.opacity, "layer opacity", 0, 1)
        if self.blend not in BLEND_MODES:
            raise ValueError("Unsupported layer blend mode.")
        if self.image.mode != "RGBA":
            self.image = self.image.convert("RGBA")
        if self.mask is not None:
            if self.mask.size != self.image.size:
                raise ValueError("A layer mask must match its image dimensions.")
            if self.mask.mode != "L":
                self.mask = self.mask.convert("L")
        if not isinstance(self.properties, dict):
            raise ValueError("Layer properties must be an object.")
        if self.source is not None:
            if not isinstance(self.source, LayerSource) or self.source.image.mode != "RGBA":
                raise ValueError("Invalid original layer pixels.")
            _dimensions(*self.source.image.size)
            affine(self.source.matrix)

    @property
    def original(self) -> LayerSource | None:
        """The original pixels behind this layer's current transform, if still valid."""
        source = self.source
        return source if source is not None and source.rendered is self.image else None

    @property
    def frame(self):
        """(centre x, centre y, width, height, degrees) of the layer's content on the canvas.

        For a transformed layer this is its original image's turned rectangle; otherwise the
        pixel array's own rectangle. Width or height is negative for a mirrored layer.
        """
        source = self.original
        if source is None:
            w, h = self.image.size
            return self.x + w / 2, self.y + h / 2, float(w), float(h), 0.0
        a, b, c, d, e, f = source.matrix
        w0, h0 = source.image.size
        cx, cy = affine_point(source.matrix, w0 / 2, h0 / 2)
        angle = math.degrees(math.atan2(d, a))
        width = w0 * math.hypot(a, d)
        height = h0 * math.hypot(b, e)
        if a * e - b * d < 0:
            height = -height
        return self.x + cx, self.y + cy, width, height, angle

    def rendered(self):
        image = self.image
        if self.mask is not None or self.opacity != 1:
            alpha = image.getchannel("A")
            if self.mask is not None:
                alpha = ImageChops.multiply(alpha, self.mask)
            if self.opacity != 1:
                opacity = max(0, min(1, self.opacity))
                alpha = alpha.point(lambda v: round(v * opacity))
            image = image.copy()
            image.putalpha(alpha)
        return image


@dataclass
class _State:
    width: int
    height: int
    layers: list[ImageLayer]
    active_index: int
    dpi: float
    metadata: dict
    revision: int
    label: str


@dataclass
class ImageDocument:
    width: int
    height: int
    layers: list[ImageLayer] | None = None
    dpi: float = 96
    source_path: str = ""
    metadata: dict = field(default_factory=dict)
    active_index: int = 0
    history_budget: int = 256 * 1024 * 1024
    revision: int = field(default=0, init=False)
    _next_revision: int = field(default=0, init=False, repr=False)
    _saved_revision: int = field(default=0, init=False, repr=False)
    _undo: list[_State] = field(default_factory=list, init=False, repr=False)
    _redo: list[_State] = field(default_factory=list, init=False, repr=False)
    _batch: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        _dimensions(self.width, self.height)
        _number(self.dpi, "DPI", 1, 100000)
        if self.layers is None:
            self.layers = [ImageLayer("Background", Image.new("RGBA", self.size))]
        if len(self.layers) > MAX_LAYERS:
            raise ValueError("Documents support at most 64 layers.")
        self.active_index = min(max(0, self.active_index), len(self.layers) - 1)

    @property
    def size(self):
        return self.width, self.height

    @property
    def active_layer(self):
        return self.layers[self.active_index] if self.layers else None

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    @property
    def dirty(self):
        return self.revision != self._saved_revision

    @property
    def history_bytes(self):
        buffers = {}
        for state in self._undo + self._redo:
            for layer in state.layers:
                for image in (layer.image, layer.mask, layer.source.image if layer.source else None):
                    if image is not None:
                        buffers[id(image)] = image.width * image.height * len(image.getbands())
        return sum(buffers.values())

    @classmethod
    def from_image(cls, image, name="Untitled"):
        _dimensions(*image.size)
        return cls(*image.size, [ImageLayer(name, image.convert("RGBA"))])

    @classmethod
    def from_file(cls, path):
        image, metadata, dpi = _decode_image(path)
        return cls(*image.size, [ImageLayer(Path(path).stem, image)], dpi=dpi,
                   source_path=str(Path(path).resolve()), metadata=metadata)

    def _state(self, label=""):
        return _State(self.width, self.height,
                      [replace(layer, properties=copy.deepcopy(layer.properties))
                       for layer in self.layers], self.active_index, self.dpi,
                      copy.deepcopy(self.metadata), self.revision, label)

    def _restore(self, state):
        self.width, self.height = state.width, state.height
        self.layers, self.active_index = state.layers, state.active_index
        self.dpi, self.metadata, self.revision = state.dpi, state.metadata, state.revision

    def _trim_history(self):
        while self._undo and (len(self._undo) > 50 or self.history_bytes > self.history_budget):
            self._undo.pop(0)
        while self._redo and self.history_bytes > self.history_budget:
            self._redo.pop(0)

    def snapshot(self, label):
        if self._batch:
            return  # inside batch(): one undo step for the whole command
        self._undo.append(self._state(str(label)))
        self._redo.clear()
        self._trim_history()

    @contextlib.contextmanager
    def batch(self, label):
        """Group several operations into one undo step."""
        self.snapshot(label)
        self._batch += 1
        try:
            yield self
        finally:
            self._batch -= 1
            self.touch()

    def touch(self):
        self._next_revision += 1
        self.revision = self._next_revision
        self._trim_history()

    def undo(self):
        if not self._undo:
            return False
        state = self._undo.pop()
        self._redo.append(self._state(state.label))
        self._restore(state)
        self._trim_history()
        return True

    def redo(self):
        if not self._redo:
            return False
        state = self._redo.pop()
        self._undo.append(self._state(state.label))
        self._restore(state)
        self._trim_history()
        return True

    def composite(self, start=0, stop=None):
        """Flattened visible layers ``start``–``stop`` (all by default) on a transparent canvas."""
        canvas = Image.new("RGBA", self.size)
        for layer in self.layers[start:stop]:
            if not layer.visible or layer.opacity <= 0:
                continue
            image = layer.rendered()
            # Work only on the overlap, avoiding a second full-canvas buffer per layer.
            left, top = max(0, layer.x), max(0, layer.y)
            right, bottom = min(self.width, layer.x + image.width), min(self.height, layer.y + image.height)
            if right <= left or bottom <= top:
                continue
            src = image.crop((left - layer.x, top - layer.y, right - layer.x, bottom - layer.y))
            if layer.blend != "normal":
                backdrop = canvas.crop((left, top, right, bottom))
                a, b = backdrop.convert("RGB"), src.convert("RGB")
                mixed = {"multiply": ImageChops.multiply, "screen": ImageChops.screen,
                         "overlay": ImageChops.overlay, "darken": ImageChops.darker,
                         "lighten": ImageChops.lighter, "difference": ImageChops.difference,
                         "soft-light": ImageChops.soft_light}[layer.blend](a, b)
                # A translucent backdrop must not tint source pixels in empty areas.
                mixed = Image.composite(mixed, b, backdrop.getchannel("A"))
                mixed.putalpha(src.getchannel("A"))
                src = mixed
            canvas.alpha_composite(src, (left, top))
        return canvas

    def _layer_canvas(self, layer, mask=False):
        canvas = Image.new("L" if mask else "RGBA", self.size)
        source = layer.mask if mask else layer.image
        if source is not None:
            canvas.paste(source, (layer.x, layer.y))
        return canvas

    def crop(self, box):
        if len(box) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in box):
            raise ValueError("Crop bounds must be four whole pixel coordinates.")
        left, top, right, bottom = box
        if left < 0 or top < 0 or right > self.width or bottom > self.height:
            raise ValueError("Crop must lie inside the canvas.")
        _dimensions(right - left, bottom - top)
        self.snapshot("Crop")
        self.width, self.height = right - left, bottom - top
        # Preserve off-canvas pixels for later canvas expansion.
        for layer in self.layers:
            layer.x -= left
            layer.y -= top
        self.touch()

    def resize(self, width, height):
        _dimensions(width, height)
        sx, sy = width / self.width, height / self.height
        sizes = [(max(1, round(l.image.width * sx)), max(1, round(l.image.height * sy))) for l in self.layers]
        for size in sizes:
            _dimensions(*size)
        replacements = [(l.image.resize(size, Image.Resampling.LANCZOS),
                         l.mask.resize(size, Image.Resampling.LANCZOS) if l.mask is not None else None)
                        for l, size in zip(self.layers, sizes)]
        self.snapshot("Resize image")
        for layer, (image, mask) in zip(self.layers, replacements):
            if layer.properties.get("tool"):
                layer.properties.setdefault("raster_ops", []).append(["resize", *image.size])
            layer.image, layer.mask = image, mask
            layer.x, layer.y = round(layer.x * sx), round(layer.y * sy)
        self.width, self.height = width, height
        self.touch()

    def rotate(self, degrees):
        _number(degrees, "rotation", -360000, 360000)
        degrees %= 360
        if not degrees:
            return
        # Preflight expanded canvas before allocating any rotated layer.
        rad = math.radians(degrees)
        bound_w = math.ceil(abs(self.width * math.cos(rad)) + abs(self.height * math.sin(rad)))
        bound_h = math.ceil(abs(self.width * math.sin(rad)) + abs(self.height * math.cos(rad)))
        _dimensions(bound_w, bound_h)
        replacements = []
        for layer in self.layers:
            image = self._layer_canvas(layer).rotate(degrees, Image.Resampling.BICUBIC, expand=True)
            mask = (self._layer_canvas(layer, True).rotate(degrees, Image.Resampling.BICUBIC, expand=True)
                    if layer.mask is not None else None)
            replacements.append((image, mask))
        self.snapshot("Rotate")
        for layer, (image, mask) in zip(self.layers, replacements):
            if layer.properties.get("tool"):
                layer.properties.setdefault("raster_ops", []).extend([
                    ["place", self.width, self.height, layer.x, layer.y], ["rotate", degrees]])
            layer.image, layer.mask, layer.x, layer.y = image, mask, 0, 0
        self.width, self.height = replacements[0][0].size
        self.touch()

    def flip(self, horizontal=True):
        operation = Image.Transpose.FLIP_LEFT_RIGHT if horizontal else Image.Transpose.FLIP_TOP_BOTTOM
        self.snapshot("Flip horizontal" if horizontal else "Flip vertical")
        for layer in self.layers:
            if layer.properties.get("tool"):
                layer.properties.setdefault("raster_ops", []).append(["flip", bool(horizontal)])
            layer.image = layer.image.transpose(operation)
            if layer.mask is not None:
                layer.mask = layer.mask.transpose(operation)
            if horizontal:
                layer.x = self.width - layer.x - layer.image.width
            else:
                layer.y = self.height - layer.y - layer.image.height
        self.touch()

    def resize_canvas(self, width, height):
        _dimensions(width, height)
        dx, dy = (width - self.width) // 2, (height - self.height) // 2
        self.snapshot("Resize canvas")
        for layer in self.layers:
            layer.x += dx
            layer.y += dy
        self.width, self.height = width, height
        self.touch()

    def add_layer(self, layer):
        if not isinstance(layer, ImageLayer):
            raise TypeError("Expected an ImageLayer.")
        if len(self.layers) >= MAX_LAYERS:
            raise ValueError("Documents support at most 64 layers.")
        if any(existing.id == layer.id for existing in self.layers):
            raise ValueError("Each layer must have a unique ID; use Duplicate Layer to make a copy.")
        self.snapshot("Add layer")
        self.layers.append(layer)
        self.active_index = len(self.layers) - 1
        self.touch()

    def insert_layer(self, layer, index=None, label="Add layer"):
        """Insert above the active layer (or at ``index``) and make it active."""
        if not isinstance(layer, ImageLayer):
            raise TypeError("Expected an ImageLayer.")
        if len(self.layers) >= MAX_LAYERS:
            raise ValueError("Documents support at most 64 layers.")
        if any(existing.id == layer.id for existing in self.layers):
            layer = replace(layer, id=uuid.uuid4().hex)
        index = self.active_index + 1 if index is None else max(0, min(len(self.layers), index))
        self.snapshot(label)
        self.layers.insert(index, layer)
        self.active_index = index
        self.touch()
        return layer

    def offset_layer(self, index, dx, dy):
        """Move a layer by whole pixels; no pixels are resampled."""
        layer = self.layers[index]
        if layer.locked:
            raise ValueError("Unlock the layer before moving it.")
        dx, dy = round(dx), round(dy)
        if not dx and not dy:
            return False
        if abs(layer.x + dx) > 1_000_000 or abs(layer.y + dy) > 1_000_000:
            raise ValueError("The layer cannot move that far.")
        self.snapshot("Move layer")
        layer.x += dx
        layer.y += dy
        self.touch()
        return True

    def transform_layer(self, index, matrix, label="Transform layer"):
        """Apply a canvas-space affine transform to a layer, resampling once.

        A layer that was transformed before is re-rendered from its original pixels with
        the combined transform, so repeated scaling and rotation never compound blur. Its
        mask follows the transform. Editable annotation layers record the transform so
        their text and shapes can still be edited.
        """
        layer = self.layers[index]
        if layer.locked:
            raise ValueError("Unlock the layer before transforming it.")
        delta = affine(matrix)
        old_local_to_canvas = affine_translate(layer.x, layer.y)
        source = layer.original
        annotation = bool(layer.properties.get("tool"))
        if source is not None:
            base, to_canvas = source.image, affine_multiply(source.matrix, old_local_to_canvas)
        else:
            base, to_canvas = layer.image, old_local_to_canvas
        to_canvas = affine_multiply(to_canvas, delta)
        left, top, right, bottom = transformed_bounds(to_canvas, *base.size)
        size = (max(1, right - left), max(1, bottom - top))
        _dimensions(*size)
        to_local = affine_multiply(to_canvas, affine_translate(-left, -top))
        image = warp(base, to_local, size)
        mask = None
        if layer.mask is not None:
            mask_matrix = affine_multiply(affine_multiply(old_local_to_canvas, delta), affine_translate(-left, -top))
            mask = warp(layer.mask, mask_matrix, size, fill=255)
        self.snapshot(label)
        if annotation:
            layer.properties.setdefault("raster_ops", []).append(
                ["affine", *affine_multiply(affine_multiply(old_local_to_canvas, delta), affine_translate(-left, -top)), *size])
            layer.source = None
        else:
            layer.source = LayerSource(source.image if source is not None else layer.image, to_local, image)
        layer.image, layer.mask, layer.x, layer.y = image, mask, left, top
        self.touch()
        return layer

    def reset_layer_transform(self, index):
        """Return a transformed layer to its original size and orientation, keeping its centre."""
        layer = self.layers[index]
        source = layer.original
        if source is None:
            raise ValueError("This layer has not been scaled or rotated.")
        cx, cy, *_ = layer.frame
        w, h = source.image.size
        current = affine_multiply(source.matrix, affine_translate(layer.x, layer.y))
        target = affine_translate(cx - w / 2, cy - h / 2)
        return self.transform_layer(index, affine_multiply(affine_invert(current), target), "Reset transform")

    def fit_layer_to_canvas(self, index):
        """Scale a layer proportionally to fit inside the canvas and centre it."""
        layer = self.layers[index]
        cx, cy, w, h, angle = layer.frame
        if angle:
            left, top, right, bottom = layer.x, layer.y, layer.x + layer.image.width, layer.y + layer.image.height
            w, h = right - left, bottom - top
        scale = min(self.width / abs(w), self.height / abs(h))
        matrix = affine_multiply(affine_about((cx, cy), scale, scale),
                                 affine_translate(self.width / 2 - cx, self.height / 2 - cy))
        return self.transform_layer(index, matrix, "Fit layer to canvas")

    def center_layer(self, index):
        layer = self.layers[index]
        cx, cy, *_ = layer.frame
        return self.offset_layer(index, self.width / 2 - cx, self.height / 2 - cy)

    def reveal_all(self):
        """Grow the canvas to include every layer's pixels (Photoshop's Reveal All)."""
        left = min([0] + [l.x for l in self.layers])
        top = min([0] + [l.y for l in self.layers])
        right = max([self.width] + [l.x + l.image.width for l in self.layers])
        bottom = max([self.height] + [l.y + l.image.height for l in self.layers])
        width, height = right - left, bottom - top
        if (width, height) == self.size:
            return False
        _dimensions(width, height)
        self.snapshot("Reveal all")
        for layer in self.layers:
            layer.x -= left
            layer.y -= top
        self.width, self.height = width, height
        guides = self.metadata.get("guides")
        if isinstance(guides, list):
            self.metadata["guides"] = [[axis, value - (left if axis == "x" else top)] for axis, value in guides]
        self.touch()
        return True

    def layer_selection(self, index, selection):
        """The canvas selection in a layer's own pixel coordinates (None stays None)."""
        if selection is None:
            return None
        layer = self.layers[index]
        return selection.crop((layer.x, layer.y, layer.x + layer.image.width, layer.y + layer.image.height))

    def copy_layer(self, index, selection=None):
        """A standalone copy of a layer, or of its selected pixels, at its canvas position.

        Without a selection the copy keeps the mask, opacity, blend mode and annotation
        data. With one, the visible selected pixels become a plain layer, transparent
        outside the selection. Returns None when the selection holds no visible pixels.
        """
        layer = self.layers[index]
        if selection is None:
            return replace(layer, id=uuid.uuid4().hex, properties=copy.deepcopy(layer.properties))
        coverage = self.layer_selection(index, selection)
        alpha = layer.image.getchannel("A")
        if layer.mask is not None:
            alpha = ImageChops.multiply(alpha, layer.mask)
        alpha = ImageChops.multiply(alpha, coverage)
        bounds = alpha.getbbox()
        if bounds is None:
            return None
        pixels = layer.image.copy()
        pixels.putalpha(alpha)
        return ImageLayer(layer.name + " (selection)", pixels.crop(bounds), x=layer.x + bounds[0], y=layer.y + bounds[1])

    def clear_selection_pixels(self, index, selection, label="Clear"):
        """Make a layer's selected pixels transparent (Cut, Delete on pixels)."""
        layer = self.layers[index]
        if layer.locked:
            raise ValueError("Unlock the layer before changing its pixels.")
        coverage = self.layer_selection(index, selection)
        if coverage is None or coverage.getbbox() is None:
            return False
        image = layer.image.copy()
        image.putalpha(ImageChops.multiply(image.getchannel("A"), ImageChops.invert(coverage)))
        self.snapshot(label)
        layer.image = image
        if layer.properties.get("tool"):
            layer.properties = {}
            layer.kind = "image"
        self.touch()
        return True

    def remove_layer(self, index):
        if not 0 <= index < len(self.layers):
            raise IndexError("Invalid layer.")
        if self.layers[index].locked:
            raise ValueError("Unlock the layer before deleting it.")
        self.snapshot("Delete layer")
        self.layers.pop(index)
        if index < self.active_index:
            self.active_index -= 1
        self.active_index = min(self.active_index, len(self.layers) - 1)
        self.touch()

    def duplicate_layer(self, index):
        if len(self.layers) >= MAX_LAYERS:
            raise ValueError("Documents support at most 64 layers.")
        original = self.layers[index]
        layer = replace(original, name=original.name + " copy", id=uuid.uuid4().hex,
                        properties=copy.deepcopy(original.properties))
        self.snapshot("Duplicate layer")
        self.layers.insert(index + 1, layer)
        self.active_index = index + 1
        self.touch()
        return layer

    def move_layer(self, index, new_index):
        if not (0 <= index < len(self.layers) and 0 <= new_index < len(self.layers)):
            raise IndexError("Invalid layer order.")
        if index == new_index:
            return
        active_id = self.active_layer.id
        self.snapshot("Reorder layer")
        self.layers.insert(new_index, self.layers.pop(index))
        self.active_index = next(i for i, layer in enumerate(self.layers) if layer.id == active_id)
        self.touch()

    def flatten(self):
        image = self.composite()
        self.snapshot("Flatten image")
        self.layers = [ImageLayer("Flattened image", image)]
        self.active_index = 0
        self.touch()

    def save_project(self, path):
        path = Path(path)
        if self.source_path and (path.resolve() == Path(self.source_path).resolve()
                                 or (path.exists() and Path(self.source_path).exists()
                                     and os.path.samefile(path, self.source_path))):
            raise ValueError("Save the editable project separately from the original image.")
        payload = self._manifest()
        fd, temporary = tempfile.mkstemp(prefix=".engimg-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                self._write_archive(file, payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        self._saved_revision = self.revision

    def to_bytes(self):
        """The project as ENGIMG bytes (used for the in-app layer clipboard)."""
        buffer = io.BytesIO()
        self._write_archive(buffer, self._manifest())
        return buffer.getvalue()

    @classmethod
    def from_bytes(cls, data):
        """Read ENGIMG bytes with every check ``load_project`` applies to files."""
        if len(data) > MAX_PROJECT_BYTES:
            raise ValueError("Project exceeds the 512 MiB archive limit.")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return cls._read_archive(archive)

    def _manifest(self):
        _dimensions(self.width, self.height)
        _number(self.dpi, "DPI", 1, 100000)
        if len(self.layers) > MAX_LAYERS or len({layer.id for layer in self.layers}) != len(self.layers):
            raise ValueError("The project must contain 1–64 layers with unique IDs.")
        if type(self.active_index) is not int or not min(0, len(self.layers) - 1) <= self.active_index < len(self.layers):
            raise ValueError("Invalid active layer.")
        for layer in self.layers:
            replace(layer)  # Validate fields changed by editing operations.
            if layer.image.mode != "RGBA" or (layer.mask is not None and layer.mask.mode != "L"):
                raise ValueError("Project images must be RGBA and masks must be grayscale.")
        sources = [layer.original for layer in self.layers]
        version = 2 if any(sources) else 1
        manifest = {"format": "ENGIMG", "version": version, "width": self.width, "height": self.height,
                    "dpi": self.dpi, "active_index": self.active_index, "layers": [],
                    "metadata": _pack_metadata(self.metadata)}
        pixels = sum(l.image.width * l.image.height for l in self.layers)
        pixels += sum(s.image.width * s.image.height for s in sources if s is not None)
        if pixels > MAX_PROJECT_PIXELS:
            raise ValueError("Project exceeds the total embedded-pixel limit.")
        for i, (layer, source) in enumerate(zip(self.layers, sources)):
            record = {"name": layer.name, "id": layer.id, "kind": layer.kind,
                "visible": layer.visible, "locked": layer.locked, "opacity": layer.opacity,
                "blend": layer.blend, "x": layer.x, "y": layer.y, "properties": layer.properties,
                "image": f"layers/{i}.png", "mask": f"masks/{i}.png" if layer.mask is not None else None}
            if source is not None:
                record["source"] = f"sources/{i}.png"
                record["source_matrix"] = list(source.matrix)
            manifest["layers"].append(record)
        payload = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise ValueError("Project metadata is too large.")
        return payload

    def _write_archive(self, file, payload):
        with zipfile.ZipFile(file, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", payload)
            for i, layer in enumerate(self.layers):
                source = layer.original
                for folder, image in (("layers", layer.image), ("masks", layer.mask),
                                      ("sources", source.image if source is not None else None)):
                    if image is not None:
                        buffer = io.BytesIO()
                        image.save(buffer, format="PNG")
                        archive.writestr(f"{folder}/{i}.png", buffer.getvalue())
                        if file.tell() > MAX_PROJECT_BYTES:
                            raise ValueError("Project exceeds the 512 MiB archive limit.")

    @classmethod
    def load_project(cls, path):
        if Path(path).stat().st_size > MAX_PROJECT_BYTES:
            raise ValueError("Project exceeds the 512 MiB archive limit.")
        with zipfile.ZipFile(path) as archive:
            return cls._read_archive(archive)

    @classmethod
    def _read_archive(cls, archive):
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if (len(infos) > MAX_LAYERS * 3 + 1 or len(set(names)) != len(names)
                or sum(i.file_size for i in infos) > MAX_PROJECT_BYTES):
            raise ValueError("Invalid or oversized project archive.")
        if any(i.flag_bits & 1 or (i.external_attr >> 16) & 0o170000 == 0o120000 for i in infos):
            raise ValueError("Encrypted assets and links are not allowed in projects.")
        try:
            manifest_info = archive.getinfo("manifest.json")
        except KeyError as exc:
            raise ValueError("Project has no manifest.") from exc
        if manifest_info.file_size > 1024 * 1024:
            raise ValueError("Project manifest is too large.")
        try:
            data = json.loads(archive.read("manifest.json"), parse_constant=_invalid_constant)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid project manifest.") from exc
        if (not isinstance(data, dict) or data.get("format") != "ENGIMG" or type(data.get("version")) is not int
                or data["version"] not in (1, 2)):
            raise ValueError("Unsupported ENGIMG project version. It may come from a newer ENG Drawing Viewer.")
        _dimensions(data.get("width"), data.get("height"))
        dpi = _number(data.get("dpi", 96), "DPI", 1, 100000)
        records = data.get("layers")
        if not isinstance(records, list) or len(records) > MAX_LAYERS:
            raise ValueError("Invalid project layers.")
        expected = {"manifest.json"}
        layers, ids, total_pixels = [], set(), 0
        for i, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError("Invalid layer record.")
            image_name = f"layers/{i}.png"
            mask_name = f"masks/{i}.png" if record.get("mask") is not None else None
            source_name = f"sources/{i}.png" if record.get("source") is not None else None
            if (record.get("image") != image_name or record.get("mask") != mask_name
                    or record.get("source") != source_name or (source_name and data["version"] < 2)):
                raise ValueError("Invalid project asset path.")
            expected.add(image_name)
            if mask_name:
                expected.add(mask_name)
            image = _read_png(archive, image_name, "RGBA", MAX_PROJECT_PIXELS - total_pixels)
            total_pixels += image.width * image.height
            if total_pixels > MAX_PROJECT_PIXELS:
                raise ValueError("Project exceeds the total embedded-pixel limit.")
            mask = _read_png(archive, mask_name, "L", expected_size=image.size) if mask_name else None
            source = None
            if source_name:
                expected.add(source_name)
                original = _read_png(archive, source_name, "RGBA", MAX_PROJECT_PIXELS - total_pixels)
                total_pixels += original.width * original.height
                source = LayerSource(original, affine(record.get("source_matrix")), image)
            for key in ("visible", "locked"):
                if type(record.get(key)) is not bool:
                    raise ValueError(f"Invalid layer {key}.")
            for key in ("x", "y"):
                if type(record.get(key)) is not int or abs(record[key]) > 1_000_000:
                    raise ValueError("Invalid layer offset.")
            layer_id = _text(record.get("id"), "layer ID", 128)
            if not layer_id or layer_id in ids:
                raise ValueError("Duplicate or empty layer ID.")
            ids.add(layer_id)
            layers.append(ImageLayer(name=_text(record.get("name"), "layer name"), image=image,
                mask=mask, visible=record["visible"], locked=record["locked"],
                opacity=_number(record.get("opacity"), "opacity", 0, 1),
                blend=record.get("blend"), x=record["x"], y=record["y"], id=layer_id,
                kind=_text(record.get("kind", "image"), "layer kind", 64),
                properties=record.get("properties", {}), source=source))
        if set(names) != expected:
            raise ValueError("Project contains missing or unrecognized assets.")
        active = data.get("active_index", 0)
        if type(active) is not int or not min(0, len(layers) - 1) <= active < len(layers):
            raise ValueError("Invalid active layer.")
        return cls(data["width"], data["height"], layers, dpi=dpi,
                   metadata=_unpack_metadata(data.get("metadata", {})), active_index=active)


def _invalid_constant(value):
    raise ValueError(f"Nonfinite JSON value: {value}.")


def _read_png(archive, name, mode, max_pixels=MAX_EDITOR_PIXELS, expected_size=None):
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError("Project is missing an image asset.") from exc
    if info.file_size > MAX_EDITOR_PIXELS * 5 + 1024 * 1024:
        raise ValueError("Project image asset is too large.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with archive.open(name) as file, Image.open(file) as image:
            if image.format != "PNG" or image.mode != mode:
                raise ValueError("Invalid project PNG asset.")
            _dimensions(*image.size)
            if expected_size is not None and image.size != expected_size:
                raise ValueError("A layer mask must match its image dimensions.")
            if image.width * image.height > max_pixels:
                raise ValueError("Project exceeds the total embedded-pixel limit.")
            result = image.copy()
    return result


def _pack_metadata(metadata):
    result = {}
    for key, value in metadata.items():
        if isinstance(value, bytes):
            if key in ("icc_profile", "exif"):
                result[key] = {"base64": base64.b64encode(value).decode("ascii")}
        elif isinstance(value, (str, int, float, bool, list, dict)) or value is None:
            result[key] = value
    return result


def _unpack_metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("Invalid project metadata.")
    result = dict(metadata)
    for key in ("icc_profile", "exif"):
        if key in result:
            value = result[key]
            if not isinstance(value, dict) or set(value) != {"base64"} or not isinstance(value["base64"], str):
                raise ValueError("Invalid encoded project metadata.")
            try:
                result[key] = base64.b64decode(value["base64"], validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid encoded project metadata.") from exc
    return result
