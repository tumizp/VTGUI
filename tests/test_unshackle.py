"""Shared completion protocol and unshackle integration; no real downloads."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vtgui.core import DEFAULTS, Engine, Store
from vtgui.projects import FinalOutputs, project_name


class OutputTests(unittest.TestCase):
    def test_split_ansi_unicode_and_final_line(self):
        with tempfile.TemporaryDirectory() as temp:
            outputs = FinalOutputs(temp)
            value = "noise\n\x1b[32m===VT_FINAL_DIR=== downloads/剧 集\x1b[0m\r\n===VT_FINAL_FILE=== downloads/movie.mp4"
            for char in value:
                outputs.feed(char)
            outputs.feed("", final=True)
            paths = [str(Path(temp) / name) for name in ("downloads/剧 集/E01.mkv", "downloads/movie.mp4", "downloads/Other/E01.mkv")]
            self.assertEqual(outputs.select(paths), paths[:2])

    def test_no_markers_keeps_snapshot_fallback(self):
        outputs = FinalOutputs(".")
        outputs.feed("normal output\n===VT_FINAL_FILE===\n", final=True)
        self.assertFalse(outputs.seen)
        self.assertEqual(outputs.select(["example.mkv"]), ["example.mkv"])

    def test_exact_file_does_not_select_siblings_or_prefixes(self):
        with tempfile.TemporaryDirectory() as temp:
            outputs = FinalOutputs(temp)
            outputs.feed("===VT_FINAL_FILE=== downloads/A/movie.mp4\n")
            paths = [str(Path(temp) / name) for name in ("downloads/A/movie.mp4", "downloads/A/other.mp4", "downloads/AB/movie.mp4")]
            self.assertEqual(outputs.select(paths), paths[:1])

    def test_identifies_both_projects_and_other_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(project_name(temp), "命令项目")
            (root / "vinetrimmer").mkdir()
            self.assertEqual(project_name(temp), "DRMLAB")
            (root / "unshackle").mkdir()
            self.assertEqual(project_name(temp), "unshackle")


class UnshackleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI unshackle 中文 ")
        self.root = Path(self.temp.name)
        (self.root / "unshackle").mkdir()
        self.store = Store(self.root / "data")
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def run_fixture(self, marker=True, code=0, mode="files", rename="", outside=False, single=False):
        source = self.root / "custom downloads"
        series = source / "Example.S01"
        series.mkdir(parents=True)
        (series / "Example.S01E00.mkv").write_bytes(b"previous")
        script = self.root / "fixture.py"
        script.write_text(
            "from pathlib import Path\nimport sys\n"
            "p=Path('custom downloads/Example.S01'); f=p/'Example.S01E01.mkv'; f.write_bytes(b'finished')\n"
            "(p/'Example.S01E01.srt').write_bytes(b'subtitle')\n"
            "q=Path('custom downloads/Other'); q.mkdir(); (q/'Other.mkv').write_bytes(b'unrelated')\n"
            + ("print('===VT_FINAL_DIR===', " + ("Path('outside').resolve()" if outside else "p.resolve()") + ", flush=True)\n" if marker else "")
            + f"sys.exit({code})\n", encoding="utf-8")
        job = {**DEFAULTS, "root": str(self.root), "source": "custom downloads", "command": subprocess.list2cmdline([sys.executable, str(script)]),
               "move_enabled": not single, "destination": str(self.root / "library"), "move_mode": mode, "rename_season": rename}
        ident = self.store.enqueue(job)
        self.engine._run(ident)
        return self.store.run(ident), source

    def test_marked_directory_moves_only_its_new_files(self):
        run, source = self.run_fixture()
        self.assertEqual(run["status"], "completed")
        self.assertEqual((self.root / "library/Example.S01E01.mkv").read_bytes(), b"finished")
        self.assertTrue((source / "Example.S01/Example.S01E00.mkv").exists())
        self.assertTrue((source / "Other/Other.mkv").exists())

    def test_folder_rename_and_move_follow_completion_directory(self):
        run, source = self.run_fixture(mode="folders", rename="+1")
        self.assertEqual(run["status"], "completed")
        folder = self.root / "library/Example.S02"
        self.assertEqual((folder / "Example.S02E01.mkv").read_bytes(), b"finished")
        self.assertEqual((folder / "Example.S02E01.srt").read_bytes(), b"subtitle")
        self.assertTrue((folder / "Example.S02E00.mkv").exists())
        self.assertTrue((source / "Other/Other.mkv").exists())

    def test_markers_outside_configured_source_do_not_expand_move(self):
        run, source = self.run_fixture(outside=True)
        self.assertEqual(run["status"], "completed")
        self.assertFalse((self.root / "library").exists())
        self.assertTrue((source / "Example.S01/Example.S01E01.mkv").exists())
        self.assertIn("没有可确认的新成品", run["message"])

    def test_failed_download_with_markers_does_not_move(self):
        run, source = self.run_fixture(code=7)
        self.assertEqual(run["status"], "failed")
        self.assertFalse((self.root / "library").exists())
        self.assertTrue((source / "Example.S01/Example.S01E01.mkv").exists())

    def test_unmarked_download_uses_existing_snapshot_behavior(self):
        run, source = self.run_fixture(marker=False)
        self.assertEqual(run["status"], "completed")
        self.assertTrue((self.root / "library/Other.mkv").exists())
        self.assertTrue((self.root / "library/Example.S01E01.mkv").exists())

    def test_single_task_keeps_downloads_and_completion_log(self):
        run, source = self.run_fixture(single=True)
        self.assertEqual(run["status"], "completed")
        self.assertFalse((self.root / "library").exists())
        self.assertIn("===VT_FINAL_DIR===", (self.store.logs / (run["id"] + ".log")).read_text(encoding="utf-8"))

    def test_environment_check_includes_unshackle_tools(self):
        from vtgui.ui import App
        with patch("vtgui.ui.shutil.which", return_value="tool") as which:
            with patch.object(App, "text_dialog") as dialog:
                class Value:
                    def __init__(self, value): self.value = value
                    def get(self): return self.value
                class Form:
                    settings_vars = {"root": Value(str(self.root)), "history_file": Value("vinetrimmer/download_history.json")}
                    text_dialog = dialog
                App.check_environment(Form())
                self.assertIn("项目：unshackle", dialog.call_args.args[1])
                self.assertTrue({"uv", "unshackle", "vt"} <= {call.args[0] for call in which.call_args_list})


if __name__ == "__main__":
    unittest.main()
