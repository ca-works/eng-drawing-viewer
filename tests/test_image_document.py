import io
import json
import zipfile

import pytest
from PIL import Image, ImageCms

from eng_viewer.core.image_document import ImageDocument, ImageLayer


def test_exif_orientation_and_icc(tmp_path):
    image = Image.new("RGB", (8, 4), "red")
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "Synthetic fixture"
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    path = tmp_path / "orientation.jpg"
    image.save(path, exif=exif, icc_profile=profile, dpi=(144, 144))
    document = ImageDocument.from_file(path)
    assert document.size == (4, 8)
    assert document.metadata["icc_profile"] == profile
    normalized = Image.Exif()
    normalized.load(document.metadata["exif"])
    assert 274 not in normalized
    assert normalized[270] == "Synthetic fixture"
    assert document.dpi == 144
    assert document.source_path == str(path.resolve())


def test_mask_opacity_offsets_and_visibility():
    document = ImageDocument.from_image(Image.new("RGBA", (4, 4), "blue"))
    mask = Image.new("L", (2, 2), 255)
    mask.putpixel((0, 0), 0)
    layer = ImageLayer("Red", Image.new("RGBA", (2, 2), "red"), mask, opacity=.5, x=1, y=1)
    document.add_layer(layer)
    image = document.composite()
    assert image.getpixel((0, 0)) == (0, 0, 255, 255)
    assert image.getpixel((1, 1)) == (0, 0, 255, 255)
    assert image.getpixel((2, 2)) == (128, 0, 127, 255)
    layer.visible = False
    assert document.composite().getpixel((2, 2)) == (0, 0, 255, 255)


@pytest.mark.parametrize("blend,expected", [("multiply", (32, 64, 96)), ("screen", (160, 192, 224)),
                                          ("overlay", (64, 128, 192))])
def test_blends_and_transparent_backdrop(blend, expected):
    document = ImageDocument.from_image(Image.new("RGBA", (2, 1), (64, 128, 192, 255)))
    document.layers[0].image.putpixel((1, 0), (0, 0, 0, 0))
    document.add_layer(ImageLayer("Gray", Image.new("RGBA", (2, 1), (128, 128, 128, 255)), blend=blend))
    actual = document.composite()
    assert all(abs(a - b) <= 1 for a, b in zip(actual.getpixel((0, 0))[:3], expected))
    assert actual.getpixel((1, 0)) == (128, 128, 128, 255)


def test_crop_preserves_hidden_pixels_and_undo():
    source = Image.new("RGBA", (6, 4), "red")
    source.putpixel((2, 1), (0, 255, 0, 255))
    document = ImageDocument.from_image(source)
    document.crop((2, 1, 5, 3))
    assert document.size == (3, 2)
    assert document.composite().getpixel((0, 0)) == (0, 255, 0, 255)
    assert document.active_layer.image.size == (6, 4)
    assert document.undo() and document.size == (6, 4)
    assert not document.dirty
    assert document.redo() and document.size == (3, 2)
    assert document.dirty


def test_resize_rotate_flip_canvas_with_mask():
    image = Image.new("RGBA", (2, 1), "red")
    image.putpixel((1, 0), (0, 255, 0, 255))
    document = ImageDocument.from_image(image)
    document.active_layer.mask = Image.new("L", (2, 1), 255)
    document.flip()
    assert document.composite().getpixel((0, 0)) == (0, 255, 0, 255)
    document.rotate(90)
    assert document.size == (1, 2)
    assert document.active_layer.mask.size == (1, 2)
    document.resize(2, 4)
    assert document.size == document.active_layer.mask.size == (2, 4)
    document.resize_canvas(6, 8)
    assert (document.active_layer.x, document.active_layer.y) == (2, 2)
    assert document.composite().getpixel((0, 0))[3] == 0
    assert document.undo() and document.size == (2, 4)


def test_history_shares_buffers_but_copies_properties_and_bounds_memory():
    document = ImageDocument.from_image(Image.new("RGBA", (10, 10), "red"))
    original = document.active_layer.image
    document.active_layer.properties = {"text": {"content": "before"}}
    document.snapshot("Paint")
    document.active_layer.image = Image.new("RGBA", (10, 10), "blue")
    document.active_layer.properties["text"]["content"] = "after"
    document.touch()
    assert document.history_bytes == 400
    assert document.undo()
    assert document.active_layer.image is original
    assert document.active_layer.properties["text"]["content"] == "before"
    assert document.redo()
    document.history_budget = 500
    document.snapshot("Paint again")
    document.active_layer.image = Image.new("RGBA", (10, 10), "green")
    document.touch()
    assert document.history_bytes <= 500


def test_layer_order_duplicate_lock_delete_flatten():
    document = ImageDocument.from_image(Image.new("RGBA", (2, 2), "red"))
    document.add_layer(ImageLayer("Blue", Image.new("RGBA", (2, 2), "blue")))
    duplicate = document.duplicate_layer(1)
    assert duplicate.id != document.layers[1].id
    document.move_layer(0, 2)
    assert document.composite().getpixel((0, 0)) == (255, 0, 0, 255)
    document.layers[1].locked = True
    with pytest.raises(ValueError, match="Unlock"):
        document.remove_layer(1)
    document.flatten()
    assert len(document.layers) == 1
    assert document.undo() and len(document.layers) == 3


def test_project_roundtrip_retains_editable_state_and_dirty_mark(tmp_path):
    document = ImageDocument.from_image(Image.new("RGBA", (5, 3), (20, 40, 60, 128)))
    document.add_layer(ImageLayer("Caption", Image.new("RGBA", (4, 2), "white"),
        mask=Image.new("L", (4, 2), 128), opacity=.75, x=2, y=-1,
        kind="text", properties={"text": "A1", "font_size": 24}, blend="screen"))
    document.metadata = {"icc_profile": b"synthetic profile", "format": "PNG"}
    path = tmp_path / "edit.engimg"
    document.save_project(path)
    assert not document.dirty
    reopened = ImageDocument.load_project(path)
    assert reopened.composite().tobytes() == document.composite().tobytes()
    assert reopened.active_layer.properties == {"text": "A1", "font_size": 24}
    assert reopened.active_layer.kind == "text"
    assert reopened.active_layer.mask.mode == "L"
    assert reopened.metadata == document.metadata
    assert reopened.source_path == ""
    assert reopened.active_index == 1
    document.flip()
    assert document.dirty and document.undo() and not document.dirty


def _mutate_project(source, target, change, extra=None):
    with zipfile.ZipFile(source) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(contents["manifest.json"])
    change(manifest)
    contents["manifest.json"] = json.dumps(manifest).encode()
    if extra:
        contents.update(extra)
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(version=2),
    lambda m: m.update(width=100000000),
    lambda m: m.update(width=True),
    lambda m: m.update(dpi=float("nan")),
    lambda m: m.update(active_index=100),
    lambda m: m["layers"][0].update(image="../../outside.png"),
    lambda m: m["layers"][0].update(opacity=-1),
    lambda m: m["layers"][0].update(visible="yes"),
    lambda m: m["layers"][0].update(properties=[]),
])
def test_project_rejects_malformed_manifest(tmp_path, mutation):
    original, malformed = tmp_path / "good.engimg", tmp_path / "bad.engimg"
    ImageDocument.from_image(Image.new("RGBA", (2, 2))).save_project(original)
    _mutate_project(original, malformed, mutation)
    with pytest.raises(ValueError):
        ImageDocument.load_project(malformed)


def test_project_rejects_extra_entries_and_wrong_asset_codec(tmp_path):
    original, malformed = tmp_path / "good.engimg", tmp_path / "bad.engimg"
    ImageDocument.from_image(Image.new("RGBA", (2, 2))).save_project(original)
    _mutate_project(original, malformed, lambda m: None, {"../../outside": b"no"})
    with pytest.raises(ValueError, match="unrecognized"):
        ImageDocument.load_project(malformed)
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, "JPEG")
    _mutate_project(original, malformed, lambda m: None, {"layers/0.png": buffer.getvalue()})
    with pytest.raises(ValueError, match="PNG"):
        ImageDocument.load_project(malformed)


def test_project_and_edits_protect_source_and_reject_invalid_sizes(tmp_path):
    source = tmp_path / "original.png"
    Image.new("RGBA", (2, 2), "red").save(source)
    original = source.read_bytes()
    document = ImageDocument.from_file(source)
    with pytest.raises(ValueError, match="separately"):
        document.save_project(source)
    assert source.read_bytes() == original
    for operation in (lambda: document.resize(0, 2), lambda: document.crop((0, 0, 3, 3)),
                      lambda: document.rotate(float("nan"))):
        with pytest.raises(ValueError):
            operation()
    assert not document.can_undo


def test_duplicate_layer_identity_and_invalid_save_state_rejected(tmp_path):
    document = ImageDocument.from_image(Image.new("RGBA", (2, 2)))
    with pytest.raises(ValueError, match="unique ID"):
        document.add_layer(document.active_layer)
    assert not document.can_undo
    document.active_layer.opacity = float("nan")
    output = tmp_path / "invalid.engimg"
    with pytest.raises(ValueError):
        document.save_project(output)
    assert not output.exists()


def test_project_mismatched_mask_rejected_before_decode(tmp_path):
    original, malformed = tmp_path / "good.engimg", tmp_path / "bad.engimg"
    document = ImageDocument.from_image(Image.new("RGBA", (2, 2)))
    document.active_layer.mask = Image.new("L", (2, 2), 255)
    document.save_project(original)
    buffer = io.BytesIO()
    Image.new("L", (300, 200), 255).save(buffer, "PNG")
    _mutate_project(original, malformed, lambda m: None, {"masks/0.png": buffer.getvalue()})
    with pytest.raises(ValueError, match="mask must match"):
        ImageDocument.load_project(malformed)
