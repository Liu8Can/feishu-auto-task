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
        2026, 9, 15, 17, 5
    )


def test_workday_overrides_are_deterministic() -> None:
    saturday = date(2026, 9, 19)
    monday = date(2026, 9, 21)

    assert not is_workday(saturday)
    assert is_workday(saturday, extra_workdays=frozenset({saturday}))
    assert not is_workday(monday, excluded_dates=frozenset({monday}))
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
