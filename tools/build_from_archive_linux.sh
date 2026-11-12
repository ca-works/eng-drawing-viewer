#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the Linux AppImage from a published source archive, exactly as a release is built.
#
#   OPENCV_WHEEL=/path/opencv_python_headless-….whl tools/build_from_archive_linux.sh \
#       eng-drawing-viewer-<version>-source.zip [work-folder]
#
# Run it on Ubuntu 22.04 (glibc 2.35; see docs/BUILDING.md). OPENCV_WHEEL is the minimal OpenCV
# made by tools/build_opencv_minimal.sh from the same archive's packaging/requirements-linux.txt.
# Unpacks the archive into a fresh folder (default /tmp/eng-drawing-viewer-build/linux-<n>), checks
# every file against SOURCE_MANIFEST.txt, builds with packaging/linux/build_linux.sh (tests included)
# and checks the result with tools/check_bundle.py.
set -euo pipefail

ARCHIVE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
BASE="/tmp/eng-drawing-viewer-build"
if [[ $# -ge 2 ]]; then
  WORK="$2"
else
  n=1; while [[ -e "$BASE/linux-$n" ]]; do n=$((n + 1)); done
  WORK="$BASE/linux-$n"
fi
[[ -n "${OPENCV_WHEEL:-}" ]] || { echo "Set OPENCV_WHEEL (tools/build_opencv_minimal.sh)." >&2; exit 1; }
mkdir -p "$WORK"
cd "$WORK"
unzip -q "$ARCHIVE"
SRC="$(find . -maxdepth 1 -type d -name 'eng-drawing-viewer-*' | head -1)"
[[ -n "$SRC" ]] || { echo "The archive has no eng-drawing-viewer-<version> folder" >&2; exit 1; }
cd "$SRC"
echo "==> Building from $ARCHIVE in $PWD"
sha256sum -c SOURCE_MANIFEST.txt --quiet && echo "    every file matches SOURCE_MANIFEST.txt"

# Archive timestamps are fixed (1980-01-01), so builds don't depend on when they ran.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-315532800}"
export BUILD_VENV="$PWD/.venv-build-linux"
export RUN_TESTS="${RUN_TESTS:-1}" ENG_REQUIRE_MINIMAL_OPENCV=1
packaging/linux/build_linux.sh

echo "==> Checking the built AppDir"
"$BUILD_VENV/bin/python" tools/check_bundle.py build/linux/AppDir \
  --json release/linux/bundle-check.json --reviewed packaging/reviewed-upstream-findings.txt \
  ${PRIVATE_TERMS:+--private-terms "$PRIVATE_TERMS"}
echo "==> Built in $PWD/release/linux"
