from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable


class CheckInTimeError(ValueError):
    """Raised when the check-in time cannot be determined safely."""


@dataclass(frozen=True, slots=True)
class AttendanceSnapshot:
    check_in_time: time | None
    already_clocked_out: bool
    button_count: int
    button_enabled: bool
    blocking_reason: str | None
    signature: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: str
    message: str
    check_in_time: time | None = None
    eligible_time: datetime | None = None


_CHECK_IN_PATTERNS = (
    re.compile(r"(?:上班已?打卡|上班打卡)(?:时间)?\s*[:：]?\s*(\d{1,2}:\d{2})"),
    re.compile(r"^\s*打卡时间\s*[:：]?\s*(\d{1,2}:\d{2})\s*$"),
    re.compile(r"^\s*正常\s*[:：]?\s*(\d{1,2}:\d{2})\s*$"),
)


def parse_unique_check_in_time(texts: Iterable[str]) -> time:
    """Return one unambiguous check-in time from attendance-row text."""
    candidates: set[time] = set()
    invalid_values: list[str] = []

    for text_value in texts:
        if not isinstance(text_value, str):
            continue
        for pattern in _CHECK_IN_PATTERNS:
            match = pattern.search(text_value)
            if match is None:
                continue
            raw_time = match.group(1)
            try:
                candidates.add(time.fromisoformat(raw_time))
            except ValueError:
                invalid_values.append(raw_time)
            break

    if invalid_values:
        raise CheckInTimeError(f"上班打卡时间格式无效：{invalid_values[0]}")
    if not candidates:
        raise CheckInTimeError("未识别到上班打卡时间")
    if len(candidates) != 1:
        raise CheckInTimeError("识别到多个不同的上班打卡时间")
    return next(iter(candidates))


def calculate_eligible_time(
    day: date,
    check_in_time: time,
    work_duration_minutes: int = 480,
    safety_buffer_minutes: int = 5,
) -> datetime:
    if work_duration_minutes < 0 or safety_buffer_minutes < 0:
        raise ValueError("工作时长和安全缓冲不能为负数")
    return datetime.combine(day, check_in_time) + timedelta(
        minutes=work_duration_minutes + safety_buffer_minutes
    )


def is_workday(
    day: date,
    *,
    skip_weekends: bool = True,
    extra_workdays: frozenset[date] = frozenset(),
    excluded_dates: frozenset[date] = frozenset(),
) -> bool:
    if day in excluded_dates:
        return False
    if day in extra_workdays:
        return True
    return not skip_weekends or day.weekday() < 5


def is_within_window(moment: datetime, start: time, end: time) -> bool:
    if start > end:
        raise ValueError("第一版不支持跨自然日的检查时间窗")
    return start <= moment.time() <= end


def snapshots_match(first: AttendanceSnapshot, second: AttendanceSnapshot) -> bool:
    """Compare every click-relevant field, including a UI-provided signature."""
    return bool(first.signature) and first == second
