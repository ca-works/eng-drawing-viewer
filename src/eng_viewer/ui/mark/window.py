"""MARK workspace window: temporary markup over a page. The PDF is never modified."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QPointF, QSize, QSizeF, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QImage, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QSplitter, QToolButton,
    QVBoxLayout, QWidget,
)

from ... import APP_NAME
from ...core.render_service import PRI_EXPORT
from ...core.spell import WordListSpeller
from .. import icons
from ..main_window import Toolbar
from ..page_view import PageRef
from ..render_helpers import FullPageRender
from ..theme import IS_MAC
from ..widgets import (
    ElidedLabel, Toast, assign_mnemonics, key_sequences, label, primary_button, secondary_button, separator, tip,
    tool_button,
)
from .canvas import TOOL_NAMES, TOOLS, MarkCanvas
from .comments import CommentsPanel
from .items import ArrowItem, BubbleItem, EllipseItem, StrokeItem, TextItem
from .tool_options import RANGES, ColorSwatches, PreviewPopover, PreviewSlider, ToolSettings, format_size

TOOL_KEYS = {"select": "V", "pen": "P", "highlighter": "H", "text": "T", "arrow": "A", "circle": "O", "bubble": "B", "eraser": "E"}
TOOL_ICONS = {"select": "select", "pen": "pen", "highlighter": "highlighter", "text": "text", "arrow": "arrow",
              "circle": "circle", "bubble": "bubble", "eraser": "eraser"}
MARK_EXPORT_MIN_DPI = 300  # the original MARK always exported at 300 DPI or more

_SPELLER = None


def get_speller(settings):
    global _SPELLER
    if _SPELLER is None:
        if settings.get("spell_backend", "system") == "system":
            if IS_MAC:
                from ...platform.macos import make_speller

                _SPELLER = make_speller()
            elif sys.platform == "win32":
                from ...platform.windows_spell import make_speller

                _SPELLER = make_speller()
        if _SPELLER is None:
            from ...app import resource_path

            def remember(words: list[str]):
                settings.set("user_words", words)
                settings.save()

            _SPELLER = WordListSpeller(resource_path("english_words.txt"), set(settings.get("user_words") or []),
                                       on_learn=remember)
            _SPELLER.preload_async()
    return _SPELLER


def _item_tool(item) -> str:
    if isinstance(item, StrokeItem):
        return "highlighter" if item.highlighter else "pen"
    return {ArrowItem: "arrow", EllipseItem: "circle", TextItem: "text", BubbleItem: "bubble"}.get(type(item), "pen")


class MarkWindow(QMainWindow):
    def __init__(self, owner, render, settings, ref: PageRef, quality_dpi: int, thumbs=None, is_image: bool = False):
        super().__init__(None)
        self.is_image = is_image
        self.owner = owner
        self.render = render
        self.settings = settings
        self.ref = ref
        self.export_dpi = max(int(quality_dpi), MARK_EXPORT_MIN_DPI)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
        name = Path(ref.path).name
        self.setWindowTitle(f"MARK — {name}" if is_image else f"MARK — {name} — page {ref.page + 1}")
        self.ts = ToolSettings(settings.get("mark"))
        self.speller = get_speller(settings)
        self.popover = PreviewPopover()
        self._style_before = None
        self._base: QImage | None = None
        self._base_job: FullPageRender | None = None
        self._waiting_action = None
        self._unified = False

        self.canvas = MarkCanvas(render, self.ts)
        self.canvas.thin_lines = owner.view.thin_lines
        self.canvas.toolChanged.connect(self._on_tool_changed)
        self.canvas.selectionChanged.connect(self._on_selection)
        self.canvas.zoomChanged.connect(lambda pct: (self.zoom_btn.setText(f"{pct:.0f}%"), self.status_zoom.setText(f"{pct:.0f}%")))
        self.canvas.bubblesChanged.connect(self._update_actions)
        self.canvas.undo_stack.cleanChanged.connect(lambda _c: self._update_actions())
        self.canvas.undo_stack.indexChanged.connect(lambda _i: self._update_actions())

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar(name))
        root.addWidget(self._build_options())
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(1)
        self.canvas_box = QWidget()
        cb = QVBoxLayout(self.canvas_box)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.addWidget(self.canvas)
        split.addWidget(self.canvas_box)
        self.comments = CommentsPanel(self.canvas, self.speller)
        self.comments.setMinimumWidth(260)
        self.comments.copyRequested.connect(self.copy_comments)
        split.addWidget(self.comments)
        split.setStretchFactor(0, 1)
        split.setSizes([1100, 340])
        self.split = split
        root.addWidget(split, 1)
        root.addWidget(self._build_status())
        self.setCentralWidget(central)
        self.toast = Toast(self.canvas_box)
        self._build_menus()

        geo = settings.get("mark_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        else:
            scr = QGuiApplication.primaryScreen().availableGeometry()
            self.resize(min(1500, int(scr.width() * 0.9)), min(940, int(scr.height() * 0.9)))
        self._prepare_native()
        from .. import windows_chrome

        windows_chrome.track(self)

        self.canvas.set_page(ref, owner.thumbs.get(ref.path, ref.page, 160, visible=False) if owner is not None else None)
        self.canvas.set_tool(self.settings.get("mark_tool", "pen") if self.settings.get("mark_tool", "pen") in TOOLS else "pen")
        QTimer.singleShot(0, self.canvas.fit_width)
        QTimer.singleShot(400, self._prefetch_base)
        self._update_actions()

    # ================================================================ build
    def _prepare_native(self):
        if IS_MAC and not self._unified:
            from ...platform.macos import unify_title_bar

            size = self.size()
            self.winId()
            self._unified = unify_title_bar(self)
            self.resize(size)
            self._toolbar_layout.setContentsMargins(84 if self._unified else 10, 0, 10, 0)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == event.Type.WindowStateChange:
            self._toolbar_layout.setContentsMargins(84 if (self._unified and not self.isFullScreen()) else 10, 0, 10, 0)

    def _build_toolbar(self, name: str) -> QWidget:
        bar = Toolbar()
        bar.setFixedHeight(52)
        lay = QHBoxLayout(bar)
        self._toolbar_layout = lay
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(3)
        self.btn_undo = tool_button("undo", tip("Undo", QKeySequence.StandardKey.Undo))
        self.btn_undo.clicked.connect(self.canvas.undo_stack.undo)
        lay.addWidget(self.btn_undo)
        self.btn_redo = tool_button("redo", tip("Redo", QKeySequence.StandardKey.Redo))
        self.btn_redo.clicked.connect(self.canvas.undo_stack.redo)
        lay.addWidget(self.btn_redo)
        lay.addWidget(separator(True))
        self.tool_buttons: dict[str, QToolButton] = {}
        for t in TOOLS:
            b = tool_button(TOOL_ICONS[t], tip(TOOL_NAMES[t], TOOL_KEYS[t]), checkable=True, size=32, icon_size=19)
            b.clicked.connect(lambda _=False, t=t: self.set_tool(t))
            self.tool_buttons[t] = b
            lay.addWidget(b)
        lay.addWidget(separator(True))
        title = ElidedLabel(f"{name}  ·  page {self.ref.page + 1}", Qt.TextElideMode.ElideMiddle)
        title.setProperty("role", "secondary")
        title.setToolTip(self.ref.path)
        lay.addWidget(title, 1)
        self.btn_zoom_out = tool_button("zoom-out", tip("Zoom Out", QKeySequence.StandardKey.ZoomOut))
        self.btn_zoom_out.clicked.connect(self.canvas.zoom_out)
        lay.addWidget(self.btn_zoom_out)
        self.zoom_btn = QToolButton()
        self.zoom_btn.setProperty("variant", "ghost")
        self.zoom_btn.setText("100%")
        self.zoom_btn.setFixedSize(58, 28)
        self.zoom_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        zm = QMenu(self.zoom_btn)
        for pct in (25, 50, 75, 100, 150, 200, 300, 400, 800):
            zm.addAction(f"{pct}%", lambda p=pct: self.canvas.set_zoom_percent(p))
        zm.addSeparator()
        zm.addAction("Fit Width", self.canvas.fit_width)
        zm.addAction("Fit Page", self.canvas.fit_page)
        self.zoom_btn.setMenu(zm)
        lay.addWidget(self.zoom_btn)
        b = tool_button("zoom-in", tip("Zoom In", QKeySequence.StandardKey.ZoomIn))
        b.clicked.connect(self.canvas.zoom_in)
        lay.addWidget(b)
        b = tool_button("fit-width", tip("Fit Width", "Ctrl+1"))
        b.clicked.connect(self.canvas.fit_width)
        lay.addWidget(b)
        b = tool_button("fit-page", tip("Fit Page", "Ctrl+2"))
        b.clicked.connect(self.canvas.fit_page)
        lay.addWidget(b)
        lay.addWidget(separator(True))
        self.btn_queue = tool_button("queue-add", tip("Send Marked Page to PowerPoint Queue", "Ctrl+D"), text="Queue")
        self.btn_queue.clicked.connect(self.send_to_queue)
        lay.addWidget(self.btn_queue)
        self.btn_pdf = tool_button("save", tip("Save Marked Page as PDF", "Ctrl+S"), text="PDF")
        self.btn_pdf.clicked.connect(self.save_pdf)
        lay.addWidget(self.btn_pdf)
        lay.addSpacing(4)
        self.btn_copy = primary_button("Copy Marked Page", "copy-image")
        self.btn_copy.setToolTip(tip("Copy the marked page as a high-resolution image", QKeySequence.StandardKey.Copy))
        self.btn_copy.clicked.connect(self.copy_marked_page)
        lay.addWidget(self.btn_copy)
        return bar

    def _build_options(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("OptionsBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setFixedHeight(42)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 12, 0)
        lay.setSpacing(10)
        self.tool_label = label("", "title")
        self.tool_label.setMinimumWidth(80)
        lay.addWidget(self.tool_label)
        self.color_box = QWidget()
        cl = QHBoxLayout(self.color_box)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        cl.addWidget(label("Colour", "secondary"))
        self.swatches = ColorSwatches()
        self.swatches.colorChosen.connect(self._color_chosen)
        cl.addWidget(self.swatches)
        lay.addWidget(self.color_box)
        self.size_box = QWidget()
        sl = QHBoxLayout(self.size_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)
        self.size_label = label("Thickness", "secondary")
        sl.addWidget(self.size_label)
        self.size_slider = PreviewSlider(0.25, 24, 2, width=170)
        self.size_slider.valueChanged.connect(self._size_changed)
        self.size_slider.pressed.connect(lambda: self._preview(True))
        self.size_slider.released.connect(lambda: self._preview(False))
        self.size_slider.hovered.connect(self._slider_hover)
        sl.addWidget(self.size_slider)
        self.size_value = label("", "secondary")
        self.size_value.setMinimumWidth(52)
        sl.addWidget(self.size_value)
        lay.addWidget(self.size_box)
        self.alpha_box = QWidget()
        al = QHBoxLayout(self.alpha_box)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(8)
        al.addWidget(label("Opacity", "secondary"))
        self.alpha_slider = PreviewSlider(0.10, 0.80, 0.35, width=110, log=False)
        self.alpha_slider.valueChanged.connect(self._alpha_changed)
        self.alpha_slider.pressed.connect(lambda: self._preview(True))
        self.alpha_slider.released.connect(lambda: self._preview(False))
        al.addWidget(self.alpha_slider)
        self.alpha_value = label("", "secondary")
        self.alpha_value.setMinimumWidth(36)
        al.addWidget(self.alpha_value)
        lay.addWidget(self.alpha_box)
        self.fill_btn = tool_button("fill", "Fill callout bubbles with white (hides drawing lines behind the number)", checkable=True, text="White fill", size=28, icon_size=16)
        self.fill_btn.toggled.connect(self._fill_toggled)
        lay.addWidget(self.fill_btn)
        self.next_label = label("", "secondary")
        lay.addWidget(self.next_label)
        lay.addStretch(1)
        self.sel_label = label("", "secondary")
        lay.addWidget(self.sel_label)
        self.btn_delete = tool_button("trash", tip("Delete Selected", "Delete"), text="Delete")
        self.btn_delete.clicked.connect(self.canvas.delete_selected)
        lay.addWidget(self.btn_delete)
        self.btn_clear = tool_button("x", "Clear all markup", text="Clear All")
        self.btn_clear.clicked.connect(self.clear_all)
        lay.addWidget(self.btn_clear)
        return bar

    def _build_status(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("StatusStrip")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setFixedHeight(24)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 12, 0)
        pan = "Space-drag or middle-drag to pan" + (" · pinch or ⌘-scroll to zoom" if IS_MAC else " · Ctrl+scroll to zoom")
        self.status_hint = label(f"{pan} · Shift constrains arrows and circles · Markup never changes the PDF", "tertiary")
        lay.addWidget(self.status_hint, 1)
        self.status_zoom = label("", "tertiary")
        lay.addWidget(self.status_zoom)
        return bar

    def _build_menus(self):
        mb = self.menuBar()

        def act(menu, text, slot, shortcut=None, checkable=False):
            a = QAction(text, self)
            if shortcut is not None:
                a.setShortcuts(key_sequences(shortcut))
            a.setCheckable(checkable)
            a.triggered.connect(lambda _=False: slot())
            menu.addAction(a)
            return a

        f = mb.addMenu("&File")
        self.act_queue = act(f, "Send to PowerPoint Queue", self.send_to_queue, "Ctrl+D")
        self.act_save_pdf = act(f, "Save Marked Page as PDF…", self.save_pdf, "Ctrl+S")
        self.act_save = act(f, "Save Marked Page as PNG…", self.save_png, "Ctrl+Shift+S")
        f.addSeparator()
        act(f, "Close MARK", self.close, QKeySequence.StandardKey.Close)

        e = mb.addMenu("&Edit")
        self.act_undo = self.canvas.undo_stack.createUndoAction(self, "Undo")
        self.act_undo.setShortcuts(key_sequences(QKeySequence.StandardKey.Undo))
        e.addAction(self.act_undo)
        self.act_redo = self.canvas.undo_stack.createRedoAction(self, "Redo")
        # Windows' standard Redo keys already include Ctrl+Y; Ctrl+Shift+Z is added for everyone.
        self.act_redo.setShortcuts(key_sequences([QKeySequence.StandardKey.Redo, "Ctrl+Shift+Z"]))
        e.addAction(self.act_redo)
        e.addSeparator()
        self.act_copy = act(e, "Copy Marked Page", self.copy_marked_page, QKeySequence.StandardKey.Copy)
        self.act_copy_comments = act(e, "Copy Numbered Comments", self.copy_comments, "Ctrl+Shift+C")
        e.addSeparator()
        self.act_delete = act(e, "Delete", self.canvas.delete_selected)
        act(e, "Select All Markup", self.canvas.select_all, QKeySequence.StandardKey.SelectAll)
        e.addSeparator()
        self.act_clear = act(e, "Clear All Markup…", self.clear_all)

        v = mb.addMenu("&View")
        act(v, "Zoom In", self.canvas.zoom_in, [QKeySequence.StandardKey.ZoomIn, "Ctrl+="])
        act(v, "Zoom Out", self.canvas.zoom_out, QKeySequence.StandardKey.ZoomOut)
        act(v, "Actual Size", self.canvas.actual_size, "Ctrl+0")
        act(v, "Fit Width", self.canvas.fit_width, "Ctrl+1")
        act(v, "Fit Page", self.canvas.fit_page, "Ctrl+2")

        t = mb.addMenu("&Tools")
        g = QActionGroup(self)
        self.tool_actions = {}
        for tool in TOOLS:
            a = act(t, TOOL_NAMES[tool], lambda tool=tool: self.set_tool(tool), TOOL_KEYS[tool], checkable=True)
            g.addAction(a)
            self.tool_actions[tool] = a
        t.addSeparator()
        act(t, "Smaller", lambda: self._nudge_size(-1), "[")
        act(t, "Larger", lambda: self._nudge_size(1), "]")
        t.addSeparator()
        self.act_renumber = act(t, "Renumber Callouts 1…n", self.canvas.renumber)
        if IS_MAC:
            w = mb.addMenu("Window")
            act(w, "Minimize", self.showMinimized, "Ctrl+M")
        else:
            assign_mnemonics(mb)

    # ================================================================ tools
    def set_tool(self, tool: str):
        self.canvas.set_tool(tool)

    def _on_tool_changed(self, tool: str):
        for t, b in self.tool_buttons.items():
            b.setChecked(t == tool)
        if tool in self.tool_actions:
            self.tool_actions[tool].setChecked(True)
        self.settings.set("mark_tool", tool)
        self._sync_options()

    def _options_tool(self) -> str:
        """Which tool's options the bar edits: the selection's kind in Select mode."""
        if self.canvas.tool == "select":
            sel = self.canvas.selected_annotations()
            return _item_tool(sel[0]) if sel else "select"
        return self.canvas.tool

    def _sync_options(self):
        tool = self._options_tool()
        canvas_tool = self.canvas.tool
        sel = self.canvas.selected_annotations()
        self.tool_label.setText(TOOL_NAMES.get(canvas_tool, ""))
        st = dict(self.ts.get(tool))
        if canvas_tool == "select" and sel:
            st.update(self._style_as_settings(sel[0]))
        has_color = tool in ("pen", "highlighter", "text", "arrow", "circle")
        self.color_box.setVisible(has_color)
        if has_color:
            self.swatches.set_current(st.get("color"))
        has_size = tool in RANGES
        self.size_box.setVisible(has_size)
        if has_size:
            lo, hi, unit, text = RANGES[tool]
            self.size_label.setText(text)
            self.size_slider.set_range(lo, hi, st["size"])
            self.size_value.setText(format_size(st["size"], unit))
        self.alpha_box.setVisible(tool == "highlighter")
        if tool == "highlighter":
            self.alpha_slider.set_value(st.get("alpha", 0.35))
            self.alpha_value.setText(f"{st.get('alpha', 0.35) * 100:.0f}%")
        self.fill_btn.setVisible(tool == "bubble")
        self.fill_btn.blockSignals(True)
        self.fill_btn.setChecked(bool(st.get("fill")))
        self.fill_btn.blockSignals(False)
        self.next_label.setVisible(canvas_tool == "bubble")
        self.next_label.setText(f"Next callout: {self.canvas.next_number()}")
        n = len(sel)
        self.sel_label.setText(f"{n} selected" if n else ("Click or drag to select markup" if canvas_tool == "select" else ""))
        self.btn_delete.setVisible(canvas_tool == "select")
        self.btn_delete.setEnabled(n > 0)

    @staticmethod
    def _style_as_settings(item) -> dict:
        s = item.style()
        out = {}
        if "color" in s:
            out["color"] = s["color"].name()
        if "width" in s:
            out["size"] = s["width"]
        if "size" in s:
            out["size"] = s["size"]
        if "alpha" in s:
            out["alpha"] = s["alpha"]
        if "fill" in s:
            out["fill"] = s["fill"]
        return out

    def _on_selection(self, _items):
        self._sync_options()
        self._update_actions()

    def _persist_tools(self):
        self.settings.set("mark", self.ts.to_dict())

    def _selection_edit(self, change: dict, label: str, live: bool) -> bool:
        if self.canvas.tool != "select" or not self.canvas.selected_annotations():
            return False
        if live:
            for it in self.canvas.selected_annotations():
                st = it.style()
                if any(k in st for k in change):
                    it.set_style({**st, **{k: v for k, v in change.items() if k in st}})
        else:
            self.canvas.apply_style_to_selection(change, label)
        return True

    def _color_chosen(self, color):
        tool = self._options_tool()
        if not self._selection_edit({"color": color}, "Change Colour", live=False):
            self.ts.set(tool, "color", color.name())
            self._persist_tools()
            self.canvas.refresh_ghost()

    def _size_key(self, item) -> str:
        return "width" if isinstance(item, (StrokeItem, ArrowItem, EllipseItem)) else "size"

    def _size_changed(self, v: float):
        tool = self._options_tool()
        lo, hi, unit, _ = RANGES.get(tool, (0, 1, "pt", ""))
        self.size_value.setText(format_size(v, unit))
        sel = self.canvas.selected_annotations() if self.canvas.tool == "select" else []
        if sel:
            key = self._size_key(sel[0])
            self._selection_edit({key: v}, "Change Size", live=True)
        else:
            self.ts.set(tool, "size", v)
            self._persist_tools()
        self._show_popover()
        if self.canvas.tool != "select":
            self.canvas.show_size_preview(True) if self._previewing else self.canvas.refresh_ghost()

    def _alpha_changed(self, v: float):
        self.alpha_value.setText(f"{v * 100:.0f}%")
        if not self._selection_edit({"alpha": v}, "Change Opacity", live=True):
            self.ts.set("highlighter", "alpha", v)
            self._persist_tools()
        self._show_popover()
        if self._previewing and self.canvas.tool != "select":
            self.canvas.show_size_preview(True)

    def _fill_toggled(self, on: bool):
        if not self._selection_edit({"fill": on}, "Change Fill", live=False):
            self.ts.set("bubble", "fill", on)
            self._persist_tools()
            self.canvas.refresh_ghost()

    _previewing = False

    def _preview(self, active: bool):
        self._previewing = active
        if active:
            sel = self.canvas.selected_annotations() if self.canvas.tool == "select" else []
            self._style_before = [i.style() for i in sel] if sel else None
            if self.canvas.tool != "select":
                self.canvas.show_size_preview(True)
            self._show_popover()
        else:
            if self._style_before is not None:
                sel = self.canvas.selected_annotations()
                after = [i.style() for i in sel]
                if after != self._style_before:
                    from .commands import ChangeStyle

                    self.canvas.undo_stack.push(ChangeStyle(self.canvas, sel, self._style_before, after, "Change Size"))
                self._style_before = None
            self.canvas.show_size_preview(False)
            self.popover.linger(900)

    def _slider_hover(self, on: bool):
        if on:
            self._show_popover()
        elif not self._previewing:
            self.popover.linger(250)

    def _show_popover(self):
        tool = self._options_tool()
        if tool not in RANGES:
            return
        st = dict(self.ts.get(tool))
        sel = self.canvas.selected_annotations() if self.canvas.tool == "select" else []
        if sel:
            st.update(self._style_as_settings(sel[0]))
        number = sel[0].number if sel and isinstance(sel[0], BubbleItem) else self.canvas.next_number()
        unit = RANGES[tool][2]
        self.popover.present(self.size_slider, tool, st, self.canvas.zoom, self.canvas.zoom_percent(), number, unit)

    def _nudge_size(self, direction: int):
        if self._options_tool() not in RANGES:
            return
        self._preview(True)
        self.size_slider.step(direction)
        QTimer.singleShot(0, lambda: self._preview(False))

    # ============================================================== actions
    def _update_actions(self):
        has = self.canvas.has_markup()
        sel = bool(self.canvas.selected_annotations())
        self.btn_undo.setEnabled(self.canvas.undo_stack.canUndo())
        self.btn_redo.setEnabled(self.canvas.undo_stack.canRedo())
        self.btn_clear.setEnabled(has)
        self.act_clear.setEnabled(has)
        self.act_delete.setEnabled(sel)
        self.act_copy_comments.setEnabled(bool(self.canvas.bubbles()))
        self.act_renumber.setEnabled(bool(self.canvas.bubbles()))
        if self.canvas.tool == "bubble":
            self.next_label.setText(f"Next callout: {self.canvas.next_number()}")

    def clear_all(self):
        if not self.canvas.has_markup():
            return
        self.canvas.clear_all()
        self.toast.show_message("Markup cleared — Undo restores it", "undo")

    def copy_comments(self):
        text = self.canvas.comments_text()
        QApplication.clipboard().setText(text)
        n = len(text.splitlines()) if text else 0
        self.toast.show_message(f"Copied {n} numbered comment{'s' if n != 1 else ''}" if n else "No comments to copy yet", "clipboard-text")

    def _prefetch_base(self):
        if self._base is not None or self._base_job is not None:
            return
        job = FullPageRender(self.render, self.ref.path, self.ref.page, self.ref.width, self.ref.height,
                             self.export_dpi, True, priority=PRI_EXPORT, parent=self, native=self.is_image)
        self._base_job = job
        job.finished.connect(self._base_ready)
        job.failed.connect(self._base_failed)
        job.start()

    def _base_ready(self, img: QImage):
        self._base = img
        self._base_job = None
        action, self._waiting_action = self._waiting_action, None
        if action:
            action()

    def _base_failed(self, msg: str):
        self._base_job = None
        self._waiting_action = None
        self.toast.show_message(f"Could not render the page: {msg}", "warning", "error", 4000)

    def _with_composite(self, action):
        """Run ``action(image)`` once the high-resolution base page is available."""
        def run():
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                img = self._base.copy()
                self.canvas.render_annotations(img, self.ref.width, self.ref.height)
            finally:
                QApplication.restoreOverrideCursor()
            action(img)

        if self._base is not None:
            run()
        else:
            self._waiting_action = run
            self.toast.show_message("Preparing the high-resolution page…", "copy-image", msec=30000)
            self._prefetch_base()

    def copy_marked_page(self):
        def done(img):
            QApplication.clipboard().setImage(img)
            self.canvas.undo_stack.setClean()
            self.toast.show_message(f"Marked page copied at {self._effective_dpi(img):.0f} DPI — the PDF is unchanged", "check")

        self._with_composite(done)

    def _effective_dpi(self, img: QImage) -> float:
        return img.width() / max(self.ref.width, 1e-3) * 72.0

    def send_to_queue(self):
        def done(img):
            text = self.canvas.comments_text()
            count = len(text.splitlines()) if text else 0
            self.owner.add_marked_page(self.ref.path, self.ref.page, img, text, count)
            self.canvas.undo_stack.setClean()
            self.toast.show_message("Marked page and numbered comments added to the PowerPoint queue", "queue-add")

        self._with_composite(done)

    def save_png(self):
        default = self._default_name(".png")
        path, _ = QFileDialog.getSaveFileName(self, "Save marked page as PNG", default, "PNG image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"

        def done(img):
            ok = img.save(path, "PNG")
            if ok:
                self.canvas.undo_stack.setClean()
            self.toast.show_message(f"Saved {Path(path).name}" if ok else "Could not save the image", "check" if ok else "warning",
                                    "info" if ok else "error")

        self._with_composite(done)

    def _default_name(self, ext: str) -> str:
        stem = Path(self.ref.path).stem
        name = f"{stem}_marked{ext}" if self.is_image else f"{stem}_page{self.ref.page + 1}_marked{ext}"
        return str(Path(self.settings.get("export_dir") or Path(self.ref.path).parent) / name)

    def save_pdf(self):
        """The page plus the markup as vectors in a new PDF; the original file is untouched."""
        from ...core.tool_support import unique_path

        default = str(unique_path(Path(self._default_name(".pdf"))))
        path, _ = QFileDialog.getSaveFileName(self, "Save marked page as PDF", default, "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        if Path(path).resolve() == Path(self.ref.path).resolve():
            QMessageBox.warning(self, "Choose another name", "The original file is never overwritten. Choose a different name.")
            return
        self.settings.set("export_dir", str(Path(path).parent))
        import tempfile

        from PySide6.QtCore import QMarginsF, QRectF
        from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter

        fd, overlay = tempfile.mkstemp(prefix="eng-mark-", suffix=".pdf")
        import os

        os.close(fd)
        w, h = self.ref.width, self.ref.height
        writer = QPdfWriter(overlay)
        writer.setResolution(72)
        writer.setCreator(APP_NAME)
        writer.setPageLayout(QPageLayout(QPageSize(QSizeF(w, h), QPageSize.Unit.Point, "", QPageSize.SizeMatchPolicy.ExactMatch),
                                         QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Point))
        painter = QPainter(writer)
        try:
            self.canvas.paint_annotations(painter, QRectF(0, 0, w, h), w, h)
        finally:
            painter.end()

        def done(res):
            try:
                os.remove(overlay)
            except OSError:
                pass
            if res.ok:
                self.canvas.undo_stack.setClean()
                self.toast.show_message(f"Saved {Path(path).name} — the original is unchanged", "check")
                if self.owner is not None:
                    self.owner.outputs_created([path])
            else:
                self.toast.show_message(f"Could not save the PDF: {res.error}", "warning", "error", 4000)

        self.render.submit("tool", {"op": "mark_pdf", "source": self.ref.path, "page": self.ref.page,
                                    "overlay_pdf": overlay, "output": path}, done, priority=PRI_EXPORT, dedupe=False)

    # ================================================================ close
    def closeEvent(self, event):
        if getattr(self, "_closed", False):
            # A second close request while the window is being deleted (Ctrl+W pressed twice,
            # the app quitting): everything below has already been done.
            super().closeEvent(event)
            return
        if self.canvas.has_markup() and not self.canvas.undo_stack.isClean():
            box = QMessageBox(QMessageBox.Icon.Warning, "Discard this markup?",
                              "Your markup has not been copied, saved or sent to the PowerPoint queue. "
                              "MARK annotations are temporary and are never written into the PDF.",
                              QMessageBox.StandardButton.Cancel, self)
            discard = box.addButton("Discard Markup", QMessageBox.ButtonRole.DestructiveRole)
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not discard:
                event.ignore()
                return
        self.settings.set("mark_geometry", bytes(self.saveGeometry().toBase64()).decode())
        self._persist_tools()
        self.settings.save()
        if self._base_job is not None:
            self._base_job.cancel()
        # Children (and the undo stack) are destroyed next; stop their signals
        # from reaching handlers that read the already-deleted scene.
        self.canvas.undo_stack.blockSignals(True)
        self.canvas.scene_obj.blockSignals(True)
        self.canvas.release()
        self.popover.close()
        self.popover.deleteLater()
        self._closed = True
        super().closeEvent(event)
