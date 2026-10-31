"""Offline-pack validation and inference contract tests; no learned weights."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from eng_viewer.core import image_ai as ai


def make_pack(tmp_path, **changes):
    (tmp_path / "model.onnx").write_bytes(b"synthetic-test-model")
    (tmp_path / "LICENSE.txt").write_text("Synthetic test fixture only. No trained model.")
    data = {
        "format": "eng-image-ai", "version": 1, "family": "birefnet",
        "id": "synthetic-test", "name": "Synthetic test (not AI)",
        "model": {"file": "model.onnx", "sha256": digest(tmp_path / "model.onnx")},
        "license": {"spdx": "CC0-1.0", "file": "LICENSE.txt", "sha256": digest(tmp_path / "LICENSE.txt")},
        "input": {"name": "input", "layout": "NCHW", "color": "RGB", "size": [32, 32],
                  "mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225]},
        "output": {"name": "output", "layout": "NCHW", "kind": "logits"},
    }
    data.update(changes)
    (tmp_path / "manifest.json").write_text(json.dumps(data))
    return ai.read_model_pack(tmp_path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def fake_runtime(monkeypatch):
    state = SimpleNamespace(constructs=0, runs=0, providers=["CPUExecutionProvider"],
                            output=np.zeros((1, 1, 32, 32), dtype=np.float32), feed=None,
                            fail_accelerator=False, telemetry_disabled=False)
    class Session:
        def __init__(self, path, *, sess_options, providers):
            state.constructs += 1
            assert Path(path).is_file()
            assert sess_options.enable_mem_pattern is False
            assert sess_options.execution_mode == "sequential"
            if state.fail_accelerator and providers[0] != "CPUExecutionProvider":
                raise RuntimeError("Synthetic accelerator failure")
            self.providers = providers
        def get_inputs(self):
            return [SimpleNamespace(name="input", type="tensor(float)", shape=[1, 3, 32, 32])]
        def get_outputs(self):
            return [SimpleNamespace(name="output")]
        def get_providers(self):
            return self.providers
        def run(self, names, feed):
            state.runs += 1
            state.feed = feed
            return [state.output]
    runtime = SimpleNamespace(
        SessionOptions=SimpleNamespace, ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=Session, get_available_providers=lambda: state.providers,
        disable_telemetry_events=lambda: setattr(state, "telemetry_disabled", True))
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)
    monkeypatch.setitem(sys.modules, "onnx", SimpleNamespace())
    monkeypatch.setattr(ai, "_validate_graph", lambda *args: None)
    return state


def test_import_does_not_load_ai_runtime():
    result = subprocess.run([sys.executable, "-c",
                             "from eng_viewer.core import image_ai; import sys; "
                             "assert 'onnxruntime' not in sys.modules; assert 'onnx' not in sys.modules; "
                             "assert 'numpy' not in sys.modules"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_discovery_and_invalid_version(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    make_pack(good)
    bad = tmp_path / "bad"
    bad.mkdir()
    with pytest.raises(ai.ModelPackError, match="version"):
        make_pack(bad, version=2)
    assert [p.identifier for p in ai.discover_models(tmp_path)] == ["synthetic-test"]


@pytest.mark.parametrize("filename", ["../escape.onnx", "/tmp/model.onnx", "x\\model.onnx", "https:evil.onnx"])
def test_rejects_path_escape(tmp_path, filename):
    with pytest.raises(ai.ModelPackError, match="filename"):
        make_pack(tmp_path, model={"file": filename, "sha256": "a" * 64})


def test_rejects_symlink(tmp_path):
    pack = make_pack(tmp_path)
    (tmp_path / "model.onnx").rename(tmp_path / "actual.onnx")
    (tmp_path / "model.onnx").symlink_to(tmp_path / "actual.onnx")
    with pytest.raises(ai.ModelPackError, match="unsafe"):
        ai.read_model_pack(pack.directory)


def test_checksum_failure_before_inference(tmp_path, fake_runtime):
    pack = make_pack(tmp_path)
    (tmp_path / "model.onnx").write_bytes(b"tampered")
    with pytest.raises(ai.ModelPackError, match="Checksum"):
        ai.OfflineSegmenter(pack).remove_background_mask(Image.new("RGB", (20, 12)))
    assert fake_runtime.constructs == 0


def test_license_hash_checked(tmp_path, fake_runtime):
    pack = make_pack(tmp_path)
    (tmp_path / "LICENSE.txt").write_text("tampered")
    with pytest.raises(ai.ModelPackError, match="Checksum"):
        ai.OfflineSegmenter(pack).remove_background_mask(Image.new("RGB", (20, 12)))
    assert fake_runtime.constructs == 0


def test_logits_normalization_cached_session_and_original_unchanged(tmp_path, fake_runtime):
    pack = make_pack(tmp_path)
    segmenter = ai.OfflineSegmenter(pack)
    image = Image.new("RGBA", (41, 17), (255, 0, 0, 90))
    before = image.tobytes()
    progress = []
    mask = segmenter.remove_background_mask(image, progress=lambda n, s: progress.append(n))
    segmenter.remove_background_mask(image)
    assert mask.size == image.size and mask.mode == "L" and mask.getextrema() == (128, 128)
    assert image.tobytes() == before
    assert fake_runtime.constructs == 1 and fake_runtime.runs == 2
    assert fake_runtime.telemetry_disabled
    assert progress == sorted(progress) and progress[-1] == 100
    assert fake_runtime.feed["input"].dtype == np.float32
    assert fake_runtime.feed["input"].shape == (1, 3, 32, 32)
    assert fake_runtime.feed["input"][0, 0, 0, 0] == pytest.approx((1 - .485) / .229)
    assert segmenter.last_metrics.providers == ("CPUExecutionProvider",)


def test_accelerator_init_failure_records_cpu_fallback(tmp_path, fake_runtime):
    fake_runtime.providers += ["DmlExecutionProvider"]
    fake_runtime.fail_accelerator = True
    segmenter = ai.OfflineSegmenter(make_pack(tmp_path), provider="directml")
    segmenter.remove_background_mask(Image.new("RGB", (12, 12)))
    assert fake_runtime.constructs == 2
    assert segmenter.last_metrics.providers == ("CPUExecutionProvider",)
    assert "using CPU" in segmenter.last_metrics.fallback_reason


def test_cancel_does_not_run_model(tmp_path, fake_runtime):
    segmenter = ai.OfflineSegmenter(make_pack(tmp_path))
    with pytest.raises(ai.InferenceCancelled):
        segmenter.remove_background_mask(Image.new("RGB", (12, 12)), cancelled=lambda: True)
    assert fake_runtime.runs == 0


@pytest.mark.parametrize("output", [np.zeros((1, 3, 32, 32)), np.full((1, 1, 32, 32), np.nan)])
def test_rejects_invalid_output(tmp_path, fake_runtime, output):
    fake_runtime.output = output
    with pytest.raises(ai.ModelPackError, match="invalid mask"):
        ai.OfflineSegmenter(make_pack(tmp_path)).remove_background_mask(Image.new("RGB", (12, 12)))


def test_alpha_contract_and_range(tmp_path, fake_runtime):
    pack = make_pack(tmp_path, output={"name": "output", "layout": "NCHW", "kind": "alpha"})
    fake_runtime.output.fill(1)
    segmenter = ai.OfflineSegmenter(pack)
    assert segmenter.remove_background_mask(Image.new("RGB", (12, 12))).getextrema() == (255, 255)
    fake_runtime.output.fill(2)
    with pytest.raises(ai.ModelPackError, match="range"):
        segmenter.remove_background_mask(Image.new("RGB", (12, 12)))


def test_missing_runtime_has_actionable_error(tmp_path, monkeypatch):
    pack = make_pack(tmp_path)
    original_import = ai.importlib.import_module
    def no_runtime(name):
        if name == "onnxruntime":
            raise ImportError("not installed")
        return original_import(name)
    monkeypatch.setattr(ai.importlib, "import_module", no_runtime)
    with pytest.raises(ai.InferenceUnavailable, match="runtime is not installed"):
        ai.OfflineSegmenter(pack).remove_background_mask(Image.new("RGB", (12, 12)))


def test_real_synthetic_onnx_inference_and_external_tensor_rejection(tmp_path):
    """Runs only when optional runtimes exist. This is not a segmentation benchmark."""
    onnx = pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    pack = make_pack(tmp_path)
    graph = onnx.helper.make_graph(
        [onnx.helper.make_node("ReduceMean", ["input"], ["output"], axes=[1], keepdims=1)],
        "synthetic-mask-contract",
        [onnx.helper.make_tensor_value_info("input", onnx.TensorProto.FLOAT, [1, 3, 32, 32])],
        [onnx.helper.make_tensor_value_info("output", onnx.TensorProto.FLOAT, [1, 1, 32, 32])])
    model = onnx.helper.make_model(graph, opset_imports=[onnx.helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(tmp_path / "model.onnx"))
    data = json.loads((tmp_path / "manifest.json").read_text())
    data["model"]["sha256"] = digest(tmp_path / "model.onnx")
    (tmp_path / "manifest.json").write_text(json.dumps(data))
    mask = ai.OfflineSegmenter(ai.read_model_pack(tmp_path)).remove_background_mask(Image.new("RGB", (13, 17), "white"))
    assert mask.size == (13, 17) and mask.getextrema()[0] > 200
    tensor = onnx.helper.make_tensor("unsafe", onnx.TensorProto.FLOAT, [1], [1])
    tensor.ClearField("float_data")
    tensor.data_location = onnx.TensorProto.EXTERNAL
    tensor.external_data.add(key="location", value="../secret")
    model.graph.initializer.append(tensor)
    onnx.save(model, str(tmp_path / "external.onnx"))
    with pytest.raises(ai.ModelPackError, match="External ONNX"):
        ai._validate_graph(onnx, tmp_path / "external.onnx")
