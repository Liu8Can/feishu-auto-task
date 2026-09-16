from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QAbstractButton, QApplication, QWidget


class ThemeMode(str, Enum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


class SemanticStatus(str, Enum):
    NEUTRAL = "neutral"
    INFO = "info"
    RUNNING = "running"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ThemeTokens:
    mode: ThemeMode
    canvas: str
    surface: str
    surface_raised: str
    surface_muted: str
    text: str
    text_muted: str
    text_subtle: str
    border: str
    border_strong: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    focus: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    error: str
    error_soft: str
    tooltip: str
    tooltip_text: str


_LIGHT = ThemeTokens(
    mode=ThemeMode.LIGHT,
    canvas="#f5f5f7",
    surface="#ffffff",
    surface_raised="#ffffff",
    surface_muted="#f0f1f3",
    text="#202124",
    text_muted="#62666d",
    text_subtle="#7b8088",
    border="#dfe1e5",
    border_strong="#c5c8ce",
    accent="#1469d2",
    accent_hover="#0f5fbe",
    accent_pressed="#0b51a4",
    accent_soft="#e9f2ff",
    focus="#2684ff",
    success="#18794e",
    success_soft="#e8f6ee",
    warning="#8a5a00",
    warning_soft="#fff5d6",
    error="#b42336",
    error_soft="#ffedf0",
    tooltip="#25272b",
    tooltip_text="#ffffff",
)

_DARK = ThemeTokens(
    mode=ThemeMode.DARK,
    canvas="#17181b",
    surface="#202226",
    surface_raised="#282a2f",
    surface_muted="#2d3035",
    text="#f3f4f6",
    text_muted="#b5b8bf",
    text_subtle="#92969f",
    border="#383b42",
    border_strong="#50545d",
    accent="#5da5f5",
    accent_hover="#79b5f7",
    accent_pressed="#438edc",
    accent_soft="#1d3a59",
    focus="#7ab8ff",
    success="#65c18c",
    success_soft="#193c2a",
    warning="#e8b64b",
    warning_soft="#483815",
    error="#f07886",
    error_soft="#4b2229",
    tooltip="#f2f3f5",
    tooltip_text="#202124",
)


def _coerce_mode(mode: ThemeMode | str) -> ThemeMode:
    try:
        return ThemeMode(mode)
    except ValueError as exc:
        choices = ", ".join(item.value for item in ThemeMode)
        raise ValueError(f"未知主题 {mode!r}，可选值：{choices}") from exc


def _system_mode(app: QApplication) -> ThemeMode:
    style_hints = app.styleHints()
    color_scheme = getattr(style_hints, "colorScheme", None)
    if callable(color_scheme):
        scheme = color_scheme()
        if scheme == Qt.ColorScheme.Dark:
            return ThemeMode.DARK
        if scheme == Qt.ColorScheme.Light:
            return ThemeMode.LIGHT
    background = app.palette().color(QPalette.ColorRole.Window)
    return ThemeMode.DARK if background.lightness() < 128 else ThemeMode.LIGHT


def theme_tokens(
    mode: ThemeMode | str,
    app: QApplication | None = None,
) -> ThemeTokens:
    selected = _coerce_mode(mode)
    if selected is ThemeMode.SYSTEM:
        application = app or QApplication.instance()
        selected = _system_mode(application) if application else ThemeMode.LIGHT
    return _DARK if selected is ThemeMode.DARK else _LIGHT


def _palette(tokens: ThemeTokens) -> QPalette:
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: tokens.canvas,
        QPalette.ColorRole.WindowText: tokens.text,
        QPalette.ColorRole.Base: tokens.surface,
        QPalette.ColorRole.AlternateBase: tokens.surface_muted,
        QPalette.ColorRole.ToolTipBase: tokens.tooltip,
        QPalette.ColorRole.ToolTipText: tokens.tooltip_text,
        QPalette.ColorRole.Text: tokens.text,
        QPalette.ColorRole.Button: tokens.surface,
        QPalette.ColorRole.ButtonText: tokens.text,
        QPalette.ColorRole.BrightText: tokens.error,
        QPalette.ColorRole.Highlight: tokens.accent,
        QPalette.ColorRole.HighlightedText: "#ffffff" if tokens.mode is ThemeMode.LIGHT else "#101114",
        QPalette.ColorRole.PlaceholderText: tokens.text_subtle,
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(tokens.text_subtle))
    return palette


def theme_qss(tokens: ThemeTokens) -> str:
    return f"""
QWidget {{
    color: {tokens.text};
    font-family: "Segoe UI Variable", "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
}}
QMainWindow, QDialog, QMessageBox, QWidget#root {{ background: {tokens.canvas}; }}
QFrame[uiSurface="true"] {{
    background: {tokens.surface};
    border: 1px solid {tokens.border};
    border-radius: 8px;
}}
QLabel[uiMuted="true"] {{ color: {tokens.text_muted}; }}
QPushButton {{
    min-height: 20px;
    padding: 8px 14px;
    color: {tokens.text};
    background: {tokens.surface};
    border: 1px solid {tokens.border_strong};
    border-radius: 7px;
    outline: 0;
}}
QPushButton:hover {{ background: {tokens.surface_muted}; }}
QPushButton:pressed {{ background: {tokens.border}; }}
QPushButton:focus {{ border: 2px solid {tokens.focus}; padding: 7px 13px; }}
QPushButton:disabled {{ color: {tokens.text_subtle}; background: {tokens.surface_muted}; border-color: {tokens.border}; }}
QPushButton[uiRole="primary"] {{ color: #ffffff; background: {tokens.accent}; border-color: {tokens.accent}; font-weight: 600; }}
QPushButton[uiRole="primary"]:hover {{ background: {tokens.accent_hover}; border-color: {tokens.accent_hover}; }}
QPushButton[uiRole="primary"]:pressed {{ background: {tokens.accent_pressed}; border-color: {tokens.accent_pressed}; }}
QPushButton[uiRole="primary"]:focus {{ border-color: {tokens.focus}; }}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit, QDateEdit, QDateTimeEdit {{
    color: {tokens.text};
    background: {tokens.surface};
    border: 1px solid {tokens.border_strong};
    border-radius: 7px;
    padding: 7px 9px;
    selection-background-color: {tokens.accent};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus, QDateEdit:focus, QDateTimeEdit:focus {{
    border: 2px solid {tokens.focus};
    padding: 6px 8px;
}}
QCheckBox, QRadioButton {{ spacing: 8px; border: 2px solid transparent; border-radius: 6px; padding: 3px; outline: 0; }}
QCheckBox:focus, QRadioButton:focus {{ border-color: {tokens.focus}; }}
QMenu {{ color: {tokens.text}; background: {tokens.surface_raised}; border: 1px solid {tokens.border_strong}; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 10px; border-radius: 6px; }}
QMenu::item:selected {{ color: {tokens.text}; background: {tokens.accent_soft}; }}
QMenu::item:disabled {{ color: {tokens.text_subtle}; }}
QMenu::separator {{ height: 1px; margin: 5px 8px; background: {tokens.border}; }}
QToolTip {{ color: {tokens.tooltip_text}; background: {tokens.tooltip}; border: 0; border-radius: 5px; padding: 6px 8px; }}
QDialogButtonBox QPushButton, QMessageBox QPushButton {{ min-width: 76px; }}
QScrollBar:vertical {{ width: 10px; margin: 2px; background: transparent; }}
QScrollBar::handle:vertical {{ min-height: 24px; border-radius: 4px; background: {tokens.border_strong}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QLabel[uiRole="statusPill"] {{ border-radius: 9px; padding: 2px 8px; font-size: 12px; font-weight: 600; }}
QLabel[uiRole="statusPill"][status="neutral"] {{ color: {tokens.text_muted}; background: {tokens.surface_muted}; }}
QLabel[uiRole="statusPill"][status="info"], QLabel[uiRole="statusPill"][status="running"] {{ color: {tokens.accent}; background: {tokens.accent_soft}; }}
QLabel[uiRole="statusPill"][status="success"] {{ color: {tokens.success}; background: {tokens.success_soft}; }}
QLabel[uiRole="statusPill"][status="warning"] {{ color: {tokens.warning}; background: {tokens.warning_soft}; }}
QLabel[uiRole="statusPill"][status="error"] {{ color: {tokens.error}; background: {tokens.error_soft}; }}
QFrame[uiRole="feedback"] {{ border: 1px solid {tokens.border}; border-radius: 8px; background: {tokens.surface}; }}
QFrame[uiRole="feedback"][status="info"], QFrame[uiRole="feedback"][status="running"] {{ border-color: {tokens.accent}; background: {tokens.accent_soft}; }}
QFrame[uiRole="feedback"][status="success"] {{ border-color: {tokens.success}; background: {tokens.success_soft}; }}
QFrame[uiRole="feedback"][status="warning"] {{ border-color: {tokens.warning}; background: {tokens.warning_soft}; }}
QFrame[uiRole="feedback"][status="error"] {{ border-color: {tokens.error}; background: {tokens.error_soft}; }}
QLabel[uiRole="feedbackTitle"] {{ font-weight: 600; }}
QFrame[uiRole="diagnosticRow"], QFrame[uiRole="timelineRow"] {{ background: transparent; border: 0; border-bottom: 1px solid {tokens.border}; }}
QFrame[uiRole="timelineMarker"] {{ min-width: 9px; max-width: 9px; min-height: 9px; max-height: 9px; border-radius: 4px; background: {tokens.border_strong}; }}
QFrame[uiRole="timelineMarker"][status="info"], QFrame[uiRole="timelineMarker"][status="running"] {{ background: {tokens.accent}; }}
QFrame[uiRole="timelineMarker"][status="success"] {{ background: {tokens.success}; }}
QFrame[uiRole="timelineMarker"][status="warning"] {{ background: {tokens.warning}; }}
QFrame[uiRole="timelineMarker"][status="error"] {{ background: {tokens.error}; }}
"""


class ThemeManager(QObject):
    themeChanged = Signal(str)

    def __init__(self, app: QApplication, mode: ThemeMode | str = ThemeMode.SYSTEM) -> None:
        super().__init__(app)
        self._app = app
        self._mode = _coerce_mode(mode)
        signal = getattr(app.styleHints(), "colorSchemeChanged", None)
        if signal is not None:
            signal.connect(self._system_theme_changed)
        self._apply()

    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @property
    def resolved_mode(self) -> ThemeMode:
        return theme_tokens(self._mode, self._app).mode

    def set_mode(self, mode: ThemeMode | str) -> None:
        selected = _coerce_mode(mode)
        if selected is self._mode:
            return
        self._mode = selected
        self._apply()

    def _system_theme_changed(self, _scheme: Qt.ColorScheme) -> None:
        if self._mode is ThemeMode.SYSTEM:
            self._apply()

    def _apply(self) -> None:
        tokens = theme_tokens(self._mode, self._app)
        self._app.setStyle("Fusion")
        self._app.setPalette(_palette(tokens))
        self._app.setStyleSheet(theme_qss(tokens))
        self.themeChanged.emit(tokens.mode.value)


def install_theme(
    app: QApplication,
    mode: ThemeMode | str = ThemeMode.SYSTEM,
) -> ThemeManager:
    existing = getattr(app, "_clockout_theme_manager", None)
    if isinstance(existing, ThemeManager):
        existing.set_mode(mode)
        return existing
    manager = ThemeManager(app, mode)
    setattr(app, "_clockout_theme_manager", manager)
    return manager


def restore_keyboard_focus(root: QWidget) -> None:
    """Restore tab navigation after legacy UI code has disabled button focus."""
    buttons = root.findChildren(QAbstractButton)
    if isinstance(root, QAbstractButton):
        buttons.insert(0, root)
    for button in buttons:
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
