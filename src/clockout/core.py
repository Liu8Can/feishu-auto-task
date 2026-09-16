from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Iterable


class CheckInTimeError(ValueError):
    """Raised when the check-in time cannot be determined safely."""


class PunchAction(str, Enum):
    CHECK_IN = "check_in"
    CHECK_OUT = "check_out"


@dataclass(frozen=True, slots=True)
class AttendanceSnapshot:
    page_date: date | None
    check_in_time: time | None
    already_clocked_out: bool
    button_count: int
    button_enabled: bool
    blocking_reason: str | None
    signature: str
    container_id: str
    button_id: str
    action: PunchAction = PunchAction.CHECK_OUT
    action_completed: bool = False


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: str
    message: str
    check_in_time: time | None = None
    eligible_time: datetime | None = None
    next_retry_time: datetime | None = None


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
    *,
    calculation_mode: str = "dynamic",
    break_start_time: time = time(12, 0),
    break_end_time: time = time(14, 0),
    fixed_checkin_time: time = time(8, 50),
    fixed_clockout_time: time = time(18, 50),
) -> datetime:
    if (
        isinstance(work_duration_minutes, bool)
        or not isinstance(work_duration_minutes, int)
        or isinstance(safety_buffer_minutes, bool)
        or not isinstance(safety_buffer_minutes, int)
        or work_duration_minutes < 0
        or safety_buffer_minutes < 0
    ):
        raise ValueError("工作时长和安全缓冲不能为负数")
    if calculation_mode not in {"dynamic", "fixed"}:
        raise ValueError("下班时间计算模式只能是 dynamic 或 fixed")
    if not all(
        isinstance(value, time)
        for value in (
            check_in_time,
            break_start_time,
            break_end_time,
            fixed_checkin_time,
            fixed_clockout_time,
        )
    ):
        raise ValueError("打卡、休息和固定下班时间必须是有效时间")
    if break_start_time >= break_end_time:
        raise ValueError("休息开始时间必须早于休息结束时间")
    if fixed_checkin_time >= fixed_clockout_time:
        raise ValueError("固定上班时间必须早于固定下班时间")

    if calculation_mode == "fixed":
        planned_checkin = datetime.combine(day, fixed_checkin_time)
        actual_checkin = datetime.combine(day, check_in_time)
        delay = max(timedelta(), actual_checkin - planned_checkin)
        return datetime.combine(day, fixed_clockout_time) + delay

    cursor = datetime.combine(day, check_in_time)
    break_start = datetime.combine(day, break_start_time)
    break_end = datetime.combine(day, break_end_time)
    remaining = timedelta(minutes=work_duration_minutes)

    if not remaining:
        return cursor + timedelta(minutes=safety_buffer_minutes)

    if cursor < break_end:
        if cursor < break_start:
            work_before_break = break_start - cursor
            if remaining <= work_before_break:
                cursor += remaining
                remaining = timedelta()
            else:
                remaining -= work_before_break
                cursor = break_end
        else:
            cursor = break_end

    return cursor + remaining + timedelta(minutes=safety_buffer_minutes)


def is_workday(
    day: date,
    *,
    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4}),
    extra_workdays: frozenset[date] = frozenset(),
    excluded_dates: frozenset[date] = frozenset(),
) -> bool:
    if day in excluded_dates:
        return False
    if day in extra_workdays:
        return True
    return day.weekday() in weekdays


def is_within_window(moment: datetime, start: time, end: time) -> bool:
    if start > end:
        raise ValueError("第一版不支持跨自然日的检查时间窗")
    return start <= moment.time() <= end


def snapshots_match(first: AttendanceSnapshot, second: AttendanceSnapshot) -> bool:
    """Compare every click-relevant field, including a UI-provided signature."""
    return bool(first.signature) and first == second
