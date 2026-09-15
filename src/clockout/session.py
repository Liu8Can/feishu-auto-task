from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Protocol


DESKTOP_SWITCHDESKTOP = 0x0100


class User32DesktopApi(Protocol):
    def OpenInputDesktop(self, flags: int, inherit: bool, access: int): ...

    def SwitchDesktop(self, desktop) -> int: ...

    def CloseDesktop(self, desktop) -> int: ...


def is_interactive_desktop(api: User32DesktopApi | None = None) -> bool:
    """Return False for a locked or otherwise non-interactive Windows desktop."""
    if os.name != "nt":
        return False
    user32 = api or ctypes.windll.user32
    if api is None:
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.SwitchDesktop.argtypes = [wintypes.HANDLE]
        user32.SwitchDesktop.restype = wintypes.BOOL
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL
    desktop = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not desktop:
        return False
    try:
        return bool(user32.SwitchDesktop(desktop))
    finally:
        user32.CloseDesktop(desktop)
