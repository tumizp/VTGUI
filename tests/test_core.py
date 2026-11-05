import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

from vtgui.core import (DEFAULTS, Engine, InstanceLock, Store, file_snapshot,
                        history_job, move_files, next_occurrence,
                        validate_job, validate_schedule)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI 测试 空格 ")
        self.base = Path(self.temp.name)
        self.root = self.base / "DRMLAB 独立目录"
        self.root.mkdir()
        self.store = Store(self.base / "data 空格")
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def job(self, script="print('你好 VTGUI')", **values):
        argv = [sys.executable, "-u", "-c", script]
        return {**DEFAULTS, "root": str(self.root), "name": "测试任务", "argv": argv,
                "command": subprocess.list2cmdline(argv), **values}

    def wait(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if value := predicate():
                return value
            time.sleep(0.04)
        self.fail("任务在限定时间内未达到预期状态")

    def finished(self, ident):
        return self.wait(lambda: (r if (r := self.store.run(ident))["status"] not in ("queued", "running") else None))

    def test_validation(self):
        with self.assertRaises(ValueError):
            validate_job(self.job(root=""))
        with self.assertRaises(ValueError):
            validate_job(self.job(retries=21))
        with self.assertRaises(ValueError):
            validate_job(self.job(move_enabled=True, source="downloads", destination="downloads/library"))
        with self.assertRaises(ValueError):
            validate_schedule({"job": self.job(), "time": "25:00", "days": [0]})
        with self.assertRaises(ValueError):
            validate_schedule({"job": self.job(), "time": "09:00", "days": []})

    def test_schedule_next(self):
        monday = datetime(2026, 10, 19, 9, 0)
        self.assertEqual(next_occurrence({"days": [0], "time": "09:00"}, monday), monday + timedelta(days=7))
        self.assertEqual(next_occurrence({"days": list(range(7)), "time": "09:00"}, monday), monday + timedelta(days=1))

    def test_schedule_dedup_and_no_offline_catchup(self):
        now = datetime(2026, 10, 19, 9, 0, 15)
        spec = {"job": self.job(), "time": "09:00", "days": [0], "enabled": True}
        self.store.save_schedule(spec)
        self.engine.last_poll = now.replace(hour=8, minute=0, second=0)
        self.engine.tick_schedules(now)
        self.engine.last_poll = now.replace(hour=8, minute=59, second=0)
        self.engine.tick_schedules(now)
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 1)
        self.engine.last_poll = now - timedelta(days=1)
        self.engine.tick_schedules(now + timedelta(hours=1))
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 1)

    def test_schedule_invalid_root_does_not_claim(self):
        ident = self.store.save_schedule({"job": self.job(), "time": "09:00", "days": [0]})
        self.root.rmdir()
        now = datetime(2026, 10, 19, 9, 0)
        self.engine.last_poll = now - timedelta(minutes=1)
        self.engine.tick_schedules(now)
        self.assertEqual(self.store.rows("SELECT * FROM fires"), [])

    def test_real_output_cwd_and_unicode(self):
        ident = self.engine.submit(self.job("import os; print('你好'); print(os.getcwd())"))
        self.engine.start()
        run = self.finished(ident)
        self.assertEqual(run["status"], "completed")
        log = (self.store.logs / f"{ident}.log").read_text(encoding="utf-8")
        self.assertIn("你好", log)
        self.assertIn(str(self.root), log)

    @unittest.skipUnless(sys.platform == "win32", "Windows shell test")
    def test_cmd_script_with_spaces_and_chinese(self):
        ident = self.engine.submit({**DEFAULTS, "root": str(self.root), "command": "echo 中文命令\necho 第二行\nexit /b 7"})
        self.engine.start()
        run = self.finished(ident)
        self.assertEqual(run["code"], 7)
        log = (self.store.logs / f"{ident}.log").read_text(encoding="utf-8")
        self.assertIn("中文命令\n第二行", log)

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell test")
    def test_powershell_failure_status(self):
        ident = self.engine.submit({**DEFAULTS, "root": str(self.root), "shell": "powershell", "command": "Write-Output '中文'; exit 3"})
        self.engine.start()
        self.assertEqual(self.finished(ident)["code"], 3)

    def test_stream_output_without_newline_and_stdin(self):
        ident = self.engine.submit(self.job("import sys; print('READY', end='', flush=True); print('GOT:' + input())"))
        self.engine.start()
        def ready():
            while True:
                try:
                    event = self.engine.events.get_nowait()
                except queue.Empty:
                    return False
                if event.get("kind") == "output" and "READY" in event.get("text", ""):
                    return True
        self.wait(ready)
        self.assertEqual(self.store.run(ident)["status"], "running")
        self.engine.send_input("输入中文")
        self.assertEqual(self.finished(ident)["status"], "completed")
        self.assertIn("GOT:输入中文", (self.store.logs / f"{ident}.log").read_text(encoding="utf-8"))

    def test_retry_budget(self):
        ident = self.engine.submit(self.job("raise SystemExit(4)", retries=2, retry_delay=1))
        self.engine.start()
        self.wait(lambda: len(self.store.rows("SELECT * FROM runs WHERE status='failed'")) == 3)
        time.sleep(0.6)
        runs = self.store.rows("SELECT * FROM runs ORDER BY attempt")
        self.assertEqual([r["attempt"] for r in runs], [0, 1, 2])
        self.assertEqual(runs[1]["parent"], ident)

    def test_timeout_retries_as_failure(self):
        ident = self.engine.submit(self.job("import time; time.sleep(30)", timeout=1))
        self.engine.start()
        run = self.finished(ident)
        self.assertEqual(run["status"], "failed")
        self.assertIn("超过", run["message"])

    def test_cancel_running_no_retry(self):
        ident = self.engine.submit(self.job("import time; print('RUNNING'); time.sleep(30)", retries=2))
        self.engine.start()
        self.wait(lambda: self.engine.process is not None)
        self.engine.cancel(ident)
        self.assertEqual(self.finished(ident)["status"], "cancelled")
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 1)

    def test_cancel_queued(self):
        ident = self.engine.submit(self.job())
        self.engine.cancel(ident)
        self.engine.start()
        self.assertEqual(self.finished(ident)["status"], "cancelled")

    @unittest.skipUnless(sys.platform == "win32", "Windows process-group test")
    def test_cancel_descendant_after_parent_exits(self):
        child = "from pathlib import Path; import time; Path('child-ready').touch(); time.sleep(2); Path('orphan-wrote').touch()"
        parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-u','-c',{child!r}], stdout=sys.stdout, stderr=sys.stderr, creationflags=subprocess.CREATE_NO_WINDOW)"
        ident = self.engine.submit(self.job(parent))
        self.engine.start()
        self.wait(lambda: (self.root / "child-ready").exists() and self.engine.process and self.engine.process.poll() is not None)
        self.engine.cancel(ident)
        self.assertEqual(self.finished(ident)["status"], "cancelled")
        time.sleep(2.1)
        self.assertFalse((self.root / "orphan-wrote").exists())

    def test_serial_queue(self):
        script = "from pathlib import Path; import time; p=Path('busy'); assert not p.exists(); p.touch(); time.sleep(.2); p.unlink()"
        ids = [self.engine.submit(self.job(script)) for _ in range(3)]
        self.engine.start()
        self.assertEqual([self.finished(i)["status"] for i in ids], ["completed"] * 3)

    def test_move_only_new_successful_files(self):
        source = self.root / "downloads"
        source.mkdir()
        (source / "old.mkv").write_bytes(b"old")
        dest = self.base / "library"
        script = "from pathlib import Path; p=Path('downloads/剧集'); p.mkdir(); (p/'本集.mkv').write_bytes(b'finished'); Path('downloads/incomplete.part').write_bytes(b'partial')"
        ident = self.engine.submit(self.job(script, move_enabled=True, move_mode="files", destination=str(dest)))
        self.engine.start()
        self.assertEqual(self.finished(ident)["status"], "completed")
        self.assertTrue((source / "old.mkv").exists())
        self.assertFalse((source / "剧集/本集.mkv").exists())
        self.assertEqual((dest / "本集.mkv").read_bytes(), b"finished")
        self.assertFalse((dest / "剧集").exists())
        self.assertTrue((source / "incomplete.part").exists())
        self.assertTrue((source / "剧集").is_dir())

    def test_failed_task_never_moves(self):
        source = self.root / "downloads"
        source.mkdir()
        ident = self.engine.submit(self.job("from pathlib import Path; Path('downloads/failed.mkv').write_bytes(b'failed'); raise SystemExit(1)", move_enabled=True, destination=str(self.base / "library")))
        self.engine.start()
        self.assertEqual(self.finished(ident)["status"], "failed")
        self.assertTrue((source / "failed.mkv").exists())
        self.assertFalse((self.base / "library").exists())

    def test_no_new_output_is_failure(self):
        source = self.root / "downloads"
        source.mkdir()
        old = source / "previous.mkv"
        old.write_bytes(b"previous")
        for options in ({"move_enabled": True, "move_mode": "files"},
                        {"move_enabled": True, "move_mode": "folders"},
                        {"rename_season": "+1"}):
            with self.subTest(options=options):
                ident = self.engine.submit(self.job("print('Processed all titles!')",
                                                    destination=str(self.base / "library"), **options))
                self.engine._run(ident)
                run = self.store.run(ident)
                self.assertEqual(run["status"], "failed")
                self.assertEqual(run["code"], 0)
                self.assertEqual(run["message"], "退出码 0；当前集未更新")
                log = (self.store.logs / f"{ident}.log").read_text(encoding="utf-8")
                self.assertIn("失败 · 退出码 0；当前集未更新", log)
                self.assertEqual(old.read_bytes(), b"previous")
                self.assertFalse((self.base / "library").exists())

    def test_move_collision_and_path_escape(self):
        source = self.root / "downloads"
        source.mkdir()
        (source / "same.mkv").write_bytes(b"new")
        dest = self.base / "library"
        dest.mkdir()
        (dest / "same.mkv").write_bytes(b"existing")
        outside = self.root / "outside.mkv"
        outside.write_bytes(b"outside")
        logs = []
        self.assertEqual(move_files(source, dest, [str(source / "same.mkv"), str(outside)], logs.append), 0)
        self.assertEqual((dest / "same.mkv").read_bytes(), b"existing")
        self.assertTrue((source / "same.mkv").exists())
        self.assertTrue(outside.exists())

    def test_restart_recovery_and_settings(self):
        ident = self.engine.submit(self.job())
        self.store.write("UPDATE runs SET status='running' WHERE id=?", (ident,))
        self.store.save_settings({"root": str(self.root), "retries": 2})
        other = Store(self.store.folder)
        try:
            self.assertEqual(other.run(ident)["status"], "interrupted")
            self.assertEqual(other.settings()["retries"], 2)
        finally:
            other.db.close()

    def test_history_baseline_dedup_and_partial_json(self):
        path = self.root / "history.json"
        existing = {"id": 1, "timestamp": "2026-01-01", "status": "failed", "service": "Example", "url": "url"}
        path.write_text(json.dumps([existing]), encoding="utf-8")
        self.store.save_settings({"root": str(self.root), "monitor_enabled": True, "history_file": str(path), "retries": 1})
        self.engine.poll_history()
        self.assertEqual(self.store.rows("SELECT * FROM runs"), [])
        new = {**existing, "id": 2, "timestamp": "2026-01-02", "command": "echo retry"}
        path.write_text(json.dumps([existing, new]), encoding="utf-8")
        self.engine.poll_history()
        self.engine.poll_history()
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 1)
        path.write_text("[", encoding="utf-8")
        self.engine.poll_history()
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 1)
        path.write_text(json.dumps([existing, new, {**new, "id": 3}]), encoding="utf-8")
        self.engine.poll_history()
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 2)

    def test_history_missing_files_does_not_move(self):
        path = self.root / "history.json"
        path.write_text("[]", encoding="utf-8")
        self.store.save_settings({"root": str(self.root), "monitor_enabled": True, "history_file": str(path), "move_enabled": True, "destination": str(self.base / "library")})
        self.engine.poll_history()
        path.write_text(json.dumps([{"id": 1, "status": "completed"}]), encoding="utf-8")
        self.engine.poll_history()
        self.assertEqual(self.store.rows("SELECT * FROM runs"), [])
        self.assertIn("不执行外部历史移动", self.store.rows("SELECT * FROM events ORDER BY id DESC")[0]["message"])

    def test_history_explicit_files_do_not_move(self):
        path = self.root / "history.json"
        path.write_text("[]", encoding="utf-8")
        source = self.root / "downloads"
        source.mkdir()
        (source / "external.mkv").write_bytes(b"external")
        self.store.save_settings({"root": str(self.root), "monitor_enabled": True, "history_file": str(path), "move_enabled": True, "destination": str(self.base / "library")})
        self.engine.poll_history()
        path.write_text(json.dumps([{"id": 1, "status": "completed", "files": ["downloads/external.mkv"]}]), encoding="utf-8")
        self.engine.poll_history()
        self.assertFalse(self.store.rows("SELECT * FROM runs"))
        self.assertEqual((source / "external.mkv").read_bytes(), b"external")
        self.assertFalse((self.base / "library").exists())

    def test_history_literal_arguments_and_unknown_params(self):
        settings = {**DEFAULTS, "root": str(self.root)}
        job = history_job({"service": "Example", "url": 'https://example.test/?a=1&b="%x%"', "params": {"quality": "1080", "no_cache": True}}, settings)
        self.assertEqual(job["argv"][-1], 'https://example.test/?a=1&b="%x%"')
        self.assertIn("--no-cache", job["argv"])
        with self.assertRaises(ValueError):
            history_job({"service": "Example", "url": "url", "params": {"unknown_flag": "x"}}, settings)

    def test_instance_lock(self):
        lock = InstanceLock(self.base / "locked")
        try:
            with self.assertRaises(RuntimeError):
                InstanceLock(self.base / "locked")
        finally:
            lock.close()
        again = InstanceLock(self.base / "locked")
        again.close()


if __name__ == "__main__":
    unittest.main()
