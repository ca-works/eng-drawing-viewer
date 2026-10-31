#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build ENG Drawing Viewer for Linux (x86-64): the application folder, an AppDir and the AppImage.
#
#   packaging/linux/build_linux.sh
#
# Uses a separate build environment (.venv-build-linux) with CPython 3.13.16 from uv and the exact,
# hash-checked packages in packaging/requirements-linux.txt. Requires uv, gpgv, mksquashfs
# (squashfs-tools 4.5 or later) and, for the smoke test, xvfb-run (or it uses Qt's offscreen platform).
#
# Release builds must run on the oldest supported glibc (Ubuntu 22.04, glibc 2.35): the libraries
# PyInstaller copies from the build system decide the minimum. A newer build system stops the build
# unless ALLOW_NEWER_GLIBC=1 (for local testing only).
#
# Environment:
#   RUN_TESTS=1                 run the test suite in the build environment first
#   APPIMAGE_RUNTIME=path       use this runtime file instead of downloading the pinned release
#   APPIMAGE_RUNTIME_SHA256=…   require this runtime hash (default: the pinned release's)
#   SKIP_APPIMAGE=1             stop after the AppDir (no runtime needed)
#
# Output in release/linux/:
#   ENG-Drawing-Viewer-<version>-Linux-x86_64.AppImage
#   ENG-Drawing-Viewer-<version>-Linux-x86_64.cdx.json      software bill of materials
#   build-report.json                                       versions, hashes, minimum glibc
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
APP_ID="io.github.ca_works.ENGDrawingViewer"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/eng_viewer/__init__.py)"
[[ -n "$VERSION" ]] || { echo "Could not read the version from src/eng_viewer/__init__.py" >&2; exit 1; }
[[ "$(uname -m)" == "x86_64" ]] || { echo "The Linux edition is built on x86-64." >&2; exit 1; }
BASENAME="ENG-Drawing-Viewer-$VERSION-Linux-x86_64"
OUT="$ROOT/release/linux"
WORK="$ROOT/build/linux"
APPDIR="$WORK/AppDir"
VENV="${BUILD_VENV:-$ROOT/.venv-build-linux}"
PYTHON_VERSION="3.13.16"
TARGET_GLIBC="2.35"
RUNTIME_TAG="20251108"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/$RUNTIME_TAG/runtime-x86_64"
# Verified with gpgv against packaging/linux/appimage-runtime/type2-runtime-signing-key.asc.
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"
step() { printf '==> %s\n' "$*"; }

export PYTHONHASHSEED=0
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}"

host_glibc="$(getconf GNU_LIBC_VERSION | awk '{print $2}')"
if [[ "$(printf '%s\n%s\n' "$TARGET_GLIBC" "$host_glibc" | sort -V | tail -1)" != "$TARGET_GLIBC" ]]; then
  if [[ "${ALLOW_NEWER_GLIBC:-0}" != "1" ]]; then
    echo "This system has glibc $host_glibc; release builds need glibc $TARGET_GLIBC (Ubuntu 22.04)." >&2
    echo "Set ALLOW_NEWER_GLIBC=1 for a local test build that runs only on glibc $host_glibc or later." >&2
    exit 1
  fi
  echo "    Test build: glibc $host_glibc (newer than the release target $TARGET_GLIBC)"
fi
tools=(uv)
[[ "${SKIP_APPIMAGE:-0}" == "1" ]] || tools+=(mksquashfs)
for tool in "${tools[@]}"; do
  command -v "$tool" >/dev/null || { echo "$tool is required (see docs/BUILDING.md)" >&2; exit 1; }
done

# Libraries the bundle carries because not every desktop has them; PyInstaller copies them from this
# system (Ubuntu/Debian: libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1
# libxcb-render-util0 libxcb-util1 libzstd1; the X11/Wayland/GL runtime is needed for the smoke test).
missing=()
# Read the cache once: piping it into "grep -q" would end the pipe early, which pipefail reports as failure.
system_libs="$(/sbin/ldconfig -p | awk '{print $1}')"
for lib in libxcb-cursor.so.0 libxcb-icccm.so.4 libxcb-image.so.0 libxcb-keysyms.so.1 \
           libxcb-render-util.so.0 libxcb-util.so.1 libzstd.so.1 libxkbcommon-x11.so.0 libEGL.so.1; do
  grep -qxF "$lib" <<< "$system_libs" || missing+=("$lib")
done
if (( ${#missing[@]} )); then
  echo "Missing on the build system (see docs/BUILDING.md): ${missing[*]}" >&2; exit 1
fi

# Python records the folder it is installed in; use a fixed, neutral folder so no personal folder
# name ends up in a release.
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/tmp/eng-drawing-viewer-build/python}"
if [[ ! -x "$VENV/bin/python" ]]; then
  step "Creating build environment ($VENV) with Python ${PYTHON:-$PYTHON_VERSION}"
  uv venv --quiet --managed-python --python "${PYTHON:-$PYTHON_VERSION}" "$VENV"
fi
PY="$VENV/bin/python"
step "Installing hash-checked build dependencies"
uv pip sync --quiet --python "$PY" --require-hashes packaging/requirements-linux.txt
uv pip install --quiet --python "$PY" --no-deps --no-build-isolation --reinstall-package eng-drawing-viewer .
"$PY" -c 'import sys; print("    Python", sys.version.split()[0])'
"$PY" -c "import PySide6.QtWidgets, pymupdf, pptx, numpy, cv2, pillow_heif, onnx, onnxruntime, ezdxf, defusedxml; print('    dependencies import')"

step "Fetching OCR language data (hash-checked)"
"$PY" tools/fetch_tessdata.py "$WORK/tessdata"
export ENG_VIEWER_TESSDATA="$WORK/tessdata"

if [[ "${RUN_TESTS:-0}" == "1" ]]; then
  step "Running the tests"
  QT_QPA_PLATFORM=offscreen "$PY" -m pytest -q -p no:cacheprovider
fi

step "Collecting the build environment's licence files"
rm -rf build/legal-notices
"$PY" packaging/collect_licenses.py build/legal-notices

step "Running PyInstaller"
rm -rf "$OUT" "$WORK/dist" "$WORK/pyinstaller" "$APPDIR"
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "$WORK/dist" --workpath "$WORK/pyinstaller" packaging/linux/ENG_Drawing_Viewer_linux.spec

step "Assembling the AppDir"
LIBDIR="$APPDIR/usr/lib/eng-drawing-viewer"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo" \
  "$APPDIR/usr/share/doc/eng-drawing-viewer"
mv "$WORK/dist/eng-drawing-viewer" "$LIBDIR"
install -m 755 packaging/linux/AppRun "$APPDIR/AppRun"
install -m 644 "packaging/linux/$APP_ID.desktop" "$APPDIR/$APP_ID.desktop"
install -m 644 "packaging/linux/$APP_ID.desktop" "$APPDIR/usr/share/applications/$APP_ID.desktop"
release_date="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%d)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$release_date/" "packaging/linux/$APP_ID.metainfo.xml" \
  > "$APPDIR/usr/share/metainfo/$APP_ID.metainfo.xml"
for size in 256 512; do
  mkdir -p "$APPDIR/usr/share/icons/hicolor/${size}x${size}/apps"
  install -m 644 "src/eng_viewer/resources/app_icon_$size.png" \
    "$APPDIR/usr/share/icons/hicolor/${size}x${size}/apps/$APP_ID.png"
done
install -m 644 src/eng_viewer/resources/app_icon_256.png "$APPDIR/$APP_ID.png"
ln -s "$APP_ID.png" "$APPDIR/.DirIcon"
install -m 644 packaging/linux/READ_ME.txt "$APPDIR/usr/share/doc/eng-drawing-viewer/READ_ME.txt"
install -m 644 LICENSE "$APPDIR/usr/share/doc/eng-drawing-viewer/LICENSE"
cp -R packaging/linux/appimage-runtime "$APPDIR/usr/share/doc/eng-drawing-viewer/appimage-runtime"
if command -v desktop-file-validate >/dev/null; then desktop-file-validate "$APPDIR/$APP_ID.desktop"; fi

step "Collecting licences and the software bill of materials"
mkdir -p "$OUT"
"$PY" tools/collect_licenses.py "$APPDIR" --out "$LIBDIR/_internal/legal/licenses" --sbom "$OUT/$BASENAME.cdx.json"

step "Smoke test: start through AppRun, render a page in a worker process, run PDF tools and the editors"
SMOKE="$WORK/smoke"
rm -rf "$SMOKE" && mkdir -p "$SMOKE/home"
"$PY" - "$SMOKE/smoke test.pdf" <<'PYEOF'
import sys, pymupdf
d = pymupdf.open(); p = d.new_page(width=842, height=595)
p.draw_rect(pymupdf.Rect(40, 40, 802, 555), width=0.5); p.insert_text((60, 80), "Smoke test", fontsize=18)
d.save(sys.argv[1])
PYEOF
# A clean environment: no Python, no build folders on any path, an empty home folder.
smoke_env=(env -i HOME="$SMOKE/home" PATH=/usr/bin:/bin LANG=C.UTF-8 ENG_VIEWER_SMOKE_TEST=1
           ENG_VIEWER_SMOKE_LOG="$SMOKE/smoke.log" XDG_RUNTIME_DIR="$SMOKE/home")
if command -v xvfb-run >/dev/null; then
  "${smoke_env[@]}" xvfb-run -a -s "-screen 0 1920x1080x24" "$APPDIR/AppRun" "$SMOKE/smoke test.pdf"
else
  "${smoke_env[@]}" QT_QPA_PLATFORM=offscreen "$APPDIR/AppRun" "$SMOKE/smoke test.pdf"
fi
cat "$SMOKE/smoke.log"
grep -q "3 PDF tool jobs OK" "$SMOKE/smoke.log"
grep -q '"original_unchanged": true' "$SMOKE/smoke.log"

step "Checking the bundle"
# A finding fails the build at the end, after the AppImage is made, so one run shows everything.
CHECK_FAILED=0
"$PY" tools/check_bundle.py "$APPDIR" --json "$WORK/check_bundle.json" --reviewed packaging/reviewed-upstream-findings.txt \
  || CHECK_FAILED=1

if [[ "${SKIP_APPIMAGE:-0}" == "1" ]]; then
  step "Done (AppDir only): $APPDIR"
  exit "$CHECK_FAILED"
fi

step "AppImage runtime $RUNTIME_TAG (signature-checked)"
RUNTIME="${APPIMAGE_RUNTIME:-$WORK/runtime-x86_64}"
if [[ -z "${APPIMAGE_RUNTIME:-}" ]]; then
  command -v gpgv >/dev/null || { echo "gpgv is required to check the AppImage runtime" >&2; exit 1; }
  curl -fsSL --retry 3 -o "$RUNTIME" "$RUNTIME_URL"
  curl -fsSL --retry 3 -o "$RUNTIME.sig" "$RUNTIME_URL.sig"
  KEYRING="$WORK/type2-runtime.gpg"
  rm -f "$KEYRING"
  gpg --batch --quiet --no-default-keyring --keyring "$KEYRING" \
    --import packaging/linux/appimage-runtime/type2-runtime-signing-key.asc
  gpgv --keyring "$KEYRING" "$RUNTIME.sig" "$RUNTIME"
fi
runtime_sha="$(sha256sum "$RUNTIME" | cut -d' ' -f1)"
expected_sha="${APPIMAGE_RUNTIME_SHA256:-}"
[[ -n "$expected_sha" || -n "${APPIMAGE_RUNTIME:-}" ]] || expected_sha="$RUNTIME_SHA256"
if [[ -n "$expected_sha" && "$runtime_sha" != "$expected_sha" ]]; then
  echo "AppImage runtime SHA-256 $runtime_sha does not match $expected_sha" >&2; exit 1
fi
echo "    runtime SHA-256 $runtime_sha"

step "Packing the AppImage"
# Reproducible image: root-owned files, no extended attributes, and (mksquashfs 4.5 and later read
# SOURCE_DATE_EPOCH themselves) every timestamp set to the source's commit time.
mksquashfs "$APPDIR" "$WORK/image.squashfs" -noappend -quiet -comp zstd -Xcompression-level 19 -b 1M \
  -all-root -no-xattrs
cat "$RUNTIME" "$WORK/image.squashfs" > "$OUT/$BASENAME.AppImage"
chmod 755 "$OUT/$BASENAME.AppImage"
rm -f "$WORK/image.squashfs"

step "Checking the AppImage: extract it and compare with the AppDir"
rm -rf "$WORK/extract" && mkdir -p "$WORK/extract"
(cd "$WORK/extract" && "$OUT/$BASENAME.AppImage" --appimage-extract >/dev/null)
diff -r --no-dereference "$APPDIR" "$WORK/extract/squashfs-root"
rm -rf "$WORK/extract"

step "Writing the build report and checksums"
"$PY" - "$OUT" "$BASENAME" "$VERSION" "$runtime_sha" "$RUNTIME_TAG" "$WORK/check_bundle.json" <<'PYEOF'
import hashlib, json, os, platform, subprocess, sys
out, base, version, runtime_sha, tag, check = sys.argv[1:]
image = os.path.join(out, base + ".AppImage")
report = {
    "edition": "linux-x86_64", "version": version, "file": base + ".AppImage",
    "size_bytes": os.path.getsize(image),
    "sha256": hashlib.sha256(open(image, "rb").read()).hexdigest(),
    "source_commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None,
    "build_glibc": platform.libc_ver()[1],
    "minimum_glibc": json.load(open(check)).get("glibc_required"),
    "appimage_runtime": {"release": tag, "sha256": runtime_sha},
    "python": sys.version.split()[0],
}
json.dump(report, open(os.path.join(out, "build-report.json"), "w"), indent=2)
print(json.dumps(report, indent=2))
PYEOF
(cd "$OUT" && sha256sum "$BASENAME.AppImage" "$BASENAME.cdx.json" > SHA256SUMS.txt)
ls -l "$OUT"
if [[ "$CHECK_FAILED" == 1 ]]; then
  echo "The bundle check reported findings (see above and $WORK/check_bundle.json)." >&2
  exit 1
fi
step "Done: $OUT/$BASENAME.AppImage"
