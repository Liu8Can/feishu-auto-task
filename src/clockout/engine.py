from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Protocol

from .core import (
    AttendanceSnapshot,
    CheckResult,
    PunchAction,
    calculate_eligible_time,
    is_within_window,
    is_workday,
    snapshots_match,
)
from .storage import DailyState, JsonStateStore


LOGGER = logging.getLogger("clockout-demo")


class AttendanceAdapter(Protocol):
    def snapshot(self, day: date) -> AttendanceSnapshot: ...

    def click_clock_out(self, expected_signature: str) -> None: ...

    def verify_success(self) -> bool: ...

    def is_session_interactive(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class EngineConfig:
    mode: str = "dry_run"
    work_duration_minutes: int = 480
    safety_buffer_minutes: int = 5
    check_start_time: time = time(15, 0)
    check_end_time: time = time(23, 30)
    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    extra_workdays: frozenset[date] = frozenset()
    excluded_dates: frozenset[date] = frozenset()
    calculation_mode: str = "dynamic"
    break_start_time: time = time(12, 0)
    break_end_time: time = time(14, 0)
    fixed_checkin_time: time = time(8, 50)
    fixed_clockout_time: time = time(18, 50)
    max_click_attempts: int = 3
    retry_delay_minutes: int = 5

    def __post_init__(self) -> None:
        if self.mode not in {"automatic", "dry_run"}:
            raise ValueError("mode 只能是 automatic 或 dry_run")
        if self.calculation_mode not in {"dynamic", "fixed"}:
            raise ValueError("calculation_mode 只能是 dynamic 或 fixed")
        if (
            isinstance(self.work_duration_minutes, bool)
            or not isinstance(self.work_duration_minutes, int)
            or not 1 <= self.work_duration_minutes <= 24 * 60
        ):
            raise ValueError("工作时长必须在 1 到 1440 分钟之间")
        if (
            isinstance(self.safety_buffer_minutes, bool)
            or not isinstance(self.safety_buffer_minutes, int)
            or not 0 <= self.safety_buffer_minutes <= 180
        ):
            raise ValueError("安全缓冲必须在 0 到 180 分钟之间")
        if (
            isinstance(self.max_click_attempts, bool)
            or not isinstance(self.max_click_attempts, int)
            or not 1 <= self.max_click_attempts <= 5
        ):
            raise ValueError("自动点击尝试次数必须在 1 到 5 次之间")
        if (
            isinstance(self.retry_delay_minutes, bool)
            or not isinstance(self.retry_delay_minutes, int)
            or not 1 <= self.retry_delay_minutes <= 60
        ):
            raise ValueError("重试间隔必须在 1 到 60 分钟之间")
        if not all(
            isinstance(value, time)
            for value in (
                self.break_start_time,
                self.break_end_time,
                self.fixed_checkin_time,
                self.fixed_clockout_time,
                self.check_start_time,
                self.check_end_time,
            )
        ):
            raise ValueError("作息和检查时间必须是有效时间")
        if self.check_start_time > self.check_end_time:
            raise ValueError("第一版不支持跨自然日的检查时间窗")
        if self.break_start_time >= self.break_end_time:
            raise ValueError("休息开始时间必须早于休息结束时间")
        if self.fixed_checkin_time >= self.fixed_clockout_time:
            raise ValueError("固定上班时间必须早于固定下班时间")
        if not self.weekdays or any(day not in range(7) for day in self.weekdays):
            raise ValueError("工作日配置无效")


class ClockoutEngine:
    def __init__(
        self,
        adapter: AttendanceAdapter,
        store: JsonStateStore,
        config: EngineConfig | None = None,
        now_provider: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.config = config or EngineConfig()
        self._now_provider = now_provider

    def check(self, now: datetime | None = None) -> CheckResult:
        now = now or self._now_provider()
        day = now.date()
        if not is_workday(
            day,
            weekdays=self.config.weekdays,
            extra_workdays=self.config.extra_workdays,
            excluded_dates=self.config.excluded_dates,
        ):
            return CheckResult("skipped", "今天不属于已配置的工作日")
        if not is_within_window(
            now, self.config.check_start_time, self.config.check_end_time
        ):
            return CheckResult("skipped", "当前不在允许检查的时间范围")

        try:
            existing = self.store.load(day)
        except Exception:
            return CheckResult("blocked", "每日状态文件损坏或无法读取")
        if existing is not None and existing.clock_out_attempted:
            if existing.outcome == "aborted_before_click":
                if existing.attempt_count >= self.config.max_click_attempts:
                    return CheckResult("retry_exhausted", "点击前检查连续失败，已达到今日重试上限")
                retry_at = (
                    datetime.fromisoformat(existing.next_retry_at)
                    if existing.next_retry_at
                    else now
                )
                if now < retry_at:
                    return CheckResult(
                        "retry_waiting",
                        "点击前检查失败，等待自动重试",
                        next_retry_time=retry_at,
                    )
            elif existing.outcome in {"attempted", "click_failed", "unknown"}:
                return self._verify_uncertain_attempt(day, existing)
            else:
                return CheckResult("already_attempted", "今天已经执行过自动打卡尝试")

        if self.config.mode == "automatic" and not self._session_is_interactive():
            return CheckResult("blocked", "Windows 当前不是可交互桌面会话")

        first = self._read_snapshot(day)
        if isinstance(first, CheckResult):
            return first
        precheck = self._validate_snapshot(first, now, day)
        if precheck is not None:
            return precheck

        eligible_time = self._calculate_eligible_time(day, first.check_in_time)
        if eligible_time > datetime.combine(day, self.config.check_end_time):
            return CheckResult(
                "blocked",
                "目标下班时间晚于检查结束时间，请调整作息或检查时间范围",
                first.check_in_time,
                eligible_time,
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

        second = self._read_snapshot(day)
        if isinstance(second, CheckResult):
            return second
        final_now = self._now_provider()
        second_precheck = self._validate_snapshot(second, final_now, day)
        if second_precheck is not None:
            return second_precheck
        if not snapshots_match(first, second):
            return CheckResult(
                "blocked",
                "点击前两次页面快照不一致",
                first.check_in_time,
                eligible_time,
            )

        if final_now.date() != day:
            return CheckResult(
                "blocked",
                "点击前日期已经变化",
                first.check_in_time,
                eligible_time,
            )
        if final_now < now:
            return CheckResult(
                "blocked",
                "检测到系统时间回拨",
                first.check_in_time,
                eligible_time,
            )
        if not is_within_window(
            final_now, self.config.check_start_time, self.config.check_end_time
        ):
            return CheckResult(
                "blocked",
                "点击前已经离开允许检查的时间范围",
                first.check_in_time,
                eligible_time,
            )
        if final_now < eligible_time:
            return CheckResult(
                "blocked",
                "点击前尚未到最早允许下班打卡时间",
                first.check_in_time,
                eligible_time,
            )
        if not self._session_is_interactive():
            return CheckResult(
                "blocked",
                "点击前 Windows 已不再是可交互桌面会话",
                first.check_in_time,
                eligible_time,
            )

        try:
            claim_token = self.store.claim_action(
                day,
                PunchAction.CHECK_OUT,
                check_in_time=first.check_in_time.strftime("%H:%M"),
                eligible_time=eligible_time,
                attempted_at=final_now,
                max_attempts=self.config.max_click_attempts,
            )
        except Exception:
            return CheckResult(
                "blocked",
                "无法安全写入每日打卡状态",
                first.check_in_time,
                eligible_time,
            )
        if not claim_token:
            return CheckResult(
                "already_attempted",
                "今天已经执行过自动打卡尝试",
                first.check_in_time,
                eligible_time,
            )

        invoke_now = self._now_provider()
        environment_error = self._final_environment_error(
            initial_now=final_now,
            current_now=invoke_now,
            day=day,
            eligible_time=eligible_time,
        )
        if environment_error or not self._session_is_interactive():
            retry_at = invoke_now + timedelta(minutes=self.config.retry_delay_minutes)
            self._record_outcome(
                day,
                claim_token,
                "aborted_before_click",
                success=False,
                next_retry_at=retry_at,
            )
            return CheckResult(
                "retry_waiting",
                environment_error
                or "点击前环境发生变化，已安排有限重试",
                first.check_in_time,
                eligible_time,
                retry_at,
            )

        try:
            self.adapter.click_clock_out(second.signature)
        except Exception as exc:
            invocation_started = bool(getattr(exc, "invocation_started", True))
            outcome = "click_failed" if invocation_started else "aborted_before_click"
            retry_at = (
                None
                if invocation_started
                else self._now_provider()
                + timedelta(minutes=self.config.retry_delay_minutes)
            )
            self._record_outcome(
                day,
                claim_token,
                outcome,
                success=False,
                invocation_started=invocation_started,
                next_retry_at=retry_at,
            )
            LOGGER.exception("下班打卡点击异常（已开始调用：%s）", invocation_started)
            return CheckResult(
                "unknown" if invocation_started else "retry_waiting",
                (
                    "点击调用失败，结果不明确，今天不会自动重试"
                    if invocation_started
                    else "点击前安全检查未通过，已安排有限重试"
                ),
                first.check_in_time,
                eligible_time,
                retry_at,
            )

        try:
            success = self.adapter.verify_success()
        except Exception:
            success = False
        if not success:
            self._record_outcome(
                day,
                claim_token,
                "unknown",
                success=False,
                invocation_started=True,
            )
            return CheckResult(
                "unknown",
                "未确认打卡成功，今天不会自动重试",
                first.check_in_time,
                eligible_time,
            )

        self._record_outcome(
            day, claim_token, "success", success=True, invocation_started=True
        )
        return CheckResult(
            "success",
            "已确认下班打卡成功",
            first.check_in_time,
            eligible_time,
        )

    def _read_snapshot(self, day: date) -> AttendanceSnapshot | CheckResult:
        try:
            return self.adapter.snapshot(day)
        except Exception:
            return CheckResult("blocked", "无法可靠读取飞书考勤页面")

    def _verify_uncertain_attempt(
        self, day: date, existing: DailyState
    ) -> CheckResult:
        if not self._session_is_interactive():
            return CheckResult("unknown", "此前点击结果不明确，解锁后将继续核验飞书页面")
        snapshot = self._read_snapshot(day)
        eligible_time = datetime.fromisoformat(existing.eligible_time)
        check_in_time = time.fromisoformat(existing.check_in_time)
        if isinstance(snapshot, CheckResult):
            return CheckResult(
                "unknown",
                "此前点击结果不明确，暂未能重新读取飞书页面",
                check_in_time,
                eligible_time,
            )
        if snapshot.page_date == day and snapshot.already_clocked_out:
            checkout = existing.action(PunchAction.CHECK_OUT)
            if checkout is not None:
                self._record_outcome(
                    day, checkout.claim_token, "success", success=True
                )
            return CheckResult(
                "already_clocked_out",
                "已从飞书页面确认今天下班打卡成功",
                check_in_time,
                eligible_time,
            )
        return CheckResult(
            "unknown",
            "此前点击结果不明确；飞书尚未显示下班记录，不会自动重复点击",
            check_in_time,
            eligible_time,
        )

    def _session_is_interactive(self) -> bool:
        try:
            return self.adapter.is_session_interactive()
        except Exception:
            return False

    def _record_outcome(
        self,
        day: date,
        claim_token: str,
        outcome: str,
        *,
        success: bool,
        invocation_started: bool = False,
        next_retry_at: datetime | None = None,
    ) -> None:
        try:
            self.store.record_action_outcome(
                day,
                PunchAction.CHECK_OUT,
                claim_token,
                outcome,
                success=success,
                invocation_started=invocation_started,
                next_retry_at=next_retry_at,
            )
        except Exception:
            # The durable attempted flag was written before clicking, so retry stays blocked.
            pass

    def _final_environment_error(
        self,
        *,
        initial_now: datetime,
        current_now: datetime,
        day: date,
        eligible_time: datetime,
    ) -> str | None:
        if current_now.date() != day:
            return "点击资格已锁定，但日期已经变化，今天不会自动重试"
        if current_now < initial_now:
            return "点击资格已锁定，但检测到系统时间回拨，今天不会自动重试"
        if not is_within_window(
            current_now, self.config.check_start_time, self.config.check_end_time
        ):
            return "点击资格已锁定，但已离开检查时间范围，今天不会自动重试"
        if current_now < eligible_time:
            return "点击资格已锁定，但当前时间早于目标时间，今天不会自动重试"
        return None

    def _validate_snapshot(
        self, snapshot: AttendanceSnapshot, now: datetime, expected_day: date
    ) -> CheckResult | None:
        if snapshot.page_date != expected_day:
            return CheckResult("blocked", "未确认当前页面属于今天")
        if snapshot.check_in_time is None:
            return CheckResult("blocked", "未识别到唯一的上班打卡时间")
        eligible_time = self._calculate_eligible_time(
            now.date(), snapshot.check_in_time
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
        if now >= eligible_time and (
            snapshot.button_count != 1 or not snapshot.button_enabled
        ):
            return CheckResult(
                "blocked",
                "未找到唯一且可用的下班打卡按钮",
                snapshot.check_in_time,
                eligible_time,
            )
        if now >= eligible_time and not snapshot.signature:
            return CheckResult(
                "blocked",
                "页面快照缺少一致性标识",
                snapshot.check_in_time,
                eligible_time,
            )
        if now >= eligible_time and (
            not snapshot.container_id or not snapshot.button_id
        ):
            return CheckResult(
                "blocked",
                "考勤页面或下班按钮缺少稳定身份标识",
                snapshot.check_in_time,
                eligible_time,
            )
        return None

    def _calculate_eligible_time(
        self, day: date, check_in_time: time
    ) -> datetime:
        return calculate_eligible_time(
            day,
            check_in_time,
            self.config.work_duration_minutes,
            self.config.safety_buffer_minutes,
            calculation_mode=self.config.calculation_mode,
            break_start_time=self.config.break_start_time,
            break_end_time=self.config.break_end_time,
            fixed_checkin_time=self.config.fixed_checkin_time,
            fixed_clockout_time=self.config.fixed_clockout_time,
        )
