"""Regression coverage for the macOS Reference dock's real input paths."""
import sys

import pymupdf
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QLineEdit

from eng_viewer.ui.main_window import MainWindow

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Reference viewer")


def test_reference_continuous_scroll_and_document_history(qtbot, render, theme, settings, tmp_path):
    def pdf(name, count):
        path = tmp_path / name
        with pymupdf.open() as document:
            for number in range(count):
                page = document.new_page(width=360 + number % 2 * 40, height=450)
                page.insert_text((20, 30), str(number + 1))
            document.save(path)
        return path

    long_pdf, single_pdf = pdf("Long.pdf", 420), pdf("Single.pdf", 1)
    window = MainWindow(settings, render, QPixmap())
    qtbot.addWidget(window)
    window.resize(880, 560)
    window.show()
    window.activateWindow()
    dock = window._standards_dock()
    dock.toggle_at(window)
    panel = dock.panel
    panel.open_paths([long_pdf, single_pdf])
    first, second = panel.tabs.widget(0), panel.tabs.widget(1)
    qtbot.waitUntil(lambda: first.count == 420 and second.count == 1, timeout=15000)
    assert dock.windowTitle() == "Reference"
    assert isinstance(first.width, QLineEdit) and not first.findChildren(QAbstractSpinBox)
    assert not hasattr(first, "page")

    qtbot.mouseClick(panel.back, Qt.MouseButton.LeftButton)
    view = first.canvas
    qtbot.waitUntil(lambda: view.hasFocus())
    qtbot.waitUntil(lambda: bool(view.requested))
    assert len(view.requested) < 10
    assert second.total.text() == "1 / 1"
    bar = view.verticalScrollBar()
    before = bar.value()
    pos = QPointF(50, 50)
    touchpad = QWheelEvent(pos, QPointF(view.viewport().mapToGlobal(pos.toPoint())),
                           QPoint(0, -42), QPoint(), Qt.MouseButton.NoButton,
                           Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
    QApplication.sendEvent(view.viewport(), touchpad)
    assert touchpad.isAccepted() and bar.value() == before + 42
    bar.setValue(round(view.bottoms[0] * view.zoom) - view.viewport().height() // 2)
    qtbot.waitUntil(lambda: {0, 1}.issubset(view.requested))

    def point(page, x, y):
        return view.mapFromScene(view.pages[page].mapToScene(QPointF(x, y)))

    def key(key, modifiers=Qt.KeyboardModifier.NoModifier):
        qtbot.keyClick(view, key, modifiers)

    ctrl = Qt.KeyboardModifier.ControlModifier
    left = Qt.MouseButton.LeftButton
    qtbot.mouseClick(first.tools["pen"], left)
    first.width.setFocus()
    first.width.selectAll()
    qtbot.keyClicks(first.width, "7")
    qtbot.keyClick(first.width, Qt.Key.Key_Return)
    assert first.style.get("pen")["size"] == 7
    qtbot.mousePress(view.viewport(), left, pos=point(0, 80, 400))
    qtbot.mouseMove(view.viewport(), point(0, 100, 410))
    qtbot.mouseRelease(view.viewport(), left, pos=point(0, 100, 410))
    stroke, = view.ink[0].childItems()
    qtbot.mouseClick(first.tools["text"], left)
    qtbot.mouseClick(view.viewport(), left, pos=point(1, 65, 40))
    assert view.editing is not None
    qtbot.keyClicks(view, "Test annotation")
    key(Qt.Key.Key_Z, ctrl)
    assert view.stack.count() == 1 and stroke.scene() is view.scene()
    key(Qt.Key.Key_Z, ctrl | Qt.KeyboardModifier.ShiftModifier)
    assert view.editing.toPlainText() == "Test annotation"
    key(Qt.Key.Key_Escape)
    annotation, = view.ink[1].childItems()
    key(Qt.Key.Key_Z, ctrl)
    assert annotation.scene() is None and stroke.scene() is view.scene()
    key(Qt.Key.Key_Z, ctrl | Qt.KeyboardModifier.ShiftModifier)
    assert annotation.parentItem() is view.ink[1]
    undo, redo = window.image_actions[:2]
    assert undo.isEnabled() and redo.isEnabled()
    undo.trigger()
    assert annotation.scene() is None
    redo.trigger()
    assert annotation.parentItem() is view.ink[1]

    qtbot.mouseClick(first.tools["eraser"], left)
    qtbot.mouseClick(view.viewport(), left, pos=point(1, 85, 49))
    assert annotation.scene() is None
    key(Qt.Key.Key_Z, ctrl)
    assert annotation.parentItem() is view.ink[1]
    qtbot.mouseClick(panel.forward, left)
    qtbot.keyClick(second.canvas, Qt.Key.Key_Z, ctrl)
    assert annotation.parentItem() is view.ink[1]
    qtbot.mouseClick(panel.back, left)
    key(Qt.Key.Key_Z, ctrl | Qt.KeyboardModifier.ShiftModifier)
    assert annotation.scene() is None
    key(Qt.Key.Key_Z, ctrl)
    assert annotation.parentItem() is view.ink[1]
    pos = annotation.pos()
    view.set_zoom(view.zoom * 1.5)
    window.resize(950, 640)
    assert annotation.pos() == pos

    bar.setValue(bar.maximum())
    qtbot.waitUntil(lambda: 419 in view.requested)
    assert first.total.text() == "420 / 420"
    index = panel.tabs.currentIndex()
    key(Qt.Key.Key_Right)
    assert panel.tabs.currentIndex() == index and bar.value() == bar.maximum()
    panel.dock.toggle_floating()
    panel.dock.resize(panel.minimumWidth(), 500)
    qtbot.waitUntil(panel.dock.isFloating)
    for widget in (*first.tools.values(), first.width, first.undo, first.redo, first.zoom):
        assert widget.geometry().right() < first.rect().right()
    panel.dock.toggle_floating()
    assert len(view.requested) < 10
    # Reopening uses the same local settings and page coordinates.
    panel.close_tab(0)
    panel.open_paths([long_pdf])
    reopened = panel.tabs.currentWidget()
    qtbot.waitUntil(lambda: reopened.count == 420)
    restored, = reopened.canvas.ink[1].childItems()
    assert restored.toPlainText() == "Test annotation"
    assert restored.pos() == pos
    window.close()
