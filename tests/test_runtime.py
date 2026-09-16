from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time as wall_time
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

import clockout.qt_app as qt_app
from clockout.config import AppConfig
from clockout.core import AttendanceSnapshot, CheckResult
from clockout.qt_app import AppController


QT_APP = QApplication.instance() or QApplication([])


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


class TraySink:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.hidden = False

    def showMessage(self, title, message, icon, duration) -> None:
        self.messages.append(message)

    def hide(self) -> None:
        self.hidden = True


class TimerSink:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _worker_controller(timeout_ms: int = 30) -> AppController:
    controller = AppController.__new__(AppController)
    controller.busy = False
    controller.window = WindowSink()
    controller.tray = TraySink()
    controller.timer = TimerSink()
    controller.thread_pool = QThreadPool()
    controller.thread_pool.setMaxThreadCount(1)
    controller._active_worker = None
    controller._active_task_name = ""
    controller._active_task_timed_out = False
    controller._worker_generation = 0
    controller._worker_timeout_ms = timeout_ms
    controller._pending_worker = None
    controller._shutting_down = False
    controller._quit_grace_ms = 100
    controller.hard_exits = []
    controller._hard_exit = controller.hard_exits.append
    controller.logger = logging.getLogger(f"clockout-test-{id(controller)}")
    controller.logged = []

    def log(message: str, *args: object) -> None:
        controller.logged.append(message % args if args else message)

    controller.log = log
    return controller


def _wait_until(predicate, timeout: float = 1.5) -> None:
    deadline = wall_time.monotonic() + timeout
    while wall_time.monotonic() < deadline:
        QT_APP.processEvents()
        if predicate():
            return
        wall_time.sleep(0.005)
    QT_APP.processEvents()
    assert predicate()


def test_next_workday_skips_weekend() -> None:
    controller = AppController.__new__(AppController)
    controller.config = AppConfig(weekdays=(0, 1, 2, 3, 4))

    result = controller._next_workday_start(date(2026, 9, 18))

    assert result == datetime(2026, 9, 21, 15, 0)


@pytest.mark.parametrize(
    ("arguments", "background", "command"),
    [
        ([], False, b"show"),
        (["--background"], True, b"show"),
        (["--startup"], True, b"show"),
        (["--scheduled-wake"], True, b"wake"),
    ],
)
def test_launch_arguments_select_background_and_ipc_command(
    arguments: list[str], background: bool, command: bytes
) -> None:
    request = qt_app._parse_launch_request(arguments)

    assert request.background is background
    assert request.ipc_command == command


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(b"show", "show"), (b"wake", "wake"), (b"", None), (b"wake-now", None)],
)
def test_ipc_protocol_accepts_only_exact_known_commands(
    payload: bytes, expected: str | None
) -> None:
    assert qt_app._parse_ipc_command(payload) == expected


def test_notify_existing_instance_sends_selected_command(monkeypatch) -> None:
    class SocketSink:
        def __init__(self) -> None:
            self.written = b""

        def connectToServer(self, name: str) -> None:
            self.name = name

        def waitForConnected(self, timeout: int) -> bool:
            return True

        def write(self, payload: bytes) -> None:
            self.written = payload

        def flush(self) -> None:
            pass

        def bytesToWrite(self) -> int:
            return 0

        def waitForReadyRead(self, timeout: int) -> bool:
            return True

        def disconnectFromServer(self) -> None:
            pass

    socket = SocketSink()
    monkeypatch.setattr(qt_app, "QLocalSocket", lambda: socket)

    assert qt_app._notify_existing_instance("test-server", b"wake")
    assert socket.name == "test-server"
    assert socket.written == b"wake"

    with pytest.raises(ValueError, match="命令无效"):
        qt_app._notify_existing_instance("test-server", b"unknown")


def test_instance_commands_show_or_silently_reschedule() -> None:
    controller = AppController.__new__(AppController)
    shown: list[bool] = []
    scheduled: list[tuple[datetime, bool]] = []
    logged: list[str] = []
    controller.window = SimpleNamespace(show_from_tray=lambda: shown.append(True))
    controller.log = logged.append
    controller._schedule_from = (
        lambda now, *, immediate=False: scheduled.append((now, immediate))
    )
    controller._engine = lambda: pytest.fail("计划唤醒不得直接调用引擎")

    assert controller.handle_instance_command("wake")
    assert shown == []
    assert len(scheduled) == 1
    assert scheduled[0][1] is False

    assert controller.handle_instance_command("show")
    assert shown == [True]
    assert len(scheduled) == 1

    assert not controller.handle_instance_command("unknown")
    assert shown == [True]
    assert len(scheduled) == 1


def test_ipc_dispatch_rejects_unknown_payload_without_controller_action() -> None:
    commands: list[str] = []
    controller = SimpleNamespace(
        handle_instance_command=lambda command: commands.append(command) or True
    )

    assert qt_app._dispatch_ipc_command(controller, b"show") == b"ok"
    assert commands == ["show"]

    assert qt_app._dispatch_ipc_command(controller, b"unknown") == b"rejected"
    assert commands == ["show"]


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


@pytest.mark.parametrize("late_failure", [False, True])
def test_worker_timeout_ignores_late_result_and_runs_queued_task(
    late_failure: bool,
) -> None:
    controller = _worker_controller()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    late_results: list[object] = []
    late_errors: list[str] = []
    next_results: list[object] = []

    def slow_call() -> object:
        first_started.set()
        release_first.wait(1)
        if late_failure:
            raise RuntimeError("late failure")
        return "late success"

    controller._start_worker(
        slow_call,
        late_results.append,
        late_errors.append,
        "首次调用",
    )
    _wait_until(first_started.is_set)
    _wait_until(lambda: controller._active_task_timed_out)

    assert not controller.busy
    assert controller._active_worker is not None
    assert not controller.window.connection_busy
    assert "上一次调用仍在收尾" in controller.window.status_message.text

    controller._worker_timeout_ms = 500

    def next_call() -> object:
        second_started.set()
        return "next success"

    assert controller._run_task(next_call, next_results.append, "准备后续调用")
    assert controller._pending_worker is not None
    assert not second_started.is_set()
    assert "已排队" in controller.window.status_message.text

    release_first.set()
    _wait_until(second_started.is_set)
    _wait_until(lambda: next_results == ["next success"])

    assert late_results == []
    assert late_errors == []
    assert controller._active_worker is None
    assert controller._pending_worker is None
    assert not controller.busy
    assert not controller.window.connection_busy
    assert controller.thread_pool.waitForDone(1000)


def test_worker_callbacks_are_ignored_during_quit() -> None:
    controller = _worker_controller(timeout_ms=500)
    controller._quit_grace_ms = 500
    started = threading.Event()
    release = threading.Event()
    results: list[object] = []
    errors: list[str] = []
    app_quit: list[bool] = []
    window_closed: list[bool] = []
    controller.app = SimpleNamespace(quit=lambda: app_quit.append(True))
    controller.window.close = lambda: window_closed.append(True)

    def slow_call() -> object:
        started.set()
        release.wait(1)
        return "too late"

    controller._start_worker(slow_call, results.append, errors.append, "退出测试")
    _wait_until(started.is_set)
    release_timer = threading.Timer(0.02, release.set)
    release_timer.start()
    controller.quit()
    release_timer.join(1)
    assert controller.thread_pool.waitForDone(1000)
    _wait_until(lambda: controller._active_worker is None)

    assert results == []
    assert errors == []
    assert controller._shutting_down
    assert controller.timer.stopped
    assert controller.tray.hidden
    assert controller.hard_exits == []
    assert app_quit == [True]
    assert window_closed == [True]


def test_quit_without_active_worker_uses_normal_application_exit() -> None:
    controller = _worker_controller()
    app_quit: list[bool] = []
    window_closed: list[bool] = []
    controller.app = SimpleNamespace(quit=lambda: app_quit.append(True))
    controller.window.close = lambda: window_closed.append(True)

    controller.quit()

    assert controller._shutting_down
    assert controller.timer.stopped
    assert controller.tray.hidden
    assert controller.hard_exits == []
    assert app_quit == [True]
    assert window_closed == [True]


def test_quit_hard_exits_subprocess_when_worker_exceeds_grace(tmp_path) -> None:
    log_path = tmp_path / "quit.log"
    script = r'''
import logging
import os
import threading
import time

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from clockout.qt_app import AppController


class Window:
    _really_close = False

    def set_connection_busy(self, busy, task_name=""):
        pass

    def close(self):
        pass


class Tray:
    def hide(self):
        pass


app = QApplication([])
controller = AppController.__new__(AppController)
controller.app = app
controller.window = Window()
controller.tray = Tray()
controller.timer = QTimer()
controller.timer.start(1000)
controller.thread_pool = QThreadPool()
controller.thread_pool.setMaxThreadCount(1)
controller.busy = False
controller._active_worker = None
controller._active_task_name = ""
controller._active_task_timed_out = False
controller._worker_generation = 0
controller._worker_timeout_ms = 30000
controller._pending_worker = None
controller._shutting_down = False
controller._quit_grace_ms = 25
controller._hard_exit = os._exit
controller.logger = logging.getLogger("quit-subprocess")
handler = logging.FileHandler(os.environ["QUIT_LOG_PATH"], encoding="utf-8")
controller.logger.addHandler(handler)
started = threading.Event()


def slow_call():
    started.set()
    time.sleep(10)


controller._start_worker(slow_call, lambda value: None, lambda error: None, "慢任务")
assert started.wait(1)
controller.quit()
raise RuntimeError("hard exit unexpectedly returned")
'''
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["QUIT_LOG_PATH"] = str(log_path)
    project_source = str((Path(__file__).resolve().parents[1] / "src"))
    environment["PYTHONPATH"] = os.pathsep.join(
        [project_source, environment.get("PYTHONPATH", "")]
    )
    started_at = wall_time.monotonic()

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=3,
        env=environment,
    )

    assert wall_time.monotonic() - started_at < 2
    assert completed.returncode == 0
    assert "RuntimeError" not in completed.stderr
    assert "程序将立即退出" in log_path.read_text(encoding="utf-8")


def test_quit_without_worker_is_normal_in_qapplication_subprocess() -> None:
    script = r'''
import logging

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from clockout.qt_app import AppController


class Window:
    _really_close = False

    def close(self):
        pass


class Tray:
    def hide(self):
        pass


app = QApplication([])
controller = AppController.__new__(AppController)
controller.app = app
controller.window = Window()
controller.tray = Tray()
controller.timer = QTimer()
controller.timer.start(1000)
controller.thread_pool = QThreadPool()
controller.busy = False
controller._active_worker = None
controller._worker_generation = 0
controller._pending_worker = None
controller._shutting_down = False
controller._quit_grace_ms = 25
controller.logger = logging.getLogger("normal-quit-subprocess")


def unexpected_hard_exit(code):
    raise RuntimeError(f"unexpected hard exit: {code}")


controller._hard_exit = unexpected_hard_exit
controller.quit()
print("normal exit")
'''
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    project_source = str((Path(__file__).resolve().parents[1] / "src"))
    environment["PYTHONPATH"] = os.pathsep.join(
        [project_source, environment.get("PYTHONPATH", "")]
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=3,
        env=environment,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "normal exit"
    assert "RuntimeError" not in completed.stderr


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
