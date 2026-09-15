from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import clockout.feishu_uia as feishu_uia
from clockout.feishu_uia import FeishuUiaAdapter


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
    adapter._pending_signature = snapshot.signature
    adapter._pending_day = date(2026, 9, 15)
    adapter._pending_container_runtime_id = (100, 200, 10)
    adapter._pending_button_runtime_id = (100, 200, 11)
    monkeypatch.setattr(adapter, "_main_window", lambda: root)
    monkeypatch.setattr(adapter, "_attendance_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(feishu_uia, "is_interactive_desktop", lambda: True)
    root.is_minimized = lambda: False  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError):
        adapter.click_clock_out(snapshot.signature)
    assert button.invoke_count == 0
