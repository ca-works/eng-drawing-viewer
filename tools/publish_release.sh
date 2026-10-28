#!/usr/bin/env bash
# Publish an approved release. Never run this before the owner has approved the exact candidate.
#
#   tools/publish_release.sh <candidate-folder> <approved-manifest-sha256>
#
# The candidate folder holds the release assets, SHA256SUMS.txt, release-manifest.json and
# RELEASE_NOTES.md. The script refuses to continue unless the manifest's SHA-256 equals the
# approved hash and every asset matches SHA256SUMS.txt. It then:
#   1. creates the public app repository if it doesn't exist, with one fresh commit made from the
#      source archive (no private history);
#   2. tags v<version> and creates a draft release with the assets;
#   3. checks GitHub's SHA-256 digest of every uploaded asset against SHA256SUMS.txt;
#   4. publishes the release and redeploys the website so its download buttons update.
set -euo pipefail

CANDIDATE="$(cd "$1" && pwd)"
APPROVED="$2"
APP_REPO="${APP_REPO:-ca-works/eng-drawing-viewer}"
SITE_REPO="${SITE_REPO:-ca-works/eng-drw}"
cd "$CANDIDATE"

actual="$(shasum -a 256 release-manifest.json | cut -d' ' -f1)"
[[ "$actual" == "$APPROVED" ]] || { echo "release-manifest.json is $actual, not the approved $APPROVED. Stopping." >&2; exit 1; }
shasum -a 256 -c SHA256SUMS.txt --quiet
VERSION="$(python3 -c 'import json; print(json.load(open("release-manifest.json"))["version"])')"
SOURCE_ZIP="eng-drawing-viewer-$VERSION-source.zip"
ASSETS=($(awk '{print $2}' SHA256SUMS.txt) SHA256SUMS.txt)
echo "==> Publishing ENG Drawing Viewer $VERSION to $APP_REPO with ${#ASSETS[@]} files"

if ! gh repo view "$APP_REPO" >/dev/null 2>&1; then
  echo "==> Creating $APP_REPO with a single commit from $SOURCE_ZIP"
  WORK="$(mktemp -d)"
  unzip -q "$SOURCE_ZIP" -d "$WORK"
  cd "$WORK/eng-drawing-viewer-$VERSION"
  rm -f SOURCE_MANIFEST.txt
  git init -q -b main
  git add -A
  git -c user.name=ca-works -c user.email=337696274+ca-works@users.noreply.github.com \
    commit -q -m "ENG Drawing Viewer $VERSION"
  gh repo create "$APP_REPO" --public \
    --description "Free, offline viewer for engineering drawings: markup, PDF tools and PowerPoint export. macOS." \
    --homepage "https://ca-works.github.io/eng-drw/" --source . --push
  gh repo edit "$APP_REPO" --enable-issues --enable-wiki=false \
    --add-topic engineering-drawings --add-topic pdf --add-topic macos --add-topic pyside6
  cd "$CANDIDATE"
fi

echo "==> Draft release v$VERSION"
gh release create "v$VERSION" -R "$APP_REPO" --draft --target main \
  --title "ENG Drawing Viewer $VERSION" --notes-file RELEASE_NOTES.md "${ASSETS[@]}"

echo "==> Checking GitHub's checksums"
gh api "repos/$APP_REPO/releases" --jq ".[] | select(.tag_name==\"v$VERSION\") | .assets[] | \"\(.digest) \(.name)\"" |
while read -r digest name; do
  if [[ "$name" == SHA256SUMS.txt ]]; then
    want="sha256:$(shasum -a 256 SHA256SUMS.txt | cut -d' ' -f1)"
  else
    want="sha256:$(grep "  $name\$" SHA256SUMS.txt | cut -d' ' -f1 || true)"
  fi
  [[ "$digest" == "$want" ]] || { echo "Digest mismatch for $name" >&2; exit 1; }
  echo "    $name ok"
done

echo "==> Publishing"
gh release edit "v$VERSION" -R "$APP_REPO" --draft=false --latest
gh workflow run deploy.yml -R "$SITE_REPO"
echo "==> Done. Verify https://ca-works.github.io/eng-drw/download/ after the site deploys."
