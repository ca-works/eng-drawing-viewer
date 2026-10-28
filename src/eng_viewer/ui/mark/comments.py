"""Numbered callout comments, linked both ways to their bubbles on the canvas."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ...core.spell import WORD_RE, checkable
from .. import icons
from ..theme import ThemeManager, qc
from ..widgets import SectionHeader, label, secondary_button, tool_button
from .items import BUBBLE_RED, BubbleItem


class SpellHighlighter(QSyntaxHighlighter):
    def __init__(self, document, speller):
        super().__init__(document)
        self.speller = speller
        self.fmt = QTextCharFormat()
        self.fmt.setUnderlineColor(QColor(225, 45, 45))
        self.fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)

    def highlightBlock(self, text: str):
        if self.speller is None:
            return
        for m in WORD_RE.finditer(text):
            w = m.group(0)
            if checkable(w) and not self.speller.check(w):
                self.setFormat(m.start(), m.end() - m.start(), self.fmt)


class SpellTextEdit(QPlainTextEdit):
    """Plain-text comment editor: offline spell check, grows with its content."""

    focused = Signal()

    def __init__(self, speller, placeholder: str = "Comment…", parent=None):
        super().__init__(parent)
        self.setObjectName("CommentEdit")
        self.speller = speller
        self._hl = SpellHighlighter(self.document(), speller)
        self.setPlaceholderText(placeholder)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.document().contentsChanged.connect(self._fit)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._fit()

    def _fit(self):
        lines = max(1, min(8, int(self.document().size().height())))
        fm = self.fontMetrics()
        self.setFixedHeight(int(lines * fm.lineSpacing() + 14))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._fit)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.focused.emit()

    def contextMenuEvent(self, e):
        menu = self.createStandardContextMenu()
        cur = self.cursorForPosition(e.pos())
        cur.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cur.selectedText()
        if self.speller is not None and word and checkable(word) and not self.speller.check(word):
            first = menu.actions()[0] if menu.actions() else None
            suggestions = self.speller.suggest(word)
            heading = menu.insertSection(first, "Spelling") if first else None
            if suggestions:
                for s in suggestions:
                    act = menu.addAction(s)
                    act.triggered.connect(lambda _=False, c=QTextCursor(cur), s=s: self._replace(c, s))
                    if first:
                        menu.removeAction(act)
                        menu.insertAction(first, act)
            else:
                none = menu.addAction("No suggestions")
                none.setEnabled(False)
                if first:
                    menu.removeAction(none)
                    menu.insertAction(first, none)
            learn = menu.addAction("Learn Spelling" if hasattr(self.speller, "close") else "Add to Dictionary")
            learn.triggered.connect(lambda: self._learn(word))
            ignore = menu.addAction("Ignore Spelling")
            ignore.triggered.connect(lambda: self._ignore(word))
            if first:
                for a in (learn, ignore):
                    menu.removeAction(a)
                    menu.insertAction(first, a)
                menu.insertSeparator(first)
        menu.exec(e.globalPos())

    def _replace(self, cur: QTextCursor, text: str):
        cur.insertText(text)

    def _learn(self, word):
        self.speller.learn(word)
        self._hl.rehighlight()
        self.window().setProperty("learned_word", word)

    def _ignore(self, word):
        self.speller.ignore(word)
        self._hl.rehighlight()


class NumberBadge(QWidget):
    clicked = Signal()

    def __init__(self, number: int, parent=None):
        super().__init__(parent)
        self.number = number
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_number(self, n: int):
        self.number = n
        self.update()

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setPen(QPen(BUBBLE_RED, 2))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(r)
        f = QFont(self.font())
        f.setBold(True)
        f.setPixelSize(11 if self.number < 100 else 9)
        p.setFont(f)
        p.setPen(QColor("#000000"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(self.number))
        p.end()


class CommentRow(QFrame):
    activated = Signal(object)  # bubble
    removeComment = Signal(object)
    restoreComment = Signal(object)
    deleteCallout = Signal(object)

    def __init__(self, bubble: BubbleItem, speller, parent=None):
        super().__init__(parent)
        self.bubble = bubble
        self.selected = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 6, 6)
        lay.setSpacing(8)
        self.badge = NumberBadge(bubble.number)
        self.badge.clicked.connect(lambda: self.activated.emit(self.bubble))
        lay.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.edit = SpellTextEdit(speller, f"Comment for callout {bubble.number}…")
        self.edit.setPlainText(bubble.comment)
        self.edit.textChanged.connect(self._text_changed)
        self.edit.focused.connect(lambda: self.activated.emit(self.bubble))
        col.addWidget(self.edit)
        self.placeholder = QPushButton("No comment — add one")
        self.placeholder.setProperty("variant", "link")
        self.placeholder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.placeholder.clicked.connect(lambda: self.restoreComment.emit(self.bubble))
        col.addWidget(self.placeholder)
        lay.addLayout(col, 1)
        self.remove_btn = QPushButton()
        self.remove_btn.setProperty("variant", "danger-x")
        self.remove_btn.setFixedSize(22, 22)
        self.remove_btn.setIcon(icons.icon("x", role="danger", on_role=None, active_role="danger", stroke=2.4))
        self.remove_btn.setIconSize(QSize(13, 13))
        self.remove_btn.setToolTip("Remove this comment (the numbered bubble stays)")
        self.remove_btn.setAccessibleName(f"Remove comment {bubble.number}")
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_btn.clicked.connect(lambda: self.removeComment.emit(self.bubble))
        lay.addWidget(self.remove_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.sync()

    def sync(self):
        b = self.bubble
        self.badge.set_number(b.number)
        self.edit.setPlaceholderText(f"Comment for callout {b.number}…")
        removed = b.comment_removed
        self.edit.setVisible(not removed)
        self.remove_btn.setVisible(not removed)
        self.placeholder.setVisible(removed)
        if not removed and self.edit.toPlainText() != b.comment:
            self.edit.blockSignals(True)
            self.edit.setPlainText(b.comment)
            self.edit.blockSignals(False)

    def _text_changed(self):
        self.bubble.comment = self.edit.toPlainText()

    def set_selected(self, on: bool):
        if on != self.selected:
            self.selected = on
            self.update()

    def _menu(self, pos):
        m = QMenu(self)
        if self.bubble.comment_removed:
            m.addAction("Add Comment", lambda: self.restoreComment.emit(self.bubble))
        else:
            m.addAction("Remove Comment (Keep Bubble)", lambda: self.removeComment.emit(self.bubble))
        m.addAction(f"Delete Callout {self.bubble.number} and Its Comment", lambda: self.deleteCallout.emit(self.bubble))
        m.exec(self.mapToGlobal(pos))

    def paintEvent(self, e):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 1, -2, -1)
        if self.selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(t.selected))
            p.drawRoundedRect(r, 8, 8)
            p.setBrush(qc(t.accent))
            p.drawRoundedRect(QRectF(r.left(), r.top() + 8, 3, r.height() - 16), 1.5, 1.5)
        elif self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(t.hover))
            p.drawRoundedRect(r, 8, 8)
        p.end()


class CommentsPanel(QWidget):
    copyRequested = Signal()

    def __init__(self, canvas, speller, parent=None):
        super().__init__(parent)
        self.setObjectName("Inspector")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.canvas = canvas
        self.speller = speller
        self.rows: dict[int, CommentRow] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = SectionHeader("Callouts")
        more = tool_button("more", "Callout options", size=24, icon_size=16)
        more.setPopupMode(more.ToolButtonPopupMode.InstantPopup)
        m = QMenu(more)
        self.act_renumber = m.addAction("Renumber Callouts 1…n", canvas.renumber)
        m.addAction("Copy Numbered Comments", self.copyRequested.emit)
        more.setMenu(m)
        self.header.add_trailing(more)
        lay.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("Comments")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.inner.setObjectName("CommentsInner")
        self.list = QVBoxLayout(self.inner)
        self.list.setContentsMargins(6, 0, 6, 6)
        self.list.setSpacing(2)
        self.empty = label("Choose the Callout tool (B) and click the drawing to place numbered red bubbles. "
                           "Each bubble gets a numbered, spell-checked comment here.", "tertiary", wrap=True)
        self.empty.setContentsMargins(8, 4, 8, 4)
        self.list.addWidget(self.empty)
        self.list.addStretch(1)
        self.scroll.setWidget(self.inner)
        lay.addWidget(self.scroll, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(10, 6, 10, 10)
        self.copy_btn = secondary_button("Copy Numbered Comments", "clipboard-text")
        self.copy_btn.clicked.connect(self.copyRequested.emit)
        foot.addWidget(self.copy_btn, 1)
        lay.addLayout(foot)

        canvas.bubblesChanged.connect(self.sync)
        canvas.bubblePlaced.connect(self.focus_comment)
        canvas.selectionChanged.connect(self._on_canvas_selection)
        self.sync()

    def sync(self):
        bubbles = self.canvas.bubbles()
        live = {id(b) for b in bubbles}
        for key in list(self.rows):
            if key not in live:
                row = self.rows.pop(key)
                row.setParent(None)
                row.deleteLater()
        for b in bubbles:
            row = self.rows.get(id(b))
            if row is None:
                row = CommentRow(b, self.speller)
                row.activated.connect(self._row_activated)
                row.removeComment.connect(lambda b: self.canvas.set_comment(b, "", True, f"Remove Comment {b.number}"))
                row.restoreComment.connect(self._restore)
                row.deleteCallout.connect(self.canvas.delete_bubble)
                self.rows[id(b)] = row
            row.sync()
        # Reorder rows by number.
        for i, b in enumerate(bubbles):
            row = self.rows[id(b)]
            self.list.removeWidget(row)
            self.list.insertWidget(1 + i, row)
        self.empty.setVisible(not bubbles)
        n = len(bubbles)
        self.header.badge.set_text(str(n) if n else "")
        self.copy_btn.setEnabled(any(b.comment.strip() and not b.comment_removed for b in bubbles) or n > 0)
        self.act_renumber.setEnabled(n > 0)

    def _restore(self, bubble):
        self.canvas.set_comment(bubble, bubble.comment if bubble.comment_removed else "", False, f"Add Comment {bubble.number}")
        QTimer.singleShot(0, lambda: self.focus_comment(bubble))

    def focus_comment(self, bubble):
        self.sync()
        row = self.rows.get(id(bubble))
        if row is None:
            return
        if bubble.comment_removed:
            self.canvas.set_comment(bubble, "", False, f"Add Comment {bubble.number}")
        self.scroll.ensureWidgetVisible(row)
        row.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _row_activated(self, bubble):
        for row in self.rows.values():
            row.set_selected(row.bubble is bubble)
        self.canvas.focus_bubble(bubble)

    def _on_canvas_selection(self, items):
        selected = {id(i) for i in items if isinstance(i, BubbleItem)}
        first = None
        for key, row in self.rows.items():
            on = key in selected
            row.set_selected(on)
            if on and first is None:
                first = row
        if first is not None:
            self.scroll.ensureWidgetVisible(first)
