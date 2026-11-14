#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the minimal OpenCV wheel for the Linux edition (x86-64), on the release build system
# (Ubuntu 22.04), with the same recipe and options as every other edition.
#
#   packaging/linux/build_opencv_linux.sh PYTHON OUT_DIR
#
#   PYTHON   CPython 3.13.16 (the interpreter the app ships, e.g. from `uv python find 3.13.16`)
#   OUT_DIR  where the wheel goes; pass it to build_linux.sh as OPENCV_WHEEL
#
# Runs tools/build_opencv_minimal.sh with packaging/requirements-linux.txt. The only Linux addition
# is packaging/opencv/glibc-2.34-compat.h (force-included through CFLAGS/CXXFLAGS, which CMake
# reads): it keeps hypotf and std::condition_variable::wait at their original symbol
# versions, so the wheel loads on glibc 2.34 with GCC 11's libstdc++ (RHEL/Rocky 9).
# Afterwards the wheel is checked: it may need nothing newer than glibc 2.34 / GLIBCXX 3.4.29, may
# link only the C/C++ runtime and may have no RPATH/RUNPATH.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$1"; OUT="$(mkdir -p "$2" && cd "$2" && pwd)"
[[ "$(uname -m)" == x86_64 ]] || { echo "Build the Linux OpenCV on x86-64." >&2; exit 1; }
# A fixed, neutral path: CMake records the compiler flags (and so this path) in cv2's build information.
COMPAT_DIR=/tmp/eng-drawing-viewer-build/opencv-compat
mkdir -p "$COMPAT_DIR"
install -m 644 "$ROOT/packaging/opencv/glibc-2.34-compat.h" "$COMPAT_DIR/glibc-2.34-compat.h"
export CFLAGS="-include $COMPAT_DIR/glibc-2.34-compat.h" CXXFLAGS="-include $COMPAT_DIR/glibc-2.34-compat.h"
# No RPATH/RUNPATH: CMake would record the temporary build tree's lib folder in cv2, a builder path
# and a place another user could plant libraries. cv2 needs only the system C/C++ runtime.
export OPENCV_EXTRA_CMAKE_ARGS="-DCMAKE_SKIP_RPATH=ON -DCMAKE_SKIP_INSTALL_RPATH=ON"
"$ROOT/tools/build_opencv_minimal.sh" "$PYTHON" "$ROOT/packaging/requirements-linux.txt" "$OUT"

WHL="$(ls "$OUT"/opencv_python_headless-*-linux_x86_64.whl | head -1)"
CHECK="$(mktemp -d)"; trap 'rm -rf "$CHECK"' EXIT
(cd "$CHECK" && unzip -q "$WHL")
fail=0
for so in $(find "$CHECK" -name '*.so*' -type f); do
  needed="$(readelf -d "$so" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p' | tr '\n' ' ')"
  glibc="$(objdump -T "$so" | grep -oE 'GLIBC_[0-9]+\.[0-9]+(\.[0-9]+)?' | sed 's/GLIBC_//' | sort -Vu | tail -1)"
  glibcxx="$(objdump -T "$so" | grep -oE 'GLIBCXX_[0-9.]+' | sed 's/GLIBCXX_//' | sort -Vu | tail -1)"
  echo "    ${so#$CHECK/}: needs glibc $glibc, GLIBCXX ${glibcxx:-none}; links: $needed"
  [[ "$(printf '%s\n2.34\n' "$glibc" | sort -V | tail -1)" == 2.34 ]] || { echo "    newer than glibc 2.34" >&2; fail=1; }
  [[ -z "$glibcxx" || "$(printf '%s\n3.4.29\n' "$glibcxx" | sort -V | tail -1)" == 3.4.29 ]] || { echo "    newer than GLIBCXX 3.4.29" >&2; fail=1; }
  if readelf -d "$so" | grep -qE '\((RPATH|RUNPATH)\)'; then
    echo "    has an RPATH/RUNPATH: $(readelf -d "$so" | grep -E 'RPATH|RUNPATH')" >&2; fail=1
  fi
  for lib in $needed; do
    case "$lib" in libstdc++.so.6|libm.so.6|libgcc_s.so.1|libc.so.6|ld-linux-x86-64.so.2|libpthread.so.0|libdl.so.2|librt.so.1) ;;
      *) echo "    unexpected library dependency: $lib" >&2; fail=1 ;;
    esac
  done
done
exit "$fail"
