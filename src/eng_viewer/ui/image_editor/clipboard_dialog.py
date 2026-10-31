"""Small reusable clipboard preferences dialog."""
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout)
from PySide6.QtGui import QColor

from ...core.image_clipboard import FORMATS


class ClipboardDialog(QDialog):
    def __init__(self, settings=None, has_selection=False, parent=None, preferences_only=False):
        super().__init__(parent)
        settings = settings or {}
        self.setWindowTitle("Copy to Clipboard")
        self.setMinimumWidth(370)
        layout = QVBoxLayout(self)
        intro = QLabel("Choose the image format for your next paste.")
        layout.addWidget(intro)
        form = QFormLayout()
        self.format = QComboBox()
        self.format.addItems(list(FORMATS))
        self.format.setCurrentText(settings.get("format", "PNG"))
        form.addRow("Format", self.format)
        self.region = QComboBox()
        self.region.addItem("Complete image", "image")
        if has_selection:
            self.region.addItem("Selected region", "selection")
        self.region.setCurrentIndex(max(0, self.region.findData(settings.get("region", "image"))))
        form.addRow("Copy", self.region)
        self.quality = QSpinBox()
        self.quality.setRange(1, 100)
        self.quality.setValue(settings.get("quality", 90))
        form.addRow("Quality", self.quality)
        self.scale = QSpinBox()
        self.scale.setRange(1, 400)
        self.scale.setSuffix(" %")
        self.scale.setValue(settings.get("scale", 100))
        form.addRow("Resolution", self.scale)
        self.dpi = QSpinBox()
        self.dpi.setRange(1, 2400)
        self.dpi.setValue(settings.get("dpi", 96))
        form.addRow("DPI", self.dpi)
        self.alpha = QCheckBox("Preserve transparency")
        self.alpha.setChecked(settings.get("transparency", True))
        form.addRow("", self.alpha)
        self.background = settings.get("background", "#ffffff")
        self.color = QPushButton(self.background)
        self.color.clicked.connect(self.pick_color)
        form.addRow("Background", self.color)
        layout.addLayout(form)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.remember = QCheckBox("Remember my choice and don’t ask again.")
        self.remember.setChecked(settings.get("remember", False))
        layout.addWidget(self.remember)
        hint = QLabel("Reopen these options from the arrow beside Copy to Clipboard.")
        hint.setWordWrap(True)
        hint.setProperty("role", "small")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton("Save" if preferences_only else "Copy", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.format.currentTextChanged.connect(self.update_format)
        self.update_format()

    def update_format(self, *_):
        opaque = self.format.currentText() in ("JPEG", "BMP")
        self.alpha.setEnabled(not opaque)
        self.quality.setEnabled(self.format.currentText() == "JPEG")
        self.note.setText("Transparency will be flattened onto your background colour." if opaque else
                          "Alpha is preserved. Compatible PNG/TIFF or bitmap fallbacks are also supplied.")

    def pick_color(self):
        color = QColorDialog.getColor(QColor(self.background), self, "Clipboard background")
        if color.isValid():
            self.background = color.name()
            self.color.setText(self.background)

    def options(self):
        return dict(format=self.format.currentText(), region=self.region.currentData(), quality=self.quality.value(),
                    scale=self.scale.value(), dpi=self.dpi.value(), transparency=self.alpha.isChecked(),
                    background=self.background, remember=self.remember.isChecked())
