from __future__ import annotations

import json
import os
import tempfile
import threading
import time as time_module
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StateStoreError(RuntimeError):
    pass


class StateCorruptionError(StateStoreError):
    pass


@dataclass(frozen=True, slots=True)
class DailyState:
    date: str
    check_in_time: str
    eligible_time: str
    clock_out_attempted: bool
    clock_out_success: bool
    attempted_at: str
    outcome: str
    attempt_count: int = 1
    physical_click_count: int = 0
    next_retry_at: str = ""
    reset_count: int = 0
    history: tuple[dict[str, object], ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "DailyState":
        state_date = value["date"]
        check_in_time = value["check_in_time"]
        eligible_time = value["eligible_time"]
        attempted = value["clock_out_attempted"]
        success = value.get("clock_out_success", False)
        attempted_at = value["attempted_at"]
        outcome = value.get("outcome", "unknown")
        attempt_count = value.get("attempt_count", 1 if attempted else 0)
        physical_click_count = value.get(
            "physical_click_count", 1 if success or outcome in {"click_failed", "unknown"} else 0
        )
        next_retry_at = value.get("next_retry_at", "")
        reset_count = value.get("reset_count", 0)
        raw_history = value.get("history", [])
        if not all(
            isinstance(item, str)
            for item in (
                state_date,
                check_in_time,
                eligible_time,
                attempted_at,
                outcome,
            )
        ) or not isinstance(attempted, bool) or not isinstance(success, bool):
            raise ValueError("状态字段类型无效")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
            or isinstance(physical_click_count, bool)
            or not isinstance(physical_click_count, int)
            or physical_click_count < 0
            or isinstance(reset_count, bool)
            or not isinstance(reset_count, int)
            or reset_count < 0
            or not isinstance(next_retry_at, str)
            or not isinstance(raw_history, list)
            or any(not isinstance(item, dict) for item in raw_history)
        ):
            raise ValueError("状态扩展字段无效")
        date.fromisoformat(state_date)
        datetime.strptime(check_in_time, "%H:%M")
        datetime.fromisoformat(eligible_time)
        datetime.fromisoformat(attempted_at)
        if next_retry_at:
            datetime.fromisoformat(next_retry_at)
        return cls(
            date=state_date,
            check_in_time=check_in_time,
            eligible_time=eligible_time,
            clock_out_attempted=attempted,
            clock_out_success=success,
            attempted_at=attempted_at,
            outcome=outcome,
            attempt_count=attempt_count,
            physical_click_count=physical_click_count,
            next_retry_at=next_retry_at,
            reset_count=reset_count,
            history=tuple(raw_history),
        )


class JsonStateStore:
    """Persist the one-attempt-per-day guard with atomic file replacement."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._thread_lock = threading.Lock()
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")

    def load(self, day: date) -> DailyState | None:
        with self._exclusive_lock():
            return self._load_unlocked(day)

    def claim_attempt(
        self,
        day: date,
        *,
        check_in_time: str,
        eligible_time: datetime,
        attempted_at: datetime,
        max_attempts: int = 1,
    ) -> bool:
        with self._exclusive_lock():
            existing = self._load_unlocked(day)
            if existing is not None:
                retryable = (
                    existing.outcome in {"aborted_before_click", "manual_reset"}
                    and existing.attempt_count < max_attempts
                    and (
                        not existing.next_retry_at
                        or attempted_at >= datetime.fromisoformat(existing.next_retry_at)
                    )
                )
                if existing.clock_out_attempted and not retryable:
                    return False
                attempt_count = existing.attempt_count + 1
                reset_count = existing.reset_count
                history = existing.history
                physical_click_count = existing.physical_click_count
            else:
                attempt_count = 1
                reset_count = 0
                history = ()
                physical_click_count = 0
            state = DailyState(
                date=day.isoformat(),
                check_in_time=check_in_time,
                eligible_time=eligible_time.isoformat(timespec="minutes"),
                clock_out_attempted=True,
                clock_out_success=False,
                attempted_at=attempted_at.isoformat(timespec="seconds"),
                outcome="attempted",
                attempt_count=attempt_count,
                physical_click_count=physical_click_count,
                reset_count=reset_count,
                history=history,
            )
            self._write_unlocked(state)
            return True

    def record_outcome(
        self,
        day: date,
        outcome: str,
        *,
        success: bool,
        invocation_started: bool = False,
        next_retry_at: datetime | None = None,
    ) -> None:
        with self._exclusive_lock():
            current = self._load_unlocked(day)
            if current is None or not current.clock_out_attempted:
                raise RuntimeError("没有可更新的打卡尝试记录")
            self._write_unlocked(
                DailyState(
                    date=current.date,
                    check_in_time=current.check_in_time,
                    eligible_time=current.eligible_time,
                    clock_out_attempted=True,
                    clock_out_success=success,
                    attempted_at=current.attempted_at,
                    outcome=outcome,
                    attempt_count=current.attempt_count,
                    physical_click_count=current.physical_click_count
                    + (1 if invocation_started else 0),
                    next_retry_at=(
                        next_retry_at.isoformat(timespec="seconds")
                        if next_retry_at is not None
                        else ""
                    ),
                    reset_count=current.reset_count,
                    history=current.history,
                )
            )

    def reset_for_retry(
        self,
        day: date,
        *,
        reset_at: datetime,
        reason: str,
    ) -> DailyState:
        cleaned_reason = " ".join(reason.split())
        if not cleaned_reason:
            raise ValueError("必须填写重置原因")
        with self._exclusive_lock():
            current = self._load_unlocked(day)
            if current is None:
                raise StateStoreError("今天没有可重置的失败记录")
            if current.clock_out_success or current.outcome == "success":
                raise StateStoreError("今天已经打卡成功，不能重置")
            history_item: dict[str, object] = {
                "reset_at": reset_at.isoformat(timespec="seconds"),
                "reason": cleaned_reason,
                "previous_outcome": current.outcome,
                "attempt_count": current.attempt_count,
                "physical_click_count": current.physical_click_count,
            }
            state = DailyState(
                date=current.date,
                check_in_time=current.check_in_time,
                eligible_time=current.eligible_time,
                clock_out_attempted=False,
                clock_out_success=False,
                attempted_at=current.attempted_at,
                outcome="manual_reset",
                attempt_count=current.attempt_count,
                physical_click_count=current.physical_click_count,
                next_retry_at="",
                reset_count=current.reset_count + 1,
                history=(*current.history, history_item),
            )
            self._write_unlocked(state)
            return state

    def _load_unlocked(self, day: date) -> DailyState | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as state_file:
                raw = json.load(state_file)
            if not isinstance(raw, dict):
                raise ValueError("状态根节点必须是对象")
            state = DailyState.from_dict(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise StateCorruptionError("每日状态文件损坏或无法读取") from exc
        return state if state.date == day.isoformat() else None

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_lock_file()
        with self._thread_lock, self._lock_path.open("r+b", buffering=0) as lock_file:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _ensure_lock_file(self) -> None:
        for _ in range(100):
            try:
                descriptor = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    if self._lock_path.stat().st_size >= 1:
                        return
                except FileNotFoundError:
                    pass
                time_module.sleep(0.01)
                continue
            try:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return
        raise StateStoreError("无法初始化每日状态锁")

    def _write_unlocked(self, state: DailyState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(asdict(state), state_file, ensure_ascii=False, indent=2)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_name, self.path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
