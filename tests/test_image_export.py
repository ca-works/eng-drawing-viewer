import io
import random

import pytest
from PIL import Image, ImageCms

from eng_viewer.core.image_document import ImageDocument
from eng_viewer.core.image_export import (batch_convert, encode_image, export_file,
                                          optimize_export, supported_export_formats)


@pytest.mark.parametrize("format", ["JPEG", "PNG", "TIFF", "WEBP", "BMP", "GIF", "AVIF"])
def test_installed_codec_roundtrip_dimensions(format):
    assert format in supported_export_formats()
    data = encode_image(Image.new("RGBA", (17, 11), (100, 80, 20, 180)), format)
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.size == (17, 11)
        assert image.format == format


@pytest.mark.parametrize("format", ["PNG", "TIFF", "WEBP", "AVIF"])
def test_alpha_survives_supported_continuous_alpha_formats(format):
    data = encode_image(Image.new("RGBA", (8, 8), (100, 80, 20, 120)), format)
    with Image.open(io.BytesIO(data)) as image:
        assert abs(image.convert("RGBA").getpixel((4, 4))[3] - 120) <= 1


def test_gif_transparency_and_jpeg_white_background():
    image = Image.new("RGBA", (8, 8), (255, 0, 0, 0))
    with Image.open(io.BytesIO(encode_image(image, "GIF"))) as gif:
        assert gif.convert("RGBA").getpixel((0, 0))[3] == 0
    with Image.open(io.BytesIO(encode_image(image, "JPEG"))) as jpeg:
        assert min(jpeg.getpixel((0, 0))) >= 250


def test_icc_dpi_and_metadata_removal():
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = Image.Exif()
    exif[274], exif[270], exif[40962] = 6, "Private test description", 999
    image = Image.new("RGBA", (20, 10), "red")
    metadata = {"icc_profile": profile, "exif": exif.tobytes()}
    with Image.open(io.BytesIO(encode_image(image, "JPEG", dpi=300, metadata=metadata))) as kept:
        assert kept.info["icc_profile"] == profile
        assert kept.info["dpi"] == (300, 300)
        assert 274 not in kept.getexif()
        assert kept.getexif()[40962] == 20
    with Image.open(io.BytesIO(encode_image(image, "JPEG"))) as stripped:
        assert "icc_profile" not in stripped.info
        assert 270 not in stripped.getexif()


def _noise(size=(128, 128)):
    randomizer = random.Random(42)
    return Image.frombytes("RGB", size, randomizer.randbytes(size[0] * size[1] * 3)).convert("RGBA")


def test_target_solver_uses_actual_bytes_and_preserves_dimensions():
    image = _noise()
    low = len(encode_image(image, "JPEG", quality=35))
    high = len(encode_image(image, "JPEG", quality=90))
    target = (low + high) // 2
    result = optimize_export(image, "JPEG", target_bytes=target)
    assert result.target_met
    assert len(result.data) <= target
    assert 35 <= result.quality < 90
    assert (result.width, result.height) == image.size


def test_unachievable_target_is_honest_and_never_silently_resizes():
    image = _noise()
    result = optimize_export(image, "JPEG", target_bytes=100)
    assert not result.target_met and result.reason
    assert (result.width, result.height) == image.size
    assert len(result.data) > 100
    assert result.quality >= 35


def test_explicit_resize_can_reach_target_and_impossible_header_fails():
    image = _noise()
    result = optimize_export(image, "JPEG", target_bytes=1500, allow_resize=True)
    assert result.target_met and len(result.data) <= 1500
    assert result.width < image.width and result.height < image.height
    tiny = optimize_export(image, "PNG", target_bytes=1, allow_resize=True)
    assert not tiny.target_met and len(tiny.data) > 1


def test_lossless_target_retains_pixels_when_fitting():
    image = _noise((30, 20))
    result = optimize_export(image, "PNG", target_bytes=10000)
    assert result.target_met
    with Image.open(io.BytesIO(result.data)) as reopened:
        assert reopened.tobytes() == image.tobytes()


def test_atomic_export_no_original_or_alias_overwrites(tmp_path):
    original = tmp_path / "source.png"
    original.write_bytes(b"original")
    hardlink = tmp_path / "alias.png"
    hardlink.hardlink_to(original)
    symlink = tmp_path / "symlink.png"
    symlink.symlink_to(original)
    result = optimize_export(Image.new("RGBA", (2, 2), "red"), "PNG")
    for path in (original, hardlink, symlink):
        with pytest.raises(ValueError, match="original"):
            export_file(result, path, source_paths=[original], overwrite=True)
    assert original.read_bytes() == b"original"
    output = export_file(result, tmp_path / "output.png", source_paths=[original])
    assert output.read_bytes() == result.data
    with pytest.raises(FileExistsError):
        export_file(result, output)
    export_file(result, output, overwrite=True)
    assert not list(tmp_path.glob(".eng-image-*"))


def test_batch_collision_handling_and_sources_unchanged(tmp_path):
    inputs = []
    for folder in ("a", "b"):
        directory = tmp_path / folder
        directory.mkdir()
        source = directory / "same.png"
        Image.new("RGBA", (12, 8), "red").save(source)
        inputs.append(source)
    hashes = [p.read_bytes() for p in inputs]
    outputs = batch_convert(inputs, tmp_path / "exports", format="WEBP")
    assert len(set(outputs)) == 2
    assert [p.name for p in outputs] == ["same_converted.webp", "same_converted_2.webp"]
    assert all(ImageDocument.from_file(path).size == (12, 8) for path in outputs)
    assert [p.read_bytes() for p in inputs] == hashes
    with pytest.raises(ValueError, match="Target not met"):
        batch_convert(inputs, tmp_path / "too-small", format="JPEG", target_bytes=1)
    assert not list((tmp_path / "too-small").iterdir())


def test_heic_decoder_on_this_build(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    source = tmp_path / "photo.heic"
    Image.new("RGB", (16, 8), "red").save(source, format="HEIF", quality=90)
    document = ImageDocument.from_file(source)
    assert document.size == (16, 8)
    assert document.metadata["format"] == "HEIF"
    assert "HEIF" in supported_export_formats()


@pytest.mark.parametrize("options", [{"target_bytes": 0}, {"quality": 10}, {"quality": 101},
                                      {"format": "not-a-format"}, {"dpi": float("nan")}])
def test_invalid_export_options(options):
    with pytest.raises(ValueError):
        optimize_export(Image.new("RGBA", (2, 2)), **options)
