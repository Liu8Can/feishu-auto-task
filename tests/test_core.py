from datetime import date, datetime, time

import pytest

from clockout.core import (
    CheckInTimeError,
    calculate_eligible_time,
    is_within_window,
    is_workday,
    parse_unique_check_in_time,
)


@pytest.mark.parametrize(
    "text_value",
    [
        "上班打卡 09:03",
        "上班已打卡 09:03",
        "打卡时间 09:03",
        "正常 09:03",
    ],
)
def test_parse_supported_check_in_text(text_value: str) -> None:
    assert parse_unique_check_in_time([text_value]) == time(9, 3)


def test_same_time_from_multiple_sources_is_not_ambiguous() -> None:
    assert parse_unique_check_in_time(
        ["上班打卡 09:03", "上班已打卡 09:03"]
    ) == time(9, 3)


@pytest.mark.parametrize(
    "texts",
    [
        [],
        ["更新时间 09:03"],
        ["已打卡 09:03"],
        ["上班打卡 09:03", "上班打卡 09:04"],
        ["上班打卡 24:00"],
    ],
)
def test_missing_ambiguous_or_invalid_check_in_time_is_rejected(
    texts: list[str],
) -> None:
    with pytest.raises(CheckInTimeError):
        parse_unique_check_in_time(texts)


def test_calculate_dynamic_eligible_time() -> None:
    assert calculate_eligible_time(date(2026, 9, 15), time(9, 0)) == datetime(
        2026, 9, 15, 19, 5
    )


@pytest.mark.parametrize(
    ("check_in", "duration", "expected"),
    [
        (time(11, 0), 120, datetime(2026, 9, 15, 15, 5)),
        (time(13, 0), 60, datetime(2026, 9, 15, 15, 5)),
        (time(14, 0), 480, datetime(2026, 9, 15, 22, 5)),
        (time(8, 0), 240, datetime(2026, 9, 15, 12, 5)),
    ],
)
def test_dynamic_time_excludes_only_overlapping_break_minutes(
    check_in: time, duration: int, expected: datetime
) -> None:
    assert (
        calculate_eligible_time(
            date(2026, 9, 15),
            check_in,
            work_duration_minutes=duration,
        )
        == expected
    )


def test_fixed_time_uses_planned_clockout_for_early_check_in() -> None:
    assert calculate_eligible_time(
        date(2026, 9, 15),
        time(8, 40),
        work_duration_minutes=1,
        safety_buffer_minutes=180,
        calculation_mode="fixed",
        break_start_time=time(1, 0),
        break_end_time=time(23, 0),
        fixed_checkin_time=time(8, 50),
        fixed_clockout_time=time(18, 50),
    ) == datetime(2026, 9, 15, 18, 50)


def test_fixed_time_delays_clockout_when_actual_check_in_is_late() -> None:
    assert calculate_eligible_time(
        date(2026, 9, 15),
        time(9, 10),
        calculation_mode="fixed",
        fixed_checkin_time=time(8, 50),
        fixed_clockout_time=time(18, 50),
    ) == datetime(2026, 9, 15, 19, 10)


@pytest.mark.parametrize(
    "changes",
    [
        {"calculation_mode": "other"},
        {"break_start_time": time(14), "break_end_time": time(12)},
        {"fixed_checkin_time": time(18, 50), "fixed_clockout_time": time(18, 50)},
    ],
)
def test_invalid_eligible_time_options_are_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        calculate_eligible_time(
            date(2026, 9, 15), time(9), **changes  # type: ignore[arg-type]
        )


def test_workday_overrides_are_deterministic() -> None:
    saturday = date(2026, 9, 19)
    monday = date(2026, 9, 21)

    assert not is_workday(saturday)
    assert is_workday(saturday, extra_workdays=frozenset({saturday}))
    assert not is_workday(monday, excluded_dates=frozenset({monday}))
    assert not is_workday(monday, weekdays=frozenset({1, 2, 3, 4}))
    assert not is_workday(
        saturday,
        extra_workdays=frozenset({saturday}),
        excluded_dates=frozenset({saturday}),
    )


def test_check_window_includes_both_boundaries() -> None:
    assert is_within_window(datetime(2026, 9, 15, 15, 0), time(15), time(23, 30))
    assert is_within_window(
        datetime(2026, 9, 15, 23, 30), time(15), time(23, 30)
    )
    assert not is_within_window(
        datetime(2026, 9, 15, 23, 31), time(15), time(23, 30)
    )
