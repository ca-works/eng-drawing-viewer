"""Integrated, layered image editing workspace for ENG Drawing Viewer."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops
from PySide6.QtCore import Qt, QSize, QThreadPool, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QRadialGradient, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog,
    QFormLayout, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QTabBar, QToolButton,
    QVBoxLayout, QWidget)

from ...core.image_document import ImageDocument, ImageLayer, BLEND_MODES
from ...core import image_tools as T
from .. import icons
from ..widgets import tool_button, secondary_button, primary_button, label, separator
from .annotations import render_annotation, render_transformed_annotation
from .canvas import ImageCanvas
from .common import Job, to_qimage, from_qimage
from .dialogs import ExportDialog, ResizeDialog

IMAGE_SUFFIXES = {".engimg", ".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".avif", ".heic", ".heif"}
IMAGE_FILTER = "Images and projects (*.engimg *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif *.avif *.heic *.heif)"
TOOLS = [("move", "move", "Move layer (V)"), ("rect", "select-rect", "Rectangle selection (M)"),
         ("ellipse", "select-ellipse", "Ellipse selection"), ("lasso", "lasso", "Freehand Lasso (L)"),
         ("polygon", "polygon", "Polygonal Lasso · double-click to finish"),
         ("wand", "wand", "Magic Wand (W)"), ("quick", "quick-select", "Quick Selection (Q)"),
         ("crop", "crop", "Crop (C) · Enter to apply"), ("pen", "pen", "Brush / Pen (B)"),
         ("highlighter", "highlighter", "Highlighter"), ("eraser", "eraser", "Mask Eraser (E)"),
         ("magic", "magic-eraser", "Magic Eraser (X) · one click erases a whole area"),
         ("background", "background-erase", "Background Eraser brush"), ("restore", "restore", "Restore Mask"),
         ("clone", "clone", "Clone Stamp (S) · Option/Alt-click to sample"),
         ("heal", "heal", "Spot Healing (J)"), ("text", "text", "Text (T)"),
         ("shape", "rectangle", "Rectangle (U)"), ("circle", "circle", "Outline circle"),
         ("arrow", "arrow", "Arrow"), ("bubble", "bubble", "Numbered comment")]


@dataclass
class TabState:
    doc: ImageDocument
    name: str
    project_path: str = ""
    selection: object = None


class BrushPreview(QWidget):
    def __init__(self):
        super().__init__()
        self.diameter, self.hardness, self.opacity = 40, .8, 1
        self.color = "#2563eb"
        self.setFixedHeight(76)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = min(self.diameter / 2, 26)
        gradient = QRadialGradient(self.width() / 2, 32, max(.1, radius))
        center = QColor(self.color)
        center.setAlphaF(self.opacity)
        gradient.setColorAt(0, center)
        gradient.setColorAt(min(.99, self.hardness), center)
        edge = QColor(center)
        edge.setAlpha(0)
        gradient.setColorAt(1, edge)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(gradient)
        p.drawEllipse(int(self.width() / 2 - radius), int(32 - radius), int(radius * 2), int(radius * 2))
        p.setPen(self.palette().text().color())
        text = f"{self.diameter} px diameter" + (" · preview scaled" if self.diameter > 52 else "")
        p.drawText(self.rect().adjusted(0, 55, 0, 0), Qt.AlignmentFlag.AlignHCenter, text)


class ImageWorkspace(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.states = []
        self._job = None
        self._opening = []
        self._refreshing = False
        self._composite = None
        self._clone_source = None
        self._model = None
        self._model_path = ""
        self._provider = "cpu"
        self.color = "#2563eb"
        self._dialogs = []
        self._build()
        self._shortcuts()
        self.setAcceptDrops(True)
        self.canvas.setAcceptDrops(False)  # dropped files go to the workspace, not the graphics scene
        self.canvas.viewport().setAcceptDrops(False)
        self._update_enabled()

    @property
    def state(self):
        i = self.tabs.currentIndex()
        return self.states[i] if 0 <= i < len(self.states) else None

    @property
    def doc(self):
        return self.state.doc if self.state else None

    @property
    def selection(self):
        return self.state.selection if self.state else None

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.tabs = QTabBar()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setExpanding(False)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        root.addWidget(self.tabs)
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(5)
        self.open_button = secondary_button("Open", "folder-open")
        self.open_button.clicked.connect(self.open_dialog)
        row.addWidget(self.open_button)
        new = tool_button("plus", "New transparent image")
        new.clicked.connect(self.new_document)
        row.addWidget(new)
        self.recent_button = self._menu_button("Recent", [("Recent images", None)])
        self.recent_button.menu().aboutToShow.connect(self._recent)
        row.addWidget(self.recent_button)
        self.undo_button = tool_button("undo", "Undo")
        self.undo_button.clicked.connect(lambda: self.history(False))
        row.addWidget(self.undo_button)
        self.redo_button = tool_button("redo", "Redo")
        self.redo_button.clicked.connect(lambda: self.history(True))
        row.addWidget(self.redo_button)
        self.image_menu = self._menu_button("Image", [
            ("Resize…", self.resize_image), ("Canvas size…", lambda: self.resize_image(True)),
            ("Rotate clockwise", lambda: self.transform("rotate", -90)),
            ("Rotate counterclockwise", lambda: self.transform("rotate", 90)),
            ("Straighten / arbitrary rotation…", self.straighten),
            ("Flip horizontal", lambda: self.transform("flip", True)),
            ("Flip vertical", lambda: self.transform("flip", False)),
            ("Flatten layers", lambda: self.transform("flatten")),
            ("Batch convert / compress…", self.batch),
            ("Trace into Vector Editor…", self.vectorize)])
        row.addWidget(self.image_menu)
        self.select_menu = self._menu_button("Select", [
            ("All", self.select_all), ("Deselect", lambda: self.set_selection(None)),
            ("Invert", self.invert_selection), ("Feather / grow / smooth…", self.refine_selection),
            ("Selection to layer mask", self.selection_to_mask), ("Selection to new layer", self.extract_selection),
            ("Delete selected pixels", self.delete_selection), ("Heal selected area", self.heal_selection),
            ("Restore selection from mask", self.mask_to_selection)])
        row.addWidget(self.select_menu)
        row.addStretch(1)
        self.save_button = tool_button("save", "Save editable project (.engimg)")
        self.save_button.clicked.connect(self.save_project)
        row.addWidget(self.save_button)
        self.copy_button = QToolButton()
        self.copy_button.setText("Copy to Clipboard")
        self.copy_button.setIcon(icons.icon("copy-image"))
        self.copy_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.copy_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.copy_button.setProperty("variant", "primary")
        copy_menu = QMenu(self.copy_button)
        copy_menu.addAction("Clipboard format and options…", lambda: self.copy_image(True))
        copy_menu.addAction("Paste image as new document", self.paste_image)
        self.copy_button.setMenu(copy_menu)
        self.copy_button.clicked.connect(lambda: self.copy_image(False))
        row.addWidget(self.copy_button)
        self.export_button = primary_button("Export…", "export")
        self.export_button.clicked.connect(self.export_image)
        row.addWidget(self.export_button)
        root.addWidget(bar)
        split = QSplitter(Qt.Orientation.Horizontal)
        tool_scroll = QScrollArea()
        tool_scroll.setFixedWidth(52)
        tool_scroll.setWidgetResizable(True)
        tool_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tools = QWidget()
        tl = QVBoxLayout(tools)
        tl.setContentsMargins(7, 6, 7, 6)
        tl.setSpacing(3)
        self.tool_buttons = {}
        for key, icon, description in TOOLS:
            button = tool_button(icon, description, checkable=True, size=32, icon_size=18)
            button.clicked.connect(lambda _=False, t=key: self.set_tool(t))
            self.tool_buttons[key] = button
            tl.addWidget(button)
        tl.addStretch()
        tool_scroll.setWidget(tools)
        split.addWidget(tool_scroll)
        self.canvas = ImageCanvas()
        self.canvas.gesture.connect(self.on_gesture)
        self.canvas.toolRequested.connect(self.set_tool)
        self.canvas.actionRequested.connect(self._canvas_action)
        self.canvas.zoomChanged.connect(lambda p: self.zoom.setText(f"{p:.0f}%"))
        self.canvas.cropChanged.connect(self._crop_changed)
        self.canvas.guidesChanged.connect(self._guides_changed)
        split.addWidget(self.canvas)
        self.side = QWidget()
        self.side.setObjectName("Inspector")
        self.side.setMinimumWidth(245)
        self.side.setMaximumWidth(330)
        sl = QVBoxLayout(self.side)
        sl.setContentsMargins(12, 12, 12, 10)
        sl.setSpacing(7)
        self.tool_title = label("Move layer", "title")
        sl.addWidget(self.tool_title)
        self.hint = QLabel("Drag the active layer. Hold Space or use the middle mouse button to pan.")
        self.hint.setWordWrap(True)
        self.hint.setProperty("role", "small")
        sl.addWidget(self.hint)
        self.brush_panel = QWidget()
        bf = QFormLayout(self.brush_panel)
        bf.setContentsMargins(0, 0, 0, 0)
        self.size = self._slider_row(bf, "Size", 1, 1000, 40)
        self.hardness = self._slider_row(bf, "Hardness", 0, 100, 80)
        self.opacity = self._slider_row(bf, "Opacity", 1, 100, 100)
        self.tolerance = self._slider_row(bf, "Tolerance", 0, 255, 32)
        self.preview = BrushPreview()
        bf.addRow(self.preview)
        sl.addWidget(self.brush_panel)
        self.color_button = QPushButton("Colour")
        self.color_button.clicked.connect(self.choose_color)
        sl.addWidget(self.color_button)
        self.selection_mode = QComboBox()
        for text, data in [("Replace selection", "replace"), ("Add · Shift", "add"),
                           ("Subtract · Option/Alt", "subtract"), ("Intersect · Shift + Option/Alt", "intersect")]:
            self.selection_mode.addItem(text, data)
        sl.addWidget(self.selection_mode)
        self.crop_panel = QWidget()
        cl = QVBoxLayout(self.crop_panel)
        cl.setContentsMargins(0, 0, 0, 0)
        self.ratio = QComboBox()
        for title, ratio in [("Freeform", 0), ("1:1", 1), ("4:3", 4 / 3), ("3:2", 1.5), ("16:9", 16 / 9), ("A series", 1 / 1.41421356)]:
            self.ratio.addItem(title, ratio)
        self.ratio.currentIndexChanged.connect(lambda _: setattr(self.canvas, "crop_ratio", self.ratio.currentData()))
        cl.addWidget(self.ratio)
        self.crop_info = QLabel("Drag a crop rectangle; adjust its corners.")
        self.crop_info.setWordWrap(True)
        cl.addWidget(self.crop_info)
        self.crop_apply = secondary_button("Apply crop", "crop")
        self.crop_apply.clicked.connect(self.apply_crop)
        cl.addWidget(self.crop_apply)
        sl.addWidget(self.crop_panel)
        self.magic_panel = QWidget()
        ml = QVBoxLayout(self.magic_panel)
        ml.setContentsMargins(0, 0, 0, 0)
        self.contiguous = QCheckBox("Contiguous")
        self.contiguous.setChecked(True)
        self.contiguous.setToolTip("Only erase the connected area you click. Off erases that colour everywhere.")
        ml.addWidget(self.contiguous)
        self.antialias = QCheckBox("Anti-alias edges")
        self.antialias.setChecked(True)
        ml.addWidget(self.antialias)
        self.auto_background = secondary_button("Remove background automatically", "magic-eraser")
        self.auto_background.setToolTip("Finds the subject and hides everything around it. Works offline, no model needed.")
        self.auto_background.clicked.connect(self.remove_background)
        ml.addWidget(self.auto_background)
        sl.addWidget(self.magic_panel)
        ai = self._menu_button("Offline subject selection", [("Choose offline model package…", self.choose_model),
            ("Select subject", lambda: self.run_model(False)), ("Remove background to mask", lambda: self.run_model(True)),
            ("Remove background automatically (no model)", self.remove_background),
            ("Inference backend…", self.choose_provider)])
        sl.addWidget(ai)
        self.model_label = QLabel("No model loaded · use a local package")
        self.model_label.setWordWrap(True)
        self.model_label.setProperty("role", "small")
        sl.addWidget(self.model_label)
        sl.addWidget(separator(False))
        layer_heading = QHBoxLayout()
        layer_heading.addWidget(label("Layers", "title"), 1)
        add = tool_button("plus", "Add transparent layer", size=26)
        add.clicked.connect(self.add_layer)
        layer_heading.addWidget(add)
        layer_menu = self._menu_button("•••", [("Import image as layer…", self.import_layer),
            ("Duplicate layer", self.duplicate_layer), ("Rename layer…", self.rename_layer),
            ("Move layer up", lambda: self.reorder_layer(1)), ("Move layer down", lambda: self.reorder_layer(-1)),
            ("Scale layer…", self.scale_layer), ("Add white mask", self.add_mask),
            ("Remove mask", self.remove_mask), ("Delete layer", self.remove_layer)])
        layer_heading.addWidget(layer_menu)
        sl.addLayout(layer_heading)
        self.layers = QListWidget()
        self.layers.setIconSize(QSize(48, 38))
        self.layers.currentRowChanged.connect(self._layer_selected)
        self.layers.itemChanged.connect(self._layer_changed)
        self.layers.itemDoubleClicked.connect(lambda _: self.edit_layer())
        self.layers.setMinimumHeight(125)
        sl.addWidget(self.layers, 1)
        lf = QFormLayout()
        self.layer_opacity = QSpinBox()
        self.layer_opacity.setRange(0, 100)
        self.layer_opacity.setSuffix(" %")
        self.layer_opacity.editingFinished.connect(self.change_layer_opacity)
        lf.addRow("Opacity", self.layer_opacity)
        self.blend = QComboBox()
        self.blend.addItems(list(BLEND_MODES))
        self.blend.currentTextChanged.connect(self.change_blend)
        lf.addRow("Blend", self.blend)
        self.locked = QCheckBox("Lock layer")
        self.locked.toggled.connect(self.change_locked)
        lf.addRow("", self.locked)
        sl.addLayout(lf)
        queue = secondary_button("Send to PowerPoint Queue", "queue-add")
        queue.clicked.connect(self.send_to_queue)
        sl.addWidget(queue)
        split.addWidget(self.side)
        split.setStretchFactor(1, 1)
        split.setSizes([52, 850, 265])
        root.addWidget(split, 1)
        status = QHBoxLayout()
        status.setContentsMargins(10, 5, 10, 5)
        self.status = label("Open an image, paste from the clipboard, or create a new canvas.", "small")
        status.addWidget(self.status, 1)
        self.zoom = QPushButton("100%")
        self.zoom.setFixedWidth(58)
        self.zoom.clicked.connect(lambda: self.canvas.set_zoom(100))
        status.addWidget(self.zoom)
        fit = QPushButton("Fit")
        fit.setFixedWidth(40)
        fit.clicked.connect(self.canvas.fit_image)
        status.addWidget(fit)
        view = self._menu_button("View", [("Fit width", self.canvas.fit_width),
            ("Toggle grid / pixel grid", lambda: self._toggle_view("grid")),
            ("Toggle rulers", lambda: self._toggle_view("rulers")),
            ("Toggle guide snapping", lambda: self._toggle_view("snap")),
            ("Clear guides", self.clear_guides)])
        status.addWidget(view)
        self.grid_box = QCheckBox("Grid")
        self.grid_box.setToolTip("Show the grid (Ctrl+'). Pixel lines appear from 1200 % zoom.")
        self.grid_box.toggled.connect(lambda on: self._set_view("grid", on))
        status.addWidget(self.grid_box)
        self.rulers_box = QCheckBox("Rulers")
        self.rulers_box.setChecked(self.canvas.rulers)
        self.rulers_box.setToolTip("Show rulers. Drag from a ruler to place a guide.")
        self.rulers_box.toggled.connect(lambda on: self._set_view("rulers", on))
        status.addWidget(self.rulers_box)
        self.units = QComboBox()
        self.units.addItems(["px", "mm", "cm", "in"])
        self.units.currentTextChanged.connect(lambda s: (setattr(self.canvas, "unit", s), self.canvas.viewport().update()))
        status.addWidget(self.units)
        root.addLayout(status)
        self.set_tool("move")

    def _menu_button(self, title, entries):
        button = QToolButton()
        button.setText(title)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        for text, callback in entries:
            if callback:
                menu.addAction(text, callback)
        button.setMenu(menu)
        return button

    def _slider_row(self, form, name, lo, hi, value):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(value)
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setFixedWidth(65)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        spin.valueChanged.connect(self._brush_changed)
        row.addWidget(slider)
        row.addWidget(spin)
        form.addRow(name, box)
        return spin

    def _shortcuts(self):
        for key, fn in [(QKeySequence.StandardKey.Save, self.save_project),
                        (QKeySequence.StandardKey.Undo, lambda: self.history(False)),
                        (QKeySequence.StandardKey.Redo, lambda: self.history(True)),
                        (QKeySequence.StandardKey.Copy, lambda: self.copy_image(False)),
                        (QKeySequence.StandardKey.Paste, self.paste_image),
                        ("Ctrl+D", lambda: self.set_selection(None)), ("Ctrl+A", self.select_all),
                        ("Ctrl+'", lambda: self._toggle_view("grid"))]:
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(fn)
            self.addAction(action)

    def _update_enabled(self):
        ready = self.doc is not None and self._job is None
        for widget in (self.side, self.image_menu, self.select_menu, self.save_button, self.export_button, self.copy_button):
            widget.setEnabled(ready)
        self.undo_button.setEnabled(ready and self.doc.can_undo)
        self.redo_button.setEnabled(ready and self.doc.can_redo)
        self.canvas.busy = not ready
        self.tabs.setEnabled(self._job is None)
        for button in self.tool_buttons.values():
            button.setEnabled(ready)

    def run(self, description, fn, done=None):
        if self._job is not None:
            self.status.setText("Finish the current operation first.")
            return False
        self.status.setText(description + "…")
        job = Job(fn)
        job.signals.progress.connect(lambda percent, message: self.status.setText(f"{message} · {percent}%"))
        self._job = job
        self._update_enabled()
        def finished(result, error):
            self._job = None
            if error:
                self.status.setText(error)
                QMessageBox.warning(self, "Image operation", error)
            elif done:
                done(result)
            self._update_enabled()
            if self._opening:
                QTimer.singleShot(0, lambda: self.open_file(self._opening.pop(0)))
        job.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(job)
        return True

    def mutate(self, description, fn, snapshot=True, clear_selection=False):
        doc = self.doc
        if doc is None:
            return
        def work():
            before = doc._state()
            undo, redo, next_revision = list(doc._undo), list(doc._redo), doc._next_revision
            try:
                if snapshot:
                    doc.snapshot(description)
                fn(doc)
                if snapshot:
                    doc.touch()
                return doc.composite()
            except Exception:
                doc._restore(before)
                doc._undo, doc._redo, doc._next_revision = undo, redo, next_revision
                raise
        def done(image):
            if clear_selection:
                self.state.selection = None
                self.canvas.crop_rect = None
            self.refresh(image)
        self.run(description, work, done)

    def refresh(self, image=None, fit=False):
        if not self.doc:
            self._update_enabled()
            return
        self._refreshing = True
        try:
            self._composite = image if image is not None else self.doc.composite()
            self.canvas.set_image(self._composite, fit)
            self.canvas.dpi = self.doc.dpi
            self.canvas.guides = list(self.doc.metadata.get("guides", []))
            self.canvas.set_selection(self.selection)
            self.layers.clear()
            for i in reversed(range(len(self.doc.layers))):
                layer = self.doc.layers[i]
                thumb = layer.rendered().copy()
                thumb.thumbnail((48, 38))
                item = QListWidgetItem(QIcon(QPixmap.fromImage(to_qimage(thumb))), layer.name + ("  [mask]" if layer.mask else ""))
                item.setData(Qt.ItemDataRole.UserRole, i)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked)
                self.layers.addItem(item)
            self.layers.setCurrentRow(len(self.doc.layers) - 1 - self.doc.active_index)
            layer = self.doc.active_layer
            self.layer_opacity.setValue(round(layer.opacity * 100))
            self.blend.setCurrentText(layer.blend)
            self.locked.setChecked(layer.locked)
            self.tabs.setTabText(self.tabs.currentIndex(), self.state.name + (" •" if self.doc.dirty else ""))
            self.status.setText(f"{self.doc.width:,} × {self.doc.height:,} px · {self.doc.dpi:g} DPI · {len(self.doc.layers)} layers · "
                                f"Undo memory {self.doc.history_bytes / 1048576:.0f} MB")
        finally:
            self._refreshing = False
        self._update_enabled()

    def open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Open images or editable projects", "", IMAGE_FILTER)
        if paths:
            self._opening.extend(paths[1:])
            self.open_file(paths[0])

    def open_file(self, path):
        if self._job:
            self._opening.append(str(path))
            return
        for index, state in enumerate(self.states):
            known = state.project_path or state.doc.source_path
            if known and Path(known).resolve() == Path(path).resolve():
                self.tabs.setCurrentIndex(index)
                self.refresh()
                return
        is_project = Path(path).suffix.lower() == ".engimg"
        def load():
            doc = ImageDocument.load_project(path) if is_project else ImageDocument.from_file(path)
            return doc, doc.composite()
        def done(result):
            doc, composite = result
            self.add_document(doc, Path(path).name, str(path) if is_project else "", composite)
            recent = self.owner.settings.get("image_recent", [])
            recent = [str(path)] + [p for p in recent if p != str(path)]
            self.owner.settings.set("image_recent", recent[:12])
            self.owner.settings.save()
            if doc.metadata.get("frame_count", 1) > 1:
                self.status.setText("Opened the first frame. Animation / multipage image editing is not supported.")
        self.run("Opening image", load, done)

    def add_document(self, doc, name="Untitled", project_path="", image=None):
        self.tabs.blockSignals(True)
        self.states.append(TabState(doc, name, project_path))
        index = self.tabs.addTab(name)
        self.tabs.setCurrentIndex(index)
        self.tabs.blockSignals(False)
        self._clone_source = None
        self.canvas.crop_rect = None
        self.refresh(image, fit=True)

    def open_image(self, image, name="Untitled"):
        doc = ImageDocument.from_image(image, name)
        doc.touch()
        self.add_document(doc, name)

    def new_document(self):
        width, ok = QInputDialog.getInt(self, "New image", "Width in pixels", 1600, 1, 20000)
        if not ok:
            return
        height, ok = QInputDialog.getInt(self, "New image", "Height in pixels", 1000, 1, 20000)
        if ok:
            try:
                doc = ImageDocument(width, height)
                doc.touch()
                self.add_document(doc)
            except ValueError as exc:
                QMessageBox.warning(self, "New image", str(exc))

    def _recent(self):
        menu = self.recent_button.menu()
        menu.clear()
        for path in self.owner.settings.get("image_recent", []):
            menu.addAction(Path(path).name, lambda p=path: self.open_file(p))
        if menu.isEmpty():
            menu.addAction("No recent images").setEnabled(False)

    def _tab_changed(self, _):
        if self._refreshing or self._job:
            return
        self._clone_source = None
        self.canvas.crop_rect = None
        self.refresh(fit=True)

    def close_tab(self, index):
        if self._job:
            return False
        state = self.states[index]
        if state.doc.dirty:
            answer = QMessageBox.question(self, "Save editable image?", f"Save changes to {state.name}?",
                                           QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Save:
                self.tabs.setCurrentIndex(index)
                if not self.save_project(synchronous=True):
                    return False
        self.tabs.blockSignals(True)
        self.states.pop(index)
        self.tabs.removeTab(index)
        self.tabs.blockSignals(False)
        if self.doc:
            self.refresh(fit=True)
        else:
            self.canvas.item.setPixmap(QPixmap())
            self.canvas._image_size = (0, 0)
            self.layers.clear()
            self.status.setText("Open an image to begin.")
            self._update_enabled()
        return True

    def maybe_close(self):
        if self._job:
            QMessageBox.information(self, "Image operation running", "Wait for the current image operation to finish before closing.")
            return False
        # Validate first; retain tabs if another workspace later cancels closing.
        for index, state in enumerate(self.states):
            if not state.doc.dirty:
                continue
            answer = QMessageBox.question(self, "Save editable image?", f"Save changes to {state.name}?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Save:
                self.tabs.setCurrentIndex(index)
                if not self.save_project(synchronous=True):
                    return False
        return True

    def save_project(self, checked=False, synchronous=False):
        if not self.doc or self._job:
            return False
        path = self.state.project_path
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save editable image project", Path(self.state.name).stem + ".engimg", "ENG image project (*.engimg)")
        if not path:
            return False
        if not path.lower().endswith(".engimg"):
            path += ".engimg"
        doc, state = self.doc, self.state
        def saved(_):
            state.project_path = path
            state.name = Path(path).name
            self.refresh(self._composite)
        if synchronous:
            try:
                doc.save_project(path)
                saved(None)
                return True
            except Exception as exc:
                QMessageBox.warning(self, "Save failed", str(exc))
                return False
        return self.run("Saving project", lambda: doc.save_project(path), saved)

    def set_tool(self, tool):
        self.canvas.set_tool(tool)
        for key, button in self.tool_buttons.items():
            button.setChecked(key == tool)
        titles = {k: d.split(" (")[0].split(" ·")[0] for k, _, d in TOOLS}
        self.tool_title.setText(titles.get(tool, tool))
        selection = tool in ("rect", "ellipse", "lasso", "polygon", "wand", "quick", "color")
        brush = tool in ("pen", "highlighter", "eraser", "background", "restore", "quick", "clone", "heal", "text", "bubble", "shape", "circle", "arrow", "wand", "magic")
        self.brush_panel.setVisible(brush)
        form = self.brush_panel.layout()
        for row in (0, 1, 4):  # size, hardness and the brush preview mean nothing to a click tool
            form.setRowVisible(row, tool != "magic")
        self.magic_panel.setVisible(tool == "magic")
        self.color_button.setVisible(tool in ("pen", "highlighter", "text", "bubble", "shape", "circle", "arrow"))
        self.selection_mode.setVisible(selection)
        self.crop_panel.setVisible(tool == "crop")
        hints = {"clone": "Option/Alt-click a source, then paint. Applies to the active layer.",
                 "quick": "Paint inside an object. Local edge and colour analysis follows nearby boundaries.",
                 "eraser": "Paint the layer mask to hide pixels; Restore recovers them.",
                 "magic": "Click a colour to erase that whole area in one go, such as a plain background. "
                          "Raise Tolerance to take in more shades. Nothing is deleted: Restore brings pixels back.",
                 "background": "Paint to hide colours matching the stroke’s starting point.",
                 "restore": "Restore hidden pixels in the active layer’s mask.",
                 "heal": "Paint over dust or an unwanted object. Local inpainting reconstructs the area.",
                 "move": "Drag the active layer. Space or middle-mouse drag pans the canvas.",
                 "crop": "Drag a crop, adjust its corners, then press Enter. Escape cancels.",
                 "polygon": "Click vertices. Double-click or Enter closes the selection.",
                 "text": "Click to add text. Double-click its layer to edit it.",
                 "bubble": "Click to add a numbered comment. Comments remain editable in PowerPoint."}
        self.hint.setText(hints.get(tool, "Work in source pixels. Shift adds to selections; Option/Alt subtracts."))
        self._brush_changed()

    def _brush_changed(self, *_):
        if not hasattr(self, "preview"):
            return
        self.canvas.diameter = self.size.value()
        self.canvas.hardness = self.hardness.value() / 100
        self.canvas.color = self.color
        self.preview.diameter = self.size.value()
        self.preview.hardness = self.hardness.value() / 100
        self.preview.opacity = self.opacity.value() / 100
        self.preview.color = self.color
        self.preview.update()
        self.canvas.viewport().update()

    def choose_color(self):
        color = QColorDialog.getColor(QColor(self.color), self, "Tool colour")
        if color.isValid():
            self.color = color.name()
            self.color_button.setText(f"Colour  {self.color}")
            self._brush_changed()

    def _selection_mode(self, mods):
        shift, alt = bool(mods & Qt.KeyboardModifier.ShiftModifier), bool(mods & Qt.KeyboardModifier.AltModifier)
        return "intersect" if shift and alt else "add" if shift else "subtract" if alt else self.selection_mode.currentData()

    def set_selection(self, mask):
        if self.state:
            self.state.selection = mask
            self.canvas.set_selection(mask)

    def on_gesture(self, tool, points, mods):
        if not self.doc or self._job:
            return
        doc, layer = self.doc, self.doc.active_layer
        mode = self._selection_mode(mods)
        radius, hardness, opacity = self.size.value() / 2, self.hardness.value() / 100, self.opacity.value() / 100
        tolerance = self.tolerance.value()
        if tool == "clone_source":
            self._clone_source = (points[0][0] - layer.x, points[0][1] - layer.y)
            self.status.setText("Clone source sampled. Paint to clone onto the active layer.")
            return
        if tool in ("rect", "ellipse", "lasso", "polygon", "wand", "quick", "color"):
            old = self.selection
            image = self._composite
            def select():
                if tool in ("rect", "ellipse"):
                    a, b = points[0], points[-1]
                    new = T.rectangle_selection(doc.size, (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), tool == "ellipse")
                elif tool in ("lasso", "polygon"):
                    new = T.polygon_selection(doc.size, points)
                elif tool == "quick":
                    new = T.quick_selection(image, points, radius=radius)
                else:
                    new = T.color_selection(image, points[0], tolerance, contiguous=tool == "wand")
                return T.combine_selection(old, new, mode)
            self.run("Updating selection", select, self.set_selection)
            return
        if layer.locked:
            self.status.setText("Unlock the active layer before editing it.")
            return
        if tool in ("pen", "highlighter", "text", "shape", "circle", "arrow", "bubble"):
            props = dict(tool=tool, points=points, color=self.color, width=self.size.value(), opacity=opacity,
                         render_size=list(doc.size))
            if tool == "text":
                text, ok = QInputDialog.getMultiLineText(self, "Add text", "Text")
                if not ok or not text:
                    return
                props["text"] = text
            if tool == "bubble":
                text, ok = QInputDialog.getMultiLineText(self, "Numbered comment", "Comment")
                if not ok:
                    return
                props["comment"] = text
                props["number"] = 1 + max([int(l.properties.get("number", 0)) for l in doc.layers] or [0])
            image = render_annotation(doc.size, props)
            if self.selection is not None:
                image.putalpha(ImageChops.multiply(image.getchannel("A"), self.selection))
            bounds = image.getbbox()
            if bounds is None:
                return
            # Keep sparse annotations small instead of a full canvas for each stroke.
            props["render_crop"] = list(bounds)
            new = ImageLayer(props.get("text", props.get("comment", tool.title()))[:60] or tool.title(), image.crop(bounds),
                             kind="text" if tool == "text" else "annotation", properties=props,
                             x=bounds[0], y=bounds[1])
            self.mutate("Add annotation", lambda d: d.add_layer(new), snapshot=False)
            return
        if tool == "magic":
            x, y = points[0][0] - layer.x, points[0][1] - layer.y
            if not (0 <= x < layer.image.width and 0 <= y < layer.image.height):
                self.status.setText("Click inside the active layer to erase with the Magic Eraser.")
                return
            selected = self.selection.crop((layer.x, layer.y, layer.x + layer.image.width, layer.y + layer.image.height)) if self.selection else None
            contiguous, antialias = self.contiguous.isChecked(), self.antialias.isChecked()
            def erase(d):
                active = d.active_layer
                base = active.mask or Image.new("L", active.image.size, 255)
                changed = T.magic_erase(active.image, (x, y), tolerance, contiguous, antialias, opacity, mask=base)
                active.mask = Image.composite(changed, base, selected) if selected else changed
            self.mutate("Magic Eraser", erase)
            return
        local = [(x - layer.x, y - layer.y) for x, y in points]
        selected = self.selection.crop((layer.x, layer.y, layer.x + layer.image.width, layer.y + layer.image.height)) if self.selection else None
        def apply(d):
            active = d.active_layer
            if tool == "move":
                active.x += round(points[-1][0] - points[0][0])
                active.y += round(points[-1][1] - points[0][1])
            elif tool in ("eraser", "restore", "background"):
                base = active.mask or Image.new("L", active.image.size, 255)
                if tool == "background":
                    changed = T.background_erase(active.image, local, radius, tolerance, hardness, mask=base)
                else:
                    changed = T.paint_mask(base, local, radius, hardness, opacity, restore=tool == "restore")
                active.mask = Image.composite(changed, base, selected) if selected else changed
            elif tool == "clone":
                if self._clone_source is None:
                    raise ValueError("Option/Alt-click to choose a clone source first.")
                result = T.clone_stamp(active.image, local, self._clone_source, radius, hardness, opacity)
                active.image = Image.composite(result, active.image, selected) if selected else result
            elif tool == "heal":
                mask = T.brush_mask(active.image.size, local, radius, hardness, opacity)
                if selected:
                    mask = ImageChops.multiply(mask, selected)
                active.image = T.heal(active.image, mask)
        self.mutate(tool.title(), apply)

    def _canvas_action(self, action):
        if action == "crop":
            self.apply_crop()
        elif action == "delete":
            self.delete_selection()
        elif action == "smaller":
            self.size.setValue(max(1, round(self.size.value() * .85)))
        elif action == "larger":
            self.size.setValue(min(1000, round(self.size.value() * 1.15) + 1))

    def _crop_changed(self, rect):
        self.crop_info.setText(f"{round(rect.width())} × {round(rect.height())} px\nDrag a corner to adjust; Enter applies.")

    def apply_crop(self):
        rect = self.canvas.crop_rect
        if self.doc and rect and rect.width() >= 1 and rect.height() >= 1:
            box = tuple(round(v) for v in (rect.left(), rect.top(), rect.right(), rect.bottom()))
            self.mutate("Crop", lambda d: d.crop(box), snapshot=False, clear_selection=True)

    def resize_image(self, canvas=False):
        if not self.doc:
            return
        dlg = ResizeDialog(self.doc, canvas, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            w, h = dlg.dimensions()
            dpi = dlg.dpi.value()
            def apply(doc):
                doc.resize_canvas(w, h) if canvas else doc.resize(w, h)
                doc.dpi = dpi
            self.mutate("Resize canvas" if canvas else "Resize", apply, snapshot=False, clear_selection=True)

    def transform(self, method, *args):
        if self.doc:
            self.mutate(method.title(), lambda d: getattr(d, method)(*args), snapshot=False, clear_selection=True)

    def straighten(self):
        angle, ok = QInputDialog.getDouble(self, "Straighten", "Degrees counterclockwise", 0, -180, 180, 2)
        if ok:
            self.transform("rotate", angle)

    def history(self, redo):
        if self.doc:
            self.mutate("Redo" if redo else "Undo", lambda d: d.redo() if redo else d.undo(), snapshot=False, clear_selection=True)

    def _layer_selected(self, row):
        if self._refreshing or not self.doc or row < 0:
            return
        self.doc.active_index = len(self.doc.layers) - row - 1
        self._refreshing = True
        layer = self.doc.active_layer
        self.layer_opacity.setValue(round(layer.opacity * 100))
        self.blend.setCurrentText(layer.blend)
        self.locked.setChecked(layer.locked)
        self._refreshing = False
        self._clone_source = None

    def _layer_changed(self, item):
        if not self._refreshing and self.doc:
            index = item.data(Qt.ItemDataRole.UserRole)
            visible = item.checkState() == Qt.CheckState.Checked
            self.mutate("Layer visibility", lambda d: setattr(d.layers[index], "visible", visible))

    def change_layer_opacity(self):
        if not self._refreshing and self.doc:
            value = self.layer_opacity.value() / 100
            self.mutate("Layer opacity", lambda d: setattr(d.active_layer, "opacity", value))

    def change_blend(self, value):
        if not self._refreshing and self.doc:
            self.mutate("Layer blend", lambda d: setattr(d.active_layer, "blend", value))

    def change_locked(self, value):
        if not self._refreshing and self.doc:
            self.mutate("Layer lock", lambda d: setattr(d.active_layer, "locked", value))

    def add_layer(self):
        if self.doc:
            self.mutate("Add layer", lambda d: d.add_layer(ImageLayer("Transparent layer", Image.new("RGBA", d.size))), snapshot=False)

    def import_layer(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import image layer", "", IMAGE_FILTER)
        if path and self.doc:
            self.mutate("Import layer", lambda d: d.add_layer(ImageDocument.from_file(path).active_layer), snapshot=False)

    def duplicate_layer(self):
        if self.doc:
            self.mutate("Duplicate layer", lambda d: d.duplicate_layer(d.active_index), snapshot=False)

    def remove_layer(self):
        if self.doc:
            self.mutate("Delete layer", lambda d: d.remove_layer(d.active_index), snapshot=False)

    def reorder_layer(self, delta):
        if self.doc:
            self.mutate("Reorder layer", lambda d: d.move_layer(d.active_index, max(0, min(len(d.layers) - 1, d.active_index + delta))), snapshot=False)

    def rename_layer(self):
        if not self.doc:
            return
        name, ok = QInputDialog.getText(self, "Rename layer", "Name", text=self.doc.active_layer.name)
        if ok and name:
            self.mutate("Rename layer", lambda d: setattr(d.active_layer, "name", name))

    def edit_layer(self):
        if not self.doc:
            return
        layer = self.doc.active_layer
        if layer.locked:
            return
        props = dict(layer.properties)
        key = "text" if props.get("tool") == "text" else "comment" if props.get("tool") == "bubble" else None
        if not key:
            self.rename_layer()
            return
        text, ok = QInputDialog.getMultiLineText(self, "Edit " + key, key.title(), props.get(key, ""))
        if ok:
            props[key] = text
            image = render_annotation(tuple(props.get("render_size", self.doc.size)), props)
            if props.get("raster_ops"):
                image = render_transformed_annotation(props)
                new_x, new_y = layer.x, layer.y
            elif props.get("render_crop"):
                # A text edit may grow beyond its previous bounds. Preserve the
                # original origin and expand the cache to contain new glyphs.
                old = props["render_crop"]
                new = image.getbbox() or old
                bounds = (min(old[0], new[0]), min(old[1], new[1]), max(old[2], new[2]), max(old[3], new[3]))
                image = image.crop(bounds)
                sx = layer.image.width / max(1, old[2] - old[0])
                sy = layer.image.height / max(1, old[3] - old[1])
                image = image.resize((max(1, round(image.width * sx)), max(1, round(image.height * sy))), Image.Resampling.LANCZOS)
                props["render_crop"] = list(bounds)
                new_x = layer.x + round((bounds[0] - old[0]) * sx)
                new_y = layer.y + round((bounds[1] - old[1]) * sy)
            else:
                new_x, new_y = layer.x, layer.y
            if not props.get("render_crop") and image.size != layer.image.size:
                image = image.resize(layer.image.size, Image.Resampling.LANCZOS)
            def apply(doc):
                doc.active_layer.properties = props
                if doc.active_layer.mask is not None and doc.active_layer.mask.size != image.size:
                    mask = Image.new("L", image.size, 255)
                    mask.paste(doc.active_layer.mask, (doc.active_layer.x - new_x, doc.active_layer.y - new_y))
                    doc.active_layer.mask = mask
                doc.active_layer.image = image
                doc.active_layer.x, doc.active_layer.y = new_x, new_y
            self.mutate("Edit annotation", apply)

    def scale_layer(self):
        if not self.doc or self.doc.active_layer.locked:
            return
        percent, ok = QInputDialog.getDouble(self, "Scale layer", "Percent", 100, 1, 1000, 1)
        if ok:
            def apply(doc):
                layer = doc.active_layer
                size = (max(1, round(layer.image.width * percent / 100)), max(1, round(layer.image.height * percent / 100)))
                from ...core.image_document import _dimensions
                _dimensions(*size)
                if layer.properties.get("tool"):
                    layer.properties.setdefault("raster_ops", []).append(["resize", *size])
                layer.image = layer.image.resize(size, Image.Resampling.LANCZOS)
                if layer.mask:
                    layer.mask = layer.mask.resize(size, Image.Resampling.LANCZOS)
            self.mutate("Scale layer", apply)

    def add_mask(self):
        if self.doc and not self.doc.active_layer.locked:
            self.mutate("Add mask", lambda d: setattr(d.active_layer, "mask", Image.new("L", d.active_layer.image.size, 255)))

    def remove_mask(self):
        if self.doc and not self.doc.active_layer.locked:
            self.mutate("Remove mask", lambda d: setattr(d.active_layer, "mask", None))

    def select_all(self):
        if self.doc:
            self.set_selection(Image.new("L", self.doc.size, 255))

    def invert_selection(self):
        if self.doc:
            self.set_selection(ImageChops.invert(self.selection) if self.selection else Image.new("L", self.doc.size, 255))

    def refine_selection(self):
        if self.selection is None:
            return
        feather, ok = QInputDialog.getDouble(self, "Refine selection", "Feather radius (pixels)", 2, 0, 256, 1)
        if not ok:
            return
        grow, ok = QInputDialog.getInt(self, "Refine selection", "Grow (+) or shrink (−), pixels", 0, -256, 256)
        if not ok:
            return
        smooth, ok = QInputDialog.getInt(self, "Refine selection", "Smooth radius (pixels)", 0, 0, 32)
        if ok:
            mask = self.selection
            self.run("Refining selection", lambda: T.refine_selection(mask, feather, grow, smooth), self.set_selection)

    def _layer_selection(self):
        if self.selection is None or not self.doc:
            return None
        l = self.doc.active_layer
        return self.selection.crop((l.x, l.y, l.x + l.image.width, l.y + l.image.height))

    def selection_to_mask(self):
        mask = self._layer_selection()
        if mask is not None and not self.doc.active_layer.locked:
            self.mutate("Selection to mask", lambda d: setattr(d.active_layer, "mask", mask))

    def mask_to_selection(self):
        if self.doc and self.doc.active_layer.mask:
            layer = self.doc.active_layer
            mask = Image.new("L", self.doc.size)
            mask.paste(layer.mask, (layer.x, layer.y))
            self.set_selection(mask)

    def extract_selection(self):
        if self.doc and self.selection is not None:
            image = self._composite.copy()
            image.putalpha(ImageChops.multiply(image.getchannel("A"), self.selection))
            self.mutate("Extract selection", lambda d: d.add_layer(ImageLayer("Extracted selection", image)), snapshot=False)

    def delete_selection(self):
        mask = self._layer_selection()
        if mask is not None and not self.doc.active_layer.locked:
            def apply(doc):
                base = doc.active_layer.mask or Image.new("L", doc.active_layer.image.size, 255)
                doc.active_layer.mask = ImageChops.multiply(base, ImageChops.invert(mask))
            self.mutate("Hide selected pixels", apply)

    def heal_selection(self):
        mask = self._layer_selection()
        if mask is not None and not self.doc.active_layer.locked:
            self.mutate("Heal selected area", lambda d: setattr(d.active_layer, "image", T.heal(d.active_layer.image, mask)))

    def choose_model(self):
        path = QFileDialog.getExistingDirectory(self, "Choose an offline model package")
        if path:
            from ...core.image_ai import read_model_pack, OfflineSegmenter
            def load():
                pack = read_model_pack(path)
                return OfflineSegmenter(pack, provider=self._provider), pack
            def done(result):
                self._model, pack = result
                self._model_path = path
                self.model_label.setText(pack.name + " · local " + self._provider)
            self.run("Validating offline model package", load, done)

    def choose_provider(self):
        value, ok = QInputDialog.getItem(self, "Offline inference backend", "Unavailable providers fall back to CPU.",
                                         ["cpu", "coreml", "directml", "auto"], 0, False)
        if ok:
            self._provider = value
            if self._model is not None:
                from ...core.image_ai import OfflineSegmenter
                self._model = OfflineSegmenter(self._model.pack, provider=value)
                self.model_label.setText(self._model.pack.name + " · local " + value)

    def run_model(self, remove):
        if not self.doc:
            return
        if self._model is None:
            self.choose_model()
            return
        if remove and self.doc.active_layer.locked:
            self.status.setText("Unlock the active layer to apply a mask.")
            return
        image = self.doc.active_layer.image if remove else self._composite
        model = self._model
        def infer():
            return model.remove_background_mask(image, progress=self._job.signals.progress.emit)
        if remove:
            self.mutate("Removing background", lambda d: setattr(d.active_layer, "mask", infer()))
        else:
            self.run("Selecting subject", infer, self.set_selection)

    def copy_image(self, force_dialog=False):
        if not self.doc or self._job:
            return
        from .clipboard_dialog import ClipboardDialog
        from ...core.image_clipboard import prepare_clipboard, write_clipboard
        options = dict(self.owner.settings.get("image_clipboard", {}))
        if force_dialog or not options.get("remember") or (options.get("region") == "selection" and self.selection is None):
            dialog = ClipboardDialog(options, self.selection is not None, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            options = dialog.options()
            self.owner.settings.set("image_clipboard", options)
            self.owner.settings.save()
        image, selection = self._composite, self.selection
        def done(payload):
            try:
                write_clipboard(payload, int(self.window().winId()))
                self.status.setText(f"Copied {payload.format} · {payload.image.width} × {payload.image.height} px · editable layers unchanged")
            except Exception as exc:
                QMessageBox.warning(self, "Clipboard", str(exc))
        self.run("Preparing clipboard image", lambda: prepare_clipboard(image, options, selection), done)

    def paste_image(self):
        image = QApplication.clipboard().image()
        if image.isNull():
            self.status.setText("The clipboard does not contain an image.")
        else:
            self.open_image(from_qimage(image), "Clipboard image")

    def send_to_queue(self):
        if not self.doc:
            return
        comments = []
        for layer in self.doc.layers:
            if layer.visible and layer.properties.get("tool") == "bubble":
                comments.append((int(layer.properties["number"]), layer.properties.get("comment", "")))
        text = "\n".join(f"{number}. {comment}" for number, comment in sorted(comments))
        self.owner.add_marked_page(self.doc.source_path or self.state.name, 0, to_qimage(self._composite), text, len(comments))
        self.status.setText("Added image and editable numbered comments to the PowerPoint queue.")

    def export_image(self):
        if self.doc:
            dialog = ExportDialog(self.doc, self)
            self._dialogs.append(dialog)
            dialog.exec()

    def batch(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Batch convert images", "", IMAGE_FILTER)
        if not paths:
            return
        output = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if not output:
            return
        fmt, ok = QInputDialog.getItem(self, "Batch conversion", "Format", ["JPEG", "PNG", "WEBP", "TIFF"], 0, False)
        if not ok:
            return
        target, ok = QInputDialog.getInt(self, "Batch compression", "Target KB (0 = no target)", 0, 0, 2_000_000)
        if ok:
            from ...core.image_export import batch_convert
            self.run("Converting images", lambda: batch_convert(paths, output, format=fmt, target_bytes=target * 1000 or None),
                     lambda outputs: self.status.setText(f"Saved {len(outputs)} new images. Originals preserved."))

    def remove_background(self):
        if not self.doc:
            return
        layer = self.doc.active_layer
        if layer.locked:
            self.status.setText("Unlock the active layer to remove its background.")
            return
        image = layer.image
        def apply(d):
            active = d.active_layer
            found = T.remove_background(image)
            active.mask = ImageChops.multiply(active.mask, found) if active.mask else found
        self.mutate("Remove background", apply)

    @staticmethod
    def _dropped(event):
        mime = event.mimeData()
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in IMAGE_SUFFIXES] if mime.hasUrls() else []

    def dragEnterEvent(self, event):
        if self._dropped(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._dropped(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = self._dropped(event)
        if paths:
            event.acceptProposedAction()
            self._opening.extend(paths[1:])
            self.open_file(paths[0])

    def vectorize(self):
        if self._composite is not None:
            self.owner.open_vector_image(self._composite)

    def _toggle_view(self, name):
        box = {"grid": self.grid_box, "rulers": self.rulers_box}.get(name)
        if box is not None:
            box.toggle()
            return
        setattr(self.canvas, name, not getattr(self.canvas, name))
        self.canvas.viewport().update()

    def _set_view(self, name, on):
        setattr(self.canvas, name, on)
        self.canvas.viewport().update()

    def clear_guides(self):
        self.canvas.guides = []
        self._guides_changed([])

    def _guides_changed(self, guides):
        if self.doc:
            self.doc.snapshot("Guides")
            self.doc.metadata["guides"] = guides
            self.doc.touch()
            self.canvas.viewport().update()
