from __future__ import annotations

from datetime import date

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
