from __future__ import annotations

import hashlib
import os
import re
import time as time_module
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, time
from typing import Iterable

import pythoncom
from pywinauto import Desktop

from .core import AttendanceSnapshot, parse_unique_check_in_time
from .session import is_interactive_desktop


CHECKOUT_BUTTON_TEXT = "下班打卡"
WORKBENCH_URI = "lark://appcenter.open"
ATTENDANCE_ENTRY_NAMES = ("考勤打卡", "考勤")
SUCCESS_PATTERNS = (
    re.compile(r"下班已打卡(?:\s*\d{1,2}:\d{2})?"),
    re.compile(r"下班打卡成功"),
)
BLOCKING_MESSAGES = (
    "需要人脸识别",
    "人脸识别失败",
    "不在考勤范围",
    "定位失败",
    "网络异常",
    "打卡处理中",
    "打卡失败",
    "当前无需打卡",
    "请重新登录",
    "登录已失效",
    "今日请假",
    "今日休息",
    "今日出差",
)


@dataclass(frozen=True)
class _AttendanceContext:
    texts: list[str]
    buttons: list[object]
    container_id: str
    button_id: str


class FeishuUiaAdapter:
    """Strict UIA adapter: ambiguous controls always produce a blocked snapshot."""

    def __init__(
        self,
        *,
        auto_open_workbench: bool = True,
        navigation_wait: float = 3.0,
        trusted_container_fingerprint: str = "",
    ):
        self.auto_open_workbench = auto_open_workbench
        self.navigation_wait = navigation_wait
        self.trusted_container_fingerprint = trusted_container_fingerprint
        self._restore_after_action = False
        self._pending_signature = ""
        self._pending_day: date | None = None

    def snapshot(self, day: date) -> AttendanceSnapshot:
        with self._com_scope():
            return self._snapshot(day)

    def _snapshot(self, day: date) -> AttendanceSnapshot:
        self._clear_pending_button()
        if not self.trusted_container_fingerprint:
            return self._blocked("尚未绑定当前飞书考勤页面")
        window = self._main_window()
        if window is None and self.auto_open_workbench:
            self.open_workbench()
            window = self._wait_for_main_window()
        if window is None:
            return self._blocked("未找到飞书主窗口")

        was_minimized = bool(window.is_minimized())
        self._restore_after_action = was_minimized
        try:
            if was_minimized:
                window.restore()
                time_module.sleep(0.8)
            context = self._attendance_context(window, day, require_trusted=True)
            if context is None and self.auto_open_workbench:
                self.open_workbench()
                time_module.sleep(self.navigation_wait)
                window = self._main_window()
                if window is None:
                    return self._blocked("打开飞书工作台后未找到主窗口")
                context = self._attendance_context(window, day, require_trusted=True)
                if context is None:
                    self._open_attendance_entry(window)
                    time_module.sleep(self.navigation_wait)
                    window = self._main_window()
                    if window is None:
                        return self._blocked("打开考勤入口后未找到飞书主窗口")
                    context = self._attendance_context(window, day, require_trusted=True)

            if context is None:
                return self._blocked("未确认当前页面为今天的考勤打卡页")
            snapshot = self._build_snapshot(
                context.texts,
                context.buttons,
                day=day,
                container_id=context.container_id,
                button_id=context.button_id,
            )
            if (
                snapshot.blocking_reason is None
                and snapshot.button_count == 1
                and snapshot.button_enabled
            ):
                self._pending_signature = snapshot.signature
                self._pending_day = day
            return snapshot
        except Exception as exc:
            return self._blocked(f"读取飞书页面失败：{type(exc).__name__}")
        finally:
            if was_minimized:
                try:
                    window.minimize()
                except Exception:
                    pass

    def click_clock_out(self, expected_signature: str) -> None:
        with self._com_scope():
            window = self._main_window()
            if window is None:
                self._restore_after_action = False
                raise RuntimeError("点击前未找到飞书主窗口")
            try:
                if self._pending_signature != expected_signature or self._pending_day is None:
                    raise RuntimeError("点击目标与最终页面快照不一致")
                if window.is_minimized():
                    window.restore()
                    time_module.sleep(0.8)
                if not is_interactive_desktop():
                    raise RuntimeError("点击前 Windows 已不再是可交互桌面会话")
                context = self._attendance_context(
                    window, self._pending_day, require_trusted=True
                )
                if context is None:
                    raise RuntimeError("点击前无法重新确认考勤页面")
                final_snapshot = self._build_snapshot(
                    context.texts,
                    context.buttons,
                    day=self._pending_day,
                    container_id=context.container_id,
                    button_id=context.button_id,
                )
                if final_snapshot.signature != expected_signature:
                    raise RuntimeError("点击前页面状态已经变化")
                if len(context.buttons) != 1:
                    raise RuntimeError("点击前下班打卡按钮不唯一")
                button = context.buttons[0]
                if self._text(button) != CHECKOUT_BUTTON_TEXT:
                    raise RuntimeError("点击前按钮文字已经变化")
                if not button.is_enabled() or not button.is_visible():
                    raise RuntimeError("点击前下班打卡按钮不唯一或不可用")
                self._clear_pending_button(keep_restore=True)
                button.invoke()
            except Exception as exc:
                self._restore_window(window)
                raise RuntimeError("下班打卡按钮无法安全调用") from exc

    def verify_success(self) -> bool:
        time_module.sleep(2.5)
        with self._com_scope():
            window = self._main_window()
            if window is None:
                self._restore_after_action = False
                self._pending_day = None
                return False
            try:
                if self._pending_day is None:
                    return False
                context = self._attendance_context(
                    window, self._pending_day, require_trusted=True
                )
                if context is None:
                    return False
                return any(
                    pattern.search(text)
                    for pattern in SUCCESS_PATTERNS
                    for text in context.texts
                )
            finally:
                self._restore_window(window)
                self._pending_day = None

    def is_session_interactive(self) -> bool:
        return is_interactive_desktop()

    def open_workbench(self) -> None:
        os.startfile(WORKBENCH_URI)

    def diagnostics(self) -> dict[str, object]:
        with self._com_scope():
            window = self._main_window()
            if window is None:
                return {"window_found": False, "element_count": 0, "named_count": 0}
            descendants = window.descendants()
            named_count = sum(bool(self._text(control)) for control in descendants)
            context = self._attendance_context(window, date.today(), require_trusted=False)
            return {
                "window_found": True,
                "element_count": len(descendants),
                "named_count": named_count,
                "attendance_page": context is not None,
                "checkout_button_count": len(context.buttons) if context else 0,
                "bound": bool(self.trusted_container_fingerprint),
            }

    def calibrate_current_page(self, day: date) -> str:
        with self._com_scope():
            if not is_interactive_desktop():
                raise RuntimeError("Windows 当前不是可交互桌面会话")
            window = self._main_window()
            if window is None:
                raise RuntimeError("未找到飞书主窗口")
            if window.is_minimized():
                window.restore()
                time_module.sleep(0.8)
            context = self._attendance_context(window, day, require_trusted=False)
            if context is None:
                raise RuntimeError("当前页面不是可唯一确认的今日考勤页")
            self.trusted_container_fingerprint = context.container_id
            return context.container_id

    def _main_window(self):
        candidates = []
        for window in Desktop(backend="uia").windows(title="飞书", control_type="Window"):
            try:
                if window.class_name() == "Chrome_WidgetWin_1":
                    candidates.append(window)
            except Exception:
                continue
        return candidates[0] if len(candidates) == 1 else None

    def _wait_for_main_window(self):
        deadline = time_module.monotonic() + 8
        while time_module.monotonic() < deadline:
            window = self._main_window()
            if window is not None:
                return window
            time_module.sleep(0.5)
        return None

    def _attendance_context(
        self, window, day: date, *, require_trusted: bool
    ) -> _AttendanceContext | None:
        candidates: dict[str, _AttendanceContext] = {}
        title_controls = [
            control
            for control in window.descendants()
            if self._text(control) == "考勤打卡"
        ]
        for title in title_controls:
            current = title
            for _ in range(8):
                try:
                    current = current.parent()
                    if current.element_info.control_type == "Window":
                        break
                    descendants = current.descendants()
                except Exception:
                    break
                texts = [self._text(current)]
                texts.extend(
                    text for control in descendants if (text := self._text(control))
                )
                page_date = self._extract_page_date(texts, day.year)
                if page_date != day:
                    continue
                if not any("上班打卡" in text for text in texts):
                    continue
                buttons = self._checkout_buttons(current, descendants)
                has_success = any(
                    pattern.search(text)
                    for pattern in SUCCESS_PATTERNS
                    for text in texts
                )
                if not buttons and not has_success:
                    continue
                container_id = self._stable_fingerprint(current)
                if not container_id:
                    continue
                if require_trusted and container_id != self.trusted_container_fingerprint:
                    continue
                button_id = (
                    self._stable_fingerprint(buttons[0]) if len(buttons) == 1 else ""
                )
                candidates[container_id] = _AttendanceContext(
                    texts=texts,
                    buttons=buttons,
                    container_id=container_id,
                    button_id=button_id,
                )
                break
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    def _checkout_buttons(self, window, descendants: Iterable[object] | None = None) -> list[object]:
        controls = descendants if descendants is not None else window.descendants()
        matches = []
        for control in controls:
            try:
                if (
                    control.element_info.control_type == "Button"
                    and self._text(control) == CHECKOUT_BUTTON_TEXT
                ):
                    matches.append(control)
            except Exception:
                continue
        return matches

    def _open_attendance_entry(self, window) -> None:
        matches = []
        for control in window.descendants():
            text = self._text(control)
            if text in ATTENDANCE_ENTRY_NAMES and control.is_visible() and control.is_enabled():
                matches.append(control)
        if len(matches) != 1:
            return
        try:
            matches[0].invoke()
        except Exception:
            return

    def _build_snapshot(
        self,
        texts: list[str],
        buttons: list[object],
        *,
        day: date,
        container_id: str,
        button_id: str,
    ) -> AttendanceSnapshot:
        check_in_time: time | None = None
        blocking_reason: str | None = None
        try:
            check_in_time = parse_unique_check_in_time(texts)
        except ValueError as exc:
            blocking_reason = str(exc)

        for message in BLOCKING_MESSAGES:
            if any(message in text for text in texts):
                blocking_reason = f"页面提示：{message}"
                break

        already_clocked_out = any(
            pattern.search(text) for pattern in SUCCESS_PATTERNS for text in texts
        )
        enabled = False
        if len(buttons) == 1:
            try:
                enabled = bool(buttons[0].is_enabled() and buttons[0].is_visible())
            except Exception:
                enabled = False

        page_date = self._extract_page_date(texts, day.year)
        if "考勤打卡" not in texts or page_date != day:
            blocking_reason = "未确认当前页面为今天的考勤打卡页"

        signature_parts = (
            check_in_time.isoformat(timespec="minutes") if check_in_time else "none",
            str(already_clocked_out),
            str(len(buttons)),
            str(enabled),
            blocking_reason or "",
            page_date.isoformat() if page_date else "none",
            container_id,
            button_id,
        )
        signature = hashlib.sha256("|".join(signature_parts).encode("utf-8")).hexdigest()
        return AttendanceSnapshot(
            check_in_time=check_in_time,
            already_clocked_out=already_clocked_out,
            button_count=len(buttons),
            button_enabled=enabled,
            blocking_reason=blocking_reason,
            signature=signature,
            page_date=page_date,
            container_id=container_id,
            button_id=button_id,
        )

    @staticmethod
    def _text(control) -> str:
        try:
            return " ".join(control.window_text().split())
        except Exception:
            return ""

    @staticmethod
    def _extract_page_date(texts: Iterable[str], default_year: int) -> date | None:
        patterns = (
            re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?"),
            re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日"),
        )
        candidates: set[date] = set()
        for text_value in texts:
            for index, pattern in enumerate(patterns):
                for match in pattern.finditer(text_value):
                    try:
                        if index == 0:
                            candidates.add(
                                date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                            )
                        else:
                            candidates.add(
                                date(default_year, int(match.group(1)), int(match.group(2)))
                            )
                    except ValueError:
                        return None
        return next(iter(candidates)) if len(candidates) == 1 else None

    @staticmethod
    def _stable_fingerprint(control) -> str:
        try:
            parts = []
            current = control
            for _ in range(6):
                info = current.element_info
                parts.append(
                    "|".join(
                        (
                            str(getattr(info, "control_type", "")),
                            str(getattr(info, "class_name", "")),
                            str(getattr(info, "automation_id", "")),
                        )
                    )
                )
                if getattr(info, "control_type", "") == "Window":
                    break
                current = current.parent()
            if not any(part.replace("|", "") for part in parts):
                return ""
            raw = ">".join(parts)
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
        except Exception:
            return ""

    @staticmethod
    def _blocked(reason: str) -> AttendanceSnapshot:
        return AttendanceSnapshot(
            check_in_time=None,
            already_clocked_out=False,
            button_count=0,
            button_enabled=False,
            blocking_reason=reason,
            signature=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            page_date=None,
            container_id="",
            button_id="",
        )

    def _restore_window(self, window) -> None:
        if self._restore_after_action:
            try:
                window.minimize()
            except Exception:
                pass
        self._restore_after_action = False

    def _clear_pending_button(self, *, keep_restore: bool = False) -> None:
        self._pending_signature = ""
        if not keep_restore:
            self._pending_day = None

    @staticmethod
    @contextmanager
    def _com_scope():
        pythoncom.CoInitialize()
        try:
            yield
        finally:
            pythoncom.CoUninitialize()
