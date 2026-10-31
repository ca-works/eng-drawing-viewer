#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the minimal OpenCV for both Mac architectures from the same source and options, then fuse the
# two wheels into one universal2 wheel in the wheelhouse. Runs on an Apple Silicon Mac with Rosetta 2:
# the x86_64 build runs the original x86_64 interpreter, which macOS translates.
#
#   packaging/macos/build_universal_opencv.sh PREPARED_DIR      (the folder prepare_universal.sh made)
#
# The two single-architecture wheels are kept in PREPARED_DIR/opencv-thin/<arch>/: the Apple Silicon
# edition ships the arm64 one (OPENCV_WHEEL for build_macos.sh), so no edition bundles FFmpeg/OpenSSL.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIR="$(cd "$1" && pwd)"
[[ "$(uname -m)" == arm64 ]] || { echo "Run this on an Apple Silicon Mac." >&2; exit 1; }
arch -x86_64 /usr/bin/true 2>/dev/null || { echo "Rosetta 2 is required." >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
for a in arm64 x86_64; do
  mkdir -p "$WORK/$a" && tar xzf "$DIR/thin/python-$a.tar.gz" -C "$WORK/$a"
  echo "==> OpenCV for $a"
  OPENCV_OSX_ARCH="$a" arch "-$a" "$ROOT/tools/build_opencv_minimal.sh" "$WORK/$a/python/bin/python3.13" \
    "$ROOT/packaging/requirements-macos.txt" "$WORK/wheels-$a"
done
for a in arm64 x86_64; do
  rm -rf "$DIR/opencv-thin/$a" && mkdir -p "$DIR/opencv-thin/$a" && cp "$WORK/wheels-$a"/opencv_python_headless-*.whl "$DIR/opencv-thin/$a/"
done
"${PYTHON:-python3}" "$ROOT/tools/fuse_macos.py" pair "$WORK"/wheels-arm64/opencv_python_headless-*.whl \
  "$WORK"/wheels-x86_64/opencv_python_headless-*.whl "$DIR/wheelhouse"
