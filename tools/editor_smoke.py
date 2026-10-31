"""Synthetic frozen-runtime smoke checks, callable from the app's smoke mode.

No user files or system clipboard are touched. No trained weights or network are
used. Import this module only for explicit validation; normal startup stays lean.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import time


def _require(condition, message):
    # Assertions are removed by the optimized packaged interpreter.
    if not condition:
        raise RuntimeError("Editor smoke check failed: " + message)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_editor_smoke():
    """Return verified runtime facts or raise; requires an existing Qt application."""
    from PIL import Image, ImageDraw
    from PySide6.QtGui import QGuiApplication
    from eng_viewer.core.image_document import ImageDocument, ImageLayer
    from eng_viewer.core.image_export import encode_image, optimize_export, export_file, supported_export_formats
    from eng_viewer.core.image_clipboard import prepare_clipboard
    from eng_viewer.core.image_tools import rectangle_selection, color_selection, heal
    from eng_viewer.core.vector_document import VectorDocument, VectorObject, import_svg, import_dxf
    from eng_viewer.core.image_ai import read_model_pack, OfflineSegmenter
    import onnx

    _require(QGuiApplication.instance() is not None, "Qt application is required")
    started = time.perf_counter()
    report = {"fixture": "synthetic-only", "trained_model_tested": False}
    with tempfile.TemporaryDirectory(prefix="eng-editor-smoke-") as directory:
        root = Path(directory)
        source = Image.new("RGBA", (128, 96), (35, 58, 85, 0))
        draw = ImageDraw.Draw(source)
        draw.rectangle((16, 12, 110, 80), fill=(30, 100, 210, 255))
        draw.ellipse((35, 25, 80, 70), fill=(220, 80, 30, 170))
        original = root / "original.png"
        source.save(original)
        original_hash = _sha(original)
        document = ImageDocument.from_file(original)
        mask = rectangle_selection(source.size, (20, 20, 90, 70))
        document.add_layer(ImageLayer("Masked detail", source.copy(), mask=mask, opacity=.5))
        before = document.composite().tobytes()
        document.crop((8, 8, 120, 88))
        _require(document.size == (112, 80), "crop dimensions")
        _require(document.undo() and document.composite().tobytes() == before, "undo crop restores pixels")
        _require(document.redo() and document.size == (112, 80), "redo crop")
        document.resize(84, 60)
        project = root / "editable.engimg"
        document.save_project(project)
        reopened = ImageDocument.load_project(project)
        _require(reopened.composite().tobytes() == document.composite().tobytes(), "ENGIMG composite round trip")
        _require(len(reopened.layers) == 2 and reopened.active_layer.mask is not None, "ENGIMG editable layers/mask")
        report["image_project_roundtrip"] = True

        formats = supported_export_formats()
        for fmt in formats:
            encoded = encode_image(source, fmt, quality=88, dpi=150)
            with Image.open(io.BytesIO(encoded)) as decoded:
                decoded.load()
                _require(decoded.size == source.size, f"{fmt} decode dimensions")
                if fmt in {"PNG", "TIFF", "WEBP"}:
                    _require(decoded.convert("RGBA").getpixel((0, 0))[3] == 0, f"{fmt} alpha preservation")
            (root / ("actual-output." + fmt.lower())).write_bytes(encoded)
        _require({"PNG", "JPEG", "TIFF", "WEBP", "BMP"}.issubset(formats), "required packaged codecs")
        report["verified_export_formats"] = formats
        optimized = optimize_export(source, "JPEG", target_bytes=8192, allow_resize=False)
        _require(optimized.target_met and len(optimized.data) <= 8192, "target-size JPEG bytes")
        _require((optimized.width, optimized.height) == source.size, "optimizer preserves dimensions")
        export_file(optimized, root / "optimized.jpg", source_paths=[original])
        impossible = optimize_export(source, "PNG", target_bytes=1)
        _require(not impossible.target_met and bool(impossible.reason), "unreachable target is explained")
        report["target_size_bytes"] = len(optimized.data)

        selection = rectangle_selection(source.size, (16, 12, 36, 32))
        for fmt in ("PNG", "JPEG", "TIFF", "BMP"):
            payload = prepare_clipboard(source, {"format": fmt, "region": "selection", "scale": 100}, selection)
            with Image.open(io.BytesIO(payload.data)) as decoded:
                decoded.load()
                _require(decoded.size == (20, 20), "clipboard selection dimensions")
                _require(decoded.format == fmt, "clipboard uses requested encoding")
        report["clipboard_payload_formats"] = ["PNG", "JPEG", "TIFF", "BMP"]
        report["system_clipboard_tested"] = False

        wand = color_selection(source, (20, 20), tolerance=10, contiguous=True)
        _require(wand.getpixel((20, 20)) == 255 and wand.getpixel((0, 0)) == 0, "color selection")
        blemish = source.copy()
        blemish.putpixel((23, 23), (255, 255, 255, 255))
        healed = heal(blemish, rectangle_selection(source.size, (22, 22, 25, 25)))
        _require(healed.getpixel((23, 23)) != blemish.getpixel((23, 23)), "native OpenCV inpainting")
        report["native_selection_inpaint"] = True

        vector = VectorDocument(50, 40)
        vector.add(VectorObject("Rectangle", [["M", 4, 5], ["L", 34, 5], ["L", 34, 25], ["L", 4, 25], ["Z"]],
                                fill="#2860eb", stroke="#203040", stroke_width=.3))
        vector.save(root / "drawing.engvec")
        vector_again = VectorDocument.load(root / "drawing.engvec")
        _require(vector_again.objects[0].commands == vector.objects[0].commands, "vector project round trip")
        vector.export_svg(root / "drawing.svg")
        svg = import_svg(root / "drawing.svg")
        _require(len(svg.objects) == 1 and abs(svg.objects[0].path().boundingRect().width() - 30) < .00001,
                 "SVG physical dimensions")
        vector.export_dxf(root / "drawing.dxf")
        dxf = import_dxf(root / "drawing.dxf")
        _require(len(dxf.objects) == 1 and abs(dxf.objects[0].path().boundingRect().width() - 30) < .00001,
                 "DXF physical dimensions")
        vector.export_pdf(root / "drawing.pdf")
        _require((root / "drawing.pdf").read_bytes().startswith(b"%PDF-"), "vector PDF output")
        _require(vector.render_image().save(str(root / "drawing.png")), "vector raster output")
        report["vector_roundtrips"] = ["ENG vector", "SVG", "DXF", "PDF", "PNG"]

        # Tiny deterministic graph verifies frozen ONNX/native wiring only.
        pack_root = root / "synthetic-model"
        pack_root.mkdir()
        graph = onnx.helper.make_graph(
            [onnx.helper.make_node("ReduceMean", ["input"], ["output"], axes=[1], keepdims=1)],
            "synthetic-mask-contract",
            [onnx.helper.make_tensor_value_info("input", onnx.TensorProto.FLOAT, [1, 3, 32, 32])],
            [onnx.helper.make_tensor_value_info("output", onnx.TensorProto.FLOAT, [1, 1, 32, 32])])
        model = onnx.helper.make_model(graph, opset_imports=[onnx.helper.make_opsetid("", 13)])
        model.ir_version = 8
        onnx.save(model, str(pack_root / "model.onnx"))
        (pack_root / "LICENSE.txt").write_text("Synthetic smoke fixture. No trained weights.\n")
        manifest = {"format": "eng-image-ai", "version": 1, "id": "synthetic-smoke", "name": "Synthetic smoke", "family": "birefnet",
                    "model": {"file": "model.onnx", "sha256": _sha(pack_root / "model.onnx")},
                    "license": {"spdx": "CC0-1.0", "file": "LICENSE.txt", "sha256": _sha(pack_root / "LICENSE.txt")},
                    "input": {"name": "input", "layout": "NCHW", "color": "RGB", "size": [32, 32],
                              "mean": [.485, .456, .406], "std": [.229, .224, .225]},
                    "output": {"name": "output", "layout": "NCHW", "kind": "logits"}}
        (pack_root / "manifest.json").write_text(json.dumps(manifest))
        segmenter = OfflineSegmenter(read_model_pack(pack_root))
        ai_mask = segmenter.remove_background_mask(Image.new("RGB", (17, 19), "white"))
        _require(ai_mask.size == (17, 19) and ai_mask.getextrema()[0] > 200, "synthetic ONNX inference")
        report["synthetic_onnx_provider"] = list(segmenter.last_metrics.providers)
        _require(_sha(original) == original_hash, "original image is unchanged")
        report["original_unchanged"] = True
    report["seconds"] = round(time.perf_counter() - started, 3)
    return report


if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from eng_viewer.core.netguard import install
    install()
    application = QApplication([])
    print(json.dumps(run_editor_smoke(), indent=2))
