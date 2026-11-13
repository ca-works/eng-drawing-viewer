"""Dockable read-only reference browser and per-document viewing sessions."""
from __future__ import annotations

import sys
import tempfile
from itertools import islice
from pathlib import Path
from threading import Event

from PySide6.QtCore import QFileInfo, QObject, QPointF, QRectF, QSize, Qt, QThreadPool, QTimer, QEvent, Signal
from PySide6.QtGui import QColor, QCursor, QDoubleValidator, QIcon, QKeySequence, QPen, QUndoStack
from PySide6.QtWidgets import (
    QApplication, QColorDialog, QDockWidget, QDoubleSpinBox, QFileDialog, QFileIconProvider,
    QGraphicsItem, QGraphicsRectItem, QHBoxLayout, QLabel, QLineEdit, QMenu, QSpinBox, QStackedWidget,
    QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.render_service import PRI_EXPORT, PRI_PREVIEW
from ..core.reference_text import TABLE_EXTS
from ..core.standards import IWORK_EXTS, OPEN_EXTS, STANDARDS_EXTS, scan_folder
from . import icons
from .image_editor.common import Job
from .mark.commands import AddItems, RemoveItems
from .mark.items import StrokeItem
from .mark.tool_options import RANGES, ToolSettings
from .page_view import PageRef, PageView
from .reference_view import ContinuousReferenceView
from .widgets import ElidedLabel, primary_button, tool_button

REFERENCE_TITLE = "Reference" if sys.platform == "win32" else "Standards"
RIGHT = Qt.DockWidgetArea.RightDockWidgetArea
PATH_ROLE = Qt.ItemDataRole.UserRole


def row(parent, margins=(0, 0, 0, 0)):
    layout = QHBoxLayout()
    layout.setContentsMargins(*margins)
    layout.setSpacing(3)
    parent.addLayout(layout)
    return layout


def button(layout, icon, tooltip, slot, checkable=False):
    control = tool_button(icon, tooltip, checkable=checkable, size=26, icon_size=16)
    control.clicked.connect(slot)
    layout.addWidget(control)
    return control


class ReferenceCanvas(PageView):
    """Page coordinates and vector strokes shared with MARK, with page-local undo."""

    historyChanged = Signal(bool, bool)

    def __init__(self, render, settings):
        self.mode = "pan"
        super().__init__(render)
        self.settings = settings
        self.left_drag_pans = True
        self.pages = {}
        self.active = None
        self.stroke = None

    def set_page(self, ref, placeholder=None, keep_view=False):
        if self.stroke is not None:
            self.detach_items([self.stroke])
            self.stroke = None
        if self.active is not None:
            self.active[0].hide()
        self.active = self.pages.get(ref.page) if ref else None
        if self.active is not None:
            self.active[0].show()
        super().set_page(ref, placeholder, keep_view)
        self._history_changed()

    def _ink(self):
        if self.active is None:
            clip = QGraphicsRectItem(0, 0, self.page_ref.width, self.page_ref.height)
            clip.setPen(QPen(Qt.PenStyle.NoPen))
            clip.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape)
            self.scene_obj.addItem(clip)
            stack = QUndoStack(self)
            stack.setUndoLimit(150)
            stack.indexChanged.connect(self._history_changed)
            self.active = self.pages[self.page_ref.page] = (clip, stack)
        return self.active

    def set_mode(self, mode):
        self.mode = mode
        self.left_drag_pans = mode == "pan"
        self._update_cursor()

    def tool_cursor(self):
        if self.mode == "pan":
            return super().tool_cursor()
        return QCursor(Qt.CursorShape.CrossCursor)

    def attach_items(self, items):
        clip, _ = self._ink()
        for item in items:
            item.setParentItem(clip)

    def detach_items(self, items):
        for item in items:
            item.setParentItem(None)
            self.scene_obj.removeItem(item)

    def _history_changed(self, *_):
        stack = self.active[1] if self.active is not None else None
        self.historyChanged.emit(bool(stack and stack.canUndo()), bool(stack and stack.canRedo()))

    def history(self, redo=False):
        if self.active is not None:
            stack = self.active[1]
            stack.redo() if redo else stack.undo()

    def _position(self, event):
        return self.viewportTransform().inverted()[0].map(event.position())

    def mousePressEvent(self, event):
        if (event.button() != Qt.MouseButton.LeftButton or self.mode == "pan" or
                self._space_down or self.page_ref is None):
            super().mousePressEvent(event)
            return
        point = self._position(event)
        if self.page_item.boundingRect().contains(point):
            if self.mode == "eraser":
                if self.active:
                    clip, stack = self.active
                    for item in self.scene_obj.items(point):
                        if item.parentItem() is clip:
                            stack.push(RemoveItems(self, [item], "Erase Stroke"))
                            break
            else:
                style = self.settings.get(self.mode)
                self.stroke = StrokeItem(QColor(style["color"]), style["size"],
                                         self.mode == "highlighter", style.get("alpha", 1.0))
                self.stroke.add_point(point)
                self.attach_items([self.stroke])
        event.accept()

    def mouseMoveEvent(self, event):
        if self.stroke is not None:
            bounds = self.page_item.boundingRect()
            point = self._position(event)
            self.stroke.add_point(QPointF(min(max(0, point.x()), bounds.right()),
                                          min(max(0, point.y()), bounds.bottom())))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.stroke is not None:
            stroke, self.stroke = self.stroke, None
            self.active[1].push(AddItems(self, [stroke], "Add Stroke"))
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class ReferenceDocument(QWidget):
    def __init__(self, panel, path, stamp):
        super().__init__()
        self.panel, self.source, self.stamp = panel, path, stamp
        self.render = panel.owner.render
        self.group = ("standards", id(self))
        self.pdf = None
        self.pdf_stamp = ()
        self.sizes = {}
        self.count = 0
        self.info_job = None
        self.conversion_cancel = Event()
        self.style = ToolSettings()
        self.continuous = sys.platform == "win32"
        canvas_type = ContinuousReferenceView if self.continuous else ReferenceCanvas
        self.canvas = canvas_type(self.render, self.style)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        nav = row(root, (4, 0, 4, 0))
        if self.continuous:
            self.total = QLabel()
            self.total.setAccessibleName("Page position")
            nav.addWidget(self.total)
            self.canvas.visiblePageChanged.connect(self._navigation)
        else:
            self.previous = button(nav, "chevron-left", "Previous page", lambda: self.go_to(self.page.value() - 2))
            self.page = QSpinBox()
            self.page.setFixedWidth(66)
            self.page.setMinimum(1)
            self.page.setKeyboardTracking(False)
            self.page.setAccessibleName("Page number")
            self.page.valueChanged.connect(lambda value: self.go_to(value - 1))
            nav.addWidget(self.page)
            self.total = QLabel()
            nav.addWidget(self.total)
            self.next = button(nav, "chevron-right", "Next page", lambda: self.go_to(self.page.value()))
        nav.addStretch(1)
        if sys.platform == "darwin" and path.suffix.casefold() in TABLE_EXTS:
            button(nav, "detach", "Send table to Numbers…", lambda: panel.send_table_to_numbers(path))
        self.zoom = QToolButton()
        self.zoom.setText("100%")
        self.zoom.setToolTip("Zoom and fit")
        self.zoom.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.zoom)
        for title, slot in (("Zoom In", self.canvas.zoom_in), ("Zoom Out", self.canvas.zoom_out),
                            ("Fit Width", self.canvas.fit_width), ("Fit Page", self.canvas.fit_page),
                            ("Actual Size", self.canvas.actual_size)):
            menu.addAction(title, slot)
        self.zoom.setMenu(menu)
        nav.addWidget(self.zoom)
        self.canvas.zoomChanged.connect(lambda z: self.zoom.setText(f"{z:.0f}%"))

        ink = row(root, (4, 0, 4, 0))
        tools = [("pan", "select", "Pan"), ("pen", "pen", "Pen"),
                 ("highlighter", "highlighter", "Highlighter"), ("eraser", "eraser", "Erase annotation")]
        if self.continuous:
            tools.insert(3, ("text", "text", "Text"))
        self.tools = {
            mode: button(ink, icon, title, lambda _=False, m=mode: self._set_mode(m), True)
            for mode, icon, title in tools
        }
        self.colour = button(ink, "fill", "Ink colour", self._colour)
        if self.continuous:
            self.width = QLineEdit()
            self.width.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.size_validator = QDoubleValidator(self.width)
            self.size_validator.setDecimals(2)
            self.size_validator.setNotation(QDoubleValidator.Notation.StandardNotation)
            self.width.setValidator(self.size_validator)
            self.width.editingFinished.connect(self._update_size)
        else:
            self.width = QDoubleSpinBox()
            self.width.setSuffix(" pt")
            self.width.setDecimals(2)
            self.width.setSingleStep(0.5)
            self.width.setKeyboardTracking(False)
            self.width.valueChanged.connect(lambda w: self.style.set(self.canvas.mode, "size", w))
        self.width.setFixedWidth(65 if self.continuous else 70)
        self.width.setAccessibleName("Ink thickness")
        self.width.setToolTip("Ink thickness")
        ink.addWidget(self.width)
        ink.addStretch(1)
        history_row = nav if self.continuous else ink
        self.undo = button(history_row, "undo", "Undo annotation", lambda: self.canvas.history())
        self.redo = button(history_row, "redo", "Redo annotation", lambda: self.canvas.history(True))
        self.undo.setEnabled(False)
        self.redo.setEnabled(False)
        self.canvas.historyChanged.connect(self._history_enabled)

        if sys.platform == "win32" and path.suffix.casefold() in {".docx", ".xlsx", ".rtf"}:
            self.canvas.setToolTip("Offline preview: RTF is text only. Office previews omit active content, linked resources and live fields; spreadsheets use cached formula results. Layout may differ from Microsoft Office.")

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setContentsMargins(8, 4, 8, 4)
        root.addWidget(self.status)
        root.addWidget(self.canvas, 1)
        self.canvas.renderError.connect(self.message)
        self.canvas.pageReady.connect(self.status.hide)
        self.canvas.fit_page()
        self._set_mode("pan")
        self._navigation()

    def _history_enabled(self, undo, redo):
        self.undo.setEnabled(undo)
        self.redo.setEnabled(redo)

    def message(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def load(self, pdf):
        self.pdf = str(pdf)
        self.pdf_stamp = PageRef.stamp_for(self.pdf)
        if not self.continuous:
            self.go_to(0)
            return
        self.message("Opening reference…")

        def ready(result):
            self.info_job = None
            sizes = result.header.get("sizes")
            if not result.ok or not sizes:
                self.message(result.error or "This document has no readable pages.")
                return
            self.count = len(sizes)
            self.message("")
            refs = [PageRef(self.pdf, i, w, h, self.pdf_stamp) for i, (w, h) in enumerate(sizes)]
            self.canvas.set_pages(refs)
            self._navigation()

        self.info_job = self.render.submit("info", {"path": self.pdf, "all_sizes": True},
                                           ready, priority=PRI_PREVIEW, group=self.group)

    def go_to(self, index):
        if self.pdf is None or index < 0 or (self.count and index >= self.count):
            return
        if self.info_job is not None:
            self.render.cancel(self.info_job, self.group)
            self.info_job = None
        self.page.blockSignals(True)
        self.page.setValue(index + 1)
        self.page.blockSignals(False)
        self._navigation()
        if index in self.sizes:
            self._display(index)
            return
        self.message("Loading page…" if self.count else "Opening reference…")

        def ready(result):
            self.info_job = None
            if not result.ok or not result.header.get("sizes"):
                self.message(result.error or "This document has no readable pages.")
                ref = self.canvas.page_ref
                self.page.blockSignals(True)
                self.page.setValue(ref.page + 1 if ref else 1)
                self.page.blockSignals(False)
                self._navigation()
                return
            self.count = result.header["page_count"]
            self.sizes[index] = result.header["sizes"][0]
            self.page.setMaximum(self.count)
            self._display(index)
            self._navigation()

        self.info_job = self.render.submit("info", {"path": self.pdf, "size_limit": 1, "first_page": index},
                                           ready, priority=PRI_PREVIEW, group=self.group)

    def _display(self, index):
        w, h = self.sizes[index]
        self.message("")
        self.canvas.set_page(PageRef(self.pdf, index, w, h, self.pdf_stamp))

    def _navigation(self, *_):
        if self.continuous:
            self.total.setText(f"{self.canvas.current_page + 1} / {self.count}" if self.count else "")
            return
        self.total.setText(f"/ {self.count}" if self.count else "/ –")
        self.page.setEnabled(self.count > 1)
        self.previous.setEnabled(self.count > 1 and self.page.value() > 1)
        self.next.setEnabled(self.page.value() < self.count)

    def _set_mode(self, mode):
        self.canvas.set_mode(mode)
        for name, control in self.tools.items():
            control.setChecked(name == mode)
        is_ink = mode in ("pen", "highlighter", "text")
        self.colour.setEnabled(is_ink)
        self.width.setEnabled(is_ink)
        if is_ink:
            settings = self.style.get(mode)
            if self.continuous:
                self.size_validator.setRange(*RANGES[mode][:2], 2)
                self._show_size(settings["size"])
                label = "Text size in points" if mode == "text" else "Ink thickness in points"
                self.width.setAccessibleName(label)
                self.width.setToolTip(label)
            else:
                self.width.blockSignals(True)
                self.width.setRange(*RANGES[mode][:2])
                self.width.setValue(settings["size"])
                self.width.blockSignals(False)
            self._colour_icon(settings["color"])
        else:
            if self.continuous:
                self.width.clear()
            self.colour.setIcon(icons.icon("fill"))

    def _show_size(self, size):
        self.width.setText(self.size_validator.locale().toString(float(size), 'g', 5))

    def _update_size(self):
        mode = self.canvas.mode
        if mode in ("pen", "highlighter", "text"):
            size, valid = self.size_validator.locale().toDouble(self.width.text())
            if valid and self.width.hasAcceptableInput():
                self.style.set(mode, "size", size)
            self._show_size(self.style.get(mode)["size"])

    def _colour_icon(self, color):
        self.colour.setIcon(QIcon(icons.pixmap("fill", 16, color)))
        self.colour.setToolTip(f"Ink colour · {color}")

    def _colour(self):
        mode = self.canvas.mode
        if mode in ("pen", "highlighter", "text"):
            color = QColorDialog.getColor(QColor(self.style.get(mode)["color"]), self, "Reference ink colour")
            if color.isValid():
                self.style.set(mode, "color", color.name())
                self._colour_icon(color.name())

    def release(self):
        self.conversion_cancel.set()
        self.render.cancel_group(self.group)
        self.canvas.release()


class StandardsPanel(QWidget):
    def __init__(self, owner, dock):
        super().__init__()
        self.owner, self.dock = owner, dock
        self.folder = None
        self.scan_cancel = Event()
        self.scan_job = None
        self.pending = None
        self.scratch = None
        self.conversions = {}
        self.preview_jobs = set()
        self.folder_nodes = {}
        self.checked = set()
        self.file_icons = {}
        self.icon_provider = QFileIconProvider()
        self.setMinimumWidth(290)
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(6)
        top = row(root)
        button(top, "folder-open", f"Choose {REFERENCE_TITLE} folder…", self.choose_folder)
        button(top, "file", "Open reference files…", self.choose_files)
        self.browse_button = button(top, "book", "Browse and add documents", self._toggle_browser)
        self.back = button(top, "chevron-left", "Previous document tab", lambda: self._step(-1))
        self.forward = button(top, "chevron-right", "Next document tab", lambda: self._step(1))
        top.addStretch(1)
        self.float_button = button(top, "detach", f"Detach {REFERENCE_TITLE}", dock.toggle_floating)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        browser = QWidget()
        layout = QVBoxLayout(browser)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.folder_name = ElidedLabel(f"Choose a {REFERENCE_TITLE} folder", Qt.TextElideMode.ElideMiddle)
        layout.addWidget(self.folder_name)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter documents")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemChanged.connect(self._checked)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        layout.addWidget(self.tree, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.open_button = primary_button("Open Selected", "file")
        self.open_button.clicked.connect(self.open_selected)
        self.open_button.setEnabled(False)
        layout.addWidget(self.open_button)
        self.stack.addWidget(browser)
        self.tabs = QTabWidget()
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._arrows)
        self.tabs.tabBar().tabMoved.connect(self._arrows)
        self.stack.addWidget(self.tabs)
        self.stack.currentChanged.connect(self._browser_changed)
        self._arrows()

        previous = self.owner.settings.get("standards_folder")
        if isinstance(previous, str) and previous:
            self.load_folder(Path(previous))
        else:
            self.message.setText("Choose a local folder containing reference documents.")

    def sizeHint(self):
        return QSize(355, 600)

    def _browser_changed(self, index):
        self.browse_button.setToolTip("Return to open documents" if index == 0 and self.tabs.count()
                                      else "Browse and add documents")

    def _toggle_browser(self):
        if self.stack.currentIndex() == 0 and self.tabs.count():
            self.stack.setCurrentIndex(1)
        else:
            self.browse()

    def browse(self):
        self.stack.setCurrentIndex(0)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, f"Choose {REFERENCE_TITLE} Folder",
                                                  str(self.folder or Path.home()))
        if folder:
            path = Path(folder)
            self.owner.settings.set("standards_folder", str(path))
            self.owner.settings.save()
            self.load_folder(path)

    def load_folder(self, path):
        self.scan_cancel.set()
        self.scan_cancel = cancel = Event()
        self.folder = path
        self.pending = None
        self.folder_nodes.clear()
        self.checked.clear()
        self.tree.clear()
        self.search.clear()
        self.search.setEnabled(False)
        self.open_button.setEnabled(False)
        self.folder_name.setText(path.name or str(path))
        self.folder_name.setToolTip(str(path))
        self.message.setText("Scanning folder…")
        self.message.show()
        self.browse()

        def read():
            root = path.resolve(strict=True)
            files, skipped = scan_folder(root, cancel)
            return root, files, skipped

        def done(result, error):
            if cancel.is_set():
                return
            self.scan_job = None
            if error:
                self.message.setText("This folder is unavailable or unreadable. Choose another folder.")
                return
            self.folder, files, skipped = result
            self.pending = iter(files)
            self._populate(cancel, len(files), skipped)

        job = self.scan_job = Job(read)
        job.signals.finished.connect(done, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(job)

    def _populate(self, cancel, count, skipped):
        if cancel.is_set():
            return
        batch = list(islice(self.pending, 250))
        self.tree.blockSignals(True)
        for path in batch:
            parent = self.tree.invisibleRootItem()
            parents = tuple(path.parts[:-1])
            for i, name in enumerate(parents):
                key = parents[:i + 1]
                node = self.folder_nodes.get(key)
                if node is None:
                    node = QTreeWidgetItem(parent, [name])
                    node.setIcon(0, self.icon_provider.icon(QFileIconProvider.IconType.Folder))
                    self.folder_nodes[key] = node
                parent = node
            item = QTreeWidgetItem(parent, [path.name])
            item.setData(0, PATH_ROLE, str(path))
            item.setToolTip(0, str(path))
            item.setCheckState(0, Qt.CheckState.Unchecked)
            ext = path.suffix.casefold()
            if ext not in self.file_icons:
                self.file_icons[ext] = self.icon_provider.icon(QFileInfo(str(self.folder / path)))
            item.setIcon(0, self.file_icons[ext])
        self.tree.blockSignals(False)
        if batch:
            QTimer.singleShot(0, self, lambda: self._populate(cancel, count, skipped))
        else:
            self.pending = None
            self.search.setEnabled(True)
            self.message.setText(f"{skipped} folder or file entries could not be read." if skipped else
                                 "" if count else "No supported reference documents in this folder.")
            self.message.setVisible(bool(self.message.text()))

    def _filter(self, query):
        term = query.strip().casefold()

        def visit(item):
            path = item.data(0, PATH_ROLE)
            if path is not None:
                visible = term in path.casefold()
            else:
                children = [visit(item.child(i)) for i in range(item.childCount())]
                visible = any(children)
                if term and visible:
                    item.setExpanded(True)
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def _checked(self, item, _column):
        path = item.data(0, PATH_ROLE)
        if path is None:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self.checked.add(path)
        else:
            self.checked.discard(path)
        self.open_button.setEnabled(bool(self.checked))

    def _double_clicked(self, item, _column):
        path = item.data(0, PATH_ROLE)
        if path is not None:
            self.open_documents([path])

    def open_selected(self):
        self.open_documents(sorted(self.checked, key=str.casefold))

    def choose_files(self):
        patterns = " ".join("*" + ext for ext in sorted(STANDARDS_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(self, "Open Reference Documents",
                                               str(self.folder or Path.home()), f"Reference documents ({patterns})")
        self.open_paths(Path(path) for path in paths)

    def open_documents(self, paths):
        self.open_paths((self.folder / relative for relative in paths), root=self.folder)

    def open_paths(self, paths, root=None):
        for path in paths:
            ext = path.suffix.casefold()
            if ext not in STANDARDS_EXTS:
                continue
            source = path
            available = True
            try:
                source = path.resolve(strict=True)
                within_root = root is None or (source == path and source.is_relative_to(root))
                compatible = source.is_file() or (ext in IWORK_EXTS and source.is_dir())
                available = within_root and compatible and source.suffix.casefold() == ext
            except (OSError, RuntimeError):
                available = False
            for i in range(self.tabs.count()):
                if self.tabs.widget(i).source == source:
                    self.tabs.setCurrentIndex(i)
                    break
            else:
                stamp = PageRef.stamp_for(source)
                doc = ReferenceDocument(self, source, stamp)
                if ext not in self.file_icons:
                    self.file_icons[ext] = self.icon_provider.icon(QFileInfo(str(source)))
                idx = self.tabs.addTab(doc, self.file_icons[ext], source.name)
                self.tabs.setTabToolTip(idx, str(source))
                self.tabs.setCurrentIndex(idx)
                if not available or not stamp:
                    doc.message("This file is missing or unavailable. Close this tab and choose the file again.")
                elif ext == ".pdf":
                    doc.load(source)
                else:
                    self._convert(doc, ext)
        if self.tabs.count():
            self.stack.setCurrentIndex(1)
            if sys.platform == "win32":
                self.tabs.currentWidget().canvas.setFocus()
        self._arrows()

    def _convert(self, doc, ext):
        if sys.platform == "win32" and ext in {".doc", ".xls"}:
            doc.message("For an offline reference preview, save this legacy Office document as DOCX, XLSX or PDF first.")
            return
        key = (doc.source, doc.stamp)
        cacheable = not doc.source.is_dir()
        converted = self.conversions.get(key)
        if converted:
            doc.load(converted)
            return
        try:
            if self.scratch is None:
                self.scratch = tempfile.TemporaryDirectory(prefix="eng-standards-")
            out_dir = Path(tempfile.mkdtemp(dir=self.scratch.name))
        except OSError as exc:
            doc.message(f"Could not create a temporary preview folder. {exc}")
            return

        def ready(path, error):
            if error:
                doc.message(error)
            else:
                if cacheable:
                    self.conversions[key] = path
                doc.load(path)

        if ext in OPEN_EXTS:
            from .reference_formats import make_preview

            doc.message("Preparing a local text preview…")
            source, cancel = doc.source, doc.conversion_cancel
            job = Job(lambda: make_preview(source, out_dir / "Reference.pdf", cancel))
            self.preview_jobs.add(job)

            def finish(result, error):
                self.preview_jobs.discard(job)
                if not cancel.is_set():
                    ready(result, error)

            job.signals.finished.connect(finish, Qt.ConnectionType.QueuedConnection)
            QThreadPool.globalInstance().start(job)
            return

        if ext in IWORK_EXTS:
            kind = "iwork"
            args = {"op": "preview", "source": str(doc.source), "out_dir": str(out_dir)}
            doc.message("Preparing a local iWork preview. macOS may ask for Automation access to export a temporary copy.")
        else:
            kind = "tool"
            args = {"op": "office_to_pdf", "paths": [str(doc.source)], "out_dir": str(out_dir), "reference": True}
            doc.message("Preparing a local Office preview…")

        def done(result):
            ready(result.header["outputs"][0] if result.ok else None, result.error)

        self.owner.render.submit(kind, args, done, priority=PRI_EXPORT, group=doc.group)

    def send_table_to_numbers(self, source):
        from .export_dialog import ExportDialog

        ExportDialog(self, self.owner.render, self.owner.settings, [], destination="Numbers",
                     table=source).exec()

    def close_tab(self, index):
        doc = self.tabs.widget(index)
        doc.release()
        self.tabs.removeTab(index)
        doc.deleteLater()
        if not self.tabs.count():
            self.browse()

    def _step(self, delta):
        self.stack.setCurrentIndex(1)
        self.tabs.setCurrentIndex(self.tabs.currentIndex() + delta)
        if sys.platform == "win32" and self.tabs.currentWidget() is not None:
            self.tabs.currentWidget().canvas.setFocus()

    def _arrows(self, *_):
        current = self.tabs.currentIndex()
        self.back.setEnabled(current > 0)
        self.forward.setEnabled(0 <= current < self.tabs.count() - 1)
        self._browser_changed(self.stack.currentIndex())

    def shutdown(self):
        self.scan_cancel.set()
        for index in reversed(range(self.tabs.count())):
            self.close_tab(index)
        if self.scratch is not None:
            self.scratch.cleanup()


class StandardsDock(QDockWidget):
    """Move the same workspace between the active drawing window and a floating panel."""

    def __init__(self, owner):
        super().__init__(REFERENCE_TITLE, owner)
        self.setAccessibleName(REFERENCE_TITLE)
        self.owner = self.host = owner
        self.width_hint = 355
        self.setAllowedAreas(RIGHT)
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable |
                         QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.panel = StandardsPanel(owner, self)
        self.setWidget(self.panel)
        owner.addDockWidget(RIGHT, self)
        self.hide()
        self.topLevelChanged.connect(self._floating_changed)
        QApplication.instance().installEventFilter(self)

    def toggle_at(self, host):
        if self.isFloating():
            if self.isHidden():
                self.show()
            self.raise_()
            self.activateWindow()
        elif self.host is host and self.isVisible():
            self.hide()
        else:
            self._dock_at(host)

    def _dock_at(self, host):
        if self.host is not host:
            self.width_hint = max(self.panel.minimumWidth(), self.width())
            self.host.removeDockWidget(self)
            self.host = host
            host.addDockWidget(RIGHT, self)
        self.show()
        host.resizeDocks([self], [self.width_hint], Qt.Orientation.Horizontal)

    def toggle_floating(self):
        if self.isFloating():
            self.setFloating(False)
            self.host.resizeDocks([self], [self.width_hint], Qt.Orientation.Horizontal)
        else:
            self.width_hint = self.width()
            self.setFloating(True)
            self.resize(max(self.width_hint, 400), max(480, self.height()))
            self.raise_()

    def _floating_changed(self, floating):
        self.panel.float_button.setIcon(icons.icon("sidebar-right" if floating else "detach"))
        label = f"Reattach {REFERENCE_TITLE}" if floating else f"Detach {REFERENCE_TITLE}"
        self.panel.float_button.setToolTip(label)
        self.panel.float_button.setAccessibleName(label)

    def eventFilter(self, watched: QObject, event: QEvent):
        if sys.platform == "win32" and event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            if self._annotation_shortcut(event):
                return True
        if (event.type() == QEvent.Type.WindowActivate and self.isVisible() and not self.isFloating()
                and watched is not self.host
                and (watched is self.owner or watched in self.owner._mark_windows.values())):
            QTimer.singleShot(0, self, lambda: self._follow_active(watched))
        return False

    def _annotation_shortcut(self, event):
        if not self.isVisible() or self.panel.stack.currentIndex() != 1:
            return False
        focus = QApplication.focusWidget()
        if focus is None or not self.isAncestorOf(focus):
            return False
        if (event.type() == QEvent.Type.ShortcutOverride
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right)):
            # The host Viewer binds these keys to its own page navigation.
            event.accept()
            return True
        undo = event.matches(QKeySequence.StandardKey.Undo)
        redo = event.matches(QKeySequence.StandardKey.Redo) or (
            event.key() == Qt.Key.Key_Z
            and event.modifiers() == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        )
        if not undo and not redo:
            return False
        doc = self.panel.tabs.currentWidget()
        if doc is None:
            return False
        if isinstance(focus, QLineEdit) or doc.canvas.editing is not None:
            # Let the editor handle its own text undo, even if the host has an Undo action.
            if event.type() == QEvent.Type.ShortcutOverride:
                event.accept()
                return True
            return False
        if event.type() == QEvent.Type.KeyPress:
            doc.canvas.history(redo)
        event.accept()
        return True

    def _follow_active(self, host):
        if QApplication.activeWindow() is host and self.isVisible() and not self.isFloating():
            self._dock_at(host)

    def release_host(self, host):
        if self.host is host:
            floating, visible, geometry = self.isFloating(), self.isVisible(), self.geometry()
            if floating:
                self.setFloating(False)
            self._dock_at(self.owner)
            if floating:
                self.setFloating(True)
                self.setGeometry(geometry)
            self.setVisible(visible)

    def shutdown(self):
        QApplication.instance().removeEventFilter(self)
        self.panel.shutdown()
        self.close()
