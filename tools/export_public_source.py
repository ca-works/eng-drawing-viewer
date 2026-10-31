#!/usr/bin/env python3
"""Export an allowlisted committed revision; fail closed on privacy/secret findings.

No publishing. External gitleaks and exiftool must be installed. A successful
export is evidence for review, never permission to release.

``--unscanned`` writes the same archive without the privacy scans, for CI machines
that check the build from an archive but don't hold the private terms. Its
SOURCE_MANIFEST.txt must match the scanned export's before either is trusted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT_FILES = {'LICENSE', 'README.md', 'CHANGELOG.md', 'pyproject.toml', '.gitattributes'}
DOCS = {'docs/INSTALL.md', 'docs/BUILDING.md', 'docs/PDF_TOOLS.md', 'docs/IMAGE_EDITOR.md', 'docs/VECTOR_EDITOR.md',
        'docs/PDF_EDITOR.md', 'docs/IMAGE_DEPENDENCIES.md', 'docs/image-ai-model-packs.md', 'docs/ARCHITECTURE.md',
        'docs/COMPATIBILITY.md', 'docs/LICENSING.md'}
TOOLS = {'tools/export_public_source.py', 'tools/collect_licenses.py', 'tools/build_wordlist.py',
         'tools/check_bundle.py', 'tools/benchmark.py', 'tools/build_from_archive.sh',
         'tools/compare_builds.py', 'tools/fetch_third_party_sources.py',
         'tools/publish_release.sh', 'tools/curate_licenses.py', 'tools/fetch_tessdata.py',
         'tools/build_from_archive.ps1',
         # Bundled into every app (the frozen self-test), so part of the corresponding source.
         'tools/editor_smoke.py',
         # Build inputs of the Linux and macOS Universal editions and the release manifest.
         'tools/fuse_macos.py', 'tools/build_opencv_minimal.sh', 'tools/release_manifest.py'}
# Build files whose names have no extension, or extensions only the packaging folder uses.
PACKAGING_NAMES = {'packaging/linux/AppRun'}
PACKAGING_SUFFIXES = {'.desktop', '.xml', '.patch', '.asc'}
DENIED = {'audit', 'reference', '.git', '.github', 'build', 'dist', 'release', '.venv', '__pycache__'}
BINARY_SUFFIXES = {'.exe', '.dll', '.dylib', '.so', '.pyc', '.zip', '.dmg', '.pdf'}
# Patterns are assembled so this scanner can itself be exported without exemptions.
PATTERNS = []
PATTERNS += [re.compile(x, re.I) for x in (
    r'/(?:Users|home)/(?!runner/|me/|you/|user/|ci/|someone/)[A-Za-z0-9_.-]+', r'[A-Z]:\\Users\\[^\\\s]+',
    r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',
    r'gh[pousr]_[A-Za-z0-9]{20,}', r'AKIA[0-9A-Z]{16}',
)]
# Email addresses are blocked except in third-party licence texts, which credit their authors.
EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
EMAIL_ALLOWED = ('noreply@', 'users.noreply.github.com', 'example.com', 'example.org',
                 # The AppImage project's own contact, cited in packaging/linux/appimage-runtime/README.md.
                 '@appimage.org')
# Lists the upstream build paths found in third-party binaries (with the reason each is accepted),
# so the home-folder pattern is not applied to it. Private terms and other patterns still are.
UPSTREAM_PATH_LIST = 'packaging/reviewed-upstream-findings.txt'


def allowed(name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or any(x in DENIED for x in p.parts):
        return False
    if p.suffix.lower() in BINARY_SUFFIXES:
        return False
    return (name in ROOT_FILES or name in DOCS or name in TOOLS or name == 'assets/app_icon_1024.png'
            or (name.startswith('docs/images/') and p.suffix.lower() == '.png')
            # Licence texts keep their upstream names, often without an extension (COPYING, LICENSE).
            or p.parts[0] == 'licenses'
            or name in PACKAGING_NAMES
            or (p.parts[0] == 'packaging' and p.suffix.lower() in PACKAGING_SUFFIXES)
            or (p.parts[0] in {'src', 'tests', 'packaging', 'licenses'}
                and p.suffix.lower() in {'.py', '.toml', '.txt', '.md', '.sh', '.ps1', '.bat', '.spec', '.go', '.mod', '.svg', '.png', '.ico', '.icns', '.in',
                                         '.iss', '.manifest'}))


def load_private_terms(path):
    """Owner-specific terms (names, former workplaces) live in a private file, not in this code."""
    terms = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('word:'):
            word = line[5:]
            rx = re.compile(r'(?<![A-Za-z0-9])' + re.escape(word) + r'(?![A-Za-z0-9])', re.I)
            terms.append((rx, len(word) <= 3))
        else:
            terms.append((re.compile(re.escape(line), re.I), False))
    if not terms:
        raise ValueError(f'No private terms in {path}; refusing to export without them.')
    return terms


PRIVATE = []


def findings(name, data):
    text = name + '\n' + data.decode('utf-8', errors='replace')
    binary = b'\0' in data[:4096]
    # In binary files (images) short terms match random bytes; exiftool checks their metadata.
    terms = [rx for rx, short in PRIVATE if not (binary and short)]
    found = [f'{name}: matches a private term' for p in terms if p.search(text)][:1]
    found += [f'{name}: matches {p.pattern!r}' for p in PATTERNS
              if p.search(text) and not (name == UPSTREAM_PATH_LIST and p is PATTERNS[0])]
    if not name.startswith(('licenses/', 'packaging/licenses/')):
        found += [f'{name}: email address' for m in EMAIL.finditer(text)
                  if not any(a in m.group(0) for a in EMAIL_ALLOWED)][:1]
    return found


def scan(name, data):
    found = findings(name, data)
    if found:
        raise ValueError('Blocked content; review privately:\n  ' + '\n  '.join(found))


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def export(root, revision, output, scan_tools=True):
    if scan_tools and (not shutil.which('gitleaks') or not shutil.which('exiftool')):
        raise RuntimeError('gitleaks and exiftool are required; no archive produced.')
    commit = git(root, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    entries = []
    problems = []
    for row in git(root, 'ls-tree', '-r', '-z', commit).split(b'\0'):
        if not row:
            continue
        info, raw_name = row.split(b'\t', 1)
        mode, kind, oid = info.decode().split()
        name = raw_name.decode('utf-8')
        if not allowed(name):
            continue
        if mode not in {'100644', '100755'} or kind != 'blob':
            raise ValueError(f'Unsupported file type: {name}')
        data = git(root, 'cat-file', 'blob', oid)
        if scan_tools:
            problems.extend(findings(name, data))
        entries.append((name, data, mode))
    if problems:
        raise ValueError('Blocked content; review privately:\n  ' + '\n  '.join(problems))
    if not any(n == 'LICENSE' for n, _, _ in entries):
        raise ValueError('A committed LICENSE is required.')
    with tempfile.TemporaryDirectory(prefix='eng-source-review-') as folder:
        staging = Path(folder)
        for name, data, _ in entries:
            dest = staging / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        if scan_tools:
            subprocess.run(['gitleaks', 'dir', '--redact', '--no-banner', str(staging)], check=True)
        meta = json.loads(subprocess.check_output(['exiftool', '-json', '-r', str(staging)])) if scan_tools else []
        forbidden = {'Artist', 'Author', 'Creator', 'OwnerName', 'GPSLatitude', 'GPSLongitude', 'Comment', 'Description', 'Copyright'}
        for item in meta:
            if any(k in item and item[k] for k in forbidden):
                raise ValueError('Media metadata requires private review; no archive produced.')
            # Filesystem metadata describes staging, not shipped content.
            scan('embedded metadata', json.dumps({k: v for k, v in item.items()
                 if k not in {'SourceFile', 'Directory', 'FileName', 'FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate'}}).encode())
        manifest = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data, _ in sorted(entries))
        init = next(d for n, d, _ in entries if n == 'src/eng_viewer/__init__.py').decode()
        version = re.search(r'__version__ = "([^"]+)"', init).group(1)
        top = f'eng-drawing-viewer-{version}/'
        entries.append(('SOURCE_MANIFEST.txt', manifest.encode(), '100644'))
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            with zipfile.ZipFile(temp_path, 'w', compression=zipfile.ZIP_STORED) as archive:
                for name, data, mode in sorted(entries):
                    info = zipfile.ZipInfo(top + name, (1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (int(mode, 8) << 16)
                    archive.writestr(info, data)
            temp_path.replace(output)
        finally:
            temp_path.unlink(missing_ok=True)
    return commit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True, help='Reviewed commit or tag; working files are never included')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--private-terms', type=Path, default=Path(__file__).resolve().parents[1] / 'audit' / 'private-terms.txt',
                        help='File of owner-specific terms that must not appear (kept out of the repository)')
    parser.add_argument('--unscanned', action='store_true',
                        help='Skip the privacy scans (CI reproducibility checks only; never for release)')
    args = parser.parse_args()
    if not args.unscanned:
        PRIVATE[:] = load_private_terms(args.private_terms)
    print(export(args.root, args.revision, args.output, scan_tools=not args.unscanned))
