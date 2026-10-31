"""Interactive raster/vector comparison with bounded asynchronous previews."""
from PIL import Image
from PySide6.QtCore import Qt, QTimer, QThreadPool
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox,
    QSpinBox, QDoubleSpinBox, QCheckBox, QDialogButtonBox)
from ..image_editor.common import Job, to_qimage
from ...core.vector_trace import trace_image

PRESETS = {
    'Laser cutting': ('threshold', 128, 4, 16, .8, .4),
    'Laser engraving': ('edges', 110, 4, 6, .5, .2),
    'Vinyl cutting': ('threshold', 140, 4, 12, 1.2, .5),
    'CAD import': ('threshold', 128, 4, 8, .4, 0),
    'General vector illustration': ('color', 128, 6, 8, .8, .5),
}


class TraceDialog(QDialog):
    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.image = image
        self._job = None
        self._closed = False
        self._pending = False
        self._generation = 0
        self.setWindowTitle('Trace image to editable vectors')
        self.resize(860, 720)
        root = QVBoxLayout(self)
        previews = QHBoxLayout()
        self.before, self.after = QLabel(), QLabel('Preparing vector preview…')
        for title, field in [('Original raster', self.before), ('Vector preview', self.after)]:
            col = QVBoxLayout()
            col.addWidget(QLabel(title))
            field.setMinimumSize(340, 260)
            field.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(field, 1)
            previews.addLayout(col, 1)
        source = image.copy(); source.thumbnail((380, 300))
        self.before.setPixmap(QPixmap.fromImage(to_qimage(source)))
        root.addLayout(previews, 1)
        form = QFormLayout()
        self.preset = QComboBox(); self.preset.addItems(list(PRESETS))
        self.mode = QComboBox(); self.mode.addItems(['threshold','color','edges'])
        self.threshold = QSpinBox(); self.threshold.setRange(0,255)
        self.colors = QSpinBox(); self.colors.setRange(2,32)
        self.noise = self.number(8,0,10000)
        self.simplify = self.number(.8,0,100)
        self.smooth = self.number(.5,0,30)
        self.dpi = self.number(96,1,2400)
        self.background = QCheckBox('Remove colour connected to image corners')
        self.invert = QCheckBox('Trace light areas instead of dark')
        for title, field in [('Preset',self.preset),('Mode',self.mode),('Threshold / sensitivity',self.threshold),
                             ('Colours',self.colors),('Minimum area, source pixels²',self.noise),
                             ('Simplification, source pixels',self.simplify),('Smoothing, source pixels',self.smooth),
                             ('Source DPI (verify physical scale)',self.dpi)]:
            form.addRow(title,field)
        form.addRow(self.background);form.addRow(self.invert);root.addLayout(form)
        self.status = QLabel();self.status.setWordWrap(True);root.addWidget(self.status)
        note = QLabel('Preview uses up to 700 pixels; Apply traces at up to 2400 pixels. Automatic tracing is not a production-ready cutting guarantee. Calibrate a known dimension, inspect nodes, then run Cutting checks.')
        note.setWordWrap(True);root.addWidget(note)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Apply trace')
        self.buttons.accepted.connect(self.accept);self.buttons.rejected.connect(self.reject);root.addWidget(self.buttons)
        self.timer = QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.preview)
        for field in [self.threshold,self.colors,self.noise,self.simplify,self.smooth,self.dpi]:
            field.valueChanged.connect(self.schedule)
        self.mode.currentTextChanged.connect(self.schedule)
        self.background.toggled.connect(self.schedule);self.invert.toggled.connect(self.schedule)
        self.preset.currentTextChanged.connect(self.apply_preset)
        self.apply_preset(self.preset.currentText())

    def done(self, result):
        self._closed = True
        self.timer.stop()
        super().done(result)

    def number(self, value, low, high):
        spin=QDoubleSpinBox();spin.setRange(low,high);spin.setDecimals(2);spin.setValue(value);return spin

    def apply_preset(self, name):
        mode, threshold, colors, noise, simplify, smooth = PRESETS[name]
        self.mode.setCurrentText(mode);self.threshold.setValue(threshold);self.colors.setValue(colors)
        self.noise.setValue(noise);self.simplify.setValue(simplify);self.smooth.setValue(smooth)
        self.schedule()

    def options(self):
        return dict(mode=self.mode.currentText(),threshold=self.threshold.value(),colors=self.colors.value(),
                    min_area=self.noise.value(),simplify=self.simplify.value(),smoothing=self.smooth.value(),
                    dpi=self.dpi.value(),invert=self.invert.isChecked(),remove_background=self.background.isChecked())

    def schedule(self,*_):
        self._generation+=1;self.timer.start(180)

    def preview(self):
        if self._job:
            self._pending=True;return
        generation=self._generation;options=self.options();source=self.image
        def work():
            doc,info=trace_image(source,max_side=700,**options)
            preview=doc.render_image(dpi=max(1,min(150,380*25.4/max(doc.width,doc.height))),transparent=False)
            return preview,info
        job=Job(work);self._job=job
        def ready(result,error):
            self._job=None
            if self._closed: return
            if generation==self._generation:
                if error:self.status.setText(error)
                else:
                    image,info=result
                    self.after.setPixmap(QPixmap.fromImage(image).scaled(380,300,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                    self.status.setText(f"Preview: {info['contours']} contours · {info['nodes']} nodes · {info['removed']} noise regions removed")
            if self._pending or generation!=self._generation:
                self._pending=False;self.timer.start(0)
        job.signals.finished.connect(ready);QThreadPool.globalInstance().start(job)
