from __future__ import annotations

import json

import pytest

from clockout.config import AppConfig, load_config, save_config


def test_config_round_trip_preserves_chinese_safe_json(tmp_path: object) -> None:
    path = tmp_path / "config.json"  # type: ignore[operator]
    config = AppConfig(buffer_minutes=8, mode="automatic")

    save_config(path, config)

    assert load_config(path) == config
    assert json.loads(path.read_text(encoding="utf-8"))["buffer_minutes"] == 8


@pytest.mark.parametrize(
    "changes",
    [
        {"work_duration_minutes": 0},
        {"buffer_minutes": -1},
        {"check_interval_minutes": 0},
        {"check_start_time": "25:00"},
        {"mode": "unsafe"},
    ],
)
def test_invalid_config_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AppConfig(**changes).validate()
