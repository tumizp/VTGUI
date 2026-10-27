"""Widget integration checks; no DRMLAB commands are run."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vtgui.core import DEFAULTS, Engine, Store
from vtgui.ui import App, ScheduleDialog


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="VTGUI-ui-")
        cls.store = Store(Path(cls.temp.name) / "data")
        cls.engine = Engine(cls.store)
        cls.app = App(cls.store, cls.engine)
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()
        cls.app.destroy()
        cls.store.db.close()
        cls.temp.cleanup()

    def test_terminal_and_controls_fit_minimum_window(self):
        self.app.geometry("1100x780")
        self.app.update()
        self.assertTrue(self.app.terminal.get("1.0", "end").strip())
        self.assertIs(self.app.terminal.master.master.master, self.app.pages["work"].winfo_children()[-1])
        for widget in (self.app.terminal, self.app.search_entry, self.app.runs_tree):
            self.assertTrue(widget.winfo_ismapped())
            bottom = widget.winfo_rooty() + widget.winfo_height()
            self.assertLessEqual(bottom, self.app.winfo_rooty() + self.app.winfo_height())
        self.assertGreater(self.app.terminal.winfo_height(), 50)

    def test_telegram_settings_save_mask_and_unsaved_test(self):
        values = {"telegram_bot_token": "123456:TEST_secret-token", "telegram_chat_ids": "123, -100456,123"}
        self.app.settings_vars["root"].set(self.temp.name)
        self.app.root_vars[0].set(self.temp.name)
        try:
            for key, value in values.items():
                self.app.settings_vars[key].set(value)
            self.assertEqual(self.app.telegram_token_entry.cget("show"), "•")
            with patch.object(self.engine.telegram, "submit") as sender:
                self.app.test_telegram()
                self.assertEqual(sender.call_args.args[0]["telegram_chat_ids"], "123,-100456")
                self.assertEqual(self.store.settings()["telegram_bot_token"], "")
            self.app.save_settings()
            self.assertEqual(self.store.settings()["telegram_chat_ids"], "123,-100456")
            self.assertEqual(self.store.settings()["telegram_bot_token"], values["telegram_bot_token"])
        finally:
            for key in values:
                self.app.settings_vars[key].set("")
            self.store.save_settings({key: "" for key in values})

    def test_schedule_editor_and_persisted_root(self):
        self.store.save_settings({"root": self.temp.name})
        dialog = ScheduleDialog(self.app)
        dialog.update()
        dialog.name.set("每天任务")
        dialog.command.insert("1.0", "echo test")
        dialog.when.set("08:30")
        dialog.save()
        spec = self.store.schedules()[0]
        self.assertEqual(spec["job"]["root"], str(Path(self.temp.name).resolve()))
        self.assertEqual(spec["days"], list(range(7)))
        self.assertEqual(spec["time"], "08:30")
        self.assertEqual(spec["job"]["move_mode"], "folders")
        editor = ScheduleDialog(self.app, spec)
        editor.policy.move_mode.set("仅成品文件")
        editor.save()
        self.assertEqual(self.store.schedules()[0]["job"]["move_mode"], "files")

    def test_episode_fields_save_and_progress_display(self):
        dialog = ScheduleDialog(self.app)
        dialog.root_dir.set(self.temp.name)
        dialog.name.set("逐集界面测试")
        dialog.command.insert("1.0", "echo S01E{episode}")
        for key, value in (("episode_start", "03"), ("episode_total", "8"), ("episodes_per_trigger", "2")):
            dialog.episode_vars[key].set(value)
        dialog.save()
        spec = next(s for s in self.store.schedules() if s["job"]["name"] == "逐集界面测试")
        try:
            self.assertEqual((spec["episode_start"], spec["episode_total"], spec["episodes_per_trigger"]), ("03", 8, 2))
            first = self.store.trigger_schedule(spec["id"])
            self.store.finish_run(first, "completed", 0, "test")
            self.app.refresh()
            self.assertIn("下次 4 / 共 8", self.app.schedules_tree.item(spec["id"], "values")[3])
            editor = ScheduleDialog(self.app, spec)
            self.assertEqual(editor.episode_vars["episode_start"].get(), "04")
            editor.destroy()
        finally:
            self.store.delete_schedule(spec["id"])

    def test_log_selection_does_not_duplicate_pending_output(self):
        job = {**DEFAULTS, "root": self.temp.name, "name": "输出去重", "command": "echo test"}
        ident = self.store.enqueue(job)
        text = "已存在的输出\n"
        raw = text.encode("utf-8")
        (self.store.logs / f"{ident}.log").write_bytes(raw)
        self.engine.emit("output", id=ident, text=text, end=len(raw))
        self.app.refresh()
        self.app.runs_tree.selection_set(ident)
        self.app.select_run()
        self.app.pump()
        self.assertEqual(self.app.terminal.get("1.0", "end").count("已存在的输出"), 1)
        self.engine.emit("output", id=ident, text="新增输出\n", end=len(raw) + len("新增输出\n".encode("utf-8")))
        self.app.pump()
        self.assertEqual(self.app.terminal.get("1.0", "end").count("新增输出"), 1)

    def test_global_settings_and_workbench_have_no_moving_policy(self):
        self.assertEqual(set(self.app.policy.values()), {"retries", "retry_delay", "timeout"})
        self.store.save_settings({"root": self.temp.name})
        self.store.write("INSERT OR REPLACE INTO settings VALUES ('move_enabled','true')")
        self.store.write("INSERT OR REPLACE INTO settings VALUES ('destination',?)", (json.dumps(self.temp.name),))
        old_command = self.app.command.get("1.0", "end-1c")
        try:
            self.app.command.delete("1.0", "end")
            self.app.command.insert("1.0", "echo test")
            job = self.app.current_job()
            self.assertFalse(job["move_enabled"])
            self.assertEqual(job["destination"], "")
        finally:
            self.app.command.delete("1.0", "end")
            self.app.command.insert("1.0", old_command)
            self.store.save_settings({})

    def test_schedule_rename_rules_preview_and_persist(self):
        dialog = ScheduleDialog(self.app)
        dialog.root_dir.set(self.temp.name)
        dialog.name.set("重命名界面测试")
        dialog.command.insert("1.0", "echo test")
        dialog.rename.season.set("+1")
        dialog.rename.episode.set("-1")
        dialog.rename.replacements.insert("1.0", "Xiong.Mei => Brother.Sister")
        dialog.rename.preview()
        self.assertIn("Brother.Sister.S02E00.mkv", dialog.rename.result.get())
        dialog.save()
        spec = next(s for s in self.store.schedules() if s["job"]["name"] == "重命名界面测试")
        try:
            self.assertEqual(spec["job"]["rename_season"], "+1")
            self.assertEqual(spec["job"]["rename_episode"], "-1")
            editor = ScheduleDialog(self.app, spec)
            self.assertEqual(editor.rename.values()["rename_replacements"], "Xiong.Mei => Brother.Sister")
            editor.destroy()
        finally:
            self.store.delete_schedule(spec["id"])


if __name__ == "__main__":
    unittest.main()
