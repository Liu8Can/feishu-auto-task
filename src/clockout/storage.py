from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path


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
        return cls(
            date=str(value["date"]),
            check_in_time=str(value["check_in_time"]),
            eligible_time=str(value["eligible_time"]),
            clock_out_attempted=bool(value["clock_out_attempted"]),
            clock_out_success=bool(value.get("clock_out_success", False)),
            attempted_at=str(value["attempted_at"]),
            outcome=str(value.get("outcome", "unknown")),
        )


class JsonStateStore:
    """Persist the one-attempt-per-day guard with atomic file replacement."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self, day: date) -> DailyState | None:
        with self._lock:
            return self._load_unlocked(day)

    def claim_attempt(
        self,
        day: date,
        *,
        check_in_time: str,
        eligible_time: datetime,
        attempted_at: datetime,
    ) -> bool:
        with self._lock:
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
        with self._lock:
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
        with self.path.open("r", encoding="utf-8") as state_file:
            raw = json.load(state_file)
        state = DailyState.from_dict(raw)
        return state if state.date == day.isoformat() else None

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
