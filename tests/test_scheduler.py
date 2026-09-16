from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from clockout.config import AppConfig
from clockout.core import PunchAction
from clockout.scheduler import ScheduledRun, next_scheduled_run


def test_default_config_only_schedules_checkout() -> None:
    now = datetime(2026, 9, 16, 9, 0)

    assert next_scheduled_run(now, AppConfig()) == ScheduledRun(
        PunchAction.CHECK_OUT,
        datetime(2026, 9, 16, 15, 0),
    )


def test_checkin_is_scheduled_when_explicitly_enabled() -> None:
    config = AppConfig(auto_check_in_enabled=True)

    assert next_scheduled_run(datetime(2026, 9, 16, 6, 30), config) == ScheduledRun(
        PunchAction.CHECK_IN,
        datetime(2026, 9, 16, 7, 0),
    )


def test_sleep_recovery_runs_pending_action_immediately_inside_window() -> None:
    config = AppConfig(auto_check_in_enabled=True)
    now = datetime(2026, 9, 16, 8, 23, 17)

    assert next_scheduled_run(now, config) == ScheduledRun(PunchAction.CHECK_IN, now)


def test_missed_checkin_is_not_backfilled_and_checkout_still_runs_today() -> None:
    config = AppConfig(
        auto_check_in_enabled=True,
        check_out_window_start="15:00",
    )

    assert next_scheduled_run(datetime(2026, 9, 16, 11, 1), config) == ScheduledRun(
        PunchAction.CHECK_OUT,
        datetime(2026, 9, 16, 15, 0),
    )


def test_completed_checkin_does_not_suppress_checkout() -> None:
    config = AppConfig(auto_check_in_enabled=True)

    assert next_scheduled_run(
        datetime(2026, 9, 16, 8, 0),
        config,
        completed_actions={PunchAction.CHECK_IN},
    ) == ScheduledRun(PunchAction.CHECK_OUT, datetime(2026, 9, 16, 15, 0))


def test_due_retry_runs_now_after_sleep_recovery() -> None:
    config = AppConfig(auto_check_in_enabled=True)
    now = datetime(2026, 9, 16, 8, 30)

    assert next_scheduled_run(
        now,
        config,
        retry_at_by_action={
            PunchAction.CHECK_IN: datetime(2026, 9, 16, 8, 10),
        },
    ) == ScheduledRun(PunchAction.CHECK_IN, now)


def test_future_retry_is_used_when_it_is_inside_window() -> None:
    config = AppConfig(auto_check_in_enabled=True)
    retry_at = datetime(2026, 9, 16, 9, 10)

    assert next_scheduled_run(
        datetime(2026, 9, 16, 8, 30),
        config,
        retry_at_by_action={PunchAction.CHECK_IN: retry_at},
    ) == ScheduledRun(PunchAction.CHECK_IN, retry_at)


def test_retry_past_window_is_discarded_instead_of_delayed() -> None:
    config = AppConfig(auto_check_in_enabled=True)

    assert next_scheduled_run(
        datetime(2026, 9, 16, 8, 30),
        config,
        retry_at_by_action={
            PunchAction.CHECK_IN: datetime(2026, 9, 16, 11, 1),
        },
    ) == ScheduledRun(PunchAction.CHECK_OUT, datetime(2026, 9, 16, 15, 0))


def test_checkout_eligibility_is_combined_with_window_start() -> None:
    now = datetime(2026, 9, 16, 14, 0)
    eligible_at = datetime(2026, 9, 16, 19, 5)

    assert next_scheduled_run(
        now,
        AppConfig(),
        eligible_at_by_action={PunchAction.CHECK_OUT: eligible_at},
    ) == ScheduledRun(PunchAction.CHECK_OUT, eligible_at)


def test_checkout_eligibility_past_window_is_not_backfilled() -> None:
    now = datetime(2026, 9, 16, 14, 0)

    assert next_scheduled_run(
        now,
        AppConfig(),
        eligible_at_by_action={
            PunchAction.CHECK_OUT: datetime(2026, 9, 16, 23, 31),
        },
    ) == ScheduledRun(PunchAction.CHECK_OUT, datetime(2026, 9, 17, 15, 0))


def test_same_time_prefers_checkin_stably() -> None:
    config = AppConfig(
        auto_check_in_enabled=True,
        check_in_window_start="09:00",
        check_in_window_end="18:00",
        check_out_window_start="09:00",
        check_out_window_end="18:00",
    )

    assert next_scheduled_run(datetime(2026, 9, 16, 8, 0), config) == ScheduledRun(
        PunchAction.CHECK_IN,
        datetime(2026, 9, 16, 9, 0),
    )


def test_weekend_is_skipped() -> None:
    saturday = datetime(2026, 9, 19, 10, 0)

    assert next_scheduled_run(saturday, AppConfig()) == ScheduledRun(
        PunchAction.CHECK_OUT,
        datetime(2026, 9, 21, 15, 0),
    )


def test_configured_weekend_and_date_overrides_are_honored() -> None:
    saturday = datetime(2026, 9, 19, 10, 0)
    config = AppConfig(weekdays=(5,))

    assert next_scheduled_run(saturday, config) == ScheduledRun(
        PunchAction.CHECK_OUT,
        datetime(2026, 9, 19, 15, 0),
    )
    assert next_scheduled_run(
        saturday,
        config,
        excluded_dates=frozenset({saturday.date()}),
    ) == ScheduledRun(PunchAction.CHECK_OUT, datetime(2026, 9, 26, 15, 0))

    sunday = datetime(2026, 9, 20, 10, 0)
    assert next_scheduled_run(
        sunday,
        AppConfig(),
        extra_workdays=frozenset({sunday.date()}),
    ) == ScheduledRun(PunchAction.CHECK_OUT, datetime(2026, 9, 20, 15, 0))


def test_completed_state_applies_only_to_current_day() -> None:
    now = datetime(2026, 9, 16, 23, 31)

    assert next_scheduled_run(
        now,
        AppConfig(),
        completed_actions={PunchAction.CHECK_OUT},
    ) == ScheduledRun(PunchAction.CHECK_OUT, datetime(2026, 9, 17, 15, 0))


def test_recalculation_uses_the_callers_current_time() -> None:
    config = AppConfig(auto_check_in_enabled=True)
    first = next_scheduled_run(datetime(2026, 9, 16, 8, 0), config)
    after_clock_rollback = next_scheduled_run(datetime(2026, 9, 16, 7, 30), config)

    assert first == ScheduledRun(PunchAction.CHECK_IN, datetime(2026, 9, 16, 8, 0))
    assert after_clock_rollback == ScheduledRun(
        PunchAction.CHECK_IN,
        datetime(2026, 9, 16, 7, 30),
    )


def test_window_end_is_inclusive_and_next_moment_moves_to_next_workday() -> None:
    at_end = datetime(2026, 9, 16, 23, 30)
    after_end = datetime(2026, 9, 16, 23, 30, 0, 1)

    assert next_scheduled_run(at_end, AppConfig()) == ScheduledRun(
        PunchAction.CHECK_OUT,
        at_end,
    )
    assert next_scheduled_run(after_end, AppConfig()) == ScheduledRun(
        PunchAction.CHECK_OUT,
        datetime(2026, 9, 17, 15, 0),
    )


def test_monitor_disabled_or_both_actions_disabled_has_no_run() -> None:
    now = datetime(2026, 9, 16, 9, 0)

    assert next_scheduled_run(now, AppConfig(monitor_enabled=False)) is None
    assert (
        next_scheduled_run(
            now,
            AppConfig(auto_check_in_enabled=False, auto_check_out_enabled=False),
        )
        is None
    )


def test_timezone_is_preserved() -> None:
    now = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)

    result = next_scheduled_run(now, AppConfig())

    assert result is not None
    assert result.run_at.tzinfo is timezone.utc


@pytest.mark.parametrize("field_name", ["retry_at_by_action", "eligible_at_by_action"])
@pytest.mark.parametrize(
    ("now", "action_time"),
    [
        (
            datetime(2026, 9, 16, 14, 0),
            datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 16, 16, 0),
        ),
    ],
)
def test_naive_and_aware_action_times_cannot_be_mixed(
    field_name: str,
    now: datetime,
    action_time: datetime,
) -> None:
    values = {PunchAction.CHECK_OUT: action_time}

    with pytest.raises(ValueError, match="同为带时区或无时区"):
        next_scheduled_run(
            now,
            AppConfig(),
            **{field_name: values},
        )


def test_aware_action_time_is_converted_to_current_timezone() -> None:
    china_timezone = timezone(timedelta(hours=8))
    now = datetime(2026, 9, 16, 8, 0, tzinfo=china_timezone)
    retry_at_utc = datetime(2026, 9, 16, 1, 30, tzinfo=timezone.utc)

    result = next_scheduled_run(
        now,
        AppConfig(auto_check_in_enabled=True),
        retry_at_by_action={PunchAction.CHECK_IN: retry_at_utc},
    )

    assert result == ScheduledRun(
        PunchAction.CHECK_IN,
        datetime(2026, 9, 16, 9, 30, tzinfo=china_timezone),
    )


def test_timezone_conversion_can_cross_calendar_day() -> None:
    china_timezone = timezone(timedelta(hours=8))
    now = datetime(2026, 9, 16, 7, 0, tzinfo=china_timezone)
    previous_utc_day = datetime(2026, 9, 15, 23, 30, tzinfo=timezone.utc)

    result = next_scheduled_run(
        now,
        AppConfig(auto_check_in_enabled=True),
        retry_at_by_action={PunchAction.CHECK_IN: previous_utc_day},
    )

    assert result == ScheduledRun(
        PunchAction.CHECK_IN,
        datetime(2026, 9, 16, 7, 30, tzinfo=china_timezone),
    )


@pytest.mark.parametrize("field_name", ["retry_at_by_action", "eligible_at_by_action"])
@pytest.mark.parametrize("day_offset", [-1, 1])
def test_action_times_must_belong_to_current_local_day(
    field_name: str, day_offset: int
) -> None:
    now = datetime(2026, 9, 16, 8, 0)
    action_time = now + timedelta(days=day_offset)

    with pytest.raises(ValueError, match="当前日期"):
        next_scheduled_run(
            now,
            AppConfig(auto_check_in_enabled=True),
            **{field_name: {PunchAction.CHECK_IN: action_time}},
        )


def test_action_date_is_checked_after_timezone_conversion() -> None:
    china_timezone = timezone(timedelta(hours=8))
    now = datetime(2026, 9, 16, 23, 0, tzinfo=china_timezone)
    next_local_day_in_utc = datetime(2026, 9, 16, 16, 30, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="当前日期"):
        next_scheduled_run(
            now,
            AppConfig(),
            eligible_at_by_action={
                PunchAction.CHECK_OUT: next_local_day_in_utc,
            },
        )
