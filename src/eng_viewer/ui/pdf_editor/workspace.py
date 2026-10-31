from __future__ import annotations

import copy
import json
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ...core.pdf_edit import FONT_NAMES, MAX_FILE_BYTES
from ..widgets import primary_button, secondary_button, tool_button


class PdfCanvas(QGraphicsView):
    clicked = Signal(object)
    drawn = Signal(object)
    zoomChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor("#59616c"))
        self.tool = "select"
        self._start = None
        self._rubber = None
        self._highlight = None
        self._pixmap = None

    def set_page(self, image, width, height, fit=False):
        if self._pixmap is not None:
            self.scene().removeItem(self._pixmap)
        self._pixmap = self.scene().addPixmap(QPixmap.fromImage(image))
        self._pixmap.setScale(width / max(1, image.width()))
        self._pixmap.setZValue(-10)
        self.scene().setSceneRect(0, 0, width, height)
        if fit:
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def highlight(self, rect=None):
        if self._highlight is not None:
            self.scene().removeItem(self._highlight)
            self._highlight = None
        if rect is not None:
            pen = QPen(QColor("#2489f5"), 1.3, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._highlight = self.scene().addRect(rect, pen)
            self._highlight.setZValue(10)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            point = self.mapToScene(event.position().toPoint())
            if self.tool == "select":
                self.clicked.emit(point)
            else:
                self._start = point
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._start is not None:
            rect = QRectF(self._start, self.mapToScene(event.position().toPoint())).normalized()
            if self._rubber is None:
                pen = QPen(QColor("#2489f5"), 1, Qt.PenStyle.DashLine)
                pen.setCosmetic(True)
                self._rubber = self.scene().addRect(rect, pen)
            else:
                self._rubber.setRect(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._start is not None:
            rect = QRectF(self._start, self.mapToScene(event.position().toPoint())).normalized()
            self._start = None
            if self._rubber is not None:
                self.scene().removeItem(self._rubber)
                self._rubber = None
            self.drawn.emit(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
            scale = self.transform().m11() * factor
            if .1 <= scale <= 12:
                self.scale(factor, factor)
                self.zoomChanged.emit()
            event.accept()
        else:
            super().wheelEvent(event)


class PdfObjectDialog(QDialog):
    def __init__(self, command, parent=None, original_font=""):
        super().__init__(parent)
        self.command = copy.deepcopy(command)
        self.setWindowTitle("Edit PDF object")
        self.resize(420, 420)
        layout = QVBoxLayout(self)
        if original_font:
            note = QLabel(f"Original font: {original_font}. Replacement uses the selected standard PDF font. "
                          "Spacing may change. Only isolated horizontal text and Latin-1 replacement characters are supported.")
            note.setWordWrap(True)
            layout.addWidget(note)
        form = QFormLayout()
        self.text = QTextEdit(command.get("text", ""))
        self.text.setAcceptRichText(False)
        self.text.setMaximumHeight(110)
        if command["kind"] in ("text", "replace_text", "note"):
            form.addRow("Text", self.text)
        self.font = QComboBox()
        for key, name in FONT_NAMES.items():
            self.font.addItem(name, key)
        self.font.setCurrentIndex(max(0, self.font.findData(command.get("font", "helv"))))
        self.size = self._number(command.get("size", 12), 1, 1000)
        if command["kind"] in ("text", "replace_text"):
            form.addRow("Font", self.font)
            form.addRow("Size (pt)", self.size)
        self.color = QColor.fromRgbF(*command.get("color", [.12, .25, .55]))
        color_button = QPushButton("Choose color…")
        color_button.clicked.connect(self._choose_color)
        form.addRow("Color", color_button)
        rect = command.get("rect", [0, 0, 120, 60])
        x, y = command.get("origin", rect[:2])
        self.x, self.y = self._number(x), self._number(y)
        self.width = self._number(max(.1, rect[2] - rect[0]), .1)
        self.height = self._number(max(.1, rect[3] - rect[1]), .1)
        form.addRow("X (pt)", self.x)
        form.addRow("Baseline Y (pt)" if command["kind"] == "replace_text" else "Y (pt)", self.y)
        if command["kind"] != "replace_text":
            form.addRow("Width (pt)", self.width)
            form.addRow("Height (pt)", self.height)
        self.stroke = self._number(command.get("width", 1.5), .1, 100)
        self.fill = QCheckBox("Fill with selected color")
        self.fill.setChecked(command.get("fill") is not None)
        if command["kind"] in ("rectangle", "ellipse", "line"):
            form.addRow("Stroke (pt)", self.stroke)
            form.addRow(self.fill)
        layout.addLayout(form)
        self.sample = QLabel()
        self.sample.setMinimumHeight(45)
        self.sample.setWordWrap(True)
        layout.addWidget(self.sample)
        self.text.textChanged.connect(self._preview)
        self.size.valueChanged.connect(self._preview)
        self._preview()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _number(self, value, minimum=-100000, maximum=100000):
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setDecimals(2)
        widget.setValue(value)
        return widget

    def _choose_color(self):
        color = QColorDialog.getColor(self.color, self, "Object color")
        if color.isValid():
            self.color = color
            self._preview()

    def _preview(self):
        self.sample.setText(self.text.toPlainText()[:100] or "Preview")
        font = self.sample.font()
        font.setPointSizeF(min(48, self.size.value()))
        self.sample.setFont(font)
        self.sample.setStyleSheet(f"color: {self.color.name()};")

    def result_command(self):
        command = copy.deepcopy(self.command)
        command.update(text=self.text.toPlainText(), font=self.font.currentData(), size=self.size.value(),
                       color=[self.color.redF(), self.color.greenF(), self.color.blueF()])
        if command["kind"] == "replace_text":
            command["origin"] = [self.x.value(), self.y.value()]
        else:
            command["rect"] = [self.x.value(), self.y.value(), self.x.value() + self.width.value(),
                               self.y.value() + self.height.value()]
        if command["kind"] in ("rectangle", "ellipse", "line"):
            command["width"] = self.stroke.value()
            command["fill"] = command["color"] if self.fill.isChecked() else None
        return command


class PdfEditWorkspace(QWidget):
    """Native UI for a worker-backed, source-preserving PDF edit journal."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.render = owner.render
        self.source_path = ""
        self.current_path = ""
        self.base_path = ""
        self.page_index = 0
        self.info = {}
        self.commands = []
        self._undo, self._redo = [], []
        self._saved = "[]"
        self._generation = 0
        self._render_generation = 0
        self._busy = False
        self._temporary = None
        self._group = ("pdf-editor", id(self))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        title = QLabel("Edit PDF")
        title.setProperty("role", "title")
        top.addWidget(title)
        self.open_button = secondary_button("Open PDF…", "folder")
        self.open_button.clicked.connect(self._open_dialog)
        top.addWidget(self.open_button)
        self.undo_button = tool_button("undo", "Undo PDF edit")
        self.redo_button = tool_button("redo", "Redo PDF edit")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        top.addWidget(self.undo_button)
        top.addWidget(self.redo_button)
        top.addStretch()
        self.name = QLabel("No PDF open")
        top.addWidget(self.name)
        self.save_button = primary_button("Save As…", "save")
        self.save_button.clicked.connect(self.save_as)
        top.addWidget(self.save_button)
        layout.addLayout(top)
        tools = QHBoxLayout()
        self.tool = QComboBox()
        for label, key in (("Select content", "select"), ("Add text", "text"),
                           ("Rectangle", "rectangle"), ("Ellipse", "ellipse"), ("Line", "line"),
                           ("Insert image", "image"), ("Add PDF note", "note"),
                           ("Redact region", "redact"), ("Crop page", "crop_page")):
            self.tool.addItem(label, key)
        self.tool.currentIndexChanged.connect(lambda: setattr(self.canvas, "tool", self.tool.currentData()))
        tools.addWidget(self.tool)
        self.replace_button = secondary_button("Replace selected text…")
        self.replace_button.clicked.connect(self.replace_selected)
        self.delete_button = secondary_button("Delete selected")
        self.delete_button.clicked.connect(self.delete_selected)
        tools.addWidget(self.replace_button)
        tools.addWidget(self.delete_button)
        rotate = secondary_button("Rotate page")
        rotate.clicked.connect(lambda: self._add({"kind": "rotate_page", "page": self.page_index, "degrees": 90}))
        tools.addWidget(rotate)
        self.rotate_button = rotate
        fit = secondary_button("Fit")
        fit.clicked.connect(self.fit_page)
        tools.addWidget(fit)
        tools.addStretch()
        self.previous = tool_button("chevron-left", "Previous page")
        self.next = tool_button("chevron-right", "Next page")
        self.previous.clicked.connect(lambda: self._go(-1))
        self.next.clicked.connect(lambda: self._go(1))
        self.page_label = QLabel("—")
        tools.addWidget(self.previous)
        tools.addWidget(self.page_label)
        tools.addWidget(self.next)
        layout.addLayout(tools)
        self.instructions = QLabel("Select a text span to replace it, or drag a box for a new object. Originals stay unchanged until Save As.")
        self.instructions.setWordWrap(True)
        layout.addWidget(self.instructions)
        splitter = QSplitter()
        self.canvas = PdfCanvas()
        self.canvas.clicked.connect(self._canvas_clicked)
        self.canvas.drawn.connect(self._drawn)
        self.canvas.zoomChanged.connect(self._schedule_render)
        splitter.addWidget(self.canvas)
        right = QWidget()
        side = QVBoxLayout(right)
        side.setContentsMargins(8, 0, 0, 0)
        self.tabs = QTabWidget()
        self.objects = QListWidget()
        self.objects.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.objects.currentItemChanged.connect(self._selected)
        self.session_objects = QListWidget()
        self.session_objects.itemDoubleClicked.connect(lambda _: self._edit_session())
        self.tabs.addTab(self.objects, "PDF contents")
        session = QWidget()
        session_layout = QVBoxLayout(session)
        session_layout.addWidget(self.session_objects)
        edit = secondary_button("Edit session object…")
        edit.clicked.connect(self._edit_session)
        remove = secondary_button("Remove session operation")
        remove.clicked.connect(self._remove_session)
        session_layout.addWidget(edit)
        session_layout.addWidget(remove)
        self.tabs.addTab(session, "This session")
        side.addWidget(self.tabs)
        self.detail = QLabel("Original vector graphics and images are inspectable. Added objects remain editable through This session.")
        self.detail.setWordWrap(True)
        side.addWidget(self.detail)
        splitter.addWidget(right)
        splitter.setSizes([900, 300])
        splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter, 1)
        self.status = QLabel("Open a PDF to edit its text, vector objects, notes and page geometry.")
        layout.addWidget(self.status)
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(140)
        self._zoom_timer.timeout.connect(self._render_page)
        for key, callback in ((QKeySequence.StandardKey.Undo, self.undo), (QKeySequence.StandardKey.Redo, self.redo),
                              (QKeySequence.StandardKey.SaveAs, self.save_as)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
        self._update_controls()

    @property
    def dirty(self):
        return json.dumps(self.commands, sort_keys=True) != self._saved

    def _error(self, message):
        self.status.setText(message)
        QMessageBox.warning(self, "PDF edit could not be applied", message)

    def _open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF for editing", "", "PDF documents (*.pdf)")
        if path:
            self.open_file(path)

    def open_dialog(self):
        """Public entry point used by the main application's Open command."""
        self._open_dialog()

    def open_file(self, path):
        if not self.maybe_close():
            return False
        source = Path(path)
        if not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
            self._error("Choose an existing PDF smaller than 512 MiB.")
            return False
        self.render.cancel_group(self._group)
        self._generation += 1
        self._render_generation += 1
        previous = self._temporary
        # A render that is still finishing may hold a revision open (Windows forbids deleting open
        # files); such leftovers stay in the system temporary folder instead of raising on close.
        self._temporary = tempfile.TemporaryDirectory(prefix="eng-pdf-edit-", ignore_cleanup_errors=True)
        self.base_path = str(Path(self._temporary.name) / "source.pdf")
        shutil.copyfile(source, self.base_path)
        if previous is not None:
            previous.cleanup()
        self.source_path, self.current_path = str(source.resolve()), self.base_path
        self.name.setText(source.name)
        self.page_index, self.commands, self._undo, self._redo = 0, [], [], []
        self._saved = "[]"
        self._inspect(fit=True)
        return True

    def _set_busy(self, busy, status=""):
        self._busy = busy
        if status:
            self.status.setText(status)
        self._update_controls()

    def _update_controls(self):
        ready = bool(self.current_path) and not self._busy
        for widget in (self.tool, self.save_button, self.replace_button, self.delete_button, self.rotate_button):
            widget.setEnabled(ready)
        self.open_button.setEnabled(not self._busy)
        self.canvas.setEnabled(not self._busy)
        self.undo_button.setEnabled(ready and bool(self._undo))
        self.redo_button.setEnabled(ready and bool(self._redo))
        self.previous.setEnabled(ready and self.page_index > 0)
        self.next.setEnabled(ready and self.page_index + 1 < self.info.get("page_count", 0))

    def _inspect(self, fit=False):
        self._set_busy(True, "Reading PDF objects…")
        generation = self._generation

        def done(result):
            if generation != self._generation:
                return
            self._set_busy(False)
            if not result.ok:
                self._error(result.error)
                return
            self.info = result.header
            self._refresh_objects()
            self._render_page(fit=fit)

        self.render.submit("pdf_edit", {"action": "inspect", "path": self.current_path, "page": self.page_index,
                                        "transient": True},
                           done, group=self._group, dedupe=False)

    def _refresh_objects(self):
        self.objects.clear()
        self.canvas.highlight()
        for title, key in (("Page text", "texts"), ("Images (inspect only)", "images"),
                           ("PDF annotations", "annotations"), ("Form fields (inspect only)", "forms")):
            heading = QListWidgetItem(title)
            heading.setFlags(Qt.ItemFlag.NoItemFlags)
            self.objects.addItem(heading)
            for entity in self.info.get(key, []):
                if key == "texts":
                    title = entity["text"].replace("\n", " ")[:90]
                elif key == "images":
                    title = f"Image · {entity['width']} × {entity['height']} px"
                elif key == "annotations":
                    title = f"{entity['type']} · {entity['text'][:50]}"
                else:
                    title = f"{entity['name']} · {entity['type']}"
                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, entity)
                self.objects.addItem(item)
        self.session_objects.clear()
        for index, command in enumerate(self.commands):
            item = QListWidgetItem(f"{index + 1}. {command['kind'].replace('_', ' ').title()} · page {command['page'] + 1}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.session_objects.addItem(item)
        self.page_label.setText(f"{self.page_index + 1} / {self.info.get('page_count', 1)}")
        self.status.setText(f"{len(self.info.get('texts', []))} text spans · {self.info.get('drawing_count', 0)} vector drawings · "
                            f"{len(self.commands)} session operations. Ctrl/Cmd + wheel zooms.")
        self._update_controls()

    @staticmethod
    def _transform(point, matrix):
        a, b, c, d, e, f = matrix
        return QPointF(a * point.x() + c * point.y() + e, b * point.x() + d * point.y() + f)

    def _map_rect(self, values, matrix):
        points = [self._transform(QPointF(x, y), matrix)
                  for x in (values[0], values[2]) for y in (values[1], values[3])]
        xs, ys = [p.x() for p in points], [p.y() for p in points]
        return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def _selected(self, item, _previous=None):
        entity = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not entity:
            return
        self.canvas.highlight(self._map_rect(entity["bbox"], self.info["rotation_matrix"]))
        if entity["kind"] == "text":
            self.detail.setText(f"PDF text · {entity['font']} · {entity['size']:.1f} pt. Replace removes the original text before inserting the selected font.")
        elif entity["kind"] == "annotation":
            self.detail.setText("This is a PDF annotation, separate from the page's original content. Delete removes the annotation object.")
        elif entity["kind"] == "form":
            self.detail.setText("This is an interactive form field. Form editing is not available in this workspace.")
        else:
            self.detail.setText("Original image inspection only. Use a deliberate region redaction to remove its content, or edit newly inserted images under This session.")

    def _canvas_clicked(self, point):
        if not self.info:
            return
        unrotated = self._transform(point, self.info["derotation_matrix"])
        for index in range(self.objects.count()):
            item = self.objects.item(index)
            entity = item.data(Qt.ItemDataRole.UserRole)
            if entity:
                x0, y0, x1, y1 = entity["bbox"]
                if QRectF(x0, y0, x1 - x0, y1 - y0).contains(unrotated):
                    self.objects.setCurrentItem(item)
                    return

    def _drawn(self, rect):
        if self._busy or not self.info:
            return
        kind = self.tool.currentData()
        if rect.width() < 3 or rect.height() < 3:
            if kind in ("redact", "crop_page"):
                self.status.setText("Drag a nonempty region on the page.")
                return
            rect.setWidth(160)
            rect.setHeight(60)
        rect = rect.intersected(self.canvas.sceneRect())
        mapped = self._map_rect([rect.left(), rect.top(), rect.right(), rect.bottom()], self.info["derotation_matrix"])
        command = {"kind": kind, "page": self.page_index,
                   "rect": [mapped.left(), mapped.top(), mapped.right(), mapped.bottom()]}
        if kind == "redact":
            answer = QMessageBox.warning(self, "Remove PDF content", "This removes text, image pixels and touching vector graphics in the region. "
                "Undo is available in this session. Save As writes the removal into a new PDF. "
                "Metadata and attachments require separate review; this is not a whole-document sanitization.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                return
        elif kind == "crop_page":
            pass
        else:
            if kind == "image":
                path, _ = QFileDialog.getOpenFileName(self, "Insert image", "", "Images (*.png *.jpg *.jpeg *.tif *.tiff *.webp *.heic *.avif *.bmp)")
                if not path:
                    return
                source = Path(path)
                if source.stat().st_size > MAX_FILE_BYTES:
                    self._error("The image file is too large.")
                    return
                target = Path(self._temporary.name) / f"asset-{len(list(Path(self._temporary.name).glob('asset-*')))}{source.suffix}"
                shutil.copyfile(source, target)
                command["image_path"] = str(target)
            dialog = PdfObjectDialog(command, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            command = dialog.result_command()
        self._add(command)

    def _entity(self):
        item = self.objects.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def replace_selected(self):
        entity = self._entity()
        if self._busy or not entity or entity["kind"] != "text":
            self.status.setText("Select an original PDF text span first.")
            return
        if not entity["horizontal"]:
            self._error("This transformed text cannot be reliably replaced. Use an explicit redaction and add new text if appropriate.")
            return
        command = {"kind": "replace_text", "page": self.page_index, "span_id": entity["id"],
                   "expected_text": entity["text"], "text": entity["text"], "origin": entity["origin"],
                   "rect": entity["bbox"], "size": entity["size"], "color": entity["color"], "font": "helv"}
        dialog = PdfObjectDialog(command, self, original_font=entity["font"])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._add(dialog.result_command())

    def delete_selected(self):
        entity = self._entity()
        if self._busy or not entity:
            return
        if entity["kind"] == "text":
            self._add({"kind": "delete_text", "page": self.page_index, "span_id": entity["id"], "expected_text": entity["text"]})
        elif entity["kind"] == "annotation":
            self._add({"kind": "delete_annotation", "page": self.page_index, "annotation_id": entity["id"]})
        else:
            self.status.setText("This original object is inspectable. Use region redaction for deliberate removal.")

    def _edit_session(self):
        item = self.session_objects.currentItem()
        if self._busy or item is None:
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        command = self.commands[index]
        if command["kind"] not in ("text", "replace_text", "rectangle", "ellipse", "line", "image", "note"):
            self.status.setText("Undo or remove this operation to change it.")
            return
        dialog = PdfObjectDialog(command, self, original_font="fallback font" if command["kind"] == "replace_text" else "")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            commands = copy.deepcopy(self.commands)
            commands[index] = dialog.result_command()
            self._submit_commands(commands)

    def _remove_session(self):
        item = self.session_objects.currentItem()
        if self._busy or item is None:
            return
        commands = copy.deepcopy(self.commands)
        commands.pop(item.data(Qt.ItemDataRole.UserRole))
        self._submit_commands(commands)

    def _add(self, command):
        if self._busy or not self.current_path:
            return
        self._submit_commands(self.commands + [command])

    def _submit_commands(self, commands, mode="edit"):
        self._set_busy(True, "Applying PDF edits in a background worker…")
        self._generation += 1
        generation = self._generation
        output = str(Path(self._temporary.name) / f"revision-{generation}.pdf")

        def done(result):
            if generation != self._generation:
                return
            self._set_busy(False)
            if not result.ok:
                self._error(result.error)
                return
            old = self.commands
            if mode == "undo":
                self._undo.pop()
                self._redo.append(old)
            elif mode == "redo":
                self._redo.pop()
                self._undo.append(old)
            else:
                self._undo.append(old)
                self._redo.clear()
            self.commands = copy.deepcopy(commands)
            self.current_path, self.info = result.header["path"], result.header
            self._refresh_objects()
            self._render_page(fit=any(c["kind"] in ("rotate_page", "crop_page") for c in commands[-1:]))

        self.render.submit("pdf_edit", {"action": "replay", "path": self.base_path, "output": output,
            "commands": commands, "page": self.page_index, "source_paths": [self.source_path]},
            done, group=self._group, dedupe=False)

    def undo(self):
        if self._undo and not self._busy:
            self._submit_commands(self._undo[-1], "undo")

    def redo(self):
        if self._redo and not self._busy:
            self._submit_commands(self._redo[-1], "redo")

    def _go(self, offset):
        page = self.page_index + offset
        if not self._busy and 0 <= page < self.info.get("page_count", 0):
            self.page_index = page
            self._generation += 1
            self._inspect(fit=True)

    def fit_page(self):
        if self.info:
            self.canvas.fitInView(self.canvas.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self._render_page()

    def _schedule_render(self):
        self._zoom_timer.start()

    def _render_page(self, fit=False):
        if not self.current_path or not self.info:
            return
        width, height = self.info["width"], self.info["height"]
        if fit:
            scale = min(max(1, self.canvas.viewport().width() - 8) / width,
                        max(1, self.canvas.viewport().height() - 8) / height)
        else:
            scale = max(.1, self.canvas.transform().m11())
        scale *= self.devicePixelRatioF()
        self._render_generation += 1
        generation = self._render_generation
        path = self.current_path

        def done(result):
            if generation != self._render_generation or path != self.current_path:
                return
            if result.ok and result.image is not None:
                self.canvas.set_page(result.image, width, height, fit)
                self._prune_revisions()
            elif not result.ok:
                self.status.setText(result.error)

        self.render.submit("render", {"path": path, "page": self.page_index, "scale": scale, "transient": True,
                           "max_pixels": 20_000_000}, done, group=self._group)

    def _prune_revisions(self):
        # Undo replays the journal, so historical PDF copies are not needed.
        # Windows may retain a worker's cached file handle; retry after the next
        # revision rather than disrupting rendering or removing the current PDF.
        if self._temporary is None:
            return
        for path in Path(self._temporary.name).glob("revision-*.pdf"):
            if str(path) != self.current_path:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def save_as(self):
        if self._busy or not self.current_path:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save edited PDF as", str(Path(self.source_path).with_name(Path(self.source_path).stem + "-edited.pdf")), "PDF (*.pdf)", options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:
            return
        if Path(path).suffix.lower() != ".pdf":
            path += ".pdf"
        if Path(path).exists():
            answer = QMessageBox.question(self, "Replace edited output?", f"Replace {Path(path).name}? Original source PDFs cannot be overwritten.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._set_busy(True, "Saving the edited PDF…")
        generation = self._generation

        def done(result):
            if generation != self._generation:
                return
            self._set_busy(False)
            if result.ok:
                self._saved = json.dumps(self.commands, sort_keys=True)
                self.status.setText(f"Saved {Path(path).name}. Original PDF unchanged.")
            else:
                self._error(result.error)

        self.render.submit("pdf_edit", {"action": "save", "path": self.current_path, "output": path,
            "page": self.page_index, "source_paths": [self.source_path, self.base_path],
            "overwrite": Path(path).exists()}, done, group=self._group, dedupe=False)

    def maybe_close(self):
        if self._busy:
            self.status.setText("Wait for the current PDF operation to finish before closing.")
            return False
        if not self.dirty:
            return True
        answer = QMessageBox.question(self, "Unsaved PDF edits", "Save the edited PDF before leaving?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            self.save_as()
            return False
        return answer == QMessageBox.StandardButton.Discard

    def closeEvent(self, event):
        if not self.maybe_close():
            event.ignore()
            return
        self.render.cancel_group(self._group)
        if self._temporary is not None:
            self._temporary.cleanup()
        event.accept()
