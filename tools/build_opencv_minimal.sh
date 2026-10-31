#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build a minimal OpenCV wheel (core, imgproc, photo and the Python bindings) from the official
# opencv-python-headless source release pinned in a lock file.
#
#   tools/build_opencv_minimal.sh PYTHON LOCK OUT_DIR
#
#   PYTHON  the interpreter to build for (its architecture is the wheel's architecture; on an Apple
#           Silicon Mac run an x86_64 interpreter under Rosetta to build the Intel wheel, and set
#           OPENCV_OSX_ARCH=x86_64 so the compiler targets it explicitly)
#   LOCK    the edition's lock (packaging/requirements-macos.txt): gives the OpenCV and numpy
#           versions and the SHA-256 of the source release
#
# Those three modules are all the app uses (cv2 calls in src/ and tools/). Leaving everything else
# out means no FFmpeg, OpenSSL, libssh, ZeroMQ or image codecs inside OpenCV, and two builds from the
# same source and options give identical file sets, which tools/fuse_macos.py can fuse into a
# genuine universal2 wheel. The published macOS wheels cannot be fused: the arm64 and x86_64 wheels
# bundle different sets of third-party libraries.
set -euo pipefail

PYTHON="$1"; LOCK="$2"; OUT="$(mkdir -p "$3" && cd "$3" && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

read -r VERSION SDIST_SHA URL < <("$PYTHON" - "$LOCK" <<'PYEOF'
import json, re, sys, urllib.request
lock = open(sys.argv[1], encoding="utf-8").read()
block = re.search(r"^opencv-python-headless==(\S+)(.*?)(?=^\S)", lock + "\nend", re.S | re.M)
version, hashes = block.group(1), set(re.findall(r"sha256:([0-9a-f]{64})", block.group(2)))
meta = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/opencv-python-headless/{version}/json", timeout=60))
sdist = [f for f in meta["urls"] if f["packagetype"] == "sdist"][0]
if sdist["digests"]["sha256"] not in hashes:
    raise SystemExit("the source release's SHA-256 is not in the lock")
print(version, sdist["digests"]["sha256"], sdist["url"])
PYEOF
)
echo "==> OpenCV $VERSION source (SHA-256 $SDIST_SHA)"
curl -fsSL --retry 3 -o "$WORK/src.tar.gz" "$URL"
echo "$SDIST_SHA  $WORK/src.tar.gz" | shasum -a 256 -c - >/dev/null
tar xzf "$WORK/src.tar.gz" -C "$WORK"
# A build of a few modules can't generate the typing stubs (for editors and type checkers only); the
# wheel recipe would stop on the missing files.
patch -s -p1 -d "$WORK/opencv_python_headless-$VERSION" < "$ROOT/packaging/opencv/optional-typing-stubs.patch"

echo "==> Build environment for $("$PYTHON" -c 'import platform, sys; print(platform.machine(), sys.version.split()[0])')"
"$PYTHON" -m venv "$WORK/venv"
VPY="$WORK/venv/bin/python"
"$PYTHON" - "$LOCK" "$WORK/numpy.txt" <<'PYEOF'
import re, sys
lock = open(sys.argv[1], encoding="utf-8").read()
block = re.search(r"^numpy==.*?(?=^\S)", lock + "\nend", re.S | re.M).group(0)
open(sys.argv[2], "w", encoding="utf-8").write(block)
PYEOF
"$VPY" -m pip install --quiet --no-deps --require-hashes -r "$ROOT/packaging/requirements-opencv-build.txt"
"$VPY" -m pip install --quiet --no-deps --require-hashes -r "$WORK/numpy.txt"

echo "==> Building (core, imgproc, photo)"
export ENABLE_HEADLESS=1
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-$(getconf _NPROCESSORS_ONLN)}"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}"
# Nothing is downloaded during the build (IPP and ITT fetch binaries) and no optional backend is used.
# Java/JNI detection is off: OpenCV records what CMake finds in its build information string, so a
# JDK in the builder's home folder would put that folder's path into cv2.
export CMAKE_ARGS="-DBUILD_LIST=core,imgproc,photo,python3 -DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF \
-DWITH_IPP=OFF -DWITH_ITT=OFF -DWITH_OPENCL=OFF -DWITH_OPENEXR=OFF -DBUILD_OPENEXR=OFF -DWITH_ADE=OFF \
-DWITH_PROTOBUF=OFF -DWITH_LAPACK=OFF -DWITH_EIGEN=OFF -DWITH_OPENJPEG=OFF -DWITH_JASPER=OFF \
-DWITH_TIFF=OFF -DWITH_WEBP=OFF -DWITH_PNG=OFF -DWITH_JPEG=OFF -DWITH_AVIF=OFF -DWITH_IMGCODEC_HDR=OFF \
-DWITH_V4L=OFF -DWITH_1394=OFF -DWITH_VTK=OFF -DOPENCV_GENERATE_PKGCONFIG=OFF \
-DBUILD_JAVA=OFF -DBUILD_opencv_java_bindings_generator=OFF -DCMAKE_DISABLE_FIND_PACKAGE_JNI=ON \
-DCMAKE_DISABLE_FIND_PACKAGE_Java=ON -DCMAKE_DISABLE_FIND_PACKAGE_Ant=ON \
-DCMAKE_OSX_DEPLOYMENT_TARGET=$MACOSX_DEPLOYMENT_TARGET${OPENCV_OSX_ARCH:+ -DCMAKE_OSX_ARCHITECTURES=$OPENCV_OSX_ARCH}"
(cd "$WORK/opencv_python_headless-$VERSION" && "$VPY" -m pip wheel . --no-build-isolation --no-deps -w "$OUT")
"$VPY" - "$OUT" <<'PYEOF'
import re, sys, zipfile, pathlib
# No home-folder path of the build machine may end up in the wheel (build information, debug data).
home = re.compile(rb"/(?:Users|home)/(?!runner/)[A-Za-z0-9_.-]+/")
for whl in pathlib.Path(sys.argv[1]).glob("opencv_python_headless-*.whl"):
    z = zipfile.ZipFile(whl)
    names = z.namelist()
    bad = [n for n in names if any(k in n.lower() for k in ("avformat", "avcodec", "libssl", "libcrypto", "libssh"))]
    bad += [f"{n}: {m.group().decode()}" for n in names for m in [home.search(z.read(n))] if m]
    print(f"    {whl.name}: {len(names)} files" + (f"; UNEXPECTED: {bad}" if bad else ""))
    if bad:
        raise SystemExit(1)
PYEOF
