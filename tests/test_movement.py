import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from vtgui import archive
from vtgui.core import DEFAULTS, Engine, Store


class FileMovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI Files 中文 ")
        self.root = Path(self.temp.name)
        self.source = self.root / "downloads"
        self.series = self.source / "Light.the.Dark.S01.2026"
        self.series.mkdir(parents=True)
        self.destination = self.root / "同步剧" / self.series.name
        self.destination.mkdir(parents=True)
        self.logs = []

    def tearDown(self):
        self.temp.cleanup()

    def test_files_go_directly_into_selected_series_directory(self):
        episode = self.series / "Light.the.Dark.S01E07.mkv"
        subtitle = self.series / "subtitles/Light.the.Dark.S01E07.zh.srt"
        subtitle.parent.mkdir()
        episode.write_bytes(b"episode-seven")
        subtitle.write_bytes(b"subtitle")
        (self.series / "poster.jpg").write_bytes(b"keep-poster")
        count = archive.move_files(self.source, self.destination, [str(episode), str(subtitle)], self.logs.append)
        self.assertEqual(count, 2)
        self.assertEqual((self.destination / episode.name).read_bytes(), b"episode-seven")
        self.assertEqual((self.destination / subtitle.name).read_bytes(), b"subtitle")
        self.assertFalse((self.destination / self.series.name).exists())
        self.assertFalse((self.destination / "subtitles").exists())
        self.assertEqual((self.series / "poster.jpg").read_bytes(), b"keep-poster")
        self.assertTrue(self.series.is_dir())

    def test_existing_flat_filename_is_skipped_and_other_files_continue(self):
        episode = self.series / "E07.mkv"
        episode.write_bytes(b"source-copy")
        (self.destination / episode.name).write_bytes(b"existing-copy")
        subtitle = self.series / "E07.srt"
        subtitle.write_bytes(b"subtitle")
        count = archive.move_files(self.source, self.destination, [str(episode), str(subtitle)], self.logs.append)
        self.assertEqual(count, 1)
        self.assertEqual(episode.read_bytes(), b"source-copy")
        self.assertEqual((self.destination / episode.name).read_bytes(), b"existing-copy")
        self.assertEqual((self.destination / subtitle.name).read_bytes(), b"subtitle")
        self.assertFalse((self.destination / self.series.name).exists())

    def test_duplicate_basenames_in_different_source_directories_never_overwrite(self):
        first = self.series / "episode.mkv"
        second = self.source / "Other.Series/episode.mkv"
        second.parent.mkdir()
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        self.assertEqual(archive.move_files(self.source, self.destination, [str(first), str(second)], self.logs.append), 1)
        self.assertEqual((self.destination / "episode.mkv").read_bytes(), b"first")
        self.assertEqual(second.read_bytes(), b"second")


class MovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI Merge 中文 ")
        self.root = Path(self.temp.name)
        self.source = self.root / "downloads"
        self.folder = self.source / "Show.S01"
        self.folder.mkdir(parents=True)
        self.destination = self.root / "library"
        self.target = self.destination / self.folder.name
        self.target.mkdir(parents=True)
        (self.folder / "Show.S01E01.mkv").write_bytes(b"source-old")
        (self.folder / "Show.S01E02.mkv").write_bytes(b"new-episode")
        (self.folder / "Show.S01E02.srt").write_bytes(b"new-subtitle")
        (self.target / "Show.S01E01.mkv").write_bytes(b"target-old")
        self.logs = []

    def tearDown(self):
        self.temp.cleanup()

    def move(self, cancelled=lambda: False):
        snapshots = {str(self.folder): archive.folder_snapshot(self.folder, "*.mkv")}
        return archive.move_folders(self.source, self.destination, snapshots, "*.mkv", self.logs.append, cancelled)

    def test_merge_skips_existing_file_but_moves_new_episode_and_subtitle(self):
        self.assertEqual(self.move(), 1)
        self.assertEqual((self.target / "Show.S01E01.mkv").read_bytes(), b"target-old")
        self.assertEqual((self.folder / "Show.S01E01.mkv").read_bytes(), b"source-old")
        self.assertEqual((self.target / "Show.S01E02.mkv").read_bytes(), b"new-episode")
        self.assertEqual((self.target / "Show.S01E02.srt").read_bytes(), b"new-subtitle")
        self.assertFalse((self.folder / "Show.S01E02.mkv").exists())

    def test_merge_recurses_and_only_removes_empty_source_subdirectories(self):
        nested = self.folder / "Season1/Subtitles/zh"
        nested.mkdir(parents=True)
        (nested / "E02.srt").write_bytes(b"nested-subtitle")
        (self.target / "Season1").mkdir()
        self.assertEqual(self.move(), 1)
        self.assertEqual((self.target / "Season1/Subtitles/zh/E02.srt").read_bytes(), b"nested-subtitle")
        self.assertFalse((self.folder / "Season1").exists())
        self.assertTrue(self.folder.exists())

    def test_all_duplicate_files_are_kept_at_source_and_target(self):
        (self.target / "Show.S01E02.mkv").write_bytes(b"existing-second")
        (self.target / "Show.S01E02.srt").write_bytes(b"existing-subtitle")
        self.assertEqual(self.move(), 0)
        self.assertEqual((self.target / "Show.S01E02.mkv").read_bytes(), b"existing-second")
        self.assertEqual((self.folder / "Show.S01E02.mkv").read_bytes(), b"new-episode")

    def test_file_directory_collision_does_not_block_other_files(self):
        (self.target / "Show.S01E02.mkv").mkdir()
        self.assertEqual(self.move(), 1)
        self.assertTrue((self.folder / "Show.S01E02.mkv").exists())
        self.assertEqual((self.target / "Show.S01E02.srt").read_bytes(), b"new-subtitle")

    def test_one_file_failure_does_not_block_other_new_files(self):
        copy_stat = archive.shutil.copystat
        def fail_one(src, dst, *args, **kwargs):
            if Path(src).name == "Show.S01E02.mkv":
                raise PermissionError("simulated locked metadata")
            return copy_stat(src, dst, *args, **kwargs)
        with patch("vtgui.archive.shutil.copystat", side_effect=fail_one):
            self.assertEqual(self.move(), 1)
        self.assertTrue((self.folder / "Show.S01E02.mkv").exists())
        self.assertEqual((self.target / "Show.S01E02.srt").read_bytes(), b"new-subtitle")
        self.assertFalse(list(self.target.glob("*.partial")))

    def test_cancel_merge_preserves_files_and_does_not_publish_partial_copy(self):
        self.assertEqual(self.move(lambda: bool(list(self.target.glob("*.partial")))), 0)
        self.assertTrue((self.folder / "Show.S01E02.mkv").exists())
        self.assertEqual((self.target / "Show.S01E01.mkv").read_bytes(), b"target-old")
        self.assertFalse(list(self.target.glob("*.partial")))

    def test_target_link_is_not_followed(self):
        (self.folder / "blocked").mkdir()
        (self.folder / "blocked/extra.srt").write_bytes(b"stay")
        (self.target / "blocked").mkdir()
        original = archive.is_link
        with patch("vtgui.archive.is_link", side_effect=lambda p: Path(p) == self.target / "blocked" or original(p)):
            self.assertEqual(self.move(), 1)
        self.assertTrue((self.folder / "blocked/extra.srt").exists())
        self.assertFalse((self.target / "blocked/extra.srt").exists())
        self.assertTrue((self.target / "Show.S01E02.mkv").exists())

    def test_merge_across_real_volumes(self):
        workdir = Path.cwd() / ".qa"
        workdir.mkdir(exist_ok=True)
        if self.root.stat().st_dev == workdir.stat().st_dev:
            self.skipTest("A second volume is not available")
        with tempfile.TemporaryDirectory(prefix="merge-cross-volume-", dir=workdir) as temp:
            self.destination = Path(temp)
            self.target = self.destination / self.folder.name
            self.target.mkdir()
            (self.target / "Show.S01E01.mkv").write_bytes(b"target-old")
            self.assertEqual(self.move(), 1)
            self.assertEqual((self.target / "Show.S01E01.mkv").read_bytes(), b"target-old")
            self.assertEqual((self.target / "Show.S01E02.srt").read_bytes(), b"new-subtitle")

    def test_legacy_global_policy_is_removed_but_schedule_policy_survives(self):
        store = Store(self.root / "data")
        job = {**DEFAULTS, "root": str(self.root), "command": "echo test", "move_enabled": True,
               "destination": str(self.destination)}
        plan = store.save_schedule({"job": job, "time": "09:00", "days": [0], "enabled": True})
        scheduled = store.trigger_schedule(plan)
        oneoff = store.enqueue(job)
        retry = store.enqueue(job, "自动重试", parent=oneoff)
        store.write("INSERT OR REPLACE INTO settings VALUES ('move_enabled','true')")
        store.write("INSERT OR REPLACE INTO settings VALUES ('destination',?)", (json.dumps(str(self.destination)),))
        store.db.close()
        reopened = Store(self.root / "data")
        try:
            self.assertFalse(reopened.settings()["move_enabled"])
            self.assertEqual(reopened.settings()["destination"], "")
            self.assertTrue(reopened.run(scheduled)["job"]["move_enabled"])
            self.assertFalse(reopened.run(oneoff)["job"]["move_enabled"])
            self.assertFalse(reopened.run(retry)["job"]["move_enabled"])
            self.assertTrue(reopened.schedules()[0]["job"]["move_enabled"])
        finally:
            reopened.db.close()

    def test_two_schedules_keep_independent_destination_and_switch(self):
        store = Store(self.root / "data")
        try:
            job = {**DEFAULTS, "root": str(self.root), "command": "echo test", "move_enabled": True, "destination": str(self.destination)}
            a = store.save_schedule({"job": job, "time": "09:00", "days": [0]})
            b = store.save_schedule({"job": {**job, "move_enabled": False, "destination": str(self.root / "other")}, "time": "10:00", "days": [1]})
            store.save_settings({"move_enabled": True, "destination": str(self.root / "global")})
            first = store.run(store.trigger_schedule(a))["job"]
            second = store.run(store.trigger_schedule(b))["job"]
            self.assertTrue(first["move_enabled"])
            self.assertFalse(second["move_enabled"])
            self.assertEqual(first["destination"], str(self.destination))
            self.assertEqual(second["destination"], str(self.root / "other"))
        finally:
            store.db.close()

    def test_rename_and_merge_multiple_episodes_keeps_leftover_original_names(self):
        store = Store(self.root / "data")
        engine = Engine(store)
        renamed_target = self.destination / "Series.S02"
        renamed_target.mkdir()
        (renamed_target / "Series.S02E02.mkv").write_bytes(b"target-episode-one")
        script = f"from pathlib import Path; p=Path({str(self.folder)!r}); p.mkdir(exist_ok=True); (p/'Show.S01E{{episode}}.mkv').write_bytes(b'new-{{episode}}'); (p/'Show.S01E{{episode}}.srt').write_bytes(b'sub-{{episode}}')"
        spec = {"job": {**DEFAULTS, "root": str(self.root), "command": script,
                        "argv": [sys.executable, "-c", script], "move_enabled": True, "destination": str(self.destination),
                        "rename_season": "+1", "rename_episode": "+1", "rename_replacements": "Show => Series"},
                "days": list(range(7)), "time": "09:00", "episode_start": "02", "episode_total": 3, "episodes_per_trigger": 2}
        try:
            plan = store.save_schedule(spec)
            engine.submit_schedule(plan)
            engine.start()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and store.episode_progress(store.schedules()[0]) != 4:
                time.sleep(.05)
            self.assertEqual(store.episode_progress(store.schedules()[0]), 4)
            self.assertEqual((renamed_target / "Series.S02E02.mkv").read_bytes(), b"target-episode-one")
            self.assertEqual((renamed_target / "Series.S02E03.mkv").read_bytes(), b"new-02")
            self.assertEqual((renamed_target / "Series.S02E04.srt").read_bytes(), b"sub-03")
            self.assertEqual((self.folder / "Show.S01E01.mkv").read_bytes(), b"source-old")
            self.assertFalse((self.source / "Series.S02").exists())
        finally:
            engine.close()
            store.db.close()


if __name__ == "__main__":
    unittest.main()
