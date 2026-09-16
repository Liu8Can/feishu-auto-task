from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace

from clockout.config import AppConfig
from clockout.core import AttendanceSnapshot, CheckResult
from clockout.qt_app import AppController


class TextSink:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, value: str) -> None:
        self.text = value

    def setPlainText(self, value: str) -> None:
        self.text = value


class WindowSink:
    def __init__(self) -> None:
        self.status_message = TextSink()
        self.binding_label = TextSink()
        self.diagnostics_result = TextSink()
        self.connection_status = "neutral"
        self.connection_busy = False

    def show_connection_result(self, message: str, status: str = "neutral") -> None:
        self.diagnostics_result.setPlainText(message)
        self.connection_status = status

    def set_connection_busy(self, busy: bool, task_name: str = "") -> None:
        self.connection_busy = busy


def test_next_workday_skips_weekend() -> None:
    controller = AppController.__new__(AppController)
    controller.config = AppConfig(weekdays=(0, 1, 2, 3, 4))

    result = controller._next_workday_start(date(2026, 9, 18))

    assert result == datetime(2026, 9, 21, 15, 0)


def test_monitor_and_retry_defaults_are_enabled_for_old_config() -> None:
    config = AppConfig()

    assert config.mode == "automatic"
    assert config.monitor_enabled
    assert config.start_with_windows
    assert config.max_click_attempts == 3
    assert config.retry_delay_minutes == 5


def test_calibrate_starts_binding_without_confirmation() -> None:
    controller = AppController.__new__(AppController)
    controller.busy = False
    controller.window = WindowSink()
    controller.adapter = SimpleNamespace(
        calibrate_current_page=lambda day: f"bound-{day.isoformat()}"
    )
    logged: list[str] = []
    controller.log = logged.append
    task: dict[str, object] = {}

    def run_task(
        function, callback, message, *, error_callback=None, task_name=None
    ):
        task.update(
            function=function,
            callback=callback,
            message=message,
            error_callback=error_callback,
            task_name=task_name,
        )
        return True

    controller._run_task = run_task

    controller.calibrate()

    assert controller.window.diagnostics_result.text.startswith("正在读取飞书当前页面")
    assert controller.window.connection_status == "running"
    assert logged == ["开始重新绑定飞书假勤页面"]
    assert task["message"] == "正在绑定假勤页面"
    assert callable(task["error_callback"])
    assert task["function"]() == f"bound-{date.today().isoformat()}"


def test_calibration_failure_is_visible_to_user(monkeypatch) -> None:
    controller = AppController.__new__(AppController)
    controller.window = WindowSink()
    logged: list[str] = []
    controller.log = logged.append
    warning: dict[str, str] = {}

    def show_warning(parent, title: str, message: str) -> None:
        warning.update(title=title, message=message)

    monkeypatch.setattr("clockout.qt_app.QMessageBox.warning", show_warning)

    controller._calibrate_failed("RuntimeError: 当前页面不是可唯一确认的今日考勤页")

    assert controller.window.status_message.text == "页面绑定失败：当前页面不是可唯一确认的今日考勤页"
    assert controller.window.binding_label.text == "页面绑定：失败"
    assert "请确认飞书已登录" in controller.window.diagnostics_result.text
    assert controller.window.connection_status == "error"
    assert logged == [controller.window.status_message.text]
    assert warning["title"] == "页面绑定失败"
    assert warning["message"].startswith("当前页面不是可唯一确认的今日考勤页")


def test_worker_failure_releases_active_worker_before_callback() -> None:
    controller = AppController.__new__(AppController)
    controller.busy = True
    controller.window = WindowSink()
    controller._active_worker = object()
    controller._active_task_name = "绑定假勤页面"
    controller._active_task_timed_out = False
    errors: list[str] = []

    controller._worker_failed(
        controller._active_worker,
        errors.append,
        "RuntimeError: failed",
    )

    assert not controller.busy
    assert controller._active_worker is None
    assert not controller.window.connection_busy
    assert errors == ["RuntimeError: failed"]


def test_today_preview_uses_real_check_in_time_for_display() -> None:
    controller = AppController.__new__(AppController)
    controller.config = AppConfig(
        work_duration_minutes=480,
        buffer_minutes=5,
        break_start_time="12:00",
        break_end_time="14:00",
    )
    shown: list[CheckResult] = []
    controller._show_result = shown.append
    snapshot = AttendanceSnapshot(
        page_date=date.today(),
        check_in_time=time(8, 31),
        already_clocked_out=False,
        button_count=1,
        button_enabled=True,
        blocking_reason=None,
        signature="snapshot",
        container_id="attendance",
        button_id="clockout",
    )

    controller._today_preview_finished(snapshot)

    assert len(shown) == 1
    assert shown[0].check_in_time == time(8, 31)
    assert shown[0].eligible_time == datetime.combine(date.today(), time(18, 36))
    assert "08:31" in shown[0].message
    assert "18:36" in shown[0].message
