from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Protocol

from .core import (
    AttendanceSnapshot,
    CheckResult,
    calculate_eligible_time,
    is_within_window,
    is_workday,
    snapshots_match,
)
from .storage import JsonStateStore


class AttendanceAdapter(Protocol):
    def snapshot(self) -> AttendanceSnapshot: ...

    def click_clock_out(self) -> None: ...

    def verify_success(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class EngineConfig:
    mode: str = "dry_run"
    work_duration_minutes: int = 480
    safety_buffer_minutes: int = 5
    check_start_time: time = time(15, 0)
    check_end_time: time = time(23, 30)
    skip_weekends: bool = True
    extra_workdays: frozenset[date] = frozenset()
    excluded_dates: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        if self.mode not in {"automatic", "dry_run"}:
            raise ValueError("mode 只能是 automatic 或 dry_run")
        if self.work_duration_minutes < 0 or self.safety_buffer_minutes < 0:
            raise ValueError("工作时长和安全缓冲不能为负数")
        if self.check_start_time > self.check_end_time:
            raise ValueError("第一版不支持跨自然日的检查时间窗")


class ClockoutEngine:
    def __init__(
        self,
        adapter: AttendanceAdapter,
        store: JsonStateStore,
        config: EngineConfig | None = None,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.config = config or EngineConfig()

    def check(self, now: datetime) -> CheckResult:
        day = now.date()
        if not is_workday(
            day,
            skip_weekends=self.config.skip_weekends,
            extra_workdays=self.config.extra_workdays,
            excluded_dates=self.config.excluded_dates,
        ):
            return CheckResult("skipped", "今天不属于已配置的工作日")
        if not is_within_window(
            now, self.config.check_start_time, self.config.check_end_time
        ):
            return CheckResult("skipped", "当前不在允许检查的时间范围")

        existing = self.store.load(day)
        if existing is not None and existing.clock_out_attempted:
            return CheckResult("already_attempted", "今天已经执行过自动打卡尝试")

        first = self._read_snapshot()
        if isinstance(first, CheckResult):
            return first
        precheck = self._validate_snapshot(first, now)
        if precheck is not None:
            return precheck

        eligible_time = calculate_eligible_time(
            day,
            first.check_in_time,
            self.config.work_duration_minutes,
            self.config.safety_buffer_minutes,
        )
        if now < eligible_time:
            return CheckResult(
                "waiting",
                "尚未到最早允许下班打卡时间",
                first.check_in_time,
                eligible_time,
            )

        if self.config.mode == "dry_run":
            return CheckResult(
                "dry_run_ready",
                "演练检查通过，未执行点击",
                first.check_in_time,
                eligible_time,
            )

        second = self._read_snapshot()
        if isinstance(second, CheckResult):
            return second
        second_precheck = self._validate_snapshot(second, now)
        if second_precheck is not None:
            return second_precheck
        if not snapshots_match(first, second):
            return CheckResult(
                "blocked",
                "点击前两次页面快照不一致",
                first.check_in_time,
                eligible_time,
            )

        claimed = self.store.claim_attempt(
            day,
            check_in_time=first.check_in_time.strftime("%H:%M"),
            eligible_time=eligible_time,
            attempted_at=now,
        )
        if not claimed:
            return CheckResult(
                "already_attempted",
                "今天已经执行过自动打卡尝试",
                first.check_in_time,
                eligible_time,
            )

        try:
            self.adapter.click_clock_out()
        except Exception:
            self.store.record_outcome(day, "click_failed", success=False)
            return CheckResult(
                "unknown",
                "点击未完成，今天不会自动重试",
                first.check_in_time,
                eligible_time,
            )

        try:
            success = self.adapter.verify_success()
        except Exception:
            success = False
        if not success:
            self.store.record_outcome(day, "unknown", success=False)
            return CheckResult(
                "unknown",
                "未确认打卡成功，今天不会自动重试",
                first.check_in_time,
                eligible_time,
            )

        self.store.record_outcome(day, "success", success=True)
        return CheckResult(
            "success",
            "已确认下班打卡成功",
            first.check_in_time,
            eligible_time,
        )

    def _read_snapshot(self) -> AttendanceSnapshot | CheckResult:
        try:
            return self.adapter.snapshot()
        except Exception:
            return CheckResult("blocked", "无法可靠读取飞书考勤页面")

    def _validate_snapshot(
        self, snapshot: AttendanceSnapshot, now: datetime
    ) -> CheckResult | None:
        if snapshot.check_in_time is None:
            return CheckResult("blocked", "未识别到唯一的上班打卡时间")
        eligible_time = calculate_eligible_time(
            now.date(),
            snapshot.check_in_time,
            self.config.work_duration_minutes,
            self.config.safety_buffer_minutes,
        )
        if snapshot.already_clocked_out:
            return CheckResult(
                "already_clocked_out",
                "飞书页面显示今天已经下班打卡",
                snapshot.check_in_time,
                eligible_time,
            )
        if snapshot.blocking_reason:
            return CheckResult(
                "blocked",
                snapshot.blocking_reason,
                snapshot.check_in_time,
                eligible_time,
            )
        if snapshot.button_count != 1 or not snapshot.button_enabled:
            return CheckResult(
                "blocked",
                "未找到唯一且可用的下班打卡按钮",
                snapshot.check_in_time,
                eligible_time,
            )
        if not snapshot.signature:
            return CheckResult(
                "blocked",
                "页面快照缺少一致性标识",
                snapshot.check_in_time,
                eligible_time,
            )
        return None
