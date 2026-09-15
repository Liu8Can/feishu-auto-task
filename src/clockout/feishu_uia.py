from __future__ import annotations

import hashlib
import os
import re
import time as time_module
import winreg
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, time
from typing import Iterable

import pythoncom
import win32gui
from pywinauto import Desktop
from pywinauto.application import process_module

from .core import AttendanceSnapshot, parse_unique_check_in_time
from .session import is_interactive_desktop


CHECKOUT_BUTTON_TEXT = "下班打卡"
FEISHU_EXECUTABLE = "feishu.exe"
FEISHU_WINDOW_TITLES = ("飞书", "假勤")
ATTENDANCE_PAGE_NAMES = ("考勤打卡", "假勤")
BARE_CLOCKED_PATTERN = re.compile(r"^已打卡\s*[:：]?\s*(\d{1,2}:\d{2})$")
ATTENDANCE_ENTRY_NAMES = ("假勤", "考勤打卡")
FEISHU_PROTOCOL_COMMAND_KEY = r"Software\Classes\lark\shell\open\command"
EXECUTABLE_FROM_COMMAND_PATTERN = re.compile(
    r'^\s*(?:"([^"]+\.exe)"|([^\s]+\.exe))', re.IGNORECASE
)
NAVIGATION_POLL_INTERVAL = 0.2
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
    container_runtime_id: tuple[object, ...]
    button_runtime_id: tuple[object, ...]


class ClockoutClickError(RuntimeError):
    def __init__(self, message: str, *, invocation_started: bool) -> None:
        super().__init__(message)
        self.invocation_started = invocation_started


class FeishuUiaAdapter:
    """Strict UIA adapter: ambiguous controls always produce a blocked snapshot."""

    def __init__(
        self,
        *,
        auto_open_workbench: bool = True,
        navigation_wait: float = 15.0,
        trusted_container_fingerprint: str = "",
    ):
        self.auto_open_workbench = auto_open_workbench
        self.navigation_wait = navigation_wait
        self.trusted_container_fingerprint = trusted_container_fingerprint
        self._restore_after_action = False
        self._pending_signature = ""
        self._pending_day: date | None = None
        self._pending_container_runtime_id: tuple[object, ...] = ()
        self._pending_button_runtime_id: tuple[object, ...] = ()

    def snapshot(self, day: date) -> AttendanceSnapshot:
        with self._com_scope():
            return self._snapshot(day)

    def _snapshot(self, day: date) -> AttendanceSnapshot:
        self._clear_pending_button()
        if not self.trusted_container_fingerprint:
            return self._blocked("尚未绑定当前飞书考勤页面")
        page = self._find_or_open_attendance_page(day, require_trusted=True)
        if page is None:
            return self._blocked("未确认当前页面为今天的考勤打卡页")

        window, context = page
        was_minimized = bool(window.is_minimized())
        self._restore_after_action = was_minimized
        try:
            if was_minimized:
                window.restore()
                time_module.sleep(0.8)
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
                self._pending_container_runtime_id = context.container_runtime_id
                self._pending_button_runtime_id = context.button_runtime_id
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
            window = None
            invocation_started = False
            try:
                window, button = self._confirmed_checkout_target(expected_signature)
                if window.is_minimized():
                    window.restore()
                    time_module.sleep(0.8)
                    window, button = self._confirmed_checkout_target(
                        expected_signature
                    )
                if button.element_info.control_type == "Text":
                    window.set_focus()
                    time_module.sleep(0.2)
                    window, button = self._confirmed_checkout_target(
                        expected_signature
                    )
                    self._validate_text_click_target(window, button)
                    if not is_interactive_desktop():
                        raise RuntimeError("点击前 Windows 已不再是可交互桌面会话")
                self._clear_pending_button(keep_restore=True)
                invocation_started = True
                self._invoke_checkout_control(button)
            except Exception as exc:
                if window is not None:
                    self._restore_window(window)
                else:
                    self._restore_after_action = False
                raise ClockoutClickError(
                    f"下班打卡按钮无法安全调用：{exc}",
                    invocation_started=invocation_started,
                ) from exc

    def _confirmed_checkout_target(
        self, expected_signature: str
    ) -> tuple[object, object]:
        if (
            self._pending_day is None
            or self._pending_signature != expected_signature
        ):
            raise RuntimeError("点击目标与最终页面快照不一致")
        if not is_interactive_desktop():
            raise RuntimeError("点击前 Windows 已不再是可交互桌面会话")
        page = self._attendance_page(self._pending_day, require_trusted=True)
        if page is None:
            raise RuntimeError("点击前未找到唯一考勤窗口")
        window, context = page
        if (
            context.container_runtime_id != self._pending_container_runtime_id
            or context.button_runtime_id != self._pending_button_runtime_id
        ):
            raise RuntimeError("点击前考勤容器或按钮实例已经变化")
        final_snapshot = self._build_snapshot(
            context.texts,
            context.buttons,
            day=self._pending_day,
            container_id=context.container_id,
            button_id=context.button_id,
        )
        if (
            final_snapshot.signature != expected_signature
            or final_snapshot.page_date != self._pending_day
            or final_snapshot.blocking_reason is not None
        ):
            raise RuntimeError("点击前页面状态已经变化")
        if len(context.buttons) != 1:
            raise RuntimeError("点击前下班打卡按钮不唯一")
        button = context.buttons[0]
        if self._text(button) != CHECKOUT_BUTTON_TEXT:
            raise RuntimeError("点击前按钮文字已经变化")
        if not button.is_enabled() or not button.is_visible():
            raise RuntimeError("点击前下班打卡按钮不唯一或不可用")
        return window, button

    @classmethod
    def _top_level_window_handle(cls, control) -> int:
        current = control
        for _ in range(64):
            try:
                if current.element_info.control_type == "Window":
                    return int(current.handle)
                current = current.parent()
            except Exception:
                return 0
        return 0

    def _validate_text_click_target(self, window, button) -> None:
        window_handle = int(window.handle)
        if int(win32gui.GetForegroundWindow()) != window_handle:
            raise RuntimeError("点击前考勤窗口未处于前台")
        button_runtime_id = self._runtime_identity(button, window)
        if not button_runtime_id or button_runtime_id != self._pending_button_runtime_id:
            raise RuntimeError("点击前下班打卡按钮实例已经变化")
        rectangle = button.rectangle()
        if rectangle.right <= rectangle.left or rectangle.bottom <= rectangle.top:
            raise RuntimeError("点击前下班打卡目标位置无效")
        center_x = (rectangle.left + rectangle.right) // 2
        center_y = (rectangle.top + rectangle.bottom) // 2
        hit_control = Desktop(backend="uia").from_point(center_x, center_y)
        if self._top_level_window_handle(hit_control) != window_handle:
            raise RuntimeError("下班打卡目标被其他窗口遮挡")
        if not self._control_chain_contains_identity(
            hit_control, button_runtime_id, window
        ):
            raise RuntimeError("下班打卡目标被页面内其他控件遮挡")
        if int(win32gui.GetForegroundWindow()) != window_handle:
            raise RuntimeError("点击前考勤窗口已离开前台")

    @classmethod
    def _control_chain_contains_identity(
        cls, control, expected_identity: tuple[object, ...], root
    ) -> bool:
        current = control
        for _ in range(64):
            if cls._runtime_identity(current, root) == expected_identity:
                return True
            try:
                if current.element_info.control_type == "Window":
                    return False
                current = current.parent()
            except Exception:
                return False
        return False

    def verify_success(self) -> bool:
        time_module.sleep(2.5)
        with self._com_scope():
            if self._pending_day is None:
                self._restore_after_action = False
                return False
            page = self._attendance_page(self._pending_day, require_trusted=True)
            if page is None:
                self._restore_after_action = False
                self._pending_day = None
                return False
            window, context = page
            try:
                context = self._attendance_context(
                    window, self._pending_day, require_trusted=True
                )
                if context is None:
                    return False
                return any(
                    pattern.search(text)
                    for pattern in SUCCESS_PATTERNS
                    for text in context.texts
                ) or self._has_independent_checkout_record(context.texts)
            finally:
                self._restore_window(window)
                self._pending_day = None

    def is_session_interactive(self) -> bool:
        return is_interactive_desktop()

    def launch_feishu(self) -> None:
        if self._main_window() is not None:
            return
        executable = self._registered_feishu_executable()
        if executable is None:
            raise OSError("未找到飞书安装路径")
        os.startfile(executable)

    def open_attendance_page(self, day: date) -> bool:
        """Open and confirm today's unique attendance page without binding it."""
        with self._com_scope():
            if not is_interactive_desktop():
                raise RuntimeError("Windows 当前不是可交互桌面会话")
            page = self._find_or_open_attendance_page(
                day,
                require_trusted=bool(self.trusted_container_fingerprint),
                allow_navigation=True,
            )
            if page is None:
                return False
            return self._show_window(page[0])

    def diagnostics(self) -> dict[str, object]:
        with self._com_scope():
            windows = self._feishu_windows()
            if not windows:
                return {"window_found": False, "element_count": 0, "named_count": 0}
            descendants = [
                control for window in windows for control in window.descendants()
            ]
            named_count = sum(bool(self._text(control)) for control in descendants)
            page = self._attendance_page(date.today(), require_trusted=False)
            context = page[1] if page else None
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
            page = self._find_or_open_attendance_page(day, require_trusted=False)
            if page is None:
                raise RuntimeError("当前页面不是可唯一确认的今日考勤页")
            window, context = page
            if window.is_minimized():
                window.restore()
                time_module.sleep(0.8)
                context = self._attendance_context(window, day, require_trusted=False)
            if context is None:
                raise RuntimeError("当前页面不是可唯一确认的今日考勤页")
            self.trusted_container_fingerprint = context.container_id
            return context.container_id

    def _find_or_open_attendance_page(
        self,
        day: date,
        *,
        require_trusted: bool,
        allow_navigation: bool | None = None,
    ) -> tuple[object, _AttendanceContext] | None:
        page = self._attendance_page(day, require_trusted=require_trusted)
        navigation_enabled = (
            self.auto_open_workbench
            if allow_navigation is None
            else allow_navigation
        )
        if page is not None or not navigation_enabled:
            return page

        try:
            self.launch_feishu()
        except OSError:
            return None

        state = self._wait_until(
            lambda: self._attendance_page_or_ready_main_window(
                day, require_trusted=require_trusted
            )
        )
        if state is None:
            return None
        kind, value = state
        if kind == "page":
            return value
        if not self._show_window(value):
            return None
        if not self._open_attendance_entry(value):
            return None
        return self._wait_until(
            lambda: self._attendance_page(day, require_trusted=require_trusted)
        )

    def _attendance_page_or_ready_main_window(
        self, day: date, *, require_trusted: bool
    ) -> tuple[str, object] | None:
        page = self._attendance_page(day, require_trusted=require_trusted)
        if page is not None:
            return "page", page
        window = self._main_window()
        if window is not None and self._attendance_entry(window) is not None:
            return "window", window
        return None

    def _wait_until(self, finder):
        deadline = time_module.monotonic() + max(0.0, self.navigation_wait)
        while True:
            result = finder()
            if result is not None:
                return result
            remaining = deadline - time_module.monotonic()
            if remaining <= 0:
                return None
            time_module.sleep(min(NAVIGATION_POLL_INTERVAL, remaining))

    @staticmethod
    def _show_window(window) -> bool:
        try:
            if window.is_minimized():
                window.restore()
                time_module.sleep(0.8)
            window.set_focus()
        except Exception:
            return False
        return True

    def _main_window(self):
        candidates = [
            window for window in self._feishu_windows() if self._text(window) == "飞书"
        ]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _registered_feishu_executable() -> str | None:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, FEISHU_PROTOCOL_COMMAND_KEY
            ) as key:
                command = winreg.QueryValueEx(key, "")[0]
        except OSError:
            return None
        if not isinstance(command, str):
            return None
        match = EXECUTABLE_FROM_COMMAND_PATTERN.match(command)
        if match is None:
            return None
        executable = os.path.expandvars(match.group(1) or match.group(2))
        if (
            os.path.basename(executable).casefold() != FEISHU_EXECUTABLE
            or not os.path.isfile(executable)
        ):
            return None
        return executable

    def _feishu_windows(self) -> list[object]:
        candidates = []
        for window in Desktop(backend="uia").windows(control_type="Window"):
            try:
                executable = os.path.basename(
                    process_module(window.process_id())
                ).casefold()
                if (
                    window.class_name() == "Chrome_WidgetWin_1"
                    and self._text(window) in FEISHU_WINDOW_TITLES
                    and executable == FEISHU_EXECUTABLE
                ):
                    candidates.append(window)
            except Exception:
                continue
        return candidates

    def _attendance_page(
        self, day: date, *, require_trusted: bool
    ) -> tuple[object, _AttendanceContext] | None:
        candidates = []
        for window in self._feishu_windows():
            context = self._attendance_context(
                window, day, require_trusted=require_trusted
            )
            if context is not None:
                candidates.append((window, context))
        return candidates[0] if len(candidates) == 1 else None

    def _attendance_context(
        self, window, day: date, *, require_trusted: bool
    ) -> _AttendanceContext | None:
        candidates: dict[tuple[object, ...], _AttendanceContext] = {}
        anchor_controls = [
            control
            for control in window.descendants()
            if self._text(control) in ("考勤打卡", CHECKOUT_BUTTON_TEXT)
            or any(pattern.search(self._text(control)) for pattern in SUCCESS_PATTERNS)
            or BARE_CLOCKED_PATTERN.fullmatch(self._text(control))
        ]
        for anchor in anchor_controls:
            current = anchor
            for _ in range(8):
                try:
                    current = current.parent()
                    descendants = current.descendants()
                except Exception:
                    break
                texts = [self._text(current)]
                texts.extend(
                    text for control in descendants if (text := self._text(control))
                )
                window_title = self._text(window)
                if window_title and window_title not in texts:
                    texts.append(window_title)
                page_date = self._extract_page_date(texts, day.year)
                if page_date != day:
                    continue
                if not self._has_attendance_structure(texts):
                    continue
                raw_buttons = self._checkout_buttons(current, descendants)
                buttons = self._deduplicate_controls(raw_buttons, window)
                if buttons is None:
                    continue
                has_success = self._has_independent_checkout_record(texts) or any(
                    pattern.search(text)
                    for pattern in SUCCESS_PATTERNS
                    for text in texts
                )
                if not buttons and not has_success:
                    continue
                container_runtime_id = self._runtime_identity(current, window)
                if not container_runtime_id:
                    continue
                container_id = self._persistent_path(current, window)
                if not container_id:
                    continue
                if require_trusted and container_id != self.trusted_container_fingerprint:
                    continue
                button_id = (
                    self._persistent_path(buttons[0], window) if len(buttons) == 1 else ""
                )
                button_runtime_id = (
                    self._runtime_identity(buttons[0], window)
                    if len(buttons) == 1
                    else ()
                )
                candidates[container_runtime_id] = _AttendanceContext(
                    texts=texts,
                    buttons=buttons,
                    container_id=container_id,
                    button_id=button_id,
                    container_runtime_id=container_runtime_id,
                    button_runtime_id=button_runtime_id,
                )
                break
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    def _checkout_buttons(
        self, window, descendants: Iterable[object] | None = None
    ) -> list[object]:
        controls = descendants if descendants is not None else window.descendants()
        matches = []
        for control in controls:
            try:
                if (
                    control.element_info.control_type in {"Button", "Text"}
                    and self._text(control) == CHECKOUT_BUTTON_TEXT
                ):
                    matches.append(control)
            except Exception:
                continue
        return matches

    def _open_attendance_entry(self, window) -> bool:
        entry = self._attendance_entry(window)
        if entry is None:
            return False
        return self._activate_navigation_control(entry)

    @staticmethod
    def _activate_navigation_control(control) -> bool:
        try:
            control_type = control.element_info.control_type
            if control_type == "Button":
                control.invoke()
            else:
                control.click_input()
        except Exception:
            return False
        return True

    def _attendance_entry(self, window):
        controls = window.descendants()
        pinned_matches = []
        for control in controls:
            try:
                text = self._text(control)
                if (
                    text in ATTENDANCE_ENTRY_NAMES
                    and control.element_info.control_type == "TabItem"
                    and control.is_visible()
                    and control.is_enabled()
                ):
                    pinned_matches.append(control)
            except Exception:
                continue
        unique_pinned = self._deduplicate_controls(pinned_matches, window)
        if unique_pinned is None or len(unique_pinned) > 1:
            return None
        if len(unique_pinned) == 1:
            return unique_pinned[0]

        matches = []
        for control in controls:
            try:
                if (
                    self._text(control) in ATTENDANCE_ENTRY_NAMES
                    and control.is_visible()
                    and control.is_enabled()
                ):
                    matches.append(control)
            except Exception:
                continue
        unique_matches = self._deduplicate_controls(matches, window)
        if unique_matches is None or len(unique_matches) != 1:
            return None
        return unique_matches[0]

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
            check_in_time = parse_unique_check_in_time(
                self._check_in_source_texts(texts)
            )
        except ValueError as exc:
            blocking_reason = str(exc)

        for message in BLOCKING_MESSAGES:
            if any(message in text for text in texts):
                blocking_reason = f"页面提示：{message}"
                break

        already_clocked_out = self._has_independent_checkout_record(texts) or any(
            pattern.search(text) for pattern in SUCCESS_PATTERNS for text in texts
        )
        enabled = False
        if len(buttons) == 1:
            try:
                enabled = bool(buttons[0].is_enabled() and buttons[0].is_visible())
            except Exception:
                enabled = False

        page_date = self._extract_page_date(texts, day.year)
        if not self._has_attendance_marker(texts) or page_date != day:
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
            re.compile(r"(?<!\d)(\d{4})([-/.])(\d{1,2})\2(\d{1,2})日?(?!\d)"),
            re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日(?!\d)"),
            re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日(?!\d)"),
        )
        candidates: set[date] = set()
        for text_value in texts:
            for index, pattern in enumerate(patterns):
                for match in pattern.finditer(text_value):
                    try:
                        if index == 0:
                            candidates.add(
                                date(
                                    int(match.group(1)),
                                    int(match.group(3)),
                                    int(match.group(4)),
                                )
                            )
                        elif index == 1:
                            candidates.add(
                                date(
                                    int(match.group(1)),
                                    int(match.group(2)),
                                    int(match.group(3)),
                                )
                            )
                        else:
                            candidates.add(
                                date(default_year, int(match.group(1)), int(match.group(2)))
                            )
                    except ValueError:
                        return None
        return next(iter(candidates)) if len(candidates) == 1 else None

    @staticmethod
    def _has_attendance_marker(texts: Iterable[str]) -> bool:
        return any(text in ATTENDANCE_PAGE_NAMES for text in texts)

    @staticmethod
    def _has_attendance_structure(texts: Iterable[str]) -> bool:
        values = list(texts)
        if "考勤打卡" in values:
            return any("上班打卡" in text for text in values)
        if "假勤" in values:
            bounds = FeishuUiaAdapter._independent_section_bounds(values)
            return bounds is not None
        return False

    @staticmethod
    def _independent_section_bounds(texts: list[str]) -> tuple[int, int] | None:
        check_in_headers = [
            index for index, text in enumerate(texts) if text.startswith("应上班")
        ]
        check_out_headers = [
            index for index, text in enumerate(texts) if text.startswith("应下班")
        ]
        if len(check_in_headers) != 1 or len(check_out_headers) != 1:
            return None
        start, end = check_in_headers[0], check_out_headers[0]
        return (start, end) if start < end else None

    @classmethod
    def _check_in_source_texts(cls, texts: list[str]) -> list[str]:
        if "假勤" not in texts:
            return texts
        bounds = cls._independent_section_bounds(texts)
        if bounds is None:
            return texts
        start, end = bounds
        normalized = list(texts)
        for text in texts[start + 1 : end]:
            match = BARE_CLOCKED_PATTERN.fullmatch(text)
            if match is not None:
                normalized.append(f"上班已打卡 {match.group(1)}")
        return normalized

    @classmethod
    def _has_independent_checkout_record(cls, texts: list[str]) -> bool:
        if "假勤" not in texts:
            return False
        bounds = cls._independent_section_bounds(texts)
        if bounds is None:
            return False
        _, end = bounds
        return any(
            BARE_CLOCKED_PATTERN.fullmatch(text) is not None
            for text in texts[end + 1 :]
        )

    @staticmethod
    def _invoke_checkout_control(control) -> None:
        control_type = control.element_info.control_type
        if control_type == "Button":
            control.invoke()
            return
        if control_type == "Text":
            control.click_input()
            return
        raise RuntimeError("下班打卡控件类型不受支持")

    @classmethod
    def _persistent_path(cls, control, root) -> str:
        try:
            root_identity = cls._runtime_identity(root, root)
            if not root_identity:
                return ""
            parts: list[str] = []
            current = control
            for _ in range(64):
                current_identity = cls._runtime_identity(current, root)
                if not current_identity:
                    return ""
                key = cls._static_control_key(current)
                if current_identity == root_identity:
                    parts.append("|".join(key))
                    break
                parent = current.parent()
                siblings = [
                    sibling
                    for sibling in parent.children()
                    if cls._static_control_key(sibling) == key
                ]
                matching_indexes = [
                    index
                    for index, sibling in enumerate(siblings)
                    if cls._runtime_identity(sibling, root) == current_identity
                ]
                if len(matching_indexes) != 1:
                    return ""
                parts.append("|".join((*key, str(matching_indexes[0]))))
                current = parent
            else:
                return ""
            raw = ">".join(reversed(parts))
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
        except Exception:
            return ""

    @staticmethod
    def _static_control_key(control) -> tuple[str, str, str, str]:
        info = control.element_info
        return (
            str(getattr(info, "control_type", "")),
            str(getattr(info, "class_name", "")),
            str(getattr(info, "automation_id", "")),
            str(getattr(info, "framework_id", "")),
        )

    @staticmethod
    def _runtime_identity(control, root) -> tuple[object, ...]:
        try:
            runtime_id = getattr(control.element_info, "runtime_id", None)
            if not runtime_id:
                return ()
            handle = int(getattr(root, "handle", 0))
            process_id = int(root.process_id())
            if not handle or not process_id:
                return ()
            return (handle, process_id, *tuple(runtime_id))
        except Exception:
            return ()

    @classmethod
    def _deduplicate_controls(cls, controls: Iterable[object], root) -> list[object] | None:
        unique: dict[tuple[object, ...], object] = {}
        for control in controls:
            identity = cls._runtime_identity(control, root)
            if not identity:
                return None
            unique.setdefault(identity, control)
        return list(unique.values())

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
        self._pending_container_runtime_id = ()
        self._pending_button_runtime_id = ()
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
