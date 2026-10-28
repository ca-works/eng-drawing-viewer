"""Generic tool panel built from a ToolSpec."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ...core.tool_support import unique_path
from ...core.render_service import PRI_EXPORT, RenderResult
from ..widgets import SegmentedControl, label, primary_button, secondary_button
from .common import Card, InputsCard, ResultCard, file_filter
from .specs import Field, ToolSpec, default_output_name


class _ColorButton(QPushButton):
    def __init__(self, color: str):
        super().__init__()
        self.setFixedSize(46, 24)
        self.color = color
        self.clicked.connect(self._pick)
        self._apply()

    def _apply(self):
        self.setStyleSheet(f"QPushButton {{ background: {self.color}; border: 1px solid rgba(0,0,0,0.25); border-radius: 6px; }}")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.color), self.window(), "Choose colour")
        if c.isValid():
            self.color = c.name()
            self._apply()


class _FilePick(QWidget):
    def __init__(self, f: Field, host):
        super().__init__()
        self.f, self.host = f, host
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setProperty("variant", "field")
        self.edit.setPlaceholderText("Choose a file…")
        lay.addWidget(self.edit, 1)
        b = secondary_button("Choose…")
        b.clicked.connect(self._choose)
        lay.addWidget(b)

    def _choose(self):
        path, _ = QFileDialog.getOpenFileName(self.window(), self.f.label, self.host.default_folder(), file_filter(self.f.file_exts))
        if path:
            self.edit.setText(path)

    def value(self) -> str:
        return self.edit.text().strip()


class ToolPanel(QWidget):
    """Inputs → options → run → result, for one ToolSpec."""

    outputsCreated = Signal(list)

    def __init__(self, spec: ToolSpec, host, parent=None):
        super().__init__(parent)
        self.spec, self.host = spec, host
        self._widgets: dict[str, tuple[Field, QWidget, QWidget]] = {}
        self._jobs: list[int] = []
        self._results: list[RenderResult] = []
        self._expected = 0
        self._group = ("tool", id(self))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        hint = ("Drag rows to set the order. " if spec.reorder else "") + (
            "All files are combined into one result." if spec.multi else
            "Each file is processed separately." if spec.batch else "")
        self.inputs = InputsCard(host, spec.accepts, spec.reorder, hint)
        self.inputs.changed.connect(self._update)
        lay.addWidget(self.inputs)

        if spec.fields:
            card = Card("Options")
            self.form = QFormLayout()
            self.form.setHorizontalSpacing(16)
            self.form.setVerticalSpacing(10)
            self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
            for f in spec.fields:
                self._add_field(f)
            card.lay.addLayout(self.form)
            lay.addWidget(card)
            self._sync_visibility()

        run_row = QHBoxLayout()
        self.note = label("", "tertiary", wrap=True)
        run_row.addWidget(self.note, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(160)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        run_row.addWidget(self.progress)
        self.cancel_btn = secondary_button("Stop")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.hide()
        run_row.addWidget(self.cancel_btn)
        self.run_btn = primary_button(spec.title.replace("&", "&&"), spec.icon)
        self.run_btn.clicked.connect(self.run)
        run_row.addWidget(self.run_btn)
        lay.addLayout(run_row)

        self.result = ResultCard()
        self.result.openInViewer.connect(host.open_in_viewer)
        lay.addWidget(self.result)
        lay.addStretch(1)
        self._update()

    # ------------------------------------------------------------ fields
    def _add_field(self, f: Field):
        if f.kind == "choice":
            if len(f.options) <= 4 and sum(len(str(o[1])) for o in f.options) < 42:
                w = SegmentedControl([(str(v), str(text), None, "") for v, text in f.options])
                w.set_current(str(f.default))
                w.changed.connect(lambda _k: self._sync_visibility())
            else:
                w = QComboBox()
                for v, text in f.options:
                    w.addItem(str(text), v)
                w.setCurrentIndex(max(0, [o[0] for o in f.options].index(f.default) if f.default in [o[0] for o in f.options] else 0))
                w.currentIndexChanged.connect(lambda _i: self._sync_visibility())
        elif f.kind == "combo":
            w = QComboBox()
            w.setEditable(True)
            w.addItems([str(o) for o in f.options])
            w.setCurrentText(str(f.default))
        elif f.kind == "bool":
            w = QCheckBox(f.label)
            w.setChecked(bool(f.default))
            w.toggled.connect(lambda _b: self._sync_visibility())
        elif f.kind == "int":
            w = QSpinBox()
            w.setRange(int(f.minimum), int(f.maximum))
            w.setValue(int(f.default or 0))
            w.setSuffix(f.suffix)
        elif f.kind == "float":
            w = QDoubleSpinBox()
            w.setRange(f.minimum, f.maximum)
            w.setDecimals(2 if f.maximum <= 1 else 1)
            w.setSingleStep(0.05 if f.maximum <= 1 else 1)
            w.setValue(float(f.default or 0))
            w.setSuffix(f.suffix)
        elif f.kind == "color":
            w = _ColorButton(f.default or "#000000")
        elif f.kind == "file":
            w = _FilePick(f, self.host)
        elif f.kind == "multiline":
            w = QPlainTextEdit()
            w.setObjectName("CommentEdit")
            w.setPlaceholderText(f.placeholder)
            w.setFixedHeight(80)
        else:
            w = QLineEdit()
            w.setProperty("variant", "field")
            w.setPlaceholderText(f.placeholder)
            if f.kind == "password":
                w.setEchoMode(QLineEdit.EchoMode.Password)
            if f.default:
                w.setText(str(f.default))
        if f.help:
            w.setToolTip(f.help)
        lab = QLabel("" if f.kind == "bool" else f.label)
        lab.setProperty("role", "secondary")
        holder = w
        if f.help and f.kind not in ("bool",):
            holder = QWidget()
            v = QVBoxLayout(holder)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(3)
            v.addWidget(w, 0, Qt.AlignmentFlag.AlignLeft)
            v.addWidget(label(f.help, "tertiary", wrap=True))
        self.form.addRow(lab, holder)
        self._widgets[f.key] = (f, w, lab)

    def _value(self, key: str):
        f, w, _ = self._widgets[key]
        if isinstance(w, SegmentedControl):
            raw = w.current_key()
            for v, _t in f.options:
                if str(v) == raw:
                    return v
            return raw
        if isinstance(w, QComboBox):
            return w.currentText() if w.isEditable() else w.currentData()
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, (QSpinBox, QDoubleSpinBox)):
            return w.value()
        if isinstance(w, _ColorButton):
            return w.color
        if isinstance(w, _FilePick):
            return w.value()
        if isinstance(w, QPlainTextEdit):
            return w.toPlainText()
        return w.text()

    def values(self) -> dict:
        return {k: self._value(k) for k in self._widgets}

    def _sync_visibility(self):
        for key, (f, w, lab) in self._widgets.items():
            vis = True
            if f.visible_if:
                other, allowed = f.visible_if
                val = self._value(other) if other in self._widgets else None
                allowed = allowed if isinstance(allowed, tuple) else (allowed,)
                vis = val in allowed
            self.form.setRowVisible(lab, vis)

    # --------------------------------------------------------------- run
    def _update(self):
        n = len(self.inputs.paths())
        reason = self.spec.available() if self.spec.available else ""
        busy = bool(self._jobs)
        self.run_btn.setEnabled(not reason and not busy and n >= self.spec.min_inputs)
        if reason:
            self.note.setText(reason)
        elif n < self.spec.min_inputs:
            self.note.setText("Add at least two files." if self.spec.min_inputs > 1 else "Add a file to begin — drop it here or use Add Files.")
        else:
            self.note.setText("Originals are never changed — results are saved as new files.")

    def add_inputs(self, paths: list[str]) -> int:
        return self.inputs.list.add_paths(paths)

    def _out_ext(self, values: dict) -> str:
        if self.spec.output == "text":
            return ".md" if values.get("markdown") else ".txt"
        return {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx"}.get(self.spec.output, "")

    def run(self):
        spec = self.spec
        inputs = self.inputs.paths()
        if len(inputs) < spec.min_inputs:
            return
        values = self.values()
        ext = self._out_ext(values)
        folder = str(Path(inputs[0]).parent)
        jobs: list[dict] = []
        groups = [inputs] if (spec.multi or not spec.batch) else [[p] for p in inputs]
        if spec.output == "folder":
            parent = QFileDialog.getExistingDirectory(self.window(), "Choose where to save", folder)
            if not parent:
                return
            for g in groups:
                # Several files combined write straight into the chosen folder;
                # one file gets its own subfolder (e.g. "Drawing 1 images").
                target = Path(parent) if spec.multi else unique_path(Path(parent) / f"{Path(g[0]).stem}{spec.suffix}")
                jobs += spec.build(values, g, str(target))
        elif len(groups) == 1:
            name = default_output_name(spec, inputs[0]).rsplit(".", 1)[0] + ext
            if spec.multi:
                name = f"{Path(inputs[0]).stem}{spec.suffix}{ext}"
            suggested = str(unique_path(Path(folder) / name))
            path, _ = QFileDialog.getSaveFileName(self.window(), f"Save {spec.title} result", suggested, f"*{ext}")
            if not path:
                return
            if ext and not path.lower().endswith(ext):
                path += ext
            if any(Path(path).resolve() == Path(i).resolve() for i in inputs):
                self.result.show_error("Choose a different name — originals are never overwritten.")
                return
            jobs += spec.build(values, groups[0], path)
        else:
            parent = QFileDialog.getExistingDirectory(self.window(), "Choose a folder for the results", folder)
            if not parent:
                return
            for g in groups:
                target = unique_path(Path(parent) / (Path(g[0]).stem + spec.suffix + ext))
                jobs += spec.build(values, g, str(target))
        self._start(jobs)

    def _start(self, jobs: list[dict]):
        self.result.hide()
        self._results = []
        self._expected = len(jobs)
        self._jobs = [self.host.render.submit("tool", args, self._on_done, priority=PRI_EXPORT, group=self._group, dedupe=False)
                      for args in jobs]
        self.progress.setRange(0, 0 if len(jobs) == 1 else len(jobs))
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_btn.setVisible(len(jobs) > 1)
        self._update()

    def _on_done(self, res: RenderResult):
        self._results.append(res)
        if self._expected > 1:
            self.progress.setValue(len(self._results))
        if len(self._results) < self._expected:
            return
        self._finish()

    def _finish(self):
        self._jobs = []
        self.progress.hide()
        self.cancel_btn.hide()
        ok = [r for r in self._results if r.ok]
        errors = [r.error for r in self._results if not r.ok]
        outputs = [o for r in ok for o in r.header.get("outputs", [])]
        if ok:
            summary = " ".join(r.header.get("summary", "") for r in ok[:3]).strip()
            if len(ok) > 3:
                summary += f" … ({len(ok)} files)"
            if errors:
                summary += f"\n{len(errors)} file{'s' if len(errors) != 1 else ''} failed: {errors[0]}"
            self.result.show_success(f"{self.spec.title} finished", summary, outputs)
            if outputs:
                self.outputsCreated.emit(outputs)
        else:
            self.result.show_error(errors[0] if errors else "Nothing was produced.")
        self._update()

    def _cancel(self):
        self.host.render.cancel_group(self._group)
        if self._results:
            self._finish()
        else:
            self._jobs = []
            self.progress.hide()
            self.cancel_btn.hide()
            self._update()
