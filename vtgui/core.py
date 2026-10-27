from __future__ import annotations

import codecs
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable

from .archive import archive_plan, folder_snapshot, move_files, move_folders
from .projects import FinalOutputs
from .rename import RENAME_DEFAULTS, RenameError, file_identity, rename_enabled, rename_items, restore_unmoved, validate_rename
from .telegram import TELEGRAM_DEFAULTS, TelegramNotifier, validate_telegram


STATUSES = {"queued": "等待中", "running": "执行中", "completed": "已完成",
            "failed": "失败", "cancelled": "已取消", "interrupted": "已中断"}
DEFAULTS = {
    **RENAME_DEFAULTS,
    "root": "", "shell": "cmd", "encoding": "utf-8", "retries": 0,
    "retry_delay": 60, "timeout": 0, "move_enabled": False,
    "move_mode": "folders",
    "source": "downloads", "destination": "", "patterns": "*.mkv;*.mp4;*.mka;*.mks",
    "monitor_enabled": False, "history_file": "vinetrimmer/download_history.json",
    "monitor_baseline": True,
}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TASK_ONLY_FIELDS = {"move_enabled", "destination", "move_mode", "source", "patterns", *RENAME_DEFAULTS}


def stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def resolved(root: str, value: str) -> Path:
    p = Path(value).expanduser()
    return (p if p.is_absolute() else Path(root) / p).resolve()


def normalize_roots(values: list[str]) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("工作目录配置应为目录列表。")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("工作目录必须是路径文本。")
        if not value.strip():
            continue
        try:
            path = Path(value.strip()).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"无效的工作目录：{value}") from exc
        if path not in [Path(item) for item in result]:
            result.append(str(path))
    if len(result) > 3:
        raise ValueError("最多只能添加 3 个工作目录，请先在工作区设置中移除一个目录。")
    return result


def validate_job(job: dict) -> dict:
    job = {**DEFAULTS, **job}
    job.pop("roots", None)  # The workspace registry is not part of a task.
    for key in TELEGRAM_DEFAULTS:
        job.pop(key, None)  # Credentials belong only to workspace settings.
    if not job.get("command", "").strip() and not job.get("argv"):
        raise ValueError("请输入终端指令。")
    if not job["root"] or not Path(job["root"]).is_dir():
        raise ValueError("请选择有效的项目目录（DRMLAB / unshackle）。")
    job["root"] = str(Path(job["root"]).resolve())
    if job["shell"] not in ("cmd", "powershell"):
        raise ValueError("终端类型无效。")
    codecs.lookup(job["encoding"])
    job.update(validate_rename(job))
    if rename_enabled(job) and not job["source"].strip():
        raise ValueError("启用重命名时必须填写成品目录。")
    if job["move_mode"] not in ("files", "folders"):
        raise ValueError("移动方式无效，请选择成品文件或剧集文件夹。")
    for field, minimum, maximum in (("retries", 0, 20), ("retry_delay", 1, 86400), ("timeout", 0, 604800)):
        try:
            job[field] = int(job[field])
        except (ValueError, TypeError):
            raise ValueError(f"{field} 必须是整数。") from None
        if not minimum <= job[field] <= maximum:
            raise ValueError(f"{field} 应在 {minimum}–{maximum} 之间。")
    if job["move_enabled"]:
        if not job["source"].strip() or not job["destination"].strip():
            raise ValueError("启用移动时必须填写成品目录和目标目录。")
        source, dest = resolved(job["root"], job["source"]), resolved(job["root"], job["destination"])
        if source == dest or source in dest.parents or dest in source.parents:
            raise ValueError("成品目录和目标目录不能相同，也不能互相包含。")
    if job["move_enabled"] or rename_enabled(job):
        patterns = [p.strip() for p in job["patterns"].split(";") if p.strip()]
        if not patterns or any("/" in p or "\\" in p for p in patterns):
            raise ValueError("请输入以分号分隔的文件名通配符，例如 *.mkv;*.mp4。")
    job["name"] = str(job.get("name") or "单次任务").strip()[:120]
    return job


def episodic(spec: dict) -> bool:
    job = spec["job"]
    return "{episode}" in job.get("command", "") or any("{episode}" in a for a in job.get("argv", []))


def normalize_schedule_time(value: str) -> str:
    try:
        hh, mm = (int(x) for x in value.split(":"))
        if not 0 <= hh <= 23 or not 0 <= mm <= 59:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("执行时间格式应为 HH:MM（24 小时制）。") from None
    return f"{hh:02}:{mm:02}"


def schedule_times(spec: dict) -> list[str]:
    return sorted({spec["time"], *spec.get("extra_times", [])})


def validate_schedule(value: dict) -> dict:
    value = dict(value)
    value["job"] = validate_job(value["job"])
    value["time"] = normalize_schedule_time(value.get("time"))
    extra_times = value.get("extra_times", [])
    if not isinstance(extra_times, list) or len(extra_times) > 3:
        raise ValueError("每个定时任务最多额外添加 3 个执行时间。")
    value["extra_times"] = [normalize_schedule_time(when) for when in extra_times]
    if len(schedule_times(value)) != 1 + len(value["extra_times"]):
        raise ValueError("执行时间不能重复，请为每个时间点设置不同的时间。")
    days = sorted(set(value.get("days", [])))
    if not days or any(type(d) is not int or d not in range(7) for d in days):
        raise ValueError("请至少选择一星期中的一天。")
    value["days"] = days
    value["enabled"] = bool(value.get("enabled", True))
    if episodic(value):
        start = str(value.get("episode_start", "1")).strip()
        if not re.fullmatch(r"[0-9]{1,6}", start):
            raise ValueError("起始 Episode 应为 1～6 位数字，可填写 01 保留补零格式。")
        value["episode_start"] = start
        for key, title, maximum in (("episode_total", "总集数", 999999), ("episodes_per_trigger", "每次触发集数", 1000)):
            raw = str(value.get(key, "1")).strip()
            if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= maximum:
                raise ValueError(f"{title} 必须是 1～{maximum} 的整数。")
            value[key] = int(raw)
        if int(start) > value["episode_total"]:
            raise ValueError("起始 Episode 不能大于总集数（末集编号）。")
    return value


def next_occurrence(spec: dict, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    times = [tuple(map(int, when.split(":"))) for when in schedule_times(spec)]
    for day in range(8):
        date = now + timedelta(days=day)
        if date.weekday() in spec["days"]:
            for hh, mm in times:
                candidate = date.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if candidate > now:
                    return candidate
    raise ValueError("无效的周期")


class Store:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.logs = self.folder / "logs"
        self.logs.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.folder / "vtgui.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY, spec TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, job TEXT NOT NULL, status TEXT NOT NULL,
                origin TEXT NOT NULL, created TEXT NOT NULL, started TEXT, ended TEXT,
                code INTEGER, attempt INTEGER NOT NULL, parent TEXT,
                available REAL NOT NULL, message TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS fires (
                schedule TEXT NOT NULL, slot TEXT NOT NULL, PRIMARY KEY(schedule, slot));
            CREATE TABLE IF NOT EXISTS seen (
                source TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(source, fingerprint));
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                source TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS episode_state (
                schedule TEXT PRIMARY KEY, next_episode INTEGER NOT NULL, start_token TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS episode_batches (
                id TEXT PRIMARY KEY, schedule TEXT NOT NULL, status TEXT NOT NULL,
                start_episode INTEGER NOT NULL, end_episode INTEGER NOT NULL,
                current_episode INTEGER NOT NULL, template TEXT NOT NULL,
                last_run TEXT, created TEXT NOT NULL);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        for name, kind in (("batch", "TEXT"), ("episode", "INTEGER")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
        self.db.execute("CREATE INDEX IF NOT EXISTS episode_batch_schedule ON episode_batches(schedule,status)")
        self.db.execute("UPDATE runs SET status='interrupted', ended=?, message=? WHERE status='running'",
                        (stamp(), "程序上次退出时任务未结束，请确认后手动重试。"))
        self.db.execute("""UPDATE episode_batches SET status='stopped' WHERE status='running'
                           AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.id=episode_batches.last_run
                                           AND runs.status IN ('queued','running'))""")
        # Retire legacy global post-processing without changing saved schedule policies.
        self.db.executemany("DELETE FROM settings WHERE key=?", [(key,) for key in TASK_ONLY_FIELDS])
        for row in self.db.execute("SELECT id,job,origin,parent,batch FROM runs WHERE status='queued'").fetchall():
            job = json.loads(row["job"])
            scheduled = bool(row["batch"] or job.get("schedule_id"))
            ancestor = row
            visited = set()
            while ancestor and not scheduled:
                scheduled = ancestor["origin"] in ("定时", "手动定时任务", "逐集续跑")
                if not ancestor["parent"] or ancestor["parent"] in visited:
                    break
                visited.add(ancestor["parent"])
                ancestor = self.db.execute("SELECT origin,parent FROM runs WHERE id=?", (ancestor["parent"],)).fetchone()
            if not scheduled and (job.get("move_enabled") or rename_enabled(job)):
                job.update(move_enabled=False, destination="", **RENAME_DEFAULTS)
                self.db.execute("UPDATE runs SET job=? WHERE id=?", (json.dumps(job, ensure_ascii=False), row["id"]))
        self.db.commit()

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def write(self, sql: str, args=()):
        with self.lock, self.db:
            self.db.execute(sql, args)

    def settings(self) -> dict:
        settings = {**DEFAULTS, **TELEGRAM_DEFAULTS, **{r["key"]: json.loads(r["value"]) for r in self.rows("SELECT * FROM settings") if r["key"] not in TASK_ONLY_FIELDS}}
        # Existing single-directory installations become the first workspace.
        roots = list(settings.get("roots", []))
        if settings["root"]:
            roots.append(settings["root"])
        settings["roots"] = normalize_roots(roots)
        settings["root"] = next((p for p in settings["roots"] if Path(p) == Path(settings["root"])), "") if settings["root"] else ""
        return settings

    def save_settings(self, values: dict):
        with self.lock, self.db:
            values = dict(values)
            current = self.settings()
            if "root" in values or "roots" in values:
                roots = normalize_roots(values.get("roots", current["roots"]))
                selected = values.get("root", current["root"])
                if "roots" not in values and selected:
                    roots = normalize_roots([*roots, selected])
                for path in roots:
                    if path not in current["roots"] and not Path(path).is_dir():
                        raise ValueError(f"工作目录不存在：{path}")
                selected = next((p for p in roots if selected and Path(p) == Path(selected)), roots[0] if roots else "")
                values.update(roots=roots, root=selected)
                if selected != current["root"]:
                    values["monitor_baseline"] = True
            if TELEGRAM_DEFAULTS.keys() & values.keys():
                values.update(validate_telegram({**current, **values}))
            self.db.executemany("DELETE FROM settings WHERE key=?", [(key,) for key in TASK_ONLY_FIELDS])
            self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items() if k not in TASK_ONLY_FIELDS])

    def schedules(self) -> list[dict]:
        return [{**json.loads(r["spec"]), "id": r["id"]} for r in self.rows("SELECT * FROM schedules")]

    def save_schedule(self, spec: dict, *, reset_episode=False) -> str:
        spec = validate_schedule(spec)
        ident = spec.get("id") or uuid.uuid4().hex
        spec["id"] = ident
        with self.lock, self.db:
            state = self.db.execute("SELECT * FROM episode_state WHERE schedule=?", (ident,)).fetchone()
            active = self.db.execute("SELECT template FROM episode_batches WHERE schedule=? AND status='running'", (ident,)).fetchone()
            if active:
                active_spec = json.loads(active["template"])
                if reset_episode or not episodic(spec) or not state or any(spec.get(k) != active_spec.get(k) for k in ("episode_start", "episode_total", "episodes_per_trigger")):
                    raise ValueError("逐集任务正在执行，暂不能修改集数配置或移除占位符；请先取消当前集。")
            if episodic(spec) and (reset_episode or not state or state["start_token"] != spec["episode_start"]):
                self.db.execute("INSERT OR REPLACE INTO episode_state VALUES (?,?,?)",
                                (ident, int(spec["episode_start"]), spec["episode_start"]))
            self.db.execute("INSERT OR REPLACE INTO schedules VALUES (?,?)", (ident, json.dumps(spec, ensure_ascii=False)))
        return ident

    def _enqueue(self, job: dict, origin="单次", attempt=0, parent=None, delay=0, batch=None, episode=None) -> str:
        job = validate_job(job)
        ident = uuid.uuid4().hex
        self.db.execute("INSERT INTO runs (id,job,status,origin,created,attempt,parent,available,batch,episode) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ident, json.dumps(job, ensure_ascii=False), "queued", origin, stamp(), attempt, parent, time.time() + delay, batch, episode))
        return ident

    def enqueue(self, job: dict, origin="单次", attempt=0, parent=None, delay=0) -> str:
        with self.lock, self.db:
            return self._enqueue(job, origin, attempt, parent, delay)

    def episode_progress(self, spec: dict) -> int | None:
        if not episodic(spec):
            return None
        rows = self.rows("SELECT next_episode FROM episode_state WHERE schedule=?", (spec["id"],))
        return rows[0]["next_episode"] if rows else int(spec.get("episode_start", "1"))

    def _event(self, source, status, message):
        self.db.execute("INSERT INTO events(time,source,status,message) VALUES(?,?,?,?)", (stamp(), source, status, message))

    def _queue_episode(self, batch_id: str, spec: dict, episode: int, origin: str) -> str:
        number = str(episode).zfill(len(spec["episode_start"]))
        job = dict(spec["job"])
        job["schedule_id"] = spec["id"]
        job["command"] = job["command"].replace("{episode}", number)
        if job.get("argv"):
            job["argv"] = [a.replace("{episode}", number) for a in job["argv"]]
        job["name"] = f"{job['name']} · Episode {number}"
        ident = self._enqueue(job, origin, batch=batch_id, episode=episode)
        self.db.execute("UPDATE episode_batches SET current_episode=?,last_run=? WHERE id=?", (episode, ident, batch_id))
        return ident

    def _trigger_schedule(self, ident: str, origin: str) -> str | None:
        row = self.db.execute("SELECT spec FROM schedules WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("定时任务已删除。")
        spec = validate_schedule(json.loads(row["spec"]))
        if not episodic(spec):
            return self._enqueue({**spec["job"], "schedule_id": ident}, origin)
        if self.db.execute("SELECT 1 FROM episode_batches WHERE schedule=? AND status='running'", (ident,)).fetchone():
            self._event(spec["job"]["name"], "queued", "上一轮逐集任务尚未结束，本次触发已跳过。")
            return None
        state = self.db.execute("SELECT * FROM episode_state WHERE schedule=?", (ident,)).fetchone()
        episode = state["next_episode"] if state else int(spec["episode_start"])
        if not state:
            self.db.execute("INSERT INTO episode_state VALUES (?,?,?)", (ident, episode, spec["episode_start"]))
        if episode > spec["episode_total"]:
            self._event(spec["job"]["name"], "completed", "已完成全部集数，不再执行。")
            return None
        end = min(episode + spec["episodes_per_trigger"] - 1, spec["episode_total"])
        batch_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO episode_batches VALUES (?,?,?,?,?,?,?,?,?)",
                        (batch_id, ident, "running", episode, end, episode, json.dumps(spec, ensure_ascii=False), None, stamp()))
        result = self._queue_episode(batch_id, spec, episode, origin)
        self._event(spec["job"]["name"], "queued", f"本轮 Episode {episode}～{end}，仅当前集完成后安排下一集。")
        return result

    def trigger_schedule(self, ident: str, origin="手动定时任务") -> str | None:
        with self.lock, self.db:
            return self._trigger_schedule(ident, origin)

    def fire(self, spec: dict, slot: str) -> bool:
        # Claim the minute and enqueue in one transaction; restarts cannot duplicate it.
        with self.lock, self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO fires VALUES (?,?)", (spec["id"], slot))
            if not cur.rowcount:
                return False
            return self._trigger_schedule(spec["id"], "定时") is not None

    def finish_run(self, ident: str, status: str, code, message: str, allow_followups=True):
        """Commit the result, retry or next episode, and progress as a single transaction."""
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] not in ("running", "queued"):
                return
            run = dict(row)
            job = json.loads(run["job"])
            self.db.execute("UPDATE runs SET status=?,ended=?,code=?,message=? WHERE id=?", (status, stamp(), code, message, ident))
            self._event(job["name"], status, message)
            batch = self.db.execute("SELECT * FROM episode_batches WHERE id=? AND status='running' AND last_run=?",
                                    (run["batch"], ident)).fetchone() if run["batch"] else None
            if status == "failed" and run["attempt"] < job["retries"] and allow_followups and not job.get("move_only") and (not run["batch"] or batch):
                try:
                    retry = self._enqueue(job, "自动重试", run["attempt"] + 1, run["parent"] or ident,
                                          job["retry_delay"], run["batch"], run["episode"])
                    if batch:
                        self.db.execute("UPDATE episode_batches SET last_run=? WHERE id=?", (retry, batch["id"]))
                    self._event(job["name"], "queued", f"第 {run['attempt'] + 1}/{job['retries']} 次重试，{job['retry_delay']} 秒后执行")
                    return
                except (ValueError, LookupError) as exc:
                    self._event(job["name"], "failed", f"无法安排重试：{exc}")
            if not batch:
                return
            spec = json.loads(batch["template"])
            if status == "completed":
                self.db.execute("UPDATE episode_state SET next_episode=MAX(next_episode,?) WHERE schedule=? AND start_token=?",
                                (run["episode"] + 1, batch["schedule"], spec["episode_start"]))
                if run["episode"] < batch["end_episode"] and allow_followups:
                    try:
                        self._queue_episode(batch["id"], spec, run["episode"] + 1, "逐集续跑")
                        self._event(spec["job"]["name"], "queued", f"Episode {run['episode']} 已完成，安排下一集 {run['episode'] + 1}。")
                        return
                    except (ValueError, LookupError) as exc:
                        self._event(spec["job"]["name"], "failed", f"无法安排下一集：{exc}")
                if run["episode"] == batch["end_episode"]:
                    self.db.execute("UPDATE episode_batches SET status='completed' WHERE id=?", (batch["id"],))
                    text = "已完成全部集数。" if run["episode"] >= spec["episode_total"] else f"本轮完成，下次从 Episode {run['episode'] + 1} 继续。"
                    self._event(spec["job"]["name"], "completed", text)
                    return
            self.db.execute("UPDATE episode_batches SET status='stopped' WHERE id=?", (batch["id"],))
            self._event(spec["job"]["name"], status, "本轮逐集执行已停止；下次触发从尚未完成的集继续。")

    def cancel_queued(self, ident: str):
        with self.lock, self.db:
            row = self.db.execute("SELECT batch FROM runs WHERE id=? AND status='queued'", (ident,)).fetchone()
            if row:
                self.db.execute("UPDATE runs SET status='cancelled',ended=? WHERE id=?", (stamp(), ident))
                if row["batch"]:
                    self.db.execute("UPDATE episode_batches SET status='stopped' WHERE id=? AND last_run=?", (row["batch"], ident))

    def delete_schedule(self, ident: str):
        with self.lock, self.db:
            self.db.execute("UPDATE runs SET status='cancelled',ended=? WHERE status='queued' AND batch IN (SELECT id FROM episode_batches WHERE schedule=?)", (stamp(), ident))
            self.db.execute("UPDATE episode_batches SET status='stopped' WHERE schedule=? AND status='running'", (ident,))
            self.db.execute("DELETE FROM episode_state WHERE schedule=?", (ident,))
            self.db.execute("DELETE FROM schedules WHERE id=?", (ident,))

    def run(self, ident: str) -> dict | None:
        rows = self.rows("SELECT * FROM runs WHERE id=?", (ident,))
        if not rows:
            return None
        return {**rows[0], "job": json.loads(rows[0]["job"])}

    def event(self, source, status, message):
        self.write("INSERT INTO events(time,source,status,message) VALUES(?,?,?,?)", (stamp(), source, status, message))

    def see(self, source, fingerprint) -> bool:
        with self.lock, self.db:
            return bool(self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (source, fingerprint)).rowcount)


def file_snapshot(source: Path, patterns: str) -> dict[str, tuple[int, int]]:
    found = {}
    if not source.is_dir():
        return found
    for pattern in patterns.split(";"):
        if not pattern.strip():
            continue
        for path in source.rglob(pattern.strip()):
            try:
                real = path.resolve()
                if path.is_symlink() or source not in real.parents or not path.is_file():
                    continue
                stat = path.stat()
                found[str(real)] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                continue
    return found


def history_job(entry: dict, settings: dict) -> dict:
    """Adapt the reference GUI's history schema without importing DRMLAB."""
    if isinstance(entry.get("argv"), list) and entry["argv"] and all(isinstance(a, str) for a in entry["argv"]):
        return validate_job({**settings, "name": "历史重试", "argv": entry["argv"],
                             "command": subprocess.list2cmdline(entry["argv"])})
    if isinstance(entry.get("command"), str) and entry["command"].strip():
        return validate_job({**settings, "name": "历史重试", "command": entry["command"]})
    if not entry.get("service") or not entry.get("url"):
        raise ValueError("记录缺少 command/argv 或 service + url，无法重试")
    params = entry.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("历史 params 不是对象")
    valued = {"slang": "-sl", "alang": "-al", "wanted": "-w", "quality": "-q", "vcodec": "-v",
              "acodec": "-a", "proxy": "--proxy", "range": "-r", "vbitrate": "-vb", "abitrate": "-ab",
              "profile": "-p", "delay": "--delay", "channels": "-ch", "cdm": "--cdm"}
    flags = {"atmos": "-aa", "a_only": "-A", "s_only": "-S", "nmux": "-nm", "ad": "-ad",
             "no_subs": "-ns", "no_forced": "-nf", "chapters_only": "-C", "no_audio": "-na",
             "no_video": "-nv", "no_chapters": "-nc", "selected": "--selected", "no_srt": "--no-srt",
             "worst": "--worst", "keys": "--keys"}
    argv = ["poetry", "run", "vt", "dl"]
    supported = set(valued) | set(flags) | {"force_proxy", "no_proxy", "no_cache", "cache", "mux"}
    unknown = [k for k, v in params.items() if k not in supported and v not in (None, "", False, [], {})]
    if unknown:
        raise ValueError("存在无法还原的参数，请手动执行：" + ", ".join(unknown))
    for key, flag in valued.items():
        if params.get(key):
            argv.extend([flag, str(params[key])])
    argv.extend(flag for key, flag in flags.items() if params.get(key))
    for a, b in (("force_proxy", "no_proxy"), ("no_cache", "cache")):
        if params.get(a):
            argv.append("--" + a.replace("_", "-"))
        elif params.get(b):
            argv.append("--" + b.replace("_", "-"))
    if params.get("mux") and not params.get("nmux"):
        argv.append("--mux")
    # No shell expansion: URLs containing &, %, quotes are passed as literal arguments.
    argv.extend([str(entry["service"]), str(entry["url"])])
    return validate_job({**settings, "name": f"历史重试 · {entry['service']}", "argv": argv,
                         "command": subprocess.list2cmdline(argv)})


class WindowsProcessGroup:
    """A Windows job keeps descendants terminable even after the shell exits."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        size = ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working", size), ("max_working", size),
                        ("active_limit", wintypes.DWORD), ("affinity", size),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_count", "write_count", "other_count", "read_bytes", "write_bytes", "other_bytes")]
        class Limits(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", size),
                        ("job_memory", size), ("peak_process", size), ("peak_job", size)]
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.TerminateJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, proc):
        if not self.api.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def terminate(self):
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.events: queue.Queue = queue.Queue(maxsize=6000)
        self.shutdown = threading.Event()
        self.cancel_event = threading.Event()
        self.process: subprocess.Popen | None = None
        self.process_group: WindowsProcessGroup | None = None
        self.active: str | None = None
        self.guard = threading.RLock()
        self.worker: threading.Thread | None = None
        self.thread: threading.Thread | None = None
        self.last_poll = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=1)
        self.last_monitor = 0.0
        self.monitor_error = ""
        self.telegram = TelegramNotifier(self.note)

    def emit(self, kind, **values):
        try:
            self.events.put_nowait({"kind": kind, **values})
        except queue.Full:
            pass  # Full output is always retained in the per-run log.

    def note(self, source, status, message):
        self.store.event(source, status, message)
        self.emit("changed")

    def notify(self, text):
        try:
            self.telegram.submit(self.store.settings(), text)
        except ValueError as exc:
            self.note("Telegram 通知", "failed", str(exc))

    def notify_run(self, ident):
        run = self.store.run(ident)
        self.notify(f"VTGUI · {STATUSES[run['status']]}\n任务：{run['job']['name']}\n"
                    f"来源：{run['origin']}\n尝试：第 {run['attempt'] + 1} 次\n"
                    f"时间：{run['ended']}\n结果：{run['message'] or STATUSES[run['status']]}\n"
                    f"任务 ID：{ident}")

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="vtgui-scheduler", daemon=True)
        self.thread.start()

    def submit(self, job: dict, origin="单次") -> str:
        ident = self.store.enqueue(job, origin)
        self.emit("changed")
        return ident

    def submit_schedule(self, ident: str) -> str | None:
        run = self.store.trigger_schedule(ident)
        self.emit("changed")
        return run

    def tick_schedules(self, now: datetime):
        minute = now.replace(second=0, microsecond=0)
        # During sleep/hibernation do not replay hours of missed schedules.
        cursor = max(self.last_poll, minute - timedelta(minutes=1))
        self.last_poll = minute
        while cursor < minute:
            cursor += timedelta(minutes=1)
            for spec in self.store.schedules():
                if spec["enabled"] and cursor.weekday() in spec["days"] and cursor.strftime("%H:%M") in schedule_times(spec):
                    try:
                        if episodic(spec) and self.store.episode_progress(spec) > int(spec.get("episode_total", 1)):
                            continue
                        self.store.fire(spec, cursor.isoformat(timespec="minutes"))
                        self.emit("changed")
                    except (ValueError, LookupError) as exc:
                        self.note("调度器", "failed", f"{spec['job']['name']}：{exc}")

    def poll_history(self):
        settings = self.store.settings()
        if not settings["monitor_enabled"] or not settings["root"]:
            self.monitor_error = ""
            return
        path = resolved(settings["root"], settings["history_file"])
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("历史文件超过 32MB，请选用独立的小型结果文件")
            entries = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(entries, list):
                raise ValueError("历史文件应为 JSON 数组")
            self.monitor_error = ""
        except (OSError, ValueError) as exc:
            msg = f"{path}：{exc}"
            if msg != self.monitor_error:
                self.monitor_error = msg
                self.note("历史监听", "failed", msg)
            return
        baseline = settings["monitor_baseline"]
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("status") not in ("completed", "failed"):
                continue
            identity = {k: entry.get(k) for k in ("id", "timestamp", "status", "service", "url", "command", "argv")}
            fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if not self.store.see(str(path), fingerprint) or baseline:
                continue
            label = str(entry.get("service") or entry.get("id") or "外部任务")
            status = entry["status"]
            self.note("历史监听", status, f"{label}：捕获{'完成' if status == 'completed' else '失败'}结果")
            self.notify(f"VTGUI · 外部任务{STATUSES[status]}\n任务：{label}\n来源：历史监听\n时间：{stamp()}")
            if status == "failed" and settings["retries"]:
                try:
                    job = history_job(entry, settings)
                    # Budget includes this external failed attempt.
                    self.store.enqueue(job, "历史钩子", 1, delay=settings["retry_delay"])
                    self.note("历史钩子", "queued", f"{label}：已安排重试，{settings['retry_delay']} 秒后执行")
                except (ValueError, LookupError) as exc:
                    self.note("历史钩子", "failed", f"{label}：{exc}")
            elif status == "completed":
                self.note("历史钩子", "completed", f"{label}：仅记录完成结果；自动移动仅由定时任务配置，不执行外部历史移动")
        if baseline:
            self.store.save_settings({"monitor_baseline": False})
            self.note("历史监听", "completed", "已建立基线；仅处理此后新增的完成/失败记录。")

    def _loop(self):
        while not self.shutdown.is_set():
            try:
                self.tick_schedules(datetime.now())
                if time.monotonic() - self.last_monitor >= 2:
                    self.last_monitor = time.monotonic()
                    self.poll_history()
                with self.guard:
                    if not self.active and not self.shutdown.is_set():
                        rows = self.store.rows("SELECT id FROM runs WHERE status='queued' AND available<=? ORDER BY available,created LIMIT 1", (time.time(),))
                        if rows:
                            self.active = rows[0]["id"]
                            self.cancel_event.clear()
                            self.worker = threading.Thread(target=self._run, args=(self.active,), daemon=True)
                            self.worker.start()
            except Exception as exc:
                self.note("引擎", "failed", str(exc))
            self.shutdown.wait(0.5)

    def _kill(self, expected=None):
        with self.guard:
            if expected is not None and self.active != expected:
                return
            if self.process_group:
                self.process_group.terminate()
                return
            proc = self.process
            if not proc or proc.poll() is not None:
                return
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            else:
                os.killpg(proc.pid, signal.SIGKILL)

    def cancel(self, ident: str):
        with self.guard:
            if ident == self.active:
                self.cancel_event.set()
                threading.Thread(target=self._kill_safely, args=(ident,), daemon=True).start()
            else:
                queued = self.store.run(ident)
                self.store.cancel_queued(ident)
                if queued and queued["status"] == "queued":
                    self.notify_run(ident)
        self.emit("changed")

    def _kill_safely(self, expected=None):
        try:
            self._kill(expected)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.note("进程终止", "failed", str(exc))

    def send_input(self, text: str):
        with self.guard:
            if not self.process or self.process.poll() is not None or not self.process.stdin:
                raise ValueError("当前没有可接收输入的进程。")
            run = self.store.run(self.active)
            self.process.stdin.write((text + "\n").encode(run["job"]["encoding"], errors="replace"))
            self.process.stdin.flush()

    def _spawn(self, job: dict, ident: str):
        env = os.environ.copy()
        if getattr(sys, "frozen", False):
            bundle = str(getattr(sys, "_MEIPASS", ""))
            for key in ("TCL_LIBRARY", "TK_LIBRARY"):
                if bundle and env.get(key, "").startswith(bundle):
                    env.pop(key, None)
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING=job["encoding"], VTGUI_RUN_ID=ident)
        local_scripts = Path(job["root"]) / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        env["PATH"] = str(local_scripts) + os.pathsep + str(Path(job["root"]) / "binaries") + os.pathsep + env.get("PATH", "")
        if job.get("argv"):
            args = list(job["argv"])
            found = shutil.which(args[0], path=env["PATH"])
            if found:
                args[0] = found
        elif os.name == "nt" and job["shell"] == "powershell":
            import base64
            script = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); $OutputEncoding = [Console]::OutputEncoding;\n" + job["command"] + "\n$vtguiSuccess = $?; $vtguiCode = $LASTEXITCODE; if ($null -ne $vtguiCode -and $vtguiCode -ne 0) { exit $vtguiCode }; if (-not $vtguiSuccess) { exit 1 }; exit 0"
            args = ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand",
                    base64.b64encode(script.encode("utf-16-le")).decode()]
        elif os.name == "nt":
            script = self.store.logs / f"{ident}.cmd"
            script.write_bytes(("@echo off\r\nchcp 65001 >nul\r\n" + job["command"].replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n").encode("utf-8"))
            comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
            args = f'"{comspec}" /d /s /c ""{script}""'
        else:
            args = ["/bin/sh", "-c", job["command"]]
        group = WindowsProcessGroup() if os.name == "nt" else None
        proc = None
        try:
            # A packaged GUI must not impose its bundled DLL search path on DRMLAB.
            restore_dlls = None
            if os.name == "nt" and getattr(sys, "frozen", False):
                import ctypes
                restore_dlls = ctypes.WinDLL("kernel32", use_last_error=True).SetDllDirectoryW
                restore_dlls.argtypes = [ctypes.c_wchar_p]
                restore_dlls.restype = ctypes.c_int
                restore_dlls(None)
            try:
                proc = subprocess.Popen(args, cwd=job["root"], env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                        start_new_session=os.name != "nt")
            finally:
                if restore_dlls:
                    restore_dlls(str(sys._MEIPASS))
            if group:
                group.assign(proc)
            self.process_group = group
            return proc
        except Exception:
            if proc:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                for pipe in (proc.stdin, proc.stdout):
                    if pipe:
                        pipe.close()
            if group:
                group.close()
            raise

    def _archive(self, job: dict, files: list[str], log, ident=None):
        source = resolved(job["root"], job["source"])
        destination = resolved(job["root"], job["destination"])
        renaming = rename_enabled(job)
        if job["move_mode"] == "files" and not renaming:
            return move_files(source, destination, files, log, self.cancel_event.is_set), len(files)
        folders, loose = archive_plan(source, files)
        snapshots = {}
        for folder in folders:
            try:
                snapshot = folder_snapshot(folder, job["patterns"])
                if "move_trees" in job and snapshot != job["move_trees"].get(str(folder)):
                    raise OSError("等待归档期间目录内容发生变化")
                snapshots[str(folder)] = snapshot
            except OSError as exc:
                if renaming:
                    raise RenameError(f"重命名预检失败：{folder}：{exc}；未执行移动") from exc
                log(f"跳过剧集文件夹：{folder}：{exc}\n")
        if snapshots and self.cancel_event.wait(1):
            return 0, len(folders) + len(loose)
        if renaming:
            preserved_names = set()
            for path in self.store.logs.glob("*.rename.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RenameError(f"无法读取重命名记录：{path}；请检查后再执行") from exc
                if not isinstance(record, dict):
                    raise RenameError(f"重命名记录格式错误：{path}")
                if record.get("source") == str(source) and record.get("status") in ("applying", "rolling_back", "rollback_failed"):
                    raise RenameError(f"存在未完成或回滚失败的重命名记录：{path}；请核实名称后再执行")
                if record.get("source") == str(source) and record.get("status") == "completed" and record.get("rules") == validate_rename(job):
                    for name, identity in record.get("identities", {}).items():
                        current = Path(name)
                        try:
                            if source in current.parents and file_identity(current) == identity:
                                preserved_names.add(str(current.absolute()).casefold())
                        except OSError:
                            pass
            merged_directories = set()
            mapping = rename_items(source, snapshots, loose, job["patterns"], job, log, self.cancel_event.is_set,
                                   journal=self.store.logs / f"{ident or uuid.uuid4().hex}.rename.json",
                                   destination=destination if job["move_enabled"] else None,
                                   folder_move=job["move_mode"] == "folders", selected_files=files,
                                   preserved_names=preserved_names, merged_directories=merged_directories)
            files = [mapping[p] for p in files]
            loose = [mapping[p] for p in loose]
            snapshots = {mapping[name]: folder_snapshot(Path(mapping[name]), job["patterns"], require_media=False) for name in snapshots}
            if not job["move_enabled"]:
                log("重命名处理完成；未启用自动移动。\n")
                return len(folders) + len(loose), len(folders) + len(loose)
            if job["move_mode"] == "files":
                count = move_files(source, destination, files, log, self.cancel_event.is_set)
                restore_unmoved(source, mapping, job["patterns"], log, self.store.logs / f"{ident or uuid.uuid4().hex}.unmoved.rename.json",
                            merged_directories=merged_directories)
                return count, len(files)
        count = move_folders(source, destination, snapshots, job["patterns"], log, self.cancel_event.is_set, require_media=not renaming)
        count += move_files(source, destination, loose, log, self.cancel_event.is_set)
        if renaming:
            restore_unmoved(source, mapping, job["patterns"], log, self.store.logs / f"{ident or uuid.uuid4().hex}.unmoved.rename.json",
                            merged_directories=merged_directories)
        return count, len(folders) + len(loose)

    def _run(self, ident: str):
        run = self.store.run(ident)
        job = run["job"]
        status, code, message = "failed", None, ""
        postprocess_failed = False
        postprocess_started = False
        log_path = self.store.logs / f"{ident}.log"
        self.store.write("UPDATE runs SET status='running',started=? WHERE id=?", (stamp(), ident))
        self.emit("changed")
        timer_stop = threading.Event()
        timed_out = threading.Event()
        watchdog = None
        try:
            with log_path.open("a", encoding="utf-8", buffering=1, newline="") as handle:
                def log(text):
                    text = ANSI.sub("", text).replace("\x00", "")
                    handle.write(text)
                    handle.flush()
                    self.emit("output", id=ident, text=text, end=handle.tell())

                log(f"[{stamp()}] {job['name']}\n目录：{job['root']}\n$ {job['command']}\n\n")
                job = validate_job(job)
                source = resolved(job["root"], job["source"])
                before = file_snapshot(source, job["patterns"]) if job["move_enabled"] or rename_enabled(job) else {}
                if job.get("move_only"):
                    if not job["move_enabled"]:
                        raise ValueError("归档任务未启用移动")
                    files = [p for p in job["move_only"] if p in before and list(before[p]) == job["move_snapshot"].get(p)]
                    # Check stability before touching explicit history-owned files.
                    if self.cancel_event.wait(1):
                        raise InterruptedError("已取消")
                    stable = file_snapshot(source, job["patterns"])
                    files = [p for p in files if stable.get(p) == before[p]]
                    postprocess_started = True
                    count, total = self._archive(job, files, log, ident)
                    status = "cancelled" if self.cancel_event.is_set() else "completed"
                    message = f"已移动 {count}/{total} 个归档项目（文件或剧集文件夹），详见日志"
                else:
                    with self.guard:
                        if self.cancel_event.is_set():
                            raise InterruptedError("启动前已取消")
                        self.process = self._spawn(job, ident)
                    def watch():
                        if not timer_stop.wait(job["timeout"]):
                            timed_out.set()
                            self._kill_safely()
                    if job["timeout"]:
                        watchdog = threading.Thread(target=watch, daemon=True)
                        watchdog.start()
                    decoder = codecs.getincrementaldecoder(job["encoding"])(errors="replace")
                    outputs = FinalOutputs(job["root"])
                    while chunk := self.process.stdout.read(4096):
                        text = decoder.decode(chunk)
                        outputs.feed(text)
                        log(text)
                    text = decoder.decode(b"", final=True)
                    outputs.feed(text, final=True)
                    log(text)
                    code = self.process.wait()
                    timer_stop.set()
                    if self.cancel_event.is_set():
                        status, message = "cancelled", "用户取消；不执行重试或自动移动"
                    elif timed_out.is_set():
                        status, message = "failed", f"超过 {job['timeout']} 秒，已终止进程树"
                    else:
                        status = "completed" if code == 0 else "failed"
                        message = f"退出码 {code}"
                    if status == "completed" and (job["move_enabled"] or rename_enabled(job)):
                        after = file_snapshot(source, job["patterns"])
                        files = sorted(p for p in after if p not in before or
                                       ((job["move_mode"] == "folders" or rename_enabled(job)) and after[p] != before[p]))
                        if outputs.seen:
                            matched = outputs.select(files)
                            log(f"完成路径标记：识别 {len(outputs.directories)} 个目录、{len(outputs.files)} 个文件；"
                                f"成品目录内匹配本次新增/更新成品 {len(matched)}/{len(files)} 个。\n")
                            files = matched
                        if files and not self.cancel_event.wait(1):
                            stable = file_snapshot(source, job["patterns"])
                            files = [p for p in files if stable.get(p) == after[p]]
                            postprocess_started = True
                            count, total = self._archive(job, files, log, ident)
                            action = "先重命名后移动" if rename_enabled(job) and job["move_enabled"] else "重命名" if rename_enabled(job) else "移动"
                            message += f"；{action} {count}/{total} 个项目，详见日志"
                        else:
                            message += "；没有可确认的新成品"
                        if self.cancel_event.is_set():
                            status, message = "cancelled", message + "；归档阶段取消"
                log(f"\n[{stamp()}] {STATUSES[status]} · {message}\n")
        except Exception as exc:
            postprocess_failed = postprocess_started or isinstance(exc, RenameError)
            status = "cancelled" if self.cancel_event.is_set() else "failed"
            message = str(exc)
            with log_path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(f"\n执行异常：{message}\n")
                handle.flush()
                end = handle.tell()
            self.emit("output", id=ident, text=f"\n执行异常：{message}\n", end=end)
        finally:
            timer_stop.set()
            if watchdog:
                watchdog.join(timeout=16)
            # Ensure no command is orphaned after an I/O or log error.
            self._kill_safely()
            with self.guard:
                if self.process:
                    self.process.wait(timeout=10)
                    for pipe in (self.process.stdin, self.process.stdout):
                        if pipe:
                            pipe.close()
                self.process = None
                if self.process_group:
                    self.process_group.close()
                    self.process_group = None
            with self.guard:
                if self.cancel_event.is_set():
                    status = "cancelled"
                    message += "；已取消，不执行重试或后续集数"
                self.store.finish_run(ident, status, code, message, allow_followups=not self.shutdown.is_set() and not postprocess_failed)
                self.active = None
            self.notify_run(ident)
            self.emit("changed")

    def close(self):
        self.shutdown.set()
        with self.guard:
            if self.active:
                self.cancel_event.set()
        self._kill_safely()
        if self.thread:
            self.thread.join(timeout=5)
        if self.worker:
            self.worker.join(timeout=20)
        self.telegram.close()


class InstanceLock:
    """OS-held lock: a second GUI must not start another scheduler on the same DB."""
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.handle = (folder / "instance.lock").open("a+b")
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            self.handle.write(b"0")
            self.handle.flush()
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.handle.close()
                raise RuntimeError("VTGUI 已在运行，请检查已打开或最小化的窗口。") from None
        else:
            import fcntl
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.handle.close()
                raise RuntimeError("VTGUI 已在运行。") from None

    def close(self):
        self.handle.close()
