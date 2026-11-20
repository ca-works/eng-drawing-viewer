"""Read-only XLSX values for Reference, using the bundled safe XML parser."""
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
import math
from pathlib import PurePosixPath
import posixpath
import re
import sqlite3
from zipfile import ZipFile

from defusedxml import ElementTree as XML

from .reference_office import MAX_UNPACKED


@dataclass(frozen=True)
class Workbook:
    sheets: tuple[tuple[str, str], ...]
    styles: tuple[str, ...]
    strings: str | None
    date_1904: bool


BUILTIN_FORMATS = {
    1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00",
    9: "0%", 10: "0.00%", 11: "0.00E+00",
    14: "m/d/yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm",
    21: "h:mm:ss", 22: "m/d/yy h:mm", 45: "mm:ss", 46: "[h]:mm:ss", 47: "mm:ss.0",
}
CELL_REF = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})$")
MAX_ROWS, MAX_COLUMNS = 1_048_576, 16_384


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(node, name):
    return next((child for child in node if _local(child.tag) == name), None)


@contextmanager
def _archive(path):
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 20000 or sum(entry.file_size for entry in entries) > MAX_UNPACKED:
            raise ValueError("This workbook exceeds the offline preview size limit.")
        yield archive


def _read_xml(archive, part):
    with archive.open(part) as stream:
        return XML.parse(stream, forbid_dtd=True).getroot()


def _relationships(archive, source=""):
    parent, name = posixpath.split(source)
    rels = posixpath.join(parent, "_rels", name + ".rels")
    result = {}
    for link in _read_xml(archive, rels):
        target = link.get("Target", "")
        if link.get("TargetMode") == "External" or not target or "://" in target or "\\" in target:
            continue
        part = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(parent, target))
        if part == ".." or ".." in PurePosixPath(part).parts:
            continue
        result[link.get("Id")] = (link.get("Type", "").rsplit("/", 1)[-1], part)
    return result


def read_workbook(path):
    with _archive(path) as archive:
        root = _relationships(archive)
        part = next((target for kind, target in root.values() if kind == "officeDocument"), None)
        if part is None:
            raise ValueError("The workbook document is missing.")
        book = _read_xml(archive, part)
        rels = _relationships(archive, part)
        by_kind = dict(rels.values())
        sheets = []
        for sheet in _child(book, "sheets"):
            link = next((value for key, value in sheet.attrib.items() if key.endswith("}id")), "")
            kind, target = rels.get(link, ("", ""))
            if kind == "worksheet" and sheet.get("state", "visible") == "visible":
                sheets.append((sheet.get("name", "Worksheet"), target))
        if not sheets:
            raise ValueError("This workbook has no visible worksheets.")
        props = _child(book, "workbookPr")
        date_1904 = props is not None and props.get("date1904") in ("1", "true")
        styles = []
        if "styles" in by_kind:
            root = _read_xml(archive, by_kind["styles"])
            custom = _child(root, "numFmts")
            formats = dict(BUILTIN_FORMATS)
            if custom is not None:
                formats.update((int(node.get("numFmtId")), node.get("formatCode", "")) for node in custom)
            xfs = _child(root, "cellXfs")
            if xfs is not None:
                styles = [formats.get(int(node.get("numFmtId", "0")), "") for node in xfs]
        return Workbook(tuple(sheets), tuple(styles), by_kind.get("sharedStrings"), date_1904)


def column_name(column):
    name = ""
    while column:
        column, digit = divmod(column - 1, 26)
        name = chr(ord("A") + digit) + name
    return name


def _coordinates(address):
    match = CELL_REF.fullmatch(address)
    if not match:
        raise ValueError("The worksheet has an invalid cell address.")
    column = 0
    for digit in match[1]:
        column = 26 * column + ord(digit) - ord("A") + 1
    row = int(match[2])
    if row > MAX_ROWS or column > MAX_COLUMNS:
        raise ValueError("The worksheet exceeds Excel's grid limits.")
    return row, column


def _text(node):
    if node is None:
        return ""
    pieces = []
    for child in node:
        element = child if _local(child.tag) == "t" else _child(child, "t") if _local(child.tag) == "r" else None
        if element is not None:
            pieces.append(element.text or "")
    return "".join(pieces)


def _elements(archive, part, name, parent, cancelled):
    """Stream records and discard each parsed element as the reader moves on."""
    with archive.open(part) as stream:
        stack = []
        for event, element in XML.iterparse(stream, events=("start", "end"), forbid_dtd=True):
            if cancelled.is_set():
                raise InterruptedError("Workbook preview closed.")
            if event == "start":
                stack.append(element)
                continue
            if len(stack) > 1:
                if _local(element.tag) == name and _local(stack[-2].tag) == parent:
                    yield element
                    stack[-2].remove(element)
                elif not any(_local(ancestor.tag) == name for ancestor in stack[:-1]):
                    stack[-2].remove(element)
            stack.pop()


@lru_cache(maxsize=256)
def _format_tokens(code):
    return re.sub(r'"[^"]*"|\\.|_.|\*.', "", code.split(";")[0]).lower()


def _number(raw, code, date_1904):
    tokens = _format_tokens(code)
    if not tokens or tokens == "general":
        return raw
    try:
        value = float(raw)
        if not math.isfinite(value):
            return raw
        if re.search(r"\[(h+|m+|s+)\]", tokens):
            seconds = round(value * 86400)
            minutes, seconds = divmod(abs(seconds), 60)
            hours, minutes = divmod(minutes, 60)
            return f"{'-' if value < 0 else ''}{hours}:{minutes:02}:{seconds:02}"
        tokens = re.sub(r"\[[^]]*\]", "", tokens)
        has_time = bool(re.search(r"h|s|am/pm", tokens))
        has_date = bool(re.search(r"y|d", tokens)) or ("m" in tokens and not has_time)
        if has_date or has_time:
            epoch = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 31)
            days = value - (not date_1904 and value >= 60)
            stamp = epoch + timedelta(days=days)
            date = "1900-02-29" if not date_1904 and 60 <= value < 61 else stamp.date().isoformat()
            clock = stamp.time().isoformat(timespec="seconds")
            return f"{date} {clock}" if has_date and has_time else date if has_date else clock
        match = re.search(r"([#0,]+)(?:\.([#0]+))?(?:e[+-]0+)?", tokens)
        if match:
            places = min(15, len(match[2] or ""))
            if "%" in tokens:
                return f"{value:.{places}%}"
            if "e+" in match[0] or "e-" in match[0]:
                return f"{value:.{places}E}"
            result = f"{value:,.{places}f}" if "," in match[1] else f"{value:.{places}f}"
            if match[2] and match[2].endswith("#"):
                optional = len(match[2]) - len(match[2].rstrip("#"))
                while optional and result.endswith("0"):
                    result = result[:-1]
                    optional -= 1
                result = result.rstrip(".")
            return result
    except (ValueError, OverflowError):
        pass
    return raw


def _value(cell, book, shared_string):
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        return _text(_child(cell, "is"))
    value = _child(cell, "v")
    raw = value.text if value is not None else None
    if raw is None:
        if value is not None and kind == "str":
            return ""
        formula = _child(cell, "f")
        return "Result unavailable" if formula is not None else ""
    if kind == "s":
        return shared_string(int(raw))
    if kind == "b":
        return "TRUE" if raw == "1" else "FALSE"
    if kind == "d":
        return raw.replace("T", " ", 1)
    if kind == "n":
        style = int(cell.get("s", "0"))
        if 0 <= style < len(book.styles):
            return _number(raw, book.styles[style], book.date_1904)
    return raw


def index_sheet(source, book, index, database, cancelled):
    """Index just the requested sheet on a worker. Keep cell storage off the GUI heap."""
    with _archive(source) as archive, closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE cells (r INTEGER, c INTEGER, value TEXT, PRIMARY KEY (r,c)) WITHOUT ROWID")
        if book.strings:
            db.execute("CREATE TEMP TABLE shared (id INTEGER PRIMARY KEY, value TEXT)")
            strings = _elements(archive, book.strings, "si", "sst", cancelled)
            db.executemany("INSERT INTO shared VALUES (?,?)", ((i, _text(item)) for i, item in enumerate(strings)))

        @lru_cache(maxsize=1024)
        def shared_string(index):
            if book.strings is None:
                return ""
            value = db.execute("SELECT value FROM shared WHERE id=?", (index,)).fetchone()
            return value[0] if value else ""

        last_row = last_column = 0

        def records():
            nonlocal last_row, last_column
            row_index = 0
            for row in _elements(archive, book.sheets[index][1], "row", "sheetData", cancelled):
                row_index = int(row.get("r", row_index + 1))
                column = 0
                for cell in row:
                    if _local(cell.tag) != "c":
                        continue
                    address = cell.get("r")
                    if address:
                        current_row, column = _coordinates(address)
                    else:
                        current_row, column = row_index, column + 1
                    if not (1 <= current_row <= MAX_ROWS and 1 <= column <= MAX_COLUMNS):
                        raise ValueError("The worksheet exceeds Excel's grid limits.")
                    value = _value(cell, book, shared_string)
                    if value:
                        last_row = max(last_row, current_row)
                        last_column = max(last_column, column)
                        yield current_row, column, value

        db.executemany("INSERT OR REPLACE INTO cells VALUES (?,?,?)", records())
    return max(1, last_row), max(1, last_column)
