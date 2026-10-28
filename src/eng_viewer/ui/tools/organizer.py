"""Visual page organiser: combine, reorder, rotate, delete and insert pages."""
from __future__ import annotations

import copy
import os
import itertools
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen, QShortcut, QTransform, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QListView, QSlider, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from ...core.documents import IMAGE_EXTS
from ...core.tool_support import unique_path
from ...core.render_service import PRI_EXPORT, RenderResult
from ..theme import ThemeManager, qc
from ..widgets import label, primary_button, secondary_button, separator, tip, tool_button
from .common import Card, ResultCard, file_filter, local_paths

ACCEPTS = frozenset({".pdf"}) | IMAGE_EXTS
MIME = "application/x-eng-organizer-rows"
_uid = itertools.count(1)


def page_item(path: str | None, page: int = 0, rotate: int = 0, blank=None) -> dict:
    return {"uid": next(_uid), "path": path, "page": page, "rotate": rotate, "blank": blank}


class OrganizerModel(QAbstractListModel):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[dict] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if it["blank"]:
                return "Blank page"
            text = f"{Path(it['path']).name} — page {it['page'] + 1}" + (f" (rotated {it['rotate']}°)" if it["rotate"] else "")
            if role == Qt.ItemDataRole.ToolTipRole:
                # The full path tells apart same-named drawings from different folders.
                return f"{text}\n{os.path.normpath(it['path'])}"
            return text
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        return f if index.isValid() else Qt.ItemFlag.ItemIsDropEnabled

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def mimeTypes(self):
        return [MIME, "text/uri-list"]

    def mimeData(self, indexes):
        md = QMimeData()
        md.setData(MIME, ",".join(str(i.row()) for i in sorted(indexes, key=lambda i: i.row())).encode())
        return md

    def set_items(self, items: list[dict]):
        self.beginResetModel()
        self.items = items
        self.endResetModel()
        self.changed.emit()


class _Snapshot(QUndoCommand):
    def __init__(self, model: OrganizerModel, before: list[dict], after: list[dict], text: str):
        super().__init__(text)
        self.model, self.before, self.after = model, before, after

    def redo(self):
        self.model.set_items(copy.deepcopy(self.after))

    def undo(self):
        self.model.set_items(copy.deepcopy(self.before))


class OrganizerDelegate(QStyledItemDelegate):
    def __init__(self, view, model: OrganizerModel, thumbs):
        super().__init__(view)
        self.view, self.model, self.thumbs = view, model, thumbs
        self.thumb_h = 140

    def cell(self) -> QSize:
        return QSize(int(self.thumb_h * 1.15) + 16, self.thumb_h + 40)

    def sizeHint(self, option, index):
        return self.cell()

    def paint(self, p: QPainter, option, index):
        t = ThemeManager.current()
        it = self.model.items[index.row()]
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(4, 4, -4, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(t.selected))
            p.drawRoundedRect(r, 8, 8)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(t.hover))
            p.drawRoundedRect(r, 8, 8)
        box = QRectF(r.left() + 6, r.top() + 8, r.width() - 12, self.thumb_h - 4)
        if it["blank"]:
            w, h = it["blank"]
            pm = None
            aspect = w / h
        else:
            pm = self.thumbs.get(it["path"], it["page"], self.thumb_h * self.view.devicePixelRatioF())
            if pm is not None and not pm.isNull() and it["rotate"] % 360:
                pm = pm.transformed(QTransform().rotate(it["rotate"]), Qt.TransformationMode.SmoothTransformation)
            aspect = (pm.width() / pm.height()) if pm is not None and not pm.isNull() else 0.707
        tw = min(box.width(), box.height() * aspect)
        th = tw / aspect
        tr = QRectF(box.center().x() - tw / 2, box.top() + (box.height() - th) / 2, tw, th)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 40 if t.dark else 22))
        p.drawRoundedRect(tr.adjusted(-1, -0.5, 1, 1.5), 2, 2)
        p.setBrush(QColor("#FFFFFF"))
        p.drawRect(tr)
        if pm is not None and not pm.isNull():
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(tr, pm, QRectF(pm.rect()))
        if selected:
            p.setPen(QPen(qc(t.accent), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(tr.adjusted(-3, -3, 3, 3), 3, 3)
        f = QFont(option.font)
        f.setPointSizeF(max(8.5, f.pointSizeF() - 2))
        p.setFont(f)
        fm = QFontMetrics(f)
        text = "Blank" if it["blank"] else f"{Path(it['path']).stem} · {it['page'] + 1}"
        p.setPen(qc(t.text_secondary))
        p.drawText(QRectF(r.left() + 4, box.bottom() + 6, r.width() - 8, fm.height()), Qt.AlignmentFlag.AlignCenter,
                   fm.elidedText(text, Qt.TextElideMode.ElideMiddle, int(r.width() - 8)))
        # Page number badge in the top-left corner, over the thumbnail.
        num = str(index.row() + 1)
        nf = QFont(option.font)
        nf.setPointSizeF(max(8.0, nf.pointSizeF() - 2.5))
        nf.setWeight(QFont.Weight.DemiBold)
        nfm = QFontMetrics(nf)
        badge = QRectF(tr.left() + 4, tr.top() + 4, max(18, nfm.horizontalAdvance(num) + 10), nfm.height() + 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(t.accent) if selected else QColor(0, 0, 0, 150))
        p.drawRoundedRect(badge, badge.height() / 2, badge.height() / 2)
        p.setFont(nf)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, num)
        p.restore()


class OrganizerView(QListView):
    filesDropped = Signal(list, int)
    rowsDropped = Signal(list, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Pages")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Snap)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def _drop_row(self, pos) -> int:
        idx = self.indexAt(pos)
        if not idx.isValid():
            return self.model().rowCount()
        rect = self.visualRect(idx)
        return idx.row() + (1 if pos.x() > rect.center().x() else 0)

    def dragEnterEvent(self, e):
        if e.source() is self or local_paths(e.mimeData(), ACCEPTS):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        super().dragMoveEvent(e)
        e.acceptProposedAction()

    def dropEvent(self, e):
        row = self._drop_row(e.position().toPoint())
        if e.source() is self and e.mimeData().hasFormat(MIME):
            rows = [int(x) for x in bytes(e.mimeData().data(MIME)).decode().split(",") if x]
            self.rowsDropped.emit(rows, row)
            e.setDropAction(Qt.DropAction.IgnoreAction)
            e.accept()
            return
        paths = local_paths(e.mimeData(), ACCEPTS)
        if paths:
            self.filesDropped.emit(paths, row)
            e.acceptProposedAction()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.model() is not None and self.model().rowCount():
            return
        t = ThemeManager.current()
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.viewport().rect()).adjusted(10, 10, -10, -10)
        p.setPen(QPen(qc(t.control_border), 1.4, Qt.PenStyle.DashLine))
        p.drawRoundedRect(r, 12, 12)
        p.setPen(qc(t.text_secondary))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter,
                   "Drop PDFs, images or folders here\nDrag pages to reorder · R rotates · Delete removes")
        p.end()


class OrganizerPanel(QWidget):
    outputsCreated = Signal(list)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.model = OrganizerModel(self)
        self.undo = QUndoStack(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        card = Card()
        bar = QHBoxLayout()
        bar.setSpacing(4)
        add = secondary_button("Add Files…", "plus")
        add.clicked.connect(self._choose)
        bar.addWidget(add)
        lib = secondary_button("From Library")
        lib.clicked.connect(lambda: self.add_files(host.library_selection()))
        bar.addWidget(lib)
        cur = secondary_button("Current Drawing")
        cur.clicked.connect(lambda: host.current_document() and self.add_files([host.current_document()]))
        bar.addWidget(cur)
        bar.addWidget(separator(True))
        for icon, tipx, fn in (("undo", tip("Undo", QKeySequence.StandardKey.Undo), self.undo.undo),
                               ("redo", tip("Redo", QKeySequence.StandardKey.Redo), self.undo.redo),
                               ("rotate-page", "Rotate right (R)", lambda: self.rotate(90)),
                               ("refresh", "Rotate left (Shift+R)", lambda: self.rotate(-90)),
                               ("copy", "Duplicate", self.duplicate), ("file", "Insert blank page", self.insert_blank),
                               ("trash", "Delete selected (Delete)", self.delete_selected)):
            b = tool_button(icon, tipx, size=30, icon_size=18)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        self.count = label("", "tertiary")
        bar.addWidget(self.count)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(80, 260)
        self.size_slider.setValue(140)
        self.size_slider.setFixedWidth(100)
        self.size_slider.setToolTip("Thumbnail size")
        self.size_slider.valueChanged.connect(self._thumb_size)
        bar.addWidget(self.size_slider)
        card.lay.addLayout(bar)

        self.view = OrganizerView()
        self.view.setModel(self.model)
        self.delegate = OrganizerDelegate(self.view, self.model, host.thumbs)
        self.view.setItemDelegate(self.delegate)
        self.view.setGridSize(self.delegate.cell())
        self.view.setMinimumHeight(360)
        self.view.filesDropped.connect(self.add_files)
        self.view.rowsDropped.connect(self._move_rows)
        card.lay.addWidget(self.view, 1)
        lay.addWidget(card, 1)

        row = QHBoxLayout()
        self.note = label("Nothing is changed in the source files — the result is a new PDF.", "tertiary")
        row.addWidget(self.note, 1)
        self.save_sel = secondary_button("Save Selected Pages…")
        self.save_sel.clicked.connect(lambda: self.save(selected_only=True))
        row.addWidget(self.save_sel)
        self.save_btn = primary_button("Save as PDF…", "save")
        self.save_btn.clicked.connect(self.save)
        row.addWidget(self.save_btn)
        lay.addLayout(row)
        self.result = ResultCard()
        self.result.openInViewer.connect(host.open_in_viewer)
        lay.addWidget(self.result)

        host.thumbs.thumbReady.connect(lambda _k: self.view.viewport().update())
        self.model.changed.connect(self._update)
        for seq, fn in ((QKeySequence.StandardKey.Undo, self.undo.undo), (QKeySequence.StandardKey.Redo, self.undo.redo),
                        (QKeySequence.StandardKey.Delete, self.delete_selected), (QKeySequence("Backspace"), self.delete_selected),
                        (QKeySequence("R"), lambda: self.rotate(90)), (QKeySequence("Shift+R"), lambda: self.rotate(-90)),
                        (QKeySequence.StandardKey.SelectAll, self.view.selectAll)):
            sc = QShortcut(seq, self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self._update()

    # ----------------------------------------------------------- editing
    def _commit(self, items: list[dict], text: str):
        self.undo.push(_Snapshot(self.model, copy.deepcopy(self.model.items), copy.deepcopy(items), text))

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.view.selectionModel().selectedIndexes()})

    def _reselect(self, rows: list[int]):
        sel = self.view.selectionModel()
        sel.clearSelection()
        for r in rows:
            if 0 <= r < self.model.rowCount():
                sel.select(self.model.index(r), sel.SelectionFlag.Select)

    def add_files(self, paths: list[str], at: int | None = None):
        paths = [p for p in paths if Path(p).suffix.lower() in ACCEPTS]
        if not paths:
            return
        insert_at = self.model.rowCount() if at is None else at
        infos: dict[str, dict] = {}

        def got(path, info):
            infos[path] = info
            if len(infos) < len(paths):
                return
            new = []
            for p in paths:
                i = infos[p]
                if "error" not in i:
                    new += [page_item(p, k) for k in range(i["count"])]
            if not new:
                self.note.setText("Those files could not be read.")
                return
            items = list(self.model.items)
            items[insert_at:insert_at] = new
            self._commit(items, f"Add {len(new)} Pages")
            if at is not None:  # dropped between pages: show what arrived
                self._reselect(list(range(insert_at, insert_at + len(new))))
            else:
                self.view.scrollTo(self.model.index(insert_at))

        for p in paths:
            self.host.document_info(p, lambda info, p=p: got(p, info))

    def _move_rows(self, rows: list[int], dest: int):
        items = list(self.model.items)
        moving = [items[r] for r in rows]
        dest -= sum(1 for r in rows if r < dest)
        for r in reversed(rows):
            del items[r]
        items[dest:dest] = moving
        self._commit(items, "Move Pages")
        self._reselect(list(range(dest, dest + len(moving))))

    def rotate(self, angle: int):
        rows = self._selected_rows()
        if not rows:
            return
        items = copy.deepcopy(self.model.items)
        for r in rows:
            items[r]["rotate"] = (items[r]["rotate"] + angle) % 360
        self._commit(items, "Rotate Pages")
        self._reselect(rows)

    def duplicate(self):
        rows = self._selected_rows()
        if not rows:
            return
        items = list(self.model.items)
        copies = [dict(copy.deepcopy(items[r]), uid=next(_uid)) for r in rows]
        at = rows[-1] + 1
        items[at:at] = copies
        self._commit(items, "Duplicate Pages")
        self._reselect(list(range(at, at + len(copies))))

    def insert_blank(self):
        rows = self._selected_rows()
        at = rows[-1] + 1 if rows else self.model.rowCount()
        ref = self.model.items[rows[-1]] if rows else (self.model.items[-1] if self.model.items else None)
        size = (595.3, 841.9)
        if ref is not None and not ref["blank"]:
            info = self.host.thumbs.info(ref["path"]) or {}
            if info.get("w"):
                size = (info["w"], info["h"])
        elif ref is not None:
            size = tuple(ref["blank"])
        items = list(self.model.items)
        items.insert(at, page_item(None, blank=list(size)))
        self._commit(items, "Insert Blank Page")
        self._reselect([at])

    def delete_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        items = [it for i, it in enumerate(self.model.items) if i not in set(rows)]
        self._commit(items, f"Delete {len(rows)} Pages")

    def _thumb_size(self, v: int):
        self.delegate.thumb_h = v
        self.view.setGridSize(self.delegate.cell())

    def _choose(self):
        files, _ = QFileDialog.getOpenFileNames(self.window(), "Add PDFs or images", self.host.default_folder(), file_filter(ACCEPTS))
        if files:
            self.add_files(files)

    def _update(self):
        n = self.model.rowCount()
        files = len({it["path"] for it in self.model.items if it["path"]})
        self.count.setText(f"{n} page{'s' if n != 1 else ''} from {files} file{'s' if files != 1 else ''}" if n else "")
        self.save_btn.setEnabled(n > 0)
        self.save_sel.setEnabled(n > 0)

    # -------------------------------------------------------------- save
    def save(self, selected_only: bool = False):
        rows = self._selected_rows() if selected_only else list(range(self.model.rowCount()))
        if not rows:
            self.note.setText("Select some pages first.")
            return
        items = [self.model.items[r] for r in rows]
        first = next((it["path"] for it in items if it["path"]), None)
        folder = str(Path(first).parent) if first else self.host.default_folder()
        name = f"{Path(first).stem} (organized).pdf" if first else "Organized.pdf"
        path, _ = QFileDialog.getSaveFileName(self.window(), "Save organised PDF", str(unique_path(Path(folder) / name)), "*.pdf")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        sources = {it["path"] for it in items if it["path"]}
        if any(Path(path).resolve() == Path(s).resolve() for s in sources):
            self.result.show_error("Choose a different name — originals are never overwritten.")
            return
        job_items = [{"blank": it["blank"]} if it["blank"] else {"path": it["path"], "page": it["page"], "rotate": it["rotate"]} for it in items]
        self.save_btn.setEnabled(False)
        self.note.setText("Saving…")
        self.host.render.submit("tool", {"op": "organize", "items": job_items, "output": path}, self._saved,
                                priority=PRI_EXPORT, dedupe=False)

    def _saved(self, res: RenderResult):
        self._update()
        self.note.setText("Nothing is changed in the source files — the result is a new PDF.")
        if res.ok:
            self.result.show_success("Saved", res.header.get("summary", ""), res.header.get("outputs", []))
            self.outputsCreated.emit(res.header.get("outputs", []))
        else:
            self.result.show_error(res.error)
