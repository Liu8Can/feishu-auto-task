from __future__ import annotations

import hashlib
import os
import re
import time as time_module
from contextlib import contextmanager
from datetime import time
from typing import Iterable

import pythoncom
from pywinauto import Desktop

from .core import AttendanceSnapshot, parse_unique_check_in_time


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


class FeishuUiaAdapter:
    """Strict UIA adapter: ambiguous controls always produce a blocked snapshot."""

    def __init__(self, *, auto_open_workbench: bool = True, navigation_wait: float = 3.0):
        self.auto_open_workbench = auto_open_workbench
        self.navigation_wait = navigation_wait
        self._restore_after_action = False

    def snapshot(self) -> AttendanceSnapshot:
        with self._com_scope():
            return self._snapshot()

    def _snapshot(self) -> AttendanceSnapshot:
        window = self._main_window()
        if window is None and self.auto_open_workbench:
            self.open_workbench()
            window = self._wait_for_main_window()
        if window is None:
            return self._blocked("未找到飞书主窗口")

        was_minimized = bool(window.is_minimized())
        self._restore_after_action = self._restore_after_action or was_minimized
        try:
            if was_minimized:
                window.restore()
                time_module.sleep(0.8)
            texts, buttons = self._read_page(window)
            if not self._looks_like_attendance(texts) and self.auto_open_workbench:
                self.open_workbench()
                time_module.sleep(self.navigation_wait)
                window = self._main_window()
                if window is None:
                    return self._blocked("打开飞书工作台后未找到主窗口")
                texts, buttons = self._read_page(window)
                if not self._looks_like_attendance(texts):
                    self._open_attendance_entry(window)
                    time_module.sleep(self.navigation_wait)
                    window = self._main_window()
                    if window is None:
                        return self._blocked("打开考勤入口后未找到飞书主窗口")
                    texts, buttons = self._read_page(window)

            return self._build_snapshot(texts, buttons)
        except Exception as exc:
            return self._blocked(f"读取飞书页面失败：{type(exc).__name__}")
        finally:
            if was_minimized:
                try:
                    window.minimize()
                except Exception:
                    pass

    def click_clock_out(self) -> None:
        with self._com_scope():
            window = self._main_window()
            if window is None:
                raise RuntimeError("点击前未找到飞书主窗口")
            if window.is_minimized():
                window.restore()
                time_module.sleep(0.8)
            buttons = self._checkout_buttons(window)
            if len(buttons) != 1 or not buttons[0].is_enabled() or not buttons[0].is_visible():
                raise RuntimeError("点击前下班打卡按钮不唯一或不可用")
            try:
                buttons[0].invoke()
            except Exception as exc:
                raise RuntimeError("下班打卡按钮不支持安全调用") from exc

    def verify_success(self) -> bool:
        time_module.sleep(2.5)
        with self._com_scope():
            window = self._main_window()
            if window is None:
                return False
            try:
                texts, _ = self._read_page(window)
                return any(pattern.search(text) for pattern in SUCCESS_PATTERNS for text in texts)
            finally:
                if self._restore_after_action:
                    try:
                        window.minimize()
                    except Exception:
                        pass
                    self._restore_after_action = False

    def open_workbench(self) -> None:
        os.startfile(WORKBENCH_URI)

    def diagnostics(self) -> dict[str, object]:
        with self._com_scope():
            window = self._main_window()
            if window is None:
                return {"window_found": False, "element_count": 0, "named_count": 0}
            descendants = window.descendants()
            named_count = sum(bool(self._text(control)) for control in descendants)
            return {
                "window_found": True,
                "element_count": len(descendants),
                "named_count": named_count,
                "attendance_page": self._looks_like_attendance(
                    [self._text(control) for control in descendants if self._text(control)]
                ),
                "checkout_button_count": len(self._checkout_buttons(window)),
            }

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

    def _read_page(self, window) -> tuple[list[str], list[object]]:
        descendants = window.descendants()
        texts = [text for control in descendants if (text := self._text(control))]
        return texts, self._checkout_buttons(window, descendants)

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

    def _build_snapshot(self, texts: list[str], buttons: list[object]) -> AttendanceSnapshot:
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

        if not self._looks_like_attendance(texts):
            blocking_reason = "未确认当前页面为考勤打卡页"

        signature_parts = (
            check_in_time.isoformat(timespec="minutes") if check_in_time else "none",
            str(already_clocked_out),
            str(len(buttons)),
            str(enabled),
            blocking_reason or "",
        )
        signature = hashlib.sha256("|".join(signature_parts).encode("utf-8")).hexdigest()
        return AttendanceSnapshot(
            check_in_time=check_in_time,
            already_clocked_out=already_clocked_out,
            button_count=len(buttons),
            button_enabled=enabled,
            blocking_reason=blocking_reason,
            signature=signature,
        )

    @staticmethod
    def _text(control) -> str:
        try:
            return " ".join(control.window_text().split())
        except Exception:
            return ""

    @staticmethod
    def _looks_like_attendance(texts: Iterable[str]) -> bool:
        return any("上班打卡" in text or "下班打卡" in text for text in texts)

    @staticmethod
    def _blocked(reason: str) -> AttendanceSnapshot:
        return AttendanceSnapshot(
            check_in_time=None,
            already_clocked_out=False,
            button_count=0,
            button_enabled=False,
            blocking_reason=reason,
            signature=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    @contextmanager
    def _com_scope():
        pythoncom.CoInitialize()
        try:
            yield
        finally:
            pythoncom.CoUninitialize()
