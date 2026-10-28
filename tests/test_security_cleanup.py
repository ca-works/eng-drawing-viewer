"""Adversarial auxiliary-data and external-converter regressions."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pymupdf
import pytest

from eng_viewer.core import pdf_tools as T
from eng_viewer.core import processes

AUX = 'AUXILIARY-PRIVATE-MARKER'


def rich_pdf(path):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), 'SECRET KEEP', fontsize=20)
        page.insert_text((72, 170), 'INVISIBLE-REVIEW', render_mode=3)
        page.add_text_annot((30, 40), AUX)
        page.add_file_annot((30, 80), AUX.encode(), 'private.txt', desc=AUX)
        page.insert_link({'kind': pymupdf.LINK_URI, 'from': pymupdf.Rect(70, 90, 150, 110),
                          'uri': 'https://example.invalid/' + AUX})
        widget = pymupdf.Widget()
        widget.field_name = AUX
        widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        widget.field_value = AUX
        widget.rect = pymupdf.Rect(40, 200, 250, 230)
        page.add_widget(widget)
        doc.set_metadata({'title': AUX, 'author': AUX})
        doc.set_xml_metadata('<private>' + AUX + '</private>')
        doc.set_toc([[1, AUX, 1]])
        doc.embfile_add('private.bin', AUX.encode(), desc=AUX)
        catalog = doc.pdf_catalog()
        action = doc.get_new_xref()
        doc.update_object(action, '<</S/JavaScript/JS' + pymupdf.get_pdf_str(AUX) + '>>')
        doc.xref_set_key(catalog, 'OpenAction', f'{action} 0 R')
        doc.xref_set_key(catalog, 'PieceInfo', '<</Private' + pymupdf.get_pdf_str(AUX) + '>>')
        doc.xref_set_key(page.xref, 'PieceInfo', '<</Private' + pymupdf.get_pdf_str(AUX) + '>>')
        doc.save(path)
    return str(path)


def all_decoded_objects(doc):
    parts = []
    for xref in range(1, doc.xref_length()):
        parts.append(doc.xref_object(xref).encode())
        if doc.xref_is_stream(xref):
            parts.append(doc.xref_stream(xref))
    return b'\n'.join(parts)


@pytest.mark.parametrize('operation', ['metadata', 'redact', 'redact_no_match'])
def test_removes_auxiliary_channels_and_preserves_original(tmp_path, operation):
    source = tmp_path / 'source.pdf'
    rich_pdf(source)
    original = source.read_bytes()
    output = tmp_path / 'clean.pdf'
    if operation == 'metadata':
        result = T.metadata(str(source), str(output), strip=True)
    else:
        result = T.redact(str(source), str(output), terms='SECRET' if operation == 'redact' else 'ABSENT')
    assert source.read_bytes() == original
    with pymupdf.open(output) as doc:
        assert not doc.metadata.get('title') and not doc.metadata.get('author')
        assert not doc.get_xml_metadata()
        assert doc.get_toc() == []
        assert doc.embfile_count() == 0
        assert list(doc[0].annots() or ()) == []
        assert list(doc[0].widgets() or ()) == []
        assert doc[0].get_links() == []
        assert AUX.encode() not in all_decoded_objects(doc)
        text = doc[0].get_text()
        assert 'KEEP' in text
        assert ('SECRET' not in text) == (operation == 'redact')
        # Explicit limitation: metadata cleanup does not erase page text layers.
        assert 'INVISIBLE-REVIEW' in text
    if operation == 'redact_no_match':
        assert 'unchanged' not in result['summary']
        assert 'No searchable text matched' in result['summary']


def test_edit_metadata_preserves_comments_and_attachments(tmp_path):
    source = rich_pdf(tmp_path / 'source.pdf')
    output = tmp_path / 'edited.pdf'
    T.metadata(source, str(output), title='Updated')
    with pymupdf.open(output) as doc:
        assert doc.metadata['title'] == 'Updated'
        assert doc.embfile_count() == 1
        assert doc.get_toc()
        assert list(doc[0].annots())


def test_cleanup_failure_is_not_silently_accepted(tmp_path, monkeypatch):
    source = rich_pdf(tmp_path / 'source.pdf')
    output = tmp_path / 'out.pdf'
    output.write_bytes(b'existing output')
    def fail(*a, **kw):
        raise RuntimeError('scrub failed')
    monkeypatch.setattr(pymupdf.Document, 'scrub', fail)
    with pytest.raises(RuntimeError, match='scrub failed'):
        T.metadata(source, str(output), strip=True)
    assert output.read_bytes() == b'existing output'


def test_redaction_removes_intersecting_line_art(tmp_path):
    source = tmp_path / 'source.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), 'SECRET', fontsize=20)
        page.draw_line((60, 95), (200, 95))
        page.draw_line((60, 300), (200, 300))
        doc.save(source)
    output = tmp_path / 'out.pdf'
    T.redact(str(source), str(output), terms='SECRET')
    with pymupdf.open(output) as doc:
        lines = [d for d in doc[0].get_drawings() if any(item[0] == 'l' for item in d['items'])]
        assert len(lines) == 1
        assert lines[0]['rect'].y0 == 300


def test_office_absolute_arguments_atomic_destination_and_unique_workdirs(tmp_path, monkeypatch):
    inputs = [tmp_path / 'a' / 'same.docx', tmp_path / 'b' / 'same.docx']
    for p in inputs:
        p.parent.mkdir()
        p.write_text('fixture')
    monkeypatch.setattr(T, 'find_office', lambda: sys.executable)
    calls = []
    def convert(command, timeout):
        assert Path(command[0]).is_absolute()
        assert Path(command[-1]).is_absolute()
        assert timeout == 300
        work = Path(command[command.index('--outdir') + 1])
        calls.append(work)
        with pymupdf.open() as doc:
            doc.new_page()
            doc.save(work / 'same.pdf')
        return 0
    monkeypatch.setattr(T, 'run_conversion', convert)
    output = tmp_path / 'outputs'
    result = T.office_to_pdf([str(p) for p in inputs], str(output))
    assert len(set(calls)) == 2
    assert len(result['outputs']) == 2
    assert all(Path(p).is_file() for p in result['outputs'])
    assert all(not p.exists() for p in calls)
    assert not list(output.glob('.*.tmp'))


def test_office_timeout_reports_failure_without_output(tmp_path, monkeypatch):
    monkeypatch.setattr(T, 'find_office', lambda: sys.executable)
    workdirs = []
    def timeout(command, timeout):
        workdirs.append(Path(command[command.index('--outdir') + 1]))
        raise subprocess.TimeoutExpired(command, timeout)
    monkeypatch.setattr(T, 'run_conversion', timeout)
    output = tmp_path / 'outputs'
    with pytest.raises(T.ToolError, match='timed out'):
        T.office_to_pdf([str(tmp_path / 'input.docx')], str(output))
    assert list(output.iterdir()) == []
    assert all(not p.exists() for p in workdirs)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX session behavior')
def test_timeout_kills_spawned_descendant(tmp_path):
    pidfile = tmp_path / 'child.pid'
    code = ("import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "open(sys.argv[1],'w').write(str(child.pid)); time.sleep(60)")
    with pytest.raises(subprocess.TimeoutExpired):
        processes.run_conversion([sys.executable, '-c', code, str(pidfile)], timeout=1)
    child = int(pidfile.read_text())
    for _ in range(100):
        result = subprocess.run(['/bin/ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True)
        if result.returncode != 0 or result.stdout.strip().startswith('Z'):
            break
        time.sleep(.02)
    else:
        os.kill(child, 9)
        pytest.fail('contained child survived conversion timeout')


def test_windows_assignment_precedes_resume_and_job_closes(monkeypatch):
    events = []
    fake_modules = {
        'win32api': SimpleNamespace(CloseHandle=lambda h: events.append(('close', h))),
        'win32con': SimpleNamespace(CREATE_SUSPENDED=4, CREATE_NO_WINDOW=8, STILL_ACTIVE=259),
        'win32event': SimpleNamespace(WAIT_TIMEOUT=258, WAIT_OBJECT_0=0,
            WaitForSingleObject=lambda *a: 258),
        'win32job': SimpleNamespace(JobObjectExtendedLimitInformation=9,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=8192,
            CreateJobObject=lambda *a: 'job',
            QueryInformationJobObject=lambda *a: {'BasicLimitInformation': {'LimitFlags': 0}},
            SetInformationJobObject=lambda *a: events.append(('limits', a[-1])),
            AssignProcessToJobObject=lambda *a: events.append(('assign', a)),
            TerminateJobObject=lambda *a: events.append(('terminate_job', a))),
        'win32process': SimpleNamespace(STARTUPINFO=lambda: None,
            CreateProcess=lambda *a: (events.append(('create', a)) or ('process', 'thread', 1, 2)),
            ResumeThread=lambda h: events.append(('resume', h)),
            GetExitCodeProcess=lambda *a: 1,
            TerminateProcess=lambda *a: events.append(('terminate_process', a))),
    }
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    with pytest.raises(subprocess.TimeoutExpired):
        processes._run_windows(['converter.exe', 'input'], .1)
    kinds = [event[0] for event in events]
    assert kinds.index('assign') < kinds.index('resume')
    assert 'terminate_job' in kinds
    assert [e[1] for e in events if e[0] == 'close'] == ['thread', 'process', 'job']
    assert next(e[1] for e in events if e[0] == 'create')[5] == 12
