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
    schema_version: int = 2
    work_duration_minutes: int = 480
    buffer_minutes: int = 5
    check_interval_minutes: int = 5
    check_in_window_start: str = "07:00"
    check_in_window_end: str = "11:00"
    check_out_window_start: str = "15:00"
    check_out_window_end: str = "23:30"
    auto_check_in_enabled: bool = False
    auto_check_out_enabled: bool = True
    mode: str = "automatic"
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    auto_open_workbench: bool = True
    trusted_container_fingerprint: str = ""
    calculation_mode: str = "dynamic"
    break_start_time: str = "12:00"
    break_end_time: str = "14:00"
    fixed_checkin_time: str = "08:50"
    fixed_clockout_time: str = "18:50"
    monitor_enabled: bool = True
    start_with_windows: bool = True
    max_click_attempts: int = 3
    retry_delay_minutes: int = 5
    theme_mode: str = "system"
    # Constructor-only compatibility for callers that still use the v1 names.
    check_start_time: str | None = None
    check_end_time: str | None = None

    def validate(self) -> AppConfig:
        if isinstance(self.schema_version, bool) or self.schema_version != 2:
            raise ValueError("配置版本无效")
        if self.calculation_mode not in {"dynamic", "fixed"}:
            raise ValueError("下班时间计算模式只能是 dynamic 或 fixed")
        if (
            isinstance(self.work_duration_minutes, bool)
            or not isinstance(self.work_duration_minutes, int)
            or not 1 <= self.work_duration_minutes <= 24 * 60
        ):
            raise ValueError("工作时长必须在 1 到 1440 分钟之间")
        if (
            isinstance(self.buffer_minutes, bool)
            or not isinstance(self.buffer_minutes, int)
            or not 0 <= self.buffer_minutes <= 180
        ):
            raise ValueError("安全缓冲必须在 0 到 180 分钟之间")
        if (
            isinstance(self.check_interval_minutes, bool)
            or not isinstance(self.check_interval_minutes, int)
            or not 1 <= self.check_interval_minutes <= 60
        ):
            raise ValueError("检查间隔必须在 1 到 60 分钟之间")
        if not isinstance(self.monitor_enabled, bool):
            raise ValueError("监控开关配置无效")
        if not isinstance(self.start_with_windows, bool):
            raise ValueError("开机自启动配置无效")
        if not isinstance(self.auto_check_in_enabled, bool):
            raise ValueError("上班自动打卡开关配置无效")
        if not isinstance(self.auto_check_out_enabled, bool):
            raise ValueError("下班自动打卡开关配置无效")
        if self.theme_mode not in {"system", "light", "dark"}:
            raise ValueError("界面主题只能是 system、light 或 dark")
        if (
            isinstance(self.max_click_attempts, bool)
            or not isinstance(self.max_click_attempts, int)
            or not 1 <= self.max_click_attempts <= 5
        ):
            raise ValueError("自动点击尝试次数必须在 1 到 5 次之间")
        if (
            isinstance(self.retry_delay_minutes, bool)
            or not isinstance(self.retry_delay_minutes, int)
            or not 1 <= self.retry_delay_minutes <= 60
        ):
            raise ValueError("重试间隔必须在 1 到 60 分钟之间")
        for label, start_value, end_value in (
            ("上班", self.check_in_window_start, self.check_in_window_end),
            ("下班", self._checkout_start_value, self._checkout_end_value),
        ):
            start = _parse_time(start_value)
            end = _parse_time(end_value)
            if start > end:
                raise ValueError(f"{label}打卡不支持跨自然日的检查时间窗")
        break_start = _parse_time(self.break_start_time)
        break_end = _parse_time(self.break_end_time)
        if break_start >= break_end:
            raise ValueError("休息开始时间必须早于休息结束时间")
        fixed_checkin = _parse_time(self.fixed_checkin_time)
        fixed_clockout = _parse_time(self.fixed_clockout_time)
        if fixed_checkin >= fixed_clockout:
            raise ValueError("固定上班时间必须早于固定下班时间")
        if self.mode not in {"dry_run", "automatic"}:
            raise ValueError("运行模式只能是 dry_run 或 automatic")
        if not self.weekdays or any(day not in range(7) for day in self.weekdays):
            raise ValueError("工作日配置无效")
        return self

    @property
    def start_time(self) -> time:
        return _parse_time(self._checkout_start_value)

    @property
    def end_time(self) -> time:
        return _parse_time(self._checkout_end_value)

    @property
    def check_in_start(self) -> time:
        return _parse_time(self.check_in_window_start)

    @property
    def check_in_end(self) -> time:
        return _parse_time(self.check_in_window_end)

    @property
    def check_out_start(self) -> time:
        return self.start_time

    @property
    def check_out_end(self) -> time:
        return self.end_time

    @property
    def _checkout_start_value(self) -> str:
        return self.check_start_time or self.check_out_window_start

    @property
    def _checkout_end_value(self) -> str:
        return self.check_end_time or self.check_out_window_end

    @property
    def break_start(self) -> time:
        return _parse_time(self.break_start_time)

    @property
    def break_end(self) -> time:
        return _parse_time(self.break_end_time)

    @property
    def fixed_checkin(self) -> time:
        return _parse_time(self.fixed_checkin_time)

    @property
    def fixed_clockout(self) -> time:
        return _parse_time(self.fixed_clockout_time)

    def with_mode(self, mode: str) -> AppConfig:
        return replace(self, mode=mode).validate()


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        config = AppConfig()
        save_config(path, config)
        return config
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("配置根节点必须是对象")
    schema_version = raw.get("schema_version", 1)
    if isinstance(schema_version, bool) or schema_version not in {1, 2}:
        raise ValueError("配置版本无效")
    migrated = schema_version != 2 or "check_start_time" in raw or "check_end_time" in raw
    legacy_start = raw.pop("check_start_time", None)
    legacy_end = raw.pop("check_end_time", None)
    if legacy_start is not None:
        canonical_start = raw.get("check_out_window_start")
        if canonical_start is not None and canonical_start != legacy_start:
            raise ValueError("旧版与新版下班开始时间冲突")
        raw["check_out_window_start"] = legacy_start
    if legacy_end is not None:
        canonical_end = raw.get("check_out_window_end")
        if canonical_end is not None and canonical_end != legacy_end:
            raise ValueError("旧版与新版下班结束时间冲突")
        raw["check_out_window_end"] = legacy_end
    raw["schema_version"] = 2
    if "weekdays" in raw:
        raw["weekdays"] = tuple(raw["weekdays"])
    config = AppConfig(**raw).validate()
    if migrated:
        save_config(path, config)
    return config


def save_config(path: Path, config: AppConfig) -> None:
    config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(config)
    payload["weekdays"] = list(config.weekdays)
    payload["check_out_window_start"] = config._checkout_start_value
    payload["check_out_window_end"] = config._checkout_end_value
    payload.pop("check_start_time", None)
    payload.pop("check_end_time", None)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)
