from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from clockout.config import AppConfig
from clockout.gui import ClockoutDemoApp
from clockout.storage import JsonStateStore


def test_gui_passes_configured_weekdays_to_engine(tmp_path: object) -> None:
    app = object.__new__(ClockoutDemoApp)
    app.config = AppConfig(
        calculation_mode="fixed",
        fixed_checkin_time="08:30",
        fixed_clockout_time="18:30",
        break_start_time="11:45",
        break_end_time="13:15",
        weekdays=(0, 2),
    )
    app.adapter = object()
    app.store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]

    engine = app._engine()

    assert engine.config.weekdays == frozenset({0, 2})
    assert engine.config.calculation_mode == "fixed"
    assert engine.config.fixed_checkin_time.strftime("%H:%M") == "08:30"
    assert engine.config.fixed_clockout_time.strftime("%H:%M") == "18:30"
    assert engine.config.break_start_time.strftime("%H:%M") == "11:45"
    assert engine.config.break_end_time.strftime("%H:%M") == "13:15"


def test_schedule_settings_build_valid_config_without_losing_binding() -> None:
    current = AppConfig(trusted_container_fingerprint="bound-page")

    updated = ClockoutDemoApp._updated_schedule_config(
        current,
        calculation_mode="fixed",
        work_duration_minutes="450",
        buffer_minutes="8",
        break_start_time="12:15",
        break_end_time="13:45",
        fixed_checkin_time="09:00",
        fixed_clockout_time="18:30",
        auto_open_workbench=False,
    )

    assert updated.calculation_mode == "fixed"
    assert updated.fixed_checkin_time == "09:00"
    assert updated.fixed_clockout_time == "18:30"
    assert not updated.auto_open_workbench
    assert updated.trusted_container_fingerprint == "bound-page"


def test_schedule_settings_reject_non_integer_minutes() -> None:
    try:
        ClockoutDemoApp._updated_schedule_config(
            AppConfig(),
            calculation_mode="dynamic",
            work_duration_minutes="八小时",
            buffer_minutes="5",
            break_start_time="12:00",
            break_end_time="14:00",
            fixed_checkin_time="08:50",
            fixed_clockout_time="18:50",
            auto_open_workbench=True,
        )
    except ValueError as exc:
        assert str(exc) == "工作时长和安全缓冲必须填写整数分钟"
    else:
        raise AssertionError("非整数工作时长必须被拒绝")


def test_schedule_change_reschedules_running_monitor_immediately() -> None:
    app = object.__new__(ClockoutDemoApp)
    app.config = AppConfig()
    app.adapter = SimpleNamespace(auto_open_workbench=True)
    app.running = True
    app.next_check = datetime.now() + timedelta(hours=1)
    shown: list[str] = []
    app.next_check_var = SimpleNamespace(set=shown.append)
    before = datetime.now()

    updated = AppConfig(calculation_mode="fixed")
    app._apply_schedule_config(updated)

    assert app.config is updated
    assert before <= app.next_check <= datetime.now()
    assert shown == ["即将按新设置检查"]
