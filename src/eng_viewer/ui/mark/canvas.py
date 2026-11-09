"""MARK canvas: the shared sharp page view plus annotation tools."""
from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QLineF, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QMouseEvent, QPainter, QPainterPath, QUndoStack
from PySide6.QtWidgets import QGraphicsItem, QGraphicsView

from ...core.render_service import RenderService
from ..page_view import PageView
from .commands import AddItems, ChangeStyle, EditText, MoveItems, RemoveItems, Renumber, ResizeItems, SetComment
from .items import (ANNOTATION, AnnotationMixin, ArrowItem, BubbleItem, EllipseItem, GhostItem, SelectionOverlay,
                    StrokeItem, TextItem, item_from_dict)

TOOLS = ("select", "pen", "highlighter", "text", "arrow", "circle", "bubble", "eraser")
TOOL_NAMES = {
    "select": "Select", "pen": "Pen", "highlighter": "Highlighter", "text": "Text",
    "arrow": "Arrow", "circle": "Circle", "bubble": "Callout", "eraser": "Eraser",
}


# Application-wide annotation clipboard: survives switching between drawings/pages.
_CLIPBOARD: dict = {"items": [], "n": 0, "src": None}
PASTE_OFFSET = 14.0
HANDLE_HIT_PX = 9.0
MIN_RESIZE = 2.0


def has_markup_clipboard() -> bool:
    return bool(_CLIPBOARD["items"])


class MarkCanvas(PageView):
    bubblesChanged = Signal()
    bubblePlaced = Signal(object)
    selectionChanged = Signal(list)
    toolChanged = Signal(str)
    statusMessage = Signal(str)

    def __init__(self, render: RenderService, tool_settings, parent=None):
        super().__init__(render, parent)
        self.left_drag_pans = False
        self.ts = tool_settings
        self.tool = "pen"
        self.undo_stack = QUndoStack(self)
        self.ghost = GhostItem()
        self.scene_obj.addItem(self.ghost)
        self._drawing = None
        self._shape_start: QPointF | None = None
        self._erasing: list | None = None
        self._move_snapshot: dict = {}
        self._editing_text = None
        self._size_preview_active = False
        self._pulse_anim = QVariantAnimation(self)
        self._pulse_anim.setDuration(700)
        self._pulse_anim.setStartValue(0.0)
        self._pulse_anim.setEndValue(1.0)
        self._pulse_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pulse_target: BubbleItem | None = None
        self._pulse_anim.valueChanged.connect(lambda v: self._pulse_target and self._pulse_target.set_pulse(float(v)))
        self._pulse_anim.finished.connect(lambda: self._pulse_target and self._pulse_target.set_pulse(0.0))
        self.overlay = SelectionOverlay()
        self.scene_obj.addItem(self.overlay)
        self._resize: dict | None = None
        self.scene_obj.selectionChanged.connect(self._on_selection)
        self.zoomChanged.connect(lambda _z: self._refresh_overlay())
        self.undo_stack.indexChanged.connect(lambda _i: self._refresh_overlay())
        self.setRubberBandSelectionMode(Qt.ItemSelectionMode.IntersectsItemShape)
        self.set_tool("pen")

    # ------------------------------------------------------------- queries
    def annotations(self) -> list:
        return [i for i in self.scene_obj.items() if i.data(0) == ANNOTATION and i.parentItem() is None]

    def bubbles(self) -> list[BubbleItem]:
        return sorted((i for i in self.annotations() if isinstance(i, BubbleItem)), key=lambda b: b.number)

    def selected_annotations(self) -> list:
        return [i for i in self.scene_obj.selectedItems() if i.data(0) == ANNOTATION]

    def next_number(self) -> int:
        nums = [b.number for b in self.bubbles()]
        return (max(nums) + 1) if nums else 1

    def has_markup(self) -> bool:
        return bool(self.annotations())

    def _scene_pos(self, event) -> QPointF:
        return self.viewportTransform().inverted()[0].map(event.position())

    def _page_rect(self) -> QRectF:
        ref = self.page_ref
        return QRectF(0, 0, ref.width, ref.height) if ref else QRectF()

    def _clamp(self, p: QPointF) -> QPointF:
        r = self._page_rect()
        return QPointF(min(max(p.x(), r.left()), r.right()), min(max(p.y(), r.top()), r.bottom()))

    # --------------------------------------------------------------- tools
    def set_tool(self, tool: str):
        if tool not in TOOLS:
            return
        self._finish_text_edit()
        self.tool = tool
        selecting = tool == "select"
        for it in self.annotations():
            it.set_interactive(selecting)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag if selecting else QGraphicsView.DragMode.NoDrag)
        self.refresh_ghost()
        self._update_cursor()
        self.toolChanged.emit(tool)

    def tool_cursor(self) -> QCursor:
        if self.tool == "select":
            return QCursor(Qt.CursorShape.ArrowCursor)
        if self.tool == "text":
            return QCursor(Qt.CursorShape.IBeamCursor)
        if self.tool in ("bubble",):
            return QCursor(Qt.CursorShape.ArrowCursor)
        return QCursor(Qt.CursorShape.CrossCursor)

    def refresh_ghost(self, at: QPointF | None = None, force_visible: bool | None = None):
        t = self.tool
        st = self.ts.get(t)
        if t == "bubble":
            self.ghost.configure("bubble", QColor("red"), st["size"], number=self.next_number(), fill=st.get("fill", False))
        elif t == "eraser":
            self.ghost.configure("eraser", QColor("black"), 0, screen_px=st["size"])
        elif t in ("pen", "highlighter", "text", "arrow", "circle"):
            self.ghost.configure(t, QColor(st["color"]), st["size"], alpha=st.get("alpha", 1.0))
        if at is not None:
            self.ghost.setPos(at)
        if force_visible is not None:
            self.ghost.setVisible(force_visible and t != "select")
        elif t == "select":
            self.ghost.setVisible(False)

    def show_size_preview(self, active: bool):
        """While a size slider moves, show the exact result in the middle of the view."""
        self._size_preview_active = active
        if active:
            center = self.viewportTransform().inverted()[0].map(QPointF(self.viewport().width() / 2, self.viewport().height() / 2))
            self.refresh_ghost(at=center, force_visible=True)
        else:
            self.ghost.setVisible(False)

    # ----------------------------------------------------- item management
    def attach_items(self, items):
        bubbles = False
        for it in items:
            if it.scene() is None:
                self.scene_obj.addItem(it)
            it.setVisible(True)
            it.set_interactive(self.tool == "select")
            bubbles = bubbles or isinstance(it, BubbleItem)
        if bubbles:
            self.bubblesChanged.emit()
            self.refresh_ghost()

    def detach_items(self, items):
        bubbles = False
        for it in items:
            it.setSelected(False)
            if it.scene() is self.scene_obj:
                self.scene_obj.removeItem(it)
            bubbles = bubbles or isinstance(it, BubbleItem)
        if bubbles:
            self.bubblesChanged.emit()
            self.refresh_ghost()

    def items_restyled(self, items):
        if any(isinstance(i, BubbleItem) for i in items):
            self.bubblesChanged.emit()

    def delete_selected(self) -> int:
        items = self.selected_annotations()
        if items:
            n_b = sum(isinstance(i, BubbleItem) for i in items)
            label = "Delete Callout" if n_b == len(items) == 1 else f"Delete {len(items)} Items" if len(items) > 1 else f"Delete {TOOL_NAMES.get(items[0].kind, 'Item')}"
            self.undo_stack.push(RemoveItems(self, items, label))
        return len(items)

    def delete_bubble(self, bubble: BubbleItem):
        self.undo_stack.push(RemoveItems(self, [bubble], f"Delete Callout {bubble.number}"))

    def clear_all(self):
        items = self.annotations()
        if items:
            self.undo_stack.push(RemoveItems(self, items, "Clear Markup"))

    def select_all(self):
        if self.tool != "select":
            self.set_tool("select")
        for it in self.annotations():
            it.setSelected(True)

    def set_comment(self, bubble: BubbleItem, text: str, removed: bool, label: str):
        self.undo_stack.push(SetComment(self, bubble, text, removed, label))

    def renumber(self):
        bubbles = sorted(self.bubbles(), key=lambda b: b.number)
        new = list(range(1, len(bubbles) + 1))
        if [b.number for b in bubbles] != new:
            self.undo_stack.push(Renumber(self, bubbles, new))

    def apply_style_to_selection(self, change: dict, label: str) -> bool:
        items = [i for i in self.selected_annotations() if self._style_applies(i, change)]
        if not items:
            return False
        before = [i.style() for i in items]
        after = []
        for i, b in zip(items, before):
            st = dict(b)
            for k, v in change.items():
                if k in st:
                    st[k] = v
            after.append(st)
        self.undo_stack.push(ChangeStyle(self, items, before, after, label))
        return True

    @staticmethod
    def _style_applies(item, change: dict) -> bool:
        return any(k in item.style() for k in change)

    def focus_bubble(self, bubble: BubbleItem, select: bool = True):
        if bubble.scene() is not self.scene_obj:
            return
        if select:
            self.scene_obj.blockSignals(True)
            self.scene_obj.clearSelection()
            if self.tool == "select":
                bubble.setSelected(True)
            self.scene_obj.blockSignals(False)
        vis = self._visible_scene_rect()
        if not vis.contains(bubble.sceneBoundingRect()):
            self.centerOn(bubble)
        self._pulse_anim.stop()
        if self._pulse_target is not None and self._pulse_target is not bubble:
            self._pulse_target.set_pulse(0.0)
        self._pulse_target = bubble
        self._pulse_anim.start()

    def _on_selection(self):
        try:
            sel = self.selected_annotations()
        except RuntimeError:
            return
        self.selectionChanged.emit(sel)
        self._refresh_overlay()

    # ----------------------------------------------- selection box + resize
    def _refresh_overlay(self):
        try:
            sel = self.selected_annotations() if self.tool == "select" else []
        except RuntimeError:
            return
        if not sel:
            self.overlay.setVisible(False)
            return
        box = QRectF()
        for i, it in enumerate(sel):
            r = it.geom_rect().normalized()
            box = r if i == 0 else box.united(r)
        self.overlay.set_box(box, self.zoom)
        self.overlay.setVisible(True)

    def items_geometry_changed(self):
        self._refresh_overlay()

    def _handle_at(self, view_pos: QPointF) -> str | None:
        if not self.overlay.isVisible() or self.tool != "select":
            return None
        best, best_d = None, HANDLE_HIT_PX
        for name, pt in self.overlay.handle_points().items():
            v = self.mapFromScene(pt)
            d = max(abs(v.x() - view_pos.x()), abs(v.y() - view_pos.y()))
            if d <= best_d:
                best, best_d = name, d
        return best

    _HANDLE_CURSORS = {
        "n": Qt.CursorShape.SizeVerCursor, "s": Qt.CursorShape.SizeVerCursor,
        "e": Qt.CursorShape.SizeHorCursor, "w": Qt.CursorShape.SizeHorCursor,
        "nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
        "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor,
    }

    def _begin_resize(self, handle: str, pos: QPointF):
        items = self.selected_annotations()
        box = QRectF()
        for i, it in enumerate(items):
            r = it.geom_rect().normalized()
            box = r if i == 0 else box.united(r)
        self._resize = {"h": handle, "start": QPointF(pos), "items": items, "old": box,
                        "before": [it.get_state() for it in items]}

    def _update_resize(self, pos: QPointF, mods):
        rs = self._resize
        old: QRectF = rs["old"]
        h = rs["h"]
        dx, dy = pos.x() - rs["start"].x(), pos.y() - rs["start"].y()
        l, t, r, b = old.left(), old.top(), old.right(), old.bottom()
        if "w" in h:
            l = min(l + dx, r - MIN_RESIZE)
        if "e" in h:
            r = max(r + dx, l + MIN_RESIZE)
        if "n" in h:
            t = min(t + dy, b - MIN_RESIZE)
        if "s" in h:
            b = max(b + dy, t + MIN_RESIZE)
        ow, oh = old.width(), old.height()
        if (mods & Qt.KeyboardModifier.ShiftModifier) and ow > 1e-6 and oh > 1e-6:
            nw, nh = r - l, b - t
            horiz, vert = ("e" in h or "w" in h), ("n" in h or "s" in h)
            if horiz and vert:
                k = max(nw / ow, nh / oh)
            elif horiz:
                k = nw / ow
            else:
                k = nh / oh
            nw, nh = ow * k, oh * k
            if horiz and vert:
                l, r = (r - nw, r) if "w" in h else (l, l + nw)
                t, b = (b - nh, b) if "n" in h else (t, t + nh)
            elif horiz:
                l, r = (r - nw, r) if "w" in h else (l, l + nw)
                cy = old.center().y()
                t, b = cy - nh / 2, cy + nh / 2
            else:
                t, b = (b - nh, b) if "n" in h else (t, t + nh)
                cx = old.center().x()
                l, r = cx - nw / 2, cx + nw / 2
        new = QRectF(QPointF(l, t), QPointF(r, b))
        for it, st in zip(rs["items"], rs["before"]):
            it.set_state(it.scaled_state(st, old, new))
        self._refresh_overlay()

    def _finish_resize(self):
        rs, self._resize = self._resize, None
        items = rs["items"]
        after = [it.get_state() for it in items]
        if any(a != b for a, b in zip(after, rs["before"])):
            label = f"Resize {len(items)} Items" if len(items) > 1 else f"Resize {TOOL_NAMES.get(items[0].kind, 'Item')}"
            self.undo_stack.push(ResizeItems(self, items, rs["before"], after, label))
            if any(isinstance(i, BubbleItem) for i in items):
                self.bubblesChanged.emit()
        self._refresh_overlay()

    @staticmethod
    def _shift_as_ctrl(event):
        """Shift-click toggles selection (Qt's native toggle key is Ctrl)."""
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier:
            mods = (mods & ~Qt.KeyboardModifier.ShiftModifier) | Qt.KeyboardModifier.ControlModifier
            return QMouseEvent(event.type(), event.position(), event.globalPosition(), event.button(),
                               event.buttons(), mods)
        return event

    # ------------------------------------------------------ copy and paste
    def copy_selection(self) -> int:
        items = self.selected_annotations()
        if not items:
            return 0
        items.sort(key=lambda i: (i.kind != "bubble", getattr(i, "number", 0), i.zValue()))
        _CLIPBOARD["items"] = [i.to_dict() for i in items]
        _CLIPBOARD["n"] = 0
        _CLIPBOARD["src"] = id(self)
        return len(items)

    def paste(self) -> int:
        if self.page_ref is None or not _CLIPBOARD["items"]:
            return 0
        self._finish_text_edit()
        fam = self.font().family()
        new = [it for it in (item_from_dict(d, fam) for d in _CLIPBOARD["items"]) if it is not None]
        if not new:
            return 0
        _CLIPBOARD["n"] += 1
        k = _CLIPBOARD["n"] if _CLIPBOARD["src"] == id(self) else _CLIPBOARD["n"] - 1
        off = PASTE_OFFSET * k
        base = self.next_number()
        for i, b in enumerate(sorted((n for n in new if isinstance(n, BubbleItem)), key=lambda b: b.number)):
            b.set_number(base + i)
        box = QRectF()
        for i, it in enumerate(new):
            it.moveBy(off, off)
            r = it.geom_rect().normalized()
            box = r if i == 0 else box.united(r)
        vis, page = self._visible_scene_rect(), self._page_rect()
        dx = dy = 0.0
        if not vis.intersects(box):
            c = vis.intersected(page).center() if vis.intersects(page) else page.center()
            dx, dy = c.x() - box.center().x(), c.y() - box.center().y()
        moved = box.translated(dx, dy)
        if moved.left() < page.left():
            dx += page.left() - moved.left()
        elif moved.right() > page.right():
            dx -= min(moved.right() - page.right(), moved.left() - page.left())
        if moved.top() < page.top():
            dy += page.top() - moved.top()
        elif moved.bottom() > page.bottom():
            dy -= min(moved.bottom() - page.bottom(), moved.top() - page.top())
        if dx or dy:
            for it in new:
                it.moveBy(dx, dy)
        if self.tool != "select":
            self.set_tool("select")
        self.scene_obj.clearSelection()
        self.undo_stack.push(AddItems(self, new, "Paste Markup" if len(new) > 1 else f"Paste {TOOL_NAMES.get(new[0].kind, 'Item')}"))
        for it in new:
            it.setSelected(True)
        return len(new)

    # --------------------------------------------------------------- mouse
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton or (event.button() == Qt.MouseButton.LeftButton and self._space_down):
            self.ghost.setVisible(False)
            super().mousePressEvent(event)
            return
        if event.button() != Qt.MouseButton.LeftButton or self.page_ref is None:
            QGraphicsView.mousePressEvent(self, event)
            return
        pos = self._scene_pos(event)
        tool = self.tool
        if tool == "select":
            handle = self._handle_at(event.position())
            if handle:
                self._begin_resize(handle, pos)
                event.accept()
                return
            QGraphicsView.mousePressEvent(self, self._shift_as_ctrl(event))
            self._move_snapshot = {it: it.pos() for it in self.selected_annotations()}
            return
        if tool == "text":
            hit = self._annotation_at(event)
            if isinstance(hit, TextItem):
                self._edit_text(hit)
                event.accept()
                return
        if not self._page_rect().contains(pos):
            event.accept()
            return
        st = self.ts.get(tool)
        color = QColor(st.get("color", "#E5202A"))
        if tool in ("pen", "highlighter"):
            item = StrokeItem(color, st["size"], highlighter=(tool == "highlighter"), opacity=st.get("alpha", 1.0))
            item.add_point(pos)
            self.scene_obj.addItem(item)
            self._drawing = item
        elif tool == "arrow":
            self._shape_start = pos
            self._drawing = ArrowItem(pos, pos, color, st["size"])
            self.scene_obj.addItem(self._drawing)
        elif tool == "circle":
            self._shape_start = pos
            self._drawing = EllipseItem(QRectF(pos, pos), color, st["size"])
            self.scene_obj.addItem(self._drawing)
        elif tool == "text":
            item = TextItem("", color, st["size"])
            item.setPos(pos - QPointF(2, st["size"] * 0.15))
            self.scene_obj.addItem(item)
            item.set_interactive(False)
            item._is_new = True
            self._edit_text(item)
        elif tool == "bubble":
            bubble = BubbleItem(self.next_number(), st["size"], st.get("fill", False), self.font().family())
            bubble.setPos(pos)
            self.undo_stack.push(AddItems(self, [bubble], f"Add Callout {bubble.number}"))
            self.bubblePlaced.emit(bubble)
        elif tool == "eraser":
            self._erasing = []
            self._erase_at(event)
        if tool not in ("bubble", "eraser"):
            self.ghost.setVisible(False)  # the stroke itself is the preview while drawing
        event.accept()

    def mouseMoveEvent(self, event):
        if self._panning:
            super().mouseMoveEvent(event)
            return
        pos = self._scene_pos(event)
        if self._resize is not None:
            self._update_resize(pos, event.modifiers())
            event.accept()
            return
        if self.tool == "select" and not event.buttons():
            h = self._handle_at(event.position())
            self.viewport().setCursor(QCursor(self._HANDLE_CURSORS[h]) if h else self.tool_cursor())
        if self._drawing is not None:
            p = self._clamp(pos)
            if isinstance(self._drawing, StrokeItem):
                last = self._drawing.points[-1]
                if QLineF(last, p).length() * self.zoom >= 1.2:
                    self._drawing.add_point(p)
            elif isinstance(self._drawing, ArrowItem):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    p = self._snap_angle(self._shape_start, p)
                self._drawing.set_end(p)
            elif isinstance(self._drawing, EllipseItem):
                self._drawing.set_rect(self._circle_rect(self._shape_start, p, event.modifiers()))
            event.accept()
            return
        if self._erasing is not None:
            self._erase_at(event)
            self.ghost.setPos(pos)
            event.accept()
            return
        if self.tool != "select" and not self._space_down and not self._size_preview_active:
            self.ghost.setPos(pos)
            self.ghost.setVisible(self._page_rect().contains(pos) or self.tool == "eraser")
        super().mouseMoveEvent(event)
        if self.tool == "select" and self._move_snapshot:
            self._refresh_overlay()

    def mouseReleaseEvent(self, event):
        if self._panning:
            super().mouseReleaseEvent(event)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            QGraphicsView.mouseReleaseEvent(self, event)
            return
        if self._resize is not None:
            self._finish_resize()
            event.accept()
            return
        if self._drawing is not None:
            item, self._drawing = self._drawing, None
            keep = True
            if isinstance(item, ArrowItem):
                keep = QLineF(item.p1, item.p2).length() * self.zoom >= 4
            elif isinstance(item, EllipseItem):
                if item.rect.width() * self.zoom < 4 and item.rect.height() * self.zoom < 4:
                    # A click places a standard circle centred on the point.
                    r = max(12.0, 18.0 / max(self.zoom, 1e-3))
                    c = item.rect.center()
                    item.set_rect(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r))
            if keep:
                self.undo_stack.push(AddItems(self, [item], f"Add {TOOL_NAMES[self.tool]}"))
            else:
                self.scene_obj.removeItem(item)
            self._shape_start = None
            event.accept()
            return
        if self._erasing is not None:
            items, self._erasing = self._erasing, None
            for it in items:
                it.setVisible(True)
            if items:
                self.undo_stack.push(RemoveItems(self, items, "Erase"))
            event.accept()
            return
        QGraphicsView.mouseReleaseEvent(self, self._shift_as_ctrl(event))
        if self.tool == "select" and self._move_snapshot:
            moved = [it for it, p in self._move_snapshot.items() if it.scene() is self.scene_obj and it.pos() != p]
            if moved:
                first = moved[0]
                delta = first.pos() - self._move_snapshot[first]
                self.undo_stack.push(MoveItems(moved, delta, "Move"))
            self._move_snapshot = {}
            self._refresh_overlay()

    def mouseDoubleClickEvent(self, event):
        hit = self._annotation_at(event)
        if isinstance(hit, TextItem):
            self._edit_text(hit)
            event.accept()
            return
        if isinstance(hit, BubbleItem):
            self.bubblePlaced.emit(hit)  # focus its comment
            event.accept()
            return
        QGraphicsView.mouseDoubleClickEvent(self, event)

    def leaveEvent(self, event):
        if not self._size_preview_active:
            self.ghost.setVisible(False)
        super().leaveEvent(event)

    def _annotation_at(self, event):
        for it in self.items(event.position().toPoint()):
            while it.parentItem() is not None:
                it = it.parentItem()
            if it.data(0) == ANNOTATION:
                return it
        return None

    @staticmethod
    def _snap_angle(a: QPointF, b: QPointF) -> QPointF:
        line = QLineF(a, b)
        ang = round(line.angle() / 15.0) * 15.0
        line.setAngle(ang)
        return line.p2()

    @staticmethod
    def _circle_rect(a: QPointF, b: QPointF, mods) -> QRectF:
        if mods & Qt.KeyboardModifier.AltModifier:
            dx, dy = b.x() - a.x(), b.y() - a.y()
            if mods & Qt.KeyboardModifier.ShiftModifier:
                d = max(abs(dx), abs(dy))
                dx, dy = d, d
            return QRectF(a.x() - abs(dx), a.y() - abs(dy), 2 * abs(dx), 2 * abs(dy))
        if mods & Qt.KeyboardModifier.ShiftModifier:
            d = max(abs(b.x() - a.x()), abs(b.y() - a.y()))
            b = QPointF(a.x() + math.copysign(d, b.x() - a.x() or 1), a.y() + math.copysign(d, b.y() - a.y() or 1))
        return QRectF(a, b).normalized()

    def _erase_at(self, event):
        pos = self._scene_pos(event)
        r = self.ts.get("eraser")["size"] / max(self.zoom, 1e-6)
        area = QPainterPath()
        area.addEllipse(pos, r, r)
        for it in self.scene_obj.items(area, Qt.ItemSelectionMode.IntersectsItemShape):
            top = it
            while top.parentItem() is not None:
                top = top.parentItem()
            if top.data(0) == ANNOTATION and top.isVisible() and top not in self._erasing:
                top.setVisible(False)
                self._erasing.append(top)

    # ----------------------------------------------------------------- text
    def _edit_text(self, item: TextItem):
        self._finish_text_edit(except_item=item)
        item._edit_before = item.toPlainText()
        item.on_edit_finished = self._text_done
        self._editing_text = item
        item.start_editing()

    def _finish_text_edit(self, except_item=None):
        cur = getattr(self, "_editing_text", None)
        if cur is not None and cur is not except_item and cur.textInteractionFlags() != Qt.TextInteractionFlag.NoTextInteraction:
            cur.clearFocus()

    def _text_done(self, item: TextItem):
        self._editing_text = None
        text = item.toPlainText()
        is_new = getattr(item, "_is_new", False)
        item._is_new = False
        if is_new:
            if text.strip():
                self.undo_stack.push(AddItems(self, [item], "Add Text"))
            else:
                self.scene_obj.removeItem(item)
        elif text != getattr(item, "_edit_before", text):
            if text.strip():
                self.undo_stack.push(EditText(item, item._edit_before, text))
            else:
                item.setPlainText(item._edit_before)
                self.undo_stack.push(RemoveItems(self, [item], "Delete Text"))
        item.set_interactive(self.tool == "select")

    # ------------------------------------------------------------- keyboard
    def keyPressEvent(self, event):
        editing = getattr(self, "_editing_text", None) is not None
        if editing:
            # Straight to the scene's text item: PageView would treat Space as "pan".
            QGraphicsView.keyPressEvent(self, event)
            return
        key = event.key()
        sel = self.selected_annotations()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and sel:
            self.delete_selected()
            event.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down) and sel:
            step = (10.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0)
            d = {Qt.Key.Key_Left: QPointF(-step, 0), Qt.Key.Key_Right: QPointF(step, 0),
                 Qt.Key.Key_Up: QPointF(0, -step), Qt.Key.Key_Down: QPointF(0, step)}[key]
            # Consecutive nudges merge into one undo step.
            self.undo_stack.push(MoveItems(sel, d, "Nudge", nudge=True))
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self._drawing is not None:
                self.scene_obj.removeItem(self._drawing)
                self._drawing = None
            elif sel:
                self.scene_obj.clearSelection()
            else:
                self.set_tool("select")
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if getattr(self, "_editing_text", None) is not None:
            QGraphicsView.keyReleaseEvent(self, event)
            return
        super().keyReleaseEvent(event)

    # --------------------------------------------------------------- output
    def render_annotations(self, image: QImage, page_w: float, page_h: float):
        """Paint the markup (vector) over ``image``, which covers the whole page."""
        p = QPainter(image)
        try:
            self.paint_annotations(p, QRectF(0, 0, image.width(), image.height()), page_w, page_h)
        finally:
            p.end()

    def paint_annotations(self, p: QPainter, target: QRectF, page_w: float, page_h: float):
        """Paint only the markup into ``target`` (an image, or a PDF page in points)."""
        ghost_visible = self.ghost.isVisible()
        self.ghost.setVisible(False)
        overlay_visible = self.overlay.isVisible()
        self.overlay.setVisible(False)
        self.page_item.setVisible(False)
        AnnotationMixin.exporting = True
        try:
            p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing | QPainter.RenderHint.SmoothPixmapTransform)
            self.scene_obj.render(p, target, QRectF(0, 0, page_w, page_h), Qt.AspectRatioMode.IgnoreAspectRatio)
        finally:
            AnnotationMixin.exporting = False
            self.page_item.setVisible(True)
            self.ghost.setVisible(ghost_visible)
            self.overlay.setVisible(overlay_visible)

    def comments_text(self) -> str:
        lines = []
        for b in sorted(self.bubbles(), key=lambda b: b.number):
            text = (b.comment or "").strip()
            if text and not b.comment_removed:
                lines.append(f"{b.number}. {text}")
        return "\n".join(lines)
