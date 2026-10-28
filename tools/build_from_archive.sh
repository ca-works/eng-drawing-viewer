#!/usr/bin/env bash
# Build the Mac app from a published source archive, exactly as a release is built.
#
#   tools/build_from_archive.sh eng-drawing-viewer-<version>-source.zip [work-folder]
#
# Unpacks the archive into a fresh folder (default /tmp/eng-drawing-viewer-build/<n>), runs the
# tests, builds with packaging/macos/build_macos.sh and checks the result with tools/check_bundle.py.
# Builds made this way from the same archive contain the same files with the same contents, apart
# from code signatures and the disk image's own metadata.
set -euo pipefail

ARCHIVE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
BASE="/tmp/eng-drawing-viewer-build"
if [[ $# -ge 2 ]]; then
  WORK="$2"
else
  n=1; while [[ -e "$BASE/$n" ]]; do n=$((n + 1)); done
  WORK="$BASE/$n"
fi
mkdir -p "$WORK"
cd "$WORK"
unzip -q "$ARCHIVE"
SRC="$(find . -maxdepth 1 -type d -name 'eng-drawing-viewer-*' | head -1)"
[[ -n "$SRC" ]] || { echo "The archive has no eng-drawing-viewer-<version> folder" >&2; exit 1; }
cd "$SRC"
echo "==> Building from $ARCHIVE in $PWD"
shasum -a 256 -c SOURCE_MANIFEST.txt --quiet && echo "    every file matches SOURCE_MANIFEST.txt"

# Archive timestamps are fixed (1980-01-01), so builds don't depend on when they ran.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-315532800}"
export BUILD_VENV="$PWD/.venv-build"
packaging/macos/build_macos.sh

echo "==> Tests"
# Test the unpacked source itself (src/), which also carries the licence files About shows.
PYTHONPATH="$PWD/src" QT_QPA_PLATFORM=offscreen "$BUILD_VENV/bin/python" -m pytest -q -p no:cacheprovider

echo "==> Checking the built app"
"$BUILD_VENV/bin/python" tools/check_bundle.py "release/macos/ENG Drawing Viewer.app" \
  --json release/macos/bundle-check.json ${PRIVATE_TERMS:+--private-terms "$PRIVATE_TERMS"}
echo "==> Built in $PWD/release/macos"
