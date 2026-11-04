"""Shared controls: dropdown buttons, the editors' view bar, and title-bar window dragging."""
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QCheckBox, QToolButton, QWidget

from eng_viewer.ui.theme import ThemeManager
from eng_viewer.ui.view_options import ViewOptionsBar
from eng_viewer.ui.widgets import DropdownButton, is_drag_background, menu_button


def _render(widget):
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    widget.render(image)
    return image


def _ink_columns(image, background):
    """Columns holding pixels that differ clearly from the button's own background colour."""
    ptr = image.constBits()
    a = np.frombuffer(ptr, np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)[:, :image.width(), :3].astype(int)
    a = a[image.height() // 4: image.height() * 3 // 4]  # the label's rows, not the button's border
    diff = np.abs(a - np.array(background)).sum(axis=2)
    return np.nonzero((diff > 120).any(axis=0))[0]


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("text", ["Image", "View", "Offline subject selection", "Recent"])
def test_dropdown_text_and_arrow_never_overlap(qtbot, theme, dark, text):
    ThemeManager.instance().set_mode("dark" if dark else "light")
    try:
        for width in (None, 70, 48):
            button = DropdownButton(text)
            qtbot.addWidget(button)
            button.resize(button.sizeHint())
            if width is not None:
                button.resize(width, button.sizeHint().height())
            t = ThemeManager.current()
            from eng_viewer.ui.theme import qc
            bg = qc(t.control)
            image = _render(button)
            ink = _ink_columns(image, (bg.blue(), bg.green(), bg.red()))
            arrow = button._arrow_rect()
            text_ink = ink[(ink > 4) & (ink < arrow.left() - 1)]
            arrow_ink = ink[(ink >= arrow.left()) & (ink < button.width() - 2)]
            assert arrow_ink.size, "the chevron is drawn"
            # A visible gap separates the label from the chevron.
            if text_ink.size:
                assert arrow_ink.min() - text_ink.max() >= 4
            assert button.sizeHint().width() >= button.fontMetrics().horizontalAdvance(text) + DropdownButton.ARROW
    finally:
        ThemeManager.instance().set_mode("light")


def test_split_dropdown_click_and_menu(qtbot, theme):
    button = DropdownButton("Copy to Clipboard", "copy-image", split=True)
    qtbot.addWidget(button)
    button.resize(button.sizeHint())
    clicks = []
    button.clicked.connect(lambda: clicks.append(1))
    opened = []
    button.open_menu = lambda: opened.append(1)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(20, button.height() // 2))
    qtbot.mousePress(button, Qt.MouseButton.LeftButton, pos=QPoint(button.width() - 6, button.height() // 2))
    assert clicks == [1] and opened == [1]


def test_every_editor_menu_button_is_a_dropdown(qtbot, theme, render, settings):
    from PySide6.QtGui import QPixmap as Pix
    from eng_viewer.ui.main_window import MainWindow
    w = MainWindow(settings, render, Pix())
    qtbot.addWidget(w)
    for mode in ("image", "vector", "pdf_edit", "tools", "viewer"):
        w.set_mode(mode)
    for button in w.findChildren(QToolButton):
        if button.menu() is None or isinstance(button, DropdownButton):
            continue
        # Remaining menu buttons are icon-only ghost buttons whose arrow is hidden by design.
        assert button.property("variant") == "ghost", button.text()
        assert not button.text() or button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly or button.text().endswith("%")
    w.close()


@pytest.mark.parametrize("dark", [False, True])
def test_view_bar_never_squeezes_or_overlaps(qtbot, theme, dark):
    ThemeManager.instance().set_mode("dark" if dark else "light")
    try:
        bar = ViewOptionsBar([("grid", "Grid", ""), ("snap", "Snap", ""), ("rulers", "Rulers", "")], ["px", "mm", "cm", "in"])
        bar.status.setText("1,200 × 800 px · 96 DPI · 3 layers · Undo memory 12 MB, a long status that must give way")
        from eng_viewer.ui.widgets import DropdownButton as DB
        bar.add_leading(DB("100%", variant="ghost"))
        host = QWidget()
        bar.setParent(host)
        qtbot.addWidget(host)
        host.show()
        for width in (1400, 900, 700, 520, 360):
            host.resize(width, 40)
            bar.resize(width, bar.sizeHint().height())
            qtbot.wait(5)
            widgets = [bar.view_button] + ([bar.box(k) for k in ("grid", "snap", "rulers")] + [bar.units] if bar.inline_visible() else [])
            rects = [wd.geometry() if wd.parent() is bar else wd.geometry().translated(bar.inline.pos()) for wd in widgets]
            for i, a in enumerate(rects):
                assert a.right() < width
                for b in rects[i + 1:]:
                    assert not a.intersects(b)
            for key in ("grid", "snap", "rulers"):
                box = bar.box(key)
                if bar.inline_visible():
                    assert box.width() >= box.sizeHint().width()  # labels never clipped
        assert not bar.inline_visible()  # 360 px: toggles live in the View menu
        texts = [a.text() for a in bar.view_menu.actions()]
        assert {"Grid", "Snap", "Rulers", "Units"} <= set(texts)
        toggled = []
        bar.toggled.connect(lambda k, on: toggled.append((k, on)))
        bar.action("grid").trigger()
        assert toggled == [("grid", True)] and bar.box("grid").isChecked()
    finally:
        ThemeManager.instance().set_mode("light")


def test_title_bar_drag_areas(qtbot, theme, render, settings, monkeypatch):
    from PySide6.QtGui import QPixmap as Pix
    from eng_viewer.ui.main_window import MainWindow
    w = MainWindow(settings, render, Pix())
    qtbot.addWidget(w)
    w.resize(1300, 800)
    w.show()
    w.set_mode("image")
    bar = w.findChild(QWidget, "Toolbar")
    w._unified = True  # as on macOS, where the bar is the title bar

    def at(widget):
        return widget.mapTo(bar, widget.rect().center())
    # The empty title area (the document title label) drags; controls do not.
    assert bar.drag_area_at(at(w.title_label))
    assert not bar.drag_area_at(at(w.btn_library))
    assert not bar.drag_area_at(at(w.mode_ctrl))
    gap = QPoint(w.mode_ctrl.geometry().right() + 4, bar.height() // 2)
    assert bar.drag_area_at(gap)
    monkeypatch.setattr(w, "isFullScreen", lambda: True)
    assert not bar.drag_area_at(at(w.title_label))
    monkeypatch.undo()
    w._unified = False  # Windows / Linux: the system title bar does the dragging
    assert not bar.drag_area_at(at(w.title_label))
    from eng_viewer.platform.macos import title_bar_double_click_action
    assert title_bar_double_click_action() in ("zoom", "minimize", "none")
    w.close()


def test_is_drag_background_respects_selectable_text_and_opt_out(qtbot):
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit
    host = QWidget()
    row = QHBoxLayout(host)
    plain, selectable, field, opted = QLabel("Title"), QLabel("path"), QLineEdit(), QLabel("No drag")
    selectable.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    opted.setProperty("windowDrag", False)
    for wdg in (plain, selectable, field, opted):
        row.addWidget(wdg)
    qtbot.addWidget(host)
    host.resize(500, 40)
    host.show()
    center = lambda wdg: wdg.mapTo(host, wdg.rect().center())  # noqa: E731
    assert is_drag_background(host, center(plain))
    assert not is_drag_background(host, center(selectable))
    assert not is_drag_background(host, center(field))
    assert not is_drag_background(host, center(opted))


def _welcome_geometry(qapp, width, dark):
    from PySide6.QtGui import QIcon, QPixmap
    from PySide6.QtWidgets import QLabel

    from eng_viewer.ui.theme import ThemeManager
    from eng_viewer.ui.widgets import EmptyState

    ThemeManager(qapp, "dark" if dark else "light").apply()
    pm = QPixmap(256, 256)
    pm.fill()
    w = EmptyState(pm)
    w.resize(width, 600)
    w.show()
    qapp.processEvents()
    qapp.processEvents()
    icon = [lab for lab in w.findChildren(QLabel) if lab.pixmap() is not None and not lab.pixmap().isNull()][0]
    heading = [lab for lab in w.findChildren(QLabel) if lab.text() == "Open a folder of drawings"][0]
    sub = [lab for lab in w.findChildren(QLabel) if lab.text().startswith("PDFs in every")][0]
    return w, icon, heading, sub


def test_welcome_icon_clears_heading(qapp):
    import pytest

    for width, dark in ((900, False), (900, True), (520, False)):
        _w, icon, heading, sub = _welcome_geometry(qapp, width, dark)
        gap = heading.mapTo(heading.window(), heading.rect().topLeft()).y() - icon.mapTo(
            icon.window(), icon.rect().bottomLeft()
        ).y()
        assert gap >= 16, (width, dark, gap, icon.geometry(), heading.geometry(), sub.geometry(), _w._content.geometry())
        # labels are tall enough for their (stylesheet) text, so nothing is clipped
        assert heading.height() >= heading.sizeHint().height()
        assert sub.height() >= sub.heightForWidth(sub.width())
