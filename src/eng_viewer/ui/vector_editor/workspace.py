"""Shared macOS/Windows vector canvas, genuine editable geometry and local tracing."""
from __future__ import annotations

import copy
import math
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QObject, QPointF, QRect, QRectF, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGraphicsEllipseItem, QGraphicsItem, QGraphicsPathItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from ...core.vector_document import VectorDocument, VectorObject, commands_from_path, import_dxf, import_svg
from ...core.vector_trace import simplify_object, trace_image
from .. import icons
from ..rulers import RULER, paint_grid, paint_rulers
from ..theme import ThemeManager, qc
from ..widgets import label, primary_button, secondary_button, tool_button


# Vector files open as documents; raster images open as a tracing reference.
VECTOR_DROP_SUFFIXES = {".engvec", ".svg", ".dxf", ".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".heic", ".heif", ".avif"}


class _Bridge(QObject):
    done = Signal(int, object, str)


class _Job(QRunnable):
    def __init__(self, generation, function, bridge):
        super().__init__(); self.generation, self.function, self.bridge = generation, function, bridge

    def run(self):
        try: result, error = self.function(), ""
        except Exception as exc: result, error = None, str(exc)
        try: self.bridge.done.emit(self.generation, result, error)
        except RuntimeError: pass  # Workspace closed while a local operation finished.


class _Node(QGraphicsEllipseItem):
    def __init__(self, workspace, obj, command_index, coordinate_index):
        super().__init__(-3.5, -3.5, 7, 7)
        self.workspace, self.obj = workspace, obj
        self.command_index, self.coordinate_index = command_index, coordinate_index
        command = obj.commands[command_index]
        self.setPos(command[coordinate_index], command[coordinate_index + 1])
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setBrush(QColor("#ffffff")); self.setPen(QPen(QColor("#0A7CFF"), 1.5)); self.setZValue(100000)
        self.setToolTip("Drag node or Bézier handle; Delete removes an anchor node")
        self.setData(0, "node")

    def mousePressEvent(self, event):
        self.workspace.document.snapshot()
        self.workspace.active_node = (self.obj.id, self.command_index, self.coordinate_index)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        point = self.pos()
        if self.workspace.snap.isChecked():
            step = self.workspace.grid_step.value()
            point = QPointF(round(point.x() / step) * step, round(point.y() / step) * step); self.setPos(point)
        command = self.obj.commands[self.command_index]
        command[self.coordinate_index:self.coordinate_index + 2] = [point.x(), point.y()]
        self.obj.text = None
        self.workspace.items[self.obj.id].setPath(self.obj.path())
        self.workspace.status.setText("Node moved · coordinates in mm · Undo available")


class VectorCanvas(QGraphicsView):
    def __init__(self, workspace):
        self.workspace = workspace
        super().__init__(workspace)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.start = None; self.ghost = None; self.pen_points = []; self._pan = None
        self._guide_drag = None
        self.setMouseTracking(True)

    def scene_point(self, event):
        point = self.mapToScene(event.position().toPoint())
        if self.workspace.snap.isChecked():
            step = self.workspace.grid_step.value()
            original = QPointF(point)
            point = QPointF(round(point.x() / step) * step, round(point.y() / step) * step)
            for axis, value in self.workspace.document.guides:
                if axis == "x" and abs(original.x() - value) * self.transform().m11() < 8: point.setX(value)
                if axis == "y" and abs(original.y() - value) * self.transform().m11() < 8: point.setY(value)
        return point

    def wheelEvent(self, event):
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier) or event.angleDelta().y():
            amount = 1.15 ** (event.angleDelta().y() / 120)
            scale = self.transform().m11()
            self.scale(max(.02 / scale, min(500 / scale, amount)), max(.02 / scale, min(500 / scale, amount)))
            self.workspace.update_status(); event.accept()
        else: super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan = event.position(); self.setCursor(Qt.CursorShape.ClosedHandCursor); return
        if event.button() == Qt.MouseButton.RightButton and self.pen_points:
            self.finish_pen(close=True); return
        if event.button() != Qt.MouseButton.LeftButton: return super().mousePressEvent(event)
        if self.workspace.rulers.isChecked() and (event.position().x() < RULER or event.position().y() < RULER):
            axis = "x" if event.position().x() < RULER else "y"
            point = self.mapToScene(event.position().toPoint())
            self.workspace.document.snapshot()
            self.workspace.document.guides.append([axis, point.x() if axis == "x" else point.y()])
            self._guide_drag = len(self.workspace.document.guides) - 1
            return
        mode = self.workspace.tool
        if mode in {"select", "nodes"}: return super().mousePressEvent(event)
        self.start = self.scene_point(event)
        if mode == "text": self.workspace.add_text(self.start); self.start = None; return
        if mode == "pen":
            self.pen_points.append((QPointF(self.start), QPointF(self.start)))
            self.update_pen(); return
        self.ghost = QGraphicsPathItem(); self.ghost.setPen(QPen(QColor("#0A7CFF"), .3)); self.ghost.setZValue(99999)
        self.scene().addItem(self.ghost)
        if mode == "freehand": self.pen_points = [(QPointF(self.start), QPointF(self.start))]

    def mouseMoveEvent(self, event):
        if self.workspace.rulers.isChecked():  # pointer markers on both rulers
            self.viewport().update(QRect(0, 0, self.viewport().width(), RULER)); self.viewport().update(QRect(0, 0, RULER, self.viewport().height()))
        if self._guide_drag is not None:
            point = self.mapToScene(event.position().toPoint())
            guide = self.workspace.document.guides[self._guide_drag]
            guide[1] = point.x() if guide[0] == "x" else point.y()
            self.viewport().update()
            return
        if self._pan is not None:
            delta = event.position() - self._pan; self._pan = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(delta.y())); return
        if self.start is None: return super().mouseMoveEvent(event)
        point = self.scene_point(event); mode = self.workspace.tool
        if mode == "pen":
            if self.pen_points: self.pen_points[-1] = (self.pen_points[-1][0], point)
            self.update_pen(); return
        path = QPainterPath(); rect = QRectF(self.start, point).normalized()
        if mode == "rectangle": path.addRect(rect)
        elif mode == "ellipse": path.addEllipse(rect)
        elif mode == "line": path.moveTo(self.start); path.lineTo(point)
        elif mode == "freehand":
            self.pen_points.append((point, point)); path.moveTo(self.pen_points[0][0])
            for p, _ in self.pen_points[1:]: path.lineTo(p)
        if self.ghost: self.ghost.setPath(path)

    def mouseReleaseEvent(self, event):
        if self._guide_drag is not None:
            self._guide_drag = None
            self.workspace.update_status()
            return
        if self._pan is not None:
            self._pan = None; self.unsetCursor(); return
        if self.workspace.tool in {"select", "nodes"}:
            super().mouseReleaseEvent(event)
            moved = [(obj, self.workspace.items[obj.id]) for obj in self.workspace.document.objects
                     if obj.id in self.workspace.items and not self.workspace.items[obj.id].pos().isNull()]
            if moved:
                self.workspace.document.snapshot()
                for obj, item in moved:
                    delta = item.pos()
                    if self.workspace.snap.isChecked():
                        step = self.workspace.grid_step.value(); delta = QPointF(round(delta.x() / step) * step, round(delta.y() / step) * step)
                    obj.commands = commands_from_path(QTransform.fromTranslate(delta.x(), delta.y()).map(obj.path())); obj.text = None
                self.workspace.refresh()
            return
        if self.workspace.tool == "pen": self.start = None; return
        if self.ghost:
            path = self.ghost.path(); self.scene().removeItem(self.ghost); self.ghost = None
            if not path.isEmpty(): self.workspace.add_path(path, self.workspace.tool.title())
        self.start = None; self.pen_points.clear()

    def update_pen(self):
        path = QPainterPath()
        if self.pen_points:
            path.moveTo(self.pen_points[0][0])
            for index in range(1, len(self.pen_points)):
                previous, previous_handle = self.pen_points[index - 1]
                point, handle = self.pen_points[index]
                if previous == previous_handle and point == handle: path.lineTo(point)
                else: path.cubicTo(previous_handle, point * 2 - handle, point)
        if self.ghost is None:
            self.ghost = QGraphicsPathItem(); self.ghost.setPen(QPen(QColor("#0A7CFF"), .3)); self.ghost.setZValue(99999); self.scene().addItem(self.ghost)
        self.ghost.setPath(path)

    def finish_pen(self, close=False):
        if self.ghost:
            path = self.ghost.path()
            if close: path.closeSubpath()
            self.scene().removeItem(self.ghost); self.ghost = None
            if len(self.pen_points) > 1: self.workspace.add_path(path, "Bézier path")
        self.pen_points.clear(); self.start = None

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and self.pen_points: self.finish_pen(); return
        if event.key() == Qt.Key.Key_Escape:
            if self.ghost: self.scene().removeItem(self.ghost); self.ghost = None
            self.pen_points.clear(); self.start = None; self.workspace.set_tool("select"); return
        if event.key() in {Qt.Key.Key_Backspace, Qt.Key.Key_Delete}: self.workspace.delete_selected(); return
        if event.key() in {Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down}:
            step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            dx = step * (int(event.key() == Qt.Key.Key_Right) - int(event.key() == Qt.Key.Key_Left))
            dy = step * (int(event.key() == Qt.Key.Key_Down) - int(event.key() == Qt.Key.Key_Up))
            self.workspace.document.transform(self.workspace.selected_ids(), QTransform.fromTranslate(dx, dy)); self.workspace.refresh(); return
        super().keyPressEvent(event)

    def drawBackground(self, painter, rect):
        theme = ThemeManager.current(); painter.fillRect(rect, qc(theme.canvas))
        doc = self.workspace.document; painter.fillRect(QRectF(0, 0, doc.width, doc.height), Qt.GlobalColor.white)
        painter.setPen(QPen(QColor("#aeb7c1"), 0)); painter.drawRect(QRectF(0, 0, doc.width, doc.height))
        if self.workspace.grid.isChecked():
            step = self.workspace.grid_step.value()
            while step * self.transform().m11() < 10: step *= 5
            paint_grid(painter, QRectF(0, 0, doc.width, doc.height), rect, step, self.transform().m11(), QColor(140, 150, 165, 90))

    def drawForeground(self, painter, rect):
        doc = self.workspace.document
        painter.setPen(QPen(QColor("#35a8ff"), 0))
        for axis, value in doc.guides:
            painter.drawLine(QPointF(value, rect.top()), QPointF(value, rect.bottom())) if axis == "x" else painter.drawLine(QPointF(rect.left(), value), QPointF(rect.right(), value))
        if not self.workspace.rulers.isChecked(): return
        theme = ThemeManager.current()
        factor = {"mm": 1, "cm": 10, "in": 25.4}.get(doc.units, 1)
        paint_rulers(self, painter, factor, qc(theme.toolbar), qc(theme.text_secondary), QColor("#0A7CFF"))

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        if self.workspace.rulers.isChecked(): self.viewport().update()  # rulers stay fixed while the drawing scrolls


class VectorWorkspace(QWidget):
    """Integrated vector workspace. All dimensions and node coordinates use mm."""
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner; self.document = VectorDocument(); self.path = ""; self.tool = "select"
        self.items = {}; self.nodes = []; self.active_node = None; self._refreshing = False
        self.raster = None; self._generation = 0; self._busy = False
        self._first_show = True
        self._bridge = _Bridge(self); self._bridge.done.connect(self._trace_finished)
        self.setAcceptDrops(True)
        self._build()
        self.canvas.setAcceptDrops(False)  # dropped files go to the workspace, not the graphics scene
        self.canvas.viewport().setAcceptDrops(False)
        ThemeManager.instance().changed.connect(lambda: self.canvas.viewport().update())
        self.refresh(fit=True)

    def showEvent(self, event):
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            QTimer.singleShot(0, self.fit)

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        top = QHBoxLayout(); top.setContentsMargins(12, 10, 12, 10)
        top.addWidget(label("Vector Editor", "title"))
        for text, callback, icon_name in (("New", self.new_document, "file"), ("Open", self.open_dialog, "folder-open"),
                                           ("Save Project", self.save_project, "save")):
            button = secondary_button(text, icon_name); button.clicked.connect(callback); top.addWidget(button)
        for name, callback in (("undo", self.undo), ("redo", self.redo)):
            button = tool_button(name, name.title()); button.clicked.connect(callback); top.addWidget(button)
        self.operations = QToolButton(); self.operations.setText("Geometry"); self.operations.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.operations)
        for name, callback in (("Duplicate", self.duplicate), ("Delete", self.delete_selected), ("Group", self.group), ("Ungroup", self.ungroup),
                               ("Union", lambda: self.boolean("union")), ("Subtract", lambda: self.boolean("subtract")), ("Intersect", lambda: self.boolean("intersect")),
                               ("Exclusion", lambda: self.boolean("exclude")),
                               ("Simplify paths…", self.simplify), ("Close paths", self.close_paths), ("Outline text", self.outline_text),
                               ("Fit artboard to artwork", self.fit_artboard), ("Calibrate selected width…", self.calibrate), ("Cutting checks", self.cutting_checks),
                               ("Clear guides", self.clear_guides)):
            menu.addAction(name, callback)
        self.operations.setMenu(menu); top.addWidget(self.operations); top.addStretch()
        export = primary_button("Export", "export"); export.clicked.connect(self.export_dialog); top.addWidget(export)
        root.addLayout(top)
        body = QHBoxLayout(); body.setSpacing(0)
        tools = QVBoxLayout(); tools.setContentsMargins(8, 10, 8, 10); self.tool_buttons = {}
        for key, icon_name, title in (("select", "select", "Select / move (V)"), ("nodes", "vector", "Edit nodes and Bézier handles (A)"),
                                      ("pen", "pen", "Bézier pen (P): click for corners, drag for curves; Enter ends, right click closes"),
                                      ("freehand", "pen", "Freehand path"), ("rectangle", "rectangle", "Rectangle (R)"),
                                      ("ellipse", "circle", "Ellipse (E)"), ("line", "minus", "Line"), ("text", "text", "Text (T)")):
            button = tool_button(icon_name, title, checkable=True, size=36, icon_size=20)
            button.clicked.connect(lambda checked=False, tool=key: self.set_tool(tool)); tools.addWidget(button); self.tool_buttons[key] = button
        tools.addStretch(); body.addLayout(tools)
        self.canvas = VectorCanvas(self); self.canvas.scene().selectionChanged.connect(self.selection_changed); body.addWidget(self.canvas, 1)
        panel = QWidget(); panel.setObjectName("Inspector"); panel.setFixedWidth(270)
        properties = QVBoxLayout(panel); properties.setContentsMargins(12, 12, 12, 12)
        properties.addWidget(label("GEOMETRY · MILLIMETRES", "section"))
        form = QFormLayout(); self.name = QLineEdit(); form.addRow("Name", self.name)
        self.stroke = QLineEdit("#202936"); self.fill = QLineEdit("none")
        form.addRow("Fill", self.fill); form.addRow("Stroke", self.stroke)
        self.stroke_width = self._number(.3, 0, 100, .1); form.addRow("Stroke mm", self.stroke_width)
        self.opacity = self._number(100, 0, 100, 5); form.addRow("Opacity %", self.opacity)
        self.layer_combo = QComboBox(); form.addRow("Layer", self.layer_combo)
        self.object_locked = QCheckBox("Lock object"); form.addRow(self.object_locked)
        properties.addLayout(form)
        apply_style = secondary_button("Apply properties"); apply_style.clicked.connect(self.apply_properties); properties.addWidget(apply_style)
        color_row = QHBoxLayout()
        for title, field in (("Fill color", self.fill), ("Stroke color", self.stroke)):
            button = secondary_button(title); button.clicked.connect(lambda checked=False, f=field: self.pick_color(f)); color_row.addWidget(button)
        properties.addLayout(color_row)
        transform_button = secondary_button("Transform / dimensions…", "resize"); transform_button.clicked.connect(self.transform_dialog); properties.addWidget(transform_button)
        text_button = secondary_button("Edit selected text…", "text"); text_button.clicked.connect(self.edit_text); properties.addWidget(text_button)
        properties.addWidget(label("LAYERS & OBJECTS", "section"))
        self.object_list = QListWidget(); self.object_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.object_list.itemSelectionChanged.connect(self.list_selection_changed); self.object_list.itemChanged.connect(self.visibility_changed)
        properties.addWidget(self.object_list, 1)
        layer_row = QHBoxLayout()
        for title, callback in (("+ Layer", self.add_layer), ("Raise", lambda: self.reorder(1)), ("Lower", lambda: self.reorder(-1))):
            button = secondary_button(title); button.clicked.connect(callback); layer_row.addWidget(button)
        properties.addLayout(layer_row)
        self.trace_button = secondary_button("Import / trace raster…", "image"); self.trace_button.clicked.connect(self.trace_dialog); properties.addWidget(self.trace_button)
        self.raster_visible = QCheckBox("Show raster reference"); self.raster_visible.setChecked(True); self.raster_visible.toggled.connect(lambda: self.refresh())
        properties.addWidget(self.raster_visible)
        scroll = QScrollArea(); scroll.setWidget(panel); scroll.setWidgetResizable(True)
        scroll.setFixedWidth(286); scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body.addWidget(scroll); root.addLayout(body, 1)
        bottom = QHBoxLayout(); bottom.setContentsMargins(12, 7, 12, 7)
        for text, callback in (("Fit", self.fit), ("100%", self.actual_size)):
            button = secondary_button(text); button.clicked.connect(callback); bottom.addWidget(button)
        self.grid = QCheckBox("Grid"); self.grid.setToolTip("Show the grid (Ctrl+')"); self.grid.toggled.connect(lambda: self.canvas.viewport().update()); bottom.addWidget(self.grid)
        self.snap = QCheckBox("Snap"); bottom.addWidget(self.snap)
        self.rulers = QCheckBox("Rulers"); self.rulers.setChecked(True); self.rulers.toggled.connect(lambda: self.canvas.viewport().update()); bottom.addWidget(self.rulers)
        self.units = QComboBox(); self.units.addItems(["mm", "cm", "in"]); self.units.currentTextChanged.connect(self.change_units); bottom.addWidget(self.units)
        self.grid_step = self._number(5, .1, 1000, 1); self.grid_step.setSuffix(" mm"); self.grid_step.setMaximumWidth(100)
        self.grid_step.valueChanged.connect(lambda: self.canvas.viewport().update()); bottom.addWidget(self.grid_step)
        self.status = label("", "small"); bottom.addWidget(self.status, 1); root.addLayout(bottom)
        for shortcut, callback in (("Ctrl+S", self.save_project), ("Ctrl+Z", self.undo), ("Ctrl+Shift+Z", self.redo), ("Ctrl+D", self.duplicate),
                                   ("V", lambda: self.set_tool("select")), ("A", lambda: self.set_tool("nodes")), ("P", lambda: self.set_tool("pen")),
                                   ("R", lambda: self.set_tool("rectangle")), ("E", lambda: self.set_tool("ellipse")), ("T", lambda: self.set_tool("text")), ("Ctrl+'", self.grid.toggle)):
            action = QAction(self); action.setShortcut(QKeySequence(shortcut)); action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(callback); self.addAction(action)
        self.tool_buttons["select"].setChecked(True)

    @staticmethod
    def _number(value, low, high, step=1):
        box = QDoubleSpinBox(); box.setRange(low, high); box.setDecimals(3); box.setSingleStep(step); box.setValue(value); return box

    def error(self, exc): QMessageBox.warning(self, "Vector Editor", str(exc))

    def selected_ids(self): return {key for key, item in self.items.items() if item.isSelected()}

    def selected(self): return [obj for obj in self.document.objects if obj.id in self.selected_ids()]

    def refresh(self, fit=False, select=None):
        selection = self.selected_ids() if select is None else set(select)
        self._refreshing = True; self.canvas.scene().clear(); self.items.clear(); self.nodes.clear(); self.active_node = None
        self.canvas.ghost = None; self.canvas.pen_points.clear(); self.canvas.start = None
        doc = self.document
        self.units.blockSignals(True); self.units.setCurrentText(doc.units); self.units.blockSignals(False)
        if self.raster is not None and self.raster_visible.isChecked():
            preview = self.raster.copy(); preview.thumbnail((2000, 2000))
            raw = preview.convert("RGBA").tobytes(); image = QImage(raw, preview.width, preview.height, preview.width * 4, QImage.Format.Format_RGBA8888).copy()
            reference = self.canvas.scene().addPixmap(QPixmap.fromImage(image)); reference.setScale(doc.width / preview.width); reference.setOpacity(.5); reference.setZValue(-10)
        self.layer_combo.clear(); self.layer_combo.addItems([layer["name"] for layer in doc.layers])
        self.object_list.clear()
        for index, obj in enumerate(doc.objects):
            item = QGraphicsPathItem(obj.path()); item.setData(0, obj.id); item.setZValue(index)
            item.setBrush(Qt.BrushStyle.NoBrush if obj.fill == "none" else QColor(obj.fill))
            item.setPen(QPen(QColor(obj.stroke), obj.stroke_width) if obj.stroke != "none" and obj.stroke_width > 0 else QPen(Qt.PenStyle.NoPen))
            item.setOpacity(obj.opacity); item.setVisible(doc.is_visible(obj))
            # Locked objects stay selectable so their lock can be released in
            # Properties, while their geometry cannot be dragged or deleted.
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not doc.is_locked(obj) and self.tool == "select")
            self.canvas.scene().addItem(item); item.setSelected(obj.id in selection); self.items[obj.id] = item
        for obj in reversed(doc.objects):
            row = QListWidgetItem(f"{obj.name}   ·   {obj.layer}" + ("   [locked]" if doc.is_locked(obj) else ""))
            row.setData(Qt.ItemDataRole.UserRole, obj.id); row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Checked if obj.visible else Qt.CheckState.Unchecked)
            self.object_list.addItem(row); row.setSelected(obj.id in selection)
        bounds = QRectF(-10, -10, doc.width + 20, doc.height + 20).united(self.canvas.scene().itemsBoundingRect().adjusted(-10, -10, 10, 10))
        self.canvas.setSceneRect(bounds); self._refreshing = False
        self.selection_changed(); self.update_status()
        if fit: self.fit()

    def selection_changed(self):
        if self._refreshing: return
        selected = self.selected()
        # Selecting one member selects the entire logical group in Select mode.
        groups = {obj.group for obj in selected if obj.group} if self.tool == "select" else set()
        self._refreshing = True
        for obj in self.document.objects:
            if obj.group in groups and not self.document.is_locked(obj): self.items[obj.id].setSelected(True)
        selected = self.selected(); ids = {obj.id for obj in selected}
        for index in range(self.object_list.count()):
            row = self.object_list.item(index); row.setSelected(row.data(Qt.ItemDataRole.UserRole) in ids)
        self._refreshing = False
        if selected:
            obj = selected[-1]; self.name.setText(obj.name); self.fill.setText(obj.fill); self.stroke.setText(obj.stroke)
            self.stroke_width.setValue(obj.stroke_width); self.opacity.setValue(obj.opacity * 100); self.layer_combo.setCurrentText(obj.layer); self.object_locked.setChecked(obj.locked)
        self.show_nodes(); self.update_status()

    def list_selection_changed(self):
        if self._refreshing: return
        ids = {item.data(Qt.ItemDataRole.UserRole) for item in self.object_list.selectedItems()}
        self._refreshing = True
        for key, item in self.items.items(): item.setSelected(key in ids)
        self._refreshing = False; self.selection_changed()

    def show_nodes(self):
        for node in self.nodes:
            if node.scene(): self.canvas.scene().removeItem(node)
        self.nodes.clear(); self.active_node = None
        if self.tool != "nodes": return
        for obj in self.selected():
            if self.document.is_locked(obj): continue
            if len(obj.commands) > 1500:
                self.status.setText("Path has many nodes; simplify it before editing individual nodes."); continue
            for index, command in enumerate(obj.commands):
                for coord in range(1, len(command), 2):
                    node = _Node(self, obj, index, coord); self.canvas.scene().addItem(node); self.nodes.append(node)

    def set_tool(self, tool):
        if self.canvas.pen_points: self.canvas.finish_pen()
        self.tool = tool
        for name, button in self.tool_buttons.items(): button.setChecked(name == tool)
        self.canvas.setDragMode(QGraphicsView.DragMode.RubberBandDrag if tool in {"select", "nodes"} else QGraphicsView.DragMode.NoDrag)
        for obj in self.document.objects: self.items[obj.id].setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, tool == "select" and not self.document.is_locked(obj))
        self.show_nodes(); self.canvas.setFocus(); self.update_status()

    def update_status(self):
        doc = self.document; selected = self.selected()
        bounds = selected[0].path().boundingRect() if selected else None
        for obj in selected[1:]: bounds = bounds.united(obj.path().boundingRect())
        suffix = f" · selection {bounds.width():.2f} × {bounds.height():.2f} mm" if bounds else ""
        self.status.setText(f"{doc.width:.2f} × {doc.height:.2f} mm · {len(doc.objects)} objects · {self.canvas.transform().m11() / (96 / 25.4) * 100:.0f}%" + suffix + (" · unsaved" if doc.dirty else ""))

    def fit(self):
        bounds = QRectF(0, 0, self.document.width, self.document.height).united(self.canvas.scene().itemsBoundingRect())
        self.canvas.fitInView(bounds.adjusted(-5, -5, 5, 5), Qt.AspectRatioMode.KeepAspectRatio); self.update_status()

    def actual_size(self): self.canvas.setTransform(QTransform.fromScale(96 / 25.4, 96 / 25.4)); self.update_status()

    def add_path(self, path, name):
        try:
            obj = VectorObject(name, commands_from_path(path), fill=self.fill.text(), stroke=self.stroke.text(),
                               stroke_width=self.stroke_width.value(), opacity=self.opacity.value() / 100,
                               layer=self.layer_combo.currentText() or "Artwork")
            self.document.add(obj); self.refresh(select=[obj.id])
        except Exception as exc: self.error(exc)

    def add_text(self, point):
        text, accepted = QInputDialog.getText(self, "Text", "Text content:")
        if not accepted or not text: return
        try:
            obj = VectorObject.text_object(text, point.x(), point.y(), fill=self.stroke.text() if self.stroke.text() != "none" else "#202936", stroke="none",
                                           layer=self.layer_combo.currentText() or "Artwork")
            self.document.add(obj); self.refresh(select=[obj.id])
        except Exception as exc: self.error(exc)

    def edit_text(self):
        selected = self.selected()
        if len(selected) != 1 or not selected[0].text: return self.error("Select one editable text object. Transformed or outlined text is path geometry.")
        obj = selected[0]
        text, accepted = QInputDialog.getText(self, "Edit text", "Text:", text=obj.text["content"])
        if accepted:
            self.document.snapshot(); params = obj.text.copy(); params["content"] = text
            replacement = VectorObject.text_object(**params, fill=obj.fill, stroke=obj.stroke, stroke_width=obj.stroke_width, layer=obj.layer)
            obj.commands, obj.text = replacement.commands, replacement.text; self.refresh(select=[obj.id])

    def apply_properties(self):
        selected = self.selected()
        if not selected: return
        try:
            replacements = []
            for obj in selected:
                replacement = copy.deepcopy(obj)
                replacement.name = self.name.text() if len(selected) == 1 else obj.name
                replacement.fill = self.fill.text(); replacement.stroke = self.stroke.text(); replacement.stroke_width = self.stroke_width.value()
                replacement.opacity = self.opacity.value() / 100; replacement.layer = self.layer_combo.currentText(); replacement.locked = self.object_locked.isChecked()
                replacement.__post_init__(); replacements.append(replacement)
            self.document.snapshot(); lookup = {obj.id: obj for obj in replacements}
            self.document.objects = [lookup.get(obj.id, obj) for obj in self.document.objects]; self.refresh(select=lookup)
        except Exception as exc: self.error(exc)

    def pick_color(self, field):
        color = QColorDialog.getColor(QColor(field.text()), self)
        if color.isValid(): field.setText(color.name())

    def visibility_changed(self, row):
        if self._refreshing: return
        obj = next((o for o in self.document.objects if o.id == row.data(Qt.ItemDataRole.UserRole)), None)
        if obj:
            self.document.snapshot(); obj.visible = row.checkState() == Qt.CheckState.Checked; self.refresh()

    def add_layer(self):
        name, accepted = QInputDialog.getText(self, "New layer", "Layer name:")
        if accepted and name.strip() and not any(layer["name"] == name.strip() for layer in self.document.layers):
            self.document.snapshot(); self.document.layers.append({"name": name.strip(), "visible": True, "locked": False}); self.refresh(); self.layer_combo.setCurrentText(name.strip())

    def reorder(self, direction):
        ids = self.selected_ids()
        if not ids: return
        self.document.snapshot(); objects = self.document.objects
        indices = range(len(objects) - 2, -1, -1) if direction > 0 else range(1, len(objects))
        for index in indices:
            other = index + direction
            if objects[index].id in ids and objects[other].id not in ids: objects[index], objects[other] = objects[other], objects[index]
        self.refresh(select=ids)

    def duplicate(self):
        selected = self.selected()
        if not selected: return
        self.document.snapshot(); created = []
        for obj in selected:
            duplicate = obj.transformed(QTransform.fromTranslate(5, 5)); duplicate.id = uuid.uuid4().hex; duplicate.name += " copy"; duplicate.group = ""
            self.document.objects.append(duplicate); created.append(duplicate.id)
        self.refresh(select=created)

    def delete_selected(self):
        if self.tool == "nodes" and self.active_node:
            obj_id, command_index, coordinate = self.active_node
            obj = next((o for o in self.document.objects if o.id == obj_id), None)
            if obj and command_index > 0 and obj.commands[command_index][0] != "Z":
                self.document.snapshot(); obj.commands.pop(command_index); obj.text = None; self.refresh(select=[obj_id]); return
        selected = {obj.id for obj in self.selected() if not self.document.is_locked(obj)}
        if selected:
            self.document.snapshot(); self.document.objects = [obj for obj in self.document.objects if obj.id not in selected]; self.refresh()

    def group(self):
        selected = self.selected()
        if len(selected) < 2: return self.error("Select two or more objects to group.")
        self.document.snapshot(); group = uuid.uuid4().hex
        for obj in selected: obj.group = group
        self.refresh()

    def ungroup(self):
        self.document.snapshot()
        for obj in self.selected(): obj.group = ""
        self.refresh()

    def boolean(self, operation):
        try:
            obj = self.document.boolean(self.selected_ids(), operation); self.refresh(select=[obj.id])
        except Exception as exc: self.error(exc)

    def simplify(self):
        if not self.selected(): return
        tolerance, accepted = QInputDialog.getDouble(self, "Simplify paths", "Maximum approximation (mm); curves become polygons:", .1, .001, 100, 3)
        if accepted:
            self.document.snapshot()
            for obj in self.selected(): simplify_object(obj, tolerance)
            self.refresh()

    def close_paths(self):
        selected = self.selected()
        if not selected: return
        self.document.snapshot()
        for obj in selected:
            commands = []
            for command in obj.commands:
                if command[0] == "M" and commands and commands[-1][0] != "Z": commands.append(["Z"])
                commands.append(command)
            if commands and commands[-1][0] != "Z": commands.append(["Z"])
            obj.commands = commands; obj.text = None
        self.refresh()

    def outline_text(self):
        selected = self.selected()
        if selected:
            self.document.snapshot()
            for obj in selected: obj.text = None
            self.refresh()

    def transform_dialog(self):
        selected = self.selected()
        if not selected: return self.error("Select geometry to transform.")
        bounds = selected[0].path().boundingRect()
        for obj in selected[1:]: bounds = bounds.united(obj.path().boundingRect())
        dialog = QDialog(self); dialog.setWindowTitle("Transform geometry"); form = QFormLayout(dialog)
        x = self._number(bounds.x(), -100000, 100000); y = self._number(bounds.y(), -100000, 100000)
        width = self._number(max(.001, bounds.width()), .001, 100000); height = self._number(max(.001, bounds.height()), .001, 100000)
        angle = self._number(0, -360, 360); lock = QCheckBox("Maintain aspect ratio"); lock.setChecked(True)
        width.valueChanged.connect(lambda value: height.setValue(value * bounds.height() / bounds.width()) if lock.isChecked() and bounds.width() else None)
        mirror = QCheckBox("Flip horizontally")
        for text, field in (("X mm", x), ("Y mm", y), ("Width mm", width), ("Height mm", height), ("Rotate degrees", angle)): form.addRow(text, field)
        form.addRow(lock); form.addRow(mirror); form.addRow(label("Transforms outline editable text.", "small"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec():
            transform = QTransform(); transform.translate(x.value(), y.value()); transform.rotate(angle.value())
            if mirror.isChecked(): transform.translate(width.value(), 0)
            transform.scale((-1 if mirror.isChecked() else 1) * width.value() / max(.001, bounds.width()), height.value() / max(.001, bounds.height()))
            transform.translate(-bounds.x(), -bounds.y()); self.document.transform(self.selected_ids(), transform); self.refresh()

    def fit_artboard(self):
        if not self.document.objects: return
        bounds = self.document.objects[0].path().boundingRect()
        for obj in self.document.objects[1:]: bounds = bounds.united(obj.path().boundingRect())
        self.document.snapshot()
        transform = QTransform.fromTranslate(-bounds.x() + 5, -bounds.y() + 5)
        self.document.objects = [obj.transformed(transform) for obj in self.document.objects]
        self.document.width, self.document.height = max(1, bounds.width() + 10), max(1, bounds.height() + 10); self.refresh(fit=True)

    def calibrate(self):
        selected = self.selected()
        if not selected: return self.error("Select a reference object whose true width is known.")
        bounds = selected[0].path().boundingRect()
        for obj in selected[1:]: bounds = bounds.united(obj.path().boundingRect())
        if bounds.width() <= 0: return self.error("Reference width must be positive.")
        unit = self.document.units
        unit_factor = {"mm": 1, "cm": 10, "in": 25.4}.get(unit, 1)
        actual, accepted = QInputDialog.getDouble(self, "Calibrate drawing", f"Actual selected width ({unit}). Scales the whole drawing and artboard:", bounds.width() / unit_factor, .001, 100000, 3)
        if accepted:
            factor = actual * unit_factor / bounds.width(); self.document.snapshot()
            self.document.objects = [obj.transformed(QTransform.fromScale(factor, factor)) for obj in self.document.objects]
            self.document.guides = [[axis, value * factor] for axis, value in self.document.guides]
            self.document.width *= factor; self.document.height *= factor; self.refresh(fit=True)

    def change_units(self, unit):
        if unit == self.document.units: return
        self.document.snapshot()
        self.document.units = unit
        self.canvas.viewport().update()

    def clear_guides(self):
        self.document.snapshot(); self.document.guides = []; self.canvas.viewport().update()

    def cutting_checks(self):
        warnings = self.document.cutting_checks()
        text = "\n".join(warnings[:100]) if warnings else "No open, duplicate, degenerate or crossing contours were found by these bounded checks."
        QMessageBox.information(self, "Cutting geometry checks", text + "\n\nThis is not machine certification. Check kerf, material, scale and cutter requirements before production.")

    def undo(self):
        if self.document.undo(): self.refresh()

    def redo(self):
        if self.document.redo(): self.refresh()

    def new_document(self):
        if not self.maybe_close(): return
        dialog = QDialog(self); dialog.setWindowTitle("New vector document"); form = QFormLayout(dialog)
        width, height = self._number(210, 1, 10000), self._number(297, 1, 10000)
        form.addRow("Width mm", width); form.addRow("Height mm", height)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec():
            self.document = VectorDocument(width.value(), height.value()); self.path = ""; self.raster = None; self._generation += 1; self.refresh(fit=True)

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open vector artwork", "", "Vector artwork (*.engvec *.svg *.dxf);;Raster to trace (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp)")
        if path: self.open_file(path)

    def open_file(self, path):
        if not self.maybe_close(): return False
        try:
            suffix = Path(path).suffix.lower()
            if suffix == ".engvec": document = VectorDocument.load(path)
            elif suffix == ".svg": document = import_svg(path)
            elif suffix == ".dxf": document = import_dxf(path)
            else:
                with Image.open(path) as image:
                    from ...core.raster_limits import check_pixels
                    check_pixels(*image.size); self.import_image(ImageOps.exif_transpose(image).convert("RGBA"), confirm=False)
                return True
            self.document = document; self.raster = None; self.path = str(path) if suffix == ".engvec" else ""; self._generation += 1
            self.refresh(fit=True)
            if document.warnings: QMessageBox.information(self, "Import details", "\n".join(document.warnings))
            return True
        except Exception as exc: self.error(exc); return False

    def import_image(self, image, *, confirm=True):
        if confirm and not self.maybe_close(): return False
        from ...core.raster_limits import check_pixels
        check_pixels(*image.size)
        self.raster = image.convert("RGBA").copy(); dpi = 96
        self.document = VectorDocument(image.width * 25.4 / dpi, image.height * 25.4 / dpi)
        self.path = ""; self._generation += 1; self.refresh(fit=True)
        self.status.setText("Raster reference loaded · use Import / trace raster to create editable paths")
        return True

    def trace_dialog(self):
        if self.raster is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import raster reference", "", "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp)")
            if not path: return
            if not self.open_file(path): return
        from .trace_dialog import TraceDialog
        dialog = TraceDialog(self.raster, self)
        if not dialog.exec(): return
        if self.document.objects and QMessageBox.question(self, "Replace traced geometry", "Replace current vector geometry with this trace? The raster remains available.") != QMessageBox.StandardButton.Yes: return
        options = dialog.options()
        self._generation += 1; generation = self._generation; image = self.raster.copy()
        self._busy = True; self.trace_button.setEnabled(False); self.status.setText("Tracing on this computer…")
        QThreadPool.globalInstance().start(_Job(generation, lambda: trace_image(image, **options), self._bridge))

    def _trace_finished(self, generation, result, error):
        self._busy = False; self.trace_button.setEnabled(True)
        if generation != self._generation: return
        if error: self.error(error); return
        document, info = result
        # Keep a single undo step that restores the previous geometry.
        previous = self.document.state(); document._undo.append(previous); self.document = document
        self.raster_visible.setChecked(False); self.refresh(fit=True)
        self.status.setText(f"Trace complete · {info['contours']} contours · {info['nodes']} nodes · {info['removed']} specks removed · verify physical scale")

    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save editable vector project", self.path or "Artwork.engvec", "ENG vector project (*.engvec)")
        if not path: return False
        if not path.lower().endswith(".engvec"):
            path += ".engvec"
            if Path(path).exists() and QMessageBox.question(self, "Replace project", f"Replace {Path(path).name}?") != QMessageBox.StandardButton.Yes: return False
        try:
            self.document.save(path, overwrite=Path(path).exists()); self.path = path; self.update_status(); return True
        except Exception as exc: self.error(exc); return False

    def export_dialog(self):
        path, chosen = QFileDialog.getSaveFileName(self, "Export vector artwork", "Artwork.svg", "SVG geometry (*.svg);;DXF polylines (*.dxf);;Vector PDF (*.pdf);;PNG image (*.png)")
        if not path: return
        extension = {"SVG": ".svg", "DXF": ".dxf", "Vector": ".pdf", "PNG": ".png"}[chosen.split()[0]]
        if Path(path).suffix.lower() != extension:
            path += extension
            if Path(path).exists() and QMessageBox.question(self, "Replace export", f"Replace {Path(path).name}?") != QMessageBox.StandardButton.Yes: return
        try:
            overwrite = Path(path).exists()
            if extension == ".svg": self.document.export_svg(path, overwrite=overwrite)
            elif extension == ".dxf":
                tolerance, accepted = QInputDialog.getDouble(self, "DXF curve tolerance", "Curves become closed polylines. Approximation tolerance (mm):", .02, .001, 10, 3)
                if not accepted: return
                self.document.export_dxf(path, tolerance=tolerance, overwrite=overwrite)
            elif extension == ".pdf": self.document.export_pdf(path, overwrite=overwrite)
            else:
                dpi, accepted = QInputDialog.getDouble(self, "Raster export", "DPI:", 150, 1, 2400, 0)
                if not accepted: return
                from PySide6.QtCore import QBuffer, QIODevice
                from ...core.vector_document import _atomic_bytes
                data = QBuffer(); data.open(QIODevice.OpenModeFlag.WriteOnly)
                if not self.document.render_image(dpi).save(data, "PNG"): raise ValueError("PNG encoding failed.")
                _atomic_bytes(path, bytes(data.data()), self.document.source_path, overwrite)
            self.status.setText(f"Exported {Path(path).name} · editable project preserved")
        except Exception as exc: self.error(exc)

    def maybe_close(self):
        if self._busy:
            self.status.setText("Wait for tracing to finish before closing.")
            return False
        if not self.document.dirty: return True
        response = QMessageBox.question(self, "Unsaved vector work", "Save the editable vector project before closing?",
                                         QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                         QMessageBox.StandardButton.Save)
        if response == QMessageBox.StandardButton.Save: return self.save_project()
        return response == QMessageBox.StandardButton.Discard

    @staticmethod
    def _dropped(event):
        mime = event.mimeData()
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()
                and Path(url.toLocalFile()).suffix.lower() in VECTOR_DROP_SUFFIXES] if mime.hasUrls() else []

    def dragEnterEvent(self, event):
        if self._dropped(event): event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._dropped(event): event.acceptProposedAction()

    def dropEvent(self, event):
        paths = self._dropped(event)
        if paths: event.acceptProposedAction(); self.open_file(paths[0])
