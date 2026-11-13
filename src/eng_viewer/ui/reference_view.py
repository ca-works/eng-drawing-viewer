"""Continuous PDF and annotation canvas for the Windows Reference dock."""
from bisect import bisect_left, bisect_right

from PySide6.QtCore import QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QCursor, QPen, QUndoStack
from PySide6.QtWidgets import QGraphicsItem, QGraphicsRectItem, QGraphicsView

from ..core.render_service import PRI_PREVIEW
from .mark.commands import AddItems, EditText, RemoveItems
from .mark.items import StrokeItem, TextItem
from .page_view import PAGE_MARGIN_PX, PageView, PdfPageItem
from .theme import ThemeManager, qc


class PageInk(QGraphicsRectItem):
    def __init__(self, page):
        super().__init__(page.boundingRect(), page)
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape)

    def attach_items(self, items):
        for item in items:
            item.setParentItem(self)

    def detach_items(self, items):
        for item in items:
            self.scene().removeItem(item)


class ContinuousReferenceView(PageView):
    historyChanged = Signal(bool, bool)
    visiblePageChanged = Signal(int)

    def __init__(self, render, settings):
        self.mode = "pan"
        self.pages = []
        super().__init__(render)
        self.scene_obj.removeItem(self.page_item)
        self.settings = settings
        self.ink = {}
        self.stack = QUndoStack(self)
        self.stack.setUndoLimit(150)
        self.stack.indexChanged.connect(self._history_changed)
        self.stroke = None
        self.editing = None
        self.requested = set()
        self.current_page = 0
        self.verticalScrollBar().valueChanged.connect(self._update_visible_page)
        self.verticalScrollBar().sliderPressed.connect(self._scroll_anim.stop)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

    def set_pages(self, refs):
        self._ref = refs[0]
        self.page_width = max(ref.width for ref in refs)
        self.tops, self.bottoms = [], []
        top = 0.0
        for ref in refs:
            page = PdfPageItem(self)
            page.set_ref(ref)
            page.setPos((self.page_width - ref.width) / 2, top)
            self.scene_obj.addItem(page)
            self.pages.append(page)
            self.tops.append(top)
            self.bottoms.append(top + ref.height)
            top += ref.height + 12
        self._apply_fit(self.fit_mode or "width")
        self._center_on_page_top()
        self._update_visible_page()
        self._schedule_tiles(immediate=True)

    def _update_scene_rect(self):
        if self.pages:
            margin = PAGE_MARGIN_PX / self.zoom
            rect = QRectF(0, 0, self.page_width, self.bottoms[-1])
            self.scene_obj.setSceneRect(rect.adjusted(-margin, -margin, margin, margin))

    def _update_visible_page(self, *_):
        if self.pages:
            rect = self._visible_scene_rect()
            index = min(bisect_right(self.bottoms, rect.top() + rect.height() * 0.25), len(self.pages) - 1)
            if index != self.current_page:
                self.current_page = index
                self.visiblePageChanged.emit(index)

    def _fit_zoom(self, mode):
        full = self.maximumViewportSize()
        width = full.width() - 2 * PAGE_MARGIN_PX - 2
        height = max(1, full.height() - 2 * PAGE_MARGIN_PX)
        if len(self.pages) > 1 or self.bottoms[-1] * width / self.page_width > height:
            width -= self.verticalScrollBar().sizeHint().width()
        zoom = max(1, width) / self.page_width
        if mode == "page":
            zoom = min(zoom, height / self.pages[self.current_page].ref.height)
        return zoom

    def _apply_fit(self, mode, animate=False):
        self._set_fit_mode(mode)
        if self.pages:
            self._zoom_anim.stop()
            self._set_zoom_raw(self._fit_zoom(mode), QPointF(self.viewport().rect().center()))
            self._schedule_tiles(settle=True)

    def resizeEvent(self, event):
        anchor = QPointF(self.viewport().rect().center())
        scene_point = self.viewportTransform().inverted()[0].map(anchor)
        QGraphicsView.resizeEvent(self, event)
        if self.pages:
            self.centerOn(scene_point)
            if self.fit_mode:
                self._apply_fit(self.fit_mode)
            self._update_visible_page()
            self._schedule_tiles(settle=True)

    def _request_tiles(self):
        self._missing_noted = False
        if (not self.pages or not self.isVisible() or self._gesture_active
                or self._zoom_anim.state() == QVariantAnimation.State.Running):
            return
        visible = self._visible_scene_rect()
        first = bisect_right(self.bottoms, visible.top())
        end = bisect_left(self.tops, visible.bottom())
        wanted = set(range(first, end))
        departed = self.requested - wanted
        for index in departed:
            ref = self.pages[index].ref
            self.render.cancel_group(self._group(ref))
        if departed:
            self._pending = {key: job for key, job in self._pending.items() if key[1][2] in wanted}
        self.requested = wanted
        scale = self._tile_scale()
        self._prepare_tile_scale(scale)
        for index in range(first, end):
            page = self.pages[index]
            self._request_page_tiles(page.ref, page.mapRectFromScene(visible), scale)

    def _tile_item(self, page_key):
        index = page_key[2]
        if index in self.requested and self.pages[index].ref.key == page_key:
            return self.pages[index]

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, qc(ThemeManager.current().canvas))

    def drawForeground(self, painter, rect):
        pass

    def release(self):
        self._tile_timer.stop()
        for index in self.requested:
            self.render.cancel_group(self._group(self.pages[index].ref))
        super().release()

    def set_mode(self, mode):
        if self.editing is not None:
            self.editing.clearFocus()
        self.mode = mode
        self.left_drag_pans = mode == "pan"
        self._update_cursor()

    def tool_cursor(self):
        return QCursor(Qt.CursorShape.CrossCursor)

    def _history_changed(self, *_):
        self.historyChanged.emit(self.stack.canUndo(), self.stack.canRedo())

    def history(self, redo=False):
        if self.editing is not None:
            self.editing.clearFocus()
        self.stack.redo() if redo else self.stack.undo()

    def _hit(self, event):
        point = self.viewportTransform().inverted()[0].map(event.position())
        index = bisect_right(self.tops, point.y()) - 1 if self.pages else -1
        if index >= 0:
            page = self.pages[index]
            local = page.mapFromScene(point)
            if page.boundingRect().contains(local):
                return index, local, point
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.mode == "pan" or self._space_down:
            super().mousePressEvent(event)
            return
        hit = self._hit(event)
        if self.editing is not None:
            if self.editing.contains(self.editing.mapFromScene(self.mapToScene(event.position().toPoint()))):
                super().mousePressEvent(event)
                return
            self.editing.clearFocus()
            event.accept()
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if hit is not None:
            index, local, scene_point = hit
            ink = self.ink.get(index)
            if self.mode == "eraser":
                if ink is not None:
                    for item in self.scene_obj.items(scene_point):
                        if item.parentItem() is ink:
                            self.stack.push(RemoveItems(ink, [item], "Erase Annotation"))
                            break
            else:
                if ink is None:
                    ink = self.ink[index] = PageInk(self.pages[index])
                style = self.settings.get(self.mode)
                color = QColor(style["color"])
                if self.mode == "text":
                    item = next((item for item in self.scene_obj.items(scene_point)
                                 if isinstance(item, TextItem) and item.parentItem() is ink), None)
                    if item is None:
                        item = TextItem("", color, style["size"])
                        item.setPos(local)
                        ink.attach_items([item])
                        self._edit_text(ink, item, None)
                    else:
                        self._edit_text(ink, item, item.toPlainText())
                else:
                    stroke = StrokeItem(color, style["size"], self.mode == "highlighter", style.get("alpha", 1.0))
                    stroke.add_point(local)
                    ink.attach_items([stroke])
                    self.stroke = ink, stroke
        event.accept()

    def _edit_text(self, ink, item, before):
        self.editing = item

        def finish(edited):
            self.editing = None
            edited.on_edit_finished = None
            after = edited.toPlainText()
            if not after.strip():
                if before is None:
                    ink.detach_items([edited])
                else:
                    edited.setPlainText(before)
                    self.stack.push(RemoveItems(ink, [edited], "Remove Text"))
            elif before is None:
                self.stack.push(AddItems(ink, [edited], "Add Text"))
            elif after != before:
                self.stack.push(EditText(edited, before, after))

        item.on_edit_finished = finish
        item.start_editing()

    def mouseMoveEvent(self, event):
        if self.stroke is None:
            super().mouseMoveEvent(event)
            return
        ink, stroke = self.stroke
        point = ink.mapFromScene(self.viewportTransform().inverted()[0].map(event.position()))
        bounds = ink.rect()
        stroke.add_point(QPointF(min(max(0, point.x()), bounds.right()),
                                 min(max(0, point.y()), bounds.bottom())))
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.stroke is not None:
            ink, stroke = self.stroke
            self.stroke = None
            self.stack.push(AddItems(ink, [stroke], "Add Stroke"))
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if self.editing is not None:
            # Deliver even Space and cursor keys to the active graphics text editor.
            QGraphicsView.keyPressEvent(self, event)
        else:
            super().keyPressEvent(event)
