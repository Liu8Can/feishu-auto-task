from __future__ import annotations

import logging
import threading
import tkinter as tk
import winsound
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from .config import AppConfig, load_config, save_config
from .core import CheckResult
from .engine import ClockoutEngine, EngineConfig
from .feishu_uia import FeishuUiaAdapter
from .storage import JsonStateStore


STATUS_LABELS = {
    "idle": "尚未检查",
    "checking": "正在检查",
    "waiting": "等待目标时间",
    "dry_run_ready": "演练条件通过",
    "success": "打卡成功",
    "blocked": "安全停止",
    "skipped": "本次跳过",
    "already_attempted": "今日已尝试",
    "already_clocked_out": "今日已打卡",
    "unknown": "结果不明确",
}


class ClockoutDemoApp:
    def __init__(self, root: tk.Tk, project_root: Path) -> None:
        self.root = root
        self.project_root = project_root
        self.config_path = project_root / "config" / "config.json"
        self.config: AppConfig = load_config(self.config_path)
        if self.config.mode == "automatic" and not self.config.trusted_container_fingerprint:
            self.config = self.config.with_mode("dry_run")
            save_config(self.config_path, self.config)
        self.adapter = FeishuUiaAdapter(
            auto_open_workbench=self.config.auto_open_workbench,
            trusted_container_fingerprint=self.config.trusted_container_fingerprint,
        )
        self.store = JsonStateStore(project_root / "data" / "state.json")
        self.logger = self._setup_logger(project_root / "logs")
        self.running = False
        self.busy = False
        self.next_check: datetime | None = None
        self.last_clock: datetime | None = None

        self.mode_var = tk.StringVar(value=self.config.mode)
        self.status_var = tk.StringVar(value=STATUS_LABELS["idle"])
        self.message_var = tk.StringVar(value="请先在飞书打开今天的考勤页并完成页面绑定")
        self.check_in_var = tk.StringVar(value="--:--")
        self.eligible_var = tk.StringVar(value="--:--")
        self.next_check_var = tk.StringVar(value="未启动")
        self.monitor_button_var = tk.StringVar(value="开始监控")

        self._build_window()
        self._build_layout()
        self._log("演示程序已启动，当前为%s", self._mode_label(self.config.mode))
        self.root.after(1000, self._tick)

    def _build_window(self) -> None:
        self.root.title("飞书动态下班打卡助手 - Demo")
        self.root.geometry("780x660")
        self.root.minsize(700, 610)
        self.root.configure(bg="#F3F5F7")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#F3F5F7")
        style.configure("Panel.TFrame", background="#FFFFFF")
        style.configure(
            "Title.TLabel",
            background="#F3F5F7",
            foreground="#1F2329",
            font=("Microsoft YaHei UI", 20, "bold"),
        )
        style.configure(
            "Sub.TLabel",
            background="#F3F5F7",
            foreground="#646A73",
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "MetricTitle.TLabel",
            background="#FFFFFF",
            foreground="#8F959E",
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Metric.TLabel",
            background="#FFFFFF",
            foreground="#1F2329",
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        style.configure(
            "Status.TLabel",
            background="#FFFFFF",
            foreground="#245BDB",
            font=("Microsoft YaHei UI", 13, "bold"),
        )
        style.configure(
            "Primary.TButton",
            background="#3370FF",
            foreground="#FFFFFF",
            padding=(16, 9),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map("Primary.TButton", background=[("active", "#245BDB")])
        style.configure(
            "TButton", padding=(13, 8), font=("Microsoft YaHei UI", 10)
        )
        style.configure(
            "TRadiobutton",
            background="#FFFFFF",
            foreground="#1F2329",
            font=("Microsoft YaHei UI", 10),
        )

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=(24, 20))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="飞书动态下班打卡助手", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            outer,
            text="自动读取上班时间，到点后二次确认并执行一次下班打卡",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(3, 16))

        mode_panel = ttk.Frame(outer, style="Panel.TFrame", padding=(18, 13))
        mode_panel.pack(fill="x", pady=(0, 12))
        ttk.Label(mode_panel, text="运行模式", style="MetricTitle.TLabel").pack(
            side="left", padx=(0, 18)
        )
        ttk.Radiobutton(
            mode_panel,
            text="演练模式",
            value="dry_run",
            variable=self.mode_var,
            command=self._change_mode,
        ).pack(side="left", padx=(0, 20))
        ttk.Radiobutton(
            mode_panel,
            text="自动模式",
            value="automatic",
            variable=self.mode_var,
            command=self._change_mode,
        ).pack(side="left")
        ttk.Label(
            mode_panel,
            text="自动模式会真实点击飞书",
            style="MetricTitle.TLabel",
        ).pack(side="right")

        metrics = ttk.Frame(outer, style="Panel.TFrame", padding=(18, 16))
        metrics.pack(fill="x", pady=(0, 12))
        for column in range(4):
            metrics.columnconfigure(column, weight=1)
        self._metric(metrics, 0, "当前状态", self.status_var, "Status.TLabel")
        self._metric(metrics, 1, "今日上班", self.check_in_var)
        self._metric(metrics, 2, "最早下班", self.eligible_var)
        self._metric(metrics, 3, "下次检查", self.next_check_var)

        message_panel = ttk.Frame(outer, style="Panel.TFrame", padding=(18, 12))
        message_panel.pack(fill="x", pady=(0, 12))
        ttk.Label(message_panel, textvariable=self.message_var, style="MetricTitle.TLabel").pack(
            anchor="w"
        )

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 12))
        primary_actions = ttk.Frame(actions)
        primary_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(
            primary_actions,
            textvariable=self.monitor_button_var,
            style="Primary.TButton",
            command=self._toggle_monitor,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(primary_actions, text="立即检查", command=self._run_check).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(
            primary_actions, text="时间设置", command=self._open_schedule_settings
        ).pack(side="left")

        secondary_actions = ttk.Frame(actions)
        secondary_actions.pack(fill="x")
        ttk.Button(
            secondary_actions, text="绑定考勤页", command=self._run_calibration
        ).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(
            secondary_actions, text="诊断当前页面", command=self._run_diagnostics
        ).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(
            secondary_actions,
            text="打开飞书考勤页",
            command=self._open_attendance_page,
        ).pack(side="left")

        log_panel = ttk.Frame(outer, style="Panel.TFrame", padding=(14, 12))
        log_panel.pack(fill="both", expand=True)
        ttk.Label(log_panel, text="运行记录", style="MetricTitle.TLabel").pack(
            anchor="w", pady=(0, 7)
        )
        self.log_text = tk.Text(
            log_panel,
            height=10,
            borderwidth=0,
            relief="flat",
            bg="#FFFFFF",
            fg="#3E4249",
            font=("Consolas", 9),
            wrap="word",
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)

    @staticmethod
    def _metric(
        parent: ttk.Frame,
        column: int,
        title: str,
        variable: tk.StringVar,
        style: str = "Metric.TLabel",
    ) -> None:
        cell = ttk.Frame(parent, style="Panel.TFrame")
        cell.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 8))
        ttk.Label(cell, text=title, style="MetricTitle.TLabel").pack(anchor="w")
        ttk.Label(cell, textvariable=variable, style=style).pack(anchor="w", pady=(4, 0))

    def _engine(self) -> ClockoutEngine:
        config = EngineConfig(
            mode=self.config.mode,
            calculation_mode=self.config.calculation_mode,
            work_duration_minutes=self.config.work_duration_minutes,
            safety_buffer_minutes=self.config.buffer_minutes,
            break_start_time=self.config.break_start,
            break_end_time=self.config.break_end,
            fixed_checkin_time=self.config.fixed_checkin,
            fixed_clockout_time=self.config.fixed_clockout,
            check_start_time=self.config.start_time,
            check_end_time=self.config.end_time,
            weekdays=frozenset(self.config.weekdays),
        )
        return ClockoutEngine(self.adapter, self.store, config, now_provider=datetime.now)

    def _open_schedule_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("时间设置")
        dialog.geometry("520x600")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        content = ttk.Frame(dialog, padding=20)
        content.pack(fill="both", expand=True)

        calculation_mode = tk.StringVar(value=self.config.calculation_mode)
        work_duration = tk.StringVar(value=str(self.config.work_duration_minutes))
        buffer_minutes = tk.StringVar(value=str(self.config.buffer_minutes))
        break_start = tk.StringVar(value=self.config.break_start_time)
        break_end = tk.StringVar(value=self.config.break_end_time)
        fixed_checkin = tk.StringVar(value=self.config.fixed_checkin_time)
        fixed_clockout = tk.StringVar(value=self.config.fixed_clockout_time)
        auto_open = tk.BooleanVar(value=self.config.auto_open_workbench)

        ttk.Label(content, text="下班时间计算方式", style="Status.TLabel").pack(
            anchor="w", pady=(0, 10)
        )
        ttk.Radiobutton(
            content,
            text="按实际上班时间和有效工时计算",
            value="dynamic",
            variable=calculation_mode,
        ).pack(anchor="w", pady=(0, 6))
        ttk.Radiobutton(
            content,
            text="使用固定上下班时间",
            value="fixed",
            variable=calculation_mode,
        ).pack(anchor="w", pady=(0, 14))

        fields = ttk.Frame(content)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        entries: dict[str, ttk.Entry] = {}

        def add_field(row: int, key: str, label: str, variable: tk.StringVar) -> None:
            ttk.Label(fields, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=5
            )
            entry = ttk.Entry(fields, textvariable=variable, width=18)
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            entries[key] = entry

        add_field(0, "work_duration", "有效工作时长（分钟）", work_duration)
        add_field(1, "break_start", "休息开始时间", break_start)
        add_field(2, "break_end", "休息结束时间", break_end)
        add_field(3, "buffer", "安全缓冲（分钟）", buffer_minutes)
        add_field(4, "fixed_checkin", "固定上班时间", fixed_checkin)
        add_field(5, "fixed_clockout", "固定下班时间", fixed_clockout)

        def update_entry_states(*_args: object) -> None:
            dynamic = calculation_mode.get() == "dynamic"
            for key in ("work_duration", "break_start", "break_end", "buffer"):
                entries[key].configure(state="normal" if dynamic else "disabled")
            for key in ("fixed_checkin", "fixed_clockout"):
                entries[key].configure(state="disabled" if dynamic else "normal")

        calculation_mode.trace_add("write", update_entry_states)
        update_entry_states()

        ttk.Checkbutton(
            content,
            text="检查时自动启动飞书并打开考勤页",
            variable=auto_open,
        ).pack(anchor="w", pady=(14, 12))

        buttons = ttk.Frame(content)
        buttons.pack(fill="x", side="bottom")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")

        def save() -> None:
            try:
                updated = self._updated_schedule_config(
                    self.config,
                    calculation_mode=calculation_mode.get(),
                    work_duration_minutes=work_duration.get(),
                    buffer_minutes=buffer_minutes.get(),
                    break_start_time=break_start.get(),
                    break_end_time=break_end.get(),
                    fixed_checkin_time=fixed_checkin.get(),
                    fixed_clockout_time=fixed_clockout.get(),
                    auto_open_workbench=auto_open.get(),
                )
                save_config(self.config_path, updated)
            except ValueError as exc:
                messagebox.showerror("设置无效", str(exc), parent=dialog)
                return
            self._apply_schedule_config(updated)
            self._log("时间设置已保存：%s", self._calculation_mode_label(updated))
            dialog.destroy()

        ttk.Button(buttons, text="保存", style="Primary.TButton", command=save).pack(
            side="right", padx=(0, 8)
        )

    def _apply_schedule_config(self, updated: AppConfig) -> None:
        self.config = updated
        self.adapter.auto_open_workbench = updated.auto_open_workbench
        if self.running:
            self.next_check = datetime.now()
            self.next_check_var.set("即将按新设置检查")

    @staticmethod
    def _updated_schedule_config(
        current: AppConfig,
        *,
        calculation_mode: str,
        work_duration_minutes: str,
        buffer_minutes: str,
        break_start_time: str,
        break_end_time: str,
        fixed_checkin_time: str,
        fixed_clockout_time: str,
        auto_open_workbench: bool,
    ) -> AppConfig:
        try:
            duration = int(work_duration_minutes.strip())
            buffer = int(buffer_minutes.strip())
        except ValueError as exc:
            raise ValueError("工作时长和安全缓冲必须填写整数分钟") from exc
        return replace(
            current,
            calculation_mode=calculation_mode,
            work_duration_minutes=duration,
            buffer_minutes=buffer,
            break_start_time=break_start_time.strip(),
            break_end_time=break_end_time.strip(),
            fixed_checkin_time=fixed_checkin_time.strip(),
            fixed_clockout_time=fixed_clockout_time.strip(),
            auto_open_workbench=auto_open_workbench,
        ).validate()

    @staticmethod
    def _calculation_mode_label(config: AppConfig) -> str:
        if config.calculation_mode == "fixed":
            return f"固定 {config.fixed_checkin_time}-{config.fixed_clockout_time}"
        return (
            f"有效工时 {config.work_duration_minutes} 分钟，"
            f"休息 {config.break_start_time}-{config.break_end_time}"
        )

    def _change_mode(self) -> None:
        requested = self.mode_var.get()
        if requested == "automatic":
            if not self.config.trusted_container_fingerprint:
                messagebox.showwarning(
                    "尚未绑定考勤页",
                    "请先打开今天的飞书考勤页并点击“绑定考勤页”。",
                    parent=self.root,
                )
                self.mode_var.set("dry_run")
                return
            confirmed = messagebox.askyesno(
                "启用自动模式",
                "自动模式在全部检查通过后会真实点击“下班打卡”。\n\n"
                "请先用演练模式确认当前飞书考勤页面能被稳定识别。是否继续？",
                parent=self.root,
            )
            if not confirmed:
                self.mode_var.set(self.config.mode)
                return
        self.config = self.config.with_mode(requested)
        save_config(self.config_path, self.config)
        self._log("运行模式已切换为%s", self._mode_label(requested))

    def _toggle_monitor(self) -> None:
        self.running = not self.running
        if self.running:
            self.next_check = datetime.now()
            self.monitor_button_var.set("暂停监控")
            self._log("自动检查已启动")
        else:
            self.next_check = None
            self.next_check_var.set("已暂停")
            self.monitor_button_var.set("开始监控")
            self._log("自动检查已暂停")

    def _tick(self) -> None:
        now = datetime.now()
        if self.last_clock is not None and now < self.last_clock:
            self.running = False
            self.next_check = None
            self.monitor_button_var.set("开始监控")
            self.status_var.set(STATUS_LABELS["blocked"])
            self.message_var.set("检测到系统时间回拨，自动检查已暂停")
            self._log("检测到系统时间回拨，自动检查已暂停")
        self.last_clock = now

        if self.running and not self.busy and self.next_check and now >= self.next_check:
            self._run_check()
        self._update_next_check(now)
        self.root.after(1000, self._tick)

    def _run_check(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.status_var.set(STATUS_LABELS["checking"])
        self.message_var.set("正在读取飞书考勤页面")
        now = datetime.now()
        self._log("开始检查")

        def work() -> None:
            try:
                result = self._engine().check(now)
            except Exception as exc:
                self.logger.exception("后台检查异常")
                result = CheckResult(
                    "blocked", f"检查异常，自动监控已暂停：{type(exc).__name__}"
                )
            self.root.after(0, lambda: self._finish_check(result))

        threading.Thread(target=work, daemon=True, name="attendance-check").start()

    def _finish_check(self, result: CheckResult) -> None:
        self.busy = False
        self.status_var.set(STATUS_LABELS.get(result.status, result.status))
        self.message_var.set(result.message)
        if result.check_in_time:
            self.check_in_var.set(result.check_in_time.strftime("%H:%M"))
        if result.eligible_time:
            self.eligible_var.set(result.eligible_time.strftime("%H:%M"))

        now = datetime.now()
        if result.status == "waiting" and result.eligible_time:
            self.next_check = result.eligible_time
        elif self.running:
            self.next_check = now + timedelta(minutes=self.config.check_interval_minutes)

        self._log("%s：%s", STATUS_LABELS.get(result.status, result.status), result.message)
        if result.status in {"success", "unknown"} or (
            result.status == "blocked" and result.message.startswith("检查异常")
        ):
            self.running = False
            self.next_check = None
            self.monitor_button_var.set("开始监控")
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            messagebox.showinfo(
                "下班打卡结果",
                result.message,
                parent=self.root,
            )

    def _run_diagnostics(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.message_var.set("正在诊断当前飞书页面")

        def work() -> None:
            try:
                data = self.adapter.diagnostics()
                message = (
                    f"窗口：{'已找到' if data.get('window_found') else '未找到'}；"
                    f"控件：{data.get('element_count', 0)}；"
                    f"考勤页：{'是' if data.get('attendance_page') else '否'}；"
                    f"下班按钮：{data.get('checkout_button_count', 0)}；"
                    f"已绑定：{'是' if data.get('bound') else '否'}"
                )
            except Exception as exc:
                message = f"诊断失败：{type(exc).__name__}"
            self.root.after(0, lambda: self._finish_diagnostics(message))

        threading.Thread(target=work, daemon=True, name="attendance-diagnostics").start()

    def _run_calibration(self) -> None:
        if self.busy:
            return
        if not messagebox.askyesno(
            "绑定当前考勤页",
            "请确认飞书当前显示的是今天的考勤打卡页面。程序只会读取页面，不会点击打卡。",
            parent=self.root,
        ):
            return
        self.busy = True
        self.message_var.set("正在绑定当前考勤页面")

        def work() -> None:
            try:
                fingerprint = self.adapter.calibrate_current_page(datetime.now().date())
                message = "考勤页面绑定成功，可以先运行演练检查"
            except Exception as exc:
                fingerprint = ""
                message = f"考勤页面绑定失败：{type(exc).__name__}"
            self.root.after(
                0, lambda: self._finish_calibration(fingerprint, message)
            )

        threading.Thread(target=work, daemon=True, name="attendance-calibration").start()

    def _finish_calibration(self, fingerprint: str, message: str) -> None:
        self.busy = False
        if fingerprint:
            self.config = replace(
                self.config, trusted_container_fingerprint=fingerprint
            ).validate()
            save_config(self.config_path, self.config)
        self.message_var.set(message)
        self._log(message)

    def _finish_diagnostics(self, message: str) -> None:
        self.busy = False
        self.message_var.set(message)
        self._log(message)

    def _open_attendance_page(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.message_var.set("正在启动飞书并打开考勤页")

        def work() -> None:
            try:
                opened = self.adapter.open_attendance_page(datetime.now().date())
                message = (
                    "飞书考勤页已打开"
                    if opened
                    else "未能自动打开飞书考勤页，请检查飞书登录状态"
                )
            except Exception as exc:
                message = f"打开飞书考勤页失败：{type(exc).__name__}"
            self.root.after(0, lambda: self._finish_diagnostics(message))

        threading.Thread(
            target=work, daemon=True, name="attendance-navigation"
        ).start()

    def _update_next_check(self, now: datetime) -> None:
        if not self.running or self.next_check is None:
            return
        if self.next_check.date() == now.date():
            self.next_check_var.set(self.next_check.strftime("%H:%M:%S"))
        else:
            self.next_check_var.set(self.next_check.strftime("%m-%d %H:%M"))

    def _log(self, message: str, *args: object) -> None:
        rendered = message % args if args else message
        self.logger.info(rendered)
        timestamped = f"{datetime.now():%H:%M:%S}  {rendered}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", timestamped)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    @staticmethod
    def _mode_label(mode: str) -> str:
        return "自动模式" if mode == "automatic" else "演练模式"

    @staticmethod
    def _setup_logger(log_dir: Path) -> logging.Logger:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("clockout-demo")
        if not logger.handlers:
            handler = logging.FileHandler(
                log_dir / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        return logger
