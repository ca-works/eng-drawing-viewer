"""tools/release_manifest.py: four editions, real sizes and hashes, never 'released'."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/eng_viewer/__init__.py").read_text()).group(1)


@pytest.fixture()
def rm():
    spec = importlib.util.spec_from_file_location("release_manifest", ROOT / "tools" / "release_manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _editions(tmp_path: Path) -> Path:
    d = tmp_path / "editions"
    (d / "edition-linux-x86_64/release/linux").mkdir(parents=True)
    (d / "edition-linux-x86_64/build/linux").mkdir(parents=True)
    image = d / f"edition-linux-x86_64/release/linux/ENG-Drawing-Viewer-{VERSION}-Linux-x86_64.AppImage"
    image.write_bytes(b"appimage bytes")
    (d / "edition-linux-x86_64/release/linux/build-report.json").write_text(json.dumps(
        {"sha256": hashlib.sha256(b"appimage bytes").hexdigest(), "source_commit": "abc123", "minimum_glibc": "2.34"}))
    (d / "edition-linux-x86_64/build/linux/check_bundle.json").write_text(json.dumps(
        {"findings": [], "reviewed_findings": [{"finding": "x", "reason": "y"}], "glibc_required": "2.34"}))
    (d / "edition-windows-x64/release/windows").mkdir(parents=True)
    (d / f"edition-windows-x64/release/windows/ENG-Drawing-Viewer-{VERSION}-Windows-x64-Portable.zip").write_bytes(b"zip")
    return d


def test_four_editions_with_measured_values(rm, tmp_path, monkeypatch):
    d = _editions(tmp_path)
    results = {"linux": {"result": "success"}, "linux-distros": {"result": "success"},
               "windows": {"result": "failure"}, "apple-silicon": {"result": "skipped"},
               "universal": {"result": "skipped"}, "universal-on-intel": {"result": "skipped"}}
    monkeypatch.setenv("RESULTS", json.dumps(results))
    out = tmp_path / "manifest.json"
    assert rm.main([str(d), "--results-env", "RESULTS", "--out", str(out), "--stage", str(tmp_path / "stage")]) == 0
    m = json.loads(out.read_text())
    assert [e["id"] for e in m["editions"]] == ["macos-universal", "macos-apple-silicon", "windows-x64", "linux-x86_64"]
    linux = m["editions"][3]
    assert linux["size_bytes"] == len(b"appimage bytes")
    assert linux["sha256"] == hashlib.sha256(b"appimage bytes").hexdigest()
    assert linux["minimum_os"]["value"] == "glibc 2.34 (x86-64)" and linux["source_commit"] == "abc123"
    assert linux["release_status"].startswith("candidate") and linux["download_url"] is None
    windows = m["editions"][2]
    assert windows["build_status"] == "failed" and windows["release_status"] == "not ready"
    for mac in m["editions"][:2]:
        assert mac["build_status"] == "not built" and mac["sha256"] is None and mac["size_bytes"] is None
    assert not any("released" == e["release_status"] for e in m["editions"])
    staged = sorted(p.name for p in (tmp_path / "stage").iterdir())
    assert staged == ["ENG-Drawing-Viewer-Linux-x86_64.AppImage", "ENG-Drawing-Viewer-Windows-x64.zip", "SHA256SUMS.txt"]


def test_a_file_that_does_not_match_its_build_report_is_a_problem(rm, tmp_path):
    d = _editions(tmp_path)
    image = next(d.rglob("*.AppImage"))
    image.write_bytes(b"tampered")
    assert rm.main([str(d), "--out", str(tmp_path / "m.json")]) == 1
    assert any("does not match" in p for p in json.loads((tmp_path / "m.json").read_text())["problems"])


def test_without_ci_results_nothing_is_called_tested(rm, tmp_path):
    d = _editions(tmp_path)
    rm.main([str(d), "--out", str(tmp_path / "m.json")])
    for e in json.loads((tmp_path / "m.json").read_text())["editions"]:
        assert e["test_status"].startswith("unknown") and not e["release_status"].startswith("candidate")
