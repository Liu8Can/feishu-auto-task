from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from pathlib import Path
from threading import Barrier
from typing import Callable

from clockout.core import AttendanceSnapshot, PunchAction
from clockout.engine import ClockoutEngine, EngineConfig
from clockout.storage import JsonStateStore


def valid_snapshot(**changes: object) -> AttendanceSnapshot:
    values: dict[str, object] = {
        "page_date": datetime(2026, 9, 15).date(),
        "check_in_time": time(9, 0),
        "already_clocked_out": False,
        "button_count": 1,
        "button_enabled": True,
        "blocking_reason": None,
        "signature": "stable-page-v1",
        "container_id": "attendance-container",
        "button_id": "clock-out-button",
    }
    values.update(changes)
    return AttendanceSnapshot(**values)  # type: ignore[arg-type]


class FakeAdapter:
    def __init__(
        self,
        snapshots: list[AttendanceSnapshot],
        *,
        verify_result: bool = True,
        click_error: bool = False,
        store: JsonStateStore | None = None,
        now: datetime | None = None,
        interactive_results: list[bool] | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.verify_result = verify_result
        self.click_error = click_error
        self.store = store
        self.now = now
        self.interactive_results = interactive_results or [True]
        self.snapshot_calls = 0
        self.click_calls = 0
        self.interactive_calls = 0
        self.cancel_calls = 0
        self.prepared_token: tuple[PunchAction, str] | None = None
        self.clicked_signature = ""
        self.attempt_was_persisted_before_click = False

    def snapshot(self, day: object) -> AttendanceSnapshot:
        snapshot = self.snapshots[self.snapshot_calls]
        self.snapshot_calls += 1
        return snapshot

    def click_clock_out(self, expected_signature: str) -> None:
        self.click_calls += 1
        self.clicked_signature = expected_signature
        if self.store is not None and self.now is not None:
            state = self.store.load(self.now.date())
            self.attempt_was_persisted_before_click = bool(
                state and state.clock_out_attempted
            )
        if self.click_error:
            raise RuntimeError("simulated click failure")

    def prepare_click(
        self, day: object, action: PunchAction, expected_signature: str
    ) -> object:
        self.prepared_token = (action, expected_signature)
        return self.prepared_token

    def cancel_click(self, token: object) -> None:
        assert token == self.prepared_token
        self.cancel_calls += 1
        self.prepared_token = None

    def execute_click(self, token: object) -> bool:
        assert token == self.prepared_token
        _, signature = token  # type: ignore[misc]
        self.click_clock_out(signature)
        self.prepared_token = None
        return self.verify_result

    def verify_success(self) -> bool:
        return self.verify_result

    def is_session_interactive(self) -> bool:
        index = min(self.interactive_calls, len(self.interactive_results) - 1)
        self.interactive_calls += 1
        return self.interactive_results[index]


def make_engine(
    tmp_path: object,
    adapter: FakeAdapter,
    *,
    mode: str = "dry_run",
    now_provider: Callable[[], datetime] | None = None,
    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4}),
) -> tuple[ClockoutEngine, JsonStateStore]:
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    adapter.store = store
    provider = now_provider or (
        lambda: adapter.now or datetime(2026, 9, 15, 17, 5)
    )
    config = EngineConfig(
        mode=mode,
        weekdays=weekdays,
        work_duration_minutes=360,
    )
    return ClockoutEngine(adapter, store, config, now_provider=provider), store


def test_before_dynamic_target_waits_without_clicking(tmp_path: object) -> None:
    adapter = FakeAdapter([valid_snapshot()])
    engine, store = make_engine(tmp_path, adapter)

    result = engine.check(datetime(2026, 9, 15, 17, 4))

    assert result.status == "waiting"
    assert result.eligible_time == datetime(2026, 9, 15, 17, 5)
    assert adapter.click_calls == 0
    assert store.load(datetime(2026, 9, 15).date()) is None


def test_before_target_does_not_require_checkout_button_yet(tmp_path: object) -> None:
    adapter = FakeAdapter(
        [
            valid_snapshot(
                button_count=0,
                button_enabled=False,
                button_id="",
            )
        ]
    )
    engine, store = make_engine(tmp_path, adapter)

    result = engine.check(datetime(2026, 9, 15, 17, 4))

    assert result.status == "waiting"
    assert result.eligible_time == datetime(2026, 9, 15, 17, 5)
    assert adapter.click_calls == 0
    assert store.load(datetime(2026, 9, 15).date()) is None


def test_default_dynamic_target_excludes_two_hour_break(tmp_path: object) -> None:
    adapter = FakeAdapter([valid_snapshot()])
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    engine = ClockoutEngine(adapter, store, EngineConfig())

    result = engine.check(datetime(2026, 9, 15, 19, 4))

    assert result.status == "waiting"
    assert result.eligible_time == datetime(2026, 9, 15, 19, 5)
    assert adapter.click_calls == 0


def test_fixed_mode_uses_configured_clockout_but_requires_check_in(
    tmp_path: object,
) -> None:
    config = EngineConfig(
        calculation_mode="fixed",
        work_duration_minutes=1,
        safety_buffer_minutes=180,
        break_start_time=time(1),
        break_end_time=time(23),
        fixed_checkin_time=time(8, 50),
        fixed_clockout_time=time(18, 50),
    )
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    adapter = FakeAdapter([valid_snapshot()])
    result = ClockoutEngine(adapter, store, config).check(
        datetime(2026, 9, 15, 19, 0)
    )

    assert result.status == "dry_run_ready"
    assert result.eligible_time == datetime(2026, 9, 15, 19, 0)
    assert adapter.click_calls == 0

    missing_store = JsonStateStore(tmp_path / "missing.json")  # type: ignore[operator]
    missing_adapter = FakeAdapter([valid_snapshot(check_in_time=None)])
    missing_result = ClockoutEngine(missing_adapter, missing_store, config).check(
        datetime(2026, 9, 15, 19, 0)
    )
    assert missing_result.status == "blocked"
    assert missing_adapter.click_calls == 0


def test_target_after_check_end_is_blocked_without_clicking(tmp_path: object) -> None:
    snapshot = valid_snapshot(check_in_time=time(16, 0))
    adapter = FakeAdapter([snapshot], interactive_results=[True])
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    engine = ClockoutEngine(
        adapter,
        store,
        EngineConfig(mode="automatic"),
    )

    result = engine.check(datetime(2026, 9, 15, 17, 0))

    assert result.status == "blocked"
    assert "晚于检查结束时间" in result.message
    assert result.eligible_time == datetime(2026, 9, 16, 0, 5)
    assert adapter.click_calls == 0
    assert store.load(datetime(2026, 9, 15).date()) is None


def test_engine_config_rejects_invalid_schedule() -> None:
    for changes in (
        {"calculation_mode": "other"},
        {"work_duration_minutes": 0},
        {"work_duration_minutes": "480"},
        {"safety_buffer_minutes": 181},
        {"check_start_time": "15:00"},
        {"break_start_time": time(14), "break_end_time": time(12)},
        {"fixed_checkin_time": time(18, 50), "fixed_clockout_time": time(18, 50)},
    ):
        try:
            EngineConfig(**changes)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"未拒绝无效配置：{changes}")


def test_dry_run_at_target_is_strictly_zero_click(tmp_path: object) -> None:
    adapter = FakeAdapter([valid_snapshot()])
    engine, store = make_engine(tmp_path, adapter)

    result = engine.check(datetime(2026, 9, 15, 17, 5))

    assert result.status == "dry_run_ready"
    assert adapter.snapshot_calls == 1
    assert adapter.click_calls == 0
    assert store.load(datetime(2026, 9, 15).date()) is None


def test_missing_check_in_or_unsafe_button_blocks(tmp_path: object) -> None:
    no_check_in = FakeAdapter([valid_snapshot(check_in_time=None)])
    engine, _ = make_engine(tmp_path, no_check_in)
    assert engine.check(datetime(2026, 9, 15, 17, 5)).status == "blocked"
    assert no_check_in.click_calls == 0

    duplicate_buttons = FakeAdapter([valid_snapshot(button_count=2)])
    engine, _ = make_engine(tmp_path, duplicate_buttons)
    assert engine.check(datetime(2026, 9, 15, 17, 5)).status == "blocked"
    assert duplicate_buttons.click_calls == 0


def test_page_date_and_control_identities_are_required(tmp_path: object) -> None:
    for unsafe_snapshot in (
        valid_snapshot(page_date=datetime(2026, 9, 14).date()),
        valid_snapshot(container_id=""),
        valid_snapshot(button_id=""),
    ):
        adapter = FakeAdapter([unsafe_snapshot])
        engine, _ = make_engine(tmp_path, adapter)
        assert engine.check(datetime(2026, 9, 15, 17, 5)).status == "blocked"
        assert adapter.click_calls == 0


def test_existing_clock_out_record_prevents_click(tmp_path: object) -> None:
    adapter = FakeAdapter([valid_snapshot(already_clocked_out=True)])
    engine, _ = make_engine(tmp_path, adapter, mode="automatic")

    result = engine.check(datetime(2026, 9, 15, 17, 5))

    assert result.status == "already_clocked_out"
    assert adapter.click_calls == 0


def test_weekend_skips_without_reading_page(tmp_path: object) -> None:
    adapter = FakeAdapter([])
    engine, _ = make_engine(tmp_path, adapter, mode="automatic")

    result = engine.check(datetime(2026, 9, 19, 17, 5))

    assert result.status == "skipped"
    assert adapter.snapshot_calls == 0


def test_configured_weekdays_are_used(tmp_path: object) -> None:
    adapter = FakeAdapter([])
    engine, _ = make_engine(tmp_path, adapter, weekdays=frozenset({0, 2, 4}))

    result = engine.check(datetime(2026, 9, 15, 17, 5))

    assert result.status == "skipped"
    assert adapter.snapshot_calls == 0


def test_changed_second_snapshot_blocks_click(tmp_path: object) -> None:
    adapter = FakeAdapter(
        [valid_snapshot(), valid_snapshot(signature="changed-page")]
    )
    engine, store = make_engine(tmp_path, adapter, mode="automatic")

    result = engine.check(datetime(2026, 9, 15, 17, 5))

    assert result.status == "blocked"
    assert adapter.click_calls == 0
    assert store.load(datetime(2026, 9, 15).date()) is None


def test_automatic_mode_persists_attempt_before_click(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot], now=now)
    engine, store = make_engine(tmp_path, adapter, mode="automatic")

    result = engine.check(now)
    state = store.load(now.date())

    assert result.status == "success"
    assert adapter.click_calls == 1
    assert adapter.attempt_was_persisted_before_click
    assert adapter.clicked_signature == snapshot.signature
    assert state is not None
    assert state.clock_out_attempted
    assert state.clock_out_success
    assert state.outcome == "success"


def test_unknown_result_is_never_retried_that_day(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot], verify_result=False, now=now)
    engine, store = make_engine(tmp_path, adapter, mode="automatic")

    first_result = engine.check(now)
    second_result = engine.check(now.replace(hour=18))
    state = store.load(now.date())

    assert first_result.status == "unknown"
    assert second_result.status == "unknown"
    assert adapter.click_calls == 1
    assert adapter.snapshot_calls == 2
    assert state is not None
    assert state.clock_out_attempted
    assert not state.clock_out_success
    assert state.outcome == "unknown"


def test_click_exception_is_never_retried_that_day(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot], click_error=True, now=now)
    engine, _ = make_engine(tmp_path, adapter, mode="automatic")

    assert engine.check(now).status == "unknown"
    assert engine.check(now.replace(hour=18)).status == "unknown"
    assert adapter.click_calls == 1


def test_preclick_exception_records_distinct_outcome(tmp_path: object) -> None:
    class PreclickError(RuntimeError):
        invocation_started = False

    now = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot], now=now)

    def fail_before_click(expected_signature: str) -> None:
        adapter.click_calls += 1
        raise PreclickError("foreground changed")

    adapter.click_clock_out = fail_before_click  # type: ignore[method-assign]
    engine, store = make_engine(tmp_path, adapter, mode="automatic")

    result = engine.check(now)
    state = store.load(now.date())

    assert result.status == "retry_waiting"
    assert "点击前安全检查未通过" in result.message
    assert result.next_retry_time == now + timedelta(minutes=5)
    assert state is not None
    assert state.outcome == "aborted_before_click"


def test_automatic_mode_requires_interactive_session_before_first_snapshot(
    tmp_path: object,
) -> None:
    adapter = FakeAdapter([valid_snapshot()], interactive_results=[False])
    engine, _ = make_engine(tmp_path, adapter, mode="automatic")

    result = engine.check(datetime(2026, 9, 15, 17, 5))

    assert result.status == "blocked"
    assert adapter.snapshot_calls == 0
    assert adapter.click_calls == 0


def test_session_is_checked_again_immediately_before_click(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter(
        [snapshot, snapshot], now=now, interactive_results=[True, False]
    )
    engine, store = make_engine(tmp_path, adapter, mode="automatic")

    result = engine.check(now)

    assert result.status == "blocked"
    assert adapter.interactive_calls == 2
    assert adapter.click_calls == 0
    assert store.load(now.date()) is None


def test_final_time_change_blocks_before_claim_or_click(tmp_path: object) -> None:
    initial = datetime(2026, 9, 15, 17, 5)
    unsafe_final_times = (
        datetime(2026, 9, 16, 0, 0),
        datetime(2026, 9, 15, 17, 4),
        datetime(2026, 9, 15, 23, 31),
    )
    for index, final_time in enumerate(unsafe_final_times):
        snapshot = valid_snapshot()
        adapter = FakeAdapter([snapshot, snapshot], now=initial)
        state_path = Path(str(tmp_path)) / f"state-{index}.json"
        store = JsonStateStore(state_path)
        adapter.store = store
        engine = ClockoutEngine(
            adapter,
            store,
            EngineConfig(mode="automatic", work_duration_minutes=360),
            now_provider=lambda value=final_time: value,
        )

        result = engine.check(initial)

        assert result.status == "blocked"
        assert adapter.click_calls == 0
        assert store.load(initial.date()) is None


def test_time_is_checked_again_after_claim(tmp_path: object) -> None:
    initial = datetime(2026, 9, 15, 17, 5)
    final_times = iter((initial, datetime(2026, 9, 15, 16, 55)))
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot], now=initial)
    engine, store = make_engine(
        tmp_path,
        adapter,
        mode="automatic",
        now_provider=lambda: next(final_times),
    )

    result = engine.check(initial)
    state = store.load(initial.date())

    assert result.status == "retry_waiting"
    assert adapter.click_calls == 0
    assert adapter.cancel_calls == 1
    assert state is not None
    assert state.outcome == "aborted_before_click"


def test_session_is_checked_again_after_claim(tmp_path: object) -> None:
    initial = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter(
        [snapshot, snapshot],
        now=initial,
        interactive_results=[True, True, False],
    )
    engine, store = make_engine(tmp_path, adapter, mode="automatic")

    result = engine.check(initial)
    state = store.load(initial.date())

    assert result.status == "retry_waiting"
    assert adapter.click_calls == 0
    assert adapter.cancel_calls == 1
    assert state is not None
    assert state.outcome == "aborted_before_click"


def test_checkout_cancels_prepared_target_when_claim_fails(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot], now=now)
    engine, store = make_engine(tmp_path, adapter, mode="automatic")
    store.claim_action = lambda *args, **kwargs: None  # type: ignore[method-assign]

    result = engine.check(PunchAction.CHECK_OUT, now)

    assert result.status == "already_attempted"
    assert adapter.cancel_calls == 1
    assert adapter.click_calls == 0


def test_checkout_claim_and_cancel_failure_returns_blocked(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot], now=now)
    engine, store = make_engine(tmp_path, adapter, mode="automatic")
    store.claim_action = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fail_cancel(token: object) -> None:
        raise RuntimeError("simulated cancel failure")

    adapter.cancel_click = fail_cancel  # type: ignore[method-assign]

    result = engine.check(PunchAction.CHECK_OUT, now)

    assert result.status == "blocked"
    assert "未能释放" in result.message
    assert adapter.click_calls == 0


def test_checkout_success_is_unknown_when_outcome_cannot_be_saved(
    tmp_path: object,
) -> None:
    now = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot], now=now)
    engine, store = make_engine(tmp_path, adapter, mode="automatic")

    def fail_record(*args: object, **kwargs: object) -> None:
        raise OSError("simulated state update failure")

    store.record_action_outcome = fail_record  # type: ignore[method-assign]

    result = engine.check(PunchAction.CHECK_OUT, now)

    assert result.status == "unknown"
    assert "状态保存失败" in result.message
    assert adapter.click_calls == 1


def test_checkout_failed_retry_state_is_not_reported_as_waiting(
    tmp_path: object,
) -> None:
    initial = datetime(2026, 9, 15, 17, 5)
    final_times = iter((initial, datetime(2026, 9, 15, 16, 55)))
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot], now=initial)
    engine, store = make_engine(
        tmp_path,
        adapter,
        mode="automatic",
        now_provider=lambda: next(final_times),
    )

    def fail_record(*args: object, **kwargs: object) -> None:
        raise OSError("simulated retry state failure")

    store.record_action_outcome = fail_record  # type: ignore[method-assign]

    result = engine.check(PunchAction.CHECK_OUT, initial)

    assert result.status == "blocked"
    assert result.next_retry_time is None
    assert adapter.cancel_calls == 1
    assert adapter.click_calls == 0


def test_preclick_failure_retries_after_delay_and_then_succeeds(tmp_path: object) -> None:
    class PreclickError(RuntimeError):
        invocation_started = False

    now = datetime(2026, 9, 15, 17, 5)
    snapshot = valid_snapshot()
    adapter = FakeAdapter([snapshot, snapshot, snapshot, snapshot], now=now)
    original_click = adapter.click_clock_out
    calls = 0

    def fail_once(expected_signature: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            adapter.click_calls += 1
            raise PreclickError("foreground changed")
        original_click(expected_signature)

    adapter.click_clock_out = fail_once  # type: ignore[method-assign]
    engine, store = make_engine(tmp_path, adapter, mode="automatic")

    first = engine.check(now)
    early = engine.check(now + timedelta(minutes=4))
    adapter.now = now + timedelta(minutes=5)
    second = engine.check(now + timedelta(minutes=5))
    state = store.load(now.date())

    assert first.status == "retry_waiting"
    assert early.status == "retry_waiting"
    assert second.status == "success"
    assert adapter.click_calls == 2
    assert state is not None
    assert state.attempt_count == 2
    assert state.physical_click_count == 1


def test_uncertain_result_is_only_verified_and_never_clicked_twice(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 17, 5)
    before = valid_snapshot()
    after = valid_snapshot(
        already_clocked_out=True,
        button_count=0,
        button_enabled=False,
        button_id="",
        signature="confirmed",
    )
    adapter = FakeAdapter([before, before, after], verify_result=False, now=now)
    engine, store = make_engine(tmp_path, adapter, mode="automatic")

    first = engine.check(now)
    verified = engine.check(now + timedelta(minutes=5))
    state = store.load(now.date())

    assert first.status == "unknown"
    assert verified.status == "already_clocked_out"
    assert adapter.click_calls == 1
    assert state is not None and state.clock_out_success


def test_corrupt_state_blocks_without_reading_page(tmp_path: object) -> None:
    state_path = Path(str(tmp_path)) / "state.json"
    state_path.write_text("{not-json", encoding="utf-8")
    store = JsonStateStore(state_path)
    adapter = FakeAdapter([valid_snapshot()])
    engine = ClockoutEngine(adapter, store)

    result = engine.check(datetime(2026, 9, 15, 17, 5))

    assert result.status == "blocked"
    assert adapter.snapshot_calls == 0
    assert adapter.click_calls == 0


def test_claim_is_atomic_across_store_instances(tmp_path: object) -> None:
    state_path = Path(str(tmp_path)) / "state.json"
    day = datetime(2026, 9, 15).date()
    barrier = Barrier(4)

    def compete() -> bool:
        store = JsonStateStore(state_path)
        barrier.wait()
        return store.claim_attempt(
            day,
            check_in_time="09:00",
            eligible_time=datetime(2026, 9, 15, 17, 5),
            attempted_at=datetime(2026, 9, 15, 17, 5),
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(lambda _: compete(), range(4)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 3


def valid_checkin_snapshot(**changes: object) -> AttendanceSnapshot:
    values: dict[str, object] = {
        "page_date": datetime(2026, 9, 15).date(),
        "check_in_time": None,
        "already_clocked_out": False,
        "button_count": 1,
        "button_enabled": True,
        "blocking_reason": None,
        "signature": "stable-checkin-v1",
        "container_id": "attendance-container",
        "button_id": "clock-in-button",
        "action": PunchAction.CHECK_IN,
        "action_completed": False,
    }
    values.update(changes)
    return AttendanceSnapshot(**values)  # type: ignore[arg-type]


class CheckinFakeAdapter:
    def __init__(
        self,
        snapshots: list[AttendanceSnapshot],
        *,
        interactive_results: list[bool] | None = None,
        click_error: Exception | None = None,
        execute_result: bool = True,
        store: JsonStateStore | None = None,
        now: datetime | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.interactive_results = interactive_results or [True]
        self.click_error = click_error
        self.execute_result = execute_result
        self.store = store
        self.now = now
        self.snapshot_calls = 0
        self.click_calls = 0
        self.interactive_calls = 0
        self.cancel_calls = 0
        self.prepared_token: tuple[PunchAction, str] | None = None
        self.clicked_action: PunchAction | None = None
        self.clicked_signature = ""
        self.attempt_was_persisted_before_click = False

    def snapshot_for_action(
        self, day: object, action: PunchAction
    ) -> AttendanceSnapshot:
        snapshot = self.snapshots[self.snapshot_calls]
        self.snapshot_calls += 1
        return snapshot

    def prepare_click(
        self, day: object, action: PunchAction, expected_signature: str
    ) -> object:
        self.prepared_token = (action, expected_signature)
        return self.prepared_token

    def cancel_click(self, token: object) -> None:
        assert token == self.prepared_token
        self.cancel_calls += 1
        self.prepared_token = None

    def execute_click(self, token: object) -> bool:
        assert token == self.prepared_token
        action, expected_signature = token  # type: ignore[misc]
        self.click_calls += 1
        self.clicked_action = action
        self.clicked_signature = expected_signature
        if self.store is not None and self.now is not None:
            state = self.store.load(self.now.date())
            checkin = state.action(PunchAction.CHECK_IN) if state else None
            self.attempt_was_persisted_before_click = bool(
                checkin and checkin.attempted
            )
        if self.click_error is not None:
            raise self.click_error
        self.prepared_token = None
        return self.execute_result

    def is_session_interactive(self) -> bool:
        index = min(self.interactive_calls, len(self.interactive_results) - 1)
        self.interactive_calls += 1
        return self.interactive_results[index]


def make_checkin_engine(
    tmp_path: object,
    adapter: CheckinFakeAdapter,
    *,
    mode: str = "automatic",
    now_provider: Callable[[], datetime] | None = None,
) -> tuple[ClockoutEngine, JsonStateStore]:
    store = JsonStateStore(tmp_path / "checkin-state.json")  # type: ignore[operator]
    adapter.store = store
    now = adapter.now or datetime(2026, 9, 15, 8, 50)
    config = EngineConfig(mode=mode, auto_check_in_enabled=True)
    return (
        ClockoutEngine(adapter, store, config, now_provider=now_provider or (lambda: now)),
        store,
    )


def test_checkin_is_disabled_by_default_without_reading_page(tmp_path: object) -> None:
    adapter = CheckinFakeAdapter([])
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    engine = ClockoutEngine(adapter, store, EngineConfig(mode="automatic"))

    result = engine.check(PunchAction.CHECK_IN, datetime(2026, 9, 15, 8, 50))

    assert result.status == "skipped"
    assert adapter.snapshot_calls == 0
    assert adapter.click_calls == 0


def test_checkin_success_uses_two_matching_snapshots_and_action_claim(
    tmp_path: object,
) -> None:
    now = datetime(2026, 9, 15, 8, 50)
    pending = valid_checkin_snapshot()
    adapter = CheckinFakeAdapter([pending, pending], now=now)
    engine, store = make_checkin_engine(tmp_path, adapter)

    result = engine.check(PunchAction.CHECK_IN, now)
    state = store.load(now.date())
    checkin = state.action(PunchAction.CHECK_IN) if state else None

    assert result.status == "success"
    assert adapter.snapshot_calls == 2
    assert adapter.click_calls == 1
    assert adapter.clicked_action is PunchAction.CHECK_IN
    assert adapter.clicked_signature == pending.signature
    assert adapter.attempt_was_persisted_before_click
    assert checkin is not None and checkin.success
    assert checkin.physical_click_count == 1


def test_checkin_rejects_snapshot_for_the_wrong_action(tmp_path: object) -> None:
    adapter = CheckinFakeAdapter([valid_snapshot()])
    engine, store = make_checkin_engine(tmp_path, adapter)

    result = engine.check(PunchAction.CHECK_IN, datetime(2026, 9, 15, 8, 50))

    assert result.status == "blocked"
    assert "动作不匹配" in result.message
    assert adapter.click_calls == 0
    assert store.load(datetime(2026, 9, 15).date()) is None


def test_existing_checkin_record_is_detected_without_click(tmp_path: object) -> None:
    completed = valid_checkin_snapshot(
        check_in_time=time(8, 48),
        button_count=0,
        button_enabled=False,
        button_id="",
        action_completed=True,
    )
    adapter = CheckinFakeAdapter([completed])
    engine, _ = make_checkin_engine(tmp_path, adapter)

    result = engine.check(PunchAction.CHECK_IN, datetime(2026, 9, 15, 8, 50))

    assert result.status == "already_clocked_in"
    assert result.check_in_time == time(8, 48)
    assert adapter.click_calls == 0


def test_checkin_unknown_result_is_only_verified_and_never_reclicked(
    tmp_path: object,
) -> None:
    now = datetime(2026, 9, 15, 8, 50)
    pending = valid_checkin_snapshot()
    adapter = CheckinFakeAdapter(
        [pending, pending, pending],
        now=now,
        execute_result=False,
    )
    engine, store = make_checkin_engine(tmp_path, adapter)

    first = engine.check(PunchAction.CHECK_IN, now)
    second = engine.check(PunchAction.CHECK_IN, now + timedelta(minutes=5))
    state = store.load(now.date())
    checkin = state.action(PunchAction.CHECK_IN) if state else None

    assert first.status == "unknown"
    assert second.status == "unknown"
    assert adapter.click_calls == 1
    assert adapter.snapshot_calls == 3
    assert checkin is not None and checkin.outcome == "unknown"
    assert checkin.physical_click_count == 1


def test_checkin_cancels_prepared_target_when_claim_fails(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 8, 50)
    pending = valid_checkin_snapshot()
    adapter = CheckinFakeAdapter([pending, pending], now=now)
    engine, store = make_checkin_engine(tmp_path, adapter)
    store.claim_action = lambda *args, **kwargs: None  # type: ignore[method-assign]

    result = engine.check(PunchAction.CHECK_IN, now)

    assert result.status == "already_attempted"
    assert adapter.cancel_calls == 1
    assert adapter.click_calls == 0


def test_checkin_claim_and_cancel_failure_returns_blocked(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 8, 50)
    pending = valid_checkin_snapshot()
    adapter = CheckinFakeAdapter([pending, pending], now=now)
    engine, store = make_checkin_engine(tmp_path, adapter)
    store.claim_action = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fail_cancel(token: object) -> None:
        raise RuntimeError("simulated cancel failure")

    adapter.cancel_click = fail_cancel  # type: ignore[method-assign]

    result = engine.check(PunchAction.CHECK_IN, now)

    assert result.status == "blocked"
    assert "未能释放" in result.message
    assert adapter.click_calls == 0


def test_checkin_success_is_unknown_when_outcome_cannot_be_saved(
    tmp_path: object,
) -> None:
    now = datetime(2026, 9, 15, 8, 50)
    pending = valid_checkin_snapshot()
    adapter = CheckinFakeAdapter([pending, pending], now=now)
    engine, store = make_checkin_engine(tmp_path, adapter)

    def fail_record(*args: object, **kwargs: object) -> None:
        raise OSError("simulated state update failure")

    store.record_action_outcome = fail_record  # type: ignore[method-assign]

    result = engine.check(PunchAction.CHECK_IN, now)

    assert result.status == "unknown"
    assert "状态保存失败" in result.message
    assert adapter.click_calls == 1


def test_checkin_failed_retry_state_is_not_reported_as_waiting(
    tmp_path: object,
) -> None:
    initial = datetime(2026, 9, 15, 8, 50)
    final_times = iter((initial, datetime(2026, 9, 15, 8, 40)))
    pending = valid_checkin_snapshot()
    adapter = CheckinFakeAdapter([pending, pending], now=initial)
    engine, store = make_checkin_engine(
        tmp_path,
        adapter,
        now_provider=lambda: next(final_times),
    )

    def fail_record(*args: object, **kwargs: object) -> None:
        raise OSError("simulated retry state failure")

    store.record_action_outcome = fail_record  # type: ignore[method-assign]

    result = engine.check(PunchAction.CHECK_IN, initial)

    assert result.status == "blocked"
    assert result.next_retry_time is None
    assert adapter.cancel_calls == 1
    assert adapter.click_calls == 0


def test_checkin_preclick_failure_has_limited_retry(tmp_path: object) -> None:
    class PreclickError(RuntimeError):
        invocation_started = False

    now = datetime(2026, 9, 15, 8, 50)
    pending = valid_checkin_snapshot()
    adapter = CheckinFakeAdapter(
        [pending, pending],
        now=now,
        click_error=PreclickError("target changed"),
    )
    engine, store = make_checkin_engine(tmp_path, adapter)

    first = engine.check(PunchAction.CHECK_IN, now)
    early = engine.check(PunchAction.CHECK_IN, now + timedelta(minutes=4))
    state = store.load(now.date())
    checkin = state.action(PunchAction.CHECK_IN) if state else None

    assert first.status == "retry_waiting"
    assert first.next_retry_time == now + timedelta(minutes=5)
    assert early.status == "retry_waiting"
    assert adapter.click_calls == 1
    assert checkin is not None and checkin.outcome == "aborted_before_click"
    assert checkin.physical_click_count == 0


def test_checkin_unknown_state_does_not_block_checkout(tmp_path: object) -> None:
    day = datetime(2026, 9, 15).date()
    state_path = tmp_path / "shared-state.json"  # type: ignore[operator]
    store = JsonStateStore(state_path)
    claim = store.claim_action(
        day,
        PunchAction.CHECK_IN,
        check_in_time="",
        eligible_time=datetime(2026, 9, 15, 7, 0),
        attempted_at=datetime(2026, 9, 15, 8, 50),
    )
    assert claim
    store.record_action_outcome(
        day,
        PunchAction.CHECK_IN,
        claim,
        "unknown",
        success=False,
        invocation_started=True,
    )
    checkout_snapshot = valid_snapshot()
    checkout_adapter = FakeAdapter(
        [checkout_snapshot, checkout_snapshot],
        now=datetime(2026, 9, 15, 17, 5),
    )
    checkout_adapter.store = store
    engine = ClockoutEngine(
        checkout_adapter,
        store,
        EngineConfig(mode="automatic", work_duration_minutes=360),
        now_provider=lambda: datetime(2026, 9, 15, 17, 5),
    )

    result = engine.check(PunchAction.CHECK_OUT, datetime(2026, 9, 15, 17, 5))
    state = store.load(day)

    assert result.status == "success"
    assert checkout_adapter.click_calls == 1
    assert state is not None
    assert state.action(PunchAction.CHECK_IN).outcome == "unknown"  # type: ignore[union-attr]
    assert state.action(PunchAction.CHECK_OUT).success  # type: ignore[union-attr]


def test_checkout_unknown_state_does_not_block_checkin(tmp_path: object) -> None:
    now = datetime(2026, 9, 15, 8, 50)
    adapter = CheckinFakeAdapter(
        [
            valid_checkin_snapshot(),
            valid_checkin_snapshot(),
            valid_checkin_snapshot(
                check_in_time=time(8, 50),
                action_completed=True,
            ),
        ],
        now=now,
    )
    engine, store = make_checkin_engine(tmp_path, adapter)
    checkout_claim = store.claim_action(
        now.date(),
        PunchAction.CHECK_OUT,
        check_in_time="08:50",
        eligible_time=datetime(2026, 9, 15, 17, 5),
        attempted_at=datetime(2026, 9, 15, 17, 5),
    )
    assert checkout_claim
    store.record_action_outcome(
        now.date(),
        PunchAction.CHECK_OUT,
        checkout_claim,
        "unknown",
        success=False,
        invocation_started=True,
    )

    result = engine.check(PunchAction.CHECK_IN, now)
    state = store.load(now.date())

    assert result.status == "success"
    assert adapter.click_calls == 1
    assert state is not None
    assert state.action(PunchAction.CHECK_IN).success  # type: ignore[union-attr]
    assert state.action(PunchAction.CHECK_OUT).outcome == "unknown"  # type: ignore[union-attr]
