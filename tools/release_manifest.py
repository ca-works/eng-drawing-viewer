#!/usr/bin/env python3
"""Write the machine-readable release manifest of the four official editions.

    python tools/release_manifest.py EDITIONS_DIR [--results-env VAR] [--stage DIR] [--out release-manifest.json]

EDITIONS_DIR holds the build outputs (for example the downloaded ``edition-*`` CI artifacts, or the
release/ folder of local builds). For each of the four editions the manifest records the file that
was actually found, its real size and SHA-256 (computed here, never copied from elsewhere), the
version and source commit from the build report, the minimum OS and how it was established, and the
build and test status. ``--results-env`` names an environment variable holding the CI job results
(GitHub's ``toJSON(needs)``); without it a status is "unknown" unless a file proves a build ran.

``--stage DIR`` also copies each found file to DIR under its official release name and writes
SHA256SUMS.txt there: the release candidate the owner reviews.

Release status is never "released" and download URLs stay empty: the manifest describes candidates.
Only the publishing step, after the owner's approval and a verified public release, may fill them in
(the website reads only manifests that have them).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The four official editions, in the order the website lists them.
EDITIONS = [
    {
        "id": "macos-universal",
        "platform": "macOS",
        "architecture": "universal2 (x86_64 + arm64)",
        "filename": "ENG-Drawing-Viewer-macOS-Universal.dmg",
        "pattern": r"ENG-Drawing-Viewer-(?P<version>[\d.]+)-macOS-Universal\.dmg",
        "report": r"build-report-universal\.json",
        "jobs": ["universal"],
        "test_jobs": ["universal", "universal-on-intel"],
        "signing": "ad-hoc signature only (not Developer ID signed, not notarized)",
    },
    {
        "id": "macos-apple-silicon",
        "platform": "macOS",
        "architecture": "arm64 (Apple silicon)",
        "filename": "ENG-Drawing-Viewer-macOS-Apple-Silicon.dmg",
        "pattern": r"ENG-Drawing-Viewer-(?P<version>[\d.]+)-macOS-Apple-Silicon\.dmg",
        "report": r"build-report-apple-silicon\.json",
        "jobs": ["apple-silicon"],
        "test_jobs": ["apple-silicon"],
        "signing": "ad-hoc signature only (not Developer ID signed, not notarized)",
    },
    {
        "id": "windows-x64",
        "platform": "Windows",
        "architecture": "x86-64",
        "filename": "ENG-Drawing-Viewer-Windows-x64.zip",
        "pattern": r"ENG-Drawing-Viewer-(?P<version>[\d.]+)-Windows-x64-Portable\.zip",
        "report": None,
        "jobs": ["windows"],
        "test_jobs": ["windows"],
        "signing": "not Authenticode signed",
        # Declared by the build (app.manifest supportedOS, READ_ME.txt), not measured on Windows 10.
        "minimum_os": {"value": "Windows 10 version 1809 or Windows 11 (x64)",
                       "basis": "declared (application manifest and Read Me); CI runs Windows Server 2022 and 2025"},
    },
    {
        "id": "linux-x86_64",
        "platform": "Linux",
        "architecture": "x86-64",
        "filename": "ENG-Drawing-Viewer-Linux-x86_64.AppImage",
        "pattern": r"ENG-Drawing-Viewer-(?P<version>[\d.]+)-Linux-x86_64\.AppImage",
        "report": r"build-report\.json",
        "jobs": ["linux"],
        "test_jobs": ["linux", "linux-distros"],
        "signing": "not signed (SHA-256 checksums only)",
    },
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find(root: Path, pattern: str) -> list[Path]:
    rx = re.compile(pattern)
    return sorted(p for p in root.rglob("*") if p.is_file() and rx.fullmatch(p.name))


def job_status(results: dict | None, jobs: list[str]) -> str:
    if results is None:
        return "unknown"
    states = [results.get(j, {}).get("result", "not run") for j in jobs]
    if all(s == "success" for s in states):
        return "passed"
    if any(s == "failure" for s in states):
        return "failed"
    if any(s == "cancelled" for s in states):
        return "cancelled"
    if all(s == "skipped" for s in states):
        return "not run"
    return "incomplete: " + ", ".join(f"{j} {s}" for j, s in zip(jobs, states))


def minimum_os(edition: dict, report: dict | None, check: dict | None) -> dict | None:
    if "minimum_os" in edition:
        return edition["minimum_os"]
    if edition["platform"] == "macOS":
        declared = (report or {}).get("minimum_macos") or (check or {}).get("LSMinimumSystemVersion")
        if declared:
            return {"value": f"macOS {declared}",
                    "basis": "LSMinimumSystemVersion, checked against every bundled binary's minimum"}
    if edition["platform"] == "Linux":
        glibc = (report or {}).get("minimum_glibc") or (check or {}).get("glibc_required")
        if glibc:
            return {"value": f"glibc {glibc} (x86-64)",
                    "basis": "highest glibc symbol version any bundled binary needs"}
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("editions", type=Path)
    ap.add_argument("--results-env")
    ap.add_argument("--stage", type=Path)
    ap.add_argument("--out", type=Path, default=Path("release-manifest.json"))
    args = ap.parse_args(argv)
    results = json.loads(os.environ[args.results_env]) if args.results_env and os.environ.get(args.results_env) else None
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/eng_viewer/__init__.py").read_text()).group(1)
    rows, problems = [], []
    for ed in EDITIONS:
        files = find(args.editions, ed["pattern"])
        row = {k: ed[k] for k in ("id", "platform", "architecture", "filename", "signing")}
        row.update(version=None, build_filename=None, size_bytes=None, sha256=None, minimum_os=None,
                   source_commit=None)
        report = check = None
        if ed["report"]:
            reports = find(args.editions, ed["report"])
            report = json.loads(reports[0].read_text()) if reports else None
        checks = [p for p in find(args.editions, r"check_bundle.*\.json")
                  if ed["id"].split("-", 1)[1] in p.name or ed["platform"] == "Linux" and p.parent.name == "linux"
                  or ed["platform"] == "Windows" and p.parent.name == "windows"]
        check = json.loads(checks[0].read_text()) if checks else None
        if len(files) > 1:
            problems.append(f"{ed['id']}: more than one candidate file: {[f.name for f in files]}")
        if files:
            f = files[0]
            row.update(version=re.fullmatch(ed["pattern"], f.name)["version"], build_filename=f.name,
                       size_bytes=f.stat().st_size, sha256=sha256(f))
            if row["version"] != version:
                problems.append(f"{ed['id']}: built as {row['version']}, the source says {version}")
            if report:
                row["source_commit"] = report.get("source_commit")
                recorded = report.get("sha256") or report.get("files", {}).get(f.name, {}).get("sha256")
                if recorded and recorded != row["sha256"]:
                    problems.append(f"{ed['id']}: {f.name} does not match its build report's SHA-256")
            if args.stage:
                args.stage.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, args.stage / ed["filename"])
        if results is None:
            row["build_status"] = "built" if files else "not built"
            row["test_status"] = "unknown (no CI results given)"
        else:
            built = job_status(results, ed["jobs"])
            row["build_status"] = "succeeded" if built == "passed" and files else ("not built" if not files else built)
            row["test_status"] = job_status(results, ed["test_jobs"])
        row["source_commit"] = row["source_commit"] or os.environ.get("GITHUB_SHA")
        row["minimum_os"] = minimum_os(ed, report, check) if files else None
        if check is not None:
            row["bundle_check"] = {"findings": len(check.get("findings", [])),
                                   "reviewed_upstream_findings": len(check.get("reviewed_findings", []))}
            if ed["id"] == "macos-universal":
                row["bundle_check"]["mach_o_architectures"] = check.get("architectures")
        ready = files and row["build_status"] == "succeeded" and row["test_status"] == "passed" \
            and row.get("bundle_check", {}).get("findings", 1) == 0
        row["release_status"] = ("candidate: awaiting the owner's audit and approval" if ready
                                 else "not ready" if files else "not built")
        row["download_url"] = None
        rows.append(row)
    manifest = {
        "schema": 1,
        "application": "ENG Drawing Viewer",
        "version": version,
        "generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "editions": rows,
        "problems": problems,
        "note": "Candidates only. Nothing here is released; download_url stays empty until a verified public release exists.",
    }
    args.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.stage:
        lines = [f"{r['sha256']}  {r['filename']}" for r in rows if r["sha256"]]
        (args.stage / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for r in rows:
        print(f"{r['id']:20} {r['build_status']:12} tests {r['test_status']:10} {r['release_status']}"
              + (f"  {r['size_bytes']:,} bytes  {r['sha256'][:12]}…" if r["sha256"] else ""))
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
