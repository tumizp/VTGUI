"""Workspace registry migration, limits, persistence and actual task cwd."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from vtgui.core import DEFAULTS, Engine, Store


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI workspaces 中文 ")
        self.base = Path(self.temp.name)
        self.paths = [self.base / f"project {i}" for i in range(4)]
        for path in self.paths:
            path.mkdir()
        self.store = Store(self.base / "data")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_legacy_root_migrates_and_survives_restart(self):
        self.store.write("INSERT INTO settings VALUES ('root',?)", (json.dumps(str(self.paths[0])),))
        self.assertEqual(self.store.settings()["roots"], [str(self.paths[0])])
        self.store.save_settings({"root": str(self.paths[1])})
        self.store.db.close()
        self.store = Store(self.base / "data")
        self.assertEqual(self.store.settings()["roots"], list(map(str, self.paths[:2])))
        self.assertEqual(self.store.settings()["root"], str(self.paths[1]))

    def test_three_directory_limit_and_duplicates_are_atomic(self):
        for path in self.paths[:3]:
            self.store.save_settings({"root": str(path)})
        self.store.save_settings({"root": str(self.paths[0] / ".." / self.paths[0].name)})
        before = self.store.settings()
        with self.assertRaisesRegex(ValueError, "3"):
            self.store.save_settings({"root": str(self.paths[3]), "retries": 10})
        self.assertEqual(self.store.settings(), before)

    def test_removing_workspace_keeps_existing_schedule_and_queue_roots(self):
        self.store.save_settings({"roots": list(map(str, self.paths[:3])), "root": str(self.paths[0])})
        job = {**DEFAULTS, "root": str(self.paths[0]), "command": "echo test"}
        spec_id = self.store.save_schedule({"job": job, "time": "09:00", "days": [0]})
        run_id = self.store.trigger_schedule(spec_id)
        self.store.save_settings({"roots": [str(self.paths[1])]})
        self.assertEqual(self.store.settings()["root"], str(self.paths[1]))
        self.assertEqual(self.store.schedules()[0]["job"]["root"], str(self.paths[0]))
        self.assertEqual(self.store.run(run_id)["job"]["root"], str(self.paths[0]))
        self.store.save_settings({"roots": []})
        self.assertEqual(self.store.settings()["root"], "")
        self.assertEqual(self.store.settings()["roots"], [])

    def test_invalid_directory_does_not_replace_registry(self):
        with self.assertRaises(ValueError):
            self.store.save_settings({"roots": [str(self.base / "missing")]})
        self.assertEqual(self.store.settings()["roots"], [])

    def test_single_and_scheduled_commands_keep_selected_cwd_after_switch(self):
        self.store.save_settings({"roots": list(map(str, self.paths[:3]))})
        engine = Engine(self.store)
        runs = []
        try:
            for i, path in enumerate(self.paths[:3]):
                script = "from pathlib import Path; Path('executed.txt').write_text(str(Path.cwd()), encoding='utf-8')"
                job = {**self.store.settings(), "root": str(path), "command": subprocess.list2cmdline([sys.executable, '-c', script])}
                if i == 1:
                    spec = self.store.save_schedule({"job": job, "time": "09:00", "days": [0]})
                    runs.append(self.store.trigger_schedule(spec))
                else:
                    runs.append(engine.submit(job))
            self.store.save_settings({"root": str(self.paths[2])})
            for ident, path in zip(runs, self.paths):
                engine._run(ident)
                self.assertEqual(self.store.run(ident)["status"], "completed")
                self.assertEqual((path / 'executed.txt').read_text(encoding='utf-8'), str(path))
                self.assertNotIn('roots', self.store.run(ident)['job'])
        finally:
            engine.close()


if __name__ == '__main__':
    unittest.main()
