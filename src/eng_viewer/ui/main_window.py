"""Main window: library, page viewer, inspector (pages + PowerPoint queue)."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import shiboken6
from PySide6.QtCore import QByteArray, QObject, QPointF, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QIcon, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__
from ..core.config import QUALITY_DPI, QUALITY_LABELS, Settings
from ..core.naturalsort import sort_paths
from ..core.render_service import PRI_INTERACTIVE, PRI_PREFETCH, RenderResult, RenderService
from ..core.documents import IMAGE_EXTS, is_document_name, kind_for
from ..core.scanning import scan_folder
from ..core.tool_support import OFFICE_EXTS
from ..platform import reveal_in_file_manager, reveal_label
from . import icons
from .library import LibraryPanel, paper_name
from .page_view import PageRef, PageView
from .pages_panel import PagesPanel
from .queue_panel import QueueEntry, QueuePanel
from .render_helpers import FullPageRender
from .theme import IS_MAC, ThemeManager
from .thumbnails import ThumbnailProvider
from .widgets import (
    SegmentedControl,
    ElidedLabel, EmptyState, Spinner, Toast, label, primary_button, secondary_button, separator, tip, tool_button,
)

ZOOM_PRESETS = [25, 50, 75, 100, 125, 150, 200, 300, 400, 800, 1600]


class _ScanBridge(QObject):
    done = Signal(int, object)  # generation, ScanResult


class _SaveRunnable(QRunnable):
    def __init__(self, image: QImage, path: str, bridge):
        super().__init__()
        self.image, self.path, self.bridge = image, path, bridge

    def run(self):
        self.bridge.saved.emit(self.path, self.image.save(self.path, "PNG"))


class _SaveBridge(QObject):
    saved = Signal(str, bool)


class Toolbar(QWidget):
    """Top bar. Empty areas drag the window (it sits in the title bar on macOS)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Toolbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def _in_title_bar(self) -> bool:
        return bool(getattr(self.window(), "_unified", False))

    def mousePressEvent(self, event):
        if (self._in_title_bar() and event.button() == Qt.MouseButton.LeftButton
                and self.childAt(event.position().toPoint()) in (None, self)):
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._in_title_bar() and self.childAt(event.position().toPoint()) in (None, self):
            w = self.window()
            w.showNormal() if w.isMaximized() else w.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class DocInfo(QWidget):
    copyFilename = Signal()
    reveal = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 12, 10)
        lay.setSpacing(3)
        self.name = QLabel("No drawing open")
        self.name.setProperty("role", "title")
        self.name.setWordWrap(True)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.name)
        self.meta = label("", "small")
        lay.addWidget(self.meta)
        self.folder = ElidedLabel("")
        self.folder.setProperty("role", "tertiary")
        lay.addWidget(self.folder)
        row = QHBoxLayout()
        row.setContentsMargins(0, 6, 0, 0)
        row.setSpacing(6)
        self.copy_btn = secondary_button("Copy Filename", "file-text")
        self.copy_btn.clicked.connect(self.copyFilename.emit)
        row.addWidget(self.copy_btn)
        self.reveal_btn = tool_button("reveal", reveal_label(), size=28, icon_size=16)
        self.reveal_btn.clicked.connect(self.reveal.emit)
        row.addWidget(self.reveal_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.set_doc(None)

    def set_doc(self, path: str | None, pages: int = 0, page: int = 0, size: tuple | None = None, info: dict | None = None):
        self.copy_btn.setEnabled(bool(path))
        self.reveal_btn.setEnabled(bool(path))
        if not path:
            self.name.setText("No drawing open")
            self.meta.setText("")
            self.folder.setText("")
            return
        p = Path(path)
        self.name.setText(p.name)
        self.name.setToolTip(path)
        meta = []
        info = info or {}
        if info.get("kind") == "image":
            meta.append(f"{p.suffix.lstrip('.').upper()} image")
            if info.get("pixels"):
                w, h = info["pixels"]
                meta.append(f"{w} × {h} px")
            if pages > 1:
                meta.append(f"frame {page + 1} of {pages}")
        else:
            if pages:
                meta.append(f"Page {page + 1} of {pages}")
            if size and size[0] > 0:
                meta.append(f"{paper_name(*size)} · {size[0] * 25.4 / 72:.0f} × {size[1] * 25.4 / 72:.0f} mm")
        self.meta.setText("  ·  ".join(meta))
        self.folder.setText(str(p.parent))


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, render: RenderService, app_icon: QPixmap | None = None):
        super().__init__()
        self.settings = settings
        self.render = render
        self.app_icon = app_icon
        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.setMinimumSize(880, 560)
        # Our toolbar sits in the (transparent) macOS title bar and leaves room
        # for the traffic lights itself, so don't let Qt push content down.
        self.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)

        thin = bool(settings.get("enhance_thin_lines"))
        self.thumbs = ThumbnailProvider(render, disk_cache=bool(settings.get("thumbnail_disk_cache")), thin_lines=thin, parent=self)
        self.thumbs.prune_async()

        # Document state
        self.source_root: Path | None = None
        self.source_files: list[Path] | None = None
        self.files: list[Path] = []
        self.doc_path: str | None = None
        self.page_index = 0
        self.page_count = 0
        self.page_sizes: list[tuple[float, float]] = []
        self._doc_info: dict[str, dict] = {}
        self._info_callbacks: dict[str, list] = {}
        self._scan_gen = 0
        self._scan_bridge = _ScanBridge(self)
        self._scan_bridge.done.connect(self._on_scan_done)
        self._save_bridge = _SaveBridge(self)
        self._save_bridge.saved.connect(self._on_png_saved)
        self._mark_windows: dict[tuple, QWidget] = {}
        self._copy_job: FullPageRender | None = None
        self._unified = False
        self._mode = "viewer"
        self.tools = None  # PDF Tools workspace, created on first use
        self._continuity = None  # iPhone scanning (macOS), created on first use
        self._mark_after_open = False

        self._build_ui()
        self._build_menus()
        self._update_nav()
        self._restore_state()
        ThemeManager.instance().changed.connect(self._on_theme_changed)
        render.activityChanged.connect(self._on_activity)
        self.prepare_native_window()

    # ================================================================== UI
    def _build_ui(self):
        self.view = PageView(self.render)
        self.view.thin_lines = bool(self.settings.get("enhance_thin_lines"))
        self.view.setAcceptDrops(False)  # file drops are handled by the window
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        self.library = LibraryPanel(self.thumbs)
        self.library.setMinimumWidth(220)
        self.library.fileActivated.connect(lambda p: self.open_document(p, 0))
        self.library.openFolderRequested.connect(self.open_folder_dialog)
        self.library.rescanRequested.connect(self.rescan)
        self.library.recursiveToggled.connect(self._set_recursive)
        self.library.modeChanged.connect(self._set_library_mode)
        self.library.thumbSizeChanged.connect(self._thumb_size_changed)
        self.library.toolRequested.connect(self.open_tool)
        self.library.markRequested.connect(self._mark_path)
        self.library.imagesToggled.connect(self._set_include_images)
        self.splitter.addWidget(self.library)

        self.viewer_stack = QStackedWidget()
        self.empty = EmptyState(self.app_icon)
        self.empty.openFolder.connect(self.open_folder_dialog)
        self.empty.openRecent.connect(lambda f: self.load_folder(Path(f)))
        self.viewer_stack.addWidget(self.empty)
        viewer_box = QWidget()
        vb = QVBoxLayout(viewer_box)
        vb.setContentsMargins(0, 0, 0, 0)
        self.view.zoomChanged.connect(self._on_zoom_changed)
        self.view.fitModeChanged.connect(self._on_fit_mode_changed)
        self.view.renderError.connect(self._on_render_error)
        vb.addWidget(self.view)
        self.viewer_stack.addWidget(viewer_box)
        self.message_page = self._build_message_page()
        self.viewer_stack.addWidget(self.message_page)
        self.content_stack = QStackedWidget()
        self.viewer_split = QSplitter(Qt.Orientation.Horizontal)
        self.viewer_split.setChildrenCollapsible(False)
        self.viewer_split.setHandleWidth(1)
        self.viewer_split.addWidget(self.viewer_stack)
        self.content_stack.addWidget(self.viewer_split)
        self.splitter.addWidget(self.content_stack)
        self.toast = Toast(self.content_stack)

        self.inspector = QWidget()
        self.inspector.setObjectName("Inspector")
        self.inspector.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.inspector.setMinimumWidth(250)
        il = QVBoxLayout(self.inspector)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(0)
        self.doc_info = DocInfo()
        self.doc_info.copyFilename.connect(self.copy_filename)
        self.doc_info.reveal.connect(lambda: reveal_in_file_manager(self.doc_path))
        il.addWidget(self.doc_info)
        il.addWidget(separator(False))
        self.inspector_split = QSplitter(Qt.Orientation.Vertical)
        self.inspector_split.setChildrenCollapsible(False)
        self.inspector_split.setHandleWidth(1)
        self.pages = PagesPanel(self.thumbs)
        self.pages.pageActivated.connect(self.go_to_page)
        self.inspector_split.addWidget(self.pages)
        self.queue = QueuePanel(self.thumbs)
        self.queue.addCurrentRequested.connect(self.add_current_to_queue)
        self.queue.addSelectedRequested.connect(self.add_selected_to_queue)
        self.queue.addAllRequested.connect(self.add_all_to_queue)
        self.queue.exportRequested.connect(self.export_queue)
        self.queue.showEntryRequested.connect(lambda e: self.open_document(e.doc_path, e.page_index))
        self.queue.filesDropped.connect(self._add_paths_to_queue)
        self.queue.exportPdfRequested.connect(self.export_queue_pdf)
        self.inspector_split.addWidget(self.queue)
        self.inspector_split.setSizes([300, 300])
        il.addWidget(self.inspector_split, 1)
        self.viewer_split.addWidget(self.inspector)
        self.viewer_split.setStretchFactor(0, 1)
        self.viewer_split.setStretchFactor(1, 0)
        self.viewer_split.setSizes([900, 300])
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([300, 1200])
        root.addWidget(self.splitter, 1)

        root.addWidget(self._build_status())
        self.setCentralWidget(central)

    def _build_toolbar(self) -> QWidget:
        bar = Toolbar()
        bar.setFixedHeight(52)
        lay = QHBoxLayout(bar)
        self._toolbar_layout = lay
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(4)
        self.btn_library = tool_button("sidebar-left", tip("Show Library", "Ctrl+Alt+1"), checkable=True)
        self.btn_library.setChecked(True)
        self.btn_library.toggled.connect(self._toggle_library)
        lay.addWidget(self.btn_library)
        self.btn_open = tool_button("folder-open", tip("Open Folder…", QKeySequence.StandardKey.Open))
        self.btn_open.clicked.connect(self.open_folder_dialog)
        lay.addWidget(self.btn_open)
        self.btn_scan = tool_button("iphone", tip("Scan with iPhone…", "Ctrl+Shift+I"))
        self.btn_scan.clicked.connect(lambda: self.start_scan(self.btn_scan))
        self.btn_scan.setVisible(IS_MAC)
        lay.addWidget(self.btn_scan)
        lay.addSpacing(8)
        self.mode_ctrl = SegmentedControl([("viewer", "Viewer", "viewer", "Drawing viewer"),
                                           ("tools", "PDF Tools", "tools", "Edit, convert and compare PDFs")], height=28)
        self.mode_ctrl.changed.connect(self.set_mode)
        lay.addWidget(self.mode_ctrl)
        lay.addSpacing(10)
        self.title_label = ElidedLabel("", Qt.TextElideMode.ElideMiddle)
        self.title_label.setProperty("role", "title")
        self.title_label.setMinimumWidth(60)
        lay.addWidget(self.title_label, 1)
        lay.addSpacing(8)

        self.viewer_controls = QWidget()
        vl = QHBoxLayout(self.viewer_controls)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(4)
        self.btn_prev = tool_button("chevron-left", tip("Previous Page", "Left"))
        self.btn_prev.clicked.connect(self.prev_page)
        vl.addWidget(self.btn_prev)
        self.page_field = QLineEdit()
        self.page_field.setProperty("variant", "page")
        self.page_field.setFixedWidth(42)
        self.page_field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_field.setToolTip("Go to page")
        self.page_field.setAccessibleName("Page number")
        self.page_field.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.page_field.returnPressed.connect(self._page_field_entered)
        vl.addWidget(self.page_field)
        self.page_total = label("/ –", "secondary")
        self.page_total.setMinimumWidth(34)
        vl.addWidget(self.page_total)
        self.btn_next = tool_button("chevron-right", tip("Next Page", "Right"))
        self.btn_next.clicked.connect(self.next_page)
        vl.addWidget(self.btn_next)
        vl.addWidget(separator(True))
        self.btn_zoom_out = tool_button("zoom-out", tip("Zoom Out", QKeySequence.StandardKey.ZoomOut))
        self.btn_zoom_out.clicked.connect(self.view.zoom_out)
        vl.addWidget(self.btn_zoom_out)
        self.zoom_btn = QToolButton()
        self.zoom_btn.setProperty("variant", "ghost")
        self.zoom_btn.setText("100%")
        self.zoom_btn.setFixedSize(58, 28)
        self.zoom_btn.setToolTip("Zoom level")
        self.zoom_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        zm = QMenu(self.zoom_btn)
        for pct in ZOOM_PRESETS:
            zm.addAction(f"{pct}%", lambda p=pct: self.view.set_zoom_percent(p))
        zm.addSeparator()
        zm.addAction("Fit Width", self.view.fit_width)
        zm.addAction("Fit Page", self.view.fit_page)
        self.zoom_btn.setMenu(zm)
        vl.addWidget(self.zoom_btn)
        self.btn_zoom_in = tool_button("zoom-in", tip("Zoom In", QKeySequence.StandardKey.ZoomIn))
        self.btn_zoom_in.clicked.connect(self.view.zoom_in)
        vl.addWidget(self.btn_zoom_in)
        self.btn_fit_width = tool_button("fit-width", tip("Fit Width", "Ctrl+1"), checkable=True)
        self.btn_fit_width.clicked.connect(lambda: self.view.fit_width())
        vl.addWidget(self.btn_fit_width)
        self.btn_fit_page = tool_button("fit-page", tip("Fit Page", "Ctrl+2"), checkable=True)
        self.btn_fit_page.clicked.connect(lambda: self.view.fit_page())
        vl.addWidget(self.btn_fit_page)
        self.btn_actual = tool_button("actual-size", tip("Actual Size (100%)", "Ctrl+0"))
        self.btn_actual.clicked.connect(self.view.actual_size)
        vl.addWidget(self.btn_actual)
        vl.addWidget(separator(True))
        self.btn_copy = tool_button("copy-image", tip("Copy Page as Image", QKeySequence.StandardKey.Copy), text="Copy")
        self.btn_copy.clicked.connect(self.copy_page_image)
        vl.addWidget(self.btn_copy)
        self.btn_queue_add = tool_button("queue-add", tip("Add Page to PowerPoint Queue", "Ctrl+D"))
        self.btn_queue_add.clicked.connect(self.add_current_to_queue)
        vl.addWidget(self.btn_queue_add)
        vl.addSpacing(4)
        self.btn_mark = primary_button("MARK", "mark")
        self.btn_mark.setToolTip(tip("Mark up this page (the original is never modified)", self._mark_shortcut()))
        self.btn_mark.clicked.connect(self.open_mark)
        vl.addWidget(self.btn_mark)
        vl.addSpacing(4)
        self.btn_inspector = tool_button("sidebar-right", tip("Show Inspector", "Ctrl+Alt+2"), checkable=True)
        self.btn_inspector.setChecked(True)
        self.btn_inspector.toggled.connect(self._toggle_inspector)
        vl.addWidget(self.btn_inspector)
        lay.addWidget(self.viewer_controls)
        return bar

    def _build_status(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("StatusStrip")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setFixedHeight(24)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(8)
        self.status_left = ElidedLabel("")
        self.status_left.setProperty("role", "tertiary")
        lay.addWidget(self.status_left, 1)
        self.spinner = Spinner(12)
        self.spinner.hide()
        lay.addWidget(self.spinner)
        self.status_right = label("", "tertiary")
        lay.addWidget(self.status_right)
        return bar

    def _build_message_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addStretch(2)
        self.msg_icon = QLabel()
        self.msg_icon.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.msg_icon)
        self.msg_title = label("", "heading")
        self.msg_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.msg_title)
        self.msg_body = label("", "secondary", wrap=True)
        self.msg_body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.msg_body)
        lay.addStretch(3)
        return w

    def _show_message(self, icon_name: str, title: str, body: str, color_role: str = "text_tertiary"):
        self.msg_icon.setPixmap(icons.pixmap(icon_name, 40, icons._resolve(color_role)))
        self.msg_title.setText(title)
        self.msg_body.setText(body)
        self.viewer_stack.setCurrentWidget(self.message_page)

    # --------------------------------------------------------------- menus
    def _mark_shortcut(self) -> str:
        return "Ctrl+Shift+M" if IS_MAC else "Ctrl+M"

    def _act(self, menu: QMenu, text: str, slot, shortcut=None, icon_name: str | None = None, checkable=False) -> QAction:
        a = QAction(text, self)
        if shortcut is not None:
            if isinstance(shortcut, (list, tuple)):
                a.setShortcuts([QKeySequence(s) if isinstance(s, str) else QKeySequence(s) for s in shortcut])
            else:
                a.setShortcut(QKeySequence(shortcut))
        if icon_name and not IS_MAC:
            a.setIcon(icons.icon(icon_name, role="text"))
        a.setCheckable(checkable)
        if checkable:
            a.toggled.connect(slot)
        else:
            a.triggered.connect(lambda _=False: slot())
        menu.addAction(a)
        return a

    def _build_menus(self):
        mb = self.menuBar()
        f = mb.addMenu("&File")
        self._act(f, "Open Folder…", self.open_folder_dialog, QKeySequence.StandardKey.Open, "folder-open")
        self._act(f, "Open Files…", self.open_files_dialog, "Ctrl+Shift+O", "file")
        if IS_MAC:
            self._act(f, "Scan with iPhone…", lambda: self.start_scan(self.btn_scan), "Ctrl+Shift+I", "iphone")
        self.recent_menu = f.addMenu("Open Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        f.addSeparator()
        self.act_rescan = self._act(f, "Rescan Folder", self.rescan, "Ctrl+R", "refresh")
        self.act_reveal = self._act(f, reveal_label(), lambda: reveal_in_file_manager(self.doc_path), "Ctrl+Shift+R", "reveal")
        f.addSeparator()
        self.act_save_png = self._act(f, "Save Page as PNG…", self.save_page_png, "Ctrl+Shift+S", "save")
        self.act_export = self._act(f, "Export Queue to PowerPoint…", self.export_queue, "Ctrl+E", "presentation")
        self._act(f, "Export Queue as PDF…", self.export_queue_pdf, "Ctrl+Shift+E", "save")
        f.addSeparator()
        self._act(f, "Close Window", self.close, QKeySequence.StandardKey.Close)
        prefs = self._act(f, "Settings…", self.show_preferences, QKeySequence.StandardKey.Preferences, "settings")
        prefs.setMenuRole(QAction.MenuRole.PreferencesRole)
        quit_act = self._act(f, "Quit", QApplication.quit, QKeySequence.StandardKey.Quit)
        quit_act.setMenuRole(QAction.MenuRole.QuitRole)

        e = mb.addMenu("&Edit")
        self.act_copy = self._act(e, "Copy Page as Image", self.copy_page_image, QKeySequence.StandardKey.Copy, "copy-image")
        self.act_copy_name = self._act(e, "Copy Filename", self.copy_filename, "Ctrl+Shift+C", "file-text")
        e.addSeparator()
        self._act(e, "Filter Library", self._focus_search, QKeySequence.StandardKey.Find, "search")

        v = mb.addMenu("&View")
        mg = QActionGroup(self)
        self.act_mode_viewer = self._act(v, "Viewer", lambda: self.set_mode("viewer"), "Ctrl+Alt+V", checkable=False)
        self.act_mode_tools = self._act(v, "PDF Tools", lambda: self.set_mode("tools"), "Ctrl+Alt+T", checkable=False)
        for a in (self.act_mode_viewer, self.act_mode_tools):
            a.setCheckable(True)
            mg.addAction(a)
        self.act_mode_viewer.setChecked(True)
        v.addSeparator()
        self.act_zoom_in = self._act(v, "Zoom In", self.view.zoom_in, [QKeySequence.StandardKey.ZoomIn, "Ctrl+="], "zoom-in")
        self.act_zoom_out = self._act(v, "Zoom Out", self.view.zoom_out, QKeySequence.StandardKey.ZoomOut, "zoom-out")
        self.act_actual = self._act(v, "Actual Size", self.view.actual_size, "Ctrl+0", "actual-size")
        self.act_fit_width = self._act(v, "Fit Width", self.view.fit_width, "Ctrl+1", "fit-width")
        self.act_fit_page = self._act(v, "Fit Page", self.view.fit_page, "Ctrl+2", "fit-page")
        v.addSeparator()
        self.act_show_library = self._act(v, "Show Library", self.btn_library.setChecked, "Ctrl+Alt+1", checkable=True)
        self.act_show_inspector = self._act(v, "Show Inspector", self.btn_inspector.setChecked, "Ctrl+Alt+2", checkable=True)
        self.act_show_library.setChecked(True)
        self.act_show_inspector.setChecked(True)
        lm = v.addMenu("Library")
        g = QActionGroup(self)
        self.act_tree = lm.addAction("Folder Tree", lambda: self._set_library_mode("tree"))
        self.act_flat = lm.addAction("All Files (flat list)", lambda: self._set_library_mode("flat"))
        for a in (self.act_tree, self.act_flat):
            a.setCheckable(True)
            g.addAction(a)
        self.act_recursive = lm.addAction("Include Subfolders")
        self.act_recursive.setCheckable(True)
        self.act_recursive.toggled.connect(self._set_recursive)
        self.act_images = lm.addAction("Include Images")
        self.act_images.setCheckable(True)
        self.act_images.setChecked(bool(self.settings.get("include_images", True)))
        self.act_images.toggled.connect(self._set_include_images)
        v.addSeparator()
        am = v.addMenu("Appearance")
        tg = QActionGroup(self)
        self.theme_actions = {}
        for key, text in (("system", "Use System Setting"), ("light", "Light"), ("dark", "Dark")):
            a = am.addAction(text, lambda k=key: self.set_theme(k))
            a.setCheckable(True)
            tg.addAction(a)
            self.theme_actions[key] = a
        self.act_thin = self._act(v, "Enhance Thin Lines", self.set_thin_lines, None, checkable=True)
        if not IS_MAC:
            v.addSeparator()
            self._act(v, "Full Screen", self._toggle_fullscreen, "F11")

        go = mb.addMenu("&Go")
        self.act_prev = self._act(go, "Previous Page", self.prev_page, ["Left", "PgUp"], "chevron-left")
        self.act_next = self._act(go, "Next Page", self.next_page, ["Right", "PgDown"], "chevron-right")
        self.act_first = self._act(go, "First Page", self.first_page, "Home")
        self.act_last = self._act(go, "Last Page", self.last_page, "End")
        go.addSeparator()
        self.act_prev_doc = self._act(go, "Previous Drawing", lambda: self._step_document(-1), "Ctrl+Shift+Up", "chevrons-left")
        self.act_next_doc = self._act(go, "Next Drawing", lambda: self._step_document(1), "Ctrl+Shift+Down", "chevrons-right")
        go.addSeparator()
        self._act(go, "Go to Page…", self._focus_page_field, "Ctrl+G")

        t = mb.addMenu("&Tools")
        self.act_mark = self._act(t, "MARK Current Page…", self.open_mark, self._mark_shortcut(), "mark")
        t.addSeparator()
        self.act_queue_add = self._act(t, "Add Current Page to Queue", self.add_current_to_queue, "Ctrl+D", "queue-add")
        self._act(t, "Add Selected PDFs to Queue", self.add_selected_to_queue, "Ctrl+Shift+D")
        self._act(t, "Add All PDFs to Queue", self.add_all_to_queue)
        t.addSeparator()
        self._act(t, "Export Queue to PowerPoint…", self.export_queue, None, "presentation")
        t.addSeparator()
        pt = t.addMenu("PDF Tools")
        self._act(pt, "All Tools", lambda: self.open_tool(""))
        pt.addSeparator()
        for tid, text in (("organize", "Organize Pages"), ("merge", "Merge"), ("split", "Split"), ("compress", "Compress"),
                          ("ocr", "Make Searchable (OCR)"), ("images_to_pdf", "Images to PDF"), ("to_images", "PDF to Images"),
                          ("to_word", "PDF to Word"), ("to_excel", "PDF to Excel"), ("watermark", "Watermark && Stamp"),
                          ("page_numbers", "Page Numbers"), ("redact", "Redact"), ("protect", "Protect"), ("compare", "Compare Drawings")):
            self._act(pt, text, lambda tid=tid: self.open_tool(tid, self._tool_inputs()))

        if IS_MAC:
            w = mb.addMenu("Window")
            self._act(w, "Minimize", self.showMinimized, "Ctrl+M")
            self._act(w, "Zoom", lambda: self.showNormal() if self.isMaximized() else self.showMaximized())
        h = mb.addMenu("&Help")
        self._act(h, "Keyboard Shortcuts", self.show_shortcuts, "Ctrl+/", "keyboard")
        about = self._act(h, f"About {APP_NAME}", self.show_about, None, "info")
        about.setMenuRole(QAction.MenuRole.AboutRole)

    # ======================================================== state restore
    def _restore_state(self):
        s = self.settings
        geo = s.get("window_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        else:
            scr = QGuiApplication.primaryScreen().availableGeometry()
            self.resize(min(1480, int(scr.width() * 0.88)), min(920, int(scr.height() * 0.88)))
        for key, splitter in (("main_splitter2", self.splitter), ("viewer_splitter", self.viewer_split),
                              ("inspector_splitter", self.inspector_split)):
            st = s.get(key)
            if st:
                splitter.restoreState(QByteArray.fromBase64(st.encode()))
        self.btn_library.setChecked(bool(s.get("library_visible")))
        self.btn_inspector.setChecked(bool(s.get("inspector_visible")))
        mode = s.get("file_view_mode")
        self.library.set_mode(mode)
        (self.act_tree if mode == "tree" else self.act_flat).setChecked(True)
        rec = bool(s.get("recursive_scan"))
        self.library.set_recursive(rec)
        self.library.set_include_images(bool(s.get("include_images", True)))
        self.act_recursive.blockSignals(True)
        self.act_recursive.setChecked(rec)
        self.act_recursive.blockSignals(False)
        self.library.set_thumb_height(int(s.get("file_thumb_height")))
        self.theme_actions.get(s.get("theme"), self.theme_actions["system"]).setChecked(True)
        self.act_thin.blockSignals(True)
        self.act_thin.setChecked(bool(s.get("enhance_thin_lines")))
        self.act_thin.blockSignals(False)
        self.empty.set_recent(s.get("recent_folders"))
        self.viewer_stack.setCurrentWidget(self.empty)

    def restore_last_session(self):
        last = self.settings.get("last_folder")
        if last and Path(last).is_dir():
            self.load_folder(Path(last), restore_doc=self.settings.get("last_document"), restore_page=int(self.settings.get("last_page", 0) or 0))

    def save_state(self):
        s = self.settings
        s.set("window_geometry", bytes(self.saveGeometry().toBase64()).decode())
        s.set("main_splitter2", bytes(self.splitter.saveState().toBase64()).decode())
        s.set("viewer_splitter", bytes(self.viewer_split.saveState().toBase64()).decode())
        s.set("inspector_splitter", bytes(self.inspector_split.saveState().toBase64()).decode())
        s.set("library_visible", self.btn_library.isChecked())
        s.set("inspector_visible", self.btn_inspector.isChecked())
        s.set("last_document", self.doc_path)
        s.set("last_page", self.page_index)
        s.save()
        self.thumbs.save_info()

    def prepare_native_window(self):
        """Put the toolbar into the macOS title bar. Call before the first show().

        Done on the not-yet-visible native window: changing the title bar while
        AppKit is ordering the window in makes it fall back to its minimum size.
        """
        if IS_MAC and not self._unified:
            from ..platform.macos import unify_title_bar

            size = self.size()
            self.winId()  # create the native window now
            self._unified = unify_title_bar(self)
            self.resize(size)
            self._apply_toolbar_insets()

    def showEvent(self, event):
        if IS_MAC and not self._unified:
            self.prepare_native_window()
        super().showEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == event.Type.WindowStateChange:
            self._apply_toolbar_insets()

    def _apply_toolbar_insets(self):
        left = 10
        if IS_MAC and self._unified and not self.isFullScreen():
            left = 84  # room for the traffic-light buttons
        self._toolbar_layout.setContentsMargins(left, 0, 10, 0)

    def closeEvent(self, event):
        for w in list(self._mark_windows.values()):
            if not w.close():
                event.ignore()
                return
        self.save_state()
        self.thumbs.cancel_prefetch()
        self.view.release()
        super().closeEvent(event)

    # ====================================================== loading sources
    def open_folder_dialog(self):
        start = str(self.source_root) if self.source_root else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder of PDF drawings", start)
        if folder:
            self.load_folder(Path(folder))

    def open_files_dialog(self):
        start = str(self.source_root) if self.source_root else str(Path.home())
        pats = " ".join(f"*{e}" for e in sorted({".pdf"} | IMAGE_EXTS))
        files, _ = QFileDialog.getOpenFileNames(self, "Open drawings and images", start, f"PDFs and images ({pats})")
        if files:
            self.open_paths([Path(f) for f in files])

    def open_paths(self, paths: list[Path]):
        """Folders, PDFs or a mix (Finder open events, drag and drop, CLI)."""
        paths = [Path(p) for p in paths if Path(p).exists()]
        folders = [p for p in paths if p.is_dir()]
        pdfs = [p for p in paths if p.is_file() and is_document_name(p.name)]
        if folders:
            self.load_folder(folders[0])
        elif len(pdfs) == 1:
            pdf = pdfs[0]
            if self.source_root is not None and any(str(f) == str(pdf) for f in self.files):
                self.open_document(str(pdf), 0)  # already in the library
            else:
                self.load_folder(pdf.parent, restore_doc=str(pdf), restore_page=0, recursive=False)
        elif pdfs:
            self.load_files(pdfs)

    def load_folder(self, folder: Path, restore_doc: str | None = None, restore_page: int = 0, recursive: bool | None = None):
        folder = Path(folder)
        self.source_root = folder
        self.source_files = None
        self.settings.set("last_folder", str(folder))
        self.settings.add_recent_folder(str(folder))
        self.settings.save()
        self.empty.set_recent(self.settings.get("recent_folders"))
        self._scan_gen += 1
        gen = self._scan_gen
        rec = bool(self.settings.get("recursive_scan")) if recursive is None else recursive
        images = bool(self.settings.get("include_images", True))
        self._pending_restore = (restore_doc, restore_page)
        self.library.title.setText(folder.name or str(folder))
        self.library.count_label.setText("Scanning…")
        self.spinner.set_running(True)
        self.thumbs.forget_stamps()
        bridge = self._scan_bridge

        def work():
            res = scan_folder(folder, recursive=rec, should_cancel=lambda: gen != self._scan_gen,
                              include_images=images)
            bridge.done.emit(gen, res)

        threading.Thread(target=work, name="folder-scan", daemon=True).start()

    def load_files(self, files: list[Path]):
        self._scan_gen += 1
        files = sort_paths(files)
        self.source_files = files
        try:
            common = Path(os.path.commonpath([str(f.parent) for f in files]))
        except ValueError:
            common = None
        self.source_root = common
        self._apply_files(files, f"{len(files)} files", str(common or ""))
        self.open_document(str(files[0]), 0)

    def rescan(self):
        if self.source_files is not None:
            self.load_files([f for f in self.source_files if f.exists()])
        elif self.source_root is not None:
            self.load_folder(self.source_root, restore_doc=self.doc_path, restore_page=self.page_index)

    def _on_scan_done(self, gen: int, res):
        if gen != self._scan_gen:
            return
        self.spinner.set_running(False)
        self._apply_files(res.paths, res.root.name or str(res.root), str(res.root))
        doc, page = getattr(self, "_pending_restore", (None, 0))
        self._pending_restore = (None, 0)
        if res.errors:
            self.toast.show_message(f"{len(res.errors)} folder(s) could not be read", "warning", "error")
        target = None
        if doc and any(str(p) == doc for p in res.paths):
            target = doc
        elif self.doc_path and any(str(p) == self.doc_path for p in res.paths):
            target = self.doc_path
            page = self.page_index
        if target:
            self.open_document(target, page)
        elif not res.paths:
            self._clear_document()
            self._show_message("folder", "No PDF drawings here", "This folder (and its subfolders) contains no PDF files.")
        else:
            self._clear_document()
            self._show_message("pages", "Select a drawing", f"{len(res.paths)} PDF{'s' if len(res.paths) != 1 else ''} found. Choose one in the library, or press → to open the first.")

    def _apply_files(self, files: list[Path], title: str, tooltip: str):
        mode = self.settings.get("file_view_mode")
        self.library.set_files(title, tooltip, self.source_root, files, mode)
        self.files = self.library.ordered_files()
        self._update_nav()

    def _set_library_mode(self, mode: str):
        self.settings.set("file_view_mode", mode)
        self.library.set_mode(mode)
        (self.act_tree if mode == "tree" else self.act_flat).setChecked(True)
        if self.files:
            files = self.source_files if self.source_files is not None else list(self.files)
            self.library.set_files(self.library.title.full_text(), self.library.title.toolTip(), self.source_root, files, mode)
            self.files = self.library.ordered_files()
            if self.doc_path:
                self.library.select_path(self.doc_path)
        self.settings.save()

    def _set_recursive(self, on: bool):
        if bool(self.settings.get("recursive_scan")) == on:
            return
        self.settings.set("recursive_scan", on)
        self.library.set_recursive(on)
        self.act_recursive.blockSignals(True)
        self.act_recursive.setChecked(on)
        self.act_recursive.blockSignals(False)
        self.settings.save()
        if self.source_root and self.source_files is None:
            self.load_folder(self.source_root, restore_doc=self.doc_path, restore_page=self.page_index)

    def _thumb_size_changed(self, h: int):
        self.settings.set("file_thumb_height", h)
        self._thumb_save_timer = getattr(self, "_thumb_save_timer", None) or QTimer(self)
        self._thumb_save_timer.setSingleShot(True)
        self._thumb_save_timer.timeout.connect(self.settings.save)
        self._thumb_save_timer.start(600)

    # ====================================================== documents/pages
    def _request_info(self, path: str, callback, priority=PRI_INTERACTIVE):
        info = self._doc_info.get(path)
        if info is not None:
            callback(info)
            return
        waiting = self._info_callbacks.setdefault(path, [])
        waiting.append(callback)
        if len(waiting) > 1:
            return

        def done(res: RenderResult, path=path):
            if not shiboken6.isValid(self):
                return
            if res.ok:
                info = {"count": res.header["page_count"], "sizes": res.header["sizes"],
                        "kind": res.header.get("kind", "pdf"), "pixels": res.header.get("pixels")}
            else:
                info = {"error": res.error or "This PDF could not be opened."}
            self._doc_info[path] = info
            for cb in self._info_callbacks.pop(path, []):
                cb(info)

        self.render.submit("info", {"path": path, "size_limit": 2000}, done, priority=priority)

    def open_document(self, path: str, page: int = 0):
        if not path:
            return
        self.doc_path = path
        self.library.select_path(path)
        self.title_label.setText(Path(path).name)
        self.setWindowFilePath(path) if not IS_MAC else None
        self.setWindowTitle(f"{Path(path).name} — {APP_NAME}")
        if IS_MAC:
            from ..platform.macos import set_represented_file

            set_represented_file(self, path)
        info = self._doc_info.get(path)
        if info is None:
            # Show the library thumbnail instantly while geometry is fetched.
            self.status_left.setText(path)
        self._request_info(path, lambda info, p=path, pg=page: self._on_doc_info(p, pg, info))

    def _on_doc_info(self, path: str, page: int, info: dict):
        if path != self.doc_path:
            return
        if "error" in info:
            self.page_count = 0
            self.page_sizes = []
            self.view.set_page(None)
            self.pages.set_document(None, 0, [])
            self.doc_info.set_doc(path)
            self._show_message("warning", "This PDF can’t be displayed", f"{Path(path).name}\n\n{info['error']}", "danger")
            self._update_nav()
            return
        self.page_count = info["count"]
        self.page_sizes = info["sizes"]
        self.pages.set_document(path, self.page_count, self.page_sizes)
        if page < 0:
            page = self.page_count - 1
        self.page_index = max(0, min(page, self.page_count - 1))
        self.viewer_stack.setCurrentIndex(1)
        self._show_page()
        if self._mark_after_open:
            self._mark_after_open = False
            QTimer.singleShot(0, self.open_mark)

    def _page_size(self, i: int) -> tuple[float, float]:
        if i < len(self.page_sizes):
            return tuple(self.page_sizes[i])
        return tuple(self.page_sizes[0]) if self.page_sizes else (612.0, 792.0)

    def _page_ref(self, path: str, i: int) -> PageRef:
        w, h = self._page_size(i) if path == self.doc_path else self._doc_info[path]["sizes"][0]
        return PageRef(path, i, w, h, PageRef.stamp_for(path))

    def _show_page(self):
        if self.doc_path is None or not self.page_count:
            return
        ref = self._page_ref(self.doc_path, self.page_index)
        placeholder = self.thumbs.get(self.doc_path, self.page_index, 160, visible=False)
        self.view.set_page(ref, placeholder)
        self.pages.set_current(self.page_index)
        self.doc_info.set_doc(self.doc_path, self.page_count, self.page_index, self._page_size(self.page_index),
                              self._doc_info.get(self.doc_path))
        w, h = self._page_size(self.page_index)
        self.status_left.setText(f"{self.doc_path}")
        self._update_nav()
        QTimer.singleShot(250, self._prefetch_neighbours)

    def _prefetch_neighbours(self):
        if self.doc_path is None:
            return
        if self.page_index + 1 < self.page_count:
            self.view.prefetch(self._page_ref(self.doc_path, self.page_index + 1))
        else:
            nxt = self._adjacent_doc(1)
            if nxt:
                self._request_info(nxt, lambda info, p=nxt: ("error" not in info and info["count"] > 0) and self.view.prefetch(
                    PageRef(p, 0, info["sizes"][0][0], info["sizes"][0][1], PageRef.stamp_for(p))), priority=PRI_PREFETCH)
        if self.page_index > 0:
            self.view.prefetch(self._page_ref(self.doc_path, self.page_index - 1))

    def _clear_document(self):
        self.doc_path = None
        self.page_count = 0
        self.page_sizes = []
        self.view.set_page(None)
        self.pages.set_document(None, 0, [])
        self.doc_info.set_doc(None)
        self.title_label.setText("")
        self.setWindowTitle(APP_NAME)
        self.library.model.set_current_path(None)
        self._update_nav()

    def _adjacent_doc(self, direction: int) -> str | None:
        if not self.files:
            return None
        if self.doc_path is None:
            return str(self.files[0]) if direction > 0 else None
        paths = [str(p) for p in self.files]
        try:
            i = paths.index(self.doc_path)
        except ValueError:
            return None
        j = i + direction
        return paths[j] if 0 <= j < len(paths) else None

    def next_page(self):
        if self.doc_path is None or not self.page_count:
            nxt = self._adjacent_doc(1)
            if nxt:
                self.open_document(nxt, 0)
            return
        if self.page_index < self.page_count - 1:
            self.go_to_page(self.page_index + 1)
        else:
            nxt = self._adjacent_doc(1)
            if nxt:
                self.open_document(nxt, 0)

    def prev_page(self):
        if self.doc_path is None:
            return
        if self.page_count and self.page_index > 0:
            self.go_to_page(self.page_index - 1)
        else:
            prv = self._adjacent_doc(-1)
            if prv:
                self.open_document(prv, -1)

    def first_page(self):
        self.go_to_page(0)

    def last_page(self):
        self.go_to_page(self.page_count - 1)

    def _step_document(self, direction: int):
        target = self._adjacent_doc(direction)
        if target:
            self.open_document(target, 0)

    def go_to_page(self, i: int):
        if self.doc_path is None or not (0 <= i < self.page_count):
            return
        if i != self.page_index:
            self.page_index = i
            self._show_page()

    def _page_field_entered(self):
        try:
            n = int(self.page_field.text().strip())
        except ValueError:
            n = self.page_index + 1
        self.go_to_page(max(1, min(n, self.page_count)) - 1)
        self.page_field.setText(str(self.page_index + 1) if self.page_count else "")
        self.view.setFocus()

    def _focus_page_field(self):
        self.page_field.setFocus()
        self.page_field.selectAll()

    def _focus_search(self):
        if not self.btn_library.isChecked():
            self.btn_library.setChecked(True)
        self.library.focus_search()

    def _update_nav(self):
        has_doc = self.doc_path is not None and self.page_count > 0 and self._mode == "viewer"
        self.page_field.setEnabled(has_doc)
        self.page_field.setText(str(self.page_index + 1) if has_doc else "")
        self.page_total.setText(f"/ {self.page_count}" if has_doc else "/ –")
        can_prev = has_doc and (self.page_index > 0 or self._adjacent_doc(-1) is not None)
        can_next = (has_doc and (self.page_index < self.page_count - 1 or self._adjacent_doc(1) is not None)) or \
            (not has_doc and bool(self.files) and self._mode == "viewer")
        for w in (self.btn_prev, self.act_prev):
            w.setEnabled(can_prev)
        for w in (self.btn_next, self.act_next):
            w.setEnabled(can_next)
        for w in (self.btn_zoom_in, self.btn_zoom_out, self.zoom_btn, self.btn_fit_width, self.btn_fit_page, self.btn_actual,
                  self.btn_copy, self.btn_queue_add, self.btn_mark, self.act_copy, self.act_copy_name, self.act_mark,
                  self.act_queue_add, self.act_save_png, self.act_zoom_in, self.act_zoom_out, self.act_actual,
                  self.act_fit_width, self.act_fit_page, self.act_first, self.act_last, self.act_reveal):
            w.setEnabled(has_doc)
        self.act_prev_doc.setEnabled(self._adjacent_doc(-1) is not None)
        self.act_next_doc.setEnabled(self._adjacent_doc(1) is not None)
        self.act_rescan.setEnabled(self.source_root is not None or self.source_files is not None)
        if not has_doc:
            self.zoom_btn.setText("–")
            self.status_right.setText("")

    # ================================================================ zoom
    def _on_zoom_changed(self, pct: float):
        self.zoom_btn.setText(f"{pct:.0f}%")
        self.status_right.setText(f"{pct:.0f}%")

    def _on_fit_mode_changed(self, mode):
        self.btn_fit_width.setChecked(mode == "width")
        self.btn_fit_page.setChecked(mode == "page")

    def _on_render_error(self, msg: str):
        if self.doc_path and msg:
            self.toast.show_message(msg, "warning", "error", 3500)

    # =========================================================== clipboard
    def _quality_dpi(self) -> int:
        return QUALITY_DPI.get(self.settings.get("quality"), 220)

    def copy_filename(self):
        if not self.doc_path:
            return
        name = Path(self.doc_path).name
        QApplication.clipboard().setText(name)
        self.toast.show_message(f"Copied “{name}”", "file-text")

    def copy_page_image(self):
        if not self.doc_path or not self.page_count:
            return
        w, h = self._page_size(self.page_index)
        job = FullPageRender(self.render, self.doc_path, self.page_index, w, h, self._quality_dpi(), True, parent=self,
                             native=self._is_image(self.doc_path))
        if self._copy_job is not None:
            self._copy_job.cancel()
        self._copy_job = job
        started = time.monotonic()
        slow = QTimer(self)
        slow.setSingleShot(True)
        slow.timeout.connect(lambda: self.toast.show_message("Rendering page for the clipboard…", "copy-image", msec=30000))
        slow.start(350)

        def finished(img: QImage):
            slow.stop()
            if self._copy_job is not job:
                return
            self._copy_job = None
            QApplication.clipboard().setImage(img)
            self.toast.show_message(f"Page {self.page_index + 1} copied at {job.effective_dpi:.0f} DPI — ready to paste", "check")

        def failed(msg: str):
            slow.stop()
            self._copy_job = None
            self.toast.show_message(f"Copy failed: {msg}", "warning", "error", 4000)

        job.finished.connect(finished)
        job.failed.connect(failed)
        job.start()

    def save_page_png(self):
        if not self.doc_path or not self.page_count:
            return
        default = str(Path(self.settings.get("export_dir") or Path.home()) / f"{Path(self.doc_path).stem}_page{self.page_index + 1}.png")
        path, _ = QFileDialog.getSaveFileName(self, "Save page as PNG", default, "PNG image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        self.settings.set("export_dir", str(Path(path).parent))
        w, h = self._page_size(self.page_index)
        job = FullPageRender(self.render, self.doc_path, self.page_index, w, h, self._quality_dpi(), True, parent=self,
                             native=self._is_image(self.doc_path))
        self.toast.show_message("Saving PNG…", "save", msec=30000)
        job.finished.connect(lambda img: QThreadPool.globalInstance().start(_SaveRunnable(img, path, self._save_bridge)))
        job.failed.connect(lambda msg: self.toast.show_message(f"Save failed: {msg}", "warning", "error", 4000))
        job.start()

    def _on_png_saved(self, path: str, ok: bool):
        if ok:
            self.toast.show_message(f"Saved {Path(path).name}", "check")
        else:
            self.toast.show_message(f"Could not write {Path(path).name}", "warning", "error", 4000)

    # ============================================================== queue
    def add_current_to_queue(self):
        if not self.doc_path or not self.page_count:
            return
        self.queue.add([QueueEntry(self.doc_path, self.page_index)])
        self.toast.show_message(f"Added page {self.page_index + 1} to the PowerPoint queue", "queue-add")
        self._reveal_inspector()

    def add_marked_page(self, path: str, page: int, image: QImage, comments: str, comment_count: int):
        preview = QPixmap.fromImage(image.scaledToHeight(160, Qt.TransformationMode.SmoothTransformation))
        self.queue.add([QueueEntry(path, page, marked_image=image, comments=comments, comment_count=comment_count, preview=preview)])
        self._reveal_inspector()

    def _reveal_inspector(self):
        if not self.btn_inspector.isChecked():
            self.btn_inspector.setChecked(True)

    def add_selected_to_queue(self):
        paths = self.library.selected_paths()
        if not paths:
            self.toast.show_message("Select one or more PDFs in the library first", "info")
            return
        self._add_paths_to_queue(paths)

    def add_all_to_queue(self):
        paths = [str(p) for p in self.files]
        if not paths:
            return
        if len(paths) > 25:
            box = QMessageBox(QMessageBox.Icon.Question, "Add all PDFs?", f"Add every page of all {len(paths)} PDFs to the PowerPoint queue?",
                              QMessageBox.StandardButton.Cancel, self)
            ok = box.addButton("Add All", QMessageBox.ButtonRole.AcceptRole)
            box.exec()
            if box.clickedButton() is not ok:
                return
        self._add_paths_to_queue(paths)

    def _add_paths_to_queue(self, paths: list[str]):
        results: dict[str, dict] = {}
        self.toast.show_message(f"Reading {len(paths)} file{'s' if len(paths) != 1 else ''}…", "queue-add", msec=30000)

        def collect(path, info):
            results[path] = info
            if len(results) == len(paths):
                entries, skipped = [], 0
                for p in paths:
                    i = results[p]
                    if "error" in i:
                        skipped += 1
                        continue
                    entries.extend(QueueEntry(p, k) for k in range(i["count"]))
                self.queue.add(entries)
                msg = f"Added {len(entries)} page{'s' if len(entries) != 1 else ''} to the PowerPoint queue"
                if skipped:
                    msg += f" ({skipped} unreadable file{'s' if skipped != 1 else ''} skipped)"
                self.toast.show_message(msg, "queue-add" if not skipped else "warning")
                self._reveal_inspector()

        for p in paths:
            self._request_info(p, lambda info, p=p: collect(p, info), priority=PRI_INTERACTIVE + 1)

    def export_queue(self):
        if not self.queue.model.entries:
            self.toast.show_message("The PowerPoint queue is empty", "info")
            return
        from .export_dialog import ExportDialog

        dlg = ExportDialog(self, self.render, self.settings, list(self.queue.model.entries))
        dlg.exec()

    # =============================================================== MARK
    def open_mark(self):
        if not self.doc_path or not self.page_count:
            return
        key = (self.doc_path, self.page_index)
        existing = self._mark_windows.get(key)
        if existing is not None:
            existing.showNormal()
            existing.raise_()
            existing.activateWindow()
            return
        from .mark.window import MarkWindow

        ref = self._page_ref(self.doc_path, self.page_index)
        win = MarkWindow(self, self.render, self.settings, ref, self._quality_dpi(), self.thumbs,
                         is_image=self._is_image(self.doc_path))
        self._mark_windows[key] = win
        win.destroyed.connect(lambda *_a, k=key: self._mark_windows.pop(k, None))
        win.show()

    # ============================================================ settings
    def set_theme(self, mode: str):
        self.settings.set("theme", mode)
        self.settings.save()
        ThemeManager.instance().set_mode(mode)
        if mode in self.theme_actions:
            self.theme_actions[mode].setChecked(True)

    def set_thin_lines(self, on: bool):
        self.settings.set("enhance_thin_lines", bool(on))
        self.settings.save()
        self.view.thin_lines = bool(on)
        self.thumbs.set_thin_lines(bool(on))
        self.view.refresh()
        self.library.view.viewport().update()
        self.pages.view.viewport().update()
        self.act_thin.blockSignals(True)
        self.act_thin.setChecked(bool(on))
        self.act_thin.blockSignals(False)

    def _on_theme_changed(self, _theme):
        self.update()

    def _toggle_library(self, on: bool):
        self.library.setVisible(on)
        self.act_show_library.blockSignals(True)
        self.act_show_library.setChecked(on)
        self.act_show_library.blockSignals(False)
        self.btn_library.setToolTip(tip("Hide Library" if on else "Show Library", "Ctrl+Alt+1"))

    def _toggle_inspector(self, on: bool):
        self.inspector.setVisible(on)
        self.act_show_inspector.blockSignals(True)
        self.act_show_inspector.setChecked(on)
        self.act_show_inspector.blockSignals(False)
        self.btn_inspector.setToolTip(tip("Hide Inspector" if on else "Show Inspector", "Ctrl+Alt+2"))

    def _toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _fill_recent_menu(self):
        self.recent_menu.clear()
        recent = [f for f in self.settings.get("recent_folders") if Path(f).is_dir()]
        for f in recent:
            self.recent_menu.addAction(f, lambda f=f: self.load_folder(Path(f)))
        if not recent:
            a = self.recent_menu.addAction("No Recent Folders")
            a.setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear Menu", lambda: (self.settings.set("recent_folders", []), self.settings.save()))

    def _on_activity(self, n: int):
        self.spinner.set_running(n > 0)

    def show_preferences(self):
        from .preferences import PreferencesDialog

        PreferencesDialog(self).exec()

    def show_shortcuts(self):
        from .preferences import ShortcutsDialog

        ShortcutsDialog(self).exec()

    def show_about(self):
        from .about import AboutDialog

        AboutDialog(self.app_icon, self).exec()

    # ========================================================= drag & drop
    def _dropped_paths(self, mime) -> list[Path]:
        out = []
        for u in mime.urls() if mime.hasUrls() else []:
            if u.isLocalFile():
                p = Path(u.toLocalFile())
                if p.is_dir() or is_document_name(p.name) or p.suffix.lower() in OFFICE_EXTS:
                    out.append(p)
        return out

    def dragEnterEvent(self, event):
        if self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = self._dropped_paths(event.mimeData())
        if not paths:
            return
        event.acceptProposedAction()
        self.route_dropped(paths)

    def route_dropped(self, paths: list[Path]):
        """Viewer: open PDFs, images and folders. Tools: feed the open tool. Office files: convert."""
        docs = [str(p) for p in paths if p.is_file() and is_document_name(p.name)]
        office = [str(p) for p in paths if p.is_file() and p.suffix.lower() in OFFICE_EXTS]
        if self._mode == "tools" and self.tools is not None:
            if (docs or office) and self.tools.drop_files(docs + office):
                return
            if docs:
                self.open_tool("organize", docs)
                return
        if office and not docs and not any(p.is_dir() for p in paths):
            self.open_tool("office_to_pdf", office)
            return
        self.open_paths(paths)

    # ======================================================== PDF Tools host
    def _is_image(self, path: str | None) -> bool:
        return bool(path) and kind_for(path) == "image"

    def set_mode(self, mode: str):
        mode = "tools" if mode == "tools" else "viewer"
        if mode == "tools" and self.tools is None:
            from .tools.workspace import ToolsWorkspace

            self.tools = ToolsWorkspace(self)
            self.content_stack.addWidget(self.tools)
        self._mode = mode
        self.content_stack.setCurrentWidget(self.tools if mode == "tools" else self.viewer_split)
        self.viewer_controls.setVisible(mode == "viewer")
        self.mode_ctrl.set_current(mode, animate=True)
        (self.act_mode_tools if mode == "tools" else self.act_mode_viewer).setChecked(True)
        self.title_label.setText("" if mode == "tools" else (Path(self.doc_path).name if self.doc_path else ""))
        self._update_nav()

    def open_tool(self, tool_id: str, inputs: list[str] | None = None):
        self.set_mode("tools")
        self.tools.open_tool(tool_id, inputs or None)

    def _tool_inputs(self) -> list[str]:
        sel = self.library.selected_paths()
        return sel or ([self.doc_path] if self.doc_path else [])

    def library_selection(self) -> list[str]:
        return self.library.selected_paths()

    def current_document(self) -> str | None:
        return self.doc_path

    def default_folder(self) -> str:
        if self.doc_path:
            return str(Path(self.doc_path).parent)
        return str(self.source_root or Path.home())

    def document_info(self, path: str, callback):
        self._request_info(path, callback, priority=PRI_INTERACTIVE + 1)

    def open_in_viewer(self, path: str | None):
        if not path:
            return
        self.set_mode("viewer")
        self.open_paths([Path(path)])

    def outputs_created(self, paths: list[str]):
        root = self.source_root
        if root is not None and self.source_files is None and any(str(Path(o)).startswith(str(root)) for o in paths):
            self.rescan()  # new files appear in the library
        n = len(paths)
        self.toast.show_message(f"Saved {n} file{'s' if n != 1 else ''}", "check")

    def open_mark_current(self):
        self.set_mode("viewer")
        if self.doc_path:
            self.open_mark()
        else:
            self.choose_and_mark()

    def _mark_path(self, path: str):
        self.set_mode("viewer")
        if path == self.doc_path and self.page_count:
            self.open_mark()
        else:
            self._mark_after_open = True
            self.open_paths([Path(path)])

    def choose_and_mark(self):
        pats = " ".join(f"*{e}" for e in sorted({".pdf"} | IMAGE_EXTS))
        path, _ = QFileDialog.getOpenFileName(self, "Choose a drawing or image to mark up", self.default_folder(), f"PDFs and images ({pats})")
        if path:
            self._mark_path(path)

    def _set_include_images(self, on: bool):
        if bool(self.settings.get("include_images", True)) == on:
            return
        self.settings.set("include_images", bool(on))
        self.settings.save()
        self.act_images.blockSignals(True)
        self.act_images.setChecked(bool(on))
        self.act_images.blockSignals(False)
        self.library.set_include_images(bool(on))
        if self.source_root and self.source_files is None:
            self.load_folder(self.source_root, restore_doc=self.doc_path, restore_page=self.page_index)

    # ================================================== export queue as PDF
    def export_queue_pdf(self):
        entries = list(self.queue.model.entries)
        if not entries:
            self.toast.show_message("The PowerPoint queue is empty", "info")
            return
        folder = self.settings.get("export_dir") or str(Path.home() / "Documents")
        from datetime import date

        from ..core.tool_support import unique_path

        default = unique_path(Path(folder) / f"Drawings {date.today().isoformat()}.pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Export queue as PDF", str(default), "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self.settings.set("export_dir", str(Path(path).parent))
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="eng-queue-pdf-"))
        items: list[dict | None] = [None] * len(entries)
        pending = {"n": 0}

        def submit():
            self.render.submit("tool", {"op": "organize", "items": items, "output": path}, done, priority=PRI_INTERACTIVE + 2,
                               dedupe=False)

        def done(res: RenderResult):
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
            if res.ok:
                self.toast.show_message(f"Exported {len(entries)} pages to {Path(path).name}", "check")
            else:
                self.toast.show_message(f"Export failed: {res.error}", "warning", "error", 4000)

        def saved(png: str, ok: bool):
            pending["n"] -= 1
            if pending["n"] == 0:
                try:
                    self._save_bridge.saved.disconnect(saved)
                except (RuntimeError, TypeError):
                    pass
                submit()

        for i, e in enumerate(entries):
            if e.marked:
                png = str(tmp / f"marked {i + 1}.png")
                items[i] = {"path": png}
                pending["n"] += 1
            else:
                items[i] = {"path": e.doc_path, "page": e.page_index}
        self.toast.show_message(f"Exporting {len(entries)} pages…", "save", msec=30000)
        if pending["n"]:
            self._save_bridge.saved.connect(saved)
            for i, e in enumerate(entries):
                if e.marked:
                    QThreadPool.globalInstance().start(_SaveRunnable(e.marked_image, items[i]["path"], self._save_bridge))
        else:
            submit()

    # ======================================================= iPhone scanning
    def scans_folder(self) -> Path:
        return Path(self.settings.get("scans_folder") or (Path.home() / "Documents" / "ENG Drawing Viewer Scans"))

    def open_scans_folder(self):
        folder = self.scans_folder()
        folder.mkdir(parents=True, exist_ok=True)
        self.set_mode("viewer")
        self.load_folder(folder)

    def start_scan(self, anchor_widget=None):
        if not IS_MAC:
            return
        if self._continuity is None:
            from ..platform.continuity import ContinuityCamera

            self._continuity = ContinuityCamera(self._receive_scan, self.open_files_dialog, self._scan_help,
                                                lambda: self.toast.show_message("Continue on your iPhone — tap Save when the scan is done",
                                                                                "iphone", msec=12000))
        anchor = anchor_widget if anchor_widget is not None and anchor_widget.isVisible() else self.btn_scan
        focused = QApplication.focusWidget()
        if focused is not None:
            focused.clearFocus()  # a focused text field would add AutoFill to Apple's menu
        self._continuity.present(anchor)

    def _scan_help(self):
        QMessageBox.information(self, "iPhone not listed?",
                                "Scanning uses Apple’s Continuity Camera:\n\n"
                                "• The iPhone needs iOS 12 or later and must be near this Mac.\n"
                                "• Both must be signed in to the same Apple Account, with Wi‑Fi and Bluetooth on.\n"
                                "• Unlock the iPhone, then try Scan with iPhone again.\n\n"
                                "You can always add files from disk instead.")

    def _receive_scan(self, payloads: list, offered: list):
        """Called inside AppKit's hand-over: save the bytes now, do the UI work right after."""
        from ..platform.continuity import save_payloads

        if not payloads:
            detail = f"\n\nDetails: {'; '.join(offered)}" if offered else ""
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Nothing arrived from the iPhone",
                                                             "The scan could not be read. Please try again." + detail))
            return
        paths = save_payloads(payloads, self.scans_folder())
        QTimer.singleShot(0, lambda: self._show_scan(paths))

    def _show_scan(self, paths: list[Path]):
        if QGuiApplication.platformName() == "cocoa":
            try:  # the iPhone had the focus; bring this window back
                from AppKit import NSApplication

                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:
                pass
        self.raise_()
        self.activateWindow()
        self.set_mode("viewer")
        self.load_folder(self.scans_folder(), restore_doc=str(paths[0]), restore_page=0, recursive=False)
        kind = "scan" if paths[0].suffix == ".pdf" else "photo"
        self.toast.show_message(f"Saved your {kind} as {paths[0].name} — MARK it, export it, or use PDF Tools", "check", msec=5000)

