import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from vtgui.archive import folder_snapshot
from vtgui.core import DEFAULTS, Engine, Store
from vtgui.rename import RenameError, renamed_name, rename_items, validate_rename


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI Rename 中文 ")
        self.root = Path(self.temp.name)
        self.source = self.root / "downloads"
        self.folder = self.source / "Xiong.Mei.S01"
        self.folder.mkdir(parents=True)
        (self.folder / "Xiong.Mei.S01E01.mkv").write_bytes(b"episode-one")
        (self.folder / "Xiong.Mei.S01E01.srt").write_bytes(b"subtitle")
        (self.folder / "poster.jpg").write_bytes(b"poster")
        self.patterns = "*.mkv"
        self.rules = {"rename_season": "+1", "rename_episode": "+2", "rename_replacements": "Xiong.Mei => Brother.Sister"}
        self.logs = []

    def tearDown(self):
        self.temp.cleanup()

    def rename(self, **options):
        return rename_items(self.source, {str(self.folder): folder_snapshot(self.folder, self.patterns)}, [],
                            self.patterns, self.rules, self.logs.append, **options)

    def test_order_and_extension_preservation(self):
        rules = {**self.rules, "rename_replacements": "Xiong.Mei => Brother.Sister\nS02 => S07"}
        self.assertEqual(renamed_name("Xiong.Mei.S01E01.mkv", rules), "Brother.Sister.S07E03.mkv")
        self.assertEqual(renamed_name("Xiong.Mei.S01E01.srt", rules), "Brother.Sister.S07E03.srt")
        self.assertEqual(renamed_name("movie.mkv", {"rename_replacements": "mkv => mp4"}), "movie.mkv")

    def test_absolute_and_relative_number_rules(self):
        self.assertEqual(renamed_name("Show.S01E12.mkv", {"rename_season": "S03", "rename_episode": "-2"}), "Show.S03E10.mkv")
        self.assertEqual(renamed_name("Show.S1E1.mkv", {"rename_season": "2", "rename_episode": "E003"}), "Show.S02E003.mkv")
        self.assertEqual(renamed_name("Show.s01e01.mkv", self.rules), "Show.s02e03.mkv")
        self.assertEqual(renamed_name("Show.S01E01-E02.mkv", {"rename_episode": "+1"}), "Show.S01E02-E03.mkv")
        self.assertEqual(renamed_name("Xiong.Mei.S01", self.rules, True), "Brother.Sister.S02")

    def test_replacement_is_literal_case_sensitive_and_runs_top_to_bottom(self):
        rules = {"rename_replacements": "A.B => C\nC => D\nremove =>"}
        self.assertEqual(renamed_name("A.B.remove.a.b.mkv", rules), "D..a.b.mkv")

    def test_invalid_rules_and_negative_results(self):
        for rules in ({"rename_season": "plus1"}, {"rename_episode": "S02"},
                      {"rename_replacements": "missing arrow"}, {"rename_replacements": "=> X"},
                      {"rename_replacements": "old => ../escape"}):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                validate_rename(rules)
        with self.assertRaises(RenameError):
            renamed_name("Show.S01E01.mkv", {"rename_episode": "-2"})
        with self.assertRaises(RenameError):
            renamed_name("Show.mkv", {"rename_replacements": "Show => CON"})

    def test_recursive_rename_preserves_bytes_and_maps_full_paths(self):
        season = self.folder / "Season.S01"
        season.mkdir()
        (season / "Xiong.Mei.S01E02.mkv").write_bytes(b"episode-two")
        mapping = self.rename(journal=self.root / "rename.json")
        target = self.source / "Brother.Sister.S02"
        self.assertFalse(self.folder.exists())
        self.assertEqual((target / "Brother.Sister.S02E03.mkv").read_bytes(), b"episode-one")
        self.assertEqual((target / "Brother.Sister.S02E03.srt").read_bytes(), b"subtitle")
        self.assertEqual((target / "Season.S02/Brother.Sister.S02E04.mkv").read_bytes(), b"episode-two")
        self.assertEqual(mapping[str(self.folder)], str(target))
        self.assertEqual(json.loads((self.root / "rename.json").read_text(encoding="utf-8"))["status"], "completed")

    def test_offset_handles_existing_sequential_names_without_overwriting(self):
        (self.folder / "Xiong.Mei.S01E02.mkv").write_bytes(b"episode-two")
        self.rules = {"rename_episode": "+1"}
        self.rename()
        self.assertEqual((self.folder / "Xiong.Mei.S01E02.mkv").read_bytes(), b"episode-one")
        self.assertEqual((self.folder / "Xiong.Mei.S01E03.mkv").read_bytes(), b"episode-two")

    def test_duplicate_final_names_abort_before_changes(self):
        (self.folder / "Xiong.Mei.S01E02.mkv").write_bytes(b"episode-two")
        self.rules = {"rename_episode": "E05"}
        with self.assertRaisesRegex(RenameError, "同名"):
            self.rename()
        self.assertEqual((self.folder / "Xiong.Mei.S01E01.mkv").read_bytes(), b"episode-one")
        self.assertFalse(list(self.source.rglob(".vtgui-rename-*")))

    def test_archive_destination_type_conflict_still_aborts(self):
        collision = self.source / "Brother.Sister.S02"
        destination = self.root / "library"
        destination.mkdir()
        (destination / collision.name).write_bytes(b"not-a-folder")
        with self.assertRaisesRegex(RenameError, "移动目标不是普通目录"):
            self.rename(destination=destination)
        self.assertTrue(self.folder.exists())

    def test_existing_directory_merges_and_overwrites_files_recursively(self):
        collision = self.source / "Brother.Sister.S02"
        collision.mkdir()
        (collision / "Brother.Sister.S02E03.mkv").write_bytes(b"old-version")
        (collision / "Brother.Sister.S02E02.mkv").write_bytes(b"earlier-episode")
        (collision / "poster.jpg").write_bytes(b"old-poster")
        nested = self.folder / "Season.S01"
        nested.mkdir()
        (nested / "Xiong.Mei.S01E02.mkv").write_bytes(b"episode-two")
        (collision / "Season.S02").mkdir()
        (collision / "Season.S02/Brother.Sister.S02E04.mkv").write_bytes(b"old-nested")
        (collision / "Season.S02/earlier.srt").write_bytes(b"older-subtitle")
        self.rename(journal=self.root / "rename.json")
        self.assertFalse(self.folder.exists())
        self.assertEqual((collision / "Brother.Sister.S02E03.mkv").read_bytes(), b"episode-one")
        self.assertEqual((collision / "Brother.Sister.S02E02.mkv").read_bytes(), b"earlier-episode")
        self.assertEqual((collision / "poster.jpg").read_bytes(), b"poster")
        self.assertEqual((collision / "Season.S02/Brother.Sister.S02E04.mkv").read_bytes(), b"episode-two")
        self.assertEqual((collision / "Season.S02/earlier.srt").read_bytes(), b"older-subtitle")
        self.assertFalse(list(self.source.rglob(".vtgui-rename-*")))

    def test_empty_existing_directory_can_be_merged(self):
        collision = self.source / "Brother.Sister.S02"
        collision.mkdir()
        self.rename()
        self.assertEqual((collision / "Brother.Sister.S02E03.mkv").read_bytes(), b"episode-one")
        self.assertFalse(self.folder.exists())

    def test_loose_file_overwrites_existing_file(self):
        old = self.source / "Xiong.Mei.S01E01.mkv"
        new = self.source / "Brother.Sister.S02E03.mkv"
        old.write_bytes(b"replacement")
        new.write_bytes(b"old-version")
        rename_items(self.source, {}, [str(old)], self.patterns, self.rules, self.logs.append)
        self.assertEqual(new.read_bytes(), b"replacement")
        self.assertFalse(old.exists())

    def test_merge_type_conflicts_abort_before_changes(self):
        for directory_source in (True, False):
            with self.subTest(directory_source=directory_source):
                collision = self.source / "Brother.Sister.S02"
                if directory_source:
                    collision.write_bytes(b"not-a-directory")
                else:
                    collision.mkdir()
                    (collision / "Brother.Sister.S02E03.mkv").mkdir()
                with self.assertRaisesRegex(RenameError, "类型冲突"):
                    self.rename()
                self.assertTrue(self.folder.exists())
                self.assertFalse(list(self.source.rglob(".vtgui-rename-*")))
                if directory_source:
                    collision.unlink()
                else:
                    (collision / "Brother.Sister.S02E03.mkv").rmdir()
                    collision.rmdir()

    def test_failure_after_overwrite_restores_both_source_and_existing_files(self):
        collision = self.source / "Brother.Sister.S02"
        collision.mkdir()
        (collision / "Brother.Sister.S02E03.mkv").write_bytes(b"old-version")
        (collision / "poster.jpg").write_bytes(b"old-poster")
        original = os.rename
        failed = False
        def fail_once(src, dst):
            nonlocal failed
            if Path(dst) == collision / "poster.jpg" and not failed:
                failed = True
                raise PermissionError("simulated locked poster")
            return original(src, dst)
        with patch("vtgui.rename.os.rename", side_effect=fail_once):
            with self.assertRaisesRegex(RenameError, "已恢复原名称"):
                self.rename(journal=self.root / "rename.json")
        self.assertTrue(failed)
        self.assertEqual((self.folder / "Xiong.Mei.S01E01.mkv").read_bytes(), b"episode-one")
        self.assertEqual((self.folder / "poster.jpg").read_bytes(), b"poster")
        self.assertEqual((collision / "Brother.Sister.S02E03.mkv").read_bytes(), b"old-version")
        self.assertEqual((collision / "poster.jpg").read_bytes(), b"old-poster")
        self.assertFalse((collision / "Brother.Sister.S02E03.srt").exists())
        self.assertFalse(list(self.source.rglob(".vtgui-rename-*")))
        self.assertEqual(json.loads((self.root / "rename.json").read_text(encoding="utf-8"))["status"], "rolled_back")

    def test_cancellation_after_overwrite_restores_existing_file(self):
        target = self.source / "Brother.Sister.S02"
        target.mkdir()
        media = target / "Brother.Sister.S02E03.mkv"
        media.write_bytes(b"old-version")
        with self.assertRaisesRegex(RenameError, "已恢复原名称"):
            self.rename(cancelled=lambda: media.exists() and media.read_bytes() == b"episode-one")
        self.assertEqual(media.read_bytes(), b"old-version")
        self.assertEqual((self.folder / "Xiong.Mei.S01E01.mkv").read_bytes(), b"episode-one")
        self.assertFalse(list(self.source.rglob(".vtgui-rename-*")))

    def test_final_verification_failure_restores_merged_directories(self):
        target = self.source / "Brother.Sister.S02"
        target.mkdir()
        (target / "Brother.Sister.S02E03.mkv").write_bytes(b"old-version")
        inspect = folder_snapshot
        def fail_verification(folder, *args, **kwargs):
            if Path(folder) == target and not self.folder.exists():
                raise OSError("simulated verification failure")
            return inspect(folder, *args, **kwargs)
        with patch("vtgui.rename.folder_snapshot", side_effect=fail_verification):
            with self.assertRaisesRegex(RenameError, "已恢复原名称"):
                self.rename(journal=self.root / "rename.json")
        self.assertEqual((self.folder / "Xiong.Mei.S01E01.mkv").read_bytes(), b"episode-one")
        self.assertEqual((target / "Brother.Sister.S02E03.mkv").read_bytes(), b"old-version")
        self.assertFalse(list(self.source.rglob(".vtgui-rename-*")))

    def test_existing_episodes_in_merged_folder_do_not_receive_offsets_again(self):
        target = self.source / "Brother.Sister.S02"
        target.mkdir()
        earlier = target / "Brother.Sister.S02E01.mkv"
        earlier.write_bytes(b"earlier")
        store = Store(self.root / "data")
        engine = Engine(store)
        job = {**DEFAULTS, **self.rules, "root": str(self.root), "command": "test", "move_enabled": False}
        try:
            engine._archive(job, [str(self.folder / "Xiong.Mei.S01E01.mkv")], self.logs.append, "first")
            new = target / "Xiong.Mei.S01E02.mkv"
            new.write_bytes(b"second")
            engine._archive(job, [str(new)], self.logs.append, "second")
            self.assertEqual(earlier.read_bytes(), b"earlier")
            self.assertEqual((target / "Brother.Sister.S02E03.mkv").read_bytes(), b"episode-one")
            self.assertEqual((target / "Brother.Sister.S02E04.mkv").read_bytes(), b"second")
        finally:
            engine.close()
            store.db.close()

    def test_engine_can_rename_into_existing_series_without_moving(self):
        target = self.source / "Brother.Sister.S02"
        target.mkdir()
        (target / "Brother.Sister.S02E04.mkv").write_bytes(b"old-version")
        (target / "Brother.Sister.S02E01.mkv").write_bytes(b"earlier")
        run, _, _ = self.run_engine(move_enabled=False, destination="")
        self.assertEqual(run["status"], "completed")
        self.assertEqual((target / "Brother.Sister.S02E04.mkv").read_bytes(), b"new-episode")
        self.assertEqual((target / "Brother.Sister.S02E01.mkv").read_bytes(), b"earlier")

    def test_file_move_after_merge_keeps_existing_series_names(self):
        target = self.source / "Brother.Sister.S02"
        target.mkdir()
        (target / "Brother.Sister.S02E01.mkv").write_bytes(b"earlier")
        run, _, _ = self.run_engine(move_mode="files")
        self.assertEqual(run["status"], "completed")
        self.assertEqual((target / "Brother.Sister.S02E01.mkv").read_bytes(), b"earlier")
        self.assertTrue((target / "Xiong.Mei.S01E01.srt").exists())
        self.assertTrue((self.root / "library/Brother.Sister.S02E04.mkv").exists())

    def test_folder_move_after_merge_includes_existing_episodes(self):
        target = self.source / "Brother.Sister.S02"
        target.mkdir()
        (target / "Brother.Sister.S02E01.mkv").write_bytes(b"earlier")
        run, _, _ = self.run_engine()
        self.assertEqual(run["status"], "completed")
        self.assertEqual((self.root / "library/Brother.Sister.S02/Brother.Sister.S02E01.mkv").read_bytes(), b"earlier")

    def test_runtime_error_rolls_back_files_and_folder(self):
        original = os.rename
        calls = 0
        def fail_once(src, dst):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise PermissionError("simulated locked file")
            return original(src, dst)
        with patch("vtgui.rename.os.rename", side_effect=fail_once):
            with self.assertRaisesRegex(RenameError, "已恢复原名称"):
                self.rename(journal=self.root / "rename.json")
        self.assertEqual((self.folder / "Xiong.Mei.S01E01.mkv").read_bytes(), b"episode-one")
        self.assertFalse(list(self.source.rglob(".vtgui-rename-*")))
        self.assertEqual(json.loads((self.root / "rename.json").read_text(encoding="utf-8"))["status"], "rolled_back")

    def test_cancellation_rolls_back_completed_steps(self):
        with self.assertRaisesRegex(RenameError, "已恢复原名称"):
            self.rename(cancelled=lambda: bool(list(self.source.rglob(".vtgui-rename-*"))))
        self.assertTrue((self.folder / "Xiong.Mei.S01E01.mkv").exists())
        self.assertFalse(list(self.source.rglob(".vtgui-rename-*")))

    def test_failed_rollback_keeps_journal_and_file_content(self):
        original = os.rename
        calls = 0
        def locked(src, dst):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise PermissionError("simulated forward and rollback lock")
            return original(src, dst)
        with patch("vtgui.rename.os.rename", side_effect=locked):
            with self.assertRaisesRegex(RenameError, "部分名称未能恢复"):
                self.rename(journal=self.root / "rename.json")
        record = json.loads((self.root / "rename.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "rollback_failed")
        contents = [p.read_bytes() for p in self.source.rglob("*") if p.is_file()]
        self.assertIn(b"episode-one", contents)
        self.assertIn(b"subtitle", contents)

    def test_case_only_name_changes_work(self):
        self.rules = {"rename_replacements": "Xiong => xiong"}
        self.rename()
        self.assertIn("xiong.Mei.S01", [p.name for p in self.source.iterdir()])

    def test_loose_files_can_be_renamed(self):
        path = self.source / "Xiong.Mei.S01E01.mkv"
        path.write_bytes(b"loose")
        mapping = rename_items(self.source, {}, [str(path)], self.patterns, self.rules, self.logs.append)
        self.assertEqual(Path(mapping[str(path)]).name, "Brother.Sister.S02E03.mkv")
        self.assertEqual(Path(mapping[str(path)]).read_bytes(), b"loose")

    def run_engine(self, **overrides):
        store = Store(self.root / "data")
        engine = Engine(store)
        script = f"from pathlib import Path; p=Path({str(self.folder / 'Xiong.Mei.S01E02.mkv')!r}); p.write_bytes(b'new-episode')"
        job = {**DEFAULTS, **self.rules, "root": str(self.root), "name": "重命名测试",
               "argv": [sys.executable, "-c", script], "command": "模拟下载新一集",
               "move_enabled": True, "destination": str(self.root / "library"), **overrides}
        try:
            ident = engine.submit(job)
            engine.start()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                run = store.run(ident)
                if run["status"] not in ("queued", "running"):
                    return run, store.rows("SELECT * FROM runs"), (store.logs / f"{ident}.log").read_text(encoding="utf-8")
                time.sleep(.04)
            self.fail("Rename engine did not finish")
        finally:
            engine.close()
            store.db.close()

    def test_engine_renames_folder_and_sidecars_before_moving(self):
        run, _, log = self.run_engine()
        self.assertEqual(run["status"], "completed")
        target = self.root / "library/Brother.Sister.S02"
        self.assertEqual((target / "Brother.Sister.S02E04.mkv").read_bytes(), b"new-episode")
        self.assertTrue((target / "Brother.Sister.S02E03.srt").exists())
        self.assertFalse(self.folder.exists())
        self.assertLess(log.index("已重命名"), log.index("已移动剧集文件夹"))

    def test_rename_only_does_not_require_moving(self):
        run, _, _ = self.run_engine(move_enabled=False, destination="")
        self.assertEqual(run["status"], "completed")
        self.assertTrue((self.source / "Brother.Sister.S02/Brother.Sister.S02E04.mkv").exists())
        self.assertFalse((self.root / "library").exists())

    def test_postprocess_failure_never_retries_download_or_moves(self):
        run, runs, _ = self.run_engine(rename_episode="E05", retries=2, retry_delay=1)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(len(runs), 1)
        self.assertTrue((self.folder / "Xiong.Mei.S01E01.mkv").exists())
        self.assertFalse((self.root / "library").exists())

    def test_file_move_mode_uses_renamed_paths(self):
        run, _, _ = self.run_engine(move_mode="files")
        self.assertEqual(run["status"], "completed")
        self.assertTrue((self.root / "library/Brother.Sister.S02E04.mkv").exists())
        self.assertFalse((self.root / "library/Brother.Sister.S02").exists())
        self.assertTrue((self.source / "Xiong.Mei.S01/Xiong.Mei.S01E01.srt").exists())

    def test_basename_globs_do_not_prevent_moving_after_rename(self):
        run, _, _ = self.run_engine(patterns="Xiong.Mei.*.mkv")
        self.assertEqual(run["status"], "completed")
        self.assertTrue((self.root / "library/Brother.Sister.S02/Brother.Sister.S02E04.mkv").exists())

    def test_real_cross_volume_after_renaming(self):
        workdir = Path.cwd() / ".qa"
        workdir.mkdir(exist_ok=True)
        if workdir.stat().st_dev == self.root.stat().st_dev:
            self.skipTest("A second volume is not available")
        with tempfile.TemporaryDirectory(prefix="rename-cross-volume-", dir=workdir) as temp:
            run, _, _ = self.run_engine(destination=temp)
            self.assertEqual(run["status"], "completed")
            self.assertEqual((Path(temp) / "Brother.Sister.S02/Brother.Sister.S02E04.mkv").read_bytes(), b"new-episode")
            self.assertFalse(self.folder.exists())

    def test_unfinished_journal_blocks_further_changes(self):
        logs = self.root / "data/logs"
        logs.mkdir(parents=True)
        (logs / "previous.rename.json").write_text(json.dumps({"status": "applying", "source": str(self.source)}), encoding="utf-8")
        run, runs, log = self.run_engine(retries=1, retry_delay=1)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(len(runs), 1)
        self.assertIn("未完成", log)
        self.assertTrue(self.folder.exists())

    def test_rename_failure_blocks_next_episode_and_download_retries(self):
        store = Store(self.root / "data")
        engine = Engine(store)
        script = f"from pathlib import Path; Path({str(self.folder / 'Xiong.Mei.S01E{episode}.mkv')!r}).write_bytes(b'new')"
        spec = {"job": {**DEFAULTS, **self.rules, "root": str(self.root), "name": "逐集重命名失败",
                        "argv": [sys.executable, "-c", script], "command": script,
                        "rename_episode": "E05", "retries": 1, "retry_delay": 1,
                        "move_enabled": True, "destination": str(self.root / "library")},
                "days": list(range(7)), "time": "09:00", "enabled": True,
                "episode_start": "02", "episode_total": 3, "episodes_per_trigger": 2}
        try:
            ident = store.save_schedule(spec)
            first = engine.submit_schedule(ident)
            engine.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and store.run(first)["status"] in ("queued", "running"):
                time.sleep(.04)
            self.assertEqual(store.run(first)["status"], "failed")
            self.assertEqual(len(store.rows("SELECT * FROM runs")), 1)
            self.assertEqual(store.episode_progress(store.schedules()[0]), 2)
            self.assertFalse((self.root / "library").exists())
        finally:
            engine.close()
            store.db.close()

    def test_existing_renamed_items_do_not_receive_offset_twice(self):
        store = Store(self.root / "data")
        engine = Engine(store)
        job = {**DEFAULTS, **self.rules, "root": str(self.root), "command": "test", "move_enabled": False}
        try:
            first = self.folder / "Xiong.Mei.S01E01.mkv"
            engine._archive(job, [str(first)], self.logs.append, "first")
            target = self.source / "Brother.Sister.S02"
            new = target / "Xiong.Mei.S01E02.mkv"
            new.write_bytes(b"second")
            engine._archive(job, [str(new)], self.logs.append, "second")
            self.assertEqual((target / "Brother.Sister.S02E03.mkv").read_bytes(), b"episode-one")
            self.assertEqual((target / "Brother.Sister.S02E04.mkv").read_bytes(), b"second")
            self.assertFalse((self.source / "Brother.Sister.S03").exists())
        finally:
            engine.close()
            store.db.close()


if __name__ == "__main__":
    unittest.main()
