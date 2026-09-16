from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle, QWidget


_STANDARD_ICONS = {
    "check": QStyle.StandardPixmap.SP_DialogApplyButton,
    "success": QStyle.StandardPixmap.SP_DialogApplyButton,
    "info": QStyle.StandardPixmap.SP_MessageBoxInformation,
    "warning": QStyle.StandardPixmap.SP_MessageBoxWarning,
    "error": QStyle.StandardPixmap.SP_MessageBoxCritical,
    "refresh": QStyle.StandardPixmap.SP_BrowserReload,
    "settings": QStyle.StandardPixmap.SP_FileDialogDetailedView,
    "folder": QStyle.StandardPixmap.SP_DirOpenIcon,
}


def _project_asset(name: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    return bundle_root / "assets" / name


def app_icon() -> QIcon:
    for filename in ("app-icon.ico", "app-icon.png"):
        path = _project_asset(filename)
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return load_icon("info")


def load_icon(
    name: str,
    *,
    widget: QWidget | None = None,
    fallback: QStyle.StandardPixmap | None = None,
) -> QIcon:
    """Load a platform theme icon, then fall back to a Qt standard icon."""
    themed = QIcon.fromTheme(name)
    if not themed.isNull():
        return themed
    standard = fallback or _STANDARD_ICONS.get(name, QStyle.StandardPixmap.SP_FileIcon)
    style = widget.style() if widget is not None else QApplication.style()
    return style.standardIcon(standard) if style is not None else QIcon()
