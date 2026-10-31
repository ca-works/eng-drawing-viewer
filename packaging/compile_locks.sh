#!/usr/bin/env bash
# Resolution only; run on a development machine with network access.
set -euo pipefail
cd "$(dirname "$0")/.."
export MACOSX_DEPLOYMENT_TARGET=14.0
uv pip compile packaging/requirements-macos.in --python-version 3.13 \
  --python-platform x86_64-apple-darwin --generate-hashes --no-header \
  -o packaging/requirements-macos-intel.txt
uv pip compile packaging/requirements-macos.in --python-version 3.13 \
  --python-platform aarch64-apple-darwin --generate-hashes --no-header \
  -o packaging/requirements-macos.txt
uv pip compile packaging/requirements-windows.in --python-version 3.13 \
  --python-platform x86_64-pc-windows-msvc --generate-hashes --no-header \
  -o packaging/requirements-windows.txt
uv pip compile packaging/requirements-linux.in --python-version 3.13 \
  --python-platform x86_64-manylinux_2_34 --generate-hashes --no-header \
  -o packaging/requirements-linux.txt
uv pip compile packaging/requirements-opencv-build.in --python-version 3.13 --universal \
  --generate-hashes --no-header -o packaging/requirements-opencv-build.txt
