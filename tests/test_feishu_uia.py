from __future__ import annotations

from clockout.feishu_uia import FeishuUiaAdapter


class DummyButton:
    def __init__(self, enabled: bool = True, visible: bool = True) -> None:
        self.enabled = enabled
        self.visible = visible

    def is_enabled(self) -> bool:
        return self.enabled

    def is_visible(self) -> bool:
        return self.visible


def test_snapshot_extracts_unique_check_in_and_button() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot(
        ["考勤打卡", "上班已打卡 09:03", "下班打卡"],
        [DummyButton()],
    )

    assert snapshot.check_in_time is not None
    assert snapshot.check_in_time.strftime("%H:%M") == "09:03"
    assert snapshot.button_count == 1
    assert snapshot.button_enabled
    assert snapshot.blocking_reason is None


def test_snapshot_blocks_multiple_check_in_times() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot(
        ["上班打卡 09:03", "上班已打卡 09:04", "下班打卡"],
        [DummyButton()],
    )

    assert snapshot.check_in_time is None
    assert snapshot.blocking_reason == "识别到多个不同的上班打卡时间"


def test_snapshot_blocks_page_warning() -> None:
    adapter = FeishuUiaAdapter(auto_open_workbench=False)

    snapshot = adapter._build_snapshot(
        ["上班打卡 09:03", "下班打卡", "不在考勤范围"],
        [DummyButton()],
    )

    assert snapshot.blocking_reason == "页面提示：不在考勤范围"
