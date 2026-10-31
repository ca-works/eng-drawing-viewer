# Offline image AI model packs

The Image Editor has an optional, lazy ONNX adapter for BiRefNet-style foreground masks. **No trained model is included, and no learned segmentation quality or device performance has been verified.** Ordinary image editing needs neither ONNX Runtime nor a model. The adapter has no download or cloud fallback. Selecting a missing runtime or incompatible model returns an explicit error before any image changes.

## Package contract (version 1)

A pack is a flat local directory containing `manifest.json`, one self-contained ONNX file, and the model weights' license text. Model and license SHA-256 values are mandatory and checked at first load. Symbolic-link files, traversal filenames, external ONNX tensor files, custom operator domains, training graphs and local ONNX functions are rejected. This is structural validation, not certification that an arbitrary third-party neural network is safe. Distribute only reviewed packs from known provenance.

```json
{
  "format": "eng-image-ai",
  "version": 1,
  "id": "birefnet-verified-version",
  "name": "BiRefNet foreground mask",
  "family": "birefnet",
  "model": {"file": "model.onnx", "sha256": "REPLACE_WITH_64_HEX_DIGITS"},
  "license": {
    "spdx": "MIT",
    "file": "LICENSE.txt",
    "sha256": "REPLACE_WITH_64_HEX_DIGITS"
  },
  "input": {
    "name": "input_image",
    "layout": "NCHW",
    "color": "RGB",
    "size": [1024, 1024],
    "mean": [0.485, 0.456, 0.406],
    "std": [0.229, 0.224, 0.225]
  },
  "output": {"name": "output", "layout": "NCHW", "kind": "logits"}
}
```

This example is documentation, not an installable or approved model. Verify names, preprocessing and output semantics against the exact exported graph. Input is float32 RGB, resized with Lanczos to `[width, height]`, divided by 255, normalized channel-by-channel, then changed to `1×3×height×width`. `size` supports 32–2048 in each dimension. Output must be finite `1×1×height×width`. `kind: logits` applies sigmoid; `kind: alpha` expects values in 0–1. The independent foreground mask is resized to original dimensions and returned as Pillow `L`. The layer compositor must multiply it by original image alpha. No source pixels are altered.

Keep a provenance document beside the pack with original checkpoint URL/revision/hash, code license, weight license, conversion code/revision, ONNX opset, tested runtime versions, output-parity results and the applicable notices. The application does not follow provenance URLs. Do not infer a model's weight license solely from the inference library's license.

## Python API

```python
from eng_viewer.core.image_ai import read_model_pack, OfflineSegmenter

pack = read_model_pack(local_directory)
segmenter = OfflineSegmenter(pack, provider="cpu")
# Run in a background worker. Callbacks execute on that worker.
mask = segmenter.remove_background_mask(
    image,
    progress=lambda percent, message: worker_progress.emit(percent, message),
    cancelled=lambda: cancellation_requested,
)
metrics = segmenter.last_metrics
```

`discover_models(root)` reads valid metadata in `root` and its immediate children without loading ML. Runtime dependencies are `numpy`, `onnx`, and `onnxruntime`, imported on first use. Package and pin these for each target platform separately. No runtime installation happens from within the app.

CPU is the default. `coreml`, `directml`, and `auto` request available accelerators, with CPU fallback recorded in `last_metrics.fallback_reason`. `last_metrics` also records load/inference/total time and registered session providers. Registered providers do not prove every operator ran on the GPU; graph profiling is needed before making acceleration claims. DirectML sessions use sequential execution and disabled memory patterns. All calls on an instance are serialized. ONNX Runtime telemetry is explicitly disabled.

Cancellation is checked during checksum verification and before/after native inference. It does not currently stop an active native inference call; discard the result when cancellation arrives. Model initialization belongs after an explicit AI action, never in ordinary startup. A session stays cached for the lifetime of its `OfflineSegmenter` instance; release the instance when a model is unloaded.

## Verification status

Unit tests cover pack discovery, traversal/symlink rejection, altered model/license hashes, input normalization, mask shape/range validation, cancellation, session reuse, provider fallback and lazy imports. The optional real-runtime test uses a tiny generated ReduceMean graph solely to verify the ONNX contract and rejection of external tensors. It does **not** measure background-removal accuracy, alpha-matting quality, hair extraction, or real-model performance.

Before releasing a trained pack, benchmark redistributable reference images on Intel macOS, Apple Silicon macOS and Windows, including CPU fallback, cold/warm latency, peak memory, fine-edge and alpha error, and input/output parity with the original checkpoint. Model licensing, those benchmarks and an offline inference UI test are release gates.
