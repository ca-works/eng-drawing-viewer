"""The PDF Tools workspace: a tool catalogue and the selected tool's panel."""
from __future__ import annotations


from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QScrollArea, QStackedWidget, QVBoxLayout,
    QWidget,
)

from .. import icons
from ..theme import ThemeManager, qc
from ..widgets import SearchField, label, primary_button, secondary_button, tool_button
from .common import Card
from .specs import BY_ID, CATEGORIES, SPECS, ToolSpec

TOOL_ROLE = Qt.ItemDataRole.UserRole + 1
CAT_COLOR = {key: color for key, _name, color in CATEGORIES}


class ToolCard(QFrame):
    clicked = Signal(str)

    def __init__(self, spec: ToolSpec, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.setFixedHeight(92)
        self.setMinimumWidth(220)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(spec.blurb)
        self.setAccessibleName(spec.title)
        reason = spec.available() if spec.available else ""
        self.disabled_reason = reason

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.spec.id)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.spec.id)
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        hover = self.underMouse() or self.hasFocus()
        p.setPen(qc(t.accent) if self.hasFocus() else qc(t.separator))
        p.setBrush(qc(t.elevated) if not hover else (qc(t.hover) if t.dark else QColor("#FFFFFF")))
        p.drawRoundedRect(r, 10, 10)
        color = QColor(CAT_COLOR.get(self.spec.category, t.accent))
        tint = QColor(color)
        tint.setAlphaF(0.14)
        circle = QRectF(r.left() + 14, r.top() + 16, 38, 38)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tint)
        p.drawRoundedRect(circle, 10, 10)
        icons.paint_icon(p, self.spec.icon, circle.adjusted(9, 9, -9, -9), color, stroke=1.9)
        x = circle.right() + 14
        w = r.right() - x - 12
        f = QFont(self.font())
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(qc(t.text if not self.disabled_reason else t.text_tertiary))
        fm = QFontMetrics(f)
        p.drawText(QRectF(x, r.top() + 14, w, fm.height()), Qt.AlignmentFlag.AlignVCenter, fm.elidedText(self.spec.title, Qt.TextElideMode.ElideRight, int(w)))
        small = QFont(self.font())
        small.setPointSizeF(max(8.5, small.pointSizeF() - 1.5))
        p.setFont(small)
        p.setPen(qc(t.text_secondary))
        text = self.disabled_reason or self.spec.blurb
        sfm = QFontMetrics(small)
        top = r.top() + 16 + fm.height()
        max_lines = max(1, int((r.bottom() - 10 - top) // sfm.lineSpacing()))
        for i, line in enumerate(_wrap(text, sfm, int(w), max_lines)):
            p.drawText(QRectF(x, top + i * sfm.lineSpacing(), w, sfm.height()), Qt.AlignmentFlag.AlignVCenter, line)
        p.end()


def _wrap(text: str, fm: QFontMetrics, width: int, max_lines: int) -> list[str]:
    """Word-wrap ``text``; the last allowed line is elided if more would follow."""
    lines: list[str] = []
    words = text.split()
    cur = ""
    for i, word in enumerate(words):
        cand = f"{cur} {word}".strip()
        if fm.horizontalAdvance(cand) <= width or not cur:
            cur = cand
            continue
        lines.append(cur)
        cur = word
        if len(lines) == max_lines - 1:
            cur = " ".join(words[i:])
            break
    if cur:
        lines.append(fm.elidedText(cur, Qt.TextElideMode.ElideRight, width) if len(lines) == max_lines - 1 else cur)
    return lines[:max_lines]


class HomePage(QWidget):
    toolChosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards: list[ToolCard] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 28)
        lay.setSpacing(6)
        lay.addWidget(label("PDF Tools", "heading"))
        lay.addWidget(label("Edit, convert and review PDFs and images — entirely on this computer. Originals are never changed.", "secondary"))
        lay.addSpacing(10)
        self.sections: list[tuple[QLabel, QGridLayout, list[ToolCard]]] = []
        for key, name, _color in CATEGORIES:
            specs = [s for s in SPECS if s.category == key]
            if not specs:
                continue
            title = label(name.upper(), "section")
            lay.addSpacing(10)
            lay.addWidget(title)
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(12)
            cards = []
            for s in specs:
                c = ToolCard(s)
                c.clicked.connect(self.toolChosen.emit)
                cards.append(c)
                self.cards.append(c)
            lay.addLayout(grid)
            self.sections.append((title, grid, cards))
        lay.addStretch(1)
        self._cols = 0

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cols = max(1, min(4, (self.width() - 56) // 270))
        if cols != self._cols:
            self._cols = cols
            for _title, grid, cards in self.sections:
                for c in cards:
                    grid.removeWidget(c)
                for i, c in enumerate(cards):
                    grid.addWidget(c, i // cols, i % cols)
                for col in range(4):
                    grid.setColumnStretch(col, 1 if col < cols else 0)


class ScanPanel(QWidget):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        card = Card("How it works")
        steps = label(
            "1.  Make sure your iPhone is near this Mac, signed in to the same Apple Account, with Wi‑Fi and Bluetooth on.\n"
            "2.  Click Scan with iPhone and choose Scan Documents (or Take Photo).\n"
            "3.  Hold the iPhone over the drawing — it captures pages automatically. Tap Save when done.\n"
            "4.  The scan opens here as a PDF. MARK it, export it, run OCR, or combine it with other files.", "secondary", wrap=True)
        card.lay.addWidget(steps)
        row = QHBoxLayout()
        self.button = primary_button("Scan with iPhone…", "iphone")
        self.button.clicked.connect(lambda: host.start_scan(self.button))
        row.addWidget(self.button)
        folder = secondary_button("Open Scans Folder")
        folder.clicked.connect(host.open_scans_folder)
        row.addWidget(folder)
        row.addStretch(1)
        card.lay.addLayout(row)
        self.where = label("", "tertiary", wrap=True)
        card.lay.addWidget(self.where)
        lay.addWidget(card)
        lay.addStretch(1)

    def showEvent(self, e):
        super().showEvent(e)
        self.where.setText(f"Scans are saved in {self.host.scans_folder()} and never leave this Mac.")


class MarkupPanel(QWidget):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        card = Card("Mark up a drawing or image")
        card.lay.addWidget(label(
            "MARK adds pen, highlighter, arrows, circles, text and numbered callouts with comments. "
            "Save the result as a PDF (vector, the original stays untouched), copy it, or send it to the PowerPoint queue.",
            "secondary", wrap=True))
        row = QHBoxLayout()
        cur = primary_button("Mark Up Current Drawing", "markup")
        cur.clicked.connect(host.open_mark_current)
        row.addWidget(cur)
        pick = secondary_button("Choose a File…")
        pick.clicked.connect(host.choose_and_mark)
        row.addWidget(pick)
        row.addStretch(1)
        card.lay.addLayout(row)
        lay.addWidget(card)
        lay.addStretch(1)


class ToolsWorkspace(QWidget):
    """Left: tool list. Right: home grid or the chosen tool."""

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.panels: dict[str, QWidget] = {}
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        nav = QWidget()
        nav.setObjectName("Sidebar")
        nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        nav.setFixedWidth(232)
        nl = QVBoxLayout(nav)
        nl.setContentsMargins(10, 12, 8, 8)
        nl.setSpacing(8)
        self.search = SearchField("Find a tool")
        self.search.textChanged.connect(self._filter)
        nl.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("ToolNav")
        self.list.setIconSize(QSize(18, 18))
        self.list.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        home = QListWidgetItem(icons.icon("tools", role="text_secondary"), "All Tools")
        home.setData(TOOL_ROLE, "")
        self.list.addItem(home)
        for key, name, _color in CATEGORIES:
            specs = [s for s in SPECS if s.category == key]
            if not specs:
                continue
            head = QListWidgetItem(name.upper())
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            f = head.font()
            f.setPointSizeF(max(8.5, f.pointSizeF() - 2.5))
            f.setWeight(QFont.Weight.DemiBold)
            head.setFont(f)
            head.setSizeHint(QSize(100, 30))
            self.list.addItem(head)
            for s in specs:
                it = QListWidgetItem(icons.icon(s.icon, role="text_secondary"), s.title)
                it.setData(TOOL_ROLE, s.id)
                it.setToolTip(s.blurb)
                it.setSizeHint(QSize(100, 30))
                self.list.addItem(it)
        self.list.currentItemChanged.connect(self._nav_changed)
        nl.addWidget(self.list, 1)
        outer.addWidget(nav)
        sep = QFrame()
        sep.setObjectName("VSep")
        sep.setStyleSheet("margin: 0px;")
        outer.addWidget(sep)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.header = QWidget()
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(24, 14, 24, 6)
        self.back = tool_button("chevron-left", "All tools", size=28)
        self.back.clicked.connect(lambda: self.open_tool(""))
        hl.addWidget(self.back)
        self.h_icon = QLabel()
        hl.addWidget(self.h_icon)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.h_title = label("", "heading")
        col.addWidget(self.h_title)
        self.h_blurb = label("", "secondary", wrap=True)
        col.addWidget(self.h_blurb)
        hl.addLayout(col, 1)
        right.addWidget(self.header)
        self.stack = QStackedWidget()
        self.home = HomePage()
        self.home.toolChosen.connect(self.open_tool)
        self.stack.addWidget(self._scroll(self.home))
        right.addWidget(self.stack, 1)
        holder = QWidget()
        holder.setLayout(right)
        outer.addWidget(holder, 1)
        self.open_tool("")

    def _scroll(self, w: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setObjectName("Comments")
        w.setObjectName("CommentsInner")
        sa.setWidget(w)
        return sa

    def _nav_changed(self, cur, _prev):
        if cur is not None and cur.flags() & Qt.ItemFlag.ItemIsSelectable:
            self.open_tool(cur.data(TOOL_ROLE) or "", from_nav=True)

    def _filter(self, text: str):
        text = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            tid = it.data(TOOL_ROLE)
            if tid:
                s = BY_ID[tid]
                it.setHidden(bool(text) and text not in (s.title + " " + s.blurb).lower())
            elif it.flags() == Qt.ItemFlag.NoItemFlags:
                it.setHidden(bool(text))
        for c in self.home.cards:
            c.setVisible(not text or text in (c.spec.title + " " + c.spec.blurb).lower())

    def _make_panel(self, spec: ToolSpec) -> QWidget:
        from .compare import ComparePanel
        from .organizer import OrganizerPanel
        from .panel import ToolPanel

        if spec.custom == "organizer":
            panel = OrganizerPanel(self.host)
        elif spec.custom == "compare":
            panel = ComparePanel(self.host)
        elif spec.custom == "scan":
            panel = ScanPanel(self.host)
        elif spec.custom == "markup":
            panel = MarkupPanel(self.host)
        else:
            panel = ToolPanel(spec, self.host)
        if hasattr(panel, "outputsCreated"):
            panel.outputsCreated.connect(self.host.outputs_created)
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(24, 8, 24, 24)
        v.addWidget(panel, 1)
        wrap.panel = panel
        return wrap

    def open_tool(self, tool_id: str, inputs: list[str] | None = None, from_nav: bool = False):
        if not tool_id:
            self.stack.setCurrentIndex(0)
            self.header.hide()
        else:
            spec = BY_ID[tool_id]
            wrap = self.panels.get(tool_id)
            if wrap is None:
                wrap = self._make_panel(spec)
                self.panels[tool_id] = wrap
                self.stack.addWidget(self._scroll(wrap) if not spec.custom in ("organizer", "compare") else wrap)
            target = wrap if spec.custom in ("organizer", "compare") else wrap.parentWidget().parentWidget()
            self.stack.setCurrentWidget(target)
            self.header.show()
            color = QColor(CAT_COLOR.get(spec.category, "#2F7CF6"))
            self.h_icon.setPixmap(icons.pixmap(spec.icon, 30, color, stroke=1.8))
            self.h_title.setText(spec.title)
            self.h_blurb.setText(spec.blurb)
            if inputs:
                panel = wrap.panel
                if hasattr(panel, "add_inputs"):
                    panel.add_inputs(inputs)
                elif hasattr(panel, "add_files"):
                    panel.add_files(inputs)
                elif hasattr(panel, "a") and inputs:
                    panel.a.set_path(inputs[0])
                    if len(inputs) > 1:
                        panel.b.set_path(inputs[1])
        if not from_nav:
            for i in range(self.list.count()):
                if (self.list.item(i).data(TOOL_ROLE) or "") == tool_id and self.list.item(i).flags() & Qt.ItemFlag.ItemIsSelectable:
                    self.list.blockSignals(True)
                    self.list.setCurrentRow(i)
                    self.list.blockSignals(False)
                    break

    def current_tool(self) -> str:
        for tid, wrap in self.panels.items():
            if self.stack.currentWidget() in (wrap, wrap.parentWidget().parentWidget() if wrap.parentWidget() else None):
                return tid
        return ""

    def drop_files(self, paths: list[str]) -> bool:
        """Files dropped on the workspace go to the open tool, if it takes them."""
        tid = self.current_tool()
        if not tid:
            return False
        panel = self.panels[tid].panel
        if hasattr(panel, "add_inputs"):
            return panel.add_inputs(paths) > 0
        if hasattr(panel, "add_files"):
            panel.add_files(paths)
            return True
        return False
