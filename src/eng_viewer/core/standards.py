"""Read-only local sources for the Standards reference workspace."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Event

from .naturalsort import natural_key

STANDARDS_EXTS = {".pdf", ".docx", ".xlsx", ".doc", ".xls"}
IWORK_EXTS = frozenset({".pages", ".numbers", ".key"}) if sys.platform == "darwin" else frozenset()
OPEN_EXTS = frozenset({".txt", ".csv", ".tsv", ".rtf", ".md"}) if sys.platform == "darwin" else frozenset()
STANDARDS_EXTS.update(IWORK_EXTS | OPEN_EXTS)


def scan_folder(root: Path, cancelled: Event) -> tuple[list[Path], int]:
    """Return regular files relative to the root, without following symlinks."""
    pending = [root]
    files = []
    unreadable = 0
    while pending and not cancelled.is_set():
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if cancelled.is_set():
                        return [], 0
                    try:
                        path = Path(entry.path)
                        ext = path.suffix.casefold()
                        if entry.is_dir(follow_symlinks=False):
                            if ext in IWORK_EXTS:
                                files.append(path.relative_to(root))
                            else:
                                pending.append(path)
                        elif ext in STANDARDS_EXTS and entry.is_file(follow_symlinks=False):
                            files.append(path.relative_to(root))
                    except OSError:
                        unreadable += 1
        except OSError:
            if folder == root:
                raise
            unreadable += 1
    files.sort(key=lambda p: tuple(natural_key(part) for part in p.parts))
    return files, unreadable


def restricted_office_command(command: list[str], work: Path) -> list[str]:
    """Disable Office active content and contain the macOS converter to its scratch directory."""
    user_profile = work / "profile" / "user"
    user_profile.mkdir(parents=True)
    (user_profile / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">\n'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">\n'
        '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>\n'
        '<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>\n'
        '</item></oor:items>\n', encoding="utf-8")
    policy = work / "reference.sb"
    # With system temp paths read-only, LibreOffice uses the private OSL_SOCKET_PATH.
    policy.write_text(
        '(version 1)\n(allow default)\n'
        '(deny network* (remote ip))\n(deny network* (local ip))\n'
        '(deny file-write*)\n'
        '(allow file-write* (subpath (param "SCRATCH")) (literal "/dev/null"))\n'
        '(deny file-write* (literal (param "SOURCE")))\n',
        encoding="utf-8")
    return ["/usr/bin/sandbox-exec", "-D", f"SCRATCH={work.resolve()}", "-D", f"SOURCE={command[-1]}",
            "-f", str(policy),
            "/usr/bin/env", f"TMPDIR={work}/", command[0], f"-env:OSL_SOCKET_PATH={work}", *command[1:]]
