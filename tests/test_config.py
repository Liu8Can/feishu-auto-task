from __future__ import annotations

import json

import pytest

from clockout.config import AppConfig, load_config, save_config


def test_config_round_trip_preserves_chinese_safe_json(tmp_path: object) -> None:
    path = tmp_path / "config.json"  # type: ignore[operator]
    config = AppConfig(
        calculation_mode="fixed",
        buffer_minutes=8,
        fixed_checkin_time="09:10",
        fixed_clockout_time="19:10",
        mode="automatic",
    )

    save_config(path, config)

    assert load_config(path) == config
    assert json.loads(path.read_text(encoding="utf-8"))["buffer_minutes"] == 8


def test_old_config_without_schedule_fields_uses_new_defaults(tmp_path: object) -> None:
    path = tmp_path / "config.json"  # type: ignore[operator]
    path.write_text('{"mode": "dry_run"}', encoding="utf-8")

    config = load_config(path)

    assert config.calculation_mode == "dynamic"
    assert config.break_start_time == "12:00"
    assert config.break_end_time == "14:00"
    assert config.fixed_checkin_time == "08:50"
    assert config.fixed_clockout_time == "18:50"


@pytest.mark.parametrize(
    "changes",
    [
        {"work_duration_minutes": 0},
        {"work_duration_minutes": "480"},
        {"buffer_minutes": True},
        {"buffer_minutes": -1},
        {"check_interval_minutes": 0},
        {"monitor_enabled": "yes"},
        {"start_with_windows": 1},
        {"max_click_attempts": 0},
        {"retry_delay_minutes": 61},
        {"check_start_time": "25:00"},
        {"check_start_time": "23:00", "check_end_time": "15:00"},
        {"calculation_mode": "unsafe"},
        {"break_start_time": "14:00", "break_end_time": "12:00"},
        {"break_start_time": "12:00", "break_end_time": "12:00"},
        {"fixed_checkin_time": "18:50", "fixed_clockout_time": "18:50"},
        {"fixed_checkin_time": "19:00", "fixed_clockout_time": "18:50"},
        {"fixed_clockout_time": "not-a-time"},
        {"mode": "unsafe"},
    ],
)
def test_invalid_config_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AppConfig(**changes).validate()
