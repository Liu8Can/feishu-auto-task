from __future__ import annotations

import json
import os
import tempfile
import threading
import time as time_module
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .core import PunchAction

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StateStoreError(RuntimeError):
    pass


class StateCorruptionError(StateStoreError):
    pass


_ATTEMPT_OUTCOMES = {
    "attempted",
    "aborted_before_click",
    "click_failed",
    "unknown",
    "success",
}
_RESETTABLE_OUTCOMES = {"aborted_before_click", "click_failed", "unknown"}
_OUTCOME_TRANSITIONS = {
    "attempted": {"aborted_before_click", "click_failed", "unknown", "success"},
    "click_failed": {"success"},
    "unknown": {"success"},
}
_PUNCH_STATE_FIELDS = {
    "check_in_time",
    "eligible_time",
    "attempted",
    "success",
    "attempted_at",
    "outcome",
    "attempt_count",
    "physical_click_count",
    "next_retry_at",
    "reset_count",
    "history",
    "claim_token",
}


@dataclass(frozen=True, slots=True)
class PunchState:
    check_in_time: str
    eligible_time: str
    attempted: bool
    success: bool
    attempted_at: str
    outcome: str
    attempt_count: int = 1
    physical_click_count: int = 0
    next_retry_at: str = ""
    reset_count: int = 0
    history: tuple[dict[str, object], ...] = ()
    claim_token: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> PunchState:
        if set(value) != _PUNCH_STATE_FIELDS:
            raise ValueError("动作状态字段不完整或包含未知字段")
        check_in_time = value["check_in_time"]
        eligible_time = value["eligible_time"]
        attempted = value["attempted"]
        success = value["success"]
        attempted_at = value["attempted_at"]
        outcome = value["outcome"]
        attempt_count = value["attempt_count"]
        physical_click_count = value["physical_click_count"]
        next_retry_at = value["next_retry_at"]
        reset_count = value["reset_count"]
        raw_history = value["history"]
        claim_token = value["claim_token"]
        if not all(
            isinstance(item, str)
            for item in (
                check_in_time,
                eligible_time,
                attempted_at,
                outcome,
                next_retry_at,
                claim_token,
            )
        ) or not isinstance(attempted, bool) or not isinstance(success, bool):
            raise ValueError("动作状态字段类型无效")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 1
            or isinstance(physical_click_count, bool)
            or not isinstance(physical_click_count, int)
            or physical_click_count < 0
            or isinstance(reset_count, bool)
            or not isinstance(reset_count, int)
            or reset_count < 0
            or not isinstance(raw_history, list)
            or any(not isinstance(item, dict) for item in raw_history)
        ):
            raise ValueError("动作状态扩展字段无效")
        if physical_click_count > attempt_count:
            raise ValueError("物理点击次数不能超过尝试次数")
        if outcome not in _ATTEMPT_OUTCOMES | {"manual_reset"}:
            raise ValueError("动作结果无效")
        if check_in_time:
            datetime.strptime(check_in_time, "%H:%M")
        datetime.fromisoformat(eligible_time)
        datetime.fromisoformat(attempted_at)
        if next_retry_at:
            datetime.fromisoformat(next_retry_at)
        valid_manual_reset = (
            outcome == "manual_reset"
            and not attempted
            and not claim_token
            and reset_count > 0
            and bool(raw_history)
        )
        if outcome == "manual_reset" and not valid_manual_reset:
            raise ValueError("人工重置状态缺少审计记录")
        if attempted and outcome == "manual_reset":
            raise ValueError("人工重置状态不能标记为正在尝试")
        if not attempted and outcome != "manual_reset":
            raise ValueError("未尝试的动作只能处于人工重置状态")
        requires_attempt = not valid_manual_reset and (
            success
            or physical_click_count > 0
            or outcome in _ATTEMPT_OUTCOMES
        )
        if requires_attempt and not attempted:
            raise ValueError("动作结果与尝试状态不一致")
        if attempted and not claim_token:
            raise ValueError("已尝试的动作缺少声明令牌")
        if not attempted and claim_token:
            raise ValueError("未尝试的动作不能保留声明令牌")
        if success != (outcome == "success"):
            raise ValueError("成功状态与动作结果不一致")
        return cls(
            check_in_time=check_in_time,
            eligible_time=eligible_time,
            attempted=attempted,
            success=success,
            attempted_at=attempted_at,
            outcome=outcome,
            attempt_count=attempt_count,
            physical_click_count=physical_click_count,
            next_retry_at=next_retry_at,
            reset_count=reset_count,
            history=tuple(raw_history),
            claim_token=claim_token,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "check_in_time": self.check_in_time,
            "eligible_time": self.eligible_time,
            "attempted": self.attempted,
            "success": self.success,
            "attempted_at": self.attempted_at,
            "outcome": self.outcome,
            "attempt_count": self.attempt_count,
            "physical_click_count": self.physical_click_count,
            "next_retry_at": self.next_retry_at,
            "reset_count": self.reset_count,
            "history": list(self.history),
            "claim_token": self.claim_token,
        }


@dataclass(frozen=True, slots=True)
class DailyState:
    schema_version: int
    date: str
    actions: dict[PunchAction, PunchState]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> DailyState:
        schema_version = value.get("schema_version")
        if type(schema_version) is not int or schema_version != 2:
            raise ValueError("状态版本无效")
        state_date = value["date"]
        raw_actions = value["actions"]
        if not isinstance(state_date, str) or not isinstance(raw_actions, dict):
            raise ValueError("状态字段类型无效")
        date.fromisoformat(state_date)
        actions: dict[PunchAction, PunchState] = {}
        for raw_action, raw_state in raw_actions.items():
            action = PunchAction(raw_action)
            if not isinstance(raw_state, dict):
                raise ValueError("动作状态必须是对象")
            actions[action] = PunchState.from_dict(raw_state)
        return cls(schema_version=2, date=state_date, actions=actions)

    @classmethod
    def migrate_v1(cls, value: dict[str, object]) -> DailyState:
        attempted = value["clock_out_attempted"]
        success = value.get("clock_out_success", False)
        outcome = value.get("outcome", "unknown")
        if not isinstance(attempted, bool):
            raise ValueError("旧版状态字段类型无效")
        if success or outcome in {"attempted", "click_failed", "unknown", "success"}:
            attempted = True
        action_value = {
            "check_in_time": value["check_in_time"],
            "eligible_time": value["eligible_time"],
            "attempted": attempted,
            "success": success,
            "attempted_at": value["attempted_at"],
            "outcome": outcome,
            "attempt_count": value.get("attempt_count", 1 if attempted else 0),
            "physical_click_count": value.get(
                "physical_click_count",
                1 if success or outcome in {"click_failed", "unknown"} else 0,
            ),
            "next_retry_at": value.get("next_retry_at", ""),
            "reset_count": value.get("reset_count", 0),
            "history": value.get("history", []),
            "claim_token": uuid.uuid4().hex if attempted else "",
        }
        migrated = {
            "schema_version": 2,
            "date": value["date"],
            "actions": {PunchAction.CHECK_OUT.value: action_value},
        }
        return cls.from_dict(migrated)

    def action(self, action: PunchAction) -> PunchState | None:
        return self.actions.get(PunchAction(action))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "date": self.date,
            "actions": {
                action.value: state.to_dict() for action, state in self.actions.items()
            },
        }

    def _checkout_value(self, name: str, default: object) -> object:
        checkout = self.action(PunchAction.CHECK_OUT)
        return getattr(checkout, name) if checkout is not None else default

    @property
    def check_in_time(self) -> str:
        return str(self._checkout_value("check_in_time", ""))

    @property
    def eligible_time(self) -> str:
        return str(self._checkout_value("eligible_time", ""))

    @property
    def clock_out_attempted(self) -> bool:
        return bool(self._checkout_value("attempted", False))

    @property
    def clock_out_success(self) -> bool:
        return bool(self._checkout_value("success", False))

    @property
    def attempted_at(self) -> str:
        return str(self._checkout_value("attempted_at", ""))

    @property
    def outcome(self) -> str:
        return str(self._checkout_value("outcome", ""))

    @property
    def attempt_count(self) -> int:
        return int(self._checkout_value("attempt_count", 0))

    @property
    def physical_click_count(self) -> int:
        return int(self._checkout_value("physical_click_count", 0))

    @property
    def next_retry_at(self) -> str:
        return str(self._checkout_value("next_retry_at", ""))

    @property
    def reset_count(self) -> int:
        return int(self._checkout_value("reset_count", 0))

    @property
    def history(self) -> tuple[dict[str, object], ...]:
        return self._checkout_value("history", ())  # type: ignore[return-value]


class JsonStateStore:
    """Persist independent daily punch actions with atomic file replacement."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._thread_lock = threading.Lock()
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")

    def load(self, day: date) -> DailyState | None:
        with self._exclusive_lock():
            return self._load_unlocked(day)

    def claim_action(
        self,
        day: date,
        action: PunchAction,
        *,
        check_in_time: str,
        eligible_time: datetime,
        attempted_at: datetime,
        max_attempts: int = 1,
    ) -> str | None:
        action = PunchAction(action)
        with self._exclusive_lock():
            stored = self._read_unlocked()
            stored_day = date.fromisoformat(stored.date) if stored is not None else None
            if stored_day is not None and day < stored_day:
                return None
            existing = stored if stored_day == day else None
            current = existing.action(action) if existing is not None else None
            if current is not None:
                retryable = (
                    current.outcome in {"aborted_before_click", "manual_reset"}
                    and current.attempt_count < max_attempts
                    and (
                        not current.next_retry_at
                        or attempted_at >= datetime.fromisoformat(current.next_retry_at)
                    )
                )
                if not retryable:
                    return None
                attempt_count = current.attempt_count + 1
                reset_count = current.reset_count
                history = current.history
                physical_click_count = current.physical_click_count
            else:
                attempt_count = 1
                reset_count = 0
                history = ()
                physical_click_count = 0
            token = uuid.uuid4().hex
            punch_state = PunchState(
                check_in_time=check_in_time,
                eligible_time=eligible_time.isoformat(timespec="minutes"),
                attempted=True,
                success=False,
                attempted_at=attempted_at.isoformat(timespec="seconds"),
                outcome="attempted",
                attempt_count=attempt_count,
                physical_click_count=physical_click_count,
                reset_count=reset_count,
                history=history,
                claim_token=token,
            )
            actions = dict(existing.actions) if existing is not None else {}
            actions[action] = punch_state
            self._write_unlocked(DailyState(2, day.isoformat(), actions))
            return token

    def claim_attempt(
        self,
        day: date,
        *,
        check_in_time: str,
        eligible_time: datetime,
        attempted_at: datetime,
        max_attempts: int = 1,
    ) -> bool:
        return bool(
            self.claim_action(
                day,
                PunchAction.CHECK_OUT,
                check_in_time=check_in_time,
                eligible_time=eligible_time,
                attempted_at=attempted_at,
                max_attempts=max_attempts,
            )
        )

    def record_action_outcome(
        self,
        day: date,
        action: PunchAction,
        claim_token: str,
        outcome: str,
        *,
        success: bool,
        invocation_started: bool = False,
        next_retry_at: datetime | None = None,
    ) -> None:
        action = PunchAction(action)
        with self._exclusive_lock():
            self._record_action_outcome_unlocked(
                day,
                action,
                claim_token,
                outcome,
                success=success,
                invocation_started=invocation_started,
                next_retry_at=next_retry_at,
            )

    def _record_action_outcome_unlocked(
        self,
        day: date,
        action: PunchAction,
        claim_token: str,
        outcome: str,
        *,
        success: bool,
        invocation_started: bool = False,
        next_retry_at: datetime | None = None,
    ) -> None:
        current_state = self._load_unlocked(day)
        current = current_state.action(action) if current_state is not None else None
        if current is None or not current.attempted:
            raise StateStoreError("没有可更新的打卡尝试记录")
        if not claim_token or current.claim_token != claim_token:
            raise StateStoreError("打卡声明令牌与动作不匹配")
        if outcome not in _OUTCOME_TRANSITIONS.get(current.outcome, set()):
            raise StateStoreError("打卡结果状态迁移无效")
        if success != (outcome == "success"):
            raise StateStoreError("成功状态与打卡结果不一致")
        if (
            invocation_started
            and current.physical_click_count >= current.attempt_count
        ):
            raise StateStoreError("本次打卡已经记录过物理点击")
        updated = replace(
            current,
            success=success,
            outcome=outcome,
            physical_click_count=current.physical_click_count
            + (1 if invocation_started else 0),
            next_retry_at=(
                next_retry_at.isoformat(timespec="seconds")
                if next_retry_at is not None
                else ""
            ),
        )
        actions = dict(current_state.actions)
        actions[action] = updated
        self._write_unlocked(DailyState(2, current_state.date, actions))

    def reset_action_for_retry(
        self,
        day: date,
        action: PunchAction,
        *,
        reset_at: datetime,
        reason: str,
    ) -> DailyState:
        action = PunchAction(action)
        cleaned_reason = " ".join(reason.split())
        if not cleaned_reason:
            raise ValueError("必须填写重置原因")
        with self._exclusive_lock():
            current_state = self._load_unlocked(day)
            current = current_state.action(action) if current_state is not None else None
            if current is None:
                raise StateStoreError("今天没有可重置的失败记录")
            if current.success or current.outcome == "success":
                raise StateStoreError("今天已经打卡成功，不能重置")
            if current.outcome not in _RESETTABLE_OUTCOMES:
                raise StateStoreError("当前打卡状态仍在进行，不能重置")
            history_item: dict[str, object] = {
                "reset_at": reset_at.isoformat(timespec="seconds"),
                "reason": cleaned_reason,
                "previous_outcome": current.outcome,
                "attempt_count": current.attempt_count,
                "physical_click_count": current.physical_click_count,
            }
            updated = replace(
                current,
                attempted=False,
                success=False,
                outcome="manual_reset",
                next_retry_at="",
                reset_count=current.reset_count + 1,
                history=(*current.history, history_item),
                claim_token="",
            )
            actions = dict(current_state.actions)
            actions[action] = updated
            state = DailyState(2, current_state.date, actions)
            self._write_unlocked(state)
            return state

    def reset_for_retry(
        self,
        day: date,
        *,
        reset_at: datetime,
        reason: str,
    ) -> DailyState:
        return self.reset_action_for_retry(
            day,
            PunchAction.CHECK_OUT,
            reset_at=reset_at,
            reason=reason,
        )

    def _load_unlocked(self, day: date) -> DailyState | None:
        state = self._read_unlocked()
        return state if state is not None and state.date == day.isoformat() else None

    def _read_unlocked(self) -> DailyState | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as state_file:
                raw = json.load(state_file)
            if not isinstance(raw, dict):
                raise ValueError("状态根节点必须是对象")
            schema_version = raw.get("schema_version", 1)
            if type(schema_version) is not int:
                raise ValueError("状态版本无效")
            if schema_version == 1:
                state = DailyState.migrate_v1(raw)
                self._write_unlocked(state)
            else:
                state = DailyState.from_dict(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise StateCorruptionError("每日状态文件损坏或无法读取") from exc
        return state

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
                json.dump(state.to_dict(), state_file, ensure_ascii=False, indent=2)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_name, self.path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
