from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from datetime import datetime

from . import __version__
from .projects import project_name
from .core import (DEFAULTS, STATUSES, Engine, Store, next_occurrence, resolved,
                   validate_job, episodic, normalize_roots, schedule_times)
from .rename import renamed_name, validate_rename, RenameError
from .telegram import TELEGRAM_DEFAULTS, validate_telegram

BG = "#0d1220"
PANEL = "#151e30"
INPUT = "#0c1425"
LINE = "#2a3750"
FG = "#e6edf7"
MUTED = "#92a3bb"
ACCENT = "#6ee7c7"
PURPLE = "#b3a0ff"
RED = "#ff8f9c"
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MOVE_MODES = {"剧集文件夹（含字幕、封面）": "folders", "仅成品文件": "files"}


def label(parent, text, size=10, color=FG, **kw):
    return tk.Label(parent, text=text, font=("Microsoft YaHei UI", size),
                    bg=parent.cget("bg"), fg=color, **kw)


def button(parent, text, command, primary=False, **kw):
    options = dict(font=("Microsoft YaHei UI", 10), bg=ACCENT if primary else LINE,
                   fg=BG if primary else FG, activebackground="#99f2db" if primary else "#364865",
                   activeforeground=BG if primary else FG, relief="flat", bd=0,
                   padx=16, pady=8, cursor="hand2")
    return tk.Button(parent, text=text, command=command, **(options | kw))


def entry(parent, variable, width=30):
    return tk.Entry(parent, textvariable=variable, width=width, bg=INPUT, fg=FG,
                    insertbackground=FG, relief="flat", highlightthickness=1,
                    highlightbackground=LINE, highlightcolor=ACCENT,
                    font=("Microsoft YaHei UI", 10))


def check(parent, text, variable, command=None):
    return tk.Checkbutton(parent, text=text, variable=variable, command=command,
                          bg=parent.cget("bg"), fg=FG, selectcolor=INPUT,
                          activebackground=parent.cget("bg"), activeforeground=ACCENT,
                          font=("Microsoft YaHei UI", 10), bd=0)


def card(parent):
    return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)


def directory_row(parent, variable, caption, row, file=False):
    label(parent, caption, color=MUTED).grid(row=row, column=0, sticky="w", pady=7, padx=(0, 12))
    entry(parent, variable).grid(row=row, column=1, sticky="ew", pady=7, ipady=7)
    def choose():
        value = filedialog.askopenfilename(parent=parent, title=caption, filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]) if file else filedialog.askdirectory(parent=parent, title=caption)
        if value:
            variable.set(value)
    button(parent, "浏览…", choose).grid(row=row, column=2, padx=(10, 0))


class ScrollArea(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=BG)
        window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        # Only handle descendants of this scroll area; leave editors and lists alone.
        top = self.winfo_toplevel()
        def wheel(event):
            if str(event.widget).startswith(str(self) + ".") and event.widget.winfo_class() not in ("Text", "Treeview", "Listbox", "TCombobox"):
                self.canvas.yview_scroll(-int(event.delta / 120), "units")
                return "break"
        binding = top.bind("<MouseWheel>", wheel, add="+")
        self.bind("<Destroy>", lambda e: top.unbind("<MouseWheel>", binding) if e.widget is self and top.winfo_exists() else None)


class PolicyForm(tk.Frame):
    def __init__(self, parent, values, task_scope=True):
        super().__init__(parent, bg=PANEL)
        self.columnconfigure(1, weight=1)
        self.task_scope = task_scope
        keys = ("retries", "retry_delay", "timeout", "source", "destination", "patterns", "move_enabled") if task_scope else ("retries", "retry_delay", "timeout")
        self.variables = {key: (tk.BooleanVar(value=bool(values.get(key, DEFAULTS[key]))) if key == "move_enabled"
                               else tk.StringVar(value=str(values.get(key, DEFAULTS[key]))))
                          for key in keys}
        numbers = tk.Frame(self, bg=PANEL)
        numbers.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(6, 12))
        for text, key in (("失败重试次数", "retries"), ("重试间隔 / 秒", "retry_delay"), ("超时 / 秒（0 不限）", "timeout")):
            label(numbers, text, color=MUTED).pack(side="left", padx=(0, 8))
            entry(numbers, self.variables[key], width=6).pack(side="left", ipady=6, padx=(0, 18))
        if not task_scope:
            label(self, "移动仅在各定时任务中设置；单次任务和外部历史监听不会自动移动文件。", 9, MUTED).grid(row=1, column=0, columnspan=3, sticky="w", pady=8)
            return
        check(self, "任务成功后自动移动成品", self.variables["move_enabled"]).grid(row=1, column=0, columnspan=3, sticky="w")
        self.move_mode = tk.StringVar(value=next(text for text, mode in MOVE_MODES.items() if mode == values.get("move_mode", DEFAULTS["move_mode"])))
        label(self, "移动方式", color=MUTED).grid(row=2, column=0, sticky="w", pady=7)
        ttk.Combobox(self, textvariable=self.move_mode, values=list(MOVE_MODES), state="readonly", width=30).grid(row=2, column=1, columnspan=2, sticky="w", pady=7)
        directory_row(self, self.variables["source"], "成品目录", 3)
        directory_row(self, self.variables["destination"], "移动至", 4)
        label(self, "成品通配符", color=MUTED).grid(row=5, column=0, sticky="w", pady=7)
        entry(self, self.variables["patterns"]).grid(row=5, column=1, columnspan=2, sticky="ew", ipady=7)
        label(self, "仅成品文件：直接移入所选目标目录，不附加源文件夹；同名文件跳过并留在源目录。\n剧集文件夹：移动整个剧集目录，同名目录递归合并，只清理移空的源目录，不覆盖文件。", 9, MUTED, justify="left", wraplength=720).grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 4))

    def values(self):
        values = {k: v.get() for k, v in self.variables.items()}
        if self.task_scope:
            values["move_mode"] = MOVE_MODES[self.move_mode.get()]
        return values


class RenameForm(tk.Frame):
    def __init__(self, parent, values):
        super().__init__(parent, bg=PANEL)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)
        self.season = tk.StringVar(value=values.get("rename_season", ""))
        self.episode = tk.StringVar(value=values.get("rename_episode", ""))
        label(self, "1. 季号 SXX", color=MUTED).grid(row=0, column=0, sticky="w", padx=(0, 10))
        entry(self, self.season, 12).grid(row=0, column=1, sticky="ew", ipady=6, padx=(0, 24))
        label(self, "2. 集号 EXX", color=MUTED).grid(row=0, column=2, sticky="w", padx=(0, 10))
        entry(self, self.episode, 12).grid(row=0, column=3, sticky="ew", ipady=6)
        label(self, "直接指定：S02 / E12 或数字；相对变化：+1 / -2；留空不处理。", 9, MUTED).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 12))
        label(self, "3. 替换词（每行：原词 => 新词）", color=MUTED).grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 7))
        self.replacements = tk.Text(self, height=4, bg=INPUT, fg=FG, insertbackground=FG,
                                    font=("Cascadia Code", 10), relief="flat", padx=10, pady=8, undo=True)
        self.replacements.grid(row=3, column=0, columnspan=4, sticky="ew")
        self.replacements.insert("1.0", values.get("rename_replacements", ""))
        self.sample = tk.StringVar(value="Xiong.Mei.S01E01.mkv")
        label(self, "名称预览", color=MUTED).grid(row=4, column=0, sticky="w", pady=(12, 0))
        entry(self, self.sample).grid(row=4, column=1, columnspan=2, sticky="ew", ipady=6, pady=(12, 0), padx=(0, 10))
        button(self, "预览规则", self.preview).grid(row=4, column=3, sticky="e", pady=(12, 0))
        self.result = tk.StringVar()
        label(self, "", 9, ACCENT, textvariable=self.result, justify="left", wraplength=730).grid(row=5, column=0, columnspan=4, sticky="w", pady=10)
        label(self, "按季号 → 集号 → 替换词顺序处理，包含剧集目录、子目录及其文件；文件扩展名保留。\n仅处理本次完成任务关联的目录/文件；全部预检及重命名成功后才移动。关闭移动时可仅重命名。", 9, MUTED, justify="left", wraplength=730).grid(row=6, column=0, columnspan=4, sticky="w")
        self.preview()

    def values(self):
        return {"rename_season": self.season.get(), "rename_episode": self.episode.get(),
                "rename_replacements": self.replacements.get("1.0", "end-1c")}

    def preview(self):
        try:
            rules = validate_rename(self.values())
            result = renamed_name(self.sample.get(), rules)
            folder = renamed_name("Xiong.Mei.S01", rules, is_directory=True)
            self.result.set(f"文件：{result}\n文件夹示例：{folder}")
        except (ValueError, RenameError) as exc:
            self.result.set(f"规则错误：{exc}")


class ScheduleDialog(tk.Toplevel):
    def __init__(self, app, spec=None):
        super().__init__(app)
        self.app = app
        self.spec = spec or {}
        self.title("编辑定时任务" if spec else "新建定时任务")
        self.geometry("900x810")
        self.minsize(790, 610)
        self.configure(bg=BG)
        self.transient(app)
        self.grab_set()
        outer = ScrollArea(self)
        outer.pack(fill="both", expand=True, padx=20, pady=15)
        body = outer.body
        job = self.spec.get("job", app.store.settings())
        label(body, "让任务按时发生", 20).pack(anchor="w", pady=(0, 6))
        label(body, "使用电脑本地时间 · 24 小时制 · 到点加入串行队列", 10, MUTED).pack(anchor="w", pady=(0, 14))
        form = card(body)
        form.pack(fill="x", pady=(0, 14))
        inner = tk.Frame(form, bg=PANEL)
        inner.pack(fill="x", padx=18, pady=15)
        inner.columnconfigure(1, weight=1)
        self.name = tk.StringVar(value=job.get("name", "定时任务"))
        self.when = tk.StringVar(value=self.spec.get("time", "09:00"))
        self.shell = tk.StringVar(value=job.get("shell", "cmd"))
        self.enabled = tk.BooleanVar(value=self.spec.get("enabled", True))
        label(inner, "任务名称", color=MUTED).grid(row=0, column=0, sticky="w", padx=(0, 14))
        entry(inner, self.name).grid(row=0, column=1, sticky="ew", ipady=7)
        check(inner, "启用", self.enabled).grid(row=0, column=2, padx=(14, 0))
        times = tk.Frame(inner, bg=PANEL)
        times.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(15, 8))
        time_row = tk.Frame(times, bg=PANEL)
        time_row.pack(fill="x")
        label(time_row, "执行时间", color=MUTED).pack(side="left", padx=(0, 15))
        entry(time_row, self.when, width=8).pack(side="left", ipady=6)
        self.add_time_button = button(time_row, "+ 添加时间", self.add_extra_time)
        self.add_time_button.pack(side="left", padx=(12, 0))
        button(time_row, "每天", lambda: [v.set(True) for v in self.days]).pack(side="left", padx=(15, 8))
        button(time_row, "工作日", lambda: [v.set(i < 5) for i, v in enumerate(self.days)]).pack(side="left")
        self.extra_times_frame = tk.Frame(times, bg=PANEL)
        self.extra_times_frame.pack(fill="x")
        self.extra_times = [tk.StringVar(value=when) for when in self.spec.get("extra_times", [])]
        self.render_extra_times()
        label(times, "可额外添加至多 3 个时间；所有时间共用下方星期设置。", 9, MUTED).pack(anchor="w", pady=(8, 0))
        days = tk.Frame(inner, bg=PANEL)
        days.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self.days = [tk.BooleanVar(value=i in self.spec.get("days", list(range(7)))) for i in range(7)]
        for text, var in zip(WEEK, self.days):
            check(days, text, var).pack(side="left", padx=(0, 13))
        commands = tk.Frame(inner, bg=PANEL)
        commands.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        label(commands, "终端指令", color=MUTED).pack(side="left")
        ttk.Combobox(commands, textvariable=self.shell, values=("cmd", "powershell"), state="readonly", width=13).pack(side="right")
        self.command = tk.Text(inner, height=5, bg=INPUT, fg=FG, insertbackground=FG,
                               font=("Cascadia Code", 11), relief="flat", padx=12, pady=10, undo=True)
        self.command.grid(row=4, column=0, columnspan=3, sticky="ew")
        self.command.insert("1.0", job.get("command", ""))
        self.encoding = tk.StringVar(value=job.get("encoding", "utf-8"))
        self.root_dir = tk.StringVar(value=job.get("root", ""))
        enc = tk.Frame(inner, bg=PANEL)
        enc.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        label(enc, "输出编码", color=MUTED).pack(side="left", padx=(0, 12))
        ttk.Combobox(enc, textvariable=self.encoding, values=("utf-8", "gb18030", "gbk"), width=12).pack(side="left")
        label(inner, "工作目录", color=MUTED).grid(row=6, column=0, sticky="w", pady=7)
        self.root_combo = ttk.Combobox(inner, textvariable=self.root_dir, values=app.store.settings()["roots"], state="readonly")
        self.root_combo.grid(row=6, column=1, columnspan=2, sticky="ew", pady=7)
        label(inner, "可在工作区设置中添加至多 3 个目录；本任务独立保存所选目录。", 9, MUTED).grid(row=7, column=0, columnspan=3, sticky="w")
        episodes = card(body)
        episodes.pack(fill="x", pady=(0, 14))
        label(episodes, "逐集执行 · {episode}", 12, ACCENT).pack(anchor="w", padx=18, pady=(15, 8))
        episode_fields = tk.Frame(episodes, bg=PANEL)
        episode_fields.pack(fill="x", padx=18, pady=(0, 10))
        self.episode_vars = {key: tk.StringVar(value=str(self.spec.get(key, "1")))
                             for key in ("episode_start", "episode_total", "episodes_per_trigger")}
        for title, key in (("起始 Episode", "episode_start"), ("总集数 / 末集编号", "episode_total"), ("每次触发集数", "episodes_per_trigger")):
            label(episode_fields, title, color=MUTED).pack(side="left", padx=(0, 8))
            entry(episode_fields, self.episode_vars[key], width=7).pack(side="left", ipady=6, padx=(0, 20))
        self.displayed_episode_start = self.episode_vars["episode_start"].get()
        self.progress_text = tk.StringVar()
        progress_label = label(episodes, "", 9, ACCENT)
        progress_label.configure(textvariable=self.progress_text)
        progress_label.pack(anchor="w", padx=18, pady=(0, 5))
        label(episodes, "命令中包含 {episode} 时生效，例如 -w S01E{episode}；01 起始会保留补零。\n起始集数自动显示下一待执行集；当前集完成才前进，失败或取消时保留当前集。\n直接保存会保留进度；手动修改起始集数会重设起点。", 9, MUTED, justify="left", wraplength=760).pack(anchor="w", padx=18, pady=(0, 15))
        self.progress_after = None
        self.refresh_episode_progress()
        rename_card = card(body)
        rename_card.pack(fill="x", pady=(0, 14))
        label(rename_card, "重命名规则 · 成功后、移动前", 12, ACCENT).pack(anchor="w", padx=18, pady=(15, 10))
        self.rename = RenameForm(rename_card, job)
        self.rename.pack(fill="x", padx=18, pady=(0, 15))
        rules = card(body)
        rules.pack(fill="x")
        label(rules, "结果处理", 12).pack(anchor="w", padx=18, pady=(15, 5))
        self.policy = PolicyForm(rules, job)
        self.policy.pack(fill="x", padx=18, pady=(0, 15))
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=20, pady=(0, 18))
        label(footer, "每个定时任务独立保存执行目录和规则。", 9, MUTED).pack(side="left")
        button(footer, "保存任务", self.save, True).pack(side="right")
        button(footer, "取消", self.destroy).pack(side="right", padx=10)

    def add_extra_time(self):
        if len(self.extra_times) < 3:
            self.extra_times.append(tk.StringVar())
            self.render_extra_times()
            self.extra_time_entries[-1].focus_set()

    def remove_extra_time(self, variable):
        self.extra_times.remove(variable)
        self.render_extra_times()

    def render_extra_times(self):
        for widget in self.extra_times_frame.winfo_children():
            widget.destroy()
        self.extra_time_entries = []
        for i, variable in enumerate(self.extra_times, 1):
            row = tk.Frame(self.extra_times_frame, bg=PANEL)
            row.pack(fill="x", pady=(8, 0))
            label(row, f"额外时间 {i}", color=MUTED).pack(side="left", padx=(0, 15))
            field = entry(row, variable, width=8)
            field.pack(side="left", ipady=6)
            self.extra_time_entries.append(field)
            button(row, "移除", lambda v=variable: self.remove_extra_time(v)).pack(side="left", padx=(12, 0))
        self.add_time_button.configure(state="disabled" if len(self.extra_times) >= 3 else "normal")

    def refresh_episode_progress(self):
        progress = self.app.store.episode_progress(self.spec) if self.spec.get("id") else None
        if progress is not None:
            number = str(progress).zfill(len(str(self.spec.get("episode_start", "1"))))
            start = self.episode_vars["episode_start"]
            if start.get() == self.displayed_episode_start:
                start.set(number)
                self.displayed_episode_start = number
            if progress > int(self.spec["episode_total"]):
                self.progress_text.set(f"全部 {self.spec['episode_total']} 集已完成；下一编号 {number}，增加末集编号后可继续。")
            else:
                self.progress_text.set(f"当前进度：下一待执行集 {number}。")
        self.progress_after = self.after(500, self._poll_episode_progress)

    def _poll_episode_progress(self):
        self.progress_after = None
        self.refresh_episode_progress()

    def destroy(self):
        if getattr(self, "progress_after", None):
            self.after_cancel(self.progress_after)
            self.progress_after = None
        super().destroy()

    def save(self):
        try:
            old_job = self.spec.get("job", self.app.store.settings())
            job = {**old_job, **self.policy.values(), **self.rename.values(), "name": self.name.get(), "shell": self.shell.get(),
                   "command": self.command.get("1.0", "end-1c"), "encoding": self.encoding.get(), "root": self.root_dir.get()}
            # A text edit must not retain a previously generated argv.
            job.pop("argv", None)
            spec = {**self.spec, "job": job, "time": self.when.get(),
                    "extra_times": [v.get() for v in self.extra_times],
                    "days": [i for i, v in enumerate(self.days) if v.get()], "enabled": self.enabled.get(),
                    **{k: v.get() for k, v in self.episode_vars.items()}}
            reset_episode = bool(self.spec.get("id") and self.episode_vars["episode_start"].get() != self.displayed_episode_start)
            if self.spec.get("id") and not reset_episode:
                # Display progress without turning an ordinary save into a reset.
                spec["episode_start"] = self.spec.get("episode_start", "1")
            self.app.store.save_schedule(spec, reset_episode=reset_episode)
            self.app.refresh()
            self.destroy()
        except (ValueError, LookupError) as exc:
            messagebox.showerror("无法保存", str(exc), parent=self)


class App(tk.Tk):
    def __init__(self, store: Store, engine: Engine):
        super().__init__()
        self.store, self.engine = store, engine
        self.title(f"VTGUI · 下载任务工作台  {__version__}")
        self.geometry("1380x940")
        self.minsize(1100, 780)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.selected_run = None
        self.log_end = 0
        self.closing = False
        self.page_name = "work"
        self._style()
        self._layout()
        self.refresh()
        self.after(100, self.pump)
        self.after(1000, self.clock_tick)
        self.bind("<Control-Return>", lambda e: self.execute() if self.page_name == "work" else None)
        self.bind("<Control-f>", lambda e: self.search_entry.focus_set() if self.page_name == "work" else None)

    def _style(self):
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                        borderwidth=0, rowheight=36, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background=LINE, foreground=MUTED, relief="flat", font=("Microsoft YaHei UI", 9))
        style.map("Treeview", background=[("selected", "#2b4660")], foreground=[("selected", "#ffffff")])
        style.configure("TCombobox", fieldbackground=INPUT, background=LINE, foreground=FG, arrowcolor=MUTED, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", INPUT)], foreground=[("readonly", FG)], selectbackground=[("readonly", INPUT)], selectforeground=[("readonly", FG)])
        style.configure("Vertical.TScrollbar", background=LINE, troughcolor=INPUT, arrowcolor=MUTED, borderwidth=0)
        style.configure("TPanedwindow", background=BG)
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)

    def _layout(self):
        sidebar = tk.Frame(self, bg="#101827", width=210)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        label(sidebar, "VT / CONSOLE", 18, ACCENT).pack(anchor="w", padx=22, pady=(32, 3))
        label(sidebar, "DRMLAB / unshackle 工作台", 9, MUTED).pack(anchor="w", padx=22, pady=(0, 35))
        label(sidebar, "工 作 空 间", 9, MUTED).pack(anchor="w", padx=22, pady=(0, 12))
        self.nav = {}
        for key, title in (("work", "01   命令工作台"), ("schedule", "02   定时任务"), ("monitor", "03   结果与钩子"), ("settings", "04   工作区设置")):
            b = button(sidebar, title, lambda k=key: self.show_page(k), anchor="w")
            b.pack(fill="x", padx=12, pady=4)
            self.nav[key] = b
        bottom = tk.Frame(sidebar, bg="#101827")
        bottom.pack(side="bottom", fill="x", padx=22, pady=24)
        label(bottom, "●  本地调度引擎", 10, ACCENT).pack(anchor="w")
        label(bottom, "运行时调度 · 串行执行\n关闭窗口将停止调度\n配置与日志独立存储", 9, MUTED, justify="left").pack(anchor="w", pady=12)
        button(bottom, "打开数据目录 ↗", lambda: os.startfile(self.store.folder)).pack(anchor="w")
        label(bottom, f"VTGUI  /  v{__version__}", 9, MUTED).pack(anchor="w", pady=(18, 0))
        main = tk.Frame(self, bg=BG)
        main.pack(side="left", fill="both", expand=True, padx=28, pady=24)
        top = tk.Frame(main, bg=BG)
        top.pack(fill="x", pady=(0, 18))
        self.heading = label(top, "命令工作台", 24)
        self.heading.pack(side="left")
        self.clock = label(top, "", 10, MUTED)
        self.clock.pack(side="right")
        workspace = card(main)
        workspace.pack(fill="x", pady=(0, 18))
        label(workspace, "WORKSPACE", 9, ACCENT).pack(side="left", padx=(16, 12), pady=13)
        self.root_caption = label(workspace, "单次工作目录", 10, anchor="w", width=14)
        self.root_caption.pack(side="left")
        self.work_root = tk.StringVar(value=self.store.settings()["root"])
        self.work_root_combo = ttk.Combobox(workspace, textvariable=self.work_root, values=self.store.settings()["roots"], state="readonly")
        self.work_root_combo.pack(side="left", fill="x", expand=True, padx=8, pady=5)
        self.work_root_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        button(workspace, "添加目录", self.choose_root).pack(side="right", padx=8, pady=5)
        container = tk.Frame(main, bg=BG)
        container.pack(fill="both", expand=True)
        self.pages = {key: tk.Frame(container, bg=BG) for key in self.nav}
        for page in self.pages.values():
            page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._work(self.pages["work"])
        self._schedules(self.pages["schedule"])
        self._monitor(self.pages["monitor"])
        self._settings(self.pages["settings"])
        self.status = label(main, "就绪", 9, MUTED, anchor="w")
        self.status.pack(fill="x", pady=(12, 0))
        self.show_page("work")

    def show_page(self, key):
        self.page_name = key
        self.pages[key].tkraise()
        self.heading.configure(text={"work": "命令工作台", "schedule": "定时任务", "monitor": "结果与钩子", "settings": "工作区设置"}[key])
        for name, b in self.nav.items():
            b.configure(bg="#253f4b" if name == key else "#101827", fg=ACCENT if name == key else MUTED)

    def tree(self, parent, columns, widths):
        frame = tk.Frame(parent, bg=PANEL)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse", height=5)
        for (key, title), width in zip(columns, widths):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=55, stretch=True, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tree.tag_configure("completed", foreground=ACCENT)
        tree.tag_configure("failed", foreground=RED)
        tree.tag_configure("running", foreground=PURPLE)
        tree.tag_configure("queued", foreground="#f5d48b")
        return frame, tree

    def _work(self, page):
        counts = tk.Frame(page, bg=BG)
        counts.pack(fill="x", pady=(0, 14))
        self.counts = {}
        for i, (key, text, color) in enumerate((("queued", "等待执行", "#f5d48b"), ("running", "正在运行", PURPLE), ("completed", "完成任务", ACCENT), ("failed", "失败任务", RED))):
            c = card(counts)
            c.pack(side="left", fill="x", expand=True, padx=(0, 10 if i < 3 else 0))
            label(c, text, 9, MUTED).pack(side="left", padx=14, pady=13)
            self.counts[key] = label(c, "0", 20, color)
            self.counts[key].pack(side="right", padx=18)
        compose = card(page)
        compose.pack(fill="x", pady=(0, 14))
        head = tk.Frame(compose, bg=PANEL)
        head.pack(fill="x", padx=16, pady=(12, 8))
        label(head, "新建单次任务", 12).pack(side="left", padx=(0, 16))
        self.task_name = tk.StringVar(value="单次任务")
        entry(head, self.task_name, 22).pack(side="left", ipady=6)
        settings = self.store.settings()
        self.shell = tk.StringVar(value=settings["shell"])
        ttk.Combobox(head, textvariable=self.shell, values=("cmd", "powershell"), state="readonly", width=13).pack(side="right")
        self.command = tk.Text(compose, height=3, bg=INPUT, fg=FG, insertbackground=FG, font=("Cascadia Code", 11),
                               relief="flat", padx=12, pady=10, undo=True, wrap="word")
        self.command.pack(fill="x", padx=16)
        foot = tk.Frame(compose, bg=PANEL)
        foot.pack(fill="x", padx=16, pady=10)
        label(foot, "使用默认重试/超时 · 移动请保存为定时任务 · Ctrl + Enter 提交", 9, MUTED).pack(side="left")
        button(foot, "▶  执行任务", self.execute, True).pack(side="right")
        button(foot, "保存为定时任务", self.from_command).pack(side="right", padx=10)
        panes = ttk.Panedwindow(page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = card(panes)
        right = card(panes)
        for pane in (left, right):
            pane.columnconfigure(0, weight=1)
            pane.rowconfigure(1, weight=1)
        panes.add(left, weight=2)
        panes.add(right, weight=4)
        title = tk.Frame(left, bg=PANEL)
        title.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        label(title, "执行记录", 12).pack(side="left")
        label(title, "最近 300 条", 9, MUTED).pack(side="right")
        holder, self.runs_tree = self.tree(left, [("name", "任务"), ("status", "状态"), ("attempt", "重试")], [150, 90, 50])
        holder.grid(row=1, column=0, sticky="nsew")
        self.runs_tree.bind("<<TreeviewSelect>>", self.select_run)
        self.runs_tree.bind("<Double-1>", lambda e: self.run_detail())
        actions = tk.Frame(left, bg=PANEL)
        actions.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
        button(actions, "重试", self.retry_selected).pack(side="left", padx=(0, 6))
        button(actions, "取消", self.cancel_selected).pack(side="left", padx=(0, 6))
        button(actions, "详情", self.run_detail).pack(side="left")
        terminal_head = tk.Frame(right, bg=PANEL)
        terminal_head.grid(row=0, column=0, sticky="ew", padx=12, pady=9)
        label(terminal_head, "TERMINAL", 10, ACCENT).pack(side="left")
        self.autoscroll = tk.BooleanVar(value=True)
        check(terminal_head, "跟随输出", self.autoscroll).pack(side="right")
        button(terminal_head, "导出日志", self.export_log, padx=9).pack(side="right", padx=6)
        terminal_body = tk.Frame(right, bg=INPUT)
        terminal_body.grid(row=1, column=0, sticky="nsew")
        self.terminal = tk.Text(terminal_body, bg="#090f1b", fg="#c8d7e9", insertbackground=FG, height=7,
                                font=("Cascadia Code", 10), wrap="word", relief="flat", padx=14, pady=12, state="disabled")
        self.terminal.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(terminal_body, orient="vertical", command=self.terminal.yview)
        sb.pack(side="right", fill="y")
        self.terminal.configure(yscrollcommand=sb.set)
        self.terminal.tag_configure("match", background="#6a5927", foreground="white")
        self.append_terminal("欢迎使用 VTGUI\n\n1. 添加工作目录（至多 3 个）并在顶部选择\n2. 输入命令并执行\n3. 在左侧记录中切换终端输出\n\n终端同时显示标准输出和错误输出；完整日志保存在独立数据目录。\n")
        bottom = tk.Frame(right, bg=PANEL)
        bottom.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 10))
        self.stdin = tk.StringVar()
        stdin_entry = entry(bottom, self.stdin)
        stdin_entry.pack(side="left", fill="x", expand=True, ipady=6)
        stdin_entry.bind("<Return>", lambda e: self.send_input())
        button(bottom, "发送输入 ↵", self.send_input).pack(side="left", padx=(8, 0))
        search = tk.Frame(right, bg=PANEL)
        search.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.search = tk.StringVar()
        label(search, "查找输出", 9, MUTED).pack(side="left", padx=(0, 8))
        self.search_entry = entry(search, self.search, 20)
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.search_entry.bind("<Return>", lambda e: self.find_output())
        button(search, "查找", self.find_output, padx=10, pady=4).pack(side="left", padx=6)
        button(search, "清屏", self.clear_terminal, padx=10, pady=4).pack(side="left")

    def _schedules(self, page):
        label(page, "以周为周期，为重复工作预留时间。", 11, MUTED).pack(anchor="w", pady=(0, 15))
        tools = tk.Frame(page, bg=BG)
        tools.pack(fill="x", pady=(0, 14))
        button(tools, "+ 新建定时任务", lambda: ScheduleDialog(self), True).pack(side="left")
        button(tools, "编辑", self.edit_schedule).pack(side="left", padx=9)
        button(tools, "启用 / 暂停", self.toggle_schedule).pack(side="left")
        button(tools, "立即执行", self.run_schedule).pack(side="left", padx=9)
        button(tools, "删除", self.delete_schedule).pack(side="right")
        holder, self.schedules_tree = self.tree(page, [("name", "任务名称"), ("cycle", "执行周期"), ("time", "执行时间"), ("episodes", "集数进度 / 每轮"), ("next", "下次执行"), ("status", "状态")], [170, 140, 200, 175, 125, 80])
        holder.pack(fill="both", expand=True)
        self.schedules_tree.bind("<Double-1>", lambda e: self.edit_schedule())
        tips = card(page)
        tips.pack(fill="x", pady=(16, 0))
        label(tips, "调度说明", 11, ACCENT).pack(anchor="w", padx=18, pady=(14, 5))
        label(tips, "使用本机时区；每天可直接勾选全部七天。每个任务每天最多设置 4 个时间点。\n多个任务到点后依次执行；程序需保持运行，最小化后仍会调度，关机或休眠期间不补跑。\n任务会保存创建时的项目目录和处理规则；变更全局设置后，可编辑任务更新。", 10, MUTED, justify="left").pack(anchor="w", padx=18, pady=(0, 16))

    def _monitor(self, page):
        area = card(page)
        area.pack(fill="x", pady=(0, 14))
        label(area, "RESULT HOOKS", 10, ACCENT).pack(anchor="w", padx=18, pady=(16, 8))
        label(area, "定时任务完成 → 按任务配置移动       失败 → 延迟重试", 16).pack(anchor="w", padx=18)
        label(area, "本工具启动的任务直接使用进程退出码。外部历史监听每 2 秒检查一次，只处理新增记录。", 10, MUTED).pack(anchor="w", padx=18, pady=(8, 12))
        row = tk.Frame(area, bg=PANEL)
        row.pack(fill="x", padx=18, pady=(0, 16))
        self.monitor_var = tk.BooleanVar(value=self.store.settings()["monitor_enabled"])
        check(row, "监听 DRMLAB 历史记录", self.monitor_var, self.toggle_monitor).pack(side="left")
        button(row, "配置结果规则", lambda: self.show_page("settings")).pack(side="right")
        self.monitor_caption = label(page, "", 9, MUTED, anchor="w", wraplength=900, justify="left")
        self.monitor_caption.pack(fill="x", pady=(0, 12))
        holder, self.events_tree = self.tree(page, [("time", "时间"), ("source", "来源"), ("status", "结果"), ("message", "操作与详情")], [170, 135, 80, 540])
        holder.pack(fill="both", expand=True)
        self.events_tree.bind("<Double-1>", self.event_detail)
        label(page, "外部历史监听仅记录完成/失败和执行失败重试；自动移动请在定时任务内开启。", 9, MUTED).pack(anchor="w", pady=(12, 0))

    def _settings(self, page):
        scroll = ScrollArea(page)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        settings = self.store.settings()
        self.settings_vars = {key: tk.StringVar(value=settings[key]) for key in ("root", "shell", "encoding", "history_file", *TELEGRAM_DEFAULTS)}
        general = card(body)
        general.pack(fill="x", pady=(0, 14))
        label(general, "连接工作区", 13).pack(anchor="w", padx=18, pady=(16, 8))
        form = tk.Frame(general, bg=PANEL)
        form.pack(fill="x", padx=18, pady=(0, 16))
        form.columnconfigure(1, weight=1)
        self.loaded_roots = tuple(settings["roots"])
        self.root_vars = [tk.StringVar(value=settings["roots"][i] if i < len(settings["roots"]) else "") for i in range(3)]
        for i, variable in enumerate(self.root_vars):
            directory_row(form, variable, f"工作目录 {i + 1}", i)
        label(form, "最多 3 个；清空某一行后保存可移除。已保存的任务仍使用原目录。", 9, MUTED).grid(row=3, column=0, columnspan=3, sticky="w", pady=5)
        label(form, "默认工作目录", color=MUTED).grid(row=4, column=0, sticky="w", pady=7)
        self.settings_root_combo = ttk.Combobox(form, textvariable=self.settings_vars["root"], values=settings["roots"], state="readonly",
                                                postcommand=self.update_root_choices)
        self.settings_root_combo.grid(row=4, column=1, columnspan=2, sticky="ew", pady=7)
        directory_row(form, self.settings_vars["history_file"], "历史记录文件", 5, file=True)
        row = tk.Frame(form, bg=PANEL)
        row.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
        for title, key, choices in (("默认终端", "shell", ("cmd", "powershell")), ("输出编码", "encoding", ("utf-8", "gb18030", "gbk"))):
            label(row, title, color=MUTED).pack(side="left", padx=(0, 10))
            ttk.Combobox(row, textvariable=self.settings_vars[key], values=choices, width=14, state="readonly" if key == "shell" else "normal").pack(side="left", padx=(0, 24))
        rules = card(body)
        rules.pack(fill="x", pady=(0, 14))
        label(rules, "默认执行规则", 13).pack(anchor="w", padx=18, pady=(16, 8))
        label(rules, "仅配置失败重试及超时；每个定时任务独立配置自己的移动规则。", 9, MUTED).pack(anchor="w", padx=18, pady=(0, 5))
        self.policy = PolicyForm(rules, settings, task_scope=False)
        self.policy.pack(fill="x", padx=18, pady=(0, 16))
        telegram = card(body)
        telegram.pack(fill="x", pady=(0, 14))
        label(telegram, "Telegram Bot 通知", 13).pack(anchor="w", padx=18, pady=(16, 8))
        notification_form = tk.Frame(telegram, bg=PANEL)
        notification_form.pack(fill="x", padx=18, pady=(0, 16))
        notification_form.columnconfigure(1, weight=1)
        label(notification_form, "Bot Token", color=MUTED).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=7)
        self.telegram_token_entry = entry(notification_form, self.settings_vars["telegram_bot_token"])
        self.telegram_token_entry.configure(show="•")
        self.telegram_token_entry.grid(row=0, column=1, sticky="ew", ipady=7, pady=7)
        show_token = tk.BooleanVar(value=False)
        check(notification_form, "显示", show_token,
              lambda: self.telegram_token_entry.configure(show="" if show_token.get() else "•")).grid(row=0, column=2, padx=(10, 0))
        label(notification_form, "接收聊天 ID", color=MUTED).grid(row=1, column=0, sticky="w", padx=(0, 12), pady=7)
        entry(notification_form, self.settings_vars["telegram_chat_ids"]).grid(row=1, column=1, columnspan=2, sticky="ew", ipady=7, pady=7)
        label(notification_form, "多个 ID 用逗号分隔，例如 123456789,-1001234567890，也支持 @频道用户名。\n保存两项后启用；两项均清空可关闭。通知任务完成、失败、取消及外部历史新增结果，每次重试和每集分别通知。\n私聊需先向 Bot 发送 /start，群组或频道需授予 Bot 发送权限。Token 保存在本机配置数据库中。", 9, MUTED,
              justify="left", wraplength=850).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 12))
        button(notification_form, "发送测试通知", self.test_telegram).grid(row=3, column=0, columnspan=3, sticky="w")
        info = card(body)
        info.pack(fill="x", pady=(0, 14))
        label(info, "独立运行", 12, ACCENT).pack(anchor="w", padx=18, pady=(14, 6))
        label(info, "支持 DRMLAB / unshackle，无需导入项目模块。执行命令时自动加入所选目录的 .venv/Scripts 和 binaries 到 PATH。\n剧集模式会搬走整部目录（包括已有集数）；请将成品目录设为各剧集目录的上级，并避免其他程序同时写入。\n交互输入适用于普通文本提示；全屏终端程序、密码隐藏提示等不提供完整 TTY 支持。", 9, MUTED, justify="left", wraplength=890).pack(anchor="w", padx=18, pady=(0, 14))
        tools = tk.Frame(page, bg=BG)
        tools.pack(fill="x", pady=(14, 0))
        button(tools, "保存设置", self.save_settings, True).pack(side="right")
        button(tools, "检查运行环境", self.check_environment).pack(side="left")
        label(tools, "所有配置与完整日志保存在本机用户数据目录。", 9, MUTED).pack(side="left", padx=14)

    def update_root_choices(self):
        roots = normalize_roots([v.get() for v in self.root_vars])
        self.settings_root_combo.configure(values=roots)

    def choose_root(self):
        value = filedialog.askdirectory(parent=self, title="添加工作目录（至多 3 个）", initialdir=self.work_root.get() or None)
        if value:
            try:
                self.store.save_settings({"root": value})
                self.settings_vars["root"].set(self.store.settings()["root"])
                self.work_root.set(self.store.settings()["root"])
                self.refresh()
                self.status.configure(text="工作目录已添加并选中；已有任务的目录保持原配置。")
            except (ValueError, OSError) as exc:
                messagebox.showerror("无法添加目录", str(exc), parent=self)

    def save_settings(self):
        try:
            values = {k: v.get().strip() for k, v in self.settings_vars.items()}
            roots = normalize_roots([v.get() for v in self.root_vars])
            values["roots"] = roots
            values["root"] = next((p for p in roots if values["root"] and Path(p) == Path(values["root"])), roots[0] if roots else "")
            values.update(validate_telegram(values))
            values.update(self.policy.values())
            job = validate_job({**values, "root": values["root"] or str(self.store.folder), "command": "环境校验"})
            values.update({k: job[k] for k in ("retries", "retry_delay", "timeout")})
            if not values["history_file"]:
                raise ValueError("历史记录文件路径不能为空。")
            old = self.store.settings()
            if values["root"] != old["root"] or values["history_file"] != old["history_file"]:
                values["monitor_baseline"] = True
            self.store.save_settings(values)
            self.shell.set(values["shell"])
            self.refresh()
            self.status.configure(text="设置已保存。")
        except (ValueError, LookupError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self)

    def test_telegram(self):
        try:
            config = validate_telegram({k: self.settings_vars[k].get() for k in TELEGRAM_DEFAULTS})
            if not config["telegram_bot_token"]:
                raise ValueError("请先填写 Bot Token 和接收聊天 ID。")
            self.engine.telegram.submit(config, "VTGUI · Telegram 测试通知\n通知配置可正常发送。")
            self.status.configure(text="测试通知已加入发送队列，结果请查看“结果与钩子”；此操作不保存设置。")
        except ValueError as exc:
            messagebox.showerror("Telegram 配置无效", str(exc), parent=self)

    def toggle_monitor(self):
        settings = self.store.settings()
        if self.monitor_var.get() and (not settings["root"] or not Path(settings["root"]).is_dir()):
            self.monitor_var.set(False)
            messagebox.showerror("尚未连接", "请先选择项目目录（DRMLAB / unshackle）。", parent=self)
            return
        self.store.save_settings({"monitor_enabled": self.monitor_var.get(), "monitor_baseline": True})
        self.refresh()

    def current_job(self):
        settings = self.store.settings()
        return validate_job({**settings, "root": self.work_root.get() or settings["root"], "name": self.task_name.get(), "shell": self.shell.get(),
                             "command": self.command.get("1.0", "end-1c")})

    def execute(self):
        try:
            ident = self.engine.submit(self.current_job())
            self.refresh()
            self.runs_tree.selection_set(ident)
            self.runs_tree.see(ident)
            self.select_run()
            self.status.configure(text="已加入执行队列。")
        except (ValueError, LookupError) as exc:
            messagebox.showerror("无法执行", str(exc), parent=self)

    def from_command(self):
        try:
            ScheduleDialog(self, {"job": self.current_job(), "time": "09:00", "days": list(range(7)), "enabled": True})
        except (ValueError, LookupError) as exc:
            messagebox.showerror("无法创建", str(exc), parent=self)

    def selected_schedule(self):
        selected = self.schedules_tree.selection()
        return next((s for s in self.store.schedules() if selected and s["id"] == selected[0]), None)

    def edit_schedule(self):
        if spec := self.selected_schedule():
            ScheduleDialog(self, spec)

    def toggle_schedule(self):
        if spec := self.selected_schedule():
            spec["enabled"] = not spec["enabled"]
            self.store.save_schedule(spec)
            self.refresh()

    def run_schedule(self):
        if spec := self.selected_schedule():
            try:
                ident = self.engine.submit_schedule(spec["id"])
                self.refresh()
                if ident:
                    self.show_page("work")
                    self.runs_tree.selection_set(ident)
                    self.select_run()
                else:
                    self.status.configure(text="已有逐集批次在执行，或全部集数已完成；详见结果事件。")
            except (ValueError, LookupError) as exc:
                messagebox.showerror("无法执行", str(exc), parent=self)

    def delete_schedule(self):
        if spec := self.selected_schedule():
            if messagebox.askyesno("删除定时任务", f"删除“{spec['job']['name']}”？后续集数将停止，正在执行的当前集继续，执行记录会保留。", parent=self):
                self.store.delete_schedule(spec["id"])
                self.refresh()

    def selected(self):
        selected = self.runs_tree.selection()
        return self.store.run(selected[0]) if selected else None

    def retry_selected(self):
        if run := self.selected():
            if run["status"] in ("running", "queued"):
                self.status.configure(text="任务尚未结束，可先取消。")
                return
            if run.get("batch"):
                self.status.configure(text="逐集任务请在定时任务页点击“立即执行”，从尚未完成的集继续。")
                return
            if run["job"].get("move_only"):
                self.status.configure(text="归档任务请查看日志并检查目标目录；不会自动重跑归档。")
                return
            try:
                ident = self.engine.submit(run["job"], "手动重试")
                self.refresh()
                self.runs_tree.selection_set(ident)
                self.select_run()
            except (ValueError, LookupError) as exc:
                messagebox.showerror("无法重试", str(exc), parent=self)

    def cancel_selected(self):
        if run := self.selected():
            if run["status"] in ("running", "queued"):
                self.engine.cancel(run["id"])
                self.status.configure(text="正在取消任务；完成后不会触发失败重试。")

    def select_run(self, event=None):
        run = self.selected()
        if not run or run["id"] == self.selected_run:
            return
        self.selected_run = run["id"]
        self.log_end = 0
        self.clear_terminal()
        path = self.store.logs / f"{run['id']}.log"
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - 1024 * 1024))
                text = handle.read().decode("utf-8", errors="replace")
                self.log_end = handle.tell()
            self.append_terminal(("[仅显示日志末尾 1MB；可导出完整日志]\n" if size > 1024 * 1024 else "") + text)
        except FileNotFoundError:
            self.append_terminal(f"任务：{run['job']['name']}\n目录：{run['job']['root']}\n$ {run['job']['command']}\n\n等待执行…\n")

    def run_detail(self):
        if run := self.selected():
            job = run["job"]
            value = f"任务：{job['name']}\n状态：{STATUSES[run['status']]}\n来源：{run['origin']}\n创建：{run['created']}\n开始：{run['started'] or '—'}\n结束：{run['ended'] or '—'}\n自动重试：{run['attempt']}/{job['retries']}\n结果：{run['message']}\n目录：{job['root']}\n\n命令：\n{job['command']}\n\n日志：{self.store.logs / (run['id'] + '.log')}\n\n任务配置：\n{json.dumps(job, ensure_ascii=False, indent=2)}"
            self.text_dialog("任务详情", value)

    def event_detail(self, event=None):
        selected = self.events_tree.selection()
        if selected:
            rows = self.store.rows("SELECT * FROM events WHERE id=?", (selected[0],))
            if rows:
                r = rows[0]
                self.text_dialog("结果事件", f"{r['time']}\n{r['source']} · {STATUSES.get(r['status'], r['status'])}\n\n{r['message']}")

    def text_dialog(self, title, text):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("820x580")
        win.configure(bg=BG)
        area = tk.Text(win, bg=INPUT, fg=FG, relief="flat", wrap="word", font=("Microsoft YaHei UI", 10), padx=18, pady=18)
        area.pack(fill="both", expand=True, padx=15, pady=15)
        area.insert("1.0", text)
        area.configure(state="disabled")
        button(win, "关闭", win.destroy).pack(pady=(0, 15))

    def clear_terminal(self):
        self.terminal.configure(state="normal")
        self.terminal.delete("1.0", "end")
        self.terminal.configure(state="disabled")

    def append_terminal(self, text):
        self.terminal.configure(state="normal")
        self.terminal.insert("end", text.replace("\r\n", "\n").replace("\r", "\n"))
        if int(self.terminal.count("1.0", "end", "chars")[0]) > 1024 * 1024:
            self.terminal.delete("1.0", "end-800000c")
        self.terminal.configure(state="disabled")
        if self.autoscroll.get():
            self.terminal.see("end")

    def find_output(self):
        needle = self.search.get()
        self.terminal.tag_remove("match", "1.0", "end")
        if not needle:
            return
        index, first, count = "1.0", None, 0
        while index := self.terminal.search(needle, index, stopindex="end", nocase=True):
            end = f"{index}+{len(needle)}c"
            self.terminal.tag_add("match", index, end)
            first = first or index
            index = end
            count += 1
            if count >= 2000:
                break
        if first:
            self.terminal.see(first)
        self.status.configure(text=f"查找到 {count} 处匹配。")

    def export_log(self):
        if run := self.selected():
            source = self.store.logs / f"{run['id']}.log"
            if not source.exists():
                self.status.configure(text="任务尚未生成日志。")
                return
            target = filedialog.asksaveasfilename(parent=self, title="导出完整日志", defaultextension=".log", initialfile=f"vtgui-{run['id'][:8]}.log")
            if target:
                try:
                    if source.resolve() != Path(target).resolve():
                        shutil.copyfile(source, target)
                    self.status.configure(text=f"日志已导出：{target}")
                except OSError as exc:
                    messagebox.showerror("导出失败", str(exc), parent=self)

    def send_input(self):
        if self.selected_run != self.engine.active:
            self.status.configure(text="请先选择正在运行的任务。")
            return
        try:
            self.engine.send_input(self.stdin.get())
            self.stdin.set("")
        except (ValueError, OSError) as exc:
            self.status.configure(text=str(exc))

    def check_environment(self):
        root = self.settings_vars["root"].get()
        if not root or not Path(root).is_dir():
            self.text_dialog("环境检查", "请选择有效的项目目录（DRMLAB / unshackle）。")
            return
        paths = [str(Path(root) / ".venv" / "Scripts"), str(Path(root) / "binaries"), os.environ.get("PATH", "")]
        path = os.pathsep.join(paths)
        lines = [f"项目：{project_name(root)}", f"目录：{root}", "", "目录结构："]
        for name in ("pyproject.toml", "vinetrimmer", "unshackle", "downloads", ".venv"):
            lines.append(f"{'✓' if (Path(root) / name).exists() else '○'}  {name}")
        lines.extend(["", "命令查找（不执行下载项目）："])
        for exe in ("uv", "unshackle", "poetry", "vt", "python", "ffmpeg", "mkvmerge"):
            lines.append(f"{exe}：{shutil.which(exe, path=path) or '未在 PATH 中找到'}")
        history = resolved(root, self.settings_vars["history_file"].get())
        lines.extend(["", f"历史文件：{history}", "存在" if history.is_file() else "尚不存在（仅适用于 DRMLAB 原 GUI；unshackle 无需开启此监听）",
                      "", "本工具仅负责运行命令；所选项目的运行环境仍应已安装。"])
        self.text_dialog("环境检查", "\n".join(lines))

    @staticmethod
    def sync_tree(tree, records):
        previous = tree.selection()
        wanted = {str(ident) for ident, _, _ in records}
        for ident in tree.get_children():
            if ident not in wanted:
                tree.delete(ident)
        for pos, (ident, values, tag) in enumerate(records):
            ident = str(ident)
            if tree.exists(ident):
                tree.item(ident, values=values, tags=(tag,))
                tree.move(ident, "", pos)
            else:
                tree.insert("", "end", iid=ident, values=values, tags=(tag,))
        if previous and tree.exists(previous[0]):
            tree.selection_set(previous)

    def refresh(self):
        settings = self.store.settings()
        root = settings["root"]
        roots = settings["roots"]
        self.work_root_combo.configure(values=roots)
        if self.work_root.get() not in roots:
            self.work_root.set(root or (roots[0] if roots else ""))
        selected_root = self.work_root.get()
        self.root_caption.configure(text=project_name(selected_root) if selected_root else "单次工作目录", fg=FG if selected_root else "#f5d48b")
        if tuple(roots) != self.loaded_roots:
            for i, variable in enumerate(self.root_vars):
                variable.set(roots[i] if i < len(roots) else "")
            self.loaded_roots = tuple(roots)
            self.settings_vars["root"].set(root)
        self.settings_root_combo.configure(values=roots)
        runs = self.store.rows("SELECT * FROM runs ORDER BY created DESC,rowid DESC LIMIT 300")
        records = []
        for r in runs:
            job = json.loads(r["job"])
            records.append((r["id"], (job["name"], STATUSES[r["status"]], str(r["attempt"])), r["status"]))
        self.sync_tree(self.runs_tree, records)
        counts = {r["status"]: r["n"] for r in self.store.rows("SELECT status,COUNT(*) n FROM runs GROUP BY status")}
        for key, widget in self.counts.items():
            widget.configure(text=str(counts.get(key, 0)))
        records = []
        for spec in self.store.schedules():
            cycle = "每天" if len(spec["days"]) == 7 else "、".join(WEEK[d] for d in spec["days"])
            progress = self.store.episode_progress(spec)
            exhausted = progress is not None and progress > int(spec.get("episode_total", 1))
            episode_text = "普通任务" if progress is None else f"下次 {progress} / 共 {spec.get('episode_total', 1)} · 每轮 {spec.get('episodes_per_trigger', 1)}"
            if exhausted:
                episode_text = f"全部 {spec.get('episode_total', 1)} 集已完成"
            records.append((spec["id"], (spec["job"]["name"], cycle, " / ".join(schedule_times(spec)),
                           episode_text, next_occurrence(spec).strftime("%m-%d %H:%M") if spec["enabled"] and not exhausted else "—",
                           "全部完成" if exhausted else "已启用" if spec["enabled"] else "已暂停"), "completed" if spec["enabled"] else ""))
        self.sync_tree(self.schedules_tree, records)
        events = self.store.rows("SELECT * FROM events ORDER BY id DESC LIMIT 300")
        self.sync_tree(self.events_tree, [(r["id"], (r["time"][5:19].replace("T", " "), r["source"], STATUSES.get(r["status"], r["status"]), r["message"]), r["status"]) for r in events])
        self.monitor_var.set(settings["monitor_enabled"])
        self.monitor_caption.configure(text=f"{'● 正在监听' if settings['monitor_enabled'] else '○ 历史监听已关闭'}  ·  {resolved(root, settings['history_file']) if root else '请先选择项目目录'}\n失败重试 {settings['retries']} 次 / 间隔 {settings['retry_delay']} 秒  ·  自动移动仅由定时任务单独配置")

    def pump(self):
        if self.closing:
            return
        changed = False
        batch = []
        for _ in range(800):
            try:
                event = self.engine.events.get_nowait()
            except queue.Empty:
                break
            if event["kind"] == "changed":
                changed = True
            elif event["kind"] == "output" and event["id"] == self.selected_run:
                if event["end"] > self.log_end:
                    batch.append(event["text"])
                    self.log_end = event["end"]
        if batch:
            self.append_terminal("".join(batch))
        if changed:
            self.refresh()
        self.after(100, self.pump)

    def clock_tick(self):
        if self.closing:
            return
        self.clock.configure(text=datetime.now().astimezone().strftime("%Y.%m.%d  %H:%M:%S  %Z"))
        if datetime.now().second == 0:
            self.refresh()
        self.after(1000, self.clock_tick)

    def on_close(self):
        if self.closing:
            return
        queued = self.store.rows("SELECT COUNT(*) n FROM runs WHERE status='queued'")[0]["n"]
        if self.engine.active or queued:
            if not messagebox.askyesno("退出 VTGUI", "退出将终止当前任务并停止调度。等待队列会保留到下次启动。是否退出？", parent=self):
                return
        self.closing = True
        self.status.configure(text="正在停止引擎和子进程…")
        done = threading.Event()
        def stop():
            try:
                self.engine.close()
            finally:
                done.set()
        threading.Thread(target=stop, daemon=True).start()
        def wait():
            if done.is_set():
                self.destroy()
            else:
                self.after(100, wait)
        wait()
