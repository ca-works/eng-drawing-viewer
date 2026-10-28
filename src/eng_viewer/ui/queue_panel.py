"""PowerPoint export queue: reorderable list of pages and MARKed pages."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QListView, QMenu, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from ..core.documents import is_document_name
from . import icons
from .theme import ThemeManager, qc
from .thumbnails import ThumbnailProvider
from .widgets import SectionHeader, label, primary_button, secondary_button, tool_button

_ids = itertools.count(1)
MIME = "application/x-eng-viewer-queue-rows"


def dropped_documents(event) -> list[str]:
    """PDFs and images dragged in from Finder / Explorer (none for internal moves)."""
    md = event.mimeData()
    if event.source() is not None or not md.hasUrls():
        return []
    return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and is_document_name(u.toLocalFile())
            and Path(u.toLocalFile()).is_file()]


@dataclass
class QueueEntry:
    doc_path: str
    page_index: int
    display_name: str = ""
    marked_image: QImage | None = None
    comments: str = ""
    comment_count: int = 0
    preview: QPixmap | None = None
    entry_id: int = field(default_factory=lambda: next(_ids))

    def __post_init__(self):
        if not self.display_name:
            self.display_name = Path(self.doc_path).name

    @property
    def marked(self) -> bool:
        return self.marked_image is not None


class QueueModel(QAbstractListModel):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[QueueEntry] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return e.display_name
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = f"{e.doc_path}\nPage {e.page_index + 1}"
            if e.marked:
                tip += f"\nMARKed page with {e.comment_count} comment(s)"
            return tip
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{e.display_name}, page {e.page_index + 1}{', marked' if e.marked else ''}"
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        return base if index.isValid() else Qt.ItemFlag.ItemIsDropEnabled

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def mimeTypes(self):
        return [MIME]

    def mimeData(self, indexes):
        md = QMimeData()
        md.setData(MIME, ",".join(str(i.row()) for i in indexes).encode())
        return md

    def add(self, entries: list[QueueEntry]):
        if not entries:
            return
        n = len(self.entries)
        self.beginInsertRows(QModelIndex(), n, n + len(entries) - 1)
        self.entries.extend(entries)
        self.endInsertRows()
        self.changed.emit()

    def remove_rows(self, rows: list[int]):
        for r in sorted(set(rows), reverse=True):
            if 0 <= r < len(self.entries):
                self.beginRemoveRows(QModelIndex(), r, r)
                del self.entries[r]
                self.endRemoveRows()
        self.changed.emit()

    def clear(self):
        self.beginResetModel()
        self.entries.clear()
        self.endResetModel()
        self.changed.emit()

    def move_rows(self, rows: list[int], dest: int) -> list[int]:
        """Move ``rows`` (any order) so they start at ``dest``. Returns new rows."""
        rows = sorted(set(r for r in rows if 0 <= r < len(self.entries)))
        if not rows:
            return []
        moving = [self.entries[r] for r in rows]
        dest -= sum(1 for r in rows if r < dest)
        self.beginResetModel()
        for r in reversed(rows):
            del self.entries[r]
        dest = max(0, min(dest, len(self.entries)))
        self.entries[dest:dest] = moving
        self.endResetModel()
        self.changed.emit()
        return list(range(dest, dest + len(moving)))


class QueueDelegate(QStyledItemDelegate):
    ROW_H = 54

    def __init__(self, view: QListView, model: QueueModel, provider: ThumbnailProvider):
        super().__init__(view)
        self.view, self.model, self.provider = view, model, provider

    def sizeHint(self, option, index):
        return QSize(100, self.ROW_H)

    def paint(self, painter: QPainter, option, index):
        t = ThemeManager.current()
        e = self.model.entries[index.row()]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(6, 1, -6, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qc(t.selected_strong if selected and self.view.hasFocus() else (t.selected if selected else t.hover)))
            painter.drawRoundedRect(r, 6, 6)
        f = QFont(option.font)
        small = QFont(f)
        small.setPointSizeF(max(8.5, f.pointSizeF() - 2))
        sfm = QFontMetrics(small)
        # Position number
        painter.setFont(small)
        painter.setPen(qc(t.text_tertiary))
        painter.drawText(QRectF(r.left() + 2, r.top(), 22, r.height()), Qt.AlignmentFlag.AlignCenter, str(index.row() + 1))
        # Thumbnail
        box = QRectF(r.left() + 26, r.top() + 6, 58, r.height() - 12)
        pm = e.preview if e.marked else self.provider.get(e.doc_path, e.page_index, box.height() * self.view.devicePixelRatioF())
        if pm is not None and not pm.isNull():
            s = min(box.width() / pm.width(), box.height() / pm.height())
            tr = QRectF(0, 0, pm.width() * s, pm.height() * s)
            tr.moveCenter(box.center())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 40 if t.dark else 22))
            painter.drawRoundedRect(tr.adjusted(-1, -0.5, 1, 1.5), 2, 2)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(tr, pm, QRectF(pm.rect()))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qc(t.hover))
            painter.drawRoundedRect(box, 3, 3)
        # Text
        x = box.right() + 10
        w = r.right() - x - 6
        fm = QFontMetrics(f)
        painter.setFont(f)
        painter.setPen(qc(t.text))
        painter.drawText(QRectF(x, r.top() + 8, w, fm.height()), Qt.AlignmentFlag.AlignVCenter,
                         fm.elidedText(e.display_name, Qt.TextElideMode.ElideMiddle, int(w)))
        painter.setFont(small)
        y2 = r.top() + 10 + fm.height()
        sub = f"Page {e.page_index + 1}"
        cx = x
        if e.marked:
            bf = QFont(small)
            bf.setWeight(QFont.Weight.Bold)
            bfm = QFontMetrics(bf)
            bw = bfm.horizontalAdvance("MARKED") + 10
            badge = QRectF(cx, y2 + 1, bw, sfm.height())
            painter.setPen(Qt.PenStyle.NoPen)
            accent = qc(t.danger)
            accent.setAlphaF(0.16)
            painter.setBrush(accent)
            painter.drawRoundedRect(badge, 4, 4)
            painter.setFont(bf)
            painter.setPen(qc(t.danger))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "MARKED")
            painter.setFont(small)
            cx = badge.right() + 6
            if e.comment_count:
                sub += f" · {e.comment_count} comment{'s' if e.comment_count != 1 else ''}"
        painter.setPen(qc(t.text_secondary))
        painter.drawText(QRectF(cx, y2, r.right() - cx - 6, sfm.height() + 2), Qt.AlignmentFlag.AlignVCenter,
                         sfm.elidedText(sub, Qt.TextElideMode.ElideRight, int(r.right() - cx - 6)))
        painter.restore()


class QueueView(QListView):
    deletePressed = Signal()
    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Queue")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def dragEnterEvent(self, event):
        if dropped_documents(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if dropped_documents(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        files = dropped_documents(event)
        if files:
            event.acceptProposedAction()
            self.filesDropped.emit(files)
            return
        if event.source() is not self or not event.mimeData().hasFormat(MIME):
            event.ignore()
            return
        rows = [int(x) for x in bytes(event.mimeData().data(MIME)).decode().split(",") if x]
        idx = self.indexAt(event.position().toPoint())
        model: QueueModel = self.model()
        if idx.isValid():
            rect = self.visualRect(idx)
            dest = idx.row() + (1 if event.position().y() > rect.center().y() else 0)
        else:
            dest = model.rowCount()
        new_rows = model.move_rows(rows, dest)
        sel = self.selectionModel()
        sel.clearSelection()
        for r in new_rows:
            sel.select(model.index(r), sel.SelectionFlag.Select)
        # The model already moved the rows; stop Qt from deleting the "source".
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.deletePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class QueuePanel(QWidget):
    addCurrentRequested = Signal()
    addSelectedRequested = Signal()
    addAllRequested = Signal()
    exportRequested = Signal()
    exportPdfRequested = Signal()
    showEntryRequested = Signal(object)  # QueueEntry
    filesDropped = Signal(list)  # PDFs / images dropped from the file manager: all their pages are queued

    def __init__(self, provider: ThumbnailProvider, parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.provider = provider
        self.model = QueueModel(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = SectionHeader("PowerPoint queue")
        add_btn = tool_button("plus", "Add to queue", size=24, icon_size=16)
        add_btn.setPopupMode(add_btn.ToolButtonPopupMode.InstantPopup)
        m = QMenu(add_btn)
        self.act_add_current = m.addAction("Current Page", self.addCurrentRequested.emit)
        self.act_add_selected = m.addAction("Selected PDFs (all pages)", self.addSelectedRequested.emit)
        self.act_add_all = m.addAction("All PDFs in Library (all pages)", self.addAllRequested.emit)
        add_btn.setMenu(m)
        self.header.add_trailing(add_btn)
        self.clear_btn = tool_button("trash", "Clear queue", size=24, icon_size=16)
        self.clear_btn.clicked.connect(self._confirm_clear)
        self.header.add_trailing(self.clear_btn)
        lay.addWidget(self.header)

        self.view = QueueView()
        self.view.setModel(self.model)
        self.delegate = QueueDelegate(self.view, self.model, provider)
        self.view.setItemDelegate(self.delegate)
        self.view.deletePressed.connect(self.remove_selected)
        self.view.filesDropped.connect(self.filesDropped.emit)
        self.setAcceptDrops(True)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(lambda idx: self.showEntryRequested.emit(self.model.entries[idx.row()]))
        lay.addWidget(self.view, 1)

        self.empty = label("Add the current page, selected files or MARKed pages (or drop files here), then export them "
                           "as PowerPoint slides or one PDF.", "tertiary", wrap=True)
        self.empty.setContentsMargins(14, 0, 14, 10)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.empty)

        foot = QHBoxLayout()
        foot.setContentsMargins(10, 6, 10, 10)
        self.export_btn = primary_button("Export to PowerPoint…", "presentation")
        self.export_btn.clicked.connect(self.exportRequested.emit)
        foot.addWidget(self.export_btn, 1)
        self.pdf_btn = secondary_button("PDF", "save")
        self.pdf_btn.setToolTip("Export the queue as one PDF (MARKed pages included)")
        self.pdf_btn.clicked.connect(self.exportPdfRequested.emit)
        foot.addWidget(self.pdf_btn)
        lay.addLayout(foot)

        self.model.changed.connect(self._update_state)
        self.model.modelReset.connect(self._update_state)
        provider.thumbReady.connect(lambda _k: self.view.viewport().update())
        self._update_state()

    def _update_state(self):
        n = len(self.model.entries)
        self.header.badge.set_text(str(n) if n else "")
        self.export_btn.setEnabled(n > 0)
        self.pdf_btn.setEnabled(n > 0)
        self.clear_btn.setEnabled(n > 0)
        self.empty.setVisible(n == 0)
        self.view.setVisible(n > 0)

    def dragEnterEvent(self, event):
        if dropped_documents(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        files = dropped_documents(event)
        if files:
            event.acceptProposedAction()
            self.filesDropped.emit(files)
        else:
            event.ignore()

    def add(self, entries: list[QueueEntry]):
        self.model.add(entries)
        last = self.model.index(len(self.model.entries) - 1)
        self.view.scrollTo(last)

    def remove_selected(self):
        rows = [i.row() for i in self.view.selectionModel().selectedIndexes()]
        self.model.remove_rows(rows)

    def _confirm_clear(self):
        from PySide6.QtWidgets import QMessageBox

        n = len(self.model.entries)
        if n == 0:
            return
        if n > 3:
            box = QMessageBox(QMessageBox.Icon.Question, "Clear export queue?", f"Remove all {n} pages from the PowerPoint queue?",
                              QMessageBox.StandardButton.Cancel, self.window())
            clear = box.addButton("Clear Queue", QMessageBox.ButtonRole.DestructiveRole)
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not clear:
                return
        self.model.clear()

    def _context_menu(self, pos):
        idx = self.view.indexAt(pos)
        rows = sorted({i.row() for i in self.view.selectionModel().selectedIndexes()})
        m = QMenu(self)
        if idx.isValid():
            entry = self.model.entries[idx.row()]
            m.addAction("Show Page", lambda: self.showEntryRequested.emit(entry))
            m.addSeparator()
            m.addAction("Move to Top", lambda: self.model.move_rows(rows, 0))
            m.addAction("Move to Bottom", lambda: self.model.move_rows(rows, len(self.model.entries)))
            m.addSeparator()
            act = m.addAction(f"Remove {len(rows)} Item{'s' if len(rows) != 1 else ''}", self.remove_selected)
            act.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
        m.addAction("Export Queue as PDF…", self.exportPdfRequested.emit).setEnabled(bool(self.model.entries))
        m.addAction("Clear Queue…", self._confirm_clear).setEnabled(bool(self.model.entries))
        m.exec(self.view.viewport().mapToGlobal(pos))
