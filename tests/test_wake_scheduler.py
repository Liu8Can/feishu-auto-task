from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import time
from pathlib import Path

import pytest

from clockout.wake_scheduler import (
    INTERACTIVE_LOGON,
    LIMITED_RUN_LEVEL,
    PowerShellTaskSchedulerBackend,
    TaskSchedulerBackend,
    WakeScheduler,
    WakeTaskSpec,
)
import clockout.wake_scheduler as wake_scheduler


class FakeBackend(TaskSchedulerBackend):
    def __init__(self) -> None:
        self.tasks: dict[str, WakeTaskSpec] = {}
        self.registered: list[WakeTaskSpec] = []
        self.deleted: list[str] = []

    def get_task(self, name: str) -> WakeTaskSpec | None:
        return self.tasks.get(name)

    def register_task(self, task: WakeTaskSpec) -> None:
        self.tasks[task.name] = task
        self.registered.append(task)

    def delete_task(self, name: str) -> None:
        self.tasks.pop(name, None)
        self.deleted.append(name)


def make_scheduler(backend: FakeBackend) -> WakeScheduler:
    return WakeScheduler(
        backend=backend,
        product_id="FeishuAutoTask",
        user_sid="S-1-5-21-1000-2000-3000-1001",
        executable=Path(r"C:\Program Files\Feishu Auto Task\assistant.exe"),
    )


def test_task_uses_stable_product_and_user_scoped_name() -> None:
    backend = FakeBackend()
    first = make_scheduler(backend)
    second = make_scheduler(backend)

    assert first.task_name == second.task_name
    assert "FeishuAutoTask" in first.task_name
    assert "S-1-5-21-1000-2000-3000-1001" in first.task_name


def test_registered_task_is_safe_background_wake_only() -> None:
    backend = FakeBackend()
    scheduler = make_scheduler(backend)

    changed = scheduler.ensure((time(8, 25), time(8, 25), time(17, 45)))

    assert changed
    task = backend.registered[-1]
    assert task.user_sid == "S-1-5-21-1000-2000-3000-1001"
    assert task.logon_type == INTERACTIVE_LOGON
    assert task.run_level == LIMITED_RUN_LEVEL
    assert task.wake_to_run
    assert task.start_when_available
    assert task.allow_on_battery
    assert task.daily_trigger
    assert task.arguments == "--scheduled-wake"
    assert task.daily_times == ("08:25", "17:45")
    assert task.action_count == 1
    assert task.trigger_count == 2
    assert "打卡" not in task.description
    assert "password" not in repr(task).lower()


def test_source_launch_uses_launcher_then_only_scheduled_wake_flag() -> None:
    backend = FakeBackend()
    scheduler = WakeScheduler(
        backend=backend,
        product_id="FeishuAutoTask",
        user_sid="S-1-5-21-1001",
        executable=Path(r"C:\Python\pythonw.exe"),
        launcher=Path(r"D:\src\launcher.py"),
    )

    scheduler.ensure((time(8, 30),))

    assert backend.registered[-1].arguments == (
        '"D:\\src\\launcher.py" --scheduled-wake'
    )


def test_ensure_is_idempotent_and_updates_only_when_definition_changes() -> None:
    backend = FakeBackend()
    scheduler = make_scheduler(backend)

    assert scheduler.ensure((time(8, 30),))
    assert not scheduler.ensure((time(8, 30),))
    assert scheduler.ensure((time(8, 35),))
    assert len(backend.registered) == 2


def test_query_and_delete_are_idempotent() -> None:
    backend = FakeBackend()
    scheduler = make_scheduler(backend)

    assert scheduler.query() is None
    assert not scheduler.delete()
    scheduler.ensure((time(8, 30),))
    assert scheduler.query() == backend.registered[-1]
    assert scheduler.delete()
    assert not scheduler.delete()
    assert backend.deleted == [scheduler.task_name]


@pytest.mark.parametrize(
    ("product_id", "user_sid"),
    [
        ("", "S-1-5-21-1001"),
        ("unsafe name", "S-1-5-21-1001"),
        ("FeishuAutoTask", "administrator"),
        ("FeishuAutoTask", "S-1-5-21-1001\\bad"),
    ],
)
def test_unsafe_task_identity_is_rejected(
    product_id: str, user_sid: str
) -> None:
    with pytest.raises(ValueError):
        WakeScheduler(
            backend=FakeBackend(),
            product_id=product_id,
            user_sid=user_sid,
            executable=Path("assistant.exe"),
        )


@pytest.mark.parametrize(
    ("executable", "launcher"),
    [
        (Path("assistant.exe"), None),
        (Path(r"C:\app\pythonw.exe"), Path("launcher.py")),
    ],
)
def test_relative_launch_paths_are_rejected(
    executable: Path, launcher: Path | None
) -> None:
    with pytest.raises(ValueError, match="路径"):
        WakeScheduler(
            backend=FakeBackend(),
            product_id="FeishuAutoTask",
            user_sid="S-1-5-21-1001",
            executable=executable,
            launcher=launcher,
        )


def test_schedule_requires_at_least_one_daily_time() -> None:
    scheduler = make_scheduler(FakeBackend())

    with pytest.raises(ValueError, match="时间"):
        scheduler.ensure(())


def test_native_backend_uses_powershell_not_schtasks() -> None:
    backend = PowerShellTaskSchedulerBackend()

    assert "powershell" in backend.executable.lower()
    assert "schtasks" not in backend.executable.lower()


def test_query_does_not_treat_scheduler_errors_as_missing_tasks() -> None:
    from clockout.wake_scheduler import _QUERY_SCRIPT

    assert "-ErrorAction Stop" in _QUERY_SCRIPT
    assert "SilentlyContinue" not in _QUERY_SCRIPT


def test_native_backend_parses_trigger_times_with_invariant_culture() -> None:
    assert "[datetime]::ParseExact" in wake_scheduler._REGISTER_SCRIPT
    assert "InvariantCulture" in wake_scheduler._REGISTER_SCRIPT
    assert "New-ScheduledTaskTrigger -Daily -At $at" in wake_scheduler._REGISTER_SCRIPT


@pytest.mark.parametrize(
    "unsafe_existing",
    [
        {"action_count": 0},
        {"action_count": 2},
        {"trigger_count": 0, "daily_times": ()},
        {"trigger_count": 2, "daily_times": ("08:30", "08:30")},
        {"trigger_count": 2, "daily_times": ("08:30", "17:30")},
    ],
)
def test_ensure_rebuilds_tasks_with_non_unique_or_unexpected_structure(
    unsafe_existing: dict[str, object],
) -> None:
    backend = FakeBackend()
    scheduler = make_scheduler(backend)
    desired = scheduler.desired_task((time(8, 30),))
    backend.tasks[scheduler.task_name] = replace(desired, **unsafe_existing)

    assert scheduler.ensure((time(8, 30),))
    assert backend.registered == [desired]


def test_native_query_preserves_duplicate_triggers_and_structure_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired = make_scheduler(FakeBackend()).desired_task((time(8, 30),))
    payload = asdict(desired)
    payload.update(
        exists=True,
        daily_times=["08:30", "08:30"],
        action_count=2,
        trigger_count=2,
    )
    backend = PowerShellTaskSchedulerBackend()
    monkeypatch.setattr(
        backend,
        "_run",
        lambda script, request: json.dumps(payload, ensure_ascii=False),
    )

    task = backend.get_task(desired.name)

    assert task is not None
    assert task.daily_times == ("08:30", "08:30")
    assert task.action_count == 2
    assert task.trigger_count == 2


@pytest.mark.parametrize("missing_field", ["action_count", "trigger_count"])
def test_native_query_rejects_missing_structure_counts(
    monkeypatch: pytest.MonkeyPatch, missing_field: str
) -> None:
    desired = make_scheduler(FakeBackend()).desired_task((time(8, 30),))
    payload = asdict(desired)
    payload["exists"] = True
    payload["daily_times"] = list(desired.daily_times)
    payload.pop(missing_field)
    backend = PowerShellTaskSchedulerBackend()
    monkeypatch.setattr(
        backend,
        "_run",
        lambda script, request: json.dumps(payload, ensure_ascii=False),
    )

    with pytest.raises(Exception, match="缺少必要字段"):
        backend.get_task(desired.name)
