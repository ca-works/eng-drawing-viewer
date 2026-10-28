"""Page thumbnail grid for the open document."""
from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget

from .theme import ThemeManager, qc
from .thumbnails import ThumbnailProvider
from .widgets import SectionHeader

THUMB_H = 96


class PagesModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.path: str | None = None
        self.count = 0
        self.sizes: list[tuple[float, float]] = []
        self.current = 0

    def set_document(self, path: str | None, count: int, sizes: list[tuple[float, float]]):
        self.beginResetModel()
        self.path, self.count, self.sizes = path, count, list(sizes)
        self.current = 0
        self.endResetModel()

    def set_current(self, i: int):
        old, self.current = self.current, i
        for r in (old, i):
            if 0 <= r < self.count:
                idx = self.index(r)
                self.dataChanged.emit(idx, idx)

    def refresh(self, page: int):
        if 0 <= page < self.count:
            idx = self.index(page)
            self.dataChanged.emit(idx, idx)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.count

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return str(index.row() + 1)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"Page {index.row() + 1}"
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"Page {index.row() + 1} of {self.count}"
        return None

    def aspect(self, row: int) -> float:
        if row < len(self.sizes) and self.sizes[row][1] > 0:
            w, h = self.sizes[row]
            return w / h
        if self.sizes and self.sizes[0][1] > 0:
            return self.sizes[0][0] / self.sizes[0][1]
        return 1.414


class PagesDelegate(QStyledItemDelegate):
    def __init__(self, view: QListView, model: PagesModel, provider: ThumbnailProvider):
        super().__init__(view)
        self.view, self.model, self.provider = view, model, provider

    def cell(self) -> QSize:
        """Cell size that divides the available width into equal columns."""
        aspect = max((self.model.aspect(i) for i in range(min(self.model.count, 20))), default=1.414)
        natural = int(min(THUMB_H * max(aspect, 0.7), THUMB_H * 1.6)) + 14
        avail = max(60, self.view.viewport().width() - 2)
        cols = max(1, avail // max(60, natural - 24))
        return QSize(max(60, (avail - 1) // cols), THUMB_H + 26)

    def sizeHint(self, option, index):
        return self.cell()

    def paint(self, painter: QPainter, option, index):
        t = ThemeManager.current()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect)
        row = index.row()
        current = row == self.model.current
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        aspect = self.model.aspect(row)
        box_h = THUMB_H
        box_w = r.width() - 14
        tw = min(box_w, box_h * aspect)
        th = tw / aspect
        tr = QRectF(r.left() + (r.width() - tw) / 2, r.top() + 6 + (box_h - th) / 2, tw, th)
        if hover and not current:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qc(t.hover))
            painter.drawRoundedRect(r.adjusted(2, 1, -2, -1), 7, 7)
        pm = self.provider.get(self.model.path, row, box_h * self.view.devicePixelRatioF()) if self.model.path else None
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 45 if t.dark else 24))
        painter.drawRoundedRect(tr.adjusted(-1, -0.5, 1, 1.5), 2.5, 2.5)
        if pm is not None and not pm.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(tr, pm, QRectF(pm.rect()))
        else:
            painter.setBrush(QColor("#FFFFFF") if not t.dark else QColor("#2A2A2F"))
            painter.drawRect(tr)
        if current:
            pen = QPen(qc(t.accent), 2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(tr.adjusted(-3, -3, 3, 3), 4, 4)
        f = QFont(option.font)
        f.setPointSizeF(max(8.5, f.pointSizeF() - 2))
        if current:
            f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(qc(t.accent if current else t.text_secondary))
        painter.drawText(QRectF(r.left(), r.top() + 8 + box_h, r.width(), 16), Qt.AlignmentFlag.AlignCenter, str(row + 1))
        painter.restore()


class PagesPanel(QWidget):
    pageActivated = Signal(int)

    def __init__(self, provider: ThumbnailProvider, parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.provider = provider
        self.model = PagesModel(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = SectionHeader("Pages")
        lay.addWidget(self.header)
        self.view = QListView()
        self.view.setObjectName("Pages")
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setFlow(QListView.Flow.LeftToRight)
        self.view.setWrapping(True)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(0)
        self.view.setMouseTracking(True)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.view.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.view.setModel(self.model)
        self.delegate = PagesDelegate(self.view, self.model, provider)
        self.view.setItemDelegate(self.delegate)
        self.view.clicked.connect(lambda idx: self.pageActivated.emit(idx.row()))
        self.view.setContentsMargins(6, 0, 6, 6)
        # Re-flow when the viewport changes width (e.g. a scroll bar appears).
        self.view.viewport().installEventFilter(self)
        lay.addWidget(self.view, 1)
        provider.thumbReady.connect(self._on_thumb)

    def set_document(self, path: str | None, count: int, sizes):
        self.model.set_document(path, count, sizes)
        self._relayout()
        self.header.badge.set_text(str(count) if count else "")

    def set_current(self, page: int):
        self.model.set_current(page)
        if 0 <= page < self.model.count:
            self.view.scrollTo(self.model.index(page), QAbstractItemView.ScrollHint.EnsureVisible)

    def _relayout(self):
        self.view.setGridSize(self.delegate.cell())

    def eventFilter(self, obj, event):
        if obj is self.view.viewport() and event.type() == QEvent.Type.Resize:
            if self.view.gridSize() != self.delegate.cell():
                self._relayout()
        return False

    def _on_thumb(self, key):
        path, page = key
        if path == self.model.path:
            self.model.refresh(page)
