"""Building blocks shared by the tool panels: file inputs, cards, results."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QSizePolicy, QStyle,
    QStyledItemDelegate, QVBoxLayout,
)

from ...core.documents import IMAGE_EXTS, extension
from ...platform import reveal_in_file_manager, reveal_label
from .. import icons
from ..theme import ThemeManager, qc
from ..widgets import label, primary_button, secondary_button, tool_button

PATH_ROLE = Qt.ItemDataRole.UserRole + 1


def file_filter(exts) -> str:
    pats = " ".join(f"*{e}" for e in sorted(exts))
    kinds = []
    if ".pdf" in exts:
        kinds.append("PDFs")
    if exts & IMAGE_EXTS:
        kinds.append("images")
    if exts & {".docx", ".xlsx", ".pptx"}:
        kinds.append("Office documents")
    return f"{' and '.join(kinds) or 'Files'} ({pats})"


def local_paths(mime, exts) -> list[str]:
    out = []
    for u in mime.urls() if mime.hasUrls() else []:
        if u.isLocalFile():
            p = Path(u.toLocalFile())
            if p.is_dir():
                out += [str(c) for c in sorted(p.iterdir()) if c.is_file() and extension(c.name) in exts and not c.name.startswith(".")]
            elif extension(p.name) in exts:
                out.append(str(p))
    return out


class Card(QFrame):
    """A softly outlined panel section."""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 12, 16, 14)
        self.lay.setSpacing(10)
        if title:
            self.header = QHBoxLayout()
            self.title = label(title.upper(), "section")
            self.header.addWidget(self.title)
            self.header.addStretch(1)
            self.lay.addLayout(self.header)


class FileListDelegate(QStyledItemDelegate):
    ROW = 46

    def __init__(self, view, thumbs):
        super().__init__(view)
        self.view, self.thumbs = view, thumbs

    def sizeHint(self, option, index):
        return QSize(100, self.ROW)

    def paint(self, p: QPainter, option, index):
        t = ThemeManager.current()
        path = index.data(PATH_ROLE)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(2, 1, -2, -1)
        if option.state & QStyle.StateFlag.State_Selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(t.selected))
            p.drawRoundedRect(r, 6, 6)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(t.hover))
            p.drawRoundedRect(r, 6, 6)
        f = QFont(option.font)
        small = QFont(f)
        small.setPointSizeF(max(8.5, f.pointSizeF() - 2))
        p.setFont(small)
        p.setPen(qc(t.text_tertiary))
        p.drawText(QRectF(r.left(), r.top(), 24, r.height()), Qt.AlignmentFlag.AlignCenter, str(index.row() + 1))
        box = QRectF(r.left() + 26, r.top() + 5, 50, r.height() - 10)
        pm = self.thumbs.get(path, 0, box.height() * self.view.devicePixelRatioF()) if extension(path) not in (".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt", ".ods", ".odp", ".rtf", ".txt", ".csv") else None
        if pm is not None and not pm.isNull():
            s = min(box.width() / pm.width(), box.height() / pm.height())
            tr = QRectF(0, 0, pm.width() * s, pm.height() * s)
            tr.moveCenter(box.center())
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(tr, pm, QRectF(pm.rect()))
            p.setPen(QPen(QColor(0, 0, 0, 30), 0.8))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(tr)
        else:
            icons.paint_icon(p, "office" if extension(path) not in IMAGE_EXTS | {".pdf"} else "file",
                             QRectF(box.center().x() - 10, box.center().y() - 10, 20, 20), qc(t.text_tertiary))
        x = box.right() + 10
        w = r.right() - x - 6
        fm = QFontMetrics(f)
        p.setFont(f)
        p.setPen(qc(t.text))
        name = Path(path).name
        p.drawText(QRectF(x, r.top() + 6, w, fm.height()), Qt.AlignmentFlag.AlignVCenter, fm.elidedText(name, Qt.TextElideMode.ElideMiddle, int(w)))
        p.setFont(small)
        p.setPen(qc(t.text_tertiary))
        info = self.thumbs.info(path) or {}
        sub = Path(path).parent.name or str(Path(path).parent)
        if info.get("pages"):
            n = info["pages"]
            sub = f"{n} page{'s' if n != 1 else ''} · {sub}"
        sfm = QFontMetrics(small)
        p.drawText(QRectF(x, r.top() + 8 + fm.height(), w, sfm.height()), Qt.AlignmentFlag.AlignVCenter,
                   sfm.elidedText(sub, Qt.TextElideMode.ElideMiddle, int(w)))
        p.restore()


class FileInputList(QListWidget):
    """Input files: drop files or folders here, drag to reorder, Delete removes."""

    changed = Signal()

    def __init__(self, thumbs, exts, reorder: bool, parent=None):
        super().__init__(parent)
        self.exts = exts
        self.setObjectName("Queue")
        self.setAcceptDrops(True)
        self.setDragEnabled(reorder)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop if reorder else QAbstractItemView.DragDropMode.DropOnly)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.setItemDelegate(FileListDelegate(self, thumbs))
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        for sig in (self.model().rowsInserted, self.model().rowsRemoved, self.model().modelReset):
            sig.connect(self._rows_changed)  # a bound slot: disconnected when the list is destroyed
        thumbs.thumbReady.connect(lambda _k: self.viewport().update())
        thumbs.infoReady.connect(lambda _p: self.viewport().update())
        self.model().rowsMoved.connect(lambda *_: self.changed.emit())

    def _rows_changed(self, *_):
        self.updateGeometry()

    def sizeHint(self):
        # Grow with the list (up to about six rows), so options stay in view.
        n = self.count()
        row = self.sizeHintForRow(0) if n else 56
        return QSize(420, min(380, max(140, n * row + 16)))

    def paths(self) -> list[str]:
        return [self.item(i).data(PATH_ROLE) for i in range(self.count())]

    def add_paths(self, paths: list[str], at: int | None = None):
        existing = set(self.paths())
        row = self.count() if at is None else at
        added = 0
        for p in paths:
            if p in existing or extension(p) not in self.exts:
                continue
            it = QListWidgetItem()
            it.setData(PATH_ROLE, p)
            it.setToolTip(p)
            self.insertItem(row, it)
            row += 1
            added += 1
            existing.add(p)
        if added:
            self.changed.emit()
        return added

    def remove_selected(self):
        for it in self.selectedItems():
            self.takeItem(self.row(it))
        self.changed.emit()

    def clear_all(self):
        self.clear()
        self.changed.emit()

    def dragEnterEvent(self, e):
        if e.source() is self:
            super().dragEnterEvent(e)
        elif local_paths(e.mimeData(), self.exts):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.source() is self:
            super().dragMoveEvent(e)
        else:
            e.acceptProposedAction()

    def dropEvent(self, e):
        if e.source() is self:
            super().dropEvent(e)
            self.changed.emit()
            return
        paths = local_paths(e.mimeData(), self.exts)
        idx = self.indexAt(e.position().toPoint())
        self.add_paths(paths, idx.row() if idx.isValid() else None)
        e.acceptProposedAction()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_selected()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count():
            return
        t = ThemeManager.current()
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.viewport().rect()).adjusted(6, 6, -6, -6)
        pen = QPen(qc(t.control_border), 1.4, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 10, 10)
        icons.paint_icon(p, "drop", QRectF(r.center().x() - 14, r.center().y() - 30, 28, 28), qc(t.text_tertiary))
        p.setPen(qc(t.text_secondary))
        p.drawText(QRectF(r.left(), r.center().y() + 4, r.width(), 22), Qt.AlignmentFlag.AlignCenter, "Drop files or folders here")
        p.end()


class InputsCard(Card):
    """File inputs with the usual ways to add files."""

    changed = Signal()

    def __init__(self, host, exts, reorder: bool, multi_hint: str, parent=None):
        super().__init__("Files", parent)
        self.host = host
        self.exts = exts
        self.list = FileInputList(host.thumbs, exts, reorder)
        self.list.changed.connect(self._changed)
        self.count = label("", "tertiary")
        self.header.insertWidget(1, self.count)
        add = secondary_button("Add Files…", "plus")
        add.clicked.connect(self._choose)
        self.header.addWidget(add)
        self.lib_btn = secondary_button("From Library")
        self.lib_btn.setToolTip("Add the files selected in the library")
        self.lib_btn.clicked.connect(lambda: self.list.add_paths(host.library_selection()))
        self.header.addWidget(self.lib_btn)
        self.cur_btn = secondary_button("Current Drawing")
        self.cur_btn.setToolTip("Add the drawing open in the viewer")
        self.cur_btn.clicked.connect(self._add_current)
        self.header.addWidget(self.cur_btn)
        clear = tool_button("trash", "Remove all files", size=26, icon_size=16)
        clear.clicked.connect(self.list.clear_all)
        self.header.addWidget(clear)
        self.lay.addWidget(self.list, 1)
        hint = label(multi_hint, "tertiary", wrap=True)
        self.lay.addWidget(hint)
        self._changed()

    def _add_current(self):
        cur = self.host.current_document()
        if cur:
            self.list.add_paths([cur])

    def _choose(self):
        start = self.host.default_folder()
        files, _ = QFileDialog.getOpenFileNames(self.window(), "Choose files", start, file_filter(self.exts))
        if files:
            self.list.add_paths(files)

    def _changed(self):
        n = self.list.count()
        self.count.setText(f"{n} file{'s' if n != 1 else ''}" if n else "")
        cur = self.host.current_document()
        self.cur_btn.setEnabled(bool(cur) and extension(cur) in self.exts)
        self.changed.emit()

    def paths(self) -> list[str]:
        return self.list.paths()


class ResultCard(Card):
    openInViewer = Signal(str)

    def __init__(self, parent=None):
        super().__init__("", parent)
        top = QHBoxLayout()
        self.icon = QLabel()
        top.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = label("", "title")
        col.addWidget(self.title)
        self.summary = label("", "secondary", wrap=True)
        col.addWidget(self.summary)
        top.addLayout(col, 1)
        self.lay.addLayout(top)
        self.files = QListWidget()
        self.files.setObjectName("Queue")
        self.files.setMaximumHeight(150)
        self.files.itemDoubleClicked.connect(lambda it: self._open(it.data(PATH_ROLE)))
        self.lay.addWidget(self.files)
        row = QHBoxLayout()
        self.reveal = secondary_button(reveal_label())
        self.reveal.clicked.connect(lambda: reveal_in_file_manager(self._first()))
        row.addWidget(self.reveal)
        row.addStretch(1)
        self.open_btn = secondary_button("Open")
        self.open_btn.clicked.connect(lambda: self._open(self._first()))
        row.addWidget(self.open_btn)
        self.viewer_btn = primary_button("Open in Viewer", "viewer")
        self.viewer_btn.clicked.connect(lambda: self.openInViewer.emit(self._first()))
        row.addWidget(self.viewer_btn)
        self.lay.addLayout(row)
        self.outputs: list[str] = []
        self.hide()

    def _first(self) -> str | None:
        it = self.files.currentItem() or (self.files.item(0) if self.files.count() else None)
        return it.data(PATH_ROLE) if it else None

    def _open(self, path):
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def show_success(self, title: str, summary: str, outputs: list[str]):
        self.outputs = outputs
        self.icon.setPixmap(icons.pixmap("check", 22, icons._resolve("success"), stroke=2.4))
        self.title.setText(title)
        self.summary.setText(summary)
        self.files.clear()
        for o in outputs:
            it = QListWidgetItem(Path(o).name)
            it.setData(PATH_ROLE, o)
            it.setToolTip(o)
            self.files.addItem(it)
        self.files.setVisible(bool(outputs))
        viewable = any(extension(o) in IMAGE_EXTS | {".pdf"} for o in outputs)
        self.viewer_btn.setVisible(viewable)
        self.open_btn.setVisible(bool(outputs))
        self.reveal.setVisible(bool(outputs))
        self.show()

    def show_error(self, message: str):
        self.outputs = []
        self.icon.setPixmap(icons.pixmap("warning", 22, icons._resolve("danger")))
        self.title.setText("That didn’t work")
        self.summary.setText(message)
        self.files.hide()
        for b in (self.viewer_btn, self.open_btn, self.reveal):
            b.hide()
        self.show()
