"""Status strip shared by the Image and Vector editors.

Status text on the left; zoom controls, a View menu and inline view toggles (Grid, Snap,
Rulers, units) on the right. The View menu always lists every toggle and the units, so when
the window or the side panels leave too little room the inline toggles simply hide instead
of being squeezed, clipped or overlapping.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QSizePolicy, QWidget

from .widgets import DropdownButton, ElidedLabel

MIN_STATUS_WIDTH = 140


class ViewOptionsBar(QWidget):
    toggled = Signal(str, bool)
    unitChanged = Signal(str)

    def __init__(self, toggles, units, parent=None):
        """``toggles``: (key, label, tooltip) triples; ``units``: unit names for the unit menu."""
        super().__init__(parent)
        self.setObjectName("ViewOptionsBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(8)
        self.status = ElidedLabel("", Qt.TextElideMode.ElideRight)
        self.status.setProperty("role", "small")
        row.addWidget(self.status, 1)
        self._leading = QHBoxLayout()
        self._leading.setSpacing(4)
        row.addLayout(self._leading)
        self.view_button = DropdownButton("View", variant="ghost")
        self.view_button.setToolTip("View options: grid, snapping, rulers and units")
        self.view_menu = self.view_button.dropdown_menu()
        row.addWidget(self.view_button)
        self._extra_actions = []
        self.inline = QWidget()
        self.inline.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        inline_row = QHBoxLayout(self.inline)
        inline_row.setContentsMargins(0, 0, 0, 0)
        inline_row.setSpacing(12)
        self._inline_row = inline_row
        self._boxes: dict[str, QCheckBox] = {}
        self._actions: dict[str, QAction] = {}
        for key, text, tooltip in toggles:
            box = QCheckBox(text)
            box.setToolTip(tooltip)
            box.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            action = QAction(text, self)
            action.setCheckable(True)
            action.setToolTip(tooltip)
            action.toggled.connect(box.setChecked)
            box.toggled.connect(lambda on, k=key, a=action: (a.setChecked(on), self.toggled.emit(k, on)))
            inline_row.addWidget(box)
            self._boxes[key] = box
            self._actions[key] = action
        self.units = QComboBox()
        self.units.addItems(list(units))
        self.units.setToolTip("Ruler and grid units")
        self.units.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.units.setMinimumContentsLength(3)
        self.units.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        inline_row.addWidget(self.units)
        row.addWidget(self.inline)
        self._unit_group = QActionGroup(self)
        self._unit_actions = {}
        for unit in units:
            action = QAction(unit, self, checkable=True)
            action.triggered.connect(lambda _=False, u=unit: self.units.setCurrentText(u))
            self._unit_group.addAction(action)
            self._unit_actions[unit] = action
        self.units.currentTextChanged.connect(self._unit_changed)
        self._unit_actions[self.units.currentText()].setChecked(True)
        self._rebuild_menu()

    # ----------------------------------------------------------------- API
    def box(self, key: str) -> QCheckBox:
        return self._boxes[key]

    def action(self, key: str) -> QAction:
        return self._actions[key]

    def add_leading(self, widget: QWidget):
        self._leading.addWidget(widget)
        self._update_inline()

    def add_inline(self, widget: QWidget):
        """Another control shown with the inline toggles (hidden with them when space is short)."""
        self._inline_row.addWidget(widget)
        self._update_inline()

    def add_menu_action(self, text: str, callback) -> QAction:
        action = QAction(text, self)
        action.triggered.connect(lambda _=False: callback())
        self._extra_actions.append(action)
        self._rebuild_menu()
        return action

    def inline_visible(self) -> bool:
        return not self.inline.isHidden()

    # ------------------------------------------------------------ internals
    def _rebuild_menu(self):
        menu = self.view_menu
        menu.clear()
        for action in self._extra_actions:
            menu.addAction(action)
        if self._extra_actions:
            menu.addSeparator()
        for action in self._actions.values():
            menu.addAction(action)
        units = menu.addMenu("Units")
        for action in self._unit_actions.values():
            units.addAction(action)

    def _unit_changed(self, unit: str):
        if unit in self._unit_actions:
            self._unit_actions[unit].setChecked(True)
        self.unitChanged.emit(unit)

    def _required_width(self) -> int:
        margins = self.layout().contentsMargins()
        spacing = self.layout().spacing()
        leading = self._leading.sizeHint().width()
        return (margins.left() + margins.right() + MIN_STATUS_WIDTH + leading + self.view_button.sizeHint().width()
                + self.inline.sizeHint().width() + spacing * 3)

    def _update_inline(self):
        show = self.width() >= self._required_width()
        if show == self.inline.isHidden():
            self.inline.setVisible(show)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_inline()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_inline()
