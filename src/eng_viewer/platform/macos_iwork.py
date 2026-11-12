"""Local iWork preview, export handoff, and explicit Keynote insertion."""
from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

APPS = {"Pages": "com.apple.iWork.Pages", "Numbers": "com.apple.iWork.Numbers",
        "Keynote": "com.apple.iWork.Keynote"}
DOCUMENT_APPS = {".pages": "Pages", ".numbers": "Numbers", ".key": "Keynote"}
MAX_COPY_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 10_000


def application_url(app):
    from AppKit import NSWorkspace

    return NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(APPS[app])


def open_in_app(app, path, completed):
    from AppKit import NSWorkspace, NSWorkspaceOpenConfiguration
    from Foundation import NSURL

    url = application_url(app)
    if url is None:
        completed(f"Install {app} to open this file.")
        return
    workspace = NSWorkspace.sharedWorkspace()
    config = NSWorkspaceOpenConfiguration.configuration()
    workspace.openURLs_withApplicationAtURL_configuration_completionHandler_(
        [NSURL.fileURLWithPath_(str(path))], url, config,
        lambda _app, error: completed(str(error.localizedDescription()) if error is not None else ""))


def _run_script(app, body, args, task):
    if application_url(app) is None:
        raise ValueError(f"Install {app} to {task}.")
    script = (
        'on run argv\nwith timeout of 45 seconds\n'
        f'tell application id "{APPS[app]}"\n{body}\nend tell\nend timeout\nend run'
    )
    try:
        result = subprocess.run(["/usr/bin/osascript", "-", *map(str, args)], input=script,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=65)
    except subprocess.TimeoutExpired:
        raise ValueError(f"{app} did not respond. Check it for a dialog and try again.") from None
    if result.returncode:
        match = re.search(r"\((-?\d+)\)\s*$", result.stderr)
        code = int(match[1]) if match else 0
        if code == -1743:
            raise ValueError(f"Automation access to {app} was denied. Allow ENG Drawing Viewer under "
                             "System Settings › Privacy & Security › Automation and try again.")
        if code == 1001:
            raise ValueError("Open a Keynote presentation and select a slide before sending drawings.")
        if code == 1002:
            raise ValueError("The selected Keynote presentation or slide changed. Try again from the intended slide.")
        if code == 1003:
            raise ValueError("Stop the Keynote slideshow before sending drawings.")
        if code == -1712:
            raise ValueError(f"{app} took too long to respond. Check it for a dialog and try again.")
        raise ValueError(f"{app} could not {task}." + (f" Apple Events error {code}." if code else ""))
    return result.stdout.strip()


def _copy_reference(source, target):
    """Copy a flat document or a package of regular files, with a bounded scratch footprint."""
    pending = [(source, target)]
    total = entries = 0
    while pending:
        origin, dest = pending.pop()
        info = origin.lstat()
        entries += 1
        if entries > MAX_PACKAGE_ENTRIES:
            raise ValueError("This iWork package contains too many entries.")
        if stat.S_ISDIR(info.st_mode):
            dest.mkdir()
            with os.scandir(origin) as children:
                for child in children:
                    if entries + len(pending) >= MAX_PACKAGE_ENTRIES:
                        raise ValueError("This iWork package contains too many entries.")
                    pending.append((Path(child.path), dest / child.name))
        elif stat.S_ISREG(info.st_mode):
            with os.fdopen(os.open(origin, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as src:
                if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                    raise ValueError("This iWork package contains a non-regular file.")
                with dest.open("xb") as out:
                    while data := src.read(1024 * 1024):
                        total += len(data)
                        if total > MAX_COPY_BYTES:
                            raise ValueError("This iWork document exceeds the 512 MB preview limit.")
                        out.write(data)
        else:
            raise ValueError("This iWork package contains a link or unsupported entry.")


def preview(source, out_dir):
    source, folder = Path(source), Path(out_dir)
    app = DOCUMENT_APPS[source.suffix.casefold()]
    # The app opens only the disposable copy. In particular, iWork autosave cannot touch the original.
    # An alias lets macOS pass file access to the sandboxed iWork app for the existing copy.
    clone = folder / source.name
    output = folder / "Reference.pdf"
    try:
        _copy_reference(source, clone)
        settings = " with properties {skipped slides:true}" if app == "Keynote" else ""
        _run_script(app, f"""
        set inputAlias to (POSIX file (item 1 of argv)) as alias
        set copiedDocument to open inputAlias
        if class of copiedDocument is list then set copiedDocument to item 1 of copiedDocument
        try
            export copiedDocument to (POSIX file (item 2 of argv)) as PDF{settings}
        on error messageText number errorCode
            try
                close copiedDocument saving no
            end try
            error messageText number errorCode
        end try
        close copiedDocument saving no
        """, [clone, output], "create a PDF preview of this document")
    finally:
        if clone.is_dir():
            shutil.rmtree(clone, ignore_errors=True)
        else:
            clone.unlink(missing_ok=True)
    if not output.is_file() or output.stat().st_size == 0:
        raise ValueError(f"{app} did not produce a readable PDF preview.")
    return {"outputs": [str(output)]}


_KEYNOTE_TARGET = """
    if playing then error number 1003
    if (count documents) is 0 then error number 1001
    set targetDocument to front document
    set anchorSlide to current slide of targetDocument
    if anchorSlide is missing value then error number 1001
    if skipped of anchorSlide then error number 1001
"""


def keynote_target():
    target = _run_script("Keynote", _KEYNOTE_TARGET + """
        return (id of targetDocument as text) & tab & (slide number of anchorSlide as text) & tab ¬
            & (width of targetDocument as text) & tab & (height of targetDocument as text)
        """, [], "read the selected slide")
    try:
        identifier, number, width, height = target.split("\t")
        return {"id": identifier, "slide": int(number), "width": int(width), "height": int(height)}
    except ValueError:
        raise ValueError("Keynote did not report a usable slide size and selection.") from None


def keynote_insert(target, slides):
    sw, sh = target["width"], target["height"]
    if min(sw, sh) < 1:
        raise ValueError("Keynote reported an invalid slide size.")
    # Pass values as argv, never as AppleScript source. Images are embedded by Keynote.
    args = [target["id"], target["slide"], sw, sh]
    for slide in slides:
        iw, ih = slide["img_w"], slide["img_h"]
        scale = min(0.9 * sw / iw, 0.9 * sh / ih)
        w, h = max(1, round(iw * scale)), max(1, round(ih * scale))
        notes = "\n".join(filter(None, [slide["title"], *slide["comments"]]))
        args.extend([slide["image"], w, h, (sw - w) // 2, (sh - h) // 2, notes])
    _run_script("Keynote", _KEYNOTE_TARGET + """
        if (id of targetDocument as text) is not (item 1 of argv) then error number 1002
        if slide number of anchorSlide is not ((item 2 of argv) as integer) then error number 1002
        if width of targetDocument is not ((item 3 of argv) as integer) then error number 1002
        if height of targetDocument is not ((item 4 of argv) as integer) then error number 1002
        set previousLayout to base layout of anchorSlide
        set insertedSlides to {}
        try
            repeat with i from 5 to (count argv) by 6
                set imageFile to (POSIX file (item i of argv)) as alias
                set imageWidth to (item (i + 1) of argv) as integer
                set imageHeight to (item (i + 2) of argv) as integer
                set imagePosition to {(item (i + 3) of argv) as integer, (item (i + 4) of argv) as integer}
                tell targetDocument
                    set addedSlide to make new slide at after anchorSlide with properties ¬
                        {base layout:previousLayout, title showing:false, body showing:false}
                end tell
                set end of insertedSlides to addedSlide
                tell addedSlide
                    set placedImage to make new image with properties {file:imageFile}
                    set width of placedImage to imageWidth
                    set height of placedImage to imageHeight
                    set position of placedImage to imagePosition
                    set presenter notes to (item (i + 5) of argv)
                end tell
                set anchorSlide to addedSlide
            end repeat
        on error messageText number errorCode
            repeat with addedSlide in (reverse of insertedSlides)
                try
                    delete (contents of addedSlide)
                end try
            end repeat
            error messageText number errorCode
        end try
        """, args, "insert the drawings into the selected presentation")
    return {"slides": len(slides), "live": True}


def file_handoff(app, spec):
    """Create a local import file without replacing an existing destination."""
    import tempfile

    output = Path(spec["output"])
    with tempfile.TemporaryDirectory(prefix=".eng-send-", dir=output.parent) as folder:
        temporary = Path(folder) / output.name
        if app == "Keynote":
            from ..core.pptx_export import build

            build({**spec, "mode": "new", "output": str(temporary)})
        elif app == "Pages":
            _pages_document(temporary, spec["slides"])
        elif app == "Numbers" and "table" in spec:
            _numbers_table(temporary, Path(spec["table"]))
        else:
            raise ValueError("This content has no compatible iWork export.")
        try:
            os.link(temporary, output)
        except FileExistsError:
            raise ValueError("A file already exists at this location. Choose a new filename.") from None
        except OSError as exc:
            if exc.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM):
                raise
            # FAT and some removable filesystems cannot create hard links.
            try:
                destination = output.open("xb")
            except FileExistsError:
                raise ValueError("A file already exists at this location. Choose a new filename.") from None
            try:
                with destination, temporary.open("rb") as source:
                    shutil.copyfileobj(source, destination)
            except OSError:
                output.unlink(missing_ok=True)
                raise
    return {"path": str(output), "slides": len(spec.get("slides", ()))}


def _pages_document(output, slides):
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    section = doc.sections[0]
    page_w = (section.page_width - section.left_margin - section.right_margin) / Inches(1)
    page_h = (section.page_height - section.top_margin - section.bottom_margin) / Inches(1)
    for index, slide in enumerate(slides):
        if index:
            doc.add_page_break()
        if slide["title"]:
            doc.add_heading(slide["title"], 1)
        w, h = slide["img_w"], slide["img_h"]
        reserve = 0.8 + (1.0 if slide["comments"] else 0.0)
        scale = min(page_w / w, (page_h - reserve) / h)
        doc.add_picture(slide["image"], width=Inches(w * scale), height=Inches(h * scale))
        for comment in slide["comments"]:
            doc.add_paragraph(comment)
    doc.save(output)


def _numbers_table(output, source):
    import csv
    import io

    import xlsxwriter

    # Use the same encoding and CSV rules as the preview. Write literal strings,
    # including leading zeroes and formula-like input, into the workbook.
    from ..core.reference_text import decode_text, MAX_TEXT_BYTES, TABLE_EXTS

    with source.open("rb") as file:
        data = file.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError("This table exceeds the 24 MB import limit.")
    with xlsxwriter.Workbook(str(output), {"constant_memory": True}) as book:
        sheet = book.add_worksheet("Data")
        reader = csv.reader(io.StringIO(decode_text(data), newline=""),
                            delimiter=TABLE_EXTS[source.suffix.casefold()], strict=True)
        for r, cells in enumerate(reader):
            for c, value in enumerate(cells):
                if sheet.write_string(r, c, value) != 0:
                    raise ValueError("A row, column, or field exceeds the workbook format limits.")
