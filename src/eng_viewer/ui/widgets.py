"""Small, reusable UI building blocks with consistent styling."""
from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer,
    QVariantAnimation, Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

from . import icons
from .theme import IS_MAC, ThemeManager, qc


def shortcut_text(seq: QKeySequence | str | QKeySequence.StandardKey) -> str:
    if isinstance(seq, QKeySequence.StandardKey):
        ks = QKeySequence(seq)
    elif isinstance(seq, str):
        ks = QKeySequence(seq)
    else:
        ks = seq
    return ks.toString(QKeySequence.SequenceFormat.NativeText)


def tip(text: str, seq=None) -> str:
    if seq is None:
        return text
    sc = shortcut_text(seq)
    return f"{text}  ({sc})" if sc else text


def tool_button(icon_name: str, tooltip: str = "", checkable: bool = False, size: int = 28,
                icon_size: int = 18, text: str | None = None) -> QToolButton:
    b = QToolButton()
    b.setProperty("variant", "ghost")
    b.setIcon(icons.icon(icon_name))
    b.setIconSize(QSize(icon_size, icon_size))
    b.setCheckable(checkable)
    b.setAutoRaise(True)
    b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    b.setCursor(Qt.CursorShape.ArrowCursor)
    if text:
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setMinimumHeight(size)
    else:
        b.setFixedSize(size, size)
    if tooltip:
        b.setToolTip(tooltip)
        b.setAccessibleName(tooltip.split("  (")[0])
    return b


def primary_button(text: str, icon_name: str | None = None) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("variant", "primary")
    if icon_name:
        b.setIcon(icons.icon(icon_name, role="#FFFFFF", on_role=None, active_role=None))
        b.setIconSize(QSize(16, 16))
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def secondary_button(text: str, icon_name: str | None = None) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("variant", "secondary")
    if icon_name:
        b.setIcon(icons.icon(icon_name, role="text", on_role=None, active_role=None))
        b.setIconSize(QSize(16, 16))
    return b


def separator(vertical: bool = True) -> QFrame:
    f = QFrame()
    f.setObjectName("VSep" if vertical else "HSep")
    return f


def label(text: str = "", role: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    return lab


class ElidedLabel(QLabel):
    """Single-line label that elides (default: in the middle) instead of growing."""

    def __init__(self, text: str = "", mode=Qt.TextElideMode.ElideMiddle, parent=None):
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(20)
        self.setText(text)

    def setText(self, text: str):  # noqa: N802 (Qt naming)
        self._full = text or ""
        self.setToolTip(self._full if self._full else "")
        self._refresh()

    def full_text(self) -> str:
        return self._full

    def _refresh(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, self._mode, max(10, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._refresh()

    def sizeHint(self):
        sh = super().sizeHint()
        return QSize(min(sh.width(), 400), sh.height())


class SearchField(QLineEdit):
    def __init__(self, placeholder: str = "Filter", parent=None):
        super().__init__(parent)
        self.setProperty("variant", "search")
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.addAction(icons.icon("search", role="text_tertiary", on_role=None, active_role=None), QLineEdit.ActionPosition.LeadingPosition)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.clear()
            event.accept()
            return
        super().keyPressEvent(event)


class SegmentedControl(QWidget):
    """Compact segmented control with a sliding selection pill."""

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str | None, str | None, str]], parent=None, height: int = 26):
        """options: (key, text or None, icon name or None, tooltip)."""
        super().__init__(parent)
        self._options = options
        self._index = 0
        self._hover = -1
        self._h = height
        self._pill = 0.0
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.setAccessibleName("Segmented control")
        tips = [o[3] for o in options if o[3]]
        if tips:
            self.setToolTip(" · ".join(tips))

    def _seg_width(self, i: int) -> int:
        key, text, icon_name, _ = self._options[i]
        fm = QFontMetrics(self._font())
        w = 14
        if icon_name:
            w += 16
        if text:
            w += fm.horizontalAdvance(text) + (6 if icon_name else 0)
        return max(w, 30)

    def _font(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(9.0, f.pointSizeF() - 1))
        f.setWeight(QFont.Weight.Medium)
        return f

    def sizeHint(self) -> QSize:
        return QSize(sum(self._seg_width(i) for i in range(len(self._options))) + 4, self._h)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _rects(self) -> list[QRectF]:
        x = 2.0
        out = []
        for i in range(len(self._options)):
            w = self._seg_width(i)
            out.append(QRectF(x, 2, w, self._h - 4))
            x += w
        return out

    def current_key(self) -> str:
        return self._options[self._index][0]

    def set_current(self, key: str, animate: bool = False):
        for i, opt in enumerate(self._options):
            if opt[0] == key:
                self._select(i, emit=False, animate=animate)
                return

    def _select(self, i: int, emit: bool = True, animate: bool = True):
        if i == self._index and self._pill == float(i):
            return
        old = self._pill
        self._index = i
        if animate and self.isVisible():
            self._anim.stop()
            self._anim.setStartValue(old)
            self._anim.setEndValue(float(i))
            self._anim.start()
        else:
            self._pill = float(i)
            self.update()
        if emit:
            self.changed.emit(self._options[i][0])

    def _on_anim(self, v):
        self._pill = float(v)
        self.update()

    def mousePressEvent(self, event):
        for i, r in enumerate(self._rects()):
            if r.contains(event.position()):
                self._select(i)
                break

    def mouseMoveEvent(self, event):
        hover = -1
        for i, r in enumerate(self._rects()):
            if r.contains(event.position()):
                hover = i
        if hover != self._hover:
            self._hover = hover
            if hover >= 0:
                self.setToolTip(self._options[hover][3] or "")
            self.update()

    def leaveEvent(self, event):
        self._hover = -1
        self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self._select(max(0, self._index - 1))
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self._select(min(len(self._options) - 1, self._index + 1))
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(0.5, 0.5, self.width() - 1, self._h - 1)
        p.setPen(QPen(qc(t.separator), 1))
        p.setBrush(qc(t.hover))
        p.drawRoundedRect(track, 7, 7)
        rects = self._rects()
        lo = int(self._pill)
        hi = min(lo + 1, len(rects) - 1)
        frac = self._pill - lo
        a, b = rects[lo], rects[hi]
        pill = QRectF(a.x() + (b.x() - a.x()) * frac, a.y(), a.width() + (b.width() - a.width()) * frac, a.height())
        p.setPen(QPen(QColor(0, 0, 0, 30 if not t.dark else 0), 0.5))
        p.setBrush(qc(t.elevated if not t.dark else "#3A3A41"))
        p.drawRoundedRect(pill, 5.5, 5.5)
        p.setFont(self._font())
        for i, (r, (key, text, icon_name, _tip)) in enumerate(zip(rects, self._options)):
            active = i == self._index
            color = qc(t.text if active else (t.text if i == self._hover else t.text_secondary))
            content_w = (16 if icon_name else 0) + (QFontMetrics(self._font()).horizontalAdvance(text) if text else 0) + (6 if icon_name and text else 0)
            x = r.x() + (r.width() - content_w) / 2
            if icon_name:
                icons.paint_icon(p, icon_name, QRectF(x, r.center().y() - 8, 16, 16), color)
                x += 22 if text else 16
            if text:
                p.setPen(color)
                p.drawText(QRectF(x, r.y(), r.right() - x, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        if self.hasFocus():
            p.setPen(QPen(qc(t.accent), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(1, 1, -1, -1), 6, 6)
        p.end()


class Toast(QWidget):
    """Transient confirmation bubble that fades in and out over its parent."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._text = ""
        self._icon = "check"
        self._kind = "info"
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(180)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        parent.installEventFilter(self)
        self.hide()

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self._place()
        return False

    def show_message(self, text: str, icon_name: str = "check", kind: str = "info", msec: int = 2200):
        self._text = text
        self._icon = icon_name
        self._kind = kind
        fm = QFontMetrics(self.font())
        self.resize(min(fm.horizontalAdvance(text) + 58, max(200, self.parent().width() - 40)), 38)
        self._place()
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._hide_timer.start(msec)
        self.update()

    def _fade_out(self):
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _place(self):
        p = self.parent()
        self.move((p.width() - self.width()) // 2, p.height() - self.height() - 26)

    def paintEvent(self, event):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        bg = QColor("#2B2B31") if t.dark else QColor("#1F1F24")
        bg.setAlpha(242)
        p.setPen(QPen(QColor(255, 255, 255, 28), 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        color = qc(t.danger) if self._kind == "error" else QColor("#5BD38E")
        icons.paint_icon(p, self._icon, QRectF(16, (self.height() - 18) / 2, 18, 18), color, stroke=2.0)
        p.setPen(QColor("#F4F4F6"))
        fm = QFontMetrics(self.font())
        p.drawText(QRectF(42, 0, self.width() - 56, self.height()), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(self._text, Qt.TextElideMode.ElideMiddle, self.width() - 56))
        p.end()


class Spinner(QWidget):
    """Tiny indeterminate activity indicator (only animates while running)."""

    def __init__(self, size: int = 14, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def set_running(self, running: bool):
        if running and not self._timer.isActive():
            self._timer.start()
            self.show()
        elif not running:
            self._timer.stop()
            self.hide()

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setPen(QPen(qc(t.separator), 1.6))
        p.drawEllipse(r)
        pen = QPen(qc(t.text_secondary), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, -self._angle * 16, 100 * 16)
        p.end()


class Badge(QWidget):
    """Rounded count/label pill."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._text = text
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_text(self, text: str):
        self._text = text
        self.updateGeometry()
        self.update()
        self.setVisible(bool(text))

    def _font(self):
        f = QFont(self.font())
        f.setPointSizeF(max(8.5, f.pointSizeF() - 2.5))
        f.setWeight(QFont.Weight.DemiBold)
        return f

    def sizeHint(self):
        fm = QFontMetrics(self._font())
        return QSize(max(18, fm.horizontalAdvance(self._text) + 12), 17)

    def paintEvent(self, event):
        t = ThemeManager.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(t.hover if not t.dark else "#34343B"))
        r = QRectF(self.rect())
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setFont(self._font())
        p.setPen(qc(t.text_secondary))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._text)
        p.end()


class SectionHeader(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 4)
        lay.setSpacing(6)
        self.title = label(title.upper(), "section")
        lay.addWidget(self.title)
        self.badge = Badge("")
        self.badge.hide()
        lay.addWidget(self.badge)
        lay.addStretch(1)
        self.trailing = QHBoxLayout()
        self.trailing.setSpacing(2)
        lay.addLayout(self.trailing)

    def add_trailing(self, w: QWidget):
        self.trailing.addWidget(w)


class AppIconLabel(QLabel):
    def __init__(self, pixmap, size: int, parent=None):
        super().__init__(parent)
        pm = pixmap.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        pm.setDevicePixelRatio(2.0)
        self.setPixmap(pm)
        self.setFixedSize(size, size)


class EmptyState(QWidget):
    """Centered welcome panel shown in the viewer before a folder is open."""

    openFolder = Signal()
    openRecent = Signal(str)

    def __init__(self, app_icon, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.addStretch(3)
        col = QVBoxLayout()
        col.setSpacing(10)
        col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        if app_icon is not None and not app_icon.isNull():
            ic = AppIconLabel(app_icon, 72)
            col.addWidget(ic, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addSpacing(6)
        title = label("Open a folder of drawings", "heading")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(title)
        sub = label("PDFs in every subfolder are listed and sorted naturally.\nYou can also drop a folder or PDF files onto this window.", "secondary", wrap=True)
        sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(sub)
        col.addSpacing(8)
        btn = primary_button("Open Folder…", "folder-open")
        btn.clicked.connect(self.openFolder.emit)
        col.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addSpacing(14)
        self.recent_title = label("RECENT", "section")
        self.recent_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(self.recent_title)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(0)
        col.addLayout(self.recent_box)
        outer.addLayout(col)
        outer.addStretch(4)

    def set_recent(self, folders: list[str]):
        while self.recent_box.count():
            item = self.recent_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        shown = 0
        from pathlib import Path

        for f in folders[:5]:
            if not Path(f).is_dir():
                continue
            b = QPushButton(Path(f).name or f)
            b.setProperty("variant", "link")
            b.setToolTip(f)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, f=f: self.openRecent.emit(f))
            self.recent_box.addWidget(b, 0, Qt.AlignmentFlag.AlignHCenter)
            shown += 1
        self.recent_title.setVisible(shown > 0)
