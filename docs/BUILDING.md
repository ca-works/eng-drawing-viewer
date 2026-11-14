# Building the four editions

Every edition is built from the same source tree with CPython 3.13.16, uv 0.12.24 and exact,
hash-checked dependency locks. Each build script makes its own build environment, builds the app
with PyInstaller, runs a smoke test of the packaged app, checks the bundle and writes its packages
to `release/<platform>/`. Nothing is published, uploaded or signed with a publisher certificate.

| Edition | Build machine | Script | Lock |
|---|---|---|---|
| macOS Apple Silicon | Apple silicon Mac | `EDITION=apple-silicon packaging/macos/build_macos.sh` | `packaging/requirements-macos.txt` |
| macOS Universal | Apple silicon Mac with Rosetta 2 | see [below](#macos-universal) | the same lock, fused |
| Windows x64 | Windows 10/11 x64 | `packaging\windows\build_windows.ps1` | `packaging/requirements-windows.txt` |
| Linux x86-64 | Ubuntu 22.04 (glibc 2.35) | `packaging/linux/build_linux.sh` | `packaging/requirements-linux.txt` |

The version comes from `src/eng_viewer/__init__.py`. Locks are regenerated on purpose with
`bash packaging/compile_locks.sh` (from the `.in` files). Network access is needed only while
building; the apps never download anything.

The smoke test (`ENG_VIEWER_SMOKE_TEST=1`) starts the packaged app on a sample PDF, waits for the
first page to render in a worker process, runs three PDF tools (Word, Excel, compress) and the
editor self-test (`tools/editor_smoke.py`: image project round trip, inpainting, eight export
formats, vector SVG/DXF/PDF round trips, ONNX Runtime on a synthetic model), checks the original
is unchanged, reports the Qt platform plugin and pixel ratio, and quits. `ENG_VIEWER_SMOKE_LOG`
writes the same lines to a file.

`tools/check_bundle.py` then inspects the result: personal or machine paths in any file or in the
compressed Python code, install metadata, network-capable components, and per platform: the real
minimum macOS of every Mach-O file (and with `--require-archs`, that each holds the required
architectures), every Windows PE file's architecture and security flags, or every Linux ELF file's
architecture, glibc requirement, RPATH and host libraries. Findings that come from upstream wheels
and were reviewed are listed in `packaging/reviewed-upstream-findings.txt` with their reasons; they
are reported but do not fail the build. Any other finding fails it, after the packages are written,
so one run shows everything.

## macOS Apple Silicon

Needs Xcode's command-line tools and uv.

```sh
EDITION=apple-silicon packaging/macos/build_macos.sh       # RUN_TESTS=1 also runs the tests first
```

Release builds replace the published OpenCV wheel with the minimal one built from the pinned source
release (the same build the Universal edition uses, below): the published arm64 wheel bundles FFmpeg
with OpenSSL, libssh and other network libraries the app never uses.

```sh
packaging/macos/prepare_universal.sh build/universal          # also makes the thin interpreters
packaging/macos/build_universal_opencv.sh build/universal     # keeps opencv-thin/arm64/*.whl
OPENCV_WHEEL="$PWD/$(ls build/universal/opencv-thin/arm64/opencv_python_headless-*.whl)" \
  EDITION=apple-silicon packaging/macos/build_macos.sh
```

The app declares macOS 14.4. Qt 6.12's frameworks and every other bundled library encode at most
14.4; the PySide6/shiboken6 binding modules carry a 26.0 label that is not enforced (the packaged app
ran on macOS 14.8, 15.7, 26.6 and 27), so `check_bundle.py` lists them under `minos_ignored`.
The app is signed ad hoc, not with a Developer ID, and not notarized.

`EDITION=intel` builds the same on an Intel Mac with `requirements-macos-intel.txt` (local use; the
official Intel-capable edition is the Universal one).

## macOS Universal

One app whose every binary contains both arm64 and x86_64 code. Build it on an Apple silicon Mac
with Rosetta 2 (needed to build and test the Intel half):

```sh
uv python install 3.13.16 && export PYTHON="$(uv python find 3.13.16)"
packaging/macos/prepare_universal.sh build/universal          # interpreter + wheelhouse
packaging/macos/build_universal_opencv.sh build/universal     # OpenCV for both, fused
UNIVERSAL_PYTHON=build/universal/python UNIVERSAL_WHEELHOUSE=build/universal/wheelhouse \
  EDITION=universal RUN_TESTS=1 packaging/macos/build_macos.sh
```

1. **Interpreter.** `prepare_universal.sh` downloads the official python-build-standalone 3.13.16
   builds for arm64 and x86_64 (the same builds uv installs elsewhere; SHA-256 pinned in
   `packaging/macos/universal-python.txt`) and `tools/fuse_macos.py python` merges them with lipo.
2. **Wheelhouse.** `tools/fuse_macos.py wheels` takes, for each package in
   `requirements-macos.txt`, its universal2 or pure-Python wheel where PyPI has one, otherwise its
   arm64 and x86_64 wheels merged with lipo. Every download must match a hash in the lock.
3. **OpenCV.** Its published macOS wheels can't be merged: the arm64 wheel bundles 95 libraries
   (FFmpeg's dependency tree) the x86_64 wheel lacks, and vice versa, so the result would contain
   arm64-only binaries. `build_universal_opencv.sh` builds OpenCV from the pinned source release
   twice with `tools/build_opencv_minimal.sh` (core, imgproc and photo, all the app uses; no FFmpeg,
   no codecs, no downloads; one small patch to the wheel recipe in `packaging/opencv/`) and merges
   the two identical-shaped wheels.
4. **App.** `build_macos.sh` installs the wheelhouse into a fresh environment of the merged
   interpreter, checks the imports natively and as Intel code, runs PyInstaller with
   `target_arch=universal2` (which refuses any single-architecture binary), runs
   `check_bundle.py --require-archs arm64,x86_64`, and smoke-tests the app natively and under
   Rosetta 2. The CI workflow also runs it on a real Intel Mac.

`fuse_macos.py` is strict: a file present in only one architecture, or a non-binary file that
differs, stops the build unless it is listed in the script with the reason it is harmless (today:
the interpreter's compiler-flag data, pip's install records, three bytecode caches, numpy's build
information). Differing C headers are merged into one header that picks each architecture with the
preprocessor. Reports listing every merged and allowed file are written next to the outputs. It
runs on Linux too (with `llvm-lipo`), which is how steps 1 and 2 were first verified.

## Windows x64

Needs Windows 10/11 x64, [uv](https://docs.astral.sh/uv/), [Go](https://go.dev/dl/) 1.26 or later
for the launcher, and [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the optional installer.

Release builds use the minimal OpenCV (core, imgproc, photo; no FFmpeg, OpenSSL or OpenCL) built
from the source release pinned in the Windows lock. It needs the Visual Studio 2022 or 2026 C++ build
tools and git:

```powershell
uv python install 3.13.16
python tools\build_opencv_minimal_windows.py (uv python find 3.13.16) packaging\requirements-windows.txt build\opencv-wheel
powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1 -OpencvWheel (Resolve-Path build\opencv-wheel\opencv_python_headless-*.whl)   # -Installer for Setup.exe
```

For a release, first build the minimal OpenCV in Git Bash with Visual Studio's C++ build tools
(`SOURCE_LOCK=packaging/requirements-macos.txt OPENCV_WORK=C:/ocv tools/build_opencv_minimal.sh
<python.exe> packaging/requirements-windows.txt build/opencv`) and set `OPENCV_WHEEL` to the wheel
before running the script; the published wheel bundles FFmpeg (`opencv_videoio_ffmpeg`). The
`editions` workflow does both.
`build_opencv_minimal_windows.py` reads its CMake options from `tools/build_opencv_minimal.sh`, so
every edition is configured identically, and uses `packaging/requirements-opencv-build-windows.txt`
(scikit-build 0.19.1 and CMake 4.3.2, which know Visual Studio 2026). Without `-OpencvWheel` the
build stops unless `-AllowPublishedOpencv` is given (local tests only: the published wheel carries
FFmpeg's `opencv_videoio_ffmpeg500_64.dll`, which the bundle check reports).

The script creates `.venv-build` with CPython 3.13.16, installs the Windows lock, downloads the
Tesseract language data (hash-checked), builds the launcher (Go, no third-party modules, `-trimpath`),
runs PyInstaller, collects the licence texts and a CycloneDX SBOM, smoke-tests the app through the
launcher, checks the bundle, and writes the application folder and
`ENG-Drawing-Viewer-<version>-Windows-x64-Portable.zip` (and with `-Installer` a per-user Setup
program) to `release\windows\`. The folder holds `ENG Drawing Viewer.exe` (the launcher),
`app\` and `legal\`. No Conda, corporate package index, machine-specific path or administrator
right is involved. `tools\build_from_archive.ps1` builds the same from a published source archive.

`.github/workflows/windows.yml` is the deeper Windows verification: tests on Windows Server 2022 and
2025, the packaged app with no Python on the PATH and outbound traffic blocked, the folders every
loaded DLL comes from, a scripted UI pass with screenshots, the installer, and a build from the
source archive.

## Linux x86-64

Build on Ubuntu 22.04: the libraries PyInstaller copies from the build system decide the oldest
glibc the AppImage runs on, so the script refuses a newer system unless `ALLOW_NEWER_GLIBC=1`
(local test builds only). Packages the build needs:

```sh
sudo apt-get install binutils squashfs-tools gpgv gnupg desktop-file-utils xvfb xauth \
  libegl1 libgl1 libopengl0 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 libdbus-1-3 \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
  libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxcb-xfixes0 libxcb-util1 libzstd1 \
  libwayland-client0 libwayland-cursor0 libwayland-egl1
uv python install 3.13.16
packaging/linux/build_opencv_linux.sh "$(uv python find 3.13.16)" build/opencv-wheel
OPENCV_WHEEL="$(ls "$PWD"/build/opencv-wheel/opencv_python_headless-*.whl)" \
  packaging/linux/build_linux.sh          # RUN_TESTS=1 also runs the tests; SKIP_APPIMAGE=1 stops at the AppDir
```

Release builds use the minimal OpenCV (core, imgproc, photo) instead of the published wheel, which
bundles FFmpeg and OpenSSL 1.1.1. Build the wheel on **Rocky Linux 9** (glibc 2.34, GCC 11), the
oldest supported system, so it needs nothing newer than the AppImage's minimum (built on Ubuntu
22.04 it would need glibc 2.35 and GLIBCXX 3.4.30). It needs `gcc-c++ make patch` and Python 3.13.16:

```sh
SOURCE_LOCK=packaging/requirements-macos.txt tools/build_opencv_minimal.sh \
  "$(uv python find 3.13.16)" packaging/requirements-linux.txt build/opencv      # on Rocky Linux 9
OPENCV_WHEEL="$PWD/build/opencv/opencv_python_headless-5.0.0.93-cp313-cp313-linux_x86_64.whl" \
  RUN_TESTS=1 packaging/linux/build_linux.sh                                    # on Ubuntu 22.04
```

`SOURCE_LOCK` supplies the source release's SHA-256 (the Linux and Windows locks pin only wheels).
Neither system needs to be a real machine: the 2.3.0 AppImage was built on an Apple silicon Mac in a
free Lima VM, with Ubuntu's official `ubuntu-base` 22.04 and Rocky's official container root
filesystem as x86-64 chroots run by Rosetta 2. A build from the source archive has no git; set
`SOURCE_COMMIT` to record the commit in `build-report.json`.
`build_opencv_linux.sh` runs the shared minimal-OpenCV recipe (`tools/build_opencv_minimal.sh`) with
the Linux lock. Linux additions: no RPATH (CMake would record the temporary build tree), and
`packaging/opencv/glibc-2.34-compat.h`, which keeps two symbols (`hypotf`, `std::condition_variable::wait`)
at their original versions so the wheel still loads on glibc 2.34 with GCC 11's libstdc++ (RHEL/Rocky
9). It fails if the wheel needs anything newer or links anything but the C/C++ runtime. Without
`OPENCV_WHEEL`, `build_linux.sh` stops unless `ALLOW_PUBLISHED_OPENCV=1` (local tests only: the
published wheel carries FFmpeg, OpenSSL 1.1.1k and OpenBLAS). `tools/build_from_archive_linux.sh`
builds the same from a published source archive.

The script installs the Linux lock, fetches the Tesseract data, runs PyInstaller
(`packaging/linux/ENG_Drawing_Viewer_linux.spec`), assembles an AppDir (`AppRun`, desktop entry,
AppStream metadata, icons, Read Me), collects licences and an SBOM, smoke-tests through `AppRun` in
an empty environment under Xvfb, checks the bundle, then packs the AppImage: the AppImage type 2
runtime (release 20251108, checked with `gpgv` against the vendored signing key and its pinned
SHA-256, see `packaging/linux/appimage-runtime/README.md`) followed by a SquashFS image of the
AppDir with fixed timestamps. It extracts the AppImage again and compares it with the AppDir, and
writes `build-report.json` and `SHA256SUMS.txt`.

The spec keeps a library from the build system only when a shipped binary needs it and it is not
one every desktop provides (graphics drivers, C/C++ runtimes, X11/Wayland, fontconfig, GLib, D-Bus).
Today that keeps seven (the libxcb helpers Qt's X11 plugin needs, and libzstd); every bundled binary
needs at most glibc 2.34. The licence collector attributes each of them to its Ubuntu package and
ships that package's copyright file.

## CI: the `build` workflow (Windows and Linux 2.3.0 candidates)

`.github/workflows/build.yml` (manual only; no macOS job) builds the minimal OpenCV and then the
Windows ZIP and the Linux AppImage from the exported source archive, runs the tests with
`ENG_REQUIRE_MINIMAL_OPENCV=1`, smoke-tests the packaged apps (Windows: no Python on the PATH,
outbound traffic blocked, DLL origins, scripted UI pass; Linux: FUSE, extract-and-run, inside a network
namespace without network, then Debian 12, Ubuntu 24.04, Fedora 42 and Rocky Linux 9). Results are
private 7-day artifacts. It is named `build.yml` because GitHub starts a workflow by hand only if a
file of that name is on the default branch.

## CI: the `editions` workflow

`.github/workflows/editions.yml` builds the four editions on clean GitHub-hosted runners when started
by hand (Actions › editions › Run workflow; GitHub offers this once the file is on the default branch).
It has no push, pull request or schedule trigger, so no commit can start it. The 2.3.0 macOS
editions were built locally on an Apple silicon Mac instead, at no cost. Inputs of a manual run:
the editions to build, whether to run the Linux distribution containers (Debian 12, Ubuntu 24.04,
Fedora 42, Rocky Linux 9) and the real Intel Mac test, and how many days to keep the artifacts.
A final job writes `release-manifest.json` (see `tools/release_manifest.py`).

### CI cost

On a private repository GitHub bills hosted-runner minutes with multipliers: Linux 1×, Windows 2×,
macOS 10×, against the account's monthly allowance (2,000 minutes on GitHub Free, 3,000 on Pro), and
artifacts count against the storage allowance (500 MB on Free). Every job has a time limit:

| Job | Runner | Limit | Expected | Billed (expected) |
|---|---|---|---|---|
| linux | ubuntu-22.04 | 40 min | ~20 min | ~20 |
| linux-distros (4) | ubuntu-24.04 | 15 min each | ~3 min each | ~12 |
| windows | windows-2025 | 45 min | ~25 min | ~50 |
| apple-silicon | macos-15 | 30 min | ~15 min | ~150 |
| universal | macos-15 | 50 min | ~35 min | ~350 |
| universal-on-intel | macos-15-intel | 15 min | ~5 min | ~50 |
| manifest | ubuntu-24.04 | 5 min | <1 min | ~1 |
| **all** | | caps at ~1,140 billed | | **~630 billed** |

Artifacts are about 180 MB (Linux) + 2 × 150–250 MB (macOS) + 150 MB (Windows); keep them for one
to three days. Check the account's usage (Settings › Billing) before a full run, and build only the
editions you need. A self-hosted runner or a local build costs no minutes: the scripts above are
the same ones CI runs.

## Development

```sh
uv venv --python 3.13 .venv
uv pip sync --python .venv/bin/python --require-hashes packaging/requirements-linux.txt   # or -macos / -windows
uv pip install --python .venv/bin/python --no-deps --no-build-isolation -e .
.venv/bin/python -m eng_viewer
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
```

macOS OCR and Continuity Camera need a desktop session; Windows UI tests use
`QT_QPA_PLATFORM=windows`. `python tests/corpus.py sample_drawings` generates synthetic drawings
(damaged files, deep nesting, unusual names). `tools/benchmark.py --app <app>` measures a packaged
app (a macOS `.app`, a Linux AppDir or AppImage, or the Windows app folder) and the renderer.

The spelling dictionary is generated with `python tools/build_wordlist.py scowl-2020.12.07.tar.gz`,
which checks the archive's SHA-256 and writes `src/eng_viewer/resources/english_words.txt` and
`licenses/SCOWL.txt`.

## Offline AI

The builds include the ONNX model-pack adapter and ONNX Runtime, but no trained weights. Models load
only after an explicit AI action, from local, hash-verified model packs (`docs/image-ai-model-packs.md`).
The synthetic ONNX check proves the runtime works, not background-removal quality.
