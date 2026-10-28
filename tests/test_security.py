"""Focused security regressions; no network requests leave the test process."""
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pymupdf
import pytest

from eng_viewer.core import pdf_tools
from eng_viewer.core.raster_limits import check_pixels


def sample(path, text='Sample'):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 50), text)
        doc.save(path)
    return str(path)


def test_hardlink_and_symlink_outputs_rejected(tmp_path):
    source = tmp_path / 'in.pdf'
    sample(source)
    for link, make in [('hard.pdf', os.link), ('sym.pdf', os.symlink)]:
        target = tmp_path / link
        make(source, target)
        with pytest.raises(pdf_tools.ToolError):
            pdf_tools._check_output(str(target), [str(source)])


def test_excel_text_is_never_formula_or_link(tmp_path):
    source = sample(tmp_path / 'in.pdf', '=1+2\nhttps://example.org\n+SUM(1,2)\n@SUM(1,2)')
    output = tmp_path / 'out.xlsx'
    pdf_tools.to_excel(source, str(output))
    with zipfile.ZipFile(output) as archive:
        sheets = [archive.read(n) for n in archive.namelist() if n.startswith('xl/worksheets/')]
        assert all(b'<f>' not in sheet and b'<hyperlink' not in sheet for sheet in sheets)
        assert b'=1+2' in archive.read('xl/sharedStrings.xml')


def test_default_owner_password_is_not_derived(tmp_path):
    source = sample(tmp_path / 'in.pdf')
    output = tmp_path / 'locked.pdf'
    pdf_tools.protect(source, str(output), 'reader')
    with pymupdf.open(output) as doc:
        assert doc.authenticate('reader-owner') == 0
        assert doc.authenticate('reader') > 0


def test_save_failure_cleans_private_temp(tmp_path):
    class Broken:
        def save(self, path, **kw):
            Path(path).write_bytes(b'partial')
            raise RuntimeError('failed')
    with pytest.raises(RuntimeError):
        pdf_tools._save(Broken(), tmp_path / 'out.pdf')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('size', [(100000, 100000), (float('inf'), 1), (float('nan'), 1), (-1, 4)])
def test_pixel_limits(size):
    with pytest.raises(ValueError):
        check_pixels(*size)


def test_offline_guard_in_fresh_worker():
    code = """
from eng_viewer.core.netguard import install
install()
import socket
try:
    socket.socket().connect(('1.1.1.1', 53))
except PermissionError:
    pass
else:
    raise AssertionError('network permitted')
import sys
if sys.platform == 'win32':
    # On Windows socketpair() is a loopback TCP connection and is blocked like any socket; nothing
    # in the app uses it. The app's own inter-process channels are named pipes, which still work.
    try:
        socket.socketpair()
    except PermissionError:
        pass
    else:
        raise AssertionError('loopback socket permitted')
    from multiprocessing import Pipe
    a, b = Pipe()
    a.send('ok'); assert b.recv() == 'ok'
else:
    a, b = socket.socketpair()
a.close(); b.close()
"""
    subprocess.run([sys.executable, '-c', code], check=True, timeout=15)


def test_export_allowlist_and_scan():
    import importlib.util
    spec = importlib.util.spec_from_file_location('exporter', Path(__file__).parents[1] / 'tools/export_public_source.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ['audit/report.md', 'reference/original/main.py', '.github/run.py', 'src/private.exe', 'src/link/../../audit/x.py']:
        assert not mod.allowed(name)
    assert mod.allowed('src/eng_viewer/app.py')
    with pytest.raises(ValueError):
        mod.scan('file.py', ('/' + 'Users/' + 'private-person/file').encode())


def test_source_has_no_network_imports():
    import ast
    root = Path(__file__).parents[1] / 'src'
    forbidden = {'socket', 'urllib', 'http', 'requests', 'httpx', 'webbrowser'}
    for path in root.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module or ''] if isinstance(node, ast.ImportFrom) else [])
            assert not any(n.split('.')[0] in forbidden for n in names), path


def test_tool_render_rejects_huge_page_before_allocation():
    from eng_viewer.core.raster_limits import render_page
    class HugePage:
        rect = pymupdf.Rect(0, 0, 10000, 10000)
        def get_pixmap(self, **kw):
            raise AssertionError('allocation must not be attempted')
    with pytest.raises(ValueError):
        render_page(HugePage(), dpi=600)


def test_xlsx_failed_extraction_keeps_output_and_cleans_temp(tmp_path, monkeypatch):
    source = sample(tmp_path / 'in.pdf')
    target = tmp_path / 'out.xlsx'
    target.write_bytes(b'existing')
    def fail(*a, **kw):
        raise RuntimeError('open failed')
    monkeypatch.setattr(pdf_tools, '_open', fail)
    with pytest.raises(RuntimeError):
        pdf_tools.to_excel(source, str(target))
    assert target.read_bytes() == b'existing'
    assert not list(tmp_path.glob('.*.xlsx'))


def test_export_is_deterministic_and_manifest_matches(tmp_path, monkeypatch):
    import importlib.util
    import hashlib
    import json
    spec = importlib.util.spec_from_file_location('exporter_test', Path(__file__).parents[1] / 'tools/export_public_source.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    blobs = {'a': b'licence fixture', 'b': b'print(1)\n', 'c': b'__version__ = "9.8.7"\n'}
    def git(root, *args):
        if args[0] == 'rev-parse':
            return b'123456\n'
        if args[0] == 'ls-tree':
            return (b'100644 blob a\tLICENSE\x00100755 blob b\tsrc/main.py\x00'
                    b'100644 blob c\tsrc/eng_viewer/__init__.py\x00')
        return blobs[args[-1]]
    monkeypatch.setattr(mod, 'git', git)
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/scanner/' + name)
    monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(mod.subprocess, 'check_output', lambda *a, **k: json.dumps([]).encode())
    a, b = tmp_path / 'a.zip', tmp_path / 'b.zip'
    mod.export(tmp_path, 'reviewed', a)
    mod.export(tmp_path, 'reviewed', b)
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as archive:
        top = 'eng-drawing-viewer-9.8.7/'
        assert all(n.startswith(top) for n in archive.namelist())
        manifest = archive.read(top + 'SOURCE_MANIFEST.txt').decode()
        for name in ['LICENSE', 'src/main.py']:
            assert hashlib.sha256(archive.read(top + name)).hexdigest() + '  ' + name in manifest


def test_export_requires_scanners(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('exporter_missing', Path(__file__).parents[1] / 'tools/export_public_source.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    output = tmp_path / 'out.zip'
    with pytest.raises(RuntimeError):
        mod.export(tmp_path, 'HEAD', output)
    assert not output.exists()


def test_raster_ceiling_allows_real_engineering_sizes():
    # A0 at 300 DPI (about 140 MP) is an ordinary scan or export, and must not be refused.
    check_pixels(9933, 14043)
    # The app's own export cap (90 MP) must fit under the ceiling, or exports would fail.
    from eng_viewer.core.exporting import EXPORT_MAX_PIXELS
    from eng_viewer.core.raster_limits import MAX_PIXELS
    assert EXPORT_MAX_PIXELS <= MAX_PIXELS


def test_pdf_to_images_lowers_dpi_for_huge_pages(tmp_path):
    from eng_viewer.core.raster_limits import MAX_PIXELS
    src = tmp_path / 'a0.pdf'
    doc = pymupdf.open()
    doc.new_page(width=2384, height=3370)  # A0 portrait in points
    doc.save(src)
    doc.close()
    result = pdf_tools.to_images(str(src), str(tmp_path / 'out'), fmt='png', dpi=600)
    assert len(result['outputs']) == 1
    assert 'lower resolution' in result['summary']
    pix = pymupdf.Pixmap(result['outputs'][0])
    assert pix.width * pix.height <= MAX_PIXELS
    assert pix.width > 9933  # still above 300 DPI


def test_export_keeps_licence_files_without_extensions():
    import importlib.util
    spec = importlib.util.spec_from_file_location('exporter_names', Path(__file__).parents[1] / 'tools/export_public_source.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ['licenses/curated/MuPDF/thirdparty__brotli__LICENSE', 'licenses/curated/libiconv/COPYING.LIB',
                 'licenses/curated/MuPDF/COPYING', '.gitattributes']:
        assert mod.allowed(name), name
