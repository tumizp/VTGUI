import errno
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vtgui import archive
from vtgui.core import DEFAULTS, Engine, Store, validate_job


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI 剧集 ")
        self.base = Path(self.temp.name)
        self.source = self.base / "downloads"
        self.folder = self.source / "剧集 第一季"
        self.folder.mkdir(parents=True)
        (self.folder / "第01集.mkv").write_bytes(b"episode-one")
        (self.folder / "第01集.srt").write_text("subtitle", encoding="utf-8")
        (self.folder / "poster.jpg").write_bytes(b"poster")
        (self.folder / "empty").mkdir()
        self.destination = self.base / "library"
        self.logs = []
        self.patterns = DEFAULTS["patterns"]

    def tearDown(self):
        self.temp.cleanup()

    def snapshots(self):
        return {str(self.folder): archive.folder_snapshot(self.folder, self.patterns)}

    def move(self, snapshots=None, cancelled=lambda: False):
        return archive.move_folders(self.source, self.destination, snapshots if snapshots is not None else self.snapshots(),
                                    self.patterns, self.logs.append, cancelled)

    def cross_volume(self):
        rename = os.rename
        def simulated(src, dst):
            if Path(src) == self.folder:
                raise OSError(errno.EXDEV, "Simulated cross-volume move")
            return rename(src, dst)
        return patch("vtgui.archive.os.rename", side_effect=simulated)

    def test_whole_series_with_sidecars_and_empty_directory(self):
        self.assertEqual(self.move(), 1)
        self.assertFalse(self.folder.exists())
        target = self.destination / self.folder.name
        self.assertEqual((target / "第01集.mkv").read_bytes(), b"episode-one")
        self.assertEqual((target / "第01集.srt").read_text(encoding="utf-8"), "subtitle")
        self.assertTrue((target / "poster.jpg").exists())
        self.assertTrue((target / "empty").is_dir())

    def test_existing_series_merges_new_files_without_overwriting(self):
        target = self.destination / self.folder.name
        target.mkdir(parents=True)
        (target / "existing.mkv").write_bytes(b"existing")
        (target / "第01集.mkv").write_bytes(b"target-existing")
        self.assertEqual(self.move(), 1)
        self.assertTrue((self.folder / "第01集.mkv").exists())
        self.assertEqual((target / "第01集.mkv").read_bytes(), b"target-existing")
        self.assertEqual((target / "第01集.srt").read_text(encoding="utf-8"), "subtitle")
        self.assertEqual((target / "existing.mkv").read_bytes(), b"existing")
        self.assertTrue((target / "poster.jpg").exists())

    def test_temporary_or_empty_media_blocks_whole_folder(self):
        temp = self.folder / "第02集.mkv.part"
        temp.touch()
        with self.assertRaisesRegex(OSError, "临时"):
            self.snapshots()
        temp.unlink()
        (self.folder / "第02集.mkv").touch()
        with self.assertRaisesRegex(OSError, "空的成品"):
            self.snapshots()

    def test_changed_folder_is_preserved(self):
        expected = self.snapshots()
        (self.folder / "new.nfo").write_text("new", encoding="utf-8")
        self.assertEqual(self.move(expected), 0)
        self.assertTrue(self.folder.exists())
        self.assertFalse((self.destination / self.folder.name).exists())

    def test_cross_volume_staged_copy_preserves_all_content(self):
        with self.cross_volume():
            self.assertEqual(self.move(), 1)
        self.assertFalse(self.folder.exists())
        self.assertEqual((self.destination / self.folder.name / "第01集.mkv").read_bytes(), b"episode-one")
        self.assertTrue((self.destination / self.folder.name / "empty").is_dir())
        self.assertFalse(list(self.destination.glob("*.partial")))

    def test_cross_volume_cancel_keeps_source(self):
        with self.cross_volume():
            self.assertEqual(self.move(cancelled=lambda: self.destination.exists() and bool(list(self.destination.glob("*.partial")))), 0)
        self.assertTrue((self.folder / "第01集.mkv").exists())
        self.assertFalse((self.destination / self.folder.name).exists())
        self.assertFalse(list(self.destination.glob("*.partial")))

    def test_source_changed_during_copy_is_not_removed(self):
        copy = archive._copy_folder
        def changing(*args):
            copy(*args)
            (self.folder / "new.nfo").write_text("new", encoding="utf-8")
        with self.cross_volume(), patch("vtgui.archive._copy_folder", side_effect=changing):
            self.assertEqual(self.move(), 0)
        self.assertTrue((self.folder / "第01集.mkv").exists())
        self.assertTrue((self.folder / "new.nfo").exists())
        self.assertFalse((self.destination / self.folder.name).exists())

    def test_real_cross_volume_move_when_available(self):
        workdir = Path.cwd() / ".qa"
        workdir.mkdir(exist_ok=True)
        if workdir.stat().st_dev == self.base.stat().st_dev:
            self.skipTest("A second volume is not available")
        with tempfile.TemporaryDirectory(prefix="real-cross-volume-", dir=workdir) as temp:
            source = Path(temp)
            series = source / "跨盘剧集"
            series.mkdir()
            (series / "episode.mkv").write_bytes(b"real-cross-volume-media")
            (series / "episode.srt").write_bytes(b"real-subtitle")
            snapshots = {str(series): archive.folder_snapshot(series, self.patterns)}
            self.assertEqual(archive.move_folders(source, self.destination, snapshots, self.patterns, self.logs.append), 1)
            self.assertFalse(series.exists())
            self.assertEqual((self.destination / series.name / "episode.mkv").read_bytes(), b"real-cross-volume-media")
            self.assertEqual((self.destination / series.name / "episode.srt").read_bytes(), b"real-subtitle")

    def test_destination_collision_during_copy_is_preserved(self):
        copy = archive._copy_folder
        def colliding(*args):
            copy(*args)
            target = self.destination / self.folder.name
            target.mkdir()
            (target / "other.txt").write_text("other", encoding="utf-8")
        with self.cross_volume(), patch("vtgui.archive._copy_folder", side_effect=colliding):
            self.assertEqual(self.move(), 1)
        self.assertFalse(self.folder.exists())
        self.assertEqual((self.destination / self.folder.name / "other.txt").read_text(encoding="utf-8"), "other")
        self.assertEqual((self.destination / self.folder.name / "第01集.mkv").read_bytes(), b"episode-one")

    def test_links_are_rejected(self):
        original = archive.is_link
        with patch("vtgui.archive.is_link", side_effect=lambda p: p.name == "poster.jpg" or original(p)):
            with self.assertRaisesRegex(OSError, "链接"):
                self.snapshots()

    def test_plan_groups_by_series_and_keeps_loose_files(self):
        folders, loose = archive.archive_plan(self.source, [str(self.folder / "第01集.mkv"),
                                                          str(self.folder / "Season2/第02集.mkv"),
                                                          str(self.source / "movie.mkv"), str(self.base / "outside.mkv")])
        self.assertEqual(folders, [self.folder])
        self.assertEqual(loose, [str(self.source / "movie.mkv")])

    def run_engine(self, job=None, history_entry=None):
        store = Store(self.base / "data")
        engine = Engine(store)
        try:
            policy = {**DEFAULTS, "root": str(self.base), "move_enabled": True, "destination": str(self.destination)}
            if history_entry:
                history = self.base / "history.json"
                history.write_text("[]", encoding="utf-8")
                store.save_settings({**policy, "monitor_enabled": True, "history_file": str(history)})
                engine.poll_history()
                history.write_text(json.dumps([history_entry]), encoding="utf-8")
                engine.poll_history()
                self.assertFalse(store.rows("SELECT id FROM runs"))
                return
            else:
                ident = engine.submit({**policy, **job})
            engine.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                run = store.run(ident)
                if run["status"] not in ("queued", "running"):
                    self.assertEqual(run["status"], "completed")
                    return
                time.sleep(0.05)
            self.fail("Engine did not finish")
        finally:
            engine.close()
            store.db.close()

    def test_task_moves_existing_series_after_new_episode(self):
        import sys
        untouched = self.source / "其他剧集"
        untouched.mkdir()
        (untouched / "old.mkv").write_bytes(b"untouched")
        script = f"from pathlib import Path; Path({str(self.folder / '第02集.mkv')!r}).write_bytes(b'episode-two')"
        self.run_engine({"argv": [sys.executable, "-c", script], "command": "测试新集完成"})
        self.assertFalse(self.folder.exists())
        self.assertEqual((self.destination / self.folder.name / "第02集.mkv").read_bytes(), b"episode-two")
        self.assertTrue((self.destination / self.folder.name / "第01集.srt").exists())
        self.assertTrue((untouched / "old.mkv").exists())

    def test_task_moves_series_after_existing_episode_updated(self):
        import sys
        script = f"from pathlib import Path; Path({str(self.folder / '第01集.mkv')!r}).write_bytes(b'updated-and-completed')"
        self.run_engine({"argv": [sys.executable, "-c", script], "command": "测试更新完成"})
        self.assertFalse(self.folder.exists())
        self.assertEqual((self.destination / self.folder.name / "第01集.mkv").read_bytes(), b"updated-and-completed")

    def test_task_keeps_series_with_unfinished_download(self):
        import sys
        (self.folder / "第03集.part").touch()
        script = f"from pathlib import Path; Path({str(self.folder / '第02集.mkv')!r}).write_bytes(b'episode-two')"
        self.run_engine({"argv": [sys.executable, "-c", script], "command": "测试未完成文件"})
        self.assertTrue((self.folder / "第02集.mkv").exists())
        self.assertFalse((self.destination / self.folder.name).exists())

    def test_external_history_does_not_move_without_a_schedule(self):
        self.run_engine(history_entry={"id": "series-1", "status": "completed", "folders": [str(self.folder)]})
        self.assertTrue(self.folder.exists())
        self.assertFalse((self.destination / self.folder.name).exists())

    def test_legacy_job_defaults_to_series_mode(self):
        job = validate_job({"root": str(self.base), "command": "echo test"})
        self.assertEqual(job["move_mode"], "folders")
        with self.assertRaises(ValueError):
            validate_job({**job, "move_mode": "unknown"})


if __name__ == "__main__":
    unittest.main()
