"""Design tokens, light/dark palettes and the application stylesheet.

macOS keeps the native ``macos`` style so dialogs, combo boxes, check boxes and
overlay scroll bars look and behave like the system. Windows uses Fusion with
the same tokens, which gives a consistent modern look that does not depend on
the Windows version. The app's own chrome (toolbars, sidebars, lists) is styled
identically on both from the tokens below.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

FALLBACK_ACCENT = "#0A7CFF"

_RGBA = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)")


def qc(value: str) -> QColor:
    """QColor from a token: hex, a named colour, or CSS ``rgba(r, g, b, a)``.

    Tokens use CSS syntax so the same strings work in style sheets, but QColor
    itself cannot parse ``rgba()``.
    """
    m = _RGBA.fullmatch(value.strip()) if isinstance(value, str) else None
    if m:
        r, g, b, a = m.groups()
        c = QColor(int(float(r)), int(float(g)), int(float(b)))
        c.setAlphaF(float(a) if a is not None else 1.0)
        return c
    return QColor(value)


@dataclass(frozen=True)
class Theme:
    dark: bool
    accent: str
    window: str
    toolbar: str
    sidebar: str
    canvas: str
    elevated: str
    text: str
    text_secondary: str
    text_tertiary: str
    text_disabled: str
    separator: str
    hover: str
    pressed: str
    selected: str
    selected_strong: str
    control: str
    control_border: str
    field: str
    field_border: str
    tooltip_bg: str
    tooltip_text: str
    danger: str
    success: str
    page_border: str
    page_shadow: str

    def c(self, name: str) -> QColor:
        return qc(getattr(self, name))

    @property
    def accent_hover(self) -> str:
        return QColor(self.accent).lighter(110 if self.dark else 108).name()

    @property
    def accent_pressed(self) -> str:
        return QColor(self.accent).darker(112).name()


def _alpha(color: str, a: float) -> str:
    c = qc(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {a:.3f})"


def make_theme(dark: bool, accent: str) -> Theme:
    if dark:
        return Theme(
            dark=True,
            accent=accent,
            window="#1C1C1F",
            toolbar="#232327",
            sidebar="#1E1E21",
            canvas="#131315",
            elevated="#2A2A2F",
            text="#EDEDF0",
            text_secondary="#A3A3AD",
            text_tertiary="#72727C",
            text_disabled="#55555D",
            separator="#2F2F35",
            hover="rgba(255, 255, 255, 0.065)",
            pressed="rgba(255, 255, 255, 0.11)",
            selected=_alpha(accent, 0.24),
            selected_strong=_alpha(accent, 0.36),
            control="#2C2C31",
            control_border="#3A3A41",
            field="#27272C",
            field_border="#38383F",
            tooltip_bg="#2E2E34",
            tooltip_text="#EDEDF0",
            danger="#F2555A",
            success="#3FB97A",
            page_border="rgba(255, 255, 255, 0.10)",
            page_shadow="rgba(0, 0, 0, 0.55)",
        )
    return Theme(
        dark=False,
        accent=accent,
        window="#F5F5F7",
        toolbar="#F7F7F9",
        sidebar="#F2F2F5",
        canvas="#E4E4E9",
        elevated="#FFFFFF",
        text="#1C1C21",
        text_secondary="#686873",
        text_tertiary="#9898A2",
        text_disabled="#B8B8C0",
        separator="#DEDEE3",
        hover="rgba(0, 0, 0, 0.05)",
        pressed="rgba(0, 0, 0, 0.09)",
        selected=_alpha(accent, 0.14),
        selected_strong=_alpha(accent, 0.22),
        control="#FFFFFF",
        control_border="#D3D3DA",
        field="#FFFFFF",
        field_border="#D6D6DD",
        tooltip_bg="#FFFFFF",
        tooltip_text="#1C1C21",
        danger="#E5383E",
        success="#1F9D5A",
        page_border="rgba(0, 0, 0, 0.12)",
        page_shadow="rgba(0, 0, 0, 0.18)",
    )


def build_stylesheet(t: Theme, base_pt: float, native_mac_style: bool = IS_MAC) -> str:
    small = max(9.0, base_pt - 2)
    tiny = max(8.5, base_pt - 2.5)
    css = f"""
QMainWindow, QDialog#Sheet {{ background: {t.window}; }}
QWidget#Toolbar {{ background: {t.toolbar}; border-bottom: 1px solid {t.separator}; }}
QWidget#Sidebar, QWidget#Inspector {{ background: {t.sidebar}; }}
QWidget#Panel {{ background: transparent; }}
QWidget#StatusStrip {{ background: {t.toolbar}; border-top: 1px solid {t.separator}; }}
QWidget#OptionsBar {{ background: {t.toolbar}; border-bottom: 1px solid {t.separator}; }}
QWidget#BottomBar {{ background: {t.toolbar}; border-top: 1px solid {t.separator}; }}
QFrame#Card {{ background: {t.elevated}; border: 1px solid {t.separator}; border-radius: 12px; }}
QFrame#HSep {{ background: {t.separator}; border: none; max-height: 1px; min-height: 1px; }}
QFrame#VSep {{ background: {t.separator}; border: none; max-width: 1px; min-width: 1px; margin: 6px 4px; }}

QLabel {{ color: {t.text}; background: transparent; }}
QLabel[role="secondary"] {{ color: {t.text_secondary}; }}
QLabel[role="tertiary"] {{ color: {t.text_tertiary}; font-size: {small}pt; }}
QLabel[role="small"] {{ color: {t.text_secondary}; font-size: {small}pt; }}
QLabel[role="section"] {{ color: {t.text_tertiary}; font-size: {tiny}pt; font-weight: 600; letter-spacing: 0.5px; }}
QLabel[role="title"] {{ color: {t.text}; font-weight: 600; }}
QLabel[role="heading"] {{ color: {t.text}; font-size: {base_pt + 5}pt; font-weight: 600; }}

QToolButton[variant="ghost"] {{
    background: transparent; border: none; border-radius: 6px; padding: 3px; color: {t.text_secondary};
}}
QToolButton[variant="ghost"]:hover {{ background: {t.hover}; color: {t.text}; }}
QToolButton[variant="ghost"]:pressed {{ background: {t.pressed}; }}
QToolButton[variant="ghost"]:checked {{ background: {t.selected}; color: {t.accent}; }}
QToolButton[variant="ghost"]:disabled {{ color: {t.text_disabled}; }}
QToolButton[variant="ghost"]::menu-indicator {{ image: none; width: 0px; }}
QToolButton[variant="ghost"][popupMode="2"] {{ padding-right: 3px; }}
QToolButton[variant="ghost"]:focus {{ outline: none; }}

QPushButton[variant="primary"] {{
    background: {t.accent}; color: #FFFFFF; border: none; border-radius: 6px;
    padding: 5px 14px; font-weight: 600; min-height: 18px;
}}
QPushButton[variant="primary"]:hover {{ background: {t.accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {t.accent_pressed}; }}
QPushButton[variant="primary"]:disabled {{ background: {t.control}; color: {t.text_disabled}; border: 1px solid {t.control_border}; }}
QPushButton[variant="secondary"] {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: 6px;
    padding: 4px 12px; min-height: 18px;
}}
QPushButton[variant="secondary"]:hover {{ background: {t.hover if t.dark else '#F6F6F8'}; border-color: {t.control_border}; }}
QPushButton[variant="secondary"]:pressed {{ background: {t.pressed}; }}
QPushButton[variant="secondary"]:disabled {{ color: {t.text_disabled}; }}
QPushButton[variant="link"] {{ background: transparent; border: none; color: {t.accent}; padding: 2px 4px; text-align: left; }}
QPushButton[variant="link"]:hover {{ text-decoration: underline; }}
QPushButton[variant="danger-x"] {{ background: transparent; border: none; border-radius: 10px; padding: 0px; }}
QPushButton[variant="danger-x"]:hover {{ background: {_alpha(t.danger, 0.14)}; }}

QLineEdit[variant="search"], QLineEdit[variant="field"] {{
    background: {t.field}; color: {t.text}; border: 1px solid {t.field_border}; border-radius: 7px;
    padding: 4px 6px; selection-background-color: {t.accent}; selection-color: #FFFFFF;
}}
QLineEdit[variant="search"]:focus, QLineEdit[variant="field"]:focus {{ border: 1px solid {t.accent}; }}
QLineEdit[variant="page"] {{
    background: {t.field}; color: {t.text}; border: 1px solid {t.field_border}; border-radius: 5px;
    padding: 1px 2px; selection-background-color: {t.accent};
}}
QLineEdit[variant="page"]:focus {{ border: 1px solid {t.accent}; }}

QTreeView#Library, QListView#Pages, QListView#Queue, QScrollArea#Comments, QWidget#CommentsInner {{
    background: transparent; border: none; outline: none; show-decoration-selected: 0;
}}
QTreeView#Library::item, QTreeView#Library::item:selected, QTreeView#Library::item:hover,
QTreeView#Library::branch, QTreeView#Library::branch:selected, QTreeView#Library::branch:hover,
QListView#Pages::item, QListView#Pages::item:selected, QListView#Queue::item, QListView#Queue::item:selected {{
    background: transparent; border: none;
}}
QListWidget#ToolNav {{ background: transparent; border: none; outline: none; }}
QListWidget#ToolNav::item {{ border: none; border-radius: 6px; padding: 3px 4px; }}
QListWidget#ToolNav::item:hover {{ background: {t.hover}; }}
QListWidget#ToolNav::item:selected {{ background: {t.selected}; color: {t.text}; }}
QSplitter::handle {{ background: {t.separator}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QToolTip {{
    background: {t.tooltip_bg}; color: {t.tooltip_text}; border: 1px solid {t.separator};
    border-radius: 6px; padding: 4px 7px;
}}
QPlainTextEdit#CommentEdit, QTextEdit#CommentEdit {{
    background: {t.field}; color: {t.text}; border: 1px solid {t.field_border}; border-radius: 7px;
    padding: 3px 4px; selection-background-color: {t.accent}; selection-color: #FFFFFF;
}}
QPlainTextEdit#CommentEdit:focus, QTextEdit#CommentEdit:focus {{ border: 1px solid {t.accent}; }}
QProgressBar {{ background: {t.control}; border: none; border-radius: 3px; height: 6px; color: transparent; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 3px; }}
"""
    if not native_mac_style:
        # The macOS style keeps native overlay scroll bars; elsewhere use slim modern ones.
        css += f"""
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {_alpha('#FFFFFF' if t.dark else '#000000', 0.22)}; border-radius: 3px; min-height: 28px; min-width: 28px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{ background: {_alpha('#FFFFFF' if t.dark else '#000000', 0.38)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMenu {{ background: {t.elevated}; color: {t.text}; border: 1px solid {t.separator}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 10px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t.accent}; color: #FFFFFF; }}
QMenu::item:disabled {{ color: {t.text_disabled}; }}
QMenu::separator {{ height: 1px; background: {t.separator}; margin: 4px 6px; }}
QComboBox {{ background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: 6px; padding: 3px 8px; }}
QComboBox:hover {{ border-color: {t.accent}; }}
QComboBox QAbstractItemView {{ background: {t.elevated}; color: {t.text}; selection-background-color: {t.accent}; border: 1px solid {t.separator}; }}
QSpinBox, QDoubleSpinBox {{ background: {t.field}; color: {t.text}; border: 1px solid {t.field_border}; border-radius: 6px; padding: 2px 6px; }}
"""
    return css


def make_palette(t: Theme) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.window,
        QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.field,
        QPalette.ColorRole.AlternateBase: t.sidebar,
        QPalette.ColorRole.ToolTipBase: t.tooltip_bg,
        QPalette.ColorRole.ToolTipText: t.tooltip_text,
        QPalette.ColorRole.PlaceholderText: t.text_tertiary,
        QPalette.ColorRole.Text: t.text,
        QPalette.ColorRole.Button: t.control,
        QPalette.ColorRole.ButtonText: t.text,
        QPalette.ColorRole.BrightText: "#FFFFFF",
        QPalette.ColorRole.Highlight: t.accent,
        QPalette.ColorRole.HighlightedText: "#FFFFFF",
        QPalette.ColorRole.Link: t.accent,
        QPalette.ColorRole.Accent: t.accent,
        QPalette.ColorRole.Light: "#3A3A40" if t.dark else "#FFFFFF",
        QPalette.ColorRole.Midlight: "#33333A" if t.dark else "#EDEDF0",
        QPalette.ColorRole.Mid: "#2A2A30" if t.dark else "#C9C9D0",
        QPalette.ColorRole.Dark: "#121214" if t.dark else "#A0A0A8",
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(QPalette.ColorGroup.All, role, qc(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, qc(t.text_disabled))
    return p


class ThemeManager(QObject):
    """Single source of truth for the active theme. Listens to the OS setting."""

    changed = Signal(object)  # Theme

    _instance: "ThemeManager | None" = None

    def __init__(self, app: QApplication, mode: str = "system"):
        super().__init__(app)
        ThemeManager._instance = self
        self._app = app
        accent = app.palette().color(QPalette.ColorRole.Accent)
        self._accent = accent.name() if accent.isValid() and accent.saturation() > 40 else FALLBACK_ACCENT
        self._mode = mode
        self._base_pt = app.font().pointSizeF() if app.font().pointSizeF() > 0 else 13.0
        self.theme = make_theme(self._system_is_dark(), self._accent)
        app.styleHints().colorSchemeChanged.connect(self._on_system_scheme)

    @classmethod
    def instance(cls) -> "ThemeManager":
        assert cls._instance is not None, "ThemeManager not created"
        return cls._instance

    @classmethod
    def current(cls) -> Theme:
        if cls._instance is None:
            return make_theme(False, FALLBACK_ACCENT)
        return cls._instance.theme

    @property
    def mode(self) -> str:
        return self._mode

    def _system_is_dark(self) -> bool:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark

    def set_mode(self, mode: str):
        self._mode = mode if mode in ("system", "light", "dark") else "system"
        self.apply()

    def _on_system_scheme(self, _scheme):
        if self._mode == "system":
            self.apply()

    def apply(self):
        hints = self._app.styleHints()
        if hasattr(hints, "setColorScheme"):
            # Drives the native title bar, menus and dialogs into the same appearance.
            target = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(self._mode, Qt.ColorScheme.Unknown)
            if hints.colorScheme() != target or target == Qt.ColorScheme.Unknown:
                try:
                    hints.colorSchemeChanged.disconnect(self._on_system_scheme)
                except (RuntimeError, TypeError):
                    pass
                hints.setColorScheme(target)
                hints.colorSchemeChanged.connect(self._on_system_scheme)
        dark = self._mode == "dark" or (self._mode == "system" and self._system_is_dark())
        self.theme = make_theme(dark, self._accent)
        self._app.setPalette(make_palette(self.theme))
        native_mac = self._app.style().name().lower() == "macos"
        self._app.setStyleSheet(build_stylesheet(self.theme, self._base_pt, native_mac))
        from . import icons

        icons.clear_cache()
        self.changed.emit(self.theme)
        for w in self._app.topLevelWidgets():
            w.update()

    def with_accent(self, accent: str) -> Theme:
        return replace(self.theme, accent=accent)


def configure_app_font(app: QApplication) -> None:
    if IS_WIN:
        app.setFont(windows_ui_font())


# Windows 11's interface font is Segoe UI Variable (its "Text" optical size for body text);
# Windows 10 has Segoe UI. Font databases list the variable font either by its named instances or
# by its family name, so look for each.
WINDOWS_FONTS = ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI")


def windows_ui_font() -> QFont:
    from PySide6.QtGui import QFontDatabase

    families = set(QFontDatabase.families())
    family = next((f for f in WINDOWS_FONTS if f in families), "Segoe UI")
    font = QFont(family)
    font.setPointSizeF(9.5)
    return font
