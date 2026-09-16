from __future__ import annotations

import multiprocessing
from datetime import date, datetime, timedelta

import pytest

from clockout.storage import JsonStateStore


def _claim_worker(path: str, start, results) -> None:
    store = JsonStateStore(path)
    start.wait()
    claimed = store.claim_attempt(
        date(2026, 9, 15),
        check_in_time="09:00",
        eligible_time=datetime(2026, 9, 15, 17, 5),
        attempted_at=datetime(2026, 9, 15, 17, 5),
    )
    results.put(claimed)


def test_claim_is_atomic_across_processes(tmp_path: object) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    state_path = str(tmp_path / "state.json")  # type: ignore[operator]
    processes = [
        context.Process(target=_claim_worker, args=(state_path, start, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1


def test_manual_reset_preserves_history_and_never_resets_success(tmp_path: object) -> None:
    path = tmp_path / "state.json"  # type: ignore[operator]
    store = JsonStateStore(path)
    day = date(2026, 9, 15)
    moment = datetime(2026, 9, 15, 17, 5)
    assert store.claim_attempt(
        day,
        check_in_time="09:00",
        eligible_time=moment,
        attempted_at=moment,
    )
    store.record_outcome(day, "click_failed", success=False, invocation_started=True)

    reset = store.reset_for_retry(
        day,
        reset_at=moment + timedelta(minutes=2),
        reason="已核对飞书没有下班记录",
    )

    assert not reset.clock_out_attempted
    assert reset.reset_count == 1
    assert reset.history[-1]["previous_outcome"] == "click_failed"
    assert store.claim_attempt(
        day,
        check_in_time="09:00",
        eligible_time=moment,
        attempted_at=moment + timedelta(minutes=3),
        max_attempts=3,
    )
    store.record_outcome(day, "success", success=True, invocation_started=True)
    with pytest.raises(Exception, match="不能重置"):
        store.reset_for_retry(
            day,
            reset_at=moment + timedelta(minutes=4),
            reason="不应允许",
        )
