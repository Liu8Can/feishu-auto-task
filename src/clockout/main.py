from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import win32api
import win32event
import winerror

from .gui import ClockoutDemoApp


def main() -> None:
    mutex = win32event.CreateMutex(None, False, "Local\\FeishuClockoutAssistantDemo")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("飞书下班助手", "程序已经在运行")
        root.destroy()
        return

    project_root = Path(__file__).resolve().parents[2]
    root = tk.Tk()
    root._clockout_mutex = mutex  # type: ignore[attr-defined]
    ClockoutDemoApp(root, project_root)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
