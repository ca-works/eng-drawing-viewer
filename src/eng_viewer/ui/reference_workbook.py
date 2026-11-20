"""Virtual, read-only worksheet grids for Windows Reference."""
import csv
from functools import lru_cache
import io
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event

from PySide6.QtCore import QAbstractTableModel, QEvent, Qt, QThreadPool, QModelIndex
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QHeaderView, QLabel, QMenu, QTableView, QTabWidget, QVBoxLayout, QWidget

from ..core.reference_workbook import column_name, index_sheet, read_workbook
from .image_editor.common import Job


class WorksheetModel(QAbstractTableModel):
    def __init__(self, database, size, parent):
        super().__init__(parent)
        self.rows, self.columns = size
        self.database = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        self.tile = lru_cache(maxsize=40)(self._tile)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.rows

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.columns

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return column_name(section + 1) if orientation == Qt.Orientation.Horizontal else str(section + 1)
        return None

    def _tile(self, row, column):
        cells = self.database.execute(
            "SELECT r,c,value FROM cells WHERE r BETWEEN ? AND ? AND c BETWEEN ? AND ?",
            (row + 1, row + 32, column + 1, column + 16),
        )
        return {(r, c): value for r, c, value in cells}

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        row, column = index.row(), index.column()
        return self.tile(row // 32 * 32, column // 16 * 16).get((row + 1, column + 1))

    def close(self):
        self.tile.cache_clear()
        self.database.close()


class WorksheetGrid(QTableView):
    def __init__(self, database, size, notify, parent):
        super().__init__(parent)
        self.notify = notify
        self.setModel(WorksheetModel(database, size, self))
        self.setAccessibleName("Read-only worksheet")
        self.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        for header, default in ((self.horizontalHeader(), 124), (self.verticalHeader(), 23)):
            header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            header.setDefaultSectionSize(default)
        self.verticalHeader().setFixedWidth(max(35, self.fontMetrics().horizontalAdvance(str(size[0])) + 14))

    def event(self, event):
        if event.type() == QEvent.Type.ShortcutOverride and event.matches(QKeySequence.StandardKey.Copy):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_cells()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        copy = menu.addAction("Copy selected cells", self.copy_cells)
        copy.setEnabled(self.selectionModel().hasSelection())
        menu.exec(event.globalPos())

    def copy_cells(self):
        selection = self.selectionModel().selection()
        if not selection:
            return
        top, bottom = min(r.top() for r in selection), max(r.bottom() for r in selection)
        left, right = min(r.left() for r in selection), max(r.right() for r in selection)
        if (bottom - top + 1) * (right - left + 1) > 100_000:
            self.notify("Select up to 100,000 cells to copy.")
            return
        model = self.model()
        output = io.StringIO()
        writer = csv.writer(output, dialect="excel-tab", lineterminator="\r\n")
        for row in range(top, bottom + 1):
            indexes = (model.index(row, column) for column in range(left, right + 1))
            writer.writerow((model.data(index) or "") if selection.contains(index) else "" for index in indexes)
            if output.tell() > 16 * 1024 * 1024:
                self.notify("The selected cells exceed the clipboard size limit.")
                return
        QApplication.clipboard().setText(output.getvalue().removesuffix("\r\n"))
        self.notify("")


class WorkbookReference(QWidget):
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.cancelled = Event()
        self.book = None
        self.jobs = set()
        self.grids = {}
        self.loading = set()
        self.storage = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.canvas = QTabWidget()
        self.canvas.setTabPosition(QTabWidget.TabPosition.South)
        self.canvas.setAccessibleName("Worksheets")
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.canvas.currentChanged.connect(self._load_sheet)
        layout.addWidget(self.canvas, 1)

    def message(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _run(self, work, done):
        job = Job(work)
        self.jobs.add(job)

        def finish(result, error):
            self.jobs.discard(job)
            if not self.cancelled.is_set():
                done(result, error)

        job.signals.finished.connect(finish, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(job)

    def load(self, source):
        self.message("Opening workbook…")

        def ready(book, error):
            if error:
                self.message(f"Could not open workbook. {error}")
                return
            self.message("")
            self.book = book
            self.canvas.blockSignals(True)
            for name, _ in book.sheets:
                page = QWidget()
                layout = QVBoxLayout(page)
                layout.setContentsMargins(0, 0, 0, 0)
                self.canvas.addTab(page, name)
            self.canvas.blockSignals(False)
            self._load_sheet(self.canvas.currentIndex())

        self._run(lambda: read_workbook(source), ready)

    def _load_sheet(self, index):
        if index in self.grids:
            self.grids[index].setFocus()
            return
        if index < 0 or index in self.loading:
            return
        try:
            if self.storage is None:
                self.storage = TemporaryDirectory(prefix="eng-xlsx-")
        except OSError as exc:
            self.message(f"Could not create temporary workbook storage. {exc}")
            return
        page = self.canvas.widget(index)
        label = QLabel("Loading worksheet…")
        label.setWordWrap(True)
        page.layout().addWidget(label)
        database = Path(self.storage.name) / f"{index}.db"
        source, book, cancelled = self.source, self.book, self.cancelled
        self.loading.add(index)

        def ready(size, error):
            if error:
                label.setText(f"Could not read worksheet. {error}")
                return
            grid = WorksheetGrid(database, size, self.message, page)
            self.grids[index] = grid
            label.deleteLater()
            page.layout().addWidget(grid)
            if self.canvas.currentIndex() == index and self.isVisible():
                grid.setFocus()

        def work(storage=self.storage):
            # Keep the directory alive while this worker owns an open SQLite file.
            return index_sheet(source, book, index, database, cancelled)

        self._run(work, ready)

    def focus_preview(self):
        self.grids.get(self.canvas.currentIndex(), self.canvas).setFocus()

    def release(self):
        self.cancelled.set()
        for grid in self.grids.values():
            model = grid.model()
            grid.setModel(None)
            model.close()
        self.storage = None
