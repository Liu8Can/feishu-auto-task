from __future__ import annotations

from datetime import datetime, time

from clockout.core import AttendanceSnapshot
from clockout.engine import ClockoutEngine, EngineConfig
from clockout.storage import JsonStateStore


def valid_snapshot(**changes: object) -> AttendanceSnapshot:
    values: dict[str, object] = {
        "check_in_time": time(9, 0),
        "already_clocked_out": False,
        "button_count": 1,
        "button_enabled": True,
        "blocking_reason": None,
        "signature": "stable-page-v1",
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
    ) -> None:
        self.snapshots = snapshots
        self.verify_result = verify_result
        self.click_error = click_error
        self.store = store
        self.now = now
        self.snapshot_calls = 0
        self.click_calls = 0
        self.attempt_was_persisted_before_click = False

    def snapshot(self) -> AttendanceSnapshot:
        snapshot = self.snapshots[self.snapshot_calls]
        self.snapshot_calls += 1
        return snapshot

    def click_clock_out(self) -> None:
        self.click_calls += 1
        if self.store is not None and self.now is not None:
            state = self.store.load(self.now.date())
            self.attempt_was_persisted_before_click = bool(
                state and state.clock_out_attempted
            )
        if self.click_error:
            raise RuntimeError("simulated click failure")

    def verify_success(self) -> bool:
        return self.verify_result


def make_engine(
    tmp_path: object,
    adapter: FakeAdapter,
    *,
    mode: str = "dry_run",
) -> tuple[ClockoutEngine, JsonStateStore]:
    store = JsonStateStore(tmp_path / "state.json")  # type: ignore[operator]
    adapter.store = store
    return ClockoutEngine(adapter, store, EngineConfig(mode=mode)), store


def test_before_dynamic_target_waits_without_clicking(tmp_path: object) -> None:
    adapter = FakeAdapter([valid_snapshot()])
    engine, store = make_engine(tmp_path, adapter)

    result = engine.check(datetime(2026, 9, 15, 17, 4))

    assert result.status == "waiting"
    assert result.eligible_time == datetime(2026, 9, 15, 17, 5)
    assert adapter.click_calls == 0
    assert store.load(datetime(2026, 9, 15).date()) is None


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
    assert second_result.status == "already_attempted"
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
    assert engine.check(now.replace(hour=18)).status == "already_attempted"
    assert adapter.click_calls == 1
