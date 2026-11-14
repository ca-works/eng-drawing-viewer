#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the minimal OpenCV wheel (core, imgproc, photo, Python bindings) on Windows x64.

    python tools/build_opencv_minimal_windows.py PYTHON LOCK OUT_DIR [--work C:\\eng-drawing-viewer-build\\opencv]

PYTHON  the CPython 3.13 interpreter the app ships (its venv builds the wheel)
LOCK    packaging/requirements-windows.txt: the OpenCV and numpy versions and the SHA-256 of the
        opencv-python-headless source release

This is the Windows twin of tools/build_opencv_minimal.sh (used for the macOS and Linux editions).
It reads the CMake options from that script, so every edition is configured with exactly the same
module list and the same disabled features; only the macOS deployment options are left out. The
published Windows wheel ships opencv_videoio_ffmpeg500_64.dll (FFmpeg, LGPL) and links OpenCL; the
minimal build has neither.

Needs the Visual Studio 2022 or 2026 C++ build tools and git (for applying the packaging patch).
The build runs in a fixed folder so no user-profile path can enter cv2's build information.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL_RECIPE = ROOT / "tools" / "build_opencv_minimal.sh"
PATCHES = [ROOT / "packaging" / "opencv" / "optional-typing-stubs.patch",   # shared with macOS/Linux
           ROOT / "packaging" / "opencv" / "windows-no-ffmpeg-plugin.patch"]  # Windows only
BUILD_TOOLS_LOCK = ROOT / "packaging" / "requirements-opencv-build-windows.txt"
# Names that must never appear in the wheel: FFmpeg, its network/crypto libraries, OpenCL.
FORBIDDEN = ("ffmpeg", "avformat", "avcodec", "avutil", "swscale", "swresample", "libssl", "libcrypto",
             "libssh", "opencl")
# A user-profile path of the build machine. "username" is the placeholder in opencv-python's own README
# (its wheel METADATA: "C:\\Users\\username\\..."), "runner"/"runneradmin" are the CI accounts.
HOME_PATH = re.compile(rb"(?:[A-Za-z]:[\\/]+Users[\\/]+|/(?:Users|home)/)(?!(?:runner|runneradmin|username)[\\/])"
                       rb"[A-Za-z0-9_.-]+[\\/]", re.I)


def shared_cmake_options(recipe: str = None) -> list[str]:
    """The -D options of tools/build_opencv_minimal.sh, minus the macOS-only ones."""
    text = recipe if recipe is not None else SHELL_RECIPE.read_text(encoding="utf-8")
    m = re.search(r'^export CMAKE_ARGS="(.*?)"$', text, re.S | re.M)
    if not m:
        raise SystemExit("CMAKE_ARGS not found in tools/build_opencv_minimal.sh")
    opts = shlex.split(m.group(1).replace("\\\n", " "))
    return [o for o in opts if not o.startswith(("-DCMAKE_OSX_", "-DCMAKE_OSX"))
            and "$" not in o]


def windows_cmake_options() -> list[str]:
    # Static MSVC runtime as in the published wheels (no msvcp140.dll beside cv2.pyd); no Windows
    # media backends (videoio is not built at all, these only make that explicit).
    return shared_cmake_options() + ["-DBUILD_WITH_STATIC_CRT=ON", "-DWITH_MSMF=OFF", "-DWITH_DSHOW=OFF",
                                     "-DWITH_DIRECTX=OFF", "-DWITH_OBSENSOR=OFF"]


def lock_block(lock: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}==.*?(?=^\S)", lock + "\nend", re.S | re.M)
    if not m:
        raise SystemExit(f"{name} is not in the lock")
    return m.group(0)


def check_wheel(whl: Path) -> list[str]:
    z = zipfile.ZipFile(whl)
    names = z.namelist()
    bad = [n for n in names if any(k in n.lower() for k in FORBIDDEN)]
    for n in names:
        data = z.read(n)
        m = HOME_PATH.search(data)
        if m:
            bad.append(f"{n}: {m.group().decode(errors='replace')}")
        if n.lower().endswith((".pyd", ".dll")):
            imports = sorted({x.decode().lower() for x in re.findall(rb"[A-Za-z0-9_.\-]+\.dll", data)})
            print(f"    {n}: DLL names referenced: {', '.join(imports)}")
            bad += [f"{n} references {d}" for d in imports if any(k in d for k in FORBIDDEN)]
    print(f"    {whl.name}: {len(names)} files" + (f"; UNEXPECTED: {bad}" if bad else ""))
    return bad


def run(cmd, **kw):
    print("    $", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("python")
    ap.add_argument("lock")
    ap.add_argument("out")
    ap.add_argument("--work", default=r"C:\eng-drawing-viewer-build\opencv")
    ap.add_argument("--print-options", action="store_true", help="print the CMake options and stop")
    a = ap.parse_args()
    if a.print_options:
        print("\n".join(windows_cmake_options()))
        return 0

    lock = Path(a.lock).read_text(encoding="utf-8")
    block = lock_block(lock, "opencv-python-headless")
    version = block.split("==", 1)[1].split()[0]
    hashes = set(re.findall(r"sha256:([0-9a-f]{64})", block))
    meta = json.load(urllib.request.urlopen(
        f"https://pypi.org/pypi/opencv-python-headless/{version}/json", timeout=60))
    sdist = [f for f in meta["urls"] if f["packagetype"] == "sdist"][0]
    sha = sdist["digests"]["sha256"]
    if sha not in hashes:
        raise SystemExit("the source release's SHA-256 is not in the lock")
    print(f"==> OpenCV {version} source (SHA-256 {sha})")

    work = Path(a.work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    archive = work / "src.tar.gz"
    with urllib.request.urlopen(sdist["url"], timeout=300) as r, open(archive, "wb") as fh:
        shutil.copyfileobj(r, fh)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != sha:
        raise SystemExit("downloaded source does not match its SHA-256")
    with tarfile.open(archive) as t:
        t.extractall(work, filter="data")
    src = work / f"opencv_python_headless-{version}"
    for patch in PATCHES:
        run(["git", "apply", "-p1", str(patch)], cwd=src)

    print("==> Build environment")
    run([a.python, "-m", "venv", work / "venv"])
    vpy = work / "venv" / "Scripts" / "python.exe"
    (work / "numpy.txt").write_text(lock_block(lock, "numpy"), encoding="utf-8")
    run([vpy, "-m", "pip", "install", "--quiet", "--no-deps", "--require-hashes", "-r", BUILD_TOOLS_LOCK])
    run([vpy, "-m", "pip", "install", "--quiet", "--no-deps", "--require-hashes", "-r", work / "numpy.txt"])

    print("==> Building (core, imgproc, photo)")
    env = dict(os.environ)
    env.update(ENABLE_HEADLESS="1", CMAKE_ARGS=" ".join(windows_cmake_options()),
               CMAKE_BUILD_PARALLEL_LEVEL=env.get("CMAKE_BUILD_PARALLEL_LEVEL", str(os.cpu_count() or 2)),
               TMP=str(work / "tmp"), TEMP=str(work / "tmp"))
    # scikit-build 0.19 picks the newest Visual Studio (2026, 2022, ...); CMAKE_GENERATOR overrides it.
    (work / "tmp").mkdir()
    print("    CMAKE_ARGS=" + env["CMAKE_ARGS"])
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    run([vpy, "-m", "pip", "wheel", ".", "--no-build-isolation", "--no-deps", "-w", out], cwd=src, env=env)

    wheels = sorted(out.glob("opencv_python_headless-*.whl"))
    if not wheels:
        raise SystemExit("no wheel was built")
    bad = []
    for whl in wheels:
        bad += check_wheel(whl)
        print(f"    SHA-256 {hashlib.sha256(whl.read_bytes()).hexdigest()}  {whl.name}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
