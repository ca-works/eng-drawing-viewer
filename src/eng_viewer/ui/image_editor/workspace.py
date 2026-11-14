"""Integrated, layered image editing workspace for ENG Drawing Viewer.

Composition works like Photoshop: images dropped on the canvas or the Layers panel, pasted
(⌘V / Ctrl+V) or imported become new layers of the open document; the Move tool moves,
scales and rotates the active layer on the canvas; ⌘C copies the active layer (or its
selected pixels), ⌘⇧C copies the merged image, ⌘X cuts and ⌘J duplicates. Dropping on the
document tabs, holding Option/Alt while dropping, or Open, opens a separate document.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops
from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QSize, QThreadPool, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QRadialGradient, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QTabBar,
    QToolButton, QVBoxLayout, QWidget)

from ...core.image_document import ImageDocument, ImageLayer, BLEND_MODES, _decode_image, affine_about
from ...core import image_tools as T
from .. import icons
from ..theme import IS_MAC, ThemeManager, qc
from ..view_options import ViewOptionsBar
from ..widgets import (DropdownButton, label, menu_button, primary_button, secondary_button, separator,
    tool_button)
from .annotations import render_annotation, render_transformed_annotation
from .canvas import ImageCanvas
from .common import Job, to_qimage, from_qimage
from .dialogs import ExportDialog, ResizeDialog

IMAGE_SUFFIXES = {".engimg", ".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".avif", ".heic", ".heif"}
IMAGE_FILTER = "Images and projects (*.engimg *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif *.avif *.heic *.heif)"
LAYER_FILTER = "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif *.avif *.heic *.heif)"
OPTION = "Option" if IS_MAC else "Alt"
TOOLS = [("move", "move", "Move and transform layer (V) · drag handles to scale, the knob to rotate"),
         ("rect", "select-rect", "Rectangle selection (M)"),
         ("ellipse", "select-ellipse", "Ellipse selection"), ("lasso", "lasso", "Freehand Lasso (L)"),
         ("polygon", "polygon", "Polygonal Lasso · double-click to finish"),
         ("wand", "wand", "Magic Wand (W)"), ("quick", "quick-select", "Quick Selection (Q)"),
         ("crop", "crop", "Crop (C) · Enter to apply"), ("pen", "pen", "Brush / Pen (B)"),
         ("highlighter", "highlighter", "Highlighter"), ("bucket", "bucket", "Paint Bucket (G) · fills a colour area"),
         ("eraser", "eraser", "Mask Eraser (E)"),
         ("magic", "magic-eraser", "Magic Eraser (X) · one click erases a whole area"),
         ("background", "background-erase", "Background Eraser brush"), ("restore", "restore", "Restore Mask"),
         ("clone", "clone", f"Clone Stamp (S) · {OPTION}-click to sample"),
         ("heal", "heal", "Spot Healing (J)"), ("text", "text", "Text (T)"),
         ("shape", "rectangle", "Rectangle (U)"), ("circle", "circle", "Outline circle"),
         ("arrow", "arrow", "Arrow"), ("bubble", "bubble", "Numbered comment")]


class DropOverlay(QWidget):
    """Drag-over feedback: what releasing the dragged images will do, and where."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title = ""
        self.detail = ""
        self.icon_name = "plus"
        self.hide()

    def show_hint(self, rect, title, detail, icon_name):
        self.setGeometry(rect)
        self.title, self.detail, self.icon_name = title, detail, icon_name
        self.raise_()
        self.show()
        self.update()

    def paintEvent(self, event):
        t = ThemeManager.current()
        accent = qc(t.accent)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        fill = QColor(accent)
        fill.setAlphaF(0.12)
        p.setBrush(fill)
        pen = QPen(accent, 2, Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawRoundedRect(area, 10, 10)
        title_font = self.font()
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setBold(True)
        detail_font = self.font()
        p.setFont(title_font)
        tm = p.fontMetrics()
        p.setFont(detail_font)
        dm = p.fontMetrics()
        width = max(tm.horizontalAdvance(self.title) + 34, dm.horizontalAdvance(self.detail)) + 36
        height = tm.height() + (dm.height() + 6 if self.detail else 0) + 26
        pill = QRectF(area.center().x() - width / 2, area.center().y() - height / 2, width, height)
        if area.height() < height + 8:
            pill.moveTop(area.top() + 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(t.elevated))
        p.drawRoundedRect(pill, 10, 10)
        p.setPen(QPen(accent, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(pill, 10, 10)
        title_w = tm.horizontalAdvance(self.title) + 26
        x = pill.center().x() - title_w / 2
        y = pill.top() + 13
        icons.paint_icon(p, self.icon_name, QRectF(x, y + (tm.height() - 18) / 2, 18, 18), accent)
        p.setPen(qc(t.text))
        p.setFont(title_font)
        p.drawText(QRectF(x + 26, y, title_w, tm.height()), Qt.AlignmentFlag.AlignVCenter, self.title)
        if self.detail:
            p.setFont(detail_font)
            p.setPen(qc(t.text_secondary))
            p.drawText(QRectF(pill.left(), y + tm.height() + 6, pill.width(), dm.height()), Qt.AlignmentFlag.AlignHCenter, self.detail)
        p.end()


class PlacementBar(QWidget):
    """Offered when a placed or pasted layer is larger than the canvas; nothing is downscaled unasked."""

    def __init__(self, workspace):
        super().__init__()
        self.setObjectName("OptionsBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 10, 6)
        row.setSpacing(8)
        info = QLabel()
        info.setPixmap(icons.pixmap("info", 16, qc(ThemeManager.current().accent)))
        row.addWidget(info)
        self.text = label("", "small")
        self.text.setWordWrap(True)
        row.addWidget(self.text, 1)
        for title, slot, tooltip in (("Fit to canvas", workspace.fit_layer, "Scale the layer down proportionally to fit inside the canvas"),
                                     ("Expand canvas", workspace.reveal_all, "Grow the canvas to include every layer"),
                                     ("Keep size", self.hide, "Keep every original pixel; move or scale it yourself")):
            button = secondary_button(title)
            button.setToolTip(tooltip)
            button.clicked.connect(slot)
            button.clicked.connect(self.hide)
            row.addWidget(button)
        self.hide()


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
        self._drop_overlay = DropOverlay(self)
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
    def active_layer(self):
        return self.doc.active_layer if self.doc and self._job is None else None

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
        self.layer_menu = self._menu_button("Layer", [
            ("New transparent layer", self.add_layer), ("Import image as layer…", self.import_layer),
            ("Paste as new layer", self.paste), ("", None),
            ("Duplicate layer / layer via copy", self.duplicate_layer), ("Layer via cut", self.layer_via_cut),
            ("Delete layer", self.remove_layer), ("", None),
            ("Transform…", self.transform_dialog), ("Fit to canvas", self.fit_layer),
            ("Center on canvas", self.center_layer), ("Reset transform (original pixels)", self.reset_layer_transform),
            ("Rotate 90° clockwise", lambda: self.rotate_layer(90)), ("Rotate 90° counterclockwise", lambda: self.rotate_layer(-90)),
            ("Flip horizontal", lambda: self.flip_layer(True)), ("Flip vertical", lambda: self.flip_layer(False)), ("", None),
            ("Bring forward", lambda: self.reorder_layer(1)), ("Send backward", lambda: self.reorder_layer(-1)),
            ("Merge down", self.merge_down), ("Expand canvas to fit layers (Reveal All)", self.reveal_all)])
        row.addWidget(self.layer_menu)
        row.addStretch(1)
        self.save_button = tool_button("save", "Save editable project (.engimg)")
        self.save_button.clicked.connect(self.save_project)
        row.addWidget(self.save_button)
        self.copy_button = DropdownButton("Copy to Clipboard", "copy-image", split=True)
        self.copy_button.compact_text = "Copy"
        self.copy_button.setToolTip("Copy the visible image in your clipboard format · the arrow shows more copy and paste options")
        copy_menu = self.copy_button.dropdown_menu()
        copy_menu.addAction(f"Copy layer or selection{self._key_hint('Ctrl+C')}", self.copy)
        copy_menu.addAction(f"Copy merged image{self._key_hint('Ctrl+Shift+C')}", lambda: self.copy(merged=True))
        copy_menu.addAction("Clipboard format and options…", lambda: self.copy_image(True))
        copy_menu.addSeparator()
        copy_menu.addAction(f"Paste as new layer{self._key_hint('Ctrl+V')}", self.paste)
        copy_menu.addAction("Paste as new document", lambda: self.paste(as_document=True))
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
        self.canvas.transformFinished.connect(self._transform_finished)
        self.canvas.layerPickRequested.connect(self._pick_layer)
        self.canvas.nudgeRequested.connect(self._nudge)
        self.canvas.preview_provider = self._layer_preview
        self.canvas.snap_provider = self._snap_targets
        self._preview_cache = None
        canvas_box = QWidget()
        canvas_column = QVBoxLayout(canvas_box)
        canvas_column.setContentsMargins(0, 0, 0, 0)
        canvas_column.setSpacing(0)
        self.placement_bar = PlacementBar(self)
        canvas_column.addWidget(self.placement_bar)
        canvas_column.addWidget(self.canvas, 1)
        split.addWidget(canvas_box)
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
        self.tolerance = self._slider_row(bf, "Tolerance", 0, 100, T.DEFAULT_TOLERANCE)
        self.tolerance.setToolTip("Perceptual colour difference from the clicked colour: about 5 takes in only near-identical "
                                  "shades, 15–25 related shades, 40 and more a broad range.")
        self.preview = BrushPreview()
        bf.addRow(self.preview)
        sl.addWidget(self.brush_panel)
        self.color_button = QPushButton("Colour")
        self.color_button.setToolTip("Foreground colour for the brush, Paint Bucket, text and shapes")
        self.color_button.clicked.connect(self.choose_color)
        sl.addWidget(self.color_button)
        self._update_color_button()
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
        self.move_panel = QWidget()
        mv = QVBoxLayout(self.move_panel)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(6)
        self.auto_select = QCheckBox("Auto-select layer")
        self.auto_select.setChecked(True)
        self.auto_select.setToolTip("Clicking picks the topmost layer under the pointer, as in Photoshop. "
                                    "Off: dragging anywhere moves the active layer.")
        self.auto_select.toggled.connect(lambda on: setattr(self.canvas, "auto_select", on))
        mv.addWidget(self.auto_select)
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        self.transform_fields = {}
        for i, (key, title, lo, hi, suffix) in enumerate((("x", "X", -1e6, 1e6, " px"), ("y", "Y", -1e6, 1e6, " px"),
                                                          ("w", "W", 1, 1e6, " px"), ("h", "H", 1, 1e6, " px"),
                                                          ("angle", "Angle", -180, 180, "°"))):
            field = QDoubleSpinBox()
            field.setRange(lo, hi)
            field.setDecimals(1 if key == "angle" else 0)
            field.setSuffix(suffix)
            field.setKeyboardTracking(False)
            field.editingFinished.connect(lambda k=key: self._transform_field_changed(k))
            self.transform_fields[key] = field
            grid.addWidget(label(title, "small"), i // 2, (i % 2) * 2)
            grid.addWidget(field, i // 2, (i % 2) * 2 + 1)
        self.keep_aspect = QCheckBox("Keep proportions")
        self.keep_aspect.setChecked(True)
        self.keep_aspect.setToolTip("Typing W or H changes the other to match")
        mv.addLayout(grid)
        mv.addWidget(self.keep_aspect)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        for icon_name, tooltip, slot in (("fit-page", "Fit layer to canvas", self.fit_layer),
                                         ("actual-size", "Center layer on canvas", self.center_layer),
                                         ("rotate-page", "Rotate layer 90° clockwise", lambda: self.rotate_layer(90)),
                                         ("split", "Flip layer horizontally", lambda: self.flip_layer(True)),
                                         ("refresh", "Reset transform to the original pixels", self.reset_layer_transform)):
            button = tool_button(icon_name, tooltip, size=28, icon_size=16)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        mv.addLayout(buttons)
        sl.addWidget(self.move_panel)
        self.magic_panel = QWidget()
        ml = QVBoxLayout(self.magic_panel)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(4)
        self.contiguous = QCheckBox("Contiguous")
        self.contiguous.setChecked(True)
        self.contiguous.setToolTip("Only the connected area you click. Off: that colour everywhere in the layer.")
        ml.addWidget(self.contiguous)
        self.stop_edges = QCheckBox("Stop at edges")
        self.stop_edges.setChecked(True)
        self.stop_edges.setToolTip("Visible boundaries stop the area, so a similar-coloured subject beside the backdrop is kept. "
                                   "Off: colour alone decides.")
        ml.addWidget(self.stop_edges)
        self.antialias = QCheckBox("Anti-alias edges")
        self.antialias.setChecked(True)
        self.antialias.setToolTip("Soft, partly transparent boundary pixels instead of a stair-stepped edge")
        ml.addWidget(self.antialias)
        self.sample_all = QCheckBox("Sample all layers")
        self.sample_all.setToolTip("Find the area in the visible image rather than in the active layer alone")
        ml.addWidget(self.sample_all)
        self.fill_new_layer = QCheckBox("Fill on a new layer")
        self.fill_new_layer.setToolTip("Put the fill on its own layer above, leaving the active layer's pixels unchanged")
        ml.addWidget(self.fill_new_layer)
        self.auto_background = secondary_button("Remove Background", "magic-eraser")
        self.auto_background.setToolTip("Finds the subject and hides everything around it through the layer mask. "
                                        "Works offline, no model needed.")
        self.auto_background.clicked.connect(self.remove_background)
        sl.addWidget(self.magic_panel)
        sl.addWidget(self.auto_background)
        ai = self._menu_button("Offline subject selection", [("Choose offline model package…", self.choose_model),
            ("Select subject", lambda: self.run_model(False)), ("Remove background to mask", lambda: self.run_model(True)),
            ("Remove background automatically (no model)", self.remove_background),
            ("Inference backend…", self.choose_provider)])
        ai.dropdown_menu().addSeparator()
        self.clean_edges = ai.dropdown_menu().addAction("Clean edge colours on a new layer")
        self.clean_edges.setCheckable(True)
        self.clean_edges.setToolTip("Background removal puts a copy with the old backdrop's tint removed from its edges on a "
                                    "new layer and hides the original layer (kept unchanged).")
        sl.addWidget(ai)
        self.model_label = QLabel("No model loaded · use a local package")
        self.model_label.setWordWrap(True)
        self.model_label.setProperty("role", "small")
        sl.addWidget(self.model_label)
        sl.addWidget(separator(False))
        layer_heading = QHBoxLayout()
        layer_heading.addWidget(label("Layers", "title"), 1)
        place = tool_button("image", "Import image as layer…", size=26, icon_size=16)
        place.clicked.connect(self.import_layer)
        layer_heading.addWidget(place)
        add = tool_button("plus", "Add transparent layer", size=26)
        add.clicked.connect(self.add_layer)
        layer_heading.addWidget(add)
        self.delete_layer_button = tool_button("trash", "Delete layer", size=26, icon_size=16)
        self.delete_layer_button.clicked.connect(self.remove_layer)
        layer_heading.addWidget(self.delete_layer_button)
        layer_more = self._menu_button("", [("Rename layer…", self.rename_layer), ("Duplicate layer", self.duplicate_layer),
            ("Scale layer…", self.scale_layer), ("Add white mask", self.add_mask),
            ("Remove mask", self.remove_mask), ("Mask from selection", self.selection_to_mask)], icon_name="more",
            variant="ghost")
        layer_more.setToolTip("More layer commands")
        layer_heading.addWidget(layer_more)
        sl.addLayout(layer_heading)
        self.layers = QListWidget()
        self.layers.setIconSize(QSize(48, 38))
        self.layers.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.layers.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.layers.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.layers.model().rowsMoved.connect(self._layer_rows_moved)
        self.layers.currentRowChanged.connect(self._layer_selected)
        self.layers.itemChanged.connect(self._layer_changed)
        self.layers.itemDoubleClicked.connect(lambda _: self.edit_layer())
        self.layers.setToolTip("Drag to reorder · double-click to rename · drop images here to add layers")
        self.layers.setMinimumHeight(125)
        sl.addWidget(self.layers, 1)
        lf = QFormLayout()
        opacity_row = QHBoxLayout()
        self.layer_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.layer_opacity_slider.setRange(0, 100)
        self.layer_opacity = QSpinBox()
        self.layer_opacity.setRange(0, 100)
        self.layer_opacity.setSuffix(" %")
        self.layer_opacity.setFixedWidth(70)
        self.layer_opacity_slider.valueChanged.connect(self.layer_opacity.setValue)
        self.layer_opacity.valueChanged.connect(self.layer_opacity_slider.setValue)
        self.layer_opacity_slider.sliderReleased.connect(self.change_layer_opacity)
        self.layer_opacity.editingFinished.connect(self.change_layer_opacity)
        opacity_row.addWidget(self.layer_opacity_slider)
        opacity_row.addWidget(self.layer_opacity)
        lf.addRow("Opacity", opacity_row)
        self.blend = QComboBox()
        for mode in BLEND_MODES:
            self.blend.addItem(mode.replace("-", " ").capitalize(), mode)
        self.blend.currentIndexChanged.connect(lambda _: self.change_blend(self.blend.currentData()))
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
        self.view_bar = ViewOptionsBar([
            ("grid", "Grid", "Show the grid (Ctrl+'). Pixel lines appear from 1200 % zoom."),
            ("snap", "Snap", "Snap to guides, the canvas and other layers while moving. Hold Control to move freely."),
            ("rulers", "Rulers", "Show rulers. Drag from a ruler to place a guide.")], ["px", "mm", "cm", "in"])
        self.status = self.view_bar.status
        self.status.setText("Open an image, paste from the clipboard, or create a new canvas.")
        self.zoom = DropdownButton("100%", variant="ghost")
        self.zoom.setToolTip("Zoom level")
        zoom_menu = self.zoom.dropdown_menu()
        for percent in (25, 50, 100, 200, 400, 800, 1600):
            zoom_menu.addAction(f"{percent}%", lambda p=percent: self.canvas.set_zoom(p))
        zoom_menu.addSeparator()
        zoom_menu.addAction("Fit on screen", self.canvas.fit_image)
        zoom_menu.addAction("Fit width", self.canvas.fit_width)
        self.view_bar.add_leading(self.zoom)
        fit = tool_button("fit-page", "Fit image in window", size=26, icon_size=16)
        fit.clicked.connect(self.canvas.fit_image)
        self.view_bar.add_leading(fit)
        self.view_bar.add_menu_action("Fit width", self.canvas.fit_width)
        self.view_bar.add_menu_action("Clear guides", self.clear_guides)
        self.grid_box = self.view_bar.box("grid")
        self.snap_box = self.view_bar.box("snap")
        self.rulers_box = self.view_bar.box("rulers")
        self.rulers_box.setChecked(self.canvas.rulers)
        self.snap_box.setChecked(self.canvas.snap)
        self.units = self.view_bar.units
        self.view_bar.toggled.connect(self._set_view)
        self.view_bar.unitChanged.connect(lambda s: (setattr(self.canvas, "unit", s), self.canvas.viewport().update()))
        root.addWidget(self.view_bar)
        self.set_tool("move")

    def _menu_button(self, title, entries, icon_name=None, tooltip="", variant="secondary"):
        return menu_button(title, entries, icon_name, variant=variant, tooltip=tooltip)

    @staticmethod
    def _key_hint(keys):
        text = QKeySequence(keys).toString(QKeySequence.SequenceFormat.NativeText)
        return f"\t{text}" if text else ""

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
        # Undo, Redo, Cut, Copy, Copy Merged, Paste and Duplicate Layer are Edit-menu commands
        # owned by the main window (enabled in this workspace), so each key has one owner.
        for key, fn in [(QKeySequence.StandardKey.Save, self.save_project),
                        ("Ctrl+D", lambda: self.set_selection(None)), ("Ctrl+A", self.select_all),
                        ("Ctrl+Shift+I", self.invert_selection), ("Ctrl+T", lambda: self.set_tool("move")),
                        ("Ctrl+'", lambda: self._toggle_view("grid"))]:
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(fn)
            self.addAction(action)

    def _update_enabled(self):
        ready = self.doc is not None and self._job is None
        for widget in (self.side, self.image_menu, self.select_menu, self.layer_menu, self.save_button,
                       self.export_button, self.copy_button):
            widget.setEnabled(ready)
        self.undo_button.setEnabled(ready and self.doc.can_undo)
        self.redo_button.setEnabled(ready and self.doc.can_redo)
        layer = self.active_layer
        self.auto_background.setEnabled(layer is not None and not layer.locked)
        self.delete_layer_button.setEnabled(layer is not None and not layer.locked)
        self.copy_button.setEnabled(layer is not None)
        for widget in (self.layer_opacity, self.blend, self.locked):
            widget.setEnabled(layer is not None)
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
                self.canvas.end_preview()
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

    def mutate(self, description, fn, snapshot=True, clear_selection=False, after=None):
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
            if after is not None:
                after()
        if not self.run(description, work, done):
            self.canvas.end_preview()

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
                item = QListWidgetItem(QIcon(self._layer_thumbnail(layer)), layer.name)
                details = [f"{layer.image.width:,} × {layer.image.height:,} px"]
                if layer.mask is not None:
                    details.append("masked")
                if layer.original is not None:
                    details.append("transformed · original pixels kept")
                if layer.locked:
                    details.append("locked")
                if layer.blend != "normal" or layer.opacity < 1:
                    details.append(f"{layer.blend} {layer.opacity:.0%}")
                item.setToolTip(f"{layer.name}\n" + " · ".join(details))
                item.setData(Qt.ItemDataRole.UserRole, i)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
                item.setCheckState(Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked)
                self.layers.addItem(item)
            self.layers.setCurrentRow(len(self.doc.layers) - 1 - self.doc.active_index if self.doc.layers else -1)
            self._show_active_layer()
            self.tabs.setTabText(self.tabs.currentIndex(), self.state.name + (" •" if self.doc.dirty else ""))
            self.status.setText(f"{self.doc.width:,} × {self.doc.height:,} px · {self.doc.dpi:g} DPI · {len(self.doc.layers)} layers · "
                                f"Undo memory {self.doc.history_bytes / 1048576:.0f} MB")
        finally:
            self._refreshing = False
        self._update_enabled()

    def _layer_thumbnail(self, layer):
        thumb = layer.rendered().copy()
        thumb.thumbnail((48, 38))
        pixmap = QPixmap(48, 38)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        tile = 6
        for y in range(0, 38, tile):
            for x in range(0, 48, tile):
                p.fillRect(x, y, tile, tile, QColor("#ffffff") if (x // tile + y // tile) % 2 else QColor("#d9dce1"))
        p.drawPixmap((48 - thumb.width) // 2, (38 - thumb.height) // 2, QPixmap.fromImage(to_qimage(thumb)))
        if layer.locked or layer.mask is not None:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            badge = QRectF(31, 21, 16, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 22, 28, 200))
            p.drawRoundedRect(badge, 4, 4)
            icons.paint_icon(p, "lock" if layer.locked else "eraser", badge.adjusted(2, 2, -2, -2), QColor("#ffffff"))
        p.end()
        return pixmap

    def _show_active_layer(self):
        """Mirror the active layer in the inspector, the Move tool's fields and the canvas frame."""
        layer = self.doc.active_layer
        if layer is None:
            self.canvas.set_layer_frame(None, False)
            for field in self.transform_fields.values():
                field.setEnabled(False)
            self.placement_bar.hide()
            return
        refreshing, self._refreshing = self._refreshing, True
        try:
            self.layer_opacity.setValue(round(layer.opacity * 100))
            self.blend.setCurrentIndex(max(0, self.blend.findData(layer.blend)))
            self.locked.setChecked(layer.locked)
            cx, cy, w, h, angle = layer.frame
            values = {"x": cx - abs(w) / 2, "y": cy - abs(h) / 2, "w": abs(w), "h": abs(h), "angle": angle}
            for key, field in self.transform_fields.items():
                field.blockSignals(True)
                field.setValue(values[key])
                field.blockSignals(False)
                field.setEnabled(not layer.locked)
        finally:
            self._refreshing = refreshing
        self.canvas.set_layer_frame(layer.frame if layer.visible else None, not layer.locked)

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
        self.placement_bar.hide()
        self.tabs.blockSignals(True)
        self.states.append(TabState(doc, name, project_path))
        index = self.tabs.addTab(name)
        self.tabs.setCurrentIndex(index)
        self.tabs.blockSignals(False)
        self._clone_source = None
        self.canvas.reset_gesture()
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
        self._clone_source = self._preview_cache = None
        self.canvas.reset_gesture()
        self.placement_bar.hide()
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
        closing_current = self.state is state
        self.tabs.blockSignals(True)
        self.states.pop(index)
        self.tabs.removeTab(index)
        self.tabs.blockSignals(False)
        if self.doc:
            if closing_current:
                self._tab_changed(self.tabs.currentIndex())
        else:
            self._composite = self._clone_source = self._preview_cache = None
            self.canvas.clear_image()
            self.layers.clear()
            self.placement_bar.hide()
            self.status.setText("Open an image, paste from the clipboard, or create a new canvas.")
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
        click_fill = tool in ("magic", "bucket", "wand")
        brush = tool in ("pen", "highlighter", "eraser", "background", "restore", "quick", "clone", "heal", "text", "bubble",
                         "shape", "circle", "arrow", "wand", "magic", "bucket")
        self.brush_panel.setVisible(brush)
        form = self.brush_panel.layout()
        for row in (0, 1, 4):  # size, hardness and the brush preview mean nothing to a click tool
            form.setRowVisible(row, not click_fill)
        form.setRowVisible(2, tool != "wand")
        form.setRowVisible(3, click_fill or tool == "background")
        self.move_panel.setVisible(tool == "move")
        self.magic_panel.setVisible(click_fill)
        self.stop_edges.setVisible(tool == "magic")
        self.sample_all.setVisible(tool in ("bucket", "wand"))
        self.fill_new_layer.setVisible(tool == "bucket")
        self.color_button.setVisible(tool in ("pen", "highlighter", "text", "bubble", "shape", "circle", "arrow", "bucket"))
        self.selection_mode.setVisible(selection)
        self.crop_panel.setVisible(tool == "crop")
        hints = {"clone": f"{OPTION}-click a source, then paint. Applies to the active layer.",
                 "quick": "Paint inside an object. Local edge and colour analysis follows nearby boundaries.",
                 "eraser": "Paint the layer mask to hide pixels; Restore recovers them.",
                 "magic": "Click a colour to erase that area through the layer mask, such as a plain backdrop. "
                          "Tolerance takes in more shades; Stop at edges keeps similar-coloured subjects. "
                          "Nothing is deleted: Restore or Undo brings pixels back.",
                 "bucket": "Click to fill the colour area under the pointer with the foreground colour. "
                           "A transparent area fills too; an active selection limits the fill.",
                 "wand": "Click to select a colour area. Shift adds, " + OPTION + " subtracts.",
                 "background": "Paint to hide colours matching the stroke’s starting point.",
                 "restore": "Paint to bring back pixels hidden in the active layer’s mask.",
                 "heal": "Paint over dust or an unwanted object. Local inpainting reconstructs the area.",
                 "move": "Drag to move the layer; drag a handle to scale (Shift: free proportions, "
                         f"{OPTION}: from the centre) or the round knob to rotate. Arrow keys nudge. "
                         "Space or middle-mouse drag pans.",
                 "crop": "Drag a crop, adjust its corners, then press Enter. Escape cancels.",
                 "polygon": "Click vertices. Double-click or Enter closes the selection.",
                 "text": "Click to add text. Double-click its layer to edit it.",
                 "bubble": "Click to add a numbered comment. Comments remain editable in PowerPoint."}
        self.hint.setText(hints.get(tool, f"Work in source pixels. Shift adds to selections; {OPTION} subtracts."))
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
        color = QColorDialog.getColor(QColor(self.color), self, "Foreground colour")
        if color.isValid():
            self.color = color.name()
            self._update_color_button()
            self._brush_changed()

    def _update_color_button(self):
        swatch = QPixmap(28, 16)
        swatch.fill(Qt.GlobalColor.transparent)
        p = QPainter(swatch)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(0, 0, 0, 90), 1))
        p.setBrush(QColor(self.color))
        p.drawRoundedRect(QRectF(0.5, 0.5, 27, 15), 4, 4)
        p.end()
        self.color_button.setIcon(QIcon(swatch))
        self.color_button.setIconSize(QSize(28, 16))
        self.color_button.setText(f"Foreground colour  {self.color.upper()}")

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
        doc, layer = self.doc, self.active_layer
        if layer is None:
            self.status.setText("Add, paste, or import a layer to edit it.")
            return
        mode = self._selection_mode(mods)
        radius, hardness, opacity = self.size.value() / 2, self.hardness.value() / 100, self.opacity.value() / 100
        tolerance = self.tolerance.value()
        if tool == "clone_source":
            self._clone_source = (points[0][0] - layer.x, points[0][1] - layer.y)
            self.status.setText("Clone source sampled. Paint to clone onto the active layer.")
            return
        if tool in ("rect", "ellipse", "lasso", "polygon", "wand", "quick", "color"):
            old = self.selection
            image = self._composite if tool != "wand" or self.sample_all.isChecked() else self._layer_on_canvas(layer)
            contiguous, antialias = self.contiguous.isChecked(), self.antialias.isChecked()
            def select():
                if tool in ("rect", "ellipse"):
                    a, b = points[0], points[-1]
                    new = T.rectangle_selection(doc.size, (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), tool == "ellipse")
                elif tool in ("lasso", "polygon"):
                    new = T.polygon_selection(doc.size, points)
                elif tool == "quick":
                    new = T.quick_selection(image, points, radius=radius)
                else:
                    new = T.color_selection(image, points[0], tolerance, contiguous=tool == "wand" and contiguous,
                                            antialias=antialias)
                return T.combine_selection(old, new, mode)
            self.run("Updating selection", select, self.set_selection)
            return
        if tool == "bucket":
            self._bucket(points[0], tolerance, opacity)
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
            contiguous, antialias, edges = self.contiguous.isChecked(), self.antialias.isChecked(), self.stop_edges.isChecked()
            stats = {}
            def erase(d):
                active = d.active_layer
                base = active.mask or Image.new("L", active.image.size, 255)
                changed = T.magic_erase(active.image, (x, y), tolerance, contiguous, antialias, opacity, mask=base,
                                        stop_at_edges=edges)
                changed = Image.composite(changed, base, selected) if selected else changed
                alpha = active.image.getchannel("A")
                before = ImageChops.multiply(alpha, base).histogram()
                after = ImageChops.multiply(alpha, changed).histogram()
                visible = sum(before[1:])
                stats["removed"] = (visible - sum(after[1:])) / visible if visible else 0
                active.mask = changed
            def report():
                removed = stats.get("removed", 0)
                if removed >= 0.35 and not contiguous:
                    self.status.setText(f"Magic Eraser hid {removed:.0%} of the layer: that colour everywhere, since Contiguous "
                                        f"is off. {self._undo_hint()} undoes it; Restore brings back parts.")
                    self.owner.toast.show_message(f"Hid {removed:.0%} of the layer · {self._undo_hint()} to undo", "warning", kind="warning")
                elif removed:
                    self.status.setText(f"Magic Eraser hid {removed:.1%} of the layer · nothing deleted: Restore or "
                                        f"{self._undo_hint()} brings it back")
            self.mutate("Magic Eraser", erase, after=report)
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
        elif action == "locked":
            self.status.setText("The active layer is locked. Unlock it in the Layers panel to move or transform it.")
        elif action == "delete":
            if self.selection is None and self.canvas.tool == "move" and self.active_layer is not None:
                self.remove_layer()
            else:
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
        self._show_active_layer()
        self._update_enabled()
        self._clone_source = None

    def _layer_changed(self, item):
        if not self._refreshing and self.doc:
            index = item.data(Qt.ItemDataRole.UserRole)
            visible = item.checkState() == Qt.CheckState.Checked
            self.mutate("Layer visibility", lambda d: setattr(d.layers[index], "visible", visible))

    def change_layer_opacity(self):
        if not self._refreshing and self.active_layer is not None:
            value = self.layer_opacity.value() / 100
            self.mutate("Layer opacity", lambda d: setattr(d.active_layer, "opacity", value))

    def change_blend(self, value):
        if not self._refreshing and self.active_layer is not None and value in BLEND_MODES:
            self.mutate("Layer blend", lambda d: setattr(d.active_layer, "blend", value))

    def change_locked(self, value):
        if not self._refreshing and self.active_layer is not None:
            self.mutate("Layer lock", lambda d: setattr(d.active_layer, "locked", value))

    def add_layer(self):
        if self.doc:
            self.mutate("Add layer", lambda d: d.insert_layer(ImageLayer("Transparent layer", Image.new("RGBA", d.size))), snapshot=False)

    def import_layer(self):
        if not self.doc:
            self.open_dialog()
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Import images as layers", "", LAYER_FILTER)
        if paths:
            self.place_files(paths)

    def duplicate_layer(self):
        """⌘J: a copy of the selected pixels as a new layer, or of the whole layer without a selection."""
        if self.active_layer is None:
            return
        selection = self.selection
        if selection is None:
            self.mutate("Duplicate layer", lambda d: d.duplicate_layer(d.active_index), snapshot=False)
            return
        def apply(d):
            copied = d.copy_layer(d.active_index, selection)
            if copied is None:
                raise ValueError("The selection holds no visible pixels of the active layer.")
            copied.name = d.active_layer.name + " (copy)"
            d.insert_layer(copied, label="Layer via copy")
        self.mutate("Layer via copy", apply, snapshot=False)

    def layer_via_cut(self):
        if self.active_layer is None:
            return
        selection = self.selection
        if selection is None:
            self.status.setText("Select pixels first: Layer via Cut moves the selected pixels to a new layer.")
            return
        def apply(d):
            index = d.active_index
            copied = d.copy_layer(index, selection)
            if copied is None:
                raise ValueError("The selection holds no visible pixels of the active layer.")
            with d.batch("Layer via cut"):
                d.clear_selection_pixels(index, selection, "Layer via cut")
                copied.name = d.layers[index].name + " (cut)"
                d.insert_layer(copied, index + 1, "Layer via cut")
        self.mutate("Layer via cut", apply, snapshot=False)

    def remove_layer(self):
        if self.active_layer is not None:
            self.mutate("Delete layer", lambda d: d.remove_layer(d.active_index), snapshot=False)

    def merge_down(self):
        if self.active_layer is None or self.doc.active_index <= 0:
            self.status.setText("There is no layer below to merge into.")
            return
        def apply(d):
            i = d.active_index
            upper, lower = d.layers[i], d.layers[i - 1]
            if upper.locked or lower.locked:
                raise ValueError("Unlock both layers before merging them.")
            left, top = min(upper.x, lower.x), min(upper.y, lower.y)
            right = max(upper.x + upper.image.width, lower.x + lower.image.width)
            bottom = max(upper.y + upper.image.height, lower.y + lower.image.height)
            part = ImageDocument(right - left, bottom - top, [
                ImageLayer("l", lower.image, lower.mask, True, lower.opacity, lower.blend, x=lower.x - left, y=lower.y - top),
                ImageLayer("u", upper.image, upper.mask, upper.visible, upper.opacity, upper.blend, x=upper.x - left, y=upper.y - top)])
            merged = ImageLayer(lower.name, part.composite(), x=left, y=top, visible=lower.visible)
            d.snapshot("Merge down")
            d.layers[i - 1:i + 1] = [merged]
            d.active_index = i - 1
            d.touch()
        self.mutate("Merge down", apply, snapshot=False)

    def reorder_layer(self, delta):
        if self.active_layer is not None:
            self.mutate("Reorder layer", lambda d: d.move_layer(d.active_index, max(0, min(len(d.layers) - 1, d.active_index + delta))), snapshot=False)

    def rename_layer(self):
        if self.active_layer is None:
            return
        name, ok = QInputDialog.getText(self, "Rename layer", "Name", text=self.doc.active_layer.name)
        if ok and name:
            self.mutate("Rename layer", lambda d: setattr(d.active_layer, "name", name))

    def edit_layer(self):
        if self.active_layer is None:
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
        if not self._active_editable():
            return
        percent, ok = QInputDialog.getDouble(self, "Scale layer", "Percent", 100, 1, 1000, 1)
        if ok and percent != 100:
            from .transform import frame_point
            layer = self.doc.active_layer
            corner = frame_point(layer.frame, -0.5, -0.5)
            matrix = affine_about(corner, percent / 100, percent / 100, axis_degrees=layer.frame[4])
            index = self.doc.active_index
            self.mutate("Scale layer", lambda d: d.transform_layer(index, matrix), snapshot=False,
                        after=self._check_placement)

    def add_mask(self):
        if self.active_layer is not None and not self.active_layer.locked:
            self.mutate("Add mask", lambda d: setattr(d.active_layer, "mask", Image.new("L", d.active_layer.image.size, 255)))

    def remove_mask(self):
        if self.active_layer is not None and not self.active_layer.locked:
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
        if self.selection is None or self.active_layer is None:
            return None
        l = self.active_layer
        return self.selection.crop((l.x, l.y, l.x + l.image.width, l.y + l.image.height))

    def selection_to_mask(self):
        mask = self._layer_selection()
        if mask is not None and not self.doc.active_layer.locked:
            self.mutate("Selection to mask", lambda d: setattr(d.active_layer, "mask", mask))

    def mask_to_selection(self):
        if self.active_layer is not None and self.active_layer.mask:
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
        if self.active_layer is None:
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
            return model.remove_background_mask(image, progress=self._job.signals.progress.emit, refine=True)
        if remove:
            clean = self.clean_edges.isChecked()
            self.mutate("Removing background", lambda d: self._apply_subject_mask(d, infer(), clean), snapshot=False)
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
        """Paste the clipboard image as a new document (kept for the old menu wording)."""
        self.paste(as_document=True)

    # ------------------------------------------------------------- clipboard
    def _undo_hint(self):
        return QKeySequence(QKeySequence.StandardKey.Undo).toString(QKeySequence.SequenceFormat.NativeText) or "Undo"

    def _clipboard_options(self):
        options = dict(self.owner.settings.get("image_clipboard", {}))
        options["region"] = "image"
        return options

    def copy(self, merged=False, cut=False):
        """Copy the active layer (or its selected pixels); ``merged`` copies the visible image.

        Other applications receive the visible pixels in the remembered clipboard format;
        pasting back into the Image Editor uses the private layer copy, which keeps the
        mask, opacity, blend mode and position.
        """
        if not self.doc or self._job:
            return False
        from ...core.image_clipboard import prepare_clipboard, write_clipboard
        doc, selection = self.doc, self.selection
        index = doc.active_index
        options = self._clipboard_options()
        if self.active_layer is None and (cut or not merged):
            return False
        if cut and doc.active_layer.locked:
            self.status.setText("Unlock the active layer before cutting from it.")
            return False
        if merged:
            image = self._composite.copy()
            if selection is not None:
                image.putalpha(ImageChops.multiply(image.getchannel("A"), selection))
            bounds = image.getbbox()
            if bounds is None:
                self.status.setText("Nothing visible to copy.")
                return False
            layer = ImageLayer("Merged copy", image.crop(bounds), x=bounds[0], y=bounds[1])
        else:
            layer = doc.copy_layer(index, selection)
            if layer is None:
                self.status.setText("The selection holds no visible pixels of the active layer.")
                return False
        what = "merged image" if merged else "selection" if selection is not None else f"layer “{layer.name}”"
        width, height, dpi = doc.width, doc.height, doc.dpi
        def prepare():
            visible = layer.image.copy()
            if layer.mask is not None:
                visible.putalpha(ImageChops.multiply(visible.getchannel("A"), layer.mask))
            bounds = visible.getbbox() or (0, 0, 1, 1)
            payload = prepare_clipboard(visible.crop(bounds), options)
            private = ImageDocument(width, height, [layer], dpi=dpi).to_bytes()
            return payload, private
        def done(result):
            payload, private = result
            try:
                write_clipboard(payload, int(self.window().winId()), layers=private)
            except Exception as exc:
                QMessageBox.warning(self, "Clipboard", str(exc))
                return
            if cut:
                if selection is not None:
                    self.mutate("Cut", lambda d: d.clear_selection_pixels(index, selection, "Cut"), snapshot=False)
                else:
                    self.mutate("Cut", lambda d: d.remove_layer(index), snapshot=False)
            verb = "Cut" if cut else "Copied"
            self.status.setText(f"{verb} {what} · {payload.format} {payload.image.width} × {payload.image.height} px · "
                                f"paste with {QKeySequence(QKeySequence.StandardKey.Paste).toString(QKeySequence.SequenceFormat.NativeText)}")
        return self.run("Copying", prepare, done)

    def cut(self):
        return self.copy(cut=True)

    def paste(self, as_document=False):
        """Paste clipboard layers or an image as new layers of this document (or a new document)."""
        if self._job:
            return
        from ...core.image_clipboard import read_clipboard
        try:
            content = read_clipboard()
        except Exception as exc:
            QMessageBox.warning(self, "Paste", f"The clipboard image could not be read: {exc}")
            return
        if content is None:
            self.status.setText("The clipboard has no image to paste. Copy an image or image file first.")
            return
        if content.layers:
            source = ImageDocument.from_bytes(content.layers)
            layers = source.layers
            if not layers:
                self.status.setText("The clipboard project has no layers to paste.")
                return
            same_canvas = self.doc is not None and source.size == self.doc.size
        else:
            source = None
            layers = [ImageLayer(content.name, content.image)]
            same_canvas = False
        if self.doc is None or as_document:
            if source is not None:
                # A new document the size of the copied content, like Photoshop's New from Clipboard.
                left = min(l.x for l in layers)
                top = min(l.y for l in layers)
                right = max(l.x + l.image.width for l in layers)
                bottom = max(l.y + l.image.height for l in layers)
                for layer in layers:
                    layer.x -= left
                    layer.y -= top
                doc = ImageDocument(right - left, bottom - top, list(layers), dpi=source.dpi, active_index=len(layers) - 1)
            else:
                doc = ImageDocument.from_image(content.image, content.name)
            doc.touch()
            self.add_document(doc, content.name if source is None else "Pasted layers")
            self.status.setText(f"Pasted as a new document · {doc.width:,} × {doc.height:,} px · "
                                "paste again or drop images to add layers")
            return
        self._place_layers(layers, keep_position=same_canvas, label="Paste")

    def _view_center(self):
        """Canvas point at the centre of the visible area, kept inside the canvas."""
        point = self.canvas.mapToScene(self.canvas.viewport().rect().center())
        return (min(max(point.x(), 0), self.doc.width), min(max(point.y(), 0), self.doc.height))

    def _place_layers(self, layers, keep_position=False, at=None, label="Place image"):
        """Add layers above the active one, centred on ``at`` (default: the visible canvas centre)."""
        if not layers:
            return
        center = at or self._view_center()
        placed = []
        if not keep_position:
            left = min(l.x for l in layers)
            top = min(l.y for l in layers)
            right = max(l.x + l.image.width for l in layers)
            bottom = max(l.y + l.image.height for l in layers)
            dx = round(center[0] - (left + right) / 2)
            dy = round(center[1] - (top + bottom) / 2)
            for i, layer in enumerate(layers):
                placed.append((layer, layer.x + dx, layer.y + dy))
        else:
            placed = [(layer, layer.x, layer.y) for layer in layers]
        def apply(d):
            with d.batch(label):
                for layer, x, y in placed:
                    layer.x, layer.y = x, y
                    layer.locked = False
                    d.insert_layer(layer, label=label)
        def after():
            self.set_tool("move")
            self._check_placement()
            names = ", ".join(l.name for l, *_ in placed[:3]) + ("…" if len(placed) > 3 else "")
            self.status.setText(f"{label}: {names} · new layer above the active one · drag to move, handles to scale "
                                f"and rotate · {self._undo_hint()} undoes")
        self.mutate(label, apply, snapshot=False, after=after)

    def _check_placement(self):
        layer = self.doc.active_layer if self.doc else None
        if layer is None:
            self.placement_bar.hide()
            return
        cx, cy, w, h, _ = layer.frame
        if abs(w) > self.doc.width + 0.5 or abs(h) > self.doc.height + 0.5:
            self.placement_bar.text.setText(
                f"“{layer.name}” is {abs(w):,.0f} × {abs(h):,.0f} px, larger than the {self.doc.width:,} × {self.doc.height:,} px "
                "canvas. Every original pixel is kept.")
            self.placement_bar.show()
        else:
            self.placement_bar.hide()

    def place_files(self, paths, at=None):
        """Import image files as new layers of the open document (decoded off the GUI thread)."""
        paths = [str(p) for p in paths if Path(p).suffix.lower() in IMAGE_SUFFIXES - {".engimg"}]
        if not paths or not self.doc:
            return
        def load():
            layers = []
            for path in paths:
                image, _metadata, _dpi = _decode_image(path)
                layers.append(ImageLayer(Path(path).stem, image))
            return layers
        def done(layers):
            # Several images cascade slightly so each stays visible.
            center = at or self._view_center()
            for i, layer in enumerate(layers):
                layer.x = round(center[0] - layer.image.width / 2 + 24 * i)
                layer.y = round(center[1] - layer.image.height / 2 + 24 * i)
            self._place_layers(layers, keep_position=True, label="Place image" if len(layers) == 1 else "Place images")
        self.run("Placing image" if len(paths) == 1 else f"Placing {len(paths)} images", load, done)

    # ------------------------------------------------------ layer transforms
    def _active_editable(self):
        if self.active_layer is None:
            return False
        if self.doc.active_layer.locked:
            self.status.setText("Unlock the active layer first.")
            return False
        return True

    def _transform_finished(self, kind, payload):
        if not self._active_editable():
            self.canvas.end_preview()
            return
        index = self.doc.active_index
        if kind == "move":
            dx, dy = payload
            self.mutate("Move layer", lambda d: d.offset_layer(index, dx, dy), snapshot=False,
                        after=self._check_placement)
        else:
            self.mutate("Transform layer", lambda d: d.transform_layer(index, payload), snapshot=False,
                        after=self._check_placement)

    def _nudge(self, dx, dy):
        if self._job is None and self._active_editable():
            index = self.doc.active_index
            self.mutate("Nudge layer", lambda d: d.offset_layer(index, dx, dy), snapshot=False)

    def _pick_layer(self, x, y):
        """Auto-select: make the topmost visible layer with a pixel at (x, y) active."""
        doc = self.doc
        if doc is None:
            return
        for index in reversed(range(len(doc.layers))):
            layer = doc.layers[index]
            lx, ly = int(math.floor(x)) - layer.x, int(math.floor(y)) - layer.y
            if not layer.visible or not (0 <= lx < layer.image.width and 0 <= ly < layer.image.height):
                continue
            alpha = layer.image.getpixel((lx, ly))[3]
            if layer.mask is not None:
                alpha = alpha * layer.mask.getpixel((lx, ly)) // 255
            if alpha > 10:
                if index != doc.active_index:
                    doc.active_index = index
                    self._refreshing = True
                    self.layers.setCurrentRow(len(doc.layers) - 1 - index)
                    self._refreshing = False
                    self._show_active_layer()
                return

    def _layer_preview(self):
        doc = self.doc
        if self.active_layer is None:
            return None
        index = doc.active_index
        key = (id(doc), doc.revision, index, tuple((id(l.image), id(l.mask), l.visible, l.opacity, l.x, l.y) for l in doc.layers))
        if self._preview_cache is None or self._preview_cache[0] != key:
            layer = doc.layers[index]
            below = to_qimage(doc.composite(0, index)) if index > 0 else None
            above = to_qimage(doc.composite(index + 1)) if index + 1 < len(doc.layers) else None
            self._preview_cache = (key, (below, to_qimage(layer.rendered()) if layer.visible else None, (layer.x, layer.y), above))
        below, active, position, above = self._preview_cache[1]
        if active is None:
            return None
        return below, active, position, above

    def _snap_targets(self):
        doc = self.doc
        xs, ys = [0, doc.width / 2, doc.width], [0, doc.height / 2, doc.height]
        for axis, value in doc.metadata.get("guides", []):
            (xs if axis == "x" else ys).append(value)
        for i, layer in enumerate(doc.layers):
            if i == doc.active_index or not layer.visible:
                continue
            from .transform import bounds
            left, top, right, bottom = bounds(layer.frame)
            xs += [left, (left + right) / 2, right]
            ys += [top, (top + bottom) / 2, bottom]
        return xs, ys

    def _transform_field_changed(self, key):
        if self._refreshing or not self._active_editable():
            return
        layer = self.doc.active_layer
        cx, cy, w, h, angle = layer.frame
        fields = {k: f.value() for k, f in self.transform_fields.items()}
        index = self.doc.active_index
        if key in ("x", "y"):
            dx = fields["x"] - (cx - abs(w) / 2)
            dy = fields["y"] - (cy - abs(h) / 2)
            if round(dx) or round(dy):
                self.mutate("Move layer", lambda d: d.offset_layer(index, dx, dy), snapshot=False)
            return
        if key == "angle":
            matrix = affine_about((cx, cy), degrees=fields["angle"] - angle)
        else:
            sx, sy = fields["w"] / abs(w), fields["h"] / abs(h)
            if self.keep_aspect.isChecked():
                sx = sy = sx if key == "w" else sy
            # Scale along the layer's own axes, keeping its top-left corner where it is.
            from .transform import frame_point
            corner = frame_point(layer.frame, -0.5, -0.5)
            matrix = affine_about(corner, sx, sy, axis_degrees=angle)
        if matrix != (1.0, 0.0, 0.0, 0.0, 1.0, 0.0):
            self.mutate("Transform layer", lambda d: d.transform_layer(index, matrix), snapshot=False,
                        after=self._check_placement)

    def transform_dialog(self):
        if not self._active_editable():
            return
        self.set_tool("move")
        self.transform_fields["w"].setFocus()
        self.transform_fields["w"].selectAll()
        self.status.setText("Type a size or angle and press Return, or drag the handles on the canvas.")

    def fit_layer(self):
        if self._active_editable():
            index = self.doc.active_index
            self.mutate("Fit layer to canvas", lambda d: d.fit_layer_to_canvas(index), snapshot=False,
                        after=self._check_placement)

    def center_layer(self):
        if self._active_editable():
            index = self.doc.active_index
            self.mutate("Center layer", lambda d: d.center_layer(index), snapshot=False)

    def reset_layer_transform(self):
        if not self._active_editable():
            return
        if self.doc.active_layer.original is None:
            self.status.setText("This layer has not been scaled or rotated since its pixels were last edited.")
            return
        index = self.doc.active_index
        self.mutate("Reset transform", lambda d: d.reset_layer_transform(index), snapshot=False,
                    after=self._check_placement)

    def rotate_layer(self, degrees):
        if self._active_editable():
            index = self.doc.active_index
            cx, cy, *_ = self.doc.active_layer.frame
            self.mutate("Rotate layer", lambda d: d.transform_layer(index, affine_about((cx, cy), degrees=degrees)),
                        snapshot=False, after=self._check_placement)

    def flip_layer(self, horizontal=True):
        if self._active_editable():
            index = self.doc.active_index
            cx, cy, w, h, angle = self.doc.active_layer.frame
            matrix = affine_about((cx, cy), -1 if horizontal else 1, 1 if horizontal else -1, axis_degrees=angle)
            self.mutate("Flip layer", lambda d: d.transform_layer(index, matrix), snapshot=False)

    def reveal_all(self):
        if self.doc:
            self.mutate("Reveal all", lambda d: d.reveal_all(), snapshot=False, clear_selection=True)

    def _layer_rows_moved(self, _parent, start, _end, _destination, row):
        if self._refreshing or not self.doc:
            return
        count = len(self.doc.layers)
        target_row = row - 1 if row > start else row
        old_index, new_index = count - 1 - start, count - 1 - target_row
        if old_index != new_index:
            QTimer.singleShot(0, lambda: self.mutate("Reorder layer", lambda d: d.move_layer(old_index, new_index), snapshot=False))

    # ------------------------------------------------------------ Paint Bucket
    def _layer_on_canvas(self, layer):
        """A layer's visible pixels positioned on a canvas-sized transparent image."""
        canvas = Image.new("RGBA", self.doc.size)
        canvas.paste(layer.rendered(), (layer.x, layer.y))
        return canvas

    def _bucket(self, point, tolerance, opacity):
        doc, layer = self.doc, self.doc.active_layer
        color = self.color
        contiguous, antialias = self.contiguous.isChecked(), self.antialias.isChecked()
        sample_all = self.sample_all.isChecked()
        annotation = bool(layer.properties.get("tool"))
        new_layer = self.fill_new_layer.isChecked() or annotation
        if not new_layer and layer.locked:
            self.status.setText("Unlock the active layer to fill it, or turn on Fill on a new layer.")
            return
        selection = self.selection
        composite = self._composite
        if new_layer:
            sample = composite if sample_all else self._layer_on_canvas(layer)
            def fill(d):
                base = Image.new("RGBA", d.size)
                filled, alpha = T.bucket_fill(base, point, color, tolerance, contiguous, antialias, opacity,
                                              sample=sample, selection=selection)
                bounds = alpha.getbbox()
                if bounds is None:
                    raise ValueError("Nothing to fill there.")
                d.insert_layer(ImageLayer("Fill", filled.crop(bounds), x=bounds[0], y=bounds[1]), label="Paint Bucket")
            self.mutate("Paint Bucket", fill, snapshot=False,
                        after=lambda: annotation and self.status.setText("Filled on a new layer: annotation layers keep their own drawing."))
            return
        x, y = point[0] - layer.x, point[1] - layer.y
        if not (0 <= x < layer.image.width and 0 <= y < layer.image.height):
            self.status.setText("Click inside the active layer, or turn on Fill on a new layer to fill anywhere.")
            return
        local_sample = composite.crop((layer.x, layer.y, layer.x + layer.image.width, layer.y + layer.image.height)) if sample_all else None
        local_selection = doc.layer_selection(doc.active_index, selection)
        def fill(d):
            active = d.active_layer
            result, alpha = T.bucket_fill(active.image, (x, y), color, tolerance, contiguous, antialias, opacity,
                                          sample=local_sample, selection=local_selection, mask=active.mask)
            if alpha.getbbox() is None:
                raise ValueError("Nothing to fill there: the area is outside the selection.")
            active.image = result
        self.mutate("Paint Bucket", fill)

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

    def send_to_iwork(self, app):
        if self.doc is None or self._job is not None:
            return
        from ..queue_panel import QueueEntry

        image, selection = self._composite, self.selection
        source = self.doc.source_path or self.state.name

        def prepare():
            if selection is None:
                return to_qimage(image)
            selected = image.copy()
            selected.putalpha(ImageChops.multiply(image.getchannel("A"), selection))
            bounds = selected.getbbox()
            if bounds is None:
                raise ValueError("The selection contains no visible image.")
            return to_qimage(selected.crop(bounds))

        self.run("Preparing image", prepare,
                 lambda result: self.owner.export_iwork(app, [QueueEntry(source, 0, marked_image=result)]))

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
        if self.active_layer is None:
            return
        layer = self.doc.active_layer
        if layer.locked:
            self.status.setText("Unlock the active layer to remove its background.")
            return
        image = layer.image
        clean = self.clean_edges.isChecked()
        self.mutate("Remove background", lambda d: self._apply_subject_mask(d, T.remove_background(image), clean), snapshot=False)

    @staticmethod
    def _apply_subject_mask(d, found, clean):
        """Hide the background through the layer mask, or (Clean edge colours) on a new layer."""
        index = d.active_index
        active = d.layers[index]
        mask = ImageChops.multiply(active.mask, found) if active.mask else found
        with d.batch("Remove background"):
            if clean:
                subject = ImageLayer(active.name + " (cut out)", T.decontaminate(active.image, mask),
                                     x=active.x, y=active.y, opacity=active.opacity, blend=active.blend)
                d.insert_layer(subject, index + 1, "Remove background")
                d.layers[index].visible = False
            else:
                active.mask = mask

    @staticmethod
    def _dropped(event):
        mime = event.mimeData()
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in IMAGE_SUFFIXES] if mime.hasUrls() else []

    def _drop_action(self, event):
        """("layer" | "document", overlay rect, title, detail, icon) for a drag at this position."""
        paths = self._dropped(event)
        pos = event.position().toPoint()
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        over = self.childAt(pos)
        on_tabs = over is not None and (over is self.tabs or self.tabs.isAncestorOf(over))
        on_layers = over is not None and (over is self.layers or self.layers.isAncestorOf(over))
        projects = any(Path(p).suffix.lower() == ".engimg" for p in paths)
        count = len(paths)
        noun = "image" if count == 1 else f"{count} images"
        canvas_rect = QRect(self.canvas.mapTo(self, QPoint(0, 0)), self.canvas.size())
        if self.doc is not None and not (alt or on_tabs or projects):
            target = QRect(self.layers.mapTo(self, QPoint(0, 0)), self.layers.size()) if on_layers else canvas_rect
            title = "Place as New Layer" if count == 1 else f"Place {count} Images as Layers"
            return "layer", target, title, f"Hold {OPTION} to open as a new document instead", "plus"
        rect = canvas_rect
        if on_tabs:
            rect = QRect(self.tabs.mapTo(self, QPoint(0, 0)), self.tabs.size()).united(canvas_rect)
        title = "Open as New Document" if count == 1 and not projects else "Open as New Documents" if not projects else "Open Project"
        detail = f"Release {OPTION} to place the {noun} as a layer" if self.doc is not None and alt and not projects else ""
        return "document", rect, title, detail, "folder-open"

    def dragEnterEvent(self, event):
        if self._dropped(event):
            event.acceptProposedAction()
            self._show_drop_hint(event)

    def dragMoveEvent(self, event):
        if self._dropped(event):
            event.acceptProposedAction()
            self._show_drop_hint(event)

    def dragLeaveEvent(self, event):
        self._drop_overlay.hide()
        super().dragLeaveEvent(event)

    def _show_drop_hint(self, event):
        _action, rect, title, detail, icon_name = self._drop_action(event)
        self._drop_overlay.show_hint(rect, title, detail, icon_name)

    def dropEvent(self, event):
        self._drop_overlay.hide()
        paths = self._dropped(event)
        if not paths:
            return
        event.acceptProposedAction()
        action, *_ = self._drop_action(event)
        if action == "layer":
            point = self.canvas.mapToScene(self.canvas.mapFrom(self, event.position().toPoint()))
            inside = self.canvas.geometry().contains(self.canvas.parentWidget().mapFrom(self, event.position().toPoint()))
            at = (min(max(point.x(), 0), self.doc.width), min(max(point.y(), 0), self.doc.height)) if inside else None
            self.place_files(paths, at)
            return
        self._opening.extend(paths[1:])
        self.open_file(paths[0])

    def vectorize(self):
        if self._composite is not None:
            self.owner.open_vector_image(self._composite)

    def _toggle_view(self, name):
        self.view_bar.box(name).toggle()

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
