"""tools/fuse_macos.py: wheel selection, Mach-O detection and strict tree fusing (any platform)."""
from __future__ import annotations

import importlib.util
import shutil
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def fm():
    spec = importlib.util.spec_from_file_location("fuse_macos", ROOT / "tools" / "fuse_macos.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def thin(cpu: int) -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu) + b"\0" * 56


def fat(*cpus: int) -> bytes:
    return b"\xca\xfe\xba\xbe" + struct.pack(">I", len(cpus)) + b"".join(
        struct.pack(">IIIII", c, 0, 0, 0, 0) for c in cpus)


def test_macho_architectures(fm):
    assert fm.macho_archs(thin(fm.CPU_ARM64)) == {"arm64"}
    assert fm.macho_archs(thin(fm.CPU_X86_64)) == {"x86_64"}
    assert fm.macho_archs(fat(fm.CPU_ARM64, fm.CPU_X86_64)) == {"arm64", "x86_64"}
    assert fm.macho_archs(b"\xca\xfe\xba\xbe" + struct.pack(">I", 52)) is None  # a Java class file
    assert fm.macho_archs(b"#!/bin/sh\n") is None


def _file(name, *tags):
    return {"filename": name, "digests": {"sha256": name}, "url": "", "tags": tags}


def test_prefers_universal2_then_both_architectures_then_pure(fm):
    files = [_file("a-1-cp313-cp313-macosx_11_0_arm64.whl"), _file("a-1-cp313-cp313-macosx_10_13_x86_64.whl"),
             _file("a-1-cp313-cp313-macosx_10_13_universal2.whl"), _file("a-1-py3-none-any.whl")]
    assert list(fm.choose(files)) == ["universal2"]
    pick = fm.choose(files[:2] + files[3:])
    assert sorted(pick) == ["arm64", "x86_64"]
    assert list(fm.choose(files[3:])) == ["pure"]


def test_newest_wheel_that_fits_the_deployment_target(fm):
    files = [_file("n-2-cp313-cp313-macosx_11_0_arm64.whl"), _file("n-2-cp313-cp313-macosx_14_0_arm64.whl"),
             _file("n-2-cp313-cp313-macosx_15_0_arm64.whl"), _file("n-2-cp313-cp313-macosx_10_13_x86_64.whl"),
             _file("n-2-cp313-cp313-macosx_14_0_x86_64.whl")]
    pick = fm.choose(files)
    assert pick["arm64"]["filename"].endswith("14_0_arm64.whl")  # 15.0 needs a newer macOS than 14.4
    assert pick["x86_64"]["filename"].endswith("14_0_x86_64.whl")


def test_python_tags(fm):
    assert fm.compatible_python("cp313", "cp313") and fm.compatible_python("cp311", "abi3")
    assert fm.compatible_python("py3", "none") and fm.compatible_python("py2.py3", "none")
    assert not fm.compatible_python("cp312", "cp312") and not fm.compatible_python("cp314", "abi3")
    assert not fm.compatible_python("cp313", "cp313t")


def test_lock_parsing(fm, tmp_path):
    lock = tmp_path / "lock.txt"
    lock.write_text("numpy==2.5.4 \\\n    --hash=sha256:" + "a" * 64 + " \\\n    --hash=sha256:" + "b" * 64 +
                    "\n    # via x\nPyMuPDF==1.28.2 \\\n    --hash=sha256:" + "c" * 64 + "\n", encoding="utf-8")
    pins = fm.parse_lock(lock)
    assert pins["numpy"] == ("2.5.4", {"a" * 64, "b" * 64})
    assert pins["pymupdf"] == ("1.28.2", {"c" * 64})


def test_download_refuses_files_not_in_the_lock(fm, tmp_path):
    with pytest.raises(SystemExit, match="not in the lock"):
        fm.download({"filename": "x.whl", "digests": {"sha256": "f" * 64}, "url": ""}, tmp_path, {"0" * 64})


def test_merged_header_selects_by_architecture(fm):
    out = fm.merged_header(b"#define SIZEOF_LONG_DOUBLE 8\n", b"#define SIZEOF_LONG_DOUBLE 16\n").decode()
    assert out.index("__arm64__") < out.index("SIZEOF_LONG_DOUBLE 8") < out.index("__x86_64__") \
        < out.index("SIZEOF_LONG_DOUBLE 16") < out.index("#error")


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def test_fuse_tree_is_strict(fm, tmp_path):
    arm = _tree(tmp_path / "arm", {"same.py": b"x = 1\n", "differs.py": b"a\n", "only_arm.txt": b"",
                                   "lib.so": fat(fm.CPU_ARM64, fm.CPU_X86_64)})
    x86 = _tree(tmp_path / "x86", {"same.py": b"x = 1\n", "differs.py": b"b\n", "only_x86.txt": b"",
                                   "lib.so": fat(fm.CPU_ARM64, fm.CPU_X86_64) + b"\0"})
    report = fm.fuse_tree(arm, x86, tmp_path / "out", {}, {})
    assert report["identical"] == 1
    problems = "\n".join(report["problems"])
    assert "only in the arm64 input: only_arm.txt" in problems
    assert "only in the x86_64 input: only_x86.txt" in problems
    assert "not a binary: differs.py" in problems
    allowed = fm.fuse_tree(arm, x86, tmp_path / "out2", {r"differs\.py": "ok"}, {r"only_.*\.txt": "ok"})
    assert allowed["problems"] == [] and allowed["allowed_differences"] == {"differs.py": "ok"}
    assert (tmp_path / "out2" / "only_x86.txt").is_file()


def test_fuse_tree_rejects_wrong_architecture_inputs(fm, tmp_path):
    arm = _tree(tmp_path / "arm", {"m.so": thin(fm.CPU_X86_64)})
    x86 = _tree(tmp_path / "x86", {"m.so": thin(fm.CPU_X86_64) + b"\0"})
    report = fm.fuse_tree(arm, x86, tmp_path / "out", {}, {})
    assert any("arm64 input has ['x86_64']" in p for p in report["problems"])


def test_fuse_tree_flags_thin_binaries_left_after_fusing(fm, tmp_path):
    arm = _tree(tmp_path / "arm", {"only.dylib": thin(fm.CPU_ARM64)})
    x86 = _tree(tmp_path / "x86", {})
    report = fm.fuse_tree(arm, x86, tmp_path / "out", {}, {r"only\.dylib": "allowed to be one-sided"})
    assert any("not universal after fusing: only.dylib" in p for p in report["problems"])


@pytest.mark.skipif(not any(shutil.which(t) for t in ("lipo", "llvm-lipo", "llvm-lipo-18", "llvm-lipo-19", "llvm-lipo-20")),
                    reason="needs lipo or llvm-lipo")
def test_lipo_merges_real_thin_binaries(fm, tmp_path):
    """Two minimal valid thin Mach-O executables merge into one fat file."""
    def macho(cpu, sub):
        # mach_header_64 with no load commands: enough for lipo to accept it.
        return struct.pack("<IiiIIII", 0xFEEDFACF, cpu, sub, 2, 0, 0, 0) + b"\0" * 4

    arm = _tree(tmp_path / "arm", {"bin": macho(fm.CPU_ARM64, 0)})
    x86 = _tree(tmp_path / "x86", {"bin": macho(fm.CPU_X86_64, 3)})
    report = fm.fuse_tree(arm, x86, tmp_path / "out", {}, {})
    assert report["problems"] == [] and report["merged"] == ["bin"]
    assert fm.macho_archs((tmp_path / "out" / "bin").read_bytes()) == {"arm64", "x86_64"}


def test_check_bundle_reads_fat_and_thin_headers(fm):
    spec = importlib.util.spec_from_file_location("check_bundle", ROOT / "tools" / "check_bundle.py")
    cb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cb)
    assert cb.macho_archs(thin(fm.CPU_ARM64)) == {"arm64"}
    assert cb.macho_archs(fat(fm.CPU_ARM64, fm.CPU_X86_64)) == {"arm64", "x86_64"}
    assert cb.macho_archs(b"\xca\xfe\xba\xbe" + struct.pack(">I", 99)) == {"other"}
