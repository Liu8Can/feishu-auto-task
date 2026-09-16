from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

import clockout.feishu_uia as feishu_uia
from clockout.core import PunchAction
from clockout.feishu_uia import ClickToken, ClockoutClickError, FeishuUiaAdapter


@pytest.fixture(autouse=True)
def forbid_real_desktop_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("测试不得访问真实飞书或启动系统协议")

    monkeypatch.setattr(feishu_uia, "Desktop", fail)
    monkeypatch.setattr(feishu_uia.os, "startfile", fail)


class DummyButton:
    def __init__(self, enabled: bool = True, visible: bool = True) -> None:
        self.enabled = enabled
        self.visible = visible
        self.invoked = False

    def is_enabled(self) -> bool:
        return self.enabled

    def is_visible(self) -> bool:
        return self.visible

    def window_text(self) -> str:
        return "下班打卡"

    def invoke(self) -> None:
        self.invoked = True


class FakeControl:
    def __init__(
        self,
        control_type: str,
        runtime_id: tuple[int, ...],
        *,
        text: str = "",
        parent: "FakeControl | None" = None,
    ) -> None:
        self.element_info = SimpleNamespace(
            control_type=control_type,
            class_name="",
            automation_id="",
            framework_id="Chrome",
            runtime_id=runtime_id,
        )
        self._text = text
        self._parent = parent
        self._children: list[FakeControl] = []
        self.handle = 100 if control_type == "Window" else 0
        self.invoke_count = 0
        self.click_input_count = 0
        self.focus_count = 0
        self.minimized = False
        self.restore_count = 0
        if parent is not None:
            parent._children.append(self)

    def parent(self) -> "FakeControl":
        assert self._parent is not None
        return self._parent

    def children(self) -> list["FakeControl"]:
        return list(self._children)

    def descendants(self) -> list["FakeControl"]:
        result: list[FakeControl] = []
        for child in self._children:
            result.append(child)
            result.extend(child.descendants())
        return result

    def window_text(self) -> str:
        return self._text

    def is_enabled(self) -> bool:
        return True

    def is_visible(self) -> bool:
        return True

    def process_id(self) -> int:
        return 200

    def invoke(self) -> None:
        self.invoke_count += 1

    def click_input(self) -> None:
        self.click_input_count += 1

    def is_minimized(self) -> bool:
        return self.minimized

    def restore(self) -> None:
        self.minimized = False
        self.restore_count += 1

    def set_focus(self) -> None:
        self.focus_count += 1

    def rectangle(self):
        return SimpleNamespace(left=10, top=10, right=110, bottom=50)


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds


def _fake_attendance_context(container_id: str = "attendance-container"):
    return feishu_uia._AttendanceContext(
        texts=["考勤打卡", "9月15日", "上班打卡 09:03", "下班打卡"],
        buttons=[],
        container_id=container_id,
        button_id="",
        container_runtime_id=(100, 200, 10),
        button_runtime_id=(),
    )


def _add_attendance_container(
    root: FakeControl, base_runtime_id: int
) -> tuple[FakeControl, FakeControl]:
    container = FakeControl("Pane", (base_runtime_id,), parent=root)
    FakeControl("Text", (base_runtime_id + 1,), text="考勤打卡", parent=container)
    FakeControl("Text", (base_runtime_id + 2,), text="9月15日", parent=container)
    FakeControl("Text", (base_runtime_id + 3,), text="上班打卡 09:03", parent=container)
    button = FakeControl(
        "Button", (base_runtime_id + 4,), text="下班打卡", parent=container
    )
    return container, button


def test_navigation_reuses_attendance_page_that_is_already_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=True)
    page = (FakeControl("Window", (1,), text="假勤"), _fake_attendance_context())
    monkeypatch.setattr(adapter, "_attendance_page", lambda *args, **kwargs: page)
    monkeypatch.setattr(
        adapter,
        "launch_feishu",
        lambda: pytest.fail("考勤页已打开时不应再次唤醒飞书"),
    )

    result = adapter._find_or_open_attendance_page(
        date(2026, 9, 15), require_trusted=False
    )

    assert result is page


def test_explicit_open_attendance_page_navigates_when_automatic_open_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False, navigation_wait=0)
    main_window = FakeControl("Window", (1,), text="飞书")
    entry = FakeControl("Button", (2,), text="假勤", parent=main_window)
    page = (FakeControl("Window", (3,), text="假勤"), _fake_attendance_context())
    wake_count = 0

    def wake() -> None:
        nonlocal wake_count
        wake_count += 1

    monkeypatch.setattr(feishu_uia, "is_interactive_desktop", lambda: True)
    monkeypatch.setattr(adapter, "launch_feishu", wake)
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: page if entry.invoke_count else None,
    )
    monkeypatch.setattr(adapter, "_main_window", lambda: main_window)

    opened = adapter.open_attendance_page(date(2026, 9, 15))

    assert opened
    assert wake_count == 1
    assert entry.invoke_count == 1
    assert page[0].focus_count == 1


def test_navigation_prefers_pinned_attendance_tab_over_duplicate_page_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=True, navigation_wait=0)
    main_window = FakeControl("Window", (1,), text="飞书")
    first_text = FakeControl("Text", (2,), text="假勤", parent=main_window)
    second_text = FakeControl("Text", (3,), text="假勤", parent=main_window)
    pinned_entry = FakeControl("TabItem", (4,), text="假勤", parent=main_window)
    page = (FakeControl("Window", (5,), text="假勤"), _fake_attendance_context())

    monkeypatch.setattr(adapter, "launch_feishu", lambda: None)
    monkeypatch.setattr(adapter, "_main_window", lambda: main_window)
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: page if pinned_entry.click_input_count else None,
    )

    result = adapter._find_or_open_attendance_page(
        date(2026, 9, 15), require_trusted=False
    )

    assert result is page
    assert pinned_entry.click_input_count == 1
    assert first_text.click_input_count == 0
    assert second_text.click_input_count == 0


def test_launch_feishu_starts_registered_executable_without_invalid_deep_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=True)
    started: list[str] = []
    executable = r"D:\Program Files\Feishu\app\Feishu.exe"

    monkeypatch.setattr(adapter, "_main_window", lambda: None)
    monkeypatch.setattr(
        adapter, "_registered_feishu_executable", lambda: executable
    )
    monkeypatch.setattr(feishu_uia.os, "startfile", started.append)

    adapter.launch_feishu()

    assert started == [executable]


def test_explicit_open_restores_and_focuses_existing_attendance_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=True)
    window = FakeControl("Window", (1,), text="假勤")
    window.minimized = True
    page = (window, _fake_attendance_context())

    monkeypatch.setattr(feishu_uia, "is_interactive_desktop", lambda: True)
    monkeypatch.setattr(adapter, "_attendance_page", lambda *args, **kwargs: page)

    assert adapter.open_attendance_page(date(2026, 9, 15))
    assert window.restore_count == 1
    assert window.focus_count == 1


def test_navigation_starts_feishu_waits_for_main_window_and_opens_unique_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=True, navigation_wait=0.6)
    clock = FakeClock()
    main_window = FakeControl("Window", (1,), text="飞书")
    entry = FakeControl("Button", (2,), text="假勤", parent=main_window)
    page = (FakeControl("Window", (3,), text="假勤"), _fake_attendance_context())
    wake_count = 0

    def wake() -> None:
        nonlocal wake_count
        wake_count += 1

    monkeypatch.setattr(feishu_uia.time_module, "monotonic", clock.monotonic)
    monkeypatch.setattr(feishu_uia.time_module, "sleep", clock.sleep)
    monkeypatch.setattr(adapter, "launch_feishu", wake)
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: page if entry.invoke_count else None,
    )
    monkeypatch.setattr(
        adapter,
        "_main_window",
        lambda: main_window if clock.current >= 0.2 else None,
    )

    result = adapter._find_or_open_attendance_page(
        date(2026, 9, 15), require_trusted=False
    )

    assert result is page
    assert wake_count == 1
    assert entry.invoke_count == 1


def test_navigation_waits_for_attendance_entry_after_main_window_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=True, navigation_wait=0.6)
    clock = FakeClock()
    main_window = FakeControl("Window", (1,), text="飞书")
    entry = FakeControl("Button", (2,), text="假勤", parent=main_window)
    page = (FakeControl("Window", (3,), text="假勤"), _fake_attendance_context())

    monkeypatch.setattr(feishu_uia.time_module, "monotonic", clock.monotonic)
    monkeypatch.setattr(feishu_uia.time_module, "sleep", clock.sleep)
    monkeypatch.setattr(adapter, "launch_feishu", lambda: None)
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: page if entry.invoke_count else None,
    )
    monkeypatch.setattr(adapter, "_main_window", lambda: main_window)
    monkeypatch.setattr(
        main_window,
        "descendants",
        lambda: [entry] if clock.current >= 0.2 else [],
    )

    result = adapter._find_or_open_attendance_page(
        date(2026, 9, 15), require_trusted=False
    )

    assert result is page
    assert clock.current == pytest.approx(0.2)
    assert entry.invoke_count == 1


def test_calibration_can_navigate_before_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=True, navigation_wait=0)
    main_window = FakeControl("Window", (1,), text="飞书")
    entry = FakeControl("Button", (2,), text="考勤打卡", parent=main_window)
    context = _fake_attendance_context("new-binding")
    page = (FakeControl("Window", (3,), text="假勤"), context)
    wake_count = 0

    def wake() -> None:
        nonlocal wake_count
        wake_count += 1

    monkeypatch.setattr(feishu_uia, "is_interactive_desktop", lambda: True)
    monkeypatch.setattr(adapter, "launch_feishu", wake)
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: page if entry.invoke_count else None,
    )
    monkeypatch.setattr(adapter, "_main_window", lambda: main_window)

    fingerprint = adapter.calibrate_current_page(date(2026, 9, 15))

    assert fingerprint == "new-binding"
    assert adapter.trusted_container_fingerprint == "new-binding"
    assert wake_count == 1
    assert entry.invoke_count == 1


def test_navigation_rejects_multiple_attendance_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=True, navigation_wait=0)
    main_window = FakeControl("Window", (1,), text="飞书")
    first = FakeControl("Button", (2,), text="假勤", parent=main_window)
    second = FakeControl("Button", (3,), text="考勤打卡", parent=main_window)
    wake_count = 0

    def wake() -> None:
        nonlocal wake_count
        wake_count += 1

    monkeypatch.setattr(adapter, "launch_feishu", wake)
    monkeypatch.setattr(adapter, "_attendance_page", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_main_window", lambda: main_window)

    result = adapter._find_or_open_attendance_page(
        date(2026, 9, 15), require_trusted=False
    )

    assert result is None
    assert wake_count == 1
    assert first.invoke_count == 0
    assert second.invoke_count == 0


def test_navigation_times_out_when_feishu_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=True, navigation_wait=0.5)
    clock = FakeClock()
    wake_count = 0

    def wake() -> None:
        nonlocal wake_count
        wake_count += 1

    monkeypatch.setattr(feishu_uia.time_module, "monotonic", clock.monotonic)
    monkeypatch.setattr(feishu_uia.time_module, "sleep", clock.sleep)
    monkeypatch.setattr(adapter, "launch_feishu", wake)
    monkeypatch.setattr(adapter, "_attendance_page", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_main_window", lambda: None)

    result = adapter._find_or_open_attendance_page(
        date(2026, 9, 15), require_trusted=False
    )

    assert result is None
    assert wake_count == 1
    assert clock.current == pytest.approx(0.5)


def test_snapshot_extracts_unique_check_in_and_button() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot(
        ["考勤打卡", "9月15日", "上班已打卡 09:03", "下班打卡"],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="clockout-button",
    )

    assert snapshot.check_in_time is not None
    assert snapshot.check_in_time.strftime("%H:%M") == "09:03"
    assert snapshot.button_count == 1
    assert snapshot.button_enabled
    assert snapshot.blocking_reason is None
    assert snapshot.page_date == date(2026, 9, 15)
    assert snapshot.container_id == "attendance-container"
    assert snapshot.button_id == "clockout-button"
    assert snapshot.action is PunchAction.CHECK_OUT


def test_check_in_snapshot_before_punch_has_a_valid_unique_target() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot_for_action(
        [
            "假勤",
            "2026.09.15",
            "应上班 08:50",
            "上班打卡",
            "应下班 18:50",
            "下班打卡",
        ],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="checkin-target",
        action=PunchAction.CHECK_IN,
    )

    assert snapshot.action is PunchAction.CHECK_IN
    assert not snapshot.action_completed
    assert snapshot.check_in_time is None
    assert snapshot.button_count == 1
    assert snapshot.button_enabled
    assert snapshot.blocking_reason is None


def test_check_in_snapshot_after_punch_uses_only_check_in_record() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot_for_action(
        [
            "假勤",
            "2026.09.15",
            "应上班 08:50",
            "已打卡 08:53",
            "应下班 18:50",
            "已打卡 18:52",
        ],
        [],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="",
        action=PunchAction.CHECK_IN,
    )

    assert snapshot.action_completed
    assert snapshot.check_in_time is not None
    assert snapshot.check_in_time.strftime("%H:%M") == "08:53"
    assert snapshot.already_clocked_out
    assert snapshot.blocking_reason is None


def test_check_in_context_ignores_checkout_button_and_record() -> None:
    root = FakeControl("Window", (1,), text="假勤")
    container = FakeControl("Document", (10,), parent=root)
    FakeControl("Text", (11,), text="2026.09.15", parent=container)
    FakeControl("Text", (12,), text="应上班 08:50", parent=container)
    FakeControl("Text", (13,), text="已打卡 08:51", parent=container)
    check_in = FakeControl("Button", (14,), text="上班打卡", parent=container)
    FakeControl("Text", (15,), text="应下班 18:50", parent=container)
    FakeControl("Text", (16,), text="已打卡 18:51", parent=container)
    check_out = FakeControl("Button", (17,), text="下班打卡", parent=container)
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    context = adapter._attendance_context(
        root,
        date(2026, 9, 15),
        require_trusted=False,
        action=PunchAction.CHECK_IN,
    )

    assert context is not None
    assert context.buttons == [check_in]
    assert check_out not in context.buttons
    snapshot = adapter._build_snapshot_for_action(
        context.texts,
        context.buttons,
        day=date(2026, 9, 15),
        container_id=context.container_id,
        button_id=context.button_id,
        action=PunchAction.CHECK_IN,
    )
    assert snapshot.check_in_time is not None
    assert snapshot.check_in_time.strftime("%H:%M") == "08:51"


def test_check_in_snapshot_blocks_duplicate_buttons_in_check_in_section() -> None:
    root = FakeControl("Window", (1,), text="假勤")
    container = FakeControl("Document", (10,), parent=root)
    FakeControl("Text", (11,), text="2026.09.15", parent=container)
    FakeControl("Text", (12,), text="应上班 08:50", parent=container)
    FakeControl("Button", (13,), text="上班打卡", parent=container)
    FakeControl("Text", (14,), text="上班打卡", parent=container)
    FakeControl("Text", (15,), text="应下班 18:50", parent=container)
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    context = adapter._attendance_context(
        root,
        date(2026, 9, 15),
        require_trusted=False,
        action=PunchAction.CHECK_IN,
    )

    assert context is not None
    snapshot = adapter._build_snapshot_for_action(
        context.texts,
        context.buttons,
        day=date(2026, 9, 15),
        container_id=context.container_id,
        button_id=context.button_id,
        action=PunchAction.CHECK_IN,
    )

    assert not snapshot.action_completed
    assert snapshot.button_count == 2
    assert snapshot.blocking_reason == "未找到唯一的上班打卡按钮"


def test_check_in_snapshot_blocks_unavailable_button() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot_for_action(
        ["假勤", "9月15日", "应上班 08:50", "上班打卡", "应下班 18:50"],
        [DummyButton(enabled=False)],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="checkin-target",
        action=PunchAction.CHECK_IN,
    )

    assert snapshot.button_count == 1
    assert not snapshot.button_enabled
    assert snapshot.blocking_reason == "上班打卡按钮不可用"


def test_check_in_context_rejects_button_from_checkout_section() -> None:
    root = FakeControl("Window", (1,), text="假勤")
    container = FakeControl("Document", (10,), parent=root)
    FakeControl("Text", (11,), text="2026.09.15", parent=container)
    FakeControl("Text", (12,), text="应上班 08:50", parent=container)
    FakeControl("Text", (13,), text="未打卡", parent=container)
    FakeControl("Text", (14,), text="应下班 18:50", parent=container)
    wrong_target = FakeControl("Button", (15,), text="上班打卡", parent=container)
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    context = adapter._attendance_context(
        root,
        date(2026, 9, 15),
        require_trusted=False,
        action=PunchAction.CHECK_IN,
    )

    assert context is not None
    assert wrong_target not in context.buttons
    snapshot = adapter._build_snapshot_for_action(
        context.texts,
        context.buttons,
        day=date(2026, 9, 15),
        container_id=context.container_id,
        button_id=context.button_id,
        action=PunchAction.CHECK_IN,
    )
    assert snapshot.blocking_reason == "未找到唯一的上班打卡按钮"


def test_check_in_snapshot_blocks_conflicting_page_dates() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot_for_action(
        [
            "假勤",
            "9月14日",
            "9月15日",
            "应上班 08:50",
            "上班打卡",
            "应下班 18:50",
        ],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="checkin-target",
        action=PunchAction.CHECK_IN,
    )

    assert snapshot.page_date is None
    assert snapshot.blocking_reason == "未确认当前页面为今天的考勤打卡页"


def test_check_in_snapshot_preserves_page_blocking_message() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot_for_action(
        [
            "假勤",
            "9月15日",
            "应上班 08:50",
            "上班打卡",
            "应下班 18:50",
            "需要人脸识别",
        ],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="checkin-target",
        action=PunchAction.CHECK_IN,
    )

    assert snapshot.blocking_reason == "页面提示：需要人脸识别"


def test_check_in_planned_time_is_not_treated_as_completed() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot_for_action(
        [
            "假勤",
            "9月15日",
            "应上班 08:50",
            "上班打卡 08:50",
            "上班打卡",
            "应下班 18:50",
        ],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="checkin-target",
        action=PunchAction.CHECK_IN,
    )

    assert snapshot.check_in_time is None
    assert not snapshot.action_completed
    assert snapshot.blocking_reason is None


def test_negated_face_check_message_does_not_block_check_in() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot_for_action(
        [
            "假勤",
            "9月15日",
            "应上班 08:50",
            "上班打卡",
            "应下班 18:50",
            "当前不需要人脸识别",
        ],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="checkin-target",
        action=PunchAction.CHECK_IN,
    )

    assert snapshot.blocking_reason is None
    assert not snapshot.action_completed


def test_independent_attendance_window_snapshot_is_supported() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)
    target = FakeControl("Text", (20,), text="下班打卡")

    snapshot = adapter._build_snapshot(
        [
            "假勤",
            "2026.09.15",
            "应上班 08:50",
            "已打卡 08:50",
            "应下班 18:50",
            "下班打卡",
        ],
        [target],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="clockout-target",
    )

    assert snapshot.check_in_time is not None
    assert snapshot.check_in_time.strftime("%H:%M") == "08:50"
    assert snapshot.button_count == 1
    assert snapshot.button_enabled
    assert snapshot.blocking_reason is None
    assert snapshot.page_date == date(2026, 9, 15)


def test_independent_checkout_record_is_not_used_as_check_in() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)
    target = FakeControl("Text", (20,), text="下班打卡")

    snapshot = adapter._build_snapshot(
        [
            "假勤",
            "2026.09.15",
            "应上班 08:50",
            "未打卡",
            "应下班 18:50",
            "已打卡 18:51",
            "下班打卡",
        ],
        [target],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="clockout-target",
    )

    assert snapshot.check_in_time is None
    assert snapshot.already_clocked_out
    assert snapshot.blocking_reason == "未识别到上班打卡时间"


def test_independent_check_in_and_check_out_records_are_separated() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot(
        [
            "假勤",
            "2026.09.15",
            "应上班 08:50",
            "已打卡 08:50",
            "应下班 18:50",
            "已打卡 18:51",
        ],
        [],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="",
    )

    assert snapshot.check_in_time is not None
    assert snapshot.check_in_time.strftime("%H:%M") == "08:50"
    assert snapshot.already_clocked_out


def test_independent_attendance_window_context_preserves_target_identity() -> None:
    root = FakeControl("Window", (1,), text="假勤")
    container = FakeControl("Document", (10,), parent=root)
    FakeControl("Text", (11,), text="2026.09.15", parent=container)
    FakeControl("Text", (12,), text="应上班 08:50", parent=container)
    FakeControl("Text", (13,), text="已打卡 08:50", parent=container)
    FakeControl("Text", (14,), text="应下班 18:50", parent=container)
    target = FakeControl("Text", (15,), text="下班打卡", parent=container)
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    context = adapter._attendance_context(
        root, date(2026, 9, 15), require_trusted=False
    )

    assert context is not None
    assert context.buttons == [target]
    assert context.container_id
    assert context.button_id
    assert context.container_runtime_id
    assert context.button_runtime_id

    adapter.trusted_container_fingerprint = "different-container"
    assert (
        adapter._attendance_context(root, date(2026, 9, 15), require_trusted=True)
        is None
    )


def test_attendance_page_is_recognized_before_checkout_button_appears() -> None:
    root = FakeControl("Window", (1,), text="假勤")
    container = FakeControl("Document", (10,), parent=root)
    FakeControl("Text", (11,), text="2026.09.15", parent=container)
    FakeControl("Text", (12,), text="应上班 08:00", parent=container)
    FakeControl("Text", (13,), text="上班打卡", parent=container)
    FakeControl("Text", (14,), text="应下班 18:00", parent=container)
    FakeControl("Text", (15,), text="未开始", parent=container)
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    context = adapter._attendance_context(
        root, date(2026, 9, 15), require_trusted=False
    )

    assert context is not None
    assert context.buttons == []
    assert context.container_id


def test_multiple_attendance_windows_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = []
    for root_id in (1, 2):
        root = FakeControl("Window", (root_id,), text="假勤")
        container = FakeControl("Document", (root_id * 10,), parent=root)
        FakeControl("Text", (root_id * 10 + 1,), text="2026.09.15", parent=container)
        FakeControl("Text", (root_id * 10 + 2,), text="应上班 08:50", parent=container)
        FakeControl("Text", (root_id * 10 + 3,), text="已打卡 08:50", parent=container)
        FakeControl("Text", (root_id * 10 + 4,), text="应下班 18:50", parent=container)
        FakeControl("Text", (root_id * 10 + 5,), text="下班打卡", parent=container)
        root.handle = 100 + root_id
        roots.append(root)
    adapter = FeishuUiaAdapter(auto_open_workbench=False)
    monkeypatch.setattr(adapter, "_feishu_windows", lambda: roots)

    assert adapter._attendance_page(date(2026, 9, 15), require_trusted=False) is None


def test_text_checkout_target_uses_click_input() -> None:
    target = FakeControl("Text", (20,), text="下班打卡")

    FeishuUiaAdapter._invoke_checkout_control(target)

    assert target.click_input_count == 1
    assert target.invoke_count == 0


def _prepare_text_click(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FeishuUiaAdapter, FakeControl, FakeControl, str]:
    root = FakeControl("Window", (1,), text="假勤")
    container = FakeControl("Document", (10,), parent=root)
    FakeControl("Text", (11,), text="2026.09.15", parent=container)
    FakeControl("Text", (12,), text="应上班 08:50", parent=container)
    FakeControl("Text", (13,), text="已打卡 08:50", parent=container)
    FakeControl("Text", (14,), text="应下班 18:50", parent=container)
    target = FakeControl("Text", (15,), text="下班打卡", parent=container)
    adapter = FeishuUiaAdapter(auto_open_workbench=False)
    context = adapter._attendance_context(
        root, date(2026, 9, 15), require_trusted=False
    )
    assert context is not None
    adapter.trusted_container_fingerprint = context.container_id
    snapshot = adapter._build_snapshot(
        context.texts,
        context.buttons,
        day=date(2026, 9, 15),
        container_id=context.container_id,
        button_id=context.button_id,
    )
    adapter._legacy_checkout_observation = (
        date(2026, 9, 15),
        snapshot.signature,
    )
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: (root, context),
    )
    monkeypatch.setattr(feishu_uia, "is_interactive_desktop", lambda: True)
    return adapter, root, target, snapshot.signature


def test_text_click_is_blocked_when_window_activation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, root, target, signature = _prepare_text_click(monkeypatch)

    def fail_focus() -> None:
        raise RuntimeError("activation failed")

    root.set_focus = fail_focus  # type: ignore[method-assign]

    with pytest.raises(ClockoutClickError) as error:
        adapter.click_clock_out(signature)
    assert not error.value.invocation_started
    assert target.click_input_count == 0


def test_text_click_is_blocked_when_target_is_occluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, root, target, signature = _prepare_text_click(monkeypatch)
    other_root = FakeControl("Window", (90,), text="其他窗口")
    other_root.handle = 999
    occluding_control = FakeControl("Pane", (91,), parent=other_root)
    desktop = SimpleNamespace(from_point=lambda x, y: occluding_control)
    monkeypatch.setattr(feishu_uia, "Desktop", lambda **kwargs: desktop)
    monkeypatch.setattr(
        feishu_uia.win32gui, "GetForegroundWindow", lambda: root.handle
    )

    with pytest.raises(RuntimeError):
        adapter.click_clock_out(signature)
    assert root.focus_count == 1
    assert target.click_input_count == 0


def test_text_click_is_blocked_by_control_in_same_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, root, target, signature = _prepare_text_click(monkeypatch)
    overlay = FakeControl("Pane", (80,), parent=root)
    desktop = SimpleNamespace(from_point=lambda x, y: overlay)
    monkeypatch.setattr(feishu_uia, "Desktop", lambda **kwargs: desktop)
    monkeypatch.setattr(
        feishu_uia.win32gui, "GetForegroundWindow", lambda: root.handle
    )

    with pytest.raises(RuntimeError):
        adapter.click_clock_out(signature)
    assert root.focus_count == 1
    assert target.click_input_count == 0


def test_text_click_succeeds_only_after_all_final_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, root, target, signature = _prepare_text_click(monkeypatch)
    desktop = SimpleNamespace(from_point=lambda x, y: target)
    monkeypatch.setattr(feishu_uia, "Desktop", lambda **kwargs: desktop)
    monkeypatch.setattr(
        feishu_uia.win32gui, "GetForegroundWindow", lambda: root.handle
    )

    adapter.click_clock_out(signature)

    assert root.focus_count == 1
    assert target.click_input_count == 1
    assert target.invoke_count == 0


def test_text_click_reports_when_physical_invocation_has_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, root, target, signature = _prepare_text_click(monkeypatch)
    desktop = SimpleNamespace(from_point=lambda x, y: target)
    monkeypatch.setattr(feishu_uia, "Desktop", lambda **kwargs: desktop)
    monkeypatch.setattr(
        feishu_uia.win32gui, "GetForegroundWindow", lambda: root.handle
    )

    def fail_click() -> None:
        raise RuntimeError("physical click failed")

    target.click_input = fail_click  # type: ignore[method-assign]

    with pytest.raises(ClockoutClickError) as error:
        adapter.click_clock_out(signature)

    assert error.value.invocation_started
    assert "physical click failed" in str(error.value)


def test_text_click_revalidates_runtime_identity_after_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, root, target, signature = _prepare_text_click(monkeypatch)
    page = adapter._attendance_page(date(2026, 9, 15), require_trusted=True)
    assert page is not None
    context = page[1]
    replaced_context = feishu_uia._AttendanceContext(
        texts=context.texts,
        buttons=context.buttons,
        container_id=context.container_id,
        button_id=context.button_id,
        container_runtime_id=context.container_runtime_id,
        button_runtime_id=(100, 200, 999),
    )
    pages = iter(
        ((root, context), (root, context), (root, replaced_context))
    )
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: next(pages),
    )

    with pytest.raises(RuntimeError):
        adapter.click_clock_out(signature)
    assert root.focus_count == 1
    assert target.click_input_count == 0


def _prepare_action_token(
    monkeypatch: pytest.MonkeyPatch,
    action: PunchAction,
    *,
    mutate_on_invoke: bool = True,
    checkout_already_completed: bool = False,
) -> tuple[FeishuUiaAdapter, FakeControl, FakeControl, ClickToken]:
    root = FakeControl("Window", (1,), text="假勤")
    container = FakeControl("Document", (10,), parent=root)
    FakeControl("Text", (11,), text="2026.09.15", parent=container)
    FakeControl("Text", (12,), text="应上班 08:50", parent=container)
    if action is PunchAction.CHECK_IN:
        target = FakeControl(
            "Button", (13,), text="上班打卡", parent=container
        )
    else:
        FakeControl("Text", (13,), text="已打卡 08:50", parent=container)
        target = None
    FakeControl("Text", (14,), text="应下班 18:50", parent=container)
    if checkout_already_completed:
        FakeControl("Text", (15,), text="已打卡 18:51", parent=container)
    elif action is PunchAction.CHECK_OUT:
        target = FakeControl(
            "Button", (15,), text="下班打卡", parent=container
        )
    else:
        FakeControl("Button", (15,), text="下班打卡", parent=container)
    assert target is not None

    adapter = FeishuUiaAdapter(auto_open_workbench=False)
    initial_context = adapter._attendance_context(
        root,
        date(2026, 9, 15),
        require_trusted=False,
        action=action,
    )
    assert initial_context is not None
    adapter.trusted_container_fingerprint = initial_context.container_id

    def current_page(*args, **kwargs):
        context = adapter._attendance_context(
            root,
            date(2026, 9, 15),
            require_trusted=True,
            action=kwargs.get("action", action),
        )
        return (root, context) if context is not None else None

    monkeypatch.setattr(adapter, "_attendance_page", current_page)
    monkeypatch.setattr(feishu_uia, "is_interactive_desktop", lambda: True)
    snapshot = adapter._build_snapshot_for_action(
        initial_context.texts,
        initial_context.buttons,
        day=date(2026, 9, 15),
        container_id=initial_context.container_id,
        button_id=initial_context.button_id,
        action=action,
    )
    token = adapter.prepare_click(
        date(2026, 9, 15), action, snapshot.signature
    )

    if mutate_on_invoke:
        def complete_action() -> None:
            target.invoke_count += 1
            target.element_info.control_type = "Text"
            target._text = (
                "已打卡 08:51"
                if action is PunchAction.CHECK_IN
                else "已打卡 18:51"
            )

        target.invoke = complete_action  # type: ignore[method-assign]
    return adapter, root, target, token


@pytest.mark.parametrize("action", list(PunchAction))
def test_action_token_executes_and_verifies_its_own_action(
    monkeypatch: pytest.MonkeyPatch, action: PunchAction
) -> None:
    adapter, _, target, token = _prepare_action_token(monkeypatch, action)

    assert adapter.execute_click(token)
    assert target.invoke_count == 1
    assert adapter._click_state == "idle"


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "forged"},
        {"action": PunchAction.CHECK_IN},
        {"day": date(2026, 9, 16)},
        {"snapshot_signature": "forged"},
        {"button_runtime_id": (999,)},
    ],
)
def test_forged_or_cross_action_token_never_invokes(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    adapter, _, target, token = _prepare_action_token(
        monkeypatch, PunchAction.CHECK_OUT
    )
    forged = replace(token, **change)

    with pytest.raises(ClockoutClickError) as error:
        adapter.execute_click(forged)

    assert not error.value.invocation_started
    assert target.invoke_count == 0
    adapter.cancel_click(token)


def test_click_token_is_one_shot_and_cannot_be_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, target, token = _prepare_action_token(
        monkeypatch, PunchAction.CHECK_OUT
    )

    assert adapter.execute_click(token)
    with pytest.raises(ClockoutClickError):
        adapter.execute_click(token)

    assert target.invoke_count == 1


def test_prepare_is_serial_and_exact_cancel_releases_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, target, token = _prepare_action_token(
        monkeypatch, PunchAction.CHECK_OUT
    )

    with pytest.raises(ClockoutClickError):
        adapter.prepare_click(token.day, token.action, token.snapshot_signature)
    with pytest.raises(ClockoutClickError):
        adapter.cancel_click(replace(token, nonce="forged"))

    adapter.cancel_click(token)
    replacement = adapter.prepare_click(
        token.day, token.action, token.snapshot_signature
    )
    assert replacement.generation > token.generation
    assert target.invoke_count == 0


def test_concurrent_execute_invokes_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, target, token = _prepare_action_token(
        monkeypatch, PunchAction.CHECK_OUT
    )
    barrier = threading.Barrier(3)
    results: list[bool] = []
    errors: list[Exception] = []

    def execute() -> None:
        barrier.wait()
        try:
            results.append(adapter.execute_click(token))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert results == [True]
    assert len(errors) == 1
    assert isinstance(errors[0], ClockoutClickError)
    assert target.invoke_count == 1


def test_verification_allows_runtime_redraw_in_same_bound_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, root, target, token = _prepare_action_token(
        monkeypatch, PunchAction.CHECK_OUT, mutate_on_invoke=False
    )
    initial_page = adapter._attendance_page(
        token.day, require_trusted=True, action=token.action
    )
    assert initial_page is not None
    initial_context = initial_page[1]
    invoked = False

    def complete_with_redraw() -> None:
        nonlocal invoked
        invoked = True
        target.invoke_count += 1

    target.invoke = complete_with_redraw  # type: ignore[method-assign]
    completed_context = replace(
        initial_context,
        texts=[
            "假勤",
            "2026.09.15",
            "应上班 08:50",
            "已打卡 08:50",
            "应下班 18:50",
            "已打卡 18:51",
        ],
        buttons=[],
        button_id="",
        container_runtime_id=(100, 200, 999),
        button_runtime_id=(),
    )
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: (
            (root, completed_context)
            if invoked
            else (root, initial_context)
        ),
    )

    assert adapter.execute_click(token)
    assert target.invoke_count == 1


@pytest.mark.parametrize("action", list(PunchAction))
def test_other_action_record_does_not_confirm_success(
    monkeypatch: pytest.MonkeyPatch, action: PunchAction
) -> None:
    fake_clock = FakeClock()
    monkeypatch.setattr(feishu_uia.time_module, "monotonic", fake_clock.monotonic)
    monkeypatch.setattr(feishu_uia.time_module, "sleep", fake_clock.sleep)
    adapter, _, target, token = _prepare_action_token(
        monkeypatch,
        action,
        mutate_on_invoke=False,
        checkout_already_completed=action is PunchAction.CHECK_IN,
    )

    assert not adapter.execute_click(token)
    assert target.invoke_count == 1
    assert adapter._click_state == "idle"


def test_snapshot_blocks_multiple_check_in_times() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot(
        ["考勤打卡", "9月15日", "上班打卡 09:03", "上班已打卡 09:04", "下班打卡"],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="clockout-button",
    )

    assert snapshot.check_in_time is None
    assert snapshot.blocking_reason == "识别到多个不同的上班打卡时间"


def test_snapshot_blocks_page_warning() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot(
        ["考勤打卡", "9月15日", "上班打卡 09:03", "下班打卡", "不在考勤范围"],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="clockout-button",
    )

    assert snapshot.blocking_reason == "页面提示：不在考勤范围"


def test_snapshot_blocks_page_without_current_day() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot(
        ["考勤打卡", "9月14日", "上班打卡 09:03", "下班打卡"],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="clockout-button",
    )

    assert snapshot.blocking_reason == "未确认当前页面为今天的考勤打卡页"


def test_generic_today_text_does_not_invent_page_date() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot(
        ["考勤打卡", "今日", "上班打卡 09:03", "下班打卡"],
        [DummyButton()],
        day=date(2026, 9, 15),
        container_id="attendance-container",
        button_id="clockout-button",
    )

    assert snapshot.page_date is None
    assert snapshot.blocking_reason == "未确认当前页面为今天的考勤打卡页"


def test_conflicting_numeric_dates_are_rejected() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    assert adapter._extract_page_date(["9月14日", "9月15日"], 2026) is None


@pytest.mark.parametrize("text_value", ["2026.09/15", "2026.09.150"])
def test_malformed_full_numeric_date_is_rejected(text_value: str) -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    assert adapter._extract_page_date([text_value], 2026) is None


def test_structurally_identical_sibling_containers_have_distinct_paths() -> None:
    root = FakeControl("Window", (1,))
    first, _ = _add_attendance_container(root, 10)
    second, _ = _add_attendance_container(root, 20)

    assert FeishuUiaAdapter._persistent_path(first, root)
    assert FeishuUiaAdapter._persistent_path(first, root) != FeishuUiaAdapter._persistent_path(
        second, root
    )


def test_two_real_attendance_candidates_are_never_folded_into_one() -> None:
    root = FakeControl("Window", (1,))
    _add_attendance_container(root, 10)
    _add_attendance_container(root, 20)
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    context = adapter._attendance_context(
        root, date(2026, 9, 15), require_trusted=False
    )

    assert context is None


def test_runtime_dedup_only_removes_the_same_control_instance() -> None:
    root = FakeControl("Window", (1,))
    _, first_button = _add_attendance_container(root, 10)
    _, second_button = _add_attendance_container(root, 20)

    same = FeishuUiaAdapter._deduplicate_controls(
        [first_button, first_button], root
    )
    different = FeishuUiaAdapter._deduplicate_controls(
        [first_button, second_button], root
    )

    assert same == [first_button]
    assert different == [first_button, second_button]


def test_third_scan_rejects_replaced_runtime_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = FakeControl("Window", (1,))
    _, button = _add_attendance_container(root, 10)
    adapter = FeishuUiaAdapter(
        auto_open_workbench=False,
        trusted_container_fingerprint="same-persistent-path",
    )
    context = feishu_uia._AttendanceContext(
        texts=["考勤打卡", "9月15日", "上班打卡 09:03", "下班打卡"],
        buttons=[button],
        container_id="same-persistent-path",
        button_id="same-button-path",
        container_runtime_id=(100, 200, 99),
        button_runtime_id=(100, 200, 100),
    )
    snapshot = adapter._build_snapshot(
        context.texts,
        context.buttons,
        day=date(2026, 9, 15),
        container_id=context.container_id,
        button_id=context.button_id,
    )
    original_context = replace(
        context,
        container_runtime_id=(100, 200, 10),
        button_runtime_id=(100, 200, 11),
    )
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: (root, original_context),
    )
    monkeypatch.setattr(adapter, "_attendance_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(feishu_uia, "is_interactive_desktop", lambda: True)
    root.is_minimized = lambda: False  # type: ignore[attr-defined]

    token = adapter.prepare_click(
        date(2026, 9, 15), PunchAction.CHECK_OUT, snapshot.signature
    )
    monkeypatch.setattr(
        adapter,
        "_attendance_page",
        lambda *args, **kwargs: (root, context),
    )

    with pytest.raises(RuntimeError):
        adapter.execute_click(token)
    assert button.invoke_count == 0
