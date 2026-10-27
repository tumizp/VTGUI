"""Multiple daily slots, legacy plans, persistence and episode progress."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from vtgui.core import DEFAULTS, Engine, Store, next_occurrence, schedule_times, validate_schedule


class ScheduleTimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI multi-time ")
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def spec(self, **values):
        return {"job": {**DEFAULTS, "root": str(self.root), "command": "echo test"},
                "time": "18:00", "extra_times": ["23:59", "00:00", "09:00"],
                "days": [0], "enabled": True, **values}

    def tick(self, now):
        self.engine.last_poll = now - timedelta(minutes=1)
        self.engine.tick_schedules(now)

    def test_validation_normalizes_all_four_slots_without_changing_primary(self):
        spec = validate_schedule(self.spec(extra_times=["23:59", "0:0", "9:0"]))
        self.assertEqual(spec["time"], "18:00")
        self.assertEqual(spec["extra_times"], ["23:59", "00:00", "09:00"])
        self.assertEqual(schedule_times(spec), ["00:00", "09:00", "18:00", "23:59"])

    def test_rejects_too_many_malformed_and_duplicate_times(self):
        for times in (["01:00", "02:00", "03:00", "04:00"], "09:00", None,
                      [""], ["24:00"], ["09:60"], ["bad"], [None], [900],
                      ["18:0"], ["09:00", "9:0"]):
            with self.subTest(times=times), self.assertRaises(ValueError):
                validate_schedule(self.spec(extra_times=times))

    def test_next_occurrence_uses_earliest_remaining_slot_then_selected_weekday(self):
        spec = self.spec()
        for now, expected in (
            (datetime(2026, 10, 18, 23, 59), datetime(2026, 10, 19, 0, 0)),
            (datetime(2026, 10, 19, 0, 0), datetime(2026, 10, 19, 9, 0)),
            (datetime(2026, 10, 19, 9, 1), datetime(2026, 10, 19, 18, 0)),
            (datetime(2026, 10, 19, 18, 0), datetime(2026, 10, 19, 23, 59)),
            (datetime(2026, 10, 19, 23, 59), datetime(2026, 10, 26, 0, 0)),
        ):
            with self.subTest(now=now):
                self.assertEqual(next_occurrence(spec, now), expected)

    def test_four_slots_fire_once_each_and_restart_does_not_repeat(self):
        ident = self.store.save_schedule(self.spec())
        for hour, minute in ((0, 0), (9, 0), (18, 0), (23, 59)):
            now = datetime(2026, 10, 19, hour, minute)
            self.tick(now)
            self.tick(now)
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 4)
        self.assertEqual(len(self.store.rows("SELECT * FROM fires")), 4)
        self.engine.close()
        self.store.db.close()
        self.store = Store(self.root / "data")
        self.engine = Engine(self.store)
        self.assertEqual(self.store.schedules()[0]["extra_times"], self.spec()["extra_times"])
        self.tick(datetime(2026, 10, 19, 23, 59))
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 4)
        self.tick(datetime(2026, 10, 20, 9, 0))  # Unselected weekday.
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 4)
        spec = self.store.schedules()[0]
        self.store.save_schedule({**spec, "enabled": False})
        self.tick(datetime(2026, 10, 26, 9, 0))
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 4)
        self.store.save_schedule({**spec, "extra_times": []})
        self.tick(datetime(2026, 10, 26, 23, 59))  # Removed slot.
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 4)
        self.assertEqual(self.store.schedules()[0]["id"], ident)

    def test_extra_slots_do_not_catch_up_after_offline_gap(self):
        self.store.save_schedule(self.spec())
        self.engine.last_poll = datetime(2026, 10, 19, 8, 0)
        self.engine.tick_schedules(datetime(2026, 10, 19, 18, 1))
        self.assertEqual(self.store.rows("SELECT * FROM runs"), [])
        self.engine.tick_schedules(datetime(2026, 10, 19, 23, 59))
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 1)

    def test_legacy_schedule_without_extra_times_still_displays_and_fires(self):
        spec = self.spec()
        spec.pop("extra_times")
        self.store.write("INSERT INTO schedules VALUES (?,?)", ("legacy", json.dumps(spec)))
        saved = self.store.schedules()[0]
        self.assertEqual(schedule_times(saved), ["18:00"])
        self.assertEqual(next_occurrence(saved, datetime(2026, 10, 19, 17)), datetime(2026, 10, 19, 18))
        self.tick(datetime(2026, 10, 19, 9))
        self.assertEqual(self.store.rows("SELECT * FROM runs"), [])
        self.tick(datetime(2026, 10, 19, 18))
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), 1)
        self.store.save_schedule(saved)
        self.assertEqual(self.store.schedules()[0]["extra_times"], [])

    def test_episode_slots_resume_progress_and_skip_unfinished_batch(self):
        spec = self.spec(episode_start="01", episode_total=3, episodes_per_trigger=1)
        spec["job"]["command"] = "echo E{episode}"
        self.store.save_schedule(spec)
        self.tick(datetime(2026, 10, 19, 0))
        self.tick(datetime(2026, 10, 19, 9))  # First batch is still queued.
        runs = self.store.rows("SELECT * FROM runs ORDER BY rowid")
        self.assertEqual([run["episode"] for run in runs], [1])
        self.store.finish_run(runs[0]["id"], "completed", 0, "fixture")
        self.tick(datetime(2026, 10, 19, 18))
        runs = self.store.rows("SELECT * FROM runs ORDER BY rowid")
        self.assertEqual([run["episode"] for run in runs], [1, 2])
        saved = self.store.schedules()[0]
        self.store.save_schedule({**saved, "extra_times": ["23:58"]})
        self.assertEqual(self.store.episode_progress(saved), 2)
        self.store.finish_run(runs[-1]["id"], "completed", 0, "fixture")
        self.tick(datetime(2026, 10, 19, 23, 58))
        runs = self.store.rows("SELECT * FROM runs ORDER BY rowid")
        self.assertEqual([run["episode"] for run in runs], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
