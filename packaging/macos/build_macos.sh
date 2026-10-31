#!/usr/bin/env bash
# Build "ENG Drawing Viewer.app" plus a DMG and ZIP for one macOS edition.
#
#   EDITION=apple-silicon packaging/macos/build_macos.sh      (on an Apple Silicon Mac)
#   EDITION=universal     packaging/macos/build_macos.sh      (on an Apple Silicon Mac)
#   EDITION=intel         packaging/macos/build_macos.sh      (on an Intel Mac; local builds only)
#
# apple-silicon / intel: a build environment (.venv-build-<arch>) with CPython 3.13.16 from uv and
# the exact, hash-checked packages for this architecture (packaging/requirements-macos*.txt).
#
# OPENCV_WHEEL (apple-silicon / intel): replaces the published OpenCV wheel with the minimal one built
# from the pinned source release (packaging/macos/build_universal_opencv.sh keeps it in
# <prepared>/opencv-thin/<arch>/). Release builds use it: the published wheel bundles FFmpeg with
# OpenSSL and network protocol libraries the app never uses.
#
# universal: one app whose every binary holds both arm64 and x86_64 code. It needs
#   UNIVERSAL_PYTHON     the fused interpreter folder  (tools/fuse_macos.py python ...)
#   UNIVERSAL_WHEELHOUSE the universal2 wheels         (tools/fuse_macos.py wheels/pair ...,
#                                                       with OpenCV from tools/build_opencv_minimal.sh)
# PyInstaller builds with target_arch=universal2, which refuses any single-architecture binary,
# and tools/check_bundle.py checks every Mach-O file again. The smoke test runs natively and, under
# Rosetta 2, as Intel code.
#
# Requires uv (https://docs.astral.sh/uv/) and the Xcode command line tools.
#
# Output in release/macos/:
#   ENG Drawing Viewer.app
#   ENG-Drawing-Viewer-<version>-macOS-<Apple-Silicon|Universal|Intel>.dmg / .zip
#   build-report-<edition>.json
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
APP_NAME="ENG Drawing Viewer"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/eng_viewer/__init__.py)"
[[ -n "$VERSION" ]] || { echo "Could not read the version from src/eng_viewer/__init__.py" >&2; exit 1; }
ARCH="$(uname -m)"
EDITION="${EDITION:-$([[ "$ARCH" == arm64 ]] && echo apple-silicon || echo intel)}"
case "$EDITION" in
  apple-silicon) [[ "$ARCH" == arm64 ]] || { echo "The Apple Silicon edition is built on Apple Silicon." >&2; exit 1; }
                 LABEL="Apple-Silicon"; LOCK="packaging/requirements-macos.txt"; TARGET_ARCH=arm64; REQUIRE_ARCHS=arm64 ;;
  intel)         [[ "$ARCH" == x86_64 ]] || { echo "The Intel build is built on an Intel Mac." >&2; exit 1; }
                 LABEL="Intel"; LOCK="packaging/requirements-macos-intel.txt"; TARGET_ARCH=x86_64; REQUIRE_ARCHS=x86_64 ;;
  universal)     LABEL="Universal"; TARGET_ARCH=universal2; REQUIRE_ARCHS=arm64,x86_64
                 : "${UNIVERSAL_PYTHON:?set UNIVERSAL_PYTHON to the fused interpreter folder}"
                 : "${UNIVERSAL_WHEELHOUSE:?set UNIVERSAL_WHEELHOUSE to the universal2 wheel folder}" ;;
  *) echo "EDITION must be apple-silicon, universal or intel" >&2; exit 1 ;;
esac
BASENAME="ENG-Drawing-Viewer-$VERSION-macOS-$LABEL"
OUT="$ROOT/release/macos"
VENV="${BUILD_VENV:-$ROOT/.venv-build-$EDITION}"
PYTHON_VERSION="3.13.16"
step() { printf '==> %s\n' "$*"; }

# Steadier output: fixed hash seed and timestamps from the source's commit time when known.
export PYTHONHASHSEED=0
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}"
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

if [[ "$EDITION" == universal ]]; then
  FUSED_PY="$UNIVERSAL_PYTHON/bin/python3.13"
  [[ -x "$FUSED_PY" ]] || { echo "No interpreter at $FUSED_PY" >&2; exit 1; }
  for a in arm64 x86_64; do
    lipo "$FUSED_PY" -verify_arch "$a" || { echo "$FUSED_PY lacks $a" >&2; exit 1; }
  done
  if ! arch -x86_64 /usr/bin/true 2>/dev/null; then
    echo "Rosetta 2 is needed to test the Intel half (softwareupdate --install-rosetta --agree-to-license)" >&2
    exit 1
  fi
  step "Signing the fused interpreter ad hoc (lipo keeps each slice's signature; this re-seals them)"
  find "$UNIVERSAL_PYTHON" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0 |
    while IFS= read -r -d '' f; do
      if [[ "$(file -b "$f")" == *Mach-O* ]]; then codesign --force --sign - "$f" 2>/dev/null; fi
    done
  rm -rf "$VENV"
  step "Creating the universal build environment ($VENV)"
  "$FUSED_PY" -m venv --without-pip "$VENV"
  PY="$VENV/bin/python"
  step "Installing the universal2 wheelhouse (fresh environment, no index, no dependency resolution)"
  uv pip install --quiet --python "$PY" --no-index --no-deps "$UNIVERSAL_WHEELHOUSE"/*.whl
  # Installing local files records their path in direct_url.json, which PyInstaller would bundle
  # with the metadata some packages need; the app has no use for it.
  find "$VENV" -path '*.dist-info/direct_url.json' -delete
else
  # Python records the folder it is installed in (in its sysconfig data, which ships in the app).
  # Install the build interpreter in a fixed, neutral folder so no personal folder name is recorded.
  export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/tmp/eng-drawing-viewer-build/python}"
  if [[ ! -x "$VENV/bin/python" ]]; then
    step "Creating build environment ($VENV) with Python ${PYTHON:-$PYTHON_VERSION}"
    uv venv --quiet --managed-python --python "${PYTHON:-$PYTHON_VERSION}" "$VENV"
  fi
  PY="$VENV/bin/python"
  "$PY" - "$ARCH" <<'PYEOF'
import platform, sys
if platform.machine() != sys.argv[1] or sys.version_info[:2] != (3, 13):
    raise SystemExit("The build requires native Python 3.13 matching the current architecture.")
PYEOF
  step "Installing hash-checked build dependencies"
  uv pip sync --quiet --python "$PY" --require-hashes "$LOCK"
  if [[ -n "${OPENCV_WHEEL:-}" ]]; then
    [[ "$OPENCV_WHEEL" == */opencv_python_headless-*.whl && -f "$OPENCV_WHEEL" ]] || { echo "OPENCV_WHEEL is not an OpenCV wheel: $OPENCV_WHEEL" >&2; exit 1; }
    step "Replacing the published OpenCV with the minimal source build ($(basename "$OPENCV_WHEEL"))"
    shasum -a 256 "$OPENCV_WHEEL" | sed 's/^/    /'
    uv pip install --quiet --python "$PY" --no-index --no-deps --reinstall "$OPENCV_WHEEL"
  fi
fi
uv pip install --quiet --python "$PY" --no-deps --no-build-isolation --reinstall-package eng-drawing-viewer .
"$PY" -c 'import sys; print("    Python", sys.version.split()[0])'
IMPORTS='import PySide6.QtWidgets, pymupdf, pptx, numpy, cv2, pillow_heif, onnx, onnxruntime, ezdxf, defusedxml, AppKit, Vision'
"$PY" -c "$IMPORTS; print('    dependencies import natively')"
if [[ "$EDITION" == universal ]]; then
  arch -x86_64 "$PY" -c "$IMPORTS; import platform; assert platform.machine() == 'x86_64'; print('    dependencies import as Intel code (Rosetta 2)')"
fi

if [[ "${RUN_TESTS:-0}" == "1" ]]; then
  step "Running the tests"
  QT_QPA_PLATFORM=offscreen "$PY" -m pytest -q -p no:cacheprovider
  if [[ "$EDITION" == universal ]]; then
    step "Running the core and editor tests as Intel code (Rosetta 2)"
    QT_QPA_PLATFORM=offscreen arch -x86_64 "$PY" -m pytest -q -p no:cacheprovider \
      tests/test_core.py tests/test_render.py tests/test_image_tools.py tests/test_image_export.py \
      tests/test_vector_workspace.py tests/test_pdf_tools.py tests/test_pptx.py
  fi
fi

step "Generating icons from assets/app_icon_1024.png"
"$PY" packaging/make_icons.py
rm -rf build/legal-notices
"$PY" packaging/collect_licenses.py build/legal-notices

step "Running PyInstaller (target architecture: $TARGET_ARCH)"
rm -rf "$OUT/$APP_NAME.app" "$OUT/$APP_NAME" "$OUT/$BASENAME".* build/macos
mkdir -p "$OUT"
ENG_MAC_TARGET_ARCH="$TARGET_ARCH" "$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "$OUT" --workpath build/macos packaging/macos/ENG_Drawing_Viewer_mac.spec
APP="$OUT/$APP_NAME.app"
rm -rf "$OUT/$APP_NAME"  # the intermediate one-folder build; the .app is what ships

step "Ad-hoc signing for local testing (not Developer ID signed or notarized)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

step "Checking the bundle (every Mach-O file: $REQUIRE_ARCHS)"
# A finding fails the build at the end, after the smoke tests and packages, so one run shows everything.
CHECK_FAILED=0
"$PY" tools/check_bundle.py "$APP" --require-archs "$REQUIRE_ARCHS" --json "build/check_bundle-$EDITION.json" \
  --reviewed packaging/reviewed-upstream-findings.txt || CHECK_FAILED=1

smoke() {  # smoke ARCH LABEL
  local dir; dir="$(mktemp -d)"
  "$PY" - "$dir/smoke test.pdf" <<'PYEOF'
import sys, pymupdf
d = pymupdf.open(); p = d.new_page(width=842, height=595)
p.draw_rect(pymupdf.Rect(40, 40, 802, 555), width=0.5); p.insert_text((60, 80), "Smoke test", fontsize=18)
d.save(sys.argv[1])
PYEOF
  step "Smoke test ($2): launch the bundle, render a page in a worker process, PDF tools, editors"
  ENG_VIEWER_SMOKE_TEST=1 ENG_VIEWER_SMOKE_LOG="$dir/smoke.log" arch "-$1" "$APP/Contents/MacOS/$APP_NAME" "$dir/smoke test.pdf"
  cat "$dir/smoke.log"
  grep -q "3 PDF tool jobs OK" "$dir/smoke.log"
  grep -q '"original_unchanged": true' "$dir/smoke.log"
  cp "$dir/smoke.log" "build/smoke-$EDITION-$1.log"
  rm -rf "$dir"
}
smoke "$ARCH" "native $ARCH"
if [[ "$EDITION" == universal ]]; then
  smoke x86_64 "Intel code under Rosetta 2"
fi

step "Packaging DMG and ZIP"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp packaging/macos/READ_ME.txt "$STAGE/Read Me.txt"
cp LICENSE "$STAGE/Licence.txt"
hdiutil create -quiet -volname "$APP_NAME $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$OUT/$BASENAME.dmg"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT/$BASENAME.zip"
rm -rf "$STAGE"

step "Writing the build report"
"$PY" - "$OUT" "$BASENAME" "$VERSION" "$EDITION" "build/check_bundle-$EDITION.json" <<'PYEOF'
import hashlib, json, os, platform, subprocess, sys
out, base, version, edition, check = sys.argv[1:]
report = {"edition": f"macos-{edition}", "version": version, "files": {},
          "opencv_wheel": os.path.basename(os.environ["OPENCV_WHEEL"]) if os.environ.get("OPENCV_WHEEL") else
                          ("fused minimal source build" if edition == "universal" else "published wheel"),
          "source_commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None,
          "build_macos": platform.mac_ver()[0], "build_machine": platform.machine(), "python": sys.version.split()[0]}
for ext in ("dmg", "zip"):
    path = os.path.join(out, f"{base}.{ext}")
    report["files"][f"{base}.{ext}"] = {"size_bytes": os.path.getsize(path),
                                        "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest()}
c = json.load(open(check))
report["minimum_macos"] = c.get("LSMinimumSystemVersion")
report["architectures"] = c.get("architectures")
report["reviewed_findings"] = [r["finding"] for r in c.get("reviewed_findings", [])]
json.dump(report, open(os.path.join(out, f"build-report-{edition}.json"), "w"), indent=2)
print(json.dumps(report, indent=2))
PYEOF
du -sh "$APP" "$OUT/$BASENAME".dmg "$OUT/$BASENAME".zip
if [[ "$CHECK_FAILED" == 1 ]]; then
  echo "The bundle check reported findings (see above and build/check_bundle-$EDITION.json)." >&2
  exit 1
fi
step "Done: $APP"
