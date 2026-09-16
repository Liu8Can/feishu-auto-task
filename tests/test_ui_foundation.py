from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from clockout.ui import (
    ThemeMode,
    theme_qss,
    theme_tokens,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_theme_tokens_resolve_explicit_modes() -> None:
    light = theme_tokens("light")
    dark = theme_tokens(ThemeMode.DARK)

    assert light.mode is ThemeMode.LIGHT
    assert dark.mode is ThemeMode.DARK
    assert light.canvas != dark.canvas
    assert light.accent != light.success != light.error


def test_theme_qss_covers_focus_and_overlay_surfaces() -> None:
    qss = theme_qss(theme_tokens("light"))

    assert qss.index('"Microsoft YaHei UI"') < qss.index('"Segoe UI Variable"')
    assert "QPushButton:focus" in qss
    assert "border: 2px solid" in qss
    assert "QMenu" in qss
    assert "QToolTip" in qss
    assert "QDialog" in qss
    assert "outline: 0" in qss
    assert 'QTextEdit#diagnostics[status="error"]' in qss
    assert "QPushButton#nav:checked" in qss


def test_qt_theme_and_components_in_isolated_application() -> None:
    script = r'''
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QPushButton
from clockout.ui import (
    DiagnosticRow,
    FeedbackBar,
    SemanticStatus,
    StatusPill,
    ThemeMode,
    TimelineRow,
    install_theme,
    load_icon,
    restore_keyboard_focus,
)

app = QApplication([])
manager = install_theme(app, "light")
first_manager = manager
manager.set_mode("dark")
assert manager.resolved_mode is ThemeMode.DARK
assert install_theme(app, "light") is first_manager
assert manager.resolved_mode is ThemeMode.LIGHT
assert app.styleSheet()

button = QPushButton("立即检查")
button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
restore_keyboard_focus(button)
assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus

pill = StatusPill("正常", SemanticStatus.SUCCESS)
feedback = FeedbackBar("绑定成功", "已识别今天的假勤页面", "success")
diagnostic = DiagnosticRow("飞书窗口", "已找到", status_text="正常", status="success")
timeline = TimelineRow("08:30", "上班打卡", "已经确认", status="success")
assert timeline.time_label.width() >= QFontMetrics(timeline.time_label.font()).horizontalAdvance("00:00-00:00")
pill.set_status("warning")
feedback.set_feedback("需要确认", "请打开今天的假勤页面", "warning")
diagnostic.set_result("未找到", "失败", "error", detail="飞书未启动")
timeline.set_entry("18:35", "下班打卡", status="running")
assert pill.status == "warning"
assert feedback.status == "warning"
assert diagnostic.status_pill.status == "error"
assert diagnostic.status_pill.toolTip() == "飞书未启动"
assert timeline.status == "running"
assert timeline.title_label.text() == "下班打卡"
assert not load_icon("name-that-does-not-exist").isNull()

try:
    StatusPill("未知", "mystery")
except ValueError as exc:
    assert "未知语义状态" in str(exc)
else:
    raise AssertionError("unknown status must be rejected")
'''
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_windows_cjk_font_is_registered_for_offscreen_qt() -> None:
    if sys.platform != "win32":
        pytest.skip("Windows 字体注册仅适用于 Windows")
    script = r'''
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import QApplication
from clockout.ui import install_theme

app = QApplication([])
install_theme(app, "light")
families = QFontDatabase.families()
assert "Microsoft YaHei UI" in families or "Microsoft YaHei" in families
metrics = QFontMetrics(QFont("Microsoft YaHei UI", 14))
assert metrics.inFontUcs4(ord("飞"))
assert metrics.inFontUcs4(ord("书"))
'''
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
