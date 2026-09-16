from __future__ import annotations

import ctypes
import getpass
import hashlib
import logging
import os
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRunnable,
    Qt,
    QThreadPool,
    QTime,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShowEvent,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .config import AppConfig, load_config, save_config
from .core import AttendanceSnapshot, CheckResult, calculate_eligible_time
from .engine import ClockoutEngine, EngineConfig
from .feishu_uia import FeishuUiaAdapter
from .runtime import (
    RuntimePaths,
    is_start_with_windows_enabled,
    open_folder,
    prepare_runtime_paths,
    runtime_paths,
    set_start_with_windows,
)
from .storage import JsonStateStore, StateStoreError


APP_TITLE = "飞书动态下班打卡助手"
STATUS_TEXT = {
    "idle": "准备就绪",
    "checking": "正在检查",
    "waiting": "等待目标时间",
    "dry_run_ready": "演练条件通过",
    "success": "今日打卡成功",
    "blocked": "等待条件恢复",
    "skipped": "等待检查时段",
    "already_attempted": "等待人工核验",
    "already_clocked_out": "今日已经打卡",
    "unknown": "结果需要确认",
    "retry_waiting": "等待自动重试",
    "retry_exhausted": "今日重试已停止",
}


def _apply_light_titlebar(window: QMainWindow) -> None:
    if sys.platform != "win32":
        return
    try:
        handle = int(window.winId())
        dark_mode = ctypes.c_int(0)
        white = ctypes.c_int(0x00FFFFFF)
        dark_text = ctypes.c_int(0x001F1D1D)
        border = ctypes.c_int(0x00D7D2D2)
        for attribute, value in ((20, dark_mode), (35, white), (36, dark_text), (34, border)):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                handle,
                attribute,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
    except (AttributeError, OSError):
        pass


def _apply_light_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#f5f5f7",
        QPalette.ColorRole.WindowText: "#1d1d1f",
        QPalette.ColorRole.Base: "#ffffff",
        QPalette.ColorRole.AlternateBase: "#f5f5f7",
        QPalette.ColorRole.ToolTipBase: "#2c2c2e",
        QPalette.ColorRole.ToolTipText: "#ffffff",
        QPalette.ColorRole.Text: "#1d1d1f",
        QPalette.ColorRole.Button: "#ffffff",
        QPalette.ColorRole.ButtonText: "#1d1d1f",
        QPalette.ColorRole.Highlight: "#0071e3",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.PlaceholderText: "#86868b",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor("#86868b"),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor("#86868b"),
    )
    app.setPalette(palette)


APP_QSS = """
QWidget {
    color: #1d1d1f;
    font-family: "Segoe UI Variable", "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
}
QMainWindow, QWidget#root { background: #f5f5f7; }
QFrame#topbar { background: #ffffff; border-bottom: 1px solid #e6e6e9; }
QFrame#tabs { background: #f5f5f7; border: 0; }
QLabel#brand { font-size: 17px; font-weight: 700; color: #1d1d1f; }
QLabel#eyebrow { color: #6e6e73; font-size: 12px; }
QLabel#pageTitle { font-size: 24px; font-weight: 700; color: #1d1d1f; }
QLabel#pageSubtitle { color: #6e6e73; font-size: 13px; }
QPushButton#nav {
    min-width: 82px; padding: 8px 16px; border: 0; border-radius: 8px;
    color: #6e6e73; background: transparent; font-weight: 600;
}
QPushButton#nav:hover { background: #eaeaee; color: #1d1d1f; }
QPushButton#nav:checked { background: #ffffff; color: #0071e3; font-weight: 700; }
QPushButton#primary {
    background: #0071e3; color: white; border: 0; border-radius: 8px;
    padding: 9px 16px; font-weight: 700;
}
QPushButton#primary:hover { background: #0077ed; }
QPushButton#primary:pressed { background: #0068d1; }
QPushButton#primary:disabled { background: #b8cbe0; }
QPushButton#secondary {
    background: #ffffff; color: #1d1d1f; border: 1px solid #d2d2d7;
    border-radius: 8px; padding: 8px 14px;
}
QPushButton#secondary:hover { background: #f7f7f8; border-color: #b8b8bd; }
QPushButton#danger {
    background: transparent; color: #d70015; border: 1px solid #edb8bd;
    border-radius: 8px; padding: 8px 14px;
}
QPushButton#danger:hover { background: #fff2f3; }
QFrame#statusPanel {
    background: #ffffff; border: 1px solid #e1e1e5; border-radius: 8px;
}
QLabel#statusTitle { font-size: 23px; font-weight: 700; color: #1d1d1f; }
QLabel#statusMessage { color: #6e6e73; }
QLabel#metricValue { font-size: 22px; font-weight: 700; color: #1d1d1f; }
QLabel#metricLabel { color: #86868b; font-size: 12px; font-weight: 600; }
QFrame#metricCell { border-right: 1px solid #e7e7eb; }
QFrame#sectionLine { background: #e5e5e9; min-height: 1px; max-height: 1px; }
QLabel#sectionTitle { font-size: 15px; font-weight: 700; color: #1d1d1f; }
QLabel#small { color: #86868b; font-size: 12px; }
QLabel#warning {
    color: #5c4b18; background: #fff9e8; border: 1px solid #eee2bd;
    border-radius: 8px; padding: 10px 12px;
}
QComboBox, QSpinBox, QTimeEdit, QTextEdit {
    background: #ffffff; border: 1px solid #d2d2d7; border-radius: 8px;
    padding: 8px 10px; selection-background-color: #cce5ff;
}
QComboBox:focus, QSpinBox:focus, QTimeEdit:focus, QTextEdit:focus {
    border-color: #0071e3;
}
QTextEdit { padding: 11px; font-family: "Microsoft YaHei UI", "Segoe UI Variable"; font-size: 13px; }
QCheckBox { spacing: 9px; }
QMenu { background: #ffffff; border: 1px solid #d2d2d7; padding: 6px; }
QMenu::item { padding: 7px 24px 7px 10px; border-radius: 6px; }
QMenu::item:selected { background: #eaf4ff; color: #0071e3; }
QToolTip { background: #2c2c2e; color: white; border: 0; padding: 5px 7px; }
QMessageBox, QDialog { background: #f5f5f7; color: #1d1d1f; }
QMessageBox QLabel, QDialog QLabel { color: #1d1d1f; background: transparent; }
QMessageBox QPushButton, QDialogButtonBox QPushButton {
    min-width: 76px; background: #ffffff; color: #1d1d1f;
    border: 1px solid #d2d2d7; border-radius: 8px; padding: 7px 14px;
    outline: none;
}
QMessageBox QPushButton:hover, QDialogButtonBox QPushButton:hover {
    background: #f0f0f2; border-color: #b8b8bd;
}
QLineEdit {
    background: #ffffff; color: #1d1d1f; border: 1px solid #d2d2d7;
    border-radius: 8px; padding: 8px 10px; selection-background-color: #cce5ff;
}
QPushButton:focus { outline: none; }
"""


class ToggleSwitch(QAbstractButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(46, 26)
        self.setToolTip("开启或暂停后台监控")

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#34c759" if self.isChecked() else "#aeaeb2"))
        painter.drawRoundedRect(self.rect(), 13, 13)
        painter.setBrush(QColor("#ffffff"))
        x = 23 if self.isChecked() else 3
        painter.drawEllipse(x, 3, 20, 20)


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    def __init__(self, function: Callable[[], object], task_name: str) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.function = function
        self.task_name = task_name
        self.signals = WorkerSignals()

    def run(self) -> None:
        logger = logging.getLogger("clockout")
        logger.info("后台任务开始：%s", self.task_name)
        try:
            result = self.function()
        except Exception as exc:
            logger.exception("后台任务异常：%s", self.task_name)
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        logger.info("后台任务完成：%s", self.task_name)
        self.signals.finished.emit(result)


def _tray_icon(color: str = "#0071e3") -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#ffffff"))
    painter.drawRoundedRect(5, 5, 54, 54, 15, 15)
    painter.setPen(QPen(QColor("#c7c7cc"), 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(6, 6, 52, 52, 14, 14)
    painter.setPen(QPen(QColor(color), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawArc(17, 17, 30, 30, 90 * 16, -285 * 16)
    painter.setPen(QPen(QColor("#1d1d1f"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(QPoint(32, 32), QPoint(32, 22))
    painter.drawLine(QPoint(32, 32), QPoint(40, 36))
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    def __init__(self, controller: "AppController") -> None:
        super().__init__()
        self.controller = controller
        self._really_close = False
        self._tray_hint_shown = False
        self.setObjectName("root")
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(_tray_icon())
        self.resize(880, 650)
        self.setMinimumSize(800, 590)
        self._build()

    def _build(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        shell = QVBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        topbar = QFrame(objectName="topbar")
        topbar.setFixedHeight(72)
        top = QHBoxLayout(topbar)
        top.setContentsMargins(28, 14, 28, 14)
        identity = QVBoxLayout()
        identity.setSpacing(1)
        brand = QLabel("下班打卡助手", objectName="brand")
        subtitle = QLabel("飞书本机自动化", objectName="eyebrow")
        identity.addWidget(brand)
        identity.addWidget(subtitle)
        top.addLayout(identity)
        top.addStretch(1)
        self.binding_label = QLabel("页面绑定：待检查", objectName="small")
        top.addWidget(self.binding_label)
        shell.addWidget(topbar)

        self.stack = QStackedWidget()
        self.nav_buttons: list[QPushButton] = []
        tabs = QFrame(objectName="tabs")
        tab_layout = QHBoxLayout(tabs)
        tab_layout.setContentsMargins(28, 10, 28, 2)
        tab_layout.setSpacing(4)
        pages = (
            ("今天", self._today_page),
            ("规则", self._settings_page),
            ("连接", self._diagnostics_page),
            ("记录", self._logs_page),
        )
        for index, (label, builder) in enumerate(pages):
            button = QPushButton(label, objectName="nav")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self._show_page(i))
            tab_layout.addWidget(button)
            self.nav_buttons.append(button)
            self.stack.addWidget(builder())
        tab_layout.addStretch(1)
        shell.addWidget(tabs)
        shell.addWidget(self.stack, 1)
        for button in self.findChildren(QPushButton):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._show_page(0)

    def _page_shell(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 20, 28, 24)
        layout.setSpacing(0)
        layout.addWidget(QLabel(title, objectName="pageTitle"))
        detail = QLabel(subtitle, objectName="pageSubtitle")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        layout.addSpacing(18)
        return page, layout

    def _today_page(self) -> QWidget:
        page, layout = self._page_shell("今日状态", datetime.now().strftime("%Y 年 %m 月 %d 日"))
        panel = QFrame(objectName="statusPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(20, 18, 20, 18)
        top = QHBoxLayout()
        status_group = QVBoxLayout()
        self.status_caption = QLabel("监控中", objectName="eyebrow")
        self.status_title = QLabel("等待检查", objectName="statusTitle")
        self.status_message = QLabel("后台监控已准备", objectName="statusMessage")
        self.status_message.setWordWrap(True)
        status_group.addWidget(self.status_caption)
        status_group.addWidget(self.status_title)
        status_group.addWidget(self.status_message)
        top.addLayout(status_group, 1)
        monitor_box = QHBoxLayout()
        self.monitor_text = QLabel("已开启")
        self.monitor_switch = ToggleSwitch()
        self.monitor_switch.setChecked(self.controller.config.monitor_enabled)
        self.monitor_switch.toggled.connect(self.controller.set_monitor_enabled)
        monitor_box.addWidget(self.monitor_text)
        monitor_box.addWidget(self.monitor_switch)
        top.addLayout(monitor_box)
        panel_layout.addLayout(top)
        panel_layout.addSpacing(17)

        metrics = QHBoxLayout()
        metrics.setSpacing(0)
        self.checkin_value = self._add_metric(metrics, "今日上班", "--:--")
        self.eligible_value = self._add_metric(metrics, "最早下班", "--:--")
        self.next_value = self._add_metric(metrics, "下次检查", "--:--", last=True)
        panel_layout.addLayout(metrics)
        layout.addWidget(panel)
        layout.addSpacing(14)

        actions = QHBoxLayout()
        check = QPushButton("立即检查", objectName="primary")
        check.clicked.connect(self.controller.run_check_now)
        settings = QPushButton("时间设置", objectName="secondary")
        settings.clicked.connect(lambda: self._show_page(1))
        more = QPushButton("更多", objectName="secondary")
        more_menu = QMenu(more)
        more_menu.addAction("打开飞书假勤", self.controller.open_attendance)
        more_menu.addAction("重新绑定页面", self.controller.calibrate)
        more_menu.addSeparator()
        self.reset_action = more_menu.addAction("重置今日失败状态", self.controller.request_reset)
        more.setMenu(more_menu)
        actions.addWidget(check)
        actions.addWidget(settings)
        actions.addWidget(more)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addSpacing(22)

        section = QHBoxLayout()
        section.addWidget(QLabel("今日计算", objectName="sectionTitle"))
        section.addStretch(1)
        self.mode_badge = QLabel("动态计算", objectName="small")
        section.addWidget(self.mode_badge)
        layout.addLayout(section)
        layout.addSpacing(10)
        timeline = QFrame(objectName="statusPanel")
        grid = QGridLayout(timeline)
        grid.setContentsMargins(18, 15, 18, 15)
        grid.setHorizontalSpacing(24)
        labels = (("上班记录", "08:50"), ("休息时段", "12:00-14:00"), ("有效工时", "8 小时"), ("安全缓冲", "5 分钟"))
        self.calc_values: list[QLabel] = []
        for column, (label, value) in enumerate(labels):
            grid.addWidget(QLabel(label, objectName="metricLabel"), 0, column)
            item = QLabel(value)
            item.setStyleSheet("font-weight: 650; padding-top: 4px;")
            grid.addWidget(item, 1, column)
            self.calc_values.append(item)
        layout.addWidget(timeline)
        layout.addSpacing(12)
        self.retry_note = QLabel("自动重试最多 3 次，仅限点击前失败；点击后结果不明确不会自动重复点击。", objectName="warning")
        self.retry_note.setWordWrap(True)
        layout.addWidget(self.retry_note)
        layout.addStretch(1)
        return page

    def _add_metric(self, layout: QHBoxLayout, label: str, value: str, *, last: bool = False) -> QLabel:
        frame = QFrame(objectName="" if last else "metricCell")
        frame.setMinimumWidth(150)
        box = QVBoxLayout(frame)
        box.setContentsMargins(0 if not layout.count() else 18, 0, 18, 0)
        box.addWidget(QLabel(label, objectName="metricLabel"))
        value_label = QLabel(value, objectName="metricValue")
        box.addWidget(value_label)
        layout.addWidget(frame, 1)
        return value_label

    def _settings_page(self) -> QWidget:
        page, layout = self._page_shell("时间与运行", "设置会保存在本机，并在下次启动时继续使用。")
        form = QGridLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(14)
        self.calc_mode = QComboBox()
        self.calc_mode.addItem("按实际打卡时间计算", "dynamic")
        self.calc_mode.addItem("使用固定上下班时间", "fixed")
        self.work_minutes = QSpinBox(); self.work_minutes.setRange(1, 1440); self.work_minutes.setSuffix(" 分钟")
        self.buffer_minutes = QSpinBox(); self.buffer_minutes.setRange(0, 180); self.buffer_minutes.setSuffix(" 分钟")
        self.break_start = QTimeEdit(); self.break_start.setDisplayFormat("HH:mm")
        self.break_end = QTimeEdit(); self.break_end.setDisplayFormat("HH:mm")
        self.fixed_checkin = QTimeEdit(); self.fixed_checkin.setDisplayFormat("HH:mm")
        self.fixed_clockout = QTimeEdit(); self.fixed_clockout.setDisplayFormat("HH:mm")
        self.max_attempts = QSpinBox(); self.max_attempts.setRange(1, 5); self.max_attempts.setSuffix(" 次")
        self.retry_delay = QSpinBox(); self.retry_delay.setRange(1, 60); self.retry_delay.setSuffix(" 分钟")
        widgets = (
            ("下班时间算法", self.calc_mode),
            ("有效工作时长", self.work_minutes),
            ("安全缓冲", self.buffer_minutes),
            ("休息开始", self.break_start),
            ("休息结束", self.break_end),
            ("固定上班时间", self.fixed_checkin),
            ("固定下班时间", self.fixed_clockout),
            ("最多自动尝试", self.max_attempts),
            ("失败重试间隔", self.retry_delay),
        )
        for index, (label, widget) in enumerate(widgets):
            row, column = divmod(index, 2)
            cell = QVBoxLayout()
            cell.addWidget(QLabel(label, objectName="metricLabel"))
            cell.addWidget(widget)
            form.addLayout(cell, row, column)
        layout.addLayout(form)
        layout.addSpacing(20)
        self.auto_open_check = QCheckBox("检查时自动启动飞书并打开假勤")
        self.startup_check = QCheckBox("登录 Windows 后自动在后台运行")
        layout.addWidget(self.auto_open_check)
        layout.addSpacing(8)
        layout.addWidget(self.startup_check)
        layout.addSpacing(20)
        save = QPushButton("保存设置", objectName="primary")
        save.setFixedWidth(110)
        save.clicked.connect(self._save_settings)
        layout.addWidget(save)
        layout.addStretch(1)
        self.load_settings()
        return page

    def _diagnostics_page(self) -> QWidget:
        page, layout = self._page_shell("连接诊断", "检查飞书窗口、假勤页面和页面绑定是否可用。")
        self.diagnostics_result = QTextEdit()
        self.diagnostics_result.setReadOnly(True)
        self.diagnostics_result.setPlainText("尚未运行诊断")
        self.diagnostics_result.setMaximumHeight(220)
        layout.addWidget(self.diagnostics_result)
        layout.addSpacing(14)
        buttons = QHBoxLayout()
        self.diagnose_button = QPushButton("运行诊断", objectName="primary")
        self.diagnose_button.clicked.connect(self.controller.diagnose)
        self.open_page_button = QPushButton("打开飞书假勤", objectName="secondary")
        self.open_page_button.clicked.connect(self.controller.open_attendance)
        self.bind_page_button = QPushButton("重新绑定页面", objectName="secondary")
        self.bind_page_button.clicked.connect(self.controller.calibrate)
        buttons.addWidget(self.diagnose_button)
        buttons.addWidget(self.open_page_button)
        buttons.addWidget(self.bind_page_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addSpacing(18)
        notice = QLabel("页面绑定通常只需一次。飞书升级导致结构变化时，程序会停止点击并要求重新绑定。", objectName="warning")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        layout.addStretch(1)
        return page

    def show_connection_result(self, message: str, status: str = "neutral") -> None:
        colors = {
            "neutral": ("#ffffff", "#1d1d1f", "#d2d2d7"),
            "running": ("#eef6ff", "#075da8", "#b9d9f7"),
            "success": ("#eef9f2", "#176b3a", "#b8dfc7"),
            "error": ("#fff1f2", "#a3182a", "#efbdc4"),
        }
        background, foreground, border = colors.get(status, colors["neutral"])
        self.diagnostics_result.setStyleSheet(
            f"background: {background}; color: {foreground}; "
            f"border: 1px solid {border}; border-radius: 8px; padding: 11px;"
        )
        self.diagnostics_result.setPlainText(message)

    def set_connection_busy(self, busy: bool, task_name: str = "") -> None:
        self.diagnose_button.setEnabled(not busy)
        self.open_page_button.setEnabled(not busy)
        self.bind_page_button.setEnabled(not busy)
        self.bind_page_button.setText(
            "正在绑定..." if busy and task_name == "绑定假勤页面" else "重新绑定页面"
        )

    def _logs_page(self) -> QWidget:
        page, layout = self._page_shell("运行记录", "这里只显示本次启动的关键事件，完整日志保存在本机。")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)
        layout.addSpacing(12)
        open_logs = QPushButton("打开日志文件夹", objectName="secondary")
        open_logs.setFixedWidth(140)
        open_logs.clicked.connect(lambda: open_folder(self.controller.paths.log_dir))
        layout.addWidget(open_logs)
        return page

    def _show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)

    def load_settings(self) -> None:
        config = self.controller.config
        self.calc_mode.setCurrentIndex(0 if config.calculation_mode == "dynamic" else 1)
        self.work_minutes.setValue(config.work_duration_minutes)
        self.buffer_minutes.setValue(config.buffer_minutes)
        self.break_start.setTime(QTime.fromString(config.break_start_time, "HH:mm"))
        self.break_end.setTime(QTime.fromString(config.break_end_time, "HH:mm"))
        self.fixed_checkin.setTime(QTime.fromString(config.fixed_checkin_time, "HH:mm"))
        self.fixed_clockout.setTime(QTime.fromString(config.fixed_clockout_time, "HH:mm"))
        self.max_attempts.setValue(config.max_click_attempts)
        self.retry_delay.setValue(config.retry_delay_minutes)
        self.auto_open_check.setChecked(config.auto_open_workbench)
        self.startup_check.setChecked(config.start_with_windows)

    def _save_settings(self) -> None:
        self.controller.save_settings(
            calculation_mode=str(self.calc_mode.currentData()),
            work_duration_minutes=self.work_minutes.value(),
            buffer_minutes=self.buffer_minutes.value(),
            break_start_time=self.break_start.time().toString("HH:mm"),
            break_end_time=self.break_end.time().toString("HH:mm"),
            fixed_checkin_time=self.fixed_checkin.time().toString("HH:mm"),
            fixed_clockout_time=self.fixed_clockout.time().toString("HH:mm"),
            max_click_attempts=self.max_attempts.value(),
            retry_delay_minutes=self.retry_delay.value(),
            auto_open_workbench=self.auto_open_check.isChecked(),
            start_with_windows=self.startup_check.isChecked(),
        )

    def update_status(self, result: CheckResult) -> None:
        self.status_title.setText(STATUS_TEXT.get(result.status, result.status))
        self.status_message.setText(result.message)
        if result.check_in_time:
            text = result.check_in_time.strftime("%H:%M")
            self.checkin_value.setText(text)
            self.calc_values[0].setText(text)
        if result.eligible_time:
            self.eligible_value.setText(result.eligible_time.strftime("%H:%M"))
        self.reset_action.setEnabled(result.status not in {"success", "already_clocked_out"})

    def update_monitor(self, enabled: bool) -> None:
        self.monitor_switch.blockSignals(True)
        self.monitor_switch.setChecked(enabled)
        self.monitor_switch.blockSignals(False)
        self.monitor_text.setText("已开启" if enabled else "已暂停")
        self.status_caption.setText("监控中" if enabled else "监控已暂停")

    def append_log(self, message: str) -> None:
        self.log_view.append(f"{datetime.now():%H:%M:%S}  {message}")

    def show_from_tray(self) -> None:
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event: QShowEvent) -> None:
        _apply_light_titlebar(self)
        super().showEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._really_close:
            event.accept()
            return
        event.ignore()
        self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        self.hide()
        if not self._tray_hint_shown:
            self.controller.tray.showMessage(APP_TITLE, "程序仍在后台监控，可从系统托盘重新打开。", QSystemTrayIcon.MessageIcon.Information, 3500)
            self._tray_hint_shown = True


class AppController(QObject):
    def __init__(self, app: QApplication, paths: RuntimePaths, background: bool) -> None:
        super().__init__()
        self.app = app
        self.paths = paths
        prepare_runtime_paths(paths)
        self.config = load_config(paths.config_path)
        save_config(paths.config_path, self.config)
        self.store = JsonStateStore(paths.state_path)
        self.adapter = FeishuUiaAdapter(
            auto_open_workbench=self.config.auto_open_workbench,
            trusted_container_fingerprint=self.config.trusted_container_fingerprint,
        )
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(1)
        self.busy = False
        self._active_worker: Worker | None = None
        self._active_task_name = ""
        self._active_task_timed_out = False
        self._worker_generation = 0
        self._worker_timeout_ms = 30000
        self._pending_worker: tuple[
            Callable[[], object],
            Callable[[object], None],
            Callable[[str], None],
            str,
        ] | None = None
        self._shutting_down = False
        self._quit_grace_ms = 1000
        self._hard_exit: Callable[[int], object] = os._exit
        self.next_check: datetime | None = None
        self.last_tick = datetime.now()
        self.logger = self._setup_logger(paths.log_dir)
        self.window = MainWindow(self)
        self.tray = QSystemTrayIcon(_tray_icon(), self.window)
        self._build_tray()
        self.tray.show()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._sync_startup_setting()
        self.window.update_monitor(self.config.monitor_enabled)
        self._schedule_from(datetime.now())
        self.log("正式版程序已启动%s", "，正在后台运行" if background else "")
        self.refresh_from_state()
        if self.config.trusted_container_fingerprint:
            QTimer.singleShot(1500, self._refresh_today_preview)
        if not background:
            self.window.show()

    def _build_tray(self) -> None:
        menu = QMenu()
        self.tray_status = menu.addAction("状态：准备就绪")
        self.tray_status.setEnabled(False)
        menu.addSeparator()
        menu.addAction("显示主界面", self.window.show_from_tray)
        self.tray_monitor = menu.addAction("暂停监控", lambda: self.set_monitor_enabled(not self.config.monitor_enabled))
        menu.addAction("立即检查", self.run_check_now)
        menu.addSeparator()
        menu.addAction("退出", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick}:
            self.window.show_from_tray()

    def _engine(self) -> ClockoutEngine:
        config = EngineConfig(
            mode=self.config.mode,
            work_duration_minutes=self.config.work_duration_minutes,
            safety_buffer_minutes=self.config.buffer_minutes,
            check_start_time=self.config.start_time,
            check_end_time=self.config.end_time,
            weekdays=frozenset(self.config.weekdays),
            calculation_mode=self.config.calculation_mode,
            break_start_time=self.config.break_start,
            break_end_time=self.config.break_end,
            fixed_checkin_time=self.config.fixed_checkin,
            fixed_clockout_time=self.config.fixed_clockout,
            max_click_attempts=self.config.max_click_attempts,
            retry_delay_minutes=self.config.retry_delay_minutes,
        )
        return ClockoutEngine(self.adapter, self.store, config, now_provider=datetime.now)

    def _eligible_time(self, day: date, check_in_time) -> datetime:
        return calculate_eligible_time(
            day,
            check_in_time,
            self.config.work_duration_minutes,
            self.config.buffer_minutes,
            calculation_mode=self.config.calculation_mode,
            break_start_time=self.config.break_start,
            break_end_time=self.config.break_end,
            fixed_checkin_time=self.config.fixed_checkin,
            fixed_clockout_time=self.config.fixed_clockout,
        )

    def _refresh_today_preview(self) -> None:
        if self.busy or not self.config.trusted_container_fingerprint:
            return
        self._run_task(
            lambda: self.adapter.snapshot(date.today()),
            self._today_preview_finished,
            "正在读取今日上班时间",
            error_callback=self._today_preview_failed,
            task_name="读取今日上班时间",
        )

    def _today_preview_finished(self, value: object) -> None:
        snapshot = value if isinstance(value, AttendanceSnapshot) else None
        if (
            snapshot is None
            or snapshot.page_date != date.today()
            or snapshot.check_in_time is None
        ):
            reason = snapshot.blocking_reason if snapshot else "页面返回了无效结果"
            self._today_preview_failed(reason or "未识别到今日上班时间")
            return
        eligible_time = self._eligible_time(date.today(), snapshot.check_in_time)
        message = (
            f"已读取今日上班时间 {snapshot.check_in_time:%H:%M}，"
            f"预计最早下班 {eligible_time:%H:%M}"
        )
        self._show_result(
            CheckResult(
                "already_clocked_out" if snapshot.already_clocked_out else "waiting",
                message,
                snapshot.check_in_time,
                eligible_time,
            )
        )

    def _today_preview_failed(self, message: str) -> None:
        reason = message.split(": ", 1)[-1].strip() or "未知错误"
        self.window.status_message.setText(f"今日上班时间读取失败：{reason}")
        self.log("今日上班时间读取失败：%s", reason)

    def set_monitor_enabled(self, enabled: bool) -> None:
        self.config = replace(self.config, monitor_enabled=enabled).validate()
        save_config(self.paths.config_path, self.config)
        self.window.update_monitor(enabled)
        self.tray_monitor.setText("暂停监控" if enabled else "恢复监控")
        if enabled:
            self._schedule_from(datetime.now(), immediate=True)
            self.log("后台监控已开启")
        else:
            self.next_check = None
            self.window.next_value.setText("已暂停")
            self.log("后台监控已暂停")

    def _schedule_from(self, now: datetime, *, immediate: bool = False) -> None:
        if not self.config.monitor_enabled:
            self.next_check = None
            return
        if self.config.start_time <= now.time() <= self.config.end_time:
            self.next_check = now if immediate else now + timedelta(seconds=2)
        elif now.time() < self.config.start_time:
            self.next_check = datetime.combine(now.date(), self.config.start_time)
        else:
            self.next_check = self._next_workday_start(now.date())
        self._update_next_label()

    def _next_workday_start(self, day: date) -> datetime:
        cursor = day + timedelta(days=1)
        while cursor.weekday() not in self.config.weekdays:
            cursor += timedelta(days=1)
        return datetime.combine(cursor, self.config.start_time)

    def _tick(self) -> None:
        now = datetime.now()
        if now < self.last_tick:
            self.set_monitor_enabled(False)
            self._show_result(CheckResult("blocked", "检测到系统时间回拨，监控已暂停"))
        elif now - self.last_tick > timedelta(seconds=90) and self.config.monitor_enabled:
            self._schedule_from(now, immediate=True)
            self.log("系统从睡眠或长时间停顿中恢复，已重新安排检查")
        self.last_tick = now
        if (
            self.config.monitor_enabled
            and not self.busy
            and self._active_worker is None
            and self.next_check
            and now >= self.next_check
        ):
            self.run_check_now()
        self._update_next_label()

    def run_check_now(self) -> None:
        if self.busy:
            self._show_busy_feedback()
            return
        now = datetime.now()
        if not (self.config.start_time <= now.time() <= self.config.end_time):
            self._schedule_from(now)
            self._show_result(CheckResult("skipped", "当前不在检查时段，已安排下一次检查"))
            return
        self.busy = True
        self._show_result(CheckResult("checking", "正在读取飞书假勤页面"))
        self.log("开始检查")
        self._start_worker(
            lambda: self._engine().check(datetime.now()),
            self._check_finished,
            self._task_failed,
            "检查飞书假勤页面",
        )

    def _check_finished(self, value: object) -> None:
        self.busy = False
        result = value if isinstance(value, CheckResult) else CheckResult("blocked", "检查返回了无效结果")
        self._show_result(result)
        now = datetime.now()
        if result.status == "waiting" and result.eligible_time:
            self.next_check = result.eligible_time
        elif result.status == "retry_waiting" and result.next_retry_time:
            self.next_check = result.next_retry_time
        elif result.status in {"success", "already_clocked_out", "retry_exhausted"}:
            self.next_check = self._next_workday_start(now.date())
        elif result.status == "unknown":
            self.next_check = now + timedelta(minutes=self.config.check_interval_minutes)
        elif self.config.monitor_enabled:
            self.next_check = now + timedelta(minutes=self.config.check_interval_minutes)
        self._update_next_label()
        if result.status in {"success", "unknown", "retry_exhausted"}:
            icon = QSystemTrayIcon.MessageIcon.Information if result.status == "success" else QSystemTrayIcon.MessageIcon.Warning
            self.tray.showMessage(APP_TITLE, result.message, icon, 5000)

    def _show_result(self, result: CheckResult) -> None:
        self.window.update_status(result)
        self.tray_status.setText(f"状态：{STATUS_TEXT.get(result.status, result.status)}")
        color = "#1c7649" if result.status in {"success", "waiting", "already_clocked_out"} else "#bd7418" if result.status in {"retry_waiting", "unknown", "retry_exhausted"} else "#667069"
        self.tray.setIcon(_tray_icon(color))
        self.log("%s：%s", STATUS_TEXT.get(result.status, result.status), result.message)

    def _update_next_label(self) -> None:
        if not self.config.monitor_enabled:
            self.window.next_value.setText("已暂停")
        elif self.next_check is None:
            self.window.next_value.setText("--:--")
        elif self.next_check.date() == date.today():
            self.window.next_value.setText(self.next_check.strftime("%H:%M"))
        else:
            self.window.next_value.setText(self.next_check.strftime("%m-%d %H:%M"))

    def refresh_from_state(self) -> None:
        try:
            state = self.store.load(date.today())
        except Exception:
            self._show_result(CheckResult("blocked", "今日状态文件无法读取"))
            return
        if state is None:
            self._show_result(CheckResult("idle", "后台监控已准备，等待进入检查时段"))
            return
        if state.clock_out_success:
            self._show_result(CheckResult("success", "今日下班打卡已经确认成功"))
        elif state.outcome == "aborted_before_click":
            retry_at = datetime.fromisoformat(state.next_retry_at) if state.next_retry_at else None
            self._show_result(CheckResult("retry_waiting", "点击前失败，等待有限重试", next_retry_time=retry_at))
        elif state.clock_out_attempted:
            self._show_result(CheckResult("unknown", "此前点击结果不明确，只会继续核验页面"))

    def diagnose(self) -> None:
        if self.busy:
            self._show_busy_feedback(connection_page=True)
            return
        self.window.show_connection_result(
            "正在检查飞书窗口、今天的假勤页面和当前绑定...", "running"
        )
        self._run_task(
            self.adapter.diagnostics,
            self._diagnostics_finished,
            "正在诊断飞书页面",
            error_callback=self._diagnostics_failed,
            task_name="诊断飞书页面",
        )

    def _diagnostics_finished(self, value: object) -> None:
        data = value if isinstance(value, dict) else {}
        message = (
            f"飞书窗口：{'已找到' if data.get('window_found') else '未找到'}\n"
            f"页面控件：{data.get('element_count', 0)}\n"
            f"今天假勤页：{'已确认' if data.get('attendance_page') else '未确认'}\n"
            f"下班按钮：{data.get('checkout_button_count', 0)}\n"
            f"页面绑定：{'有效' if data.get('bound') else '无效或尚未绑定'}"
        )
        self.window.show_connection_result(message, "success" if data.get("bound") else "error")
        self.window.binding_label.setText("页面绑定：" + ("有效" if data.get("bound") else "待处理"))
        self.log("飞书页面诊断完成")

    def _diagnostics_failed(self, message: str) -> None:
        reason = message.split(": ", 1)[-1].strip() or "未知错误"
        self.window.show_connection_result(f"诊断失败：{reason}", "error")
        self.log("飞书页面诊断失败：%s", reason)

    def open_attendance(self) -> None:
        if self.busy:
            self._show_busy_feedback(connection_page=True)
            return
        self.window.show_connection_result("正在打开飞书假勤页面...", "running")
        self._run_task(
            lambda: self.adapter.open_attendance_page(date.today()),
            self._open_finished,
            "正在打开飞书假勤",
            error_callback=self._open_failed,
            task_name="打开飞书假勤",
        )

    def _open_finished(self, value: object) -> None:
        message = "飞书假勤页已打开" if value is True else "未能打开飞书假勤页，请检查登录状态"
        self.window.status_message.setText(message)
        self.window.show_connection_result(
            message, "success" if value is True else "error"
        )
        self.log(message)

    def _open_failed(self, message: str) -> None:
        reason = message.split(": ", 1)[-1].strip() or "未知错误"
        self.window.show_connection_result(f"打开飞书假勤失败：{reason}", "error")
        self.log("打开飞书假勤失败：%s", reason)

    def calibrate(self) -> None:
        if self.busy:
            self._show_busy_feedback(connection_page=True)
            return
        self.window.show_connection_result(
            "正在读取飞书当前页面并重新绑定，不会点击打卡...", "running"
        )
        started = self._run_task(
            lambda: self.adapter.calibrate_current_page(date.today()),
            self._calibrate_finished,
            "正在绑定假勤页面",
            error_callback=self._calibrate_failed,
            task_name="绑定假勤页面",
        )
        if started:
            self.log("开始重新绑定飞书假勤页面")

    def _calibrate_finished(self, value: object) -> None:
        fingerprint = value if isinstance(value, str) else ""
        if not fingerprint:
            self._calibrate_failed("绑定过程没有返回有效的页面标识")
            return
        self.config = replace(self.config, trusted_container_fingerprint=fingerprint).validate()
        save_config(self.paths.config_path, self.config)
        self.adapter.trusted_container_fingerprint = fingerprint
        self.window.binding_label.setText("页面绑定：有效")
        self.window.status_message.setText("飞书假勤页面绑定成功")
        self.window.show_connection_result(
            "页面绑定成功。程序已确认当前为今天的飞书假勤页面。",
            "success",
        )
        self.log("飞书假勤页面绑定成功")
        self.tray.showMessage(
            APP_TITLE,
            "飞书假勤页面绑定成功",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )
        QTimer.singleShot(0, self._refresh_today_preview)

    def _calibrate_failed(self, message: str) -> None:
        reason = message.split(": ", 1)[-1].strip() or "未知错误"
        detail = f"页面绑定失败：{reason}"
        self.window.status_message.setText(detail)
        self.window.binding_label.setText("页面绑定：失败")
        self.window.show_connection_result(
            f"{detail}\n\n请确认飞书已登录，并停留在今天的假勤页面后重试。",
            "error",
        )
        self.log(detail)
        QMessageBox.warning(
            self.window,
            "页面绑定失败",
            f"{reason}\n\n请确认飞书已登录，并停留在今天的假勤页面后重试。",
        )

    def request_reset(self) -> None:
        if self.busy:
            return
        try:
            state = self.store.load(date.today())
        except Exception:
            QMessageBox.warning(self.window, "无法重置", "今日状态文件无法读取。")
            return
        if state is None:
            QMessageBox.information(self.window, "无需重置", "今天还没有失败记录。")
            return
        if state.clock_out_success:
            QMessageBox.warning(self.window, "不能重置", "飞书已确认今天打卡成功，不能再次开放点击。")
            return
        self._run_task(lambda: self.adapter.snapshot(date.today()), self._reset_snapshot_ready, "正在核对飞书今日状态")

    def _reset_snapshot_ready(self, value: object) -> None:
        snapshot = value if isinstance(value, AttendanceSnapshot) else None
        if snapshot is None or snapshot.page_date != date.today():
            QMessageBox.warning(self.window, "无法重置", "未能确认飞书当前显示的是今天的假勤页面。")
            return
        if snapshot.already_clocked_out:
            QMessageBox.warning(self.window, "不能重置", "飞书页面已经存在今天的下班打卡记录。")
            return
        if snapshot.blocking_reason or snapshot.button_count != 1 or not snapshot.button_enabled:
            QMessageBox.warning(self.window, "无法重置", "尚未确认唯一可用的下班打卡按钮。")
            return
        answer = QMessageBox.warning(
            self.window,
            "重置今日失败状态",
            "请先确认飞书今天确实没有下班打卡记录。重置后程序可能再次真实点击。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        reason, accepted = QInputDialog.getText(self.window, "记录重置原因", "原因：", text="已人工核对飞书今日没有下班打卡记录")
        if not accepted:
            return
        try:
            self.store.reset_for_retry(date.today(), reset_at=datetime.now(), reason=reason)
        except (StateStoreError, ValueError) as exc:
            QMessageBox.warning(self.window, "重置失败", str(exc))
            return
        self.log("用户已安全重置今日失败状态：%s", reason)
        self._schedule_from(datetime.now(), immediate=True)
        self._show_result(CheckResult("idle", "今日失败状态已重置，等待重新检查"))

    def save_settings(self, **changes: object) -> None:
        try:
            updated = replace(self.config, **changes).validate()
            set_start_with_windows(updated.start_with_windows)
            self.config = updated
            save_config(self.paths.config_path, updated)
        except Exception as exc:
            QMessageBox.warning(self.window, "设置未保存", str(exc))
            self.window.load_settings()
            return
        self.adapter.auto_open_workbench = updated.auto_open_workbench
        self._schedule_from(datetime.now(), immediate=True)
        self.window.retry_note.setText(
            f"自动重试最多 {updated.max_click_attempts} 次，仅限点击前失败；点击后结果不明确不会自动重复点击。"
        )
        self.log("时间与运行设置已保存")
        QMessageBox.information(self.window, "设置已保存", "新的设置已经生效。")

    def _sync_startup_setting(self) -> None:
        try:
            if self.config.start_with_windows != is_start_with_windows_enabled():
                set_start_with_windows(self.config.start_with_windows)
        except OSError:
            self.log("开机自启动设置同步失败")

    def _run_task(
        self,
        function: Callable[[], object],
        callback: Callable[[object], None],
        message: str,
        *,
        error_callback: Callable[[str], None] | None = None,
        task_name: str | None = None,
    ) -> bool:
        if self.busy:
            self._show_busy_feedback()
            return False
        self.busy = True
        self.window.status_message.setText(message)
        self._start_worker(
            function,
            callback,
            lambda error: self._task_failed(error, error_callback),
            task_name or message,
        )
        return True

    def _start_worker(
        self,
        function: Callable[[], object],
        callback: Callable[[object], None],
        error_callback: Callable[[str], None],
        task_name: str,
    ) -> None:
        if self._shutting_down:
            self.busy = False
            return
        if self._active_worker is not None:
            if self._pending_worker is None:
                self._pending_worker = (
                    function,
                    callback,
                    error_callback,
                    task_name,
                )
                self.busy = True
                message = (
                    f"{self._active_task_name or '上一次调用'}仍在收尾，"
                    f"{task_name}已排队"
                )
                self.window.status_message.setText(message)
                self.window.set_connection_busy(True, task_name)
                self.log(message)
            else:
                self._show_busy_feedback()
            return
        self._launch_worker(function, callback, error_callback, task_name)

    def _launch_worker(
        self,
        function: Callable[[], object],
        callback: Callable[[object], None],
        error_callback: Callable[[str], None],
        task_name: str,
    ) -> None:
        worker = Worker(function, task_name)
        self._worker_generation += 1
        token = self._worker_generation
        self.busy = True
        self._active_worker = worker
        self._active_task_name = task_name
        self._active_task_timed_out = False
        self.window.set_connection_busy(True, task_name)
        worker.signals.finished.connect(
            lambda value, active=worker, generation=token: self._worker_finished(
                active, callback, value, generation
            )
        )
        worker.signals.failed.connect(
            lambda error, active=worker, generation=token: self._worker_failed(
                active, error_callback, error, generation
            )
        )
        self.thread_pool.start(worker)
        QTimer.singleShot(
            self._worker_timeout_ms,
            lambda active=worker, generation=token: self._worker_timed_out(
                active, generation
            ),
        )

    def _worker_finished(
        self,
        worker: Worker,
        callback: Callable[[object], None],
        value: object,
        token: int | None = None,
    ) -> None:
        if worker is not self._active_worker:
            return
        if getattr(self, "_shutting_down", False):
            self._complete_active_worker(worker)
            return
        should_deliver = (
            not self._active_task_timed_out
            and (token is None or token == getattr(self, "_worker_generation", token))
        )
        self._complete_active_worker(worker)
        if should_deliver:
            callback(value)

    def _worker_failed(
        self,
        worker: Worker,
        callback: Callable[[str], None],
        message: str,
        token: int | None = None,
    ) -> None:
        if worker is not self._active_worker:
            return
        if getattr(self, "_shutting_down", False):
            self._complete_active_worker(worker)
            return
        should_deliver = (
            not self._active_task_timed_out
            and (token is None or token == getattr(self, "_worker_generation", token))
        )
        self._complete_active_worker(worker)
        if should_deliver:
            callback(message)

    def _release_worker(self) -> None:
        worker = self._active_worker
        if worker is None:
            return
        self._complete_active_worker(worker)

    def _complete_active_worker(self, worker: Worker) -> None:
        if worker is not self._active_worker:
            return
        timed_out = self._active_task_timed_out
        completed_name = self._active_task_name
        pending = getattr(self, "_pending_worker", None)
        self._pending_worker = None
        self.busy = False
        self._active_worker = None
        self._active_task_name = ""
        self._active_task_timed_out = False
        if getattr(self, "_shutting_down", False):
            return
        if timed_out:
            self.log("后台任务已结束收尾：%s", completed_name)
        if pending is not None:
            self._launch_worker(*pending)
        else:
            self.window.set_connection_busy(False)

    def _worker_timed_out(self, worker: Worker, token: int | None = None) -> None:
        if (
            self._shutting_down
            or worker is not self._active_worker
            or self._active_task_timed_out
            or (token is not None and token != self._worker_generation)
        ):
            return
        self._active_task_timed_out = True
        self._worker_generation += 1
        self.busy = False
        self.window.set_connection_busy(False)
        seconds = max(1, self._worker_timeout_ms // 1000)
        message = (
            f"{self._active_task_name}超过 {seconds} 秒仍未完成；"
            "界面已恢复，上一次调用仍在收尾"
        )
        self.window.status_message.setText(message)
        self.window.show_connection_result(message, "error")
        self.log("后台任务超时：%s", self._active_task_name)
        self.tray.showMessage(
            APP_TITLE,
            message,
            QSystemTrayIcon.MessageIcon.Warning,
            5000,
        )

    def _show_busy_feedback(self, *, connection_page: bool = False) -> None:
        task_name = self._active_task_name or "上一项操作"
        message = f"{task_name}仍在进行，请稍候"
        self.window.status_message.setText(message)
        if connection_page:
            self.window.show_connection_result(message, "running")
        self.log("操作未重复执行：%s", message)

    def _task_finished(self, callback: Callable[[object], None], value: object) -> None:
        callback(value)

    def _task_failed(
        self,
        message: str,
        error_callback: Callable[[str], None] | None = None,
    ) -> None:
        if error_callback is not None:
            error_callback(message)
            return
        self.window.status_message.setText("操作失败，详细信息已写入日志")
        self.log("后台操作失败：%s", message)

    def log(self, message: str, *args: object) -> None:
        rendered = message % args if args else message
        self.logger.info(rendered)
        self.window.append_log(rendered)

    def quit(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._worker_generation += 1
        self._pending_worker = None
        self.busy = False
        self.timer.stop()
        self.window._really_close = True
        self.tray.hide()
        self.window.close()
        if (
            self._active_worker is not None
            and not self.thread_pool.waitForDone(self._quit_grace_ms)
        ):
            try:
                self.logger.warning(
                    "后台任务未在退出宽限期内结束，程序将立即退出"
                )
            finally:
                self._close_log_handlers()
                self._hard_exit(0)
            return
        self.app.quit()

    def _close_log_handlers(self) -> None:
        for handler in list(self.logger.handlers):
            try:
                handler.flush()
            except Exception:
                pass
            try:
                handler.close()
            except Exception:
                pass
            finally:
                self.logger.removeHandler(handler)

    @staticmethod
    def _setup_logger(log_dir: Path) -> logging.Logger:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("clockout")
        logger.setLevel(logging.INFO)
        target = (log_dir / f"{datetime.now():%Y-%m-%d}.log").resolve()
        if not any(isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == target for handler in logger.handlers):
            handler = logging.FileHandler(target, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        return logger


def _server_name() -> str:
    identity = hashlib.sha256(getpass.getuser().encode("utf-8")).hexdigest()[:12]
    return f"FeishuClockoutAssistant-{identity}"


def _notify_existing_instance(name: str) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(1000):
        return False
    socket.write(b"show")
    socket.flush()
    if socket.bytesToWrite() > 0:
        socket.waitForBytesWritten(1000)
    socket.waitForReadyRead(500)
    socket.disconnectFromServer()
    return True


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)
    _apply_light_palette(app)
    app.setStyleSheet(APP_QSS)
    name = _server_name()
    if _notify_existing_instance(name):
        return 0
    QLocalServer.removeServer(name)
    server = QLocalServer(app)
    if not server.listen(name):
        return 1
    background = any(argument in {"--background", "--startup"} for argument in sys.argv[1:])
    controller = AppController(app, runtime_paths(), background)

    def activate_from_socket() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return

        def handle_message() -> None:
            if bytes(connection.readAll()) == b"show":
                controller.window.show_from_tray()
                controller.log("已从第二次启动请求唤回主界面")
                connection.write(b"ok")
                connection.flush()
                connection.waitForBytesWritten(500)
            connection.disconnectFromServer()

        if connection.bytesAvailable():
            handle_message()
        else:
            connection.readyRead.connect(handle_message)

    server.newConnection.connect(activate_from_socket)
    app._clockout_server = server  # type: ignore[attr-defined]
    app._clockout_controller = controller  # type: ignore[attr-defined]
    return app.exec()
