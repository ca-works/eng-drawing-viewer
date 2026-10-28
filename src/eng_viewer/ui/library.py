"""Library sidebar: folder tree / flat list of PDFs with thumbnails."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QAbstractItemModel, QMimeData, QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize,
    QSortFilterProxyModel, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QMenu, QSlider, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTreeView, QVBoxLayout, QWidget,
)

from ..core.naturalsort import natural_key, path_sort_key
from . import icons
from .theme import ThemeManager, qc
from .thumbnails import ASPECT, ThumbnailProvider
from .widgets import ElidedLabel, SearchField, SegmentedControl, label, tool_button

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
KIND_ROLE = Qt.ItemDataRole.UserRole + 2
COUNT_ROLE = Qt.ItemDataRole.UserRole + 3
REL_ROLE = Qt.ItemDataRole.UserRole + 4

PAPER = [
    ("A0", 841, 1189), ("A1", 594, 841), ("A2", 420, 594), ("A3", 297, 420), ("A4", 210, 297), ("A5", 148, 210),
    ("ANSI E", 864, 1118), ("ANSI D", 559, 864), ("ANSI C", 432, 559), ("Tabloid", 279, 432), ("Letter", 216, 279),
]


def paper_name(w_pt: float, h_pt: float) -> str:
    """Name the sheet size when it matches a standard within 1.5 %."""
    a, b = sorted((w_pt * 25.4 / 72, h_pt * 25.4 / 72))
    for name, sw, sh in PAPER:
        if abs(a - sw) / sw < 0.015 and abs(b - sh) / sh < 0.015:
            return name
    return f"{w_pt * 25.4 / 72:.0f}×{h_pt * 25.4 / 72:.0f} mm"


class Node:
    __slots__ = ("kind", "name", "path", "parent", "children", "row", "count", "rel")

    def __init__(self, kind: str, name: str, path: Path | None, parent: "Node | None", rel: str = ""):
        self.kind = kind
        self.name = name
        self.path = path
        self.parent = parent
        self.children: list[Node] = []
        self.row = 0
        self.count = 0
        self.rel = rel


class LibraryModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = Node("root", "", None, None)
        self.mode = "tree"
        self.base: Path | None = None
        self._files: list[Path] = []
        self._by_path: dict[str, Node] = {}
        self.current_path: str | None = None

    # ---------------------------------------------------------------- build
    def set_files(self, base: Path | None, files: list[Path], mode: str):
        self.beginResetModel()
        self.base = base
        self.mode = mode
        self._files = list(files)
        self._by_path = {}
        self.root = Node("root", "", None, None)
        if mode == "tree" and base is not None:
            self._build_tree(base, files)
        else:
            ordered = sorted(files, key=path_sort_key)
            for f in ordered:
                rel = self._rel_parent(f)
                n = Node("file", f.name, f, self.root, rel)
                self.root.children.append(n)
                self._by_path[str(f)] = n
        self._finalize(self.root)
        self.endResetModel()

    def _rel_parent(self, f: Path) -> str:
        if self.base is None:
            return str(f.parent)
        try:
            rel = f.parent.relative_to(self.base)
            return "" if str(rel) == "." else str(rel)
        except ValueError:
            return str(f.parent)

    def _build_tree(self, base: Path, files: list[Path]):
        folders: dict[tuple, Node] = {(): self.root}
        for f in files:
            try:
                parts = f.parent.relative_to(base).parts
            except ValueError:
                parts = (str(f.parent),)
            parent = self.root
            for i in range(len(parts)):
                key = parts[: i + 1]
                node = folders.get(key)
                if node is None:
                    node = Node("folder", parts[i], base.joinpath(*key), parent, "/".join(key))
                    parent.children.append(node)
                    folders[key] = node
                parent = node
            n = Node("file", f.name, f, parent, "/".join(parts))
            parent.children.append(n)
            self._by_path[str(f)] = n

    def _finalize(self, node: Node) -> int:
        node.children.sort(key=lambda c: (0 if c.kind == "folder" else 1, natural_key(c.name)))
        total = 0
        for i, c in enumerate(node.children):
            c.row = i
            total += self._finalize(c) if c.kind == "folder" else 1
        node.count = total
        return total

    def files(self) -> list[Path]:
        """Files in display order (used for previous/next document)."""
        out: list[Path] = []

        def walk(n: Node):
            for c in n.children:
                if c.kind == "file":
                    out.append(c.path)
                else:
                    walk(c)

        walk(self.root)
        return out

    def index_for_path(self, path: str) -> QModelIndex:
        n = self._by_path.get(path)
        if n is None:
            return QModelIndex()
        return self.createIndex(n.row, 0, n)

    def set_current_path(self, path: str | None):
        old, self.current_path = self.current_path, path
        for p in (old, path):
            if p:
                idx = self.index_for_path(p)
                if idx.isValid():
                    self.dataChanged.emit(idx, idx)

    def refresh_path(self, path: str):
        idx = self.index_for_path(path)
        if idx.isValid():
            self.dataChanged.emit(idx, idx)

    # ---------------------------------------------------------- model API
    def _node(self, index: QModelIndex) -> Node:
        return index.internalPointer() if index.isValid() else self.root

    def index(self, row, column, parent=QModelIndex()):
        p = self._node(parent)
        if 0 <= row < len(p.children) and column == 0:
            return self.createIndex(row, 0, p.children[row])
        return QModelIndex()

    def parent(self, index=QModelIndex()):
        if not index.isValid():
            return QModelIndex()
        n: Node = index.internalPointer()
        p = n.parent
        if p is None or p is self.root:
            return QModelIndex()
        return self.createIndex(p.row, 0, p)

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0
        return len(self._node(parent).children)

    def columnCount(self, parent=QModelIndex()):
        return 1

    def hasChildren(self, parent=QModelIndex()):
        return bool(self._node(parent).children)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        n: Node = index.internalPointer()
        if role == Qt.ItemDataRole.DisplayRole:
            return n.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(n.path) if n.path else n.name
        if role == PATH_ROLE:
            return str(n.path) if n.path else None
        if role == KIND_ROLE:
            return n.kind
        if role == COUNT_ROLE:
            return n.count
        if role == REL_ROLE:
            return n.rel
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{'Folder' if n.kind == 'folder' else 'PDF'} {n.name}"
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.internalPointer().kind == "file":
            f |= Qt.ItemFlag.ItemIsDragEnabled
        return f

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, indexes):
        md = QMimeData()
        urls = [QUrl.fromLocalFile(str(i.internalPointer().path)) for i in indexes if i.isValid() and i.internalPointer().kind == "file"]
        md.setUrls(urls)
        return md

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction


class LibraryFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self._tokens: list[str] = []

    def set_query(self, text: str):
        tokens = [t for t in text.casefold().split() if t]
        if hasattr(self, "beginFilterChange"):  # Qt 6.10+
            self.beginFilterChange()
            self._tokens = tokens
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self._tokens = tokens
            self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if not self._tokens:
            return True
        idx = self.sourceModel().index(row, 0, parent)
        n: Node = idx.internalPointer()
        if n.kind != "file":
            return False  # folders are shown when a descendant matches (recursive filtering)
        hay = f"{n.rel}/{n.name}".casefold()
        return all(t in hay for t in self._tokens)


class LibraryDelegate(QStyledItemDelegate):
    def __init__(self, view: "LibraryView", provider: ThumbnailProvider):
        super().__init__(view)
        self.view = view
        self.provider = provider
        self.thumb_h = 72

    def _source(self, index):
        proxy = index.model()
        return proxy.mapToSource(index) if isinstance(proxy, QSortFilterProxyModel) else index

    def sizeHint(self, option, index):
        kind = index.data(KIND_ROLE)
        if kind == "folder":
            return QSize(100, 28)
        return QSize(100, max(30, self.thumb_h + 8))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        t = ThemeManager.current()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(2, 1, -6, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        active = self.view.hasFocus() or self.view.window().isActiveWindow()
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qc(t.selected_strong if active and self.view.hasFocus() else t.selected))
            painter.drawRoundedRect(r, 6, 6)
        elif hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qc(t.hover))
            painter.drawRoundedRect(r, 6, 6)

        kind = index.data(KIND_ROLE)
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        font = QFont(option.font)
        fm = QFontMetrics(font)
        if kind == "folder":
            icons.paint_icon(painter, "folder", QRectF(r.left() + 4, r.center().y() - 8, 16, 16), qc(t.accent))
            count = str(index.data(COUNT_ROLE) or 0)
            small = QFont(font)
            small.setPointSizeF(max(8.5, font.pointSizeF() - 2))
            sfm = QFontMetrics(small)
            cw = sfm.horizontalAdvance(count) + 10
            text_rect = QRectF(r.left() + 26, r.top(), r.width() - 32 - cw, r.height())
            painter.setPen(qc(t.text))
            f2 = QFont(font)
            f2.setWeight(QFont.Weight.Medium)
            painter.setFont(f2)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, QFontMetrics(f2).elidedText(name, Qt.TextElideMode.ElideMiddle, int(text_rect.width())))
            painter.setFont(small)
            painter.setPen(qc(t.text_tertiary))
            painter.drawText(QRectF(r.right() - cw - 2, r.top(), cw, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, count)
            painter.restore()
            return

        path = index.data(PATH_ROLE)
        th = self.thumb_h
        box = QRectF(r.left() + 4, r.top() + (r.height() - th) / 2, round(th * ASPECT), th)
        dpr = self.view.devicePixelRatioF()
        pm = self.provider.get(path, 0, th * dpr)
        info = self.provider.info(path) or {}
        failed = "error" in info or self.provider.failed(path)
        if pm is not None and not pm.isNull():
            iw, ih = pm.width(), pm.height()
            s = min(box.width() / iw, box.height() / ih)
            tr = QRectF(0, 0, iw * s, ih * s)
            tr.moveCenter(box.center())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 40 if t.dark else 22))
            painter.drawRoundedRect(tr.adjusted(-1, -0.5, 1, 1.5), 2.5, 2.5)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            clip = QPainterPath()
            clip.addRoundedRect(tr, 2, 2)
            painter.setClipPath(clip)
            painter.drawPixmap(tr, pm, QRectF(pm.rect()))
            painter.setClipping(False)
        else:
            painter.setPen(QPen(qc(t.separator), 1))
            painter.setBrush(qc(t.hover))
            ph = QRectF(0, 0, min(box.width(), box.height() * 1.414), box.height())
            ph.moveCenter(box.center())
            painter.drawRoundedRect(ph, 3, 3)
            ic = min(22.0, th * 0.4)
            icons.paint_icon(painter, "warning" if failed else "file", QRectF(ph.center().x() - ic / 2, ph.center().y() - ic / 2, ic, ic),
                             qc(t.danger if failed else t.text_tertiary))

        tx = box.right() + 10
        text_w = r.right() - tx - 4
        is_current = path == self.view.model_source().current_path
        f_name = QFont(font)
        f_name.setWeight(QFont.Weight.DemiBold if is_current else QFont.Weight.Normal)
        nfm = QFontMetrics(f_name)
        small = QFont(font)
        small.setPointSizeF(max(8.5, font.pointSizeF() - 2))
        sfm = QFontMetrics(small)
        lines = []
        if failed:
            lines.append((info.get("error") or "Cannot be opened", qc(t.danger)))
        elif info.get("pages") and Path(path).suffix.lower() != ".pdf":
            meta = f"{Path(path).suffix.lstrip('.').upper()} image"
            if info.get("pixels"):
                meta += f" · {info['pixels'][0]}×{info['pixels'][1]} px"
            lines.append((meta, qc(t.text_tertiary)))
        elif info.get("pages"):
            n = info["pages"]
            meta = f"{n} page{'s' if n != 1 else ''}"
            if info.get("w"):
                meta += f" · {paper_name(info['w'], info['h'])}"
            lines.append((meta, qc(t.text_tertiary)))
        rel = index.data(REL_ROLE)
        if self.view.model_source().mode == "flat" and rel:
            lines.append((rel, qc(t.text_tertiary)))
        show_lines = lines[: max(0, int((r.height() - nfm.height()) // (sfm.height() + 1)))]
        block_h = nfm.height() + sum(sfm.height() + 1 for _ in show_lines)
        y = r.top() + (r.height() - block_h) / 2
        painter.setFont(f_name)
        painter.setPen(qc(t.accent) if is_current and not selected else qc(t.text))
        painter.drawText(QRectF(tx, y, text_w, nfm.height()), Qt.AlignmentFlag.AlignVCenter,
                         nfm.elidedText(name, Qt.TextElideMode.ElideMiddle, int(text_w)))
        y += nfm.height() + 1
        painter.setFont(small)
        for text, color in show_lines:
            painter.setPen(color)
            painter.drawText(QRectF(tx, y, text_w, sfm.height()), Qt.AlignmentFlag.AlignVCenter,
                             sfm.elidedText(text, Qt.TextElideMode.ElideRight, int(text_w)))
            y += sfm.height() + 1
        painter.restore()


class LibraryView(QTreeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Library")
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setUniformRowHeights(False)
        self.setAnimated(True)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setExpandsOnDoubleClick(True)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def model_source(self) -> LibraryModel:
        m = self.model()
        return m.sourceModel() if isinstance(m, QSortFilterProxyModel) else m

    def drawBranches(self, painter: QPainter, rect: QRect, index):
        if not self.model().hasChildren(index):
            return
        t = ThemeManager.current()
        expanded = self.isExpanded(index)
        size = 12.0
        r = QRectF(rect.right() - 14, rect.center().y() - size / 2 + 0.5, size, size)
        icons.paint_icon(painter, "chevron-down" if expanded else "chevron-right", r, qc(t.text_tertiary), stroke=2.2)

    def keyPressEvent(self, event):
        # Left/Right flip pages app-wide; expand/collapse folders with
        # Option/Alt+arrows or Return instead.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            idx = self.currentIndex()
            if idx.isValid() and self.model().hasChildren(idx):
                self.setExpanded(idx, not self.isExpanded(idx))
                return
        super().keyPressEvent(event)


class LibraryPanel(QWidget):
    fileActivated = Signal(str)
    openFolderRequested = Signal()
    rescanRequested = Signal()
    recursiveToggled = Signal(bool)
    modeChanged = Signal(str)
    thumbSizeChanged = Signal(int)
    toolRequested = Signal(str, list)  # PDF tool id, paths
    markRequested = Signal(str)
    imagesToggled = Signal(bool)

    def __init__(self, provider: ThumbnailProvider, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.provider = provider
        self.model = LibraryModel(self)
        self.proxy = LibraryFilter(self)
        self.proxy.setSourceModel(self.model)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(12, 10, 6, 4)
        head.setSpacing(4)
        self.title = ElidedLabel("Library", Qt.TextElideMode.ElideMiddle)
        self.title.setProperty("role", "title")
        head.addWidget(self.title, 1)
        self.menu_btn = tool_button("more", "Library options", size=24, icon_size=16)
        self.menu_btn.setPopupMode(self.menu_btn.ToolButtonPopupMode.InstantPopup)
        head.addWidget(self.menu_btn)
        lay.addLayout(head)

        row = QHBoxLayout()
        row.setContentsMargins(10, 2, 10, 8)
        row.setSpacing(6)
        self.mode_ctrl = SegmentedControl([
            ("tree", None, "tree", "Folder tree"),
            ("flat", None, "list", "All files (flat list)"),
        ])
        self.mode_ctrl.changed.connect(self._mode_changed)
        row.addWidget(self.mode_ctrl)
        self.search = SearchField("Filter drawings")
        self.search.textChanged.connect(self._filter_changed)
        row.addWidget(self.search, 1)
        lay.addLayout(row)

        self.view = LibraryView()
        self.view.setModel(self.proxy)
        self.delegate = LibraryDelegate(self.view, provider)
        self.view.setItemDelegate(self.delegate)
        self.view.selectionModel().currentChanged.connect(self._current_changed)
        self.view.clicked.connect(self._clicked)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        lay.addWidget(self.view, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(12, 6, 10, 8)
        foot.setSpacing(6)
        self.count_label = label("", "tertiary")
        foot.addWidget(self.count_label, 1)
        small_ic = tool_button("image", "Smaller previews", size=20, icon_size=12)
        small_ic.clicked.connect(lambda: self.size_slider.setValue(self.size_slider.value() - 16))
        foot.addWidget(small_ic)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(32, 200)
        self.size_slider.setFixedWidth(92)
        self.size_slider.setToolTip("Preview size")
        self.size_slider.setAccessibleName("Preview size")
        self.size_slider.valueChanged.connect(self._thumb_size_changed)
        foot.addWidget(self.size_slider)
        big_ic = tool_button("image", "Larger previews", size=22, icon_size=16)
        big_ic.clicked.connect(lambda: self.size_slider.setValue(self.size_slider.value() + 16))
        foot.addWidget(big_ic)
        lay.addLayout(foot)

        self._build_menu()
        self._keyboard_timer = QTimer(self)
        self._keyboard_timer.setSingleShot(True)
        self._keyboard_timer.setInterval(90)
        self._keyboard_timer.timeout.connect(self._emit_current)
        provider.thumbReady.connect(self._on_thumb)
        provider.infoReady.connect(self.model.refresh_path)
        self._recursive = True
        self._suppress_activation = False

    def _build_menu(self):
        m = QMenu(self)
        self.act_open = m.addAction("Open Folder…", self.openFolderRequested.emit)
        self.act_rescan = m.addAction("Rescan Folder", self.rescanRequested.emit)
        m.addSeparator()
        self.act_recursive = QAction("Include Subfolders", self, checkable=True)
        self.act_recursive.setChecked(True)
        self.act_recursive.toggled.connect(self.recursiveToggled.emit)
        m.addAction(self.act_recursive)
        self.act_images = QAction("Include Images", self, checkable=True)
        self.act_images.setChecked(True)
        self.act_images.setToolTip("Show PNG, JPEG, TIFF, HEIC and other images next to the PDFs")
        self.act_images.toggled.connect(self.imagesToggled.emit)
        m.addAction(self.act_images)
        m.addSeparator()
        m.addAction("Expand All", self.view.expandAll)
        m.addAction("Collapse All", self.view.collapseAll)
        self.menu_btn.setMenu(m)

    # -------------------------------------------------------------- public
    def set_recursive(self, on: bool):
        self.act_recursive.blockSignals(True)
        self.act_recursive.setChecked(on)
        self.act_recursive.blockSignals(False)

    def set_include_images(self, on: bool):
        self.act_images.blockSignals(True)
        self.act_images.setChecked(on)
        self.act_images.blockSignals(False)

    def set_mode(self, mode: str):
        self.mode_ctrl.set_current(mode)

    def set_thumb_height(self, h: int):
        self.size_slider.blockSignals(True)
        self.size_slider.setValue(h)
        self.size_slider.blockSignals(False)
        self.delegate.thumb_h = h
        self.view.doItemsLayout()

    def set_files(self, title: str, tooltip: str, base: Path | None, files: list[Path], mode: str):
        current = self.model.current_path
        self.title.setText(title)
        self.title.setToolTip(tooltip)
        self.model.set_files(base, files, mode)
        self.model.current_path = current
        self.view.expandAll()
        n = len(files)
        n_img = sum(1 for p in files if p.suffix.lower() != ".pdf")
        if not n:
            text = "No PDFs or images found" if self.act_images.isChecked() else "No PDFs found"
        elif n_img:
            n_pdf = n - n_img
            text = f"{n_pdf} PDF{'s' if n_pdf != 1 else ''}, {n_img} image{'s' if n_img != 1 else ''}"
        else:
            text = f"{n} PDF{'s' if n != 1 else ''}"
        self.count_label.setText(text)
        if current:
            self.select_path(current, emit=False)
        self.provider.prefetch([str(p) for p in self.model.files()], self.delegate.thumb_h * self.devicePixelRatioF())

    def ordered_files(self) -> list[Path]:
        return self.model.files()

    def select_path(self, path: str, emit: bool = False):
        self.model.set_current_path(path)
        src = self.model.index_for_path(path)
        if not src.isValid():
            return
        idx = self.proxy.mapFromSource(src)
        if not idx.isValid():
            return
        self._suppress_activation = not emit
        self.view.selectionModel().setCurrentIndex(idx, self.view.selectionModel().SelectionFlag.ClearAndSelect)
        self._suppress_activation = False
        self.view.scrollTo(idx, QAbstractItemView.ScrollHint.EnsureVisible)

    def selected_paths(self) -> list[str]:
        out: list[str] = []

        def collect(src_idx):
            n: Node = src_idx.internalPointer()
            if n.kind == "file":
                out.append(str(n.path))
            else:
                for i in range(self.model.rowCount(src_idx)):
                    collect(self.model.index(i, 0, src_idx))

        for idx in self.view.selectionModel().selectedIndexes():
            collect(self.proxy.mapToSource(idx))
        return list(dict.fromkeys(out))

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    # ------------------------------------------------------------- internals
    def _mode_changed(self, mode: str):
        self.modeChanged.emit(mode)

    def _filter_changed(self, text: str):
        self.proxy.set_query(text)
        if text:
            self.view.expandAll()

    def _thumb_size_changed(self, v: int):
        self.delegate.thumb_h = v
        self.view.doItemsLayout()
        self.thumbSizeChanged.emit(v)

    def _clicked(self, idx):
        if idx.data(KIND_ROLE) == "file":
            self._keyboard_timer.stop()
            self.fileActivated.emit(idx.data(PATH_ROLE))

    def _current_changed(self, cur, _prev):
        if self._suppress_activation or not cur.isValid() or cur.data(KIND_ROLE) != "file":
            return
        from PySide6.QtWidgets import QApplication

        if QApplication.mouseButtons() == Qt.MouseButton.NoButton:
            self._keyboard_timer.start()

    def _emit_current(self):
        idx = self.view.currentIndex()
        if idx.isValid() and idx.data(KIND_ROLE) == "file":
            self.fileActivated.emit(idx.data(PATH_ROLE))

    def _context_menu(self, pos):
        idx = self.view.indexAt(pos)
        if idx.isValid() and not self.view.selectionModel().isSelected(idx):
            self.view.selectionModel().setCurrentIndex(idx, self.view.selectionModel().SelectionFlag.ClearAndSelect)
        paths = self.selected_paths()
        if not paths:
            return
        pdfs = [p for p in paths if p.lower().endswith(".pdf")]
        images = [p for p in paths if not p.lower().endswith(".pdf")]
        m = QMenu(self)
        if len(paths) == 1:
            m.addAction(icons.icon("viewer"), "Open", lambda: self.fileActivated.emit(paths[0]))
            m.addAction(icons.icon("mark"), "MARK…", lambda: self.markRequested.emit(paths[0]))
            m.addSeparator()
        tools = m.addMenu(icons.icon("tools"), "PDF Tools")
        if images:
            tools.addAction(icons.icon("image"), "Images to PDF…", lambda: self.toolRequested.emit("images_to_pdf", images))
        if pdfs:
            if len(pdfs) > 1:
                tools.addAction(icons.icon("merge"), "Merge PDFs…", lambda: self.toolRequested.emit("merge", pdfs))
            for tid, title, ic in (("organize", "Organize Pages…", "pages"), ("split", "Split…", "split"),
                                   ("compress", "Compress…", "compress"), ("to_images", "Convert to Images…", "to-image"),
                                   ("to_word", "Convert to Word…", "word"), ("ocr", "Make Searchable (OCR)…", "ocr"),
                                   ("watermark", "Watermark or Stamp…", "stamp"), ("protect", "Password Protect…", "lock")):
                tools.addAction(icons.icon(ic), title, lambda tid=tid: self.toolRequested.emit(tid, pdfs))
            if len(pdfs) == 2:
                tools.addAction(icons.icon("compare"), "Compare Revisions…", lambda: self.toolRequested.emit("compare", pdfs))
        tools.addSeparator()
        tools.addAction("All Tools…", lambda: self.toolRequested.emit("", paths))
        from ..platform import reveal_in_file_manager, reveal_label

        m.addSeparator()
        m.addAction(icons.icon("reveal"), reveal_label(), lambda: reveal_in_file_manager(paths[0]))
        m.exec(self.view.viewport().mapToGlobal(pos))

    def _on_thumb(self, key):
        path, page = key
        if page == 0:
            self.model.refresh_path(path)
