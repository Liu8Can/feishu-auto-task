from __future__ import annotations

import os
import subprocess
import sys
import winreg
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "FeishuClockoutAssistant"
APP_DIR_NAME = "FeishuClockoutAssistant"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    base_dir: Path
    config_path: Path
    state_path: Path
    log_dir: Path


def source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_paths() -> RuntimePaths:
    override = os.environ.get("FEISHU_CLOCKOUT_HOME")
    if override:
        base_dir = Path(override).expanduser().resolve()
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            local_app_data = str(Path.home() / "AppData" / "Local")
        base_dir = Path(local_app_data) / APP_DIR_NAME
    return RuntimePaths(
        base_dir=base_dir,
        config_path=base_dir / "config.json",
        state_path=base_dir / "state.json",
        log_dir=base_dir / "logs",
    )


def prepare_runtime_paths(paths: RuntimePaths) -> None:
    paths.base_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    if paths.config_path.exists():
        return
    legacy_config = source_root() / "config" / "config.json"
    if legacy_config.exists() and not getattr(sys, "frozen", False):
        paths.config_path.write_bytes(legacy_config.read_bytes())


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        return f'"{executable}" --startup'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = source_root() / "launcher.py"
    return f'"{pythonw}" "{launcher}" --startup'


def set_start_with_windows(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(
                key,
                APP_NAME,
                0,
                winreg.REG_SZ,
                startup_command(),
            )
            return
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass


def is_start_with_windows_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, value_type = winreg.QueryValueEx(key, APP_NAME)
    except FileNotFoundError:
        return False
    return value_type == winreg.REG_SZ and value == startup_command()


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer.exe", str(path)])
