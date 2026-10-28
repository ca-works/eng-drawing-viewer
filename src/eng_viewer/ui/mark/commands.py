"""Undo/redo commands for MARK (every edit, including callout comments)."""
from __future__ import annotations

from PySide6.QtCore import QPointF
from PySide6.QtGui import QUndoCommand

from .items import BubbleItem


class AddItems(QUndoCommand):
    def __init__(self, canvas, items, text: str):
        super().__init__(text)
        self.canvas, self.items = canvas, list(items)

    def redo(self):
        self.canvas.attach_items(self.items)

    def undo(self):
        self.canvas.detach_items(self.items)


class RemoveItems(QUndoCommand):
    def __init__(self, canvas, items, text: str = "Delete"):
        super().__init__(text)
        self.canvas, self.items = canvas, list(items)

    def redo(self):
        self.canvas.detach_items(self.items)

    def undo(self):
        self.canvas.attach_items(self.items)


class MoveItems(QUndoCommand):
    MERGE_ID = 0x4D4F5645

    def __init__(self, items, delta: QPointF, text: str = "Move", nudge: bool = False):
        super().__init__(text)
        self.items, self.delta, self.nudge = list(items), QPointF(delta), nudge
        self._first = True

    def id(self):
        return self.MERGE_ID if self.nudge else -1

    def mergeWith(self, other):
        if not (self.nudge and other.nudge) or set(map(id, self.items)) != set(map(id, other.items)):
            return False
        self.delta += other.delta
        return True

    def redo(self):
        if self._first and not self.nudge:
            self._first = False  # the drag already moved the items
            return
        for it in self.items:
            it.moveBy(self.delta.x(), self.delta.y())

    def undo(self):
        for it in self.items:
            it.moveBy(-self.delta.x(), -self.delta.y())


class ChangeStyle(QUndoCommand):
    def __init__(self, canvas, items, before: list[dict], after: list[dict], text: str = "Change Style"):
        super().__init__(text)
        self.canvas, self.items, self.before, self.after = canvas, list(items), before, after

    def redo(self):
        for it, st in zip(self.items, self.after):
            it.set_style(st)
        self.canvas.items_restyled(self.items)

    def undo(self):
        for it, st in zip(self.items, self.before):
            it.set_style(st)
        self.canvas.items_restyled(self.items)


class EditText(QUndoCommand):
    def __init__(self, item, before: str, after: str):
        super().__init__("Edit Text")
        self.item, self.before, self.after = item, before, after
        self._first = True

    def redo(self):
        if self._first:
            self._first = False
            return
        self.item.setPlainText(self.after)

    def undo(self):
        self.item.setPlainText(self.before)


class SetComment(QUndoCommand):
    """Remove or restore a callout's comment while keeping the bubble."""

    def __init__(self, canvas, bubble: BubbleItem, text: str, removed: bool, label: str):
        super().__init__(label)
        self.canvas, self.bubble = canvas, bubble
        self.before = (bubble.comment, bubble.comment_removed)
        self.after = (text, removed)

    def _apply(self, state):
        self.bubble.comment, self.bubble.comment_removed = state
        self.canvas.bubblesChanged.emit()

    def redo(self):
        self._apply(self.after)

    def undo(self):
        self._apply(self.before)


class Renumber(QUndoCommand):
    def __init__(self, canvas, bubbles: list[BubbleItem], new_numbers: list[int]):
        super().__init__("Renumber Callouts")
        self.canvas, self.bubbles = canvas, list(bubbles)
        self.before = [b.number for b in bubbles]
        self.after = list(new_numbers)

    def _apply(self, numbers):
        for b, n in zip(self.bubbles, numbers):
            b.set_number(n)
        self.canvas.bubblesChanged.emit()

    def redo(self):
        self._apply(self.after)

    def undo(self):
        self._apply(self.before)
