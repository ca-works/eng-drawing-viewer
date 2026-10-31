"""Copy complete installed dependency notices into the local bundle staging area.

This inventory describes the isolated build environment, not a binary-level SBOM.
It deliberately records no installation paths or direct_url metadata.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import re
import shutil


def collect(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    inventory = []
    for dist in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata.get("Name", "").lower()):
        name = dist.metadata.get("Name", "unknown")
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
        target = output / f"{safe_name}-{dist.version}"
        copied = []
        for item in sorted(dist.files or [], key=str):
            base = Path(str(item)).name.lower()
            if not (base.startswith(("license", "licence", "copying", "notice", "copyright", "thirdparty"))
                    or "licenses" in Path(str(item)).parts):
                continue
            source = Path(dist.locate_file(item))
            if not source.is_file() or source.stat().st_size > 8 * 1024 * 1024:
                continue
            # A PyObjC test binary named copying.cpython-*.so is not a license.
            if source.suffix.lower() in {".so", ".pyd", ".dll", ".dylib", ".a", ".o", ".pyc", ".py"}:
                continue
            if b"\x00" in source.read_bytes():
                continue
            target.mkdir(parents=True, exist_ok=True)
            dest_name = re.sub(r"[^a-zA-Z0-9._-]", "_", str(item))
            shutil.copyfile(source, target / dest_name)
            copied.append(dest_name)
        # Some packages store their entire license in METADATA instead of a file.
        license_text = dist.metadata.get("License", "")
        if license_text and license_text != "UNKNOWN":
            target.mkdir(parents=True, exist_ok=True)
            (target / "METADATA-LICENSE.txt").write_text(license_text + "\n", encoding="utf-8")
            copied.append("METADATA-LICENSE.txt")
        inventory.append({"name": name, "version": dist.version,
                          "license_expression": dist.metadata.get("License-Expression", ""),
                          "notices": copied})
    (output / "BUILD-ENVIRONMENT-INVENTORY.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return inventory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = collect(args.output)
    print(f"Preserved dependency notices for {len(result)} installed distributions.")
