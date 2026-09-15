from __future__ import annotations

import multiprocessing
from datetime import date, datetime

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
