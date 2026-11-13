#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Prepare the inputs of the macOS Universal edition: the universal2 interpreter and the universal2
# wheelhouse (all packages of packaging/requirements-macos.txt except OpenCV, which
# build_universal_opencv.sh builds). Runs on Linux (with llvm-lipo) or macOS; needs network access.
#
#   packaging/macos/prepare_universal.sh OUT_DIR
#
# OUT_DIR/python/        the interpreter (bin/python3.13 holds arm64 and x86_64 code)
# OUT_DIR/thin/          the two original interpreter archives (for the per-architecture OpenCV builds)
# OUT_DIR/wheelhouse/    universal2 and pure-Python wheels, with wheelhouse-report.json
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
PY="${PYTHON:-python3}"
mkdir -p "$OUT/thin"
sum() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
while read -r arch sha url; do
  [[ -z "$arch" || "$arch" == \#* ]] && continue
  file="$OUT/thin/python-$arch.tar.gz"
  [[ -f "$file" && "$(sum "$file")" == "$sha" ]] || curl -fsSL --retry 3 -o "$file" "$url"
  [[ "$(sum "$file")" == "$sha" ]] || { echo "python-$arch.tar.gz: SHA-256 mismatch" >&2; exit 1; }
  echo "    python-$arch.tar.gz verified"
done < "$ROOT/packaging/macos/universal-python.txt"
rm -rf "$OUT/python" "$OUT/fused"
"$PY" "$ROOT/tools/fuse_macos.py" python "$OUT/thin/python-arm64.tar.gz" "$OUT/thin/python-x86_64.tar.gz" "$OUT/fused"
mv "$OUT/fused/python" "$OUT/python" && mv "$OUT/fused/fuse-python-report.json" "$OUT/" && rmdir "$OUT/fused"
"$PY" "$ROOT/tools/fuse_macos.py" wheels "$ROOT/packaging/requirements-macos.txt" "$OUT/wheelhouse" \
  --cache "$OUT/downloads" --skip opencv-python-headless
rm -rf "$OUT/downloads"
