"""Explicitly installed, entirely local ONNX foreground-mask inference.

No model or runtime is imported at application startup, and no download API exists
here. A model pack is a directory with a versioned manifest, a self-contained ONNX
file and its weight-license text. See docs/image-ai-model-packs.md for the contract.
The output is a selection/mask, never a replacement for the original pixels.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Callable

from PIL import Image

from .raster_limits import check_pixels

_MAX_MANIFEST = 64 * 1024
_MAX_MODEL = 2 * 1024**3
_MAX_LICENSE = 1024 * 1024
_HEX = re.compile(r"[a-fA-F0-9]{64}\Z")
Progress = Callable[[int, str], None]


class ModelPackError(ValueError):
    """The installed pack is incomplete, corrupt or incompatible."""


class InferenceUnavailable(RuntimeError):
    """A local runtime or provider could not be loaded."""


class InferenceCancelled(RuntimeError):
    """The caller cancelled before a result was committed."""


@dataclass(frozen=True)
class ModelPack:
    directory: Path
    identifier: str
    name: str
    model_file: str
    sha256: str
    license_file: str
    license_sha256: str
    license_spdx: str
    input_name: str
    output_name: str
    width: int
    height: int
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    output_kind: str
    resample: str = "lanczos"


@dataclass(frozen=True)
class InferenceMetrics:
    load_seconds: float
    inference_seconds: float
    total_seconds: float
    providers: tuple[str, ...]
    fallback_reason: str | None


def _text(value, label: str, *, maximum=160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ModelPackError(f"Invalid {label} in model manifest.")
    if any(ord(c) < 32 for c in value):
        raise ModelPackError(f"Invalid {label} in model manifest.")
    return value


def _local_file(directory: Path, value, maximum: int) -> Path:
    name = _text(value, "local filename")
    # Flat packages cannot make the inference engine read external directories.
    if "/" in name or "\\" in name or name in {".", ".."} or ":" in name:
        raise ModelPackError("Model-pack files must be plain local filenames.")
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise ModelPackError(f"Missing or unsafe model-pack file: {name}")
    if not 0 < path.stat().st_size <= maximum:
        raise ModelPackError(f"Model-pack file has an unsupported size: {name}")
    return path


def _digest(value) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ModelPackError("Each model-pack file needs a SHA-256 checksum.")
    return value.lower()


def _normalization(value, label, *, positive=False):
    if not isinstance(value, list) or len(value) != 3:
        raise ModelPackError(f"{label} must contain three numbers.")
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or abs(v) > 10 or (positive and v <= 0)
           for v in value):
        raise ModelPackError(f"Invalid {label} values.")
    return tuple(float(v) for v in value)


def read_model_pack(directory: str | Path) -> ModelPack:
    """Read metadata without initializing ML or hashing a heavyweight model."""
    directory = Path(directory).expanduser().resolve(strict=True)
    manifest = _local_file(directory, "manifest.json", _MAX_MANIFEST)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format") != "eng-image-ai":
            raise ModelPackError("Not an ENG offline image model pack.")
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ModelPackError("Unsupported offline model-pack version.")
        if data.get("family") != "birefnet":
            raise ModelPackError("This version supports BiRefNet mask models only.")
        model, license_info = data["model"], data["license"]
        inp, out = data["input"], data["output"]
        model_path = _local_file(directory, model["file"], _MAX_MODEL)
        license_path = _local_file(directory, license_info["file"], _MAX_LICENSE)
        if model_path.suffix.lower() != ".onnx":
            raise ModelPackError("The local model must be an ONNX file.")
        if inp.get("layout") != "NCHW" or inp.get("color") != "RGB":
            raise ModelPackError("Model input must be RGB in NCHW layout.")
        if out.get("layout") != "NCHW" or out.get("kind") not in {"logits", "alpha"}:
            raise ModelPackError("Model output must be NCHW logits or alpha.")
        size = inp["size"]
        if (not isinstance(size, list) or len(size) != 2
                or any(type(v) is not int or not 32 <= v <= 2048 for v in size)):
            raise ModelPackError("Model input size must be [width, height], 32–2048 pixels.")
        identifier = _text(data["id"], "model id", maximum=80)
        resample = inp.get("resample", "lanczos")
        if resample not in {"lanczos", "bilinear", "bicubic"}:
            raise ModelPackError("Unsupported image resampling method.")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", identifier):
            raise ModelPackError("Model id must contain letters, digits, dots, underscores or hyphens.")
        return ModelPack(directory, identifier, _text(data["name"], "model name"),
                         model_path.name, _digest(model["sha256"]), license_path.name,
                         _digest(license_info["sha256"]),
                         _text(license_info["spdx"], "weight license"),
                         _text(inp["name"], "input name"), _text(out["name"], "output name"),
                         size[0], size[1], _normalization(inp["mean"], "mean"),
                         _normalization(inp["std"], "standard deviation", positive=True),
                         out["kind"], resample)
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelPackError("The offline model manifest is malformed or incomplete.") from exc


def discover_models(root: str | Path) -> list[ModelPack]:
    """List valid pack metadata in root and its immediate child directories.

    Incomplete packs are omitted. Use read_model_pack for a specific error when
    the user explicitly chooses one. No recursive scanning, network or ML load.
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    candidates = [root] + sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    packs = []
    for directory in candidates:
        if not (directory / "manifest.json").exists():
            continue
        try:
            packs.append(read_model_pack(directory))
        except (OSError, ModelPackError):
            continue
    return packs


def _verify_file(path: Path, expected: str, cancelled) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            _check_cancelled(cancelled)
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ModelPackError(f"Checksum failed for {path.name}; reinstall the offline model pack.")


def _check_cancelled(cancelled) -> None:
    if cancelled is not None and cancelled():
        raise InferenceCancelled("Image AI operation cancelled.")


def _validate_graph(onnx, path: Path) -> None:
    """Disallow external tensors and custom ops before ORT opens model data."""
    try:
        model = onnx.load_model(str(path), load_external_data=False)
        if model.training_info or model.functions:
            raise ModelPackError("Training graphs and local functions are not supported in model packs.")
        def validate_graph(graph):
            def tensor(t):
                if t.external_data or t.data_location == onnx.TensorProto.EXTERNAL:
                    raise ModelPackError("External ONNX tensors are not allowed; use a self-contained model.")
            for t in graph.initializer:
                tensor(t)
            for sparse in graph.sparse_initializer:
                tensor(sparse.values)
                tensor(sparse.indices)
            for node in graph.node:
                if node.domain not in {"", "ai.onnx"}:
                    raise ModelPackError("Custom ONNX operator domains are not supported.")
                for attr in node.attribute:
                    if attr.HasField("t"):
                        tensor(attr.t)
                    for t in attr.tensors:
                        tensor(t)
                    if attr.HasField("sparse_tensor"):
                        tensor(attr.sparse_tensor.values)
                        tensor(attr.sparse_tensor.indices)
                    for s in attr.sparse_tensors:
                        tensor(s.values)
                        tensor(s.indices)
                    if attr.HasField("g"):
                        validate_graph(attr.g)
                    for graph_child in attr.graphs:
                        validate_graph(graph_child)
        validate_graph(model.graph)
        onnx.checker.check_model(model)
    except ModelPackError:
        raise
    except Exception as exc:
        raise ModelPackError("The ONNX model failed local structural validation.") from exc


class OfflineSegmenter:
    """Lazy session for a verified local BiRefNet model; call from a worker.

    CPU is the conservative default. Explicit acceleration requests fall back to
    CPU on initialization failure and record that fact in last_metrics. Session
    execution is serialized, including for DirectML. Cancellation is checked
    before/after inference; it cannot interrupt an in-progress native Run call.
    """

    def __init__(self, pack: ModelPack, *, provider="cpu"):
        if provider not in {"cpu", "auto", "coreml", "directml"}:
            raise ValueError("Unknown inference provider preference.")
        self.pack = pack
        self.provider = provider
        self._session = None
        self._lock = threading.Lock()
        self.last_metrics: InferenceMetrics | None = None
        self.fallback_reason: str | None = None

    def _load(self, cancelled=None):
        if self._session is not None:
            return self._session
        try:
            ort = importlib.import_module("onnxruntime")
            onnx = importlib.import_module("onnx")
        except ImportError as exc:
            raise InferenceUnavailable(
                "Offline AI runtime is not installed. Install an ENG offline AI runtime package; "
                "ordinary image editing remains available.") from exc
        # Explicitly disable native-runtime telemetry as well as using no network APIs.
        ort.disable_telemetry_events()
        pack = read_model_pack(self.pack.directory)
        if pack != self.pack:
            raise ModelPackError("The model manifest changed; select the model pack again.")
        path = _local_file(pack.directory, pack.model_file, _MAX_MODEL)
        _verify_file(path, pack.sha256, cancelled)
        _verify_file(_local_file(pack.directory, pack.license_file, _MAX_LICENSE),
                     pack.license_sha256, cancelled)
        _validate_graph(onnx, path)
        _check_cancelled(cancelled)
        available = set(ort.get_available_providers())
        candidates = {"coreml": "CoreMLExecutionProvider", "directml": "DmlExecutionProvider"}
        requested = []
        if self.provider == "auto":
            requested = [p for p in candidates.values() if p in available][:1]
        elif self.provider != "cpu":
            accelerator = candidates[self.provider]
            if accelerator in available:
                requested = [accelerator]
            else:
                self.fallback_reason = f"{accelerator} is unavailable; using CPU."
        providers = requested + ["CPUExecutionProvider"]
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.enable_mem_pattern = False
        options.log_severity_level = 3
        try:
            session = ort.InferenceSession(str(path), sess_options=options, providers=providers)
        except Exception as exc:
            if not requested:
                raise InferenceUnavailable("The installed model could not initialize on the CPU.") from exc
            self.fallback_reason = "The requested accelerator could not initialize; using CPU."
            try:
                session = ort.InferenceSession(str(path), sess_options=options,
                                               providers=["CPUExecutionProvider"])
            except Exception as cpu_exc:
                raise InferenceUnavailable("The installed model could not initialize on the CPU.") from cpu_exc
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if (len(inputs) != 1 or inputs[0].name != pack.input_name
                or inputs[0].type != "tensor(float)"
                or pack.output_name not in {o.name for o in outputs}):
            raise ModelPackError("Model input/output names or types do not match the manifest.")
        shape = inputs[0].shape
        expected = (1, 3, pack.height, pack.width)
        if len(shape) != 4 or any(isinstance(got, int) and got != want
                                  for got, want in zip(shape, expected)):
            raise ModelPackError("Model input shape does not match the manifest.")
        if requested and not self.fallback_reason and not any(p in session.get_providers() for p in requested):
            self.fallback_reason = "The runtime fell back to CPU; accelerator is inactive."
        self._session = session
        return session

    def remove_background_mask(self, image: Image.Image, *, progress: Progress | None = None,
                               cancelled: Callable[[], bool] | None = None) -> Image.Image:
        """Return a full-size L foreground mask, preserving the caller's image.

        The mask is independent of any existing image alpha; the layer compositor
        multiplies both. A probability mask is not a guarantee of fine-edge matting.
        """
        check_pixels(*image.size)
        emit = progress or (lambda _percent, _message: None)
        with self._lock:
            start = time.perf_counter()
            _check_cancelled(cancelled)
            emit(0, "Verifying local model")
            load_start = time.perf_counter()
            session = self._load(cancelled)
            load_seconds = time.perf_counter() - load_start
            np = importlib.import_module("numpy")
            _check_cancelled(cancelled)
            emit(35, "Preparing image")
            pack = self.pack
            interpolation = {"lanczos": Image.Resampling.LANCZOS, "bilinear": Image.Resampling.BILINEAR,
                             "bicubic": Image.Resampling.BICUBIC}[pack.resample]
            resized = image.convert("RGB").resize((pack.width, pack.height), interpolation)
            values = np.asarray(resized, dtype=np.float32) / np.float32(255)
            values = (values - np.asarray(pack.mean, dtype=np.float32)) / np.asarray(pack.std, dtype=np.float32)
            tensor = np.ascontiguousarray(values.transpose(2, 0, 1)[None, ...])
            _check_cancelled(cancelled)
            emit(50, "Computing foreground mask locally")
            inference_start = time.perf_counter()
            try:
                output = session.run([pack.output_name], {pack.input_name: tensor})[0]
            except Exception as exc:
                raise InferenceUnavailable("Local inference failed; no image changes were applied.") from exc
            inference_seconds = time.perf_counter() - inference_start
            _check_cancelled(cancelled)
            output = np.asarray(output)
            if output.shape != (1, 1, pack.height, pack.width) or not np.isfinite(output).all():
                raise ModelPackError("Model returned an invalid mask shape or nonfinite values.")
            alpha = output[0, 0].astype(np.float32)
            if pack.output_kind == "logits":
                alpha = 1 / (1 + np.exp(-np.clip(alpha, -80, 80)))
            elif alpha.min() < -0.0001 or alpha.max() > 1.0001:
                raise ModelPackError("Model alpha output is outside the 0–1 range.")
            emit(90, "Restoring mask resolution")
            mask = Image.fromarray(np.rint(np.clip(alpha, 0, 1) * 255).astype(np.uint8))
            mask = mask.resize(image.size, Image.Resampling.LANCZOS)
            _check_cancelled(cancelled)
            self.last_metrics = InferenceMetrics(load_seconds, inference_seconds,
                                                 time.perf_counter() - start,
                                                 tuple(session.get_providers()), self.fallback_reason)
            emit(100, "Foreground mask ready")
            return mask
