from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import time
from pathlib import Path
from typing import Iterable, Protocol


INTERACTIVE_LOGON = "Interactive"
LIMITED_RUN_LEVEL = "Limited"
SCHEDULED_WAKE_ARGUMENT = "--scheduled-wake"
_PRODUCT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_SID_PATTERN = re.compile(r"^S-\d+(?:-\d+)+$")


class WakeSchedulerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WakeTaskSpec:
    name: str
    description: str
    user_sid: str
    executable: str
    arguments: str
    daily_times: tuple[str, ...]
    action_count: int
    trigger_count: int
    wake_to_run: bool = True
    start_when_available: bool = True
    allow_on_battery: bool = True
    daily_trigger: bool = True
    logon_type: str = INTERACTIVE_LOGON
    run_level: str = LIMITED_RUN_LEVEL


class TaskSchedulerBackend(Protocol):
    def get_task(self, name: str) -> WakeTaskSpec | None: ...

    def register_task(self, task: WakeTaskSpec) -> None: ...

    def delete_task(self, name: str) -> None: ...


class WakeScheduler:
    """Manage one user-scoped wake task without performing attendance actions."""

    def __init__(
        self,
        *,
        backend: TaskSchedulerBackend,
        product_id: str,
        user_sid: str,
        executable: Path,
        launcher: Path | None = None,
    ) -> None:
        if len(product_id) > 64 or not _PRODUCT_ID_PATTERN.fullmatch(product_id):
            raise ValueError("产品标识只能包含字母、数字、点、横线和下划线")
        if not _SID_PATTERN.fullmatch(user_sid):
            raise ValueError("Windows 用户 SID 无效")
        self._validate_path(executable, "启动程序")
        if launcher is not None:
            self._validate_path(launcher, "启动脚本")
        self.backend = backend
        self.product_id = product_id
        self.user_sid = user_sid
        self.executable = executable
        self.launcher = launcher
        self.task_name = f"{product_id}-Wake-{user_sid}"

    def desired_task(self, daily_times: Iterable[time]) -> WakeTaskSpec:
        normalized_times = tuple(
            sorted({self._format_time(value) for value in daily_times})
        )
        if not normalized_times:
            raise ValueError("至少需要一个唤醒时间")
        arguments = SCHEDULED_WAKE_ARGUMENT
        if self.launcher is not None:
            arguments = f'"{self.launcher}" {SCHEDULED_WAKE_ARGUMENT}'
        return WakeTaskSpec(
            name=self.task_name,
            description="飞书自动任务后台唤醒检查，不执行考勤操作。",
            user_sid=self.user_sid,
            executable=str(self.executable),
            arguments=arguments,
            daily_times=normalized_times,
            action_count=1,
            trigger_count=len(normalized_times),
        )

    def ensure(self, daily_times: Iterable[time]) -> bool:
        desired = self.desired_task(daily_times)
        if self.backend.get_task(self.task_name) == desired:
            return False
        self.backend.register_task(desired)
        return True

    def query(self) -> WakeTaskSpec | None:
        return self.backend.get_task(self.task_name)

    def delete(self) -> bool:
        if self.backend.get_task(self.task_name) is None:
            return False
        self.backend.delete_task(self.task_name)
        return True

    @staticmethod
    def _format_time(value: time) -> str:
        if not isinstance(value, time):
            raise ValueError("唤醒时间必须是有效时间")
        if value.second or value.microsecond:
            raise ValueError("唤醒时间只支持分钟精度")
        return value.strftime("%H:%M")

    @staticmethod
    def _validate_path(path: Path, label: str) -> None:
        value = str(path)
        if (
            not path.is_absolute()
            or any(character in value for character in ('"', "\r", "\n"))
        ):
            raise ValueError(f"{label}路径无效")


class PowerShellTaskSchedulerBackend:
    """Use structured Task Scheduler cmdlets without parsing localized text."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or "powershell.exe"

    def get_task(self, name: str) -> WakeTaskSpec | None:
        output = self._run(_QUERY_SCRIPT, {"name": name})
        try:
            value = json.loads(output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise WakeSchedulerError("任务计划程序返回了无效的结构化结果") from exc
        if not isinstance(value, dict):
            raise WakeSchedulerError("任务计划程序返回了无效的结构化结果")
        if value.get("exists") is False:
            return None
        try:
            daily_times = value["daily_times"]
            action_count = value["action_count"]
            trigger_count = value["trigger_count"]
            if (
                not isinstance(daily_times, list)
                or any(not isinstance(item, str) for item in daily_times)
                or isinstance(action_count, bool)
                or not isinstance(action_count, int)
                or action_count < 0
                or isinstance(trigger_count, bool)
                or not isinstance(trigger_count, int)
                or trigger_count < 0
            ):
                raise TypeError("任务结构计数无效")
            return WakeTaskSpec(
                name=value["name"],
                description=value["description"],
                user_sid=value["user_sid"],
                executable=value["executable"],
                arguments=value["arguments"],
                daily_times=tuple(daily_times),
                action_count=action_count,
                trigger_count=trigger_count,
                wake_to_run=value["wake_to_run"],
                start_when_available=value["start_when_available"],
                allow_on_battery=value["allow_on_battery"],
                daily_trigger=value["daily_trigger"],
                logon_type=value["logon_type"],
                run_level=value["run_level"],
            )
        except (KeyError, TypeError) as exc:
            raise WakeSchedulerError("任务计划程序结果缺少必要字段") from exc

    def register_task(self, task: WakeTaskSpec) -> None:
        self._run(_REGISTER_SCRIPT, asdict(task))

    def delete_task(self, name: str) -> None:
        self._run(_DELETE_SCRIPT, {"name": name})

    def _run(self, script: str, payload: dict[str, object]) -> str:
        if os.name != "nt":
            raise WakeSchedulerError("Windows 任务计划程序仅能在 Windows 上使用")
        completed = subprocess.run(
            [
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise WakeSchedulerError(
                f"Windows 任务计划程序操作失败，退出代码 {completed.returncode}"
            )
        return completed.stdout.strip()


_QUERY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$matches = @(
    Get-ScheduledTask -TaskPath '\' -ErrorAction Stop |
        Where-Object { $_.TaskName -eq $request.name }
)
if ($matches.Count -eq 0) {
    [pscustomobject]@{ exists = $false } | ConvertTo-Json -Compress
    exit 0
}
$task = $matches[0]
$actions = @($task.Actions)
$action = $actions[0]
$triggers = @($task.Triggers)
$times = @(
    $triggers | ForEach-Object {
        ([datetime]$_.StartBoundary).ToString(
            'HH:mm',
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
)
$allDaily = $triggers.Count -gt 0 -and @(
    $triggers | Where-Object { [int]$_.DaysInterval -eq 1 }
).Count -eq $triggers.Count
[pscustomobject]@{
    exists = $true
    name = $task.TaskName
    description = $task.Description
    user_sid = $task.Principal.UserId
    executable = $action.Execute
    arguments = $action.Arguments
    daily_times = @($times)
    action_count = $actions.Count
    trigger_count = $triggers.Count
    wake_to_run = [bool]$task.Settings.WakeToRun
    start_when_available = [bool]$task.Settings.StartWhenAvailable
    allow_on_battery = -not [bool]$task.Settings.DisallowStartIfOnBatteries
    daily_trigger = $allDaily
    logon_type = [string]$task.Principal.LogonType
    run_level = [string]$task.Principal.RunLevel
} | ConvertTo-Json -Compress
"""


_REGISTER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$spec = [Console]::In.ReadToEnd() | ConvertFrom-Json
$action = New-ScheduledTaskAction `
    -Execute $spec.executable `
    -Argument $spec.arguments
$triggers = @(
    $spec.daily_times | ForEach-Object {
        $at = [datetime]::ParseExact(
            $_,
            'HH:mm',
            [Globalization.CultureInfo]::InvariantCulture
        )
        New-ScheduledTaskTrigger -Daily -At $at
    }
)
$principal = New-ScheduledTaskPrincipal `
    -UserId $spec.user_sid `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$definition = New-ScheduledTask `
    -Action $action `
    -Trigger $triggers `
    -Principal $principal `
    -Settings $settings `
    -Description $spec.description
Register-ScheduledTask `
    -TaskPath '\' `
    -TaskName $spec.name `
    -InputObject $definition `
    -Force | Out-Null
"""


_DELETE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
Unregister-ScheduledTask `
    -TaskPath '\' `
    -TaskName $request.name `
    -Confirm:$false
"""
