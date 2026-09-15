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

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "DailyState":
        state_date = value["date"]
        check_in_time = value["check_in_time"]
        eligible_time = value["eligible_time"]
        attempted = value["clock_out_attempted"]
        success = value.get("clock_out_success", False)
        attempted_at = value["attempted_at"]
        outcome = value.get("outcome", "unknown")
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
        date.fromisoformat(state_date)
        datetime.strptime(check_in_time, "%H:%M")
        datetime.fromisoformat(eligible_time)
        datetime.fromisoformat(attempted_at)
        return cls(
            date=state_date,
            check_in_time=check_in_time,
            eligible_time=eligible_time,
            clock_out_attempted=attempted,
            clock_out_success=success,
            attempted_at=attempted_at,
            outcome=outcome,
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
    ) -> bool:
        with self._exclusive_lock():
            existing = self._load_unlocked(day)
            if existing is not None and existing.clock_out_attempted:
                return False
            state = DailyState(
                date=day.isoformat(),
                check_in_time=check_in_time,
                eligible_time=eligible_time.isoformat(timespec="minutes"),
                clock_out_attempted=True,
                clock_out_success=False,
                attempted_at=attempted_at.isoformat(timespec="seconds"),
                outcome="attempted",
            )
            self._write_unlocked(state)
            return True

    def record_outcome(self, day: date, outcome: str, *, success: bool) -> None:
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
                )
            )

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
