from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import AppConfig
from .core import PunchAction, is_workday


@dataclass(frozen=True, slots=True)
class ScheduledRun:
    action: PunchAction
    run_at: datetime


_ACTION_PRIORITY = {
    PunchAction.CHECK_IN: 0,
    PunchAction.CHECK_OUT: 1,
}


def next_scheduled_run(
    now: datetime,
    config: AppConfig,
    *,
    completed_actions: Collection[PunchAction] = (),
    retry_at_by_action: Mapping[PunchAction, datetime | None] | None = None,
    eligible_at_by_action: Mapping[PunchAction, datetime | None] | None = None,
    extra_workdays: frozenset[date] = frozenset(),
    excluded_dates: frozenset[date] = frozenset(),
) -> ScheduledRun | None:
    """Return the earliest enabled action at or after ``now``.

    Completion, retry and eligibility values describe ``now.date()`` only. Future
    workdays always begin with fresh action state. A pending action that becomes
    due while the computer sleeps is returned at ``now`` when it is still inside
    its window; actions are never moved past their configured window end.
    """
    config.validate()
    if not config.monitor_enabled:
        return None

    completed = {PunchAction(action) for action in completed_actions}
    retries = _normalize_action_times(retry_at_by_action, now)
    eligibility = _normalize_action_times(eligible_at_by_action, now)
    enabled_actions = _enabled_actions(config)
    if not enabled_actions:
        return None

    day = now.date()
    while True:
        if is_workday(
            day,
            weekdays=frozenset(config.weekdays),
            extra_workdays=extra_workdays,
            excluded_dates=excluded_dates,
        ):
            candidates = _day_candidates(
                day,
                now,
                config,
                enabled_actions,
                completed if day == now.date() else set(),
                retries if day == now.date() else {},
                eligibility if day == now.date() else {},
            )
            if candidates:
                return min(
                    candidates,
                    key=lambda candidate: (
                        candidate.run_at,
                        _ACTION_PRIORITY[candidate.action],
                    ),
                )
        day += timedelta(days=1)


def _normalize_action_times(
    values: Mapping[PunchAction, datetime | None] | None,
    now: datetime,
) -> dict[PunchAction, datetime]:
    if values is None:
        return {}
    normalized: dict[PunchAction, datetime] = {}
    now_is_aware = _is_aware(now)
    for action, moment in values.items():
        if moment is None:
            continue
        if _is_aware(moment) != now_is_aware:
            raise ValueError("动作时间必须与当前时间同为带时区或无时区时间")
        normalized_moment = (
            moment.astimezone(now.tzinfo) if now_is_aware else moment
        )
        if normalized_moment.date() != now.date():
            raise ValueError("动作时间必须属于当前日期")
        normalized[PunchAction(action)] = normalized_moment
    return normalized


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _enabled_actions(config: AppConfig) -> tuple[PunchAction, ...]:
    actions: list[PunchAction] = []
    if config.auto_check_in_enabled:
        actions.append(PunchAction.CHECK_IN)
    if config.auto_check_out_enabled:
        actions.append(PunchAction.CHECK_OUT)
    return tuple(actions)


def _day_candidates(
    day: date,
    now: datetime,
    config: AppConfig,
    enabled_actions: Collection[PunchAction],
    completed_actions: Collection[PunchAction],
    retry_at_by_action: Mapping[PunchAction, datetime],
    eligible_at_by_action: Mapping[PunchAction, datetime],
) -> list[ScheduledRun]:
    candidates: list[ScheduledRun] = []
    windows = {
        PunchAction.CHECK_IN: (config.check_in_start, config.check_in_end),
        PunchAction.CHECK_OUT: (config.check_out_start, config.check_out_end),
    }

    for action in enabled_actions:
        if action in completed_actions:
            continue
        start_time, end_time = windows[action]
        window_start = _on_day(day, start_time, now)
        window_end = _on_day(day, end_time, now)
        run_at = max(
            now,
            window_start,
            retry_at_by_action.get(action, window_start),
            eligible_at_by_action.get(action, window_start),
        )
        if run_at <= window_end:
            candidates.append(ScheduledRun(action, run_at))
    return candidates


def _on_day(day: date, value: time, now: datetime) -> datetime:
    return datetime.combine(day, value, tzinfo=now.tzinfo)
