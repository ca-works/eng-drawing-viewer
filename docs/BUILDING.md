# Building ENG Drawing Viewer

## Mac (Apple silicon)

Requirements: an Apple silicon Mac, Xcode's command-line tools (`xcode-select --install`) and [uv](https://docs.astral.sh/uv/).

```bash
packaging/macos/build_macos.sh
```

The script:

1. creates `.venv-build` with CPython 3.13.16 (downloaded and checksum-verified by uv);
2. installs the exact packages in `packaging/requirements-macos.txt`, each checked against its SHA-256 hash (`--require-hashes`);
3. installs ENG Drawing Viewer itself (not in editable mode);
4. generates the icons and runs PyInstaller with `packaging/macos/ENG_Drawing_Viewer_mac.spec`;
5. signs the app ad hoc (Apple silicon requires a signature), checks the signature, and runs a smoke test that starts the bundled app, renders a page in a worker process and runs three PDF tools;
6. writes `ENG Drawing Viewer.app`, `ENG-Drawing-Viewer-<version>-macOS-AppleSilicon.dmg` and `.zip` to `release/macos/`.

The version number comes from `src/eng_viewer/__init__.py`.

To change dependencies, edit `packaging/requirements-macos.in` and regenerate the lock file:

```bash
MACOSX_DEPLOYMENT_TARGET=14.0 uv pip compile packaging/requirements-macos.in \
  --python-version 3.13 --python-platform aarch64-apple-darwin --generate-hashes \
  -o packaging/requirements-macos.txt
```

### Checking a build

```bash
.venv-build/bin/python tools/check_bundle.py "release/macos/ENG Drawing Viewer.app"
```

reports the bundle's size, the real minimum macOS version declared by its binaries, and anything that should not ship (local paths, install metadata, network modules).

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python --require-hashes -r packaging/requirements-macos.txt
uv pip install --python .venv/bin/python --no-deps -e .
.venv/bin/python -m eng_viewer
.venv/bin/python -m pytest
```

Qt tests use the offscreen platform by default. macOS OCR (Apple Vision) and Continuity Camera need a normal desktop session.

`python tests/corpus.py sample_drawings` creates a set of sample drawings, including damaged files, deep nesting and unusual filenames. `tools/benchmark.py` measures rendering and scanning speed.

## Spelling dictionary

`src/eng_viewer/resources/english_words.txt` is generated from SCOWL:

```bash
python tools/build_wordlist.py scowl-2020.12.07.tar.gz
```

The script checks the archive's SHA-256 and writes the word list and `licenses/SCOWL.txt`.

## Windows

The Windows version is being rebuilt. The files in `packaging/windows/` are not yet used for releases.
