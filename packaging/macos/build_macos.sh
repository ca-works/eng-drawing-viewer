#!/usr/bin/env bash
# Build "ENG Drawing Viewer.app" plus a DMG and ZIP for Apple silicon Macs.
#
#   packaging/macos/build_macos.sh
#
# Uses a separate build environment (.venv-build) with CPython 3.13.16 from uv, and installs the
# exact, hash-checked packages in packaging/requirements-macos.txt. Override the interpreter with
# PYTHON=/path/to/python3.13. Requires uv (https://docs.astral.sh/uv/).
#
# Output in release/macos/:
#   ENG Drawing Viewer.app
#   ENG-Drawing-Viewer-<version>-macOS-AppleSilicon.dmg
#   ENG-Drawing-Viewer-<version>-macOS-AppleSilicon.zip
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
APP_NAME="ENG Drawing Viewer"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/eng_viewer/__init__.py)"
[[ -n "$VERSION" ]] || { echo "Could not read the version from src/eng_viewer/__init__.py" >&2; exit 1; }
BASENAME="ENG-Drawing-Viewer-$VERSION-macOS-AppleSilicon"
OUT="$ROOT/release/macos"
VENV="${BUILD_VENV:-$ROOT/.venv-build}"
PYTHON_VERSION="3.13.16"

# Steadier output: fixed hash seed and timestamps from the source's commit time when known.
export PYTHONHASHSEED=0
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}"

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
# Python records the folder it is installed in (in its sysconfig data, which ships in the app). Install
# the build interpreter in a fixed, neutral folder so no personal folder name ends up in a release.
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/tmp/eng-drawing-viewer-build/python}"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "==> Creating build environment ($VENV) with Python ${PYTHON:-$PYTHON_VERSION}"
  uv venv --quiet --managed-python --python "${PYTHON:-$PYTHON_VERSION}" "$VENV"
fi
PY="$VENV/bin/python"
echo "==> Installing hash-checked build dependencies"
uv pip install --quiet --python "$PY" --require-hashes -r packaging/requirements-macos.txt
uv pip install --quiet --python "$PY" --no-deps --reinstall-package eng-drawing-viewer .
"$PY" -c 'import sys; print("    Python", sys.version.split()[0])'

echo "==> Generating icons from assets/app_icon_1024.png"
"$PY" packaging/make_icons.py

echo "==> Running PyInstaller"
rm -rf "$OUT" build/macos
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "$OUT" --workpath build/macos packaging/macos/ENG_Drawing_Viewer_mac.spec
APP="$OUT/$APP_NAME.app"
rm -rf "$OUT/$APP_NAME"  # the intermediate one-folder build; the .app is what ships

echo "==> Ad-hoc signing (required to run on Apple silicon; not notarized)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "==> Smoke test: launch the bundle, render a page in a worker process, quit"
SAMPLE="$(mktemp -d)/smoke.pdf"
"$PY" - "$SAMPLE" <<'PYEOF'
import sys, pymupdf
d = pymupdf.open(); p = d.new_page(width=842, height=595)
p.draw_rect(pymupdf.Rect(40, 40, 802, 555), width=0.5); p.insert_text((60, 80), "Smoke test", fontsize=18)
d.save(sys.argv[1])
PYEOF
ENG_VIEWER_SMOKE_TEST=1 "$APP/Contents/MacOS/$APP_NAME" "$SAMPLE"

echo "==> Packaging DMG and ZIP"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp packaging/macos/READ_ME.txt "$STAGE/Read Me.txt"
cp LICENSE "$STAGE/Licence.txt"
hdiutil create -quiet -volname "$APP_NAME $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$OUT/$BASENAME.dmg"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT/$BASENAME.zip"
rm -rf "$STAGE"

du -sh "$APP" "$OUT"/*.dmg "$OUT"/*.zip
echo "==> Done: $APP"
