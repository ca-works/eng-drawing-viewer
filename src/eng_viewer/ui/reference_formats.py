"""Fixed-page, offline previews for open text formats in Standards."""
from __future__ import annotations

import csv
import io
import subprocess
import sys
from math import ceil
from pathlib import Path

from PySide6.QtCore import QMarginsF, QPointF, QRectF, QSizeF
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QColor, QFont, QFontMetricsF, QPageLayout, QPageSize,
    QPainter, QPdfWriter, QTextDocument, QTextLayout, QTextOption,
)

from ..core.reference_text import MAX_TEXT_BYTES, TABLE_EXTS, decode_text

MARGIN = 32
PAGE_HEIGHT = 842
BODY_HEIGHT = PAGE_HEIGHT - 2 * MARGIN
COLUMN_WIDTH = 128
ROW_GUTTER = 38
MAX_PAGES = 3000


class _StaticDocument(QTextDocument):
    def loadResource(self, _kind, _url):
        # Formatted RTF text is allowed. Linked images and other resources are never loaded.
        return None


def _writer(path, width):
    writer = QPdfWriter(str(path))
    writer.setResolution(72)
    page = QPageSize(QSizeF(width, PAGE_HEIGHT), QPageSize.Unit.Point, "", QPageSize.SizeMatchPolicy.ExactMatch)
    writer.setPageLayout(QPageLayout(page, QPageLayout.Orientation.Portrait,
                                    QMarginsF(MARGIN, MARGIN, MARGIN, MARGIN), QPageLayout.Unit.Point))
    return writer


def _next_page(writer, cancelled, count):
    if cancelled.is_set():
        raise ValueError("Preview cancelled.")
    if count > MAX_PAGES:
        raise ValueError(f"This file exceeds the {MAX_PAGES:,}-page preview limit.")
    if count > 1 and not writer.newPage():
        raise ValueError("Could not write the preview PDF.")


def _text_pdf(data, extension, output, cancelled):
    doc = _StaticDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(QFont("Menlo", 10))
    option = doc.defaultTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    doc.setDefaultTextOption(option)
    if extension == ".rtf" and sys.platform != "darwin":
        from ..core.reference_rtf import rtf_text

        doc.setPlainText(rtf_text(data))
    elif extension == ".rtf":
        if not data.lstrip().startswith(b"{\\rtf"):
            raise ValueError("This file does not contain valid RTF.")
        try:
            result = subprocess.run(
                ["/usr/bin/textutil", "-convert", "html", "-format", "rtf", "-noload", "-stdin", "-stdout"],
                input=data, capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            raise ValueError("The macOS RTF reader timed out.") from None
        if result.returncode or not result.stdout:
            raise ValueError("The macOS text reader could not open this RTF.")
        doc.setHtml(result.stdout.decode("utf-8"))
    else:
        doc.setPlainText(decode_text(data))
    width = 595
    body_width = width - 2 * MARGIN
    doc.setPageSize(QSizeF(body_width, BODY_HEIGHT))
    writer = _writer(output, width)
    painter = QPainter(writer)
    if not painter.isActive():
        raise ValueError("Could not create the preview PDF.")
    try:
        layout = doc.documentLayout()
        for i in range(max(1, layout.pageCount())):
            _next_page(writer, cancelled, i + 1)
            painter.save()
            painter.translate(0, -i * BODY_HEIGHT)
            clip = QRectF(0, i * BODY_HEIGHT, body_width, BODY_HEIGHT)
            painter.setClipRect(clip)
            context = QAbstractTextDocumentLayout.PaintContext()
            context.clip = clip
            layout.draw(painter, context)
            painter.restore()
    finally:
        painter.end()


def _rows(text, delimiter):
    return csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)


def _cell_layout(value, font):
    layout = QTextLayout(value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\u2028"), font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    lines = []
    while (line := layout.createLine()).isValid():
        line.setLineWidth(COLUMN_WIDTH - 12)
        line.setPosition(QPointF())
        lines.append(line)
    layout.endLayout()
    return layout, lines


def _table_pdf(text, delimiter, output, cancelled):
    columns = cells = 0
    for record in _rows(text, delimiter):
        if cancelled.is_set():
            raise ValueError("Preview cancelled.")
        columns = max(columns, len(record))
        cells += max(1, len(record))
        if columns > 80 or cells > 450_000:
            raise ValueError("The preview supports up to 80 columns and 450,000 cells.")
    font = QFont("Menlo", 9)
    metrics = QFontMetricsF(font)
    line_height = ceil(metrics.height()) + 2
    max_lines = (BODY_HEIGHT - 8) // line_height
    width = max(420, columns * COLUMN_WIDTH + ROW_GUTTER + 2 * MARGIN)
    writer = _writer(output, width)
    painter = QPainter(writer)
    if not painter.isActive():
        raise ValueError("Could not create the table preview.")
    page = 1
    y = 0
    try:
        for number, record in enumerate(_rows(text, delimiter), 1):
            if cancelled.is_set():
                raise ValueError("Preview cancelled.")
            layouts = [_cell_layout(cell, font) for cell in record]
            lines = max((len(box[1]) for box in layouts), default=1)
            lines = max(1, lines)
            for start in range(0, lines, max_lines):
                length = min(max_lines, lines - start)
                height = length * line_height + 8
                if y + height > BODY_HEIGHT:
                    page += 1
                    _next_page(writer, cancelled, page)
                    y = 0
                painter.setFont(font)
                painter.setPen(QColor("#606878"))
                painter.drawText(QPointF(4, y + 4 + metrics.ascent()), str(number))
                painter.setPen(QColor("#CDD2DA"))
                for col in range(columns + 1):
                    x = ROW_GUTTER + col * COLUMN_WIDTH
                    painter.drawLine(x, y, x, y + height)
                painter.drawLine(0, y + height, ROW_GUTTER + columns * COLUMN_WIDTH, y + height)
                painter.setPen(QColor("#20252D"))
                for col, (_, wrapped) in enumerate(layouts):
                    for i, line in enumerate(wrapped[start:start + length]):
                        line.draw(painter, QPointF(ROW_GUTTER + col * COLUMN_WIDTH + 6, y + 4 + i * line_height))
                y += height
    finally:
        painter.end()


def make_preview(source: Path, output: Path, cancelled) -> str:
    with source.open("rb") as file:
        data = file.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError("This file exceeds the 24 MB text preview limit.")
    ext = source.suffix.casefold()
    try:
        if ext in TABLE_EXTS:
            _table_pdf(decode_text(data), TABLE_EXTS[ext], output, cancelled)
        else:
            _text_pdf(data, ext, output, cancelled)
    except (csv.Error, UnicodeError) as exc:
        raise ValueError(f"Could not read this {ext[1:].upper()} file. {exc}") from None
    return str(output)
