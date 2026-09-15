from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import time
from pathlib import Path


def _parse_time(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"无效时间：{value!r}") from exc


@dataclass(frozen=True)
class AppConfig:
    work_duration_minutes: int = 480
    buffer_minutes: int = 5
    check_interval_minutes: int = 5
    check_start_time: str = "15:00"
    check_end_time: str = "23:30"
    mode: str = "dry_run"
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    auto_open_workbench: bool = True

    def validate(self) -> AppConfig:
        if not 1 <= self.work_duration_minutes <= 24 * 60:
            raise ValueError("工作时长必须在 1 到 1440 分钟之间")
        if not 0 <= self.buffer_minutes <= 180:
            raise ValueError("安全缓冲必须在 0 到 180 分钟之间")
        if not 1 <= self.check_interval_minutes <= 60:
            raise ValueError("检查间隔必须在 1 到 60 分钟之间")
        _parse_time(self.check_start_time)
        _parse_time(self.check_end_time)
        if self.mode not in {"dry_run", "automatic"}:
            raise ValueError("运行模式只能是 dry_run 或 automatic")
        if not self.weekdays or any(day not in range(7) for day in self.weekdays):
            raise ValueError("工作日配置无效")
        return self

    @property
    def start_time(self) -> time:
        return _parse_time(self.check_start_time)

    @property
    def end_time(self) -> time:
        return _parse_time(self.check_end_time)

    def with_mode(self, mode: str) -> AppConfig:
        return replace(self, mode=mode).validate()


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        config = AppConfig()
        save_config(path, config)
        return config
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "weekdays" in raw:
        raw["weekdays"] = tuple(raw["weekdays"])
    return AppConfig(**raw).validate()


def save_config(path: Path, config: AppConfig) -> None:
    config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(config)
    payload["weekdays"] = list(config.weekdays)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)

