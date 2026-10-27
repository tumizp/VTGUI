"""Opt-in black-box verification of the built executable and its child environment."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from vtgui.core import DEFAULTS, Store


@unittest.skipUnless(os.environ.get("VTGUI_TEST_EXE"), "Set VTGUI_TEST_EXE to test a built executable")
class PackagedTests(unittest.TestCase):
    def test_extra_time_triggers_packaged_scheduler(self):
        exe = Path(os.environ["VTGUI_TEST_EXE"]).resolve()
        with tempfile.TemporaryDirectory(prefix="VTGUI Extra Time 中文 ") as temp:
            root = Path(temp)
            store = Store(root / "data")
            # Allow enough time to launch the GUI within the current minute.
            now = datetime.now()
            if now.second >= 57:
                time.sleep(60 - now.second + .1)
            now = datetime.now()
            times = [(now + timedelta(hours=offset)).strftime("%H:%M") for offset in (4, 2, 0, 6)]
            plan = store.save_schedule({"job": {**DEFAULTS, "root": str(root), "name": "额外时间打包验证",
                                                "command": "echo EXTRA_TIME_OK>extra-time.txt"},
                                        "time": times[0], "extra_times": times[1:], "days": list(range(7))})
            proc = subprocess.Popen([str(exe), "--data-dir", str(root / "data")], creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    runs = store.rows("SELECT * FROM runs")
                    if runs and runs[0]["status"] == "completed":
                        break
                    self.assertIsNone(proc.poll(), "Packaged GUI exited unexpectedly")
                    time.sleep(.1)
                self.assertEqual(len(runs), 1)
                self.assertEqual((runs[0]["status"], runs[0]["origin"]), ("completed", "定时"))
                self.assertEqual((root / "extra-time.txt").read_text().strip(), "EXTRA_TIME_OK")
                fires = store.rows("SELECT * FROM fires")
                self.assertEqual([(r["schedule"], r["slot"][-5:]) for r in fires], [(plan, times[2])])
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                store.db.close()

    def test_scheduled_files_move_directly_into_selected_series_directory(self):
        exe = Path(os.environ["VTGUI_TEST_EXE"]).resolve()
        with tempfile.TemporaryDirectory(prefix="VTGUI Flat Move 中文 ") as temp:
            root = Path(temp)
            store = Store(root / "data")
            series = "Light.the.Dark.S01.2026.2160p.IQIYI.WEB-DL"
            fixture = root / "download.py"
            fixture.write_text("from pathlib import Path\n"
                               f"p=Path('downloads/{series}'); p.mkdir(parents=True)\n"
                               "f=p/'Light.the.Dark.S01E07.mkv'; f.write_bytes(b'episode-seven')\n"
                               "(p/'poster.jpg').write_bytes(b'keep-poster')\n"
                               "print('===VT_FINAL_DIR===', p.resolve(), flush=True)\n"
                               "print('===VT_FINAL_FILE===', f.resolve(), flush=True)\n", encoding="utf-8")
            destination = root / "library" / series
            destination.mkdir(parents=True)
            (destination / "Light.the.Dark.S01E06.mkv").write_bytes(b'previous-episode')
            plan = store.save_schedule({"job": {**DEFAULTS, "root": str(root), "name": "仅文件模式回归验证",
                                                 "command": f'"{sys.executable}" "{fixture}"',
                                                 "move_enabled": True, "move_mode": "files", "destination": str(destination)},
                                        "time": "09:00", "days": list(range(7)), "enabled": True})
            run_id = store.trigger_schedule(plan)
            proc = subprocess.Popen([str(exe), "--data-dir", str(root / "data")], creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and store.run(run_id)["status"] in ("queued", "running"):
                    self.assertIsNone(proc.poll(), "Packaged GUI exited unexpectedly")
                    time.sleep(.1)
                self.assertEqual(store.run(run_id)["status"], "completed")
                self.assertEqual((destination / "Light.the.Dark.S01E07.mkv").read_bytes(), b'episode-seven')
                self.assertEqual((destination / "Light.the.Dark.S01E06.mkv").read_bytes(), b'previous-episode')
                self.assertFalse((destination / series).exists())
                self.assertEqual((root / "downloads" / series / "poster.jpg").read_bytes(), b'keep-poster')
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                store.db.close()

    def test_scheduled_rename_overwrites_existing_download_directory(self):
        exe = Path(os.environ["VTGUI_TEST_EXE"]).resolve()
        with tempfile.TemporaryDirectory(prefix="VTGUI Existing Rename 中文 ") as temp:
            root = Path(temp)
            store = Store(root / "data")
            target = root / "downloads/China.Beyond.Tastes.S02"
            target.mkdir(parents=True)
            (target / "China.Beyond.Tastes.S02E03.mkv").write_bytes(b"old-version")
            (target / "China.Beyond.Tastes.S02E02.mkv").write_bytes(b"earlier-episode")
            fixture = root / "download.py"
            fixture.write_text(
                "from pathlib import Path\n"
                "p=Path('downloads/Wo.De.Mei.Shi.S01'); p.mkdir(parents=True)\n"
                "f=p/'Wo.De.Mei.Shi.S01E03.mkv'; f.write_bytes(b'new-version')\n"
                "print('===VT_FINAL_DIR===', p.resolve(), flush=True)\n"
                "print('===VT_FINAL_FILE===', f.resolve(), flush=True)\n", encoding="utf-8")
            plan = store.save_schedule({"job": {**DEFAULTS, "root": str(root), "name": "已有重命名目录覆盖",
                                                "command": f'"{sys.executable}" "{fixture}"',
                                                "rename_season": "+1",
                                                "rename_replacements": "Wo.De.Mei.Shi => China.Beyond.Tastes"},
                                        "time": "09:00", "days": list(range(7)), "enabled": True})
            run_id = store.trigger_schedule(plan)
            proc = subprocess.Popen([str(exe), "--data-dir", str(root / "data")],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and store.run(run_id)["status"] in ("queued", "running"):
                    self.assertIsNone(proc.poll(), "Packaged GUI exited unexpectedly")
                    time.sleep(.1)
                self.assertEqual(store.run(run_id)["status"], "completed")
                self.assertEqual((target / "China.Beyond.Tastes.S02E03.mkv").read_bytes(), b"new-version")
                self.assertEqual((target / "China.Beyond.Tastes.S02E02.mkv").read_bytes(), b"earlier-episode")
                self.assertFalse((root / "downloads/Wo.De.Mei.Shi.S01").exists())
                self.assertFalse(list((root / "downloads").rglob(".vtgui-rename-*")))
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                store.db.close()

    def test_scheduled_rename_merges_existing_series(self):
        exe = Path(os.environ["VTGUI_TEST_EXE"]).resolve()
        with tempfile.TemporaryDirectory(prefix="VTGUI Rename EXE ") as temp:
            root = Path(temp)
            store = Store(root / "data")
            fixture = root / "download.py"
            fixture.write_text("from pathlib import Path\np=Path('downloads/Xiong.Mei.S01'); p.mkdir(parents=True)\n(p/'Xiong.Mei.S01E03.mkv').write_bytes(b'media')\n(p/'Xiong.Mei.S01E03.srt').write_bytes(b'subtitle')\n(p/'Xiong.Mei.S01E04.mkv').write_bytes(b'new-episode')\n", encoding="utf-8")
            target = root / "library/Brother.Sister.S02"
            target.mkdir(parents=True)
            (target / "Brother.Sister.S02E02.mkv").write_bytes(b'existing-media')
            ident = store.save_schedule({"job": {**DEFAULTS, "root": str(root), "name": "打包重命名验证",
                                                "command": f'"{sys.executable}" "{fixture}"',
                                                "move_enabled": True, "destination": str(root / "library"),
                                                "rename_season": "+1", "rename_episode": "-1",
                                                "rename_replacements": "Xiong.Mei => Brother.Sister"},
                                         "time": "09:00", "days": list(range(7)), "enabled": True})
            run_id = store.trigger_schedule(ident)
            proc = subprocess.Popen([str(exe), "--data-dir", str(root / "data")], creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and store.run(run_id)["status"] in ("queued", "running"):
                    self.assertIsNone(proc.poll(), "Packaged GUI exited unexpectedly")
                    time.sleep(.1)
                self.assertEqual(store.run(run_id)["status"], "completed")
                target = root / "library/Brother.Sister.S02"
                self.assertEqual((target / "Brother.Sister.S02E02.mkv").read_bytes(), b"existing-media")
                self.assertEqual((target / "Brother.Sister.S02E03.mkv").read_bytes(), b"new-episode")
                self.assertEqual((target / "Brother.Sister.S02E02.srt").read_bytes(), b"subtitle")
                self.assertEqual((root / "downloads/Xiong.Mei.S01/Xiong.Mei.S01E03.mkv").read_bytes(), b"media")
                self.assertFalse((root / "downloads/Xiong.Mei.S01/Xiong.Mei.S01E04.mkv").exists())
                log = (store.logs / f"{run_id}.log").read_text(encoding="utf-8")
                self.assertLess(log.index("已重命名"), log.index("已合并剧集文件夹"))
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                store.db.close()

    def test_executable_executes_episode_sequence(self):
        exe = Path(os.environ["VTGUI_TEST_EXE"]).resolve()
        with tempfile.TemporaryDirectory(prefix="VTGUI Episode EXE ") as temp:
            root = Path(temp)
            data = root / "data"
            store = Store(data)
            fixture = root / "episode.py"
            fixture.write_text("from pathlib import Path\nimport sys\nepisode=sys.argv[1]\np=Path('episodes.txt')\nold=p.read_text() if p.exists() else ''\nassert old == ('' if episode == '03' else '03\\n')\np.write_text(old+episode+'\\n')\n", encoding="utf-8")
            ident = store.save_schedule({"job": {**DEFAULTS, "root": str(root), "name": "打包逐集任务",
                                                "command": f'"{sys.executable}" "{fixture}" {{episode}}'},
                                         "time": "09:00", "days": list(range(7)), "enabled": True,
                                         "episode_start": "03", "episode_total": 4, "episodes_per_trigger": 2})
            store.trigger_schedule(ident)
            self.assertEqual(len(store.rows("SELECT * FROM runs")), 1)
            proc = subprocess.Popen([str(exe), "--data-dir", str(data)], creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 15
                spec = store.schedules()[0]
                while time.monotonic() < deadline and store.episode_progress(spec) != 5:
                    self.assertIsNone(proc.poll(), "Packaged GUI exited unexpectedly")
                    time.sleep(.1)
                self.assertEqual(store.episode_progress(spec), 5)
                self.assertEqual((root / "episodes.txt").read_text(), "03\n04\n")
                runs = store.rows("SELECT episode,status FROM runs ORDER BY rowid")
                self.assertEqual([(r["episode"], r["status"]) for r in runs], [(3, "completed"), (4, "completed")])
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                store.db.close()

    def test_executable_starts_and_executes_external_commands(self):
        exe = Path(os.environ["VTGUI_TEST_EXE"]).resolve()
        with tempfile.TemporaryDirectory(prefix="VTGUI EXE 中文 ") as temp:
            root = Path(temp)
            data = root / "data"
            subprocess.run([str(exe), "--smoke-test", "--data-dir", str(data)], check=True, timeout=15)
            self.assertFalse((data / "startup-error.log").exists())
            store = Store(data)
            fixture = root / "外部 Python 测试.py"
            fixture.write_text("import os, sqlite3, ssl\nfrom pathlib import Path\nassert '_internal' not in os.environ.get('TCL_LIBRARY', '')\nprint('PACKAGED_EXTERNAL_OK', flush=True)\np = Path('downloads/剧集'); p.mkdir(parents=True, exist_ok=True); (p/'output.mkv').write_bytes(b'packaged-output'); (p/'output.srt').write_bytes(b'subtitle'); (p/'poster.jpg').write_bytes(b'poster')\n", encoding="utf-8")
            job = {**DEFAULTS, "root": str(root), "name": "打包运行验证", "move_enabled": True,
                   "destination": str(root / "library"), "command": f'"{sys.executable}" "{fixture}"'}
            plan = store.save_schedule({"job": job, "time": "09:00", "days": list(range(7)), "enabled": True})
            success_id = store.trigger_schedule(plan)
            ps_id = store.enqueue({**DEFAULTS, "root": str(root), "shell": "powershell", "command": "Write-Output 'PACKAGED_POWERSHELL_OK'"})
            failed_id = store.enqueue({**DEFAULTS, "root": str(root), "command": "exit /b 7", "retries": 1, "retry_delay": 1})
            proc = subprocess.Popen([str(exe), "--data-dir", str(data)], creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    rows = store.rows("SELECT status FROM runs")
                    if len(rows) == 4 and all(r["status"] not in ("queued", "running") for r in rows):
                        break
                    self.assertIsNone(proc.poll(), "Packaged GUI exited unexpectedly")
                    time.sleep(0.1)
                self.assertEqual(store.run(success_id)["status"], "completed")
                self.assertEqual(store.run(ps_id)["status"], "completed")
                self.assertEqual(store.run(failed_id)["code"], 7)
                self.assertEqual(len(store.rows("SELECT * FROM runs WHERE status='failed'")), 2)
                self.assertIn("PACKAGED_EXTERNAL_OK", (store.logs / f"{success_id}.log").read_text(encoding="utf-8"))
                self.assertEqual((root / "library/剧集/output.mkv").read_bytes(), b"packaged-output")
                self.assertEqual((root / "library/剧集/output.srt").read_bytes(), b"subtitle")
                self.assertEqual((root / "library/剧集/poster.jpg").read_bytes(), b"poster")
                self.assertFalse((root / "downloads/剧集").exists())
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                store.db.close()


if __name__ == "__main__":
    unittest.main()
