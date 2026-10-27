import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from vtgui.core import DEFAULTS, Engine, Store, validate_schedule


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI Episodes 中文 ")
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def spec(self, **values):
        return {"job": {**DEFAULTS, "root": str(self.root), "name": "逐集计划", "command": "echo S01E{episode}"},
                "days": list(range(7)), "time": "09:00", "enabled": True,
                "episode_start": "01", "episode_total": 5, "episodes_per_trigger": 2, **values}

    def saved(self, **values):
        ident = self.store.save_schedule(self.spec(**values))
        return next(s for s in self.store.schedules() if s["id"] == ident)

    def finish(self, ident, status="completed", follow=True):
        self.store.finish_run(ident, status, 0 if status == "completed" else 1, "test result", follow)

    def queued(self):
        return self.store.rows("SELECT * FROM runs WHERE status='queued' ORDER BY rowid")

    def wait(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if value := predicate():
                return value
            time.sleep(0.04)
        self.fail("Episode task did not reach expected state")

    def real_spec(self, script, **values):
        spec = self.spec(**values)
        argv = [sys.executable, "-u", "-c", script]
        spec["job"].update(argv=argv, command=subprocess.list2cmdline(argv))
        ident = self.store.save_schedule(spec)
        return next(s for s in self.store.schedules() if s["id"] == ident)

    def test_validation(self):
        for values in ({"episode_start": "-1"}, {"episode_start": "1.5"}, {"episode_total": 0},
                       {"episode_start": "06"}, {"episodes_per_trigger": "1.5"}, {"episodes_per_trigger": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_schedule(self.spec(**values))

    def test_only_one_episode_is_queued_and_other_braces_are_literal(self):
        spec = self.spec()
        spec["job"]["command"] = "Write-Output @{episode='{episode}'; other={1+2}}"
        ident = self.store.save_schedule(spec)
        run = self.store.run(self.store.trigger_schedule(ident))
        self.assertEqual(run["job"]["command"], "Write-Output @{episode='01'; other={1+2}}")
        self.assertEqual(run["episode"], 1)
        self.assertEqual(len(self.queued()), 1)

    def test_batches_advance_until_total_and_clamp_final_batch(self):
        spec = self.saved(episode_start="03", episode_total=5)
        first = self.store.trigger_schedule(spec["id"])
        self.finish(first)
        self.assertEqual(self.store.episode_progress(spec), 4)
        next_run = self.queued()[0]
        self.assertEqual(next_run["episode"], 4)
        self.finish(next_run["id"])
        self.assertFalse(self.queued())
        self.assertEqual(self.store.episode_progress(spec), 5)
        final = self.store.trigger_schedule(spec["id"])
        self.assertEqual(self.store.run(final)["episode"], 5)
        self.finish(final)
        self.assertFalse(self.queued())
        self.assertEqual(self.store.episode_progress(spec), 6)
        self.assertIsNone(self.store.trigger_schedule(spec["id"]))

    def test_failure_stops_batch_without_advancing_and_next_trigger_resumes(self):
        spec = self.saved()
        first = self.store.trigger_schedule(spec["id"])
        self.finish(first, "failed")
        self.assertEqual(self.store.episode_progress(spec), 1)
        self.assertFalse(self.queued())
        resumed = self.store.trigger_schedule(spec["id"])
        self.assertEqual(self.store.run(resumed)["episode"], 1)
        self.finish(resumed)
        second = self.queued()[0]
        self.finish(second["id"], "cancelled")
        self.assertEqual(self.store.episode_progress(spec), 2)
        self.assertFalse(self.queued())

    def test_retry_same_episode_before_next_episode(self):
        spec = self.spec()
        spec["job"].update(retries=1, retry_delay=1)
        ident = self.store.save_schedule(spec)
        first = self.store.trigger_schedule(ident)
        self.finish(first, "failed")
        retry = self.queued()[0]
        self.assertEqual((retry["episode"], retry["attempt"]), (1, 1))
        self.assertEqual(retry["parent"], first)
        self.finish(retry["id"])
        next_run = self.queued()[0]
        self.assertEqual((next_run["episode"], next_run["attempt"]), (2, 0))

    def test_retry_exhaustion_blocks_later_episodes(self):
        spec = self.spec()
        spec["job"].update(retries=1, retry_delay=1)
        ident = self.store.save_schedule(spec)
        self.finish(self.store.trigger_schedule(ident), "failed")
        self.finish(self.queued()[0]["id"], "failed")
        self.assertFalse(self.queued())
        self.assertEqual(self.store.episode_progress(self.store.schedules()[0]), 1)

    def test_duplicate_trigger_and_duplicate_completion_do_not_duplicate_runs(self):
        spec = self.saved()
        first = self.store.trigger_schedule(spec["id"])
        self.assertIsNone(self.store.trigger_schedule(spec["id"]))
        self.finish(first)
        self.finish(first)
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 2)

    def test_cancel_queued_stops_batch(self):
        spec = self.saved()
        first = self.store.trigger_schedule(spec["id"])
        self.engine.cancel(first)
        self.assertEqual(self.store.run(first)["status"], "cancelled")
        self.assertEqual(self.store.episode_progress(spec), 1)
        self.assertIsNotNone(self.store.trigger_schedule(spec["id"]))

    def test_progress_survives_restart_and_queued_episode_is_preserved(self):
        spec = self.saved()
        self.finish(self.store.trigger_schedule(spec["id"]))
        next_run = self.queued()[0]
        self.store.db.close()
        self.store = Store(self.root / "data")
        self.engine = Engine(self.store)
        self.assertEqual(self.store.episode_progress(spec), 2)
        self.assertEqual(self.store.run(next_run["id"])["status"], "queued")
        self.assertIsNone(self.store.trigger_schedule(spec["id"]))
        self.finish(next_run["id"])
        self.assertEqual(self.store.episode_progress(spec), 3)

    def test_interrupted_episode_is_not_skipped_on_restart(self):
        spec = self.saved()
        first = self.store.trigger_schedule(spec["id"])
        self.store.write("UPDATE runs SET status='running' WHERE id=?", (first,))
        self.store.db.close()
        self.store = Store(self.root / "data")
        self.engine = Engine(self.store)
        self.assertEqual(self.store.run(first)["status"], "interrupted")
        resumed = self.store.trigger_schedule(spec["id"])
        self.assertEqual(self.store.run(resumed)["episode"], 1)

    def test_schedule_edits_preserve_progress_and_start_change_resets(self):
        spec = self.saved(episodes_per_trigger=1)
        self.finish(self.store.trigger_schedule(spec["id"]))
        spec["episode_total"] = 7
        self.store.save_schedule(spec)
        self.assertEqual(self.store.episode_progress(spec), 2)
        spec["episode_start"] = "04"
        self.store.save_schedule(spec)
        self.assertEqual(self.store.episode_progress(spec), 4)
        run = self.store.run(self.store.trigger_schedule(spec["id"]))
        self.assertIn("S01E04", run["job"]["command"])
        spec["episode_total"] = 8
        with self.assertRaises(ValueError):
            self.store.save_schedule(spec)

    def test_deleting_schedule_cancels_future_episodes(self):
        spec = self.saved()
        first = self.store.trigger_schedule(spec["id"])
        self.store.write("UPDATE runs SET status='running' WHERE id=?", (first,))
        self.store.delete_schedule(spec["id"])
        self.finish(first)
        self.assertFalse(self.queued())
        self.assertFalse(self.store.schedules())

    def test_scheduler_uses_episode_sequence(self):
        spec = self.saved()
        now = datetime(2026, 10, 19, 9, 0)
        self.engine.last_poll = now - timedelta(minutes=1)
        self.engine.tick_schedules(now)
        self.assertEqual(self.queued()[0]["episode"], 1)
        self.assertIn("S01E01", json.loads(self.queued()[0]["job"])["command"])

    def test_failed_transaction_does_not_claim_slot_or_advance_progress(self):
        spec = self.saved()
        with patch.object(self.store, "_queue_episode", side_effect=ValueError("test enqueue failure")):
            with self.assertRaises(ValueError):
                self.store.fire(spec, "test-slot")
        self.assertFalse(self.store.rows("SELECT * FROM fires"))
        self.assertFalse(self.store.rows("SELECT * FROM episode_batches"))
        self.assertEqual(self.store.episode_progress(spec), 1)

    def test_real_commands_execute_in_order_and_only_after_completion(self):
        script = "from pathlib import Path; import time; p=Path('order.txt'); old=p.read_text() if p.exists() else ''; assert old == ''.join(str(i)+'\\n' for i in range(1, int('{episode}'))); time.sleep(.15); p.write_text(old+str(int('{episode}'))+'\\n')"
        spec = self.real_spec(script, episode_total=3, episodes_per_trigger=3)
        self.engine.submit_schedule(spec["id"])
        self.engine.start()
        self.wait(lambda: self.store.episode_progress(spec) == 4)
        self.assertEqual((self.root / "order.txt").read_text(), "1\n2\n3\n")
        runs = self.store.rows("SELECT * FROM runs ORDER BY rowid")
        self.assertEqual([r["status"] for r in runs], ["completed"] * 3)
        self.assertEqual([r["episode"] for r in runs], [1, 2, 3])
        self.assertTrue(all(runs[i]["ended"] <= runs[i + 1]["started"] for i in range(2)))

    def test_real_failed_episode_prevents_next_episode(self):
        spec = self.real_spec("from pathlib import Path; Path('attempt-{episode}').touch(); raise SystemExit(2)", episodes_per_trigger=3)
        first = self.engine.submit_schedule(spec["id"])
        self.engine.start()
        self.wait(lambda: self.store.run(first)["status"] == "failed")
        time.sleep(.6)
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 1)
        self.assertFalse((self.root / "attempt-02").exists())
        self.assertEqual(self.store.episode_progress(spec), 1)

    def test_cancel_running_episode_stops_sequence(self):
        spec = self.real_spec("import time; print('{episode}', flush=True); time.sleep(30)", episodes_per_trigger=3)
        first = self.engine.submit_schedule(spec["id"])
        self.engine.start()
        self.wait(lambda: self.engine.process is not None)
        self.engine.cancel(first)
        self.wait(lambda: self.store.run(first)["status"] == "cancelled")
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 1)
        self.assertEqual(self.store.episode_progress(spec), 1)

    def test_real_retry_completes_before_next_episode(self):
        script = "from pathlib import Path; import sys; p=Path('attempt-{episode}'); prior=p.exists(); p.touch(); sys.exit(0 if prior or int('{episode}') != 1 else 3)"
        spec = self.spec(episode_total=2, episodes_per_trigger=2)
        argv = [sys.executable, "-u", "-c", script]
        spec["job"].update(argv=argv, command=subprocess.list2cmdline(argv), retries=1, retry_delay=1)
        ident = self.store.save_schedule(spec)
        saved = self.store.schedules()[0]
        self.engine.submit_schedule(ident)
        self.engine.start()
        self.wait(lambda: self.store.episode_progress(saved) == 3)
        runs = self.store.rows("SELECT episode,status,attempt FROM runs ORDER BY rowid")
        self.assertEqual([(r["episode"], r["status"], r["attempt"]) for r in runs],
                         [(1, "failed", 0), (1, "completed", 1), (2, "completed", 0)])

    def test_old_database_migrates_without_losing_jobs(self):
        folder = self.root / "old-data"
        folder.mkdir()
        db = sqlite3.connect(folder / "vtgui.db")
        db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY,job TEXT,status TEXT,origin TEXT,created TEXT,started TEXT,ended TEXT,code INTEGER,attempt INTEGER,parent TEXT,available REAL,message TEXT)")
        db.execute("INSERT INTO runs VALUES ('old',?,'completed','单次','time',NULL,NULL,0,0,NULL,0,'ok')", (json.dumps(self.spec()["job"]),))
        db.commit()
        db.close()
        migrated = Store(folder)
        try:
            self.assertEqual(migrated.run("old")["status"], "completed")
            self.assertIsNone(migrated.run("old")["batch"])
        finally:
            migrated.db.close()


if __name__ == "__main__":
    unittest.main()
