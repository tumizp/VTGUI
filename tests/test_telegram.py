import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from vtgui.core import Engine, Store
from vtgui.telegram import TELEGRAM_DEFAULTS, TelegramNotifier, send_message, validate_telegram


CONFIG = {"telegram_bot_token": "123456:TEST_secret-token", "telegram_chat_ids": "123,-100456"}


class TelegramTests(unittest.TestCase):
    def test_config_normalizes_and_rejects_invalid_values(self):
        self.assertEqual(validate_telegram({}), TELEGRAM_DEFAULTS)
        config = validate_telegram({**CONFIG, "telegram_chat_ids": " 123, -100456，123, @channel_name, "})
        self.assertEqual(config["telegram_chat_ids"], "123,-100456,@channel_name")
        for invalid in ({"telegram_bot_token": CONFIG["telegram_bot_token"]},
                        {**CONFIG, "telegram_bot_token": "invalid"},
                        {**CONFIG, "telegram_chat_ids": "0"},
                        {**CONFIG, "telegram_chat_ids": "123,wrong"},
                        {**CONFIG, "telegram_chat_ids": ", ,"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_telegram(invalid)

    def test_https_json_plain_text_and_length(self):
        with patch("vtgui.telegram.urlopen", return_value=io.BytesIO(b'{"ok":true}')) as opener:
            send_message(CONFIG["telegram_bot_token"], "-100456", "中文 <test> 😀" * 1000)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, f"https://api.telegram.org/bot{CONFIG['telegram_bot_token']}/sendMessage")
        self.assertEqual(request.method, "POST")
        payload = json.loads(request.data)
        self.assertEqual(payload["chat_id"], "-100456")
        self.assertNotIn("parse_mode", payload)
        self.assertLessEqual(len(payload["text"].encode("utf-16-le")), 8000)
        self.assertEqual(opener.call_args.kwargs["timeout"], 10)

    def test_errors_do_not_expose_token(self):
        secret_url = f"https://api.telegram.org/bot{CONFIG['telegram_bot_token']}/sendMessage"
        for failure in (HTTPError(secret_url, 403, "Forbidden", {}, None), URLError(secret_url), TimeoutError(secret_url)):
            with self.subTest(failure=type(failure)), patch("vtgui.telegram.urlopen", side_effect=failure):
                with self.assertRaises(RuntimeError) as caught:
                    send_message(CONFIG["telegram_bot_token"], "123", "test")
                self.assertNotIn(CONFIG["telegram_bot_token"], str(caught.exception))
        for response in (b'{"ok":false}', b'invalid', b'[]'):
            with patch("vtgui.telegram.urlopen", return_value=io.BytesIO(response)), self.assertRaises(RuntimeError):
                send_message(CONFIG["telegram_bot_token"], "123", "test")

    def test_multiple_recipients_continue_after_one_fails(self):
        reports = []
        notifier = TelegramNotifier(lambda *args: reports.append(args))
        try:
            with patch("vtgui.telegram.send_message", side_effect=[RuntimeError("发送失败"), None]) as sender:
                notifier.submit(CONFIG, "任务结果")
                notifier.pending.join()
                self.assertEqual([c.args[1] for c in sender.call_args_list], ["123", "-100456"])
            self.assertEqual([r[1] for r in reports], ["failed", "completed"])
        finally:
            notifier.close()

    def test_close_discards_remaining_recipients_and_callbacks(self):
        entered, release, closing = threading.Event(), threading.Event(), threading.Event()
        reports = []
        notifier = TelegramNotifier(lambda *args: reports.append(args))
        def slow_send(*args):
            entered.set()
            release.wait(3)
        with patch("vtgui.telegram.send_message", side_effect=slow_send) as sender:
            notifier.submit(CONFIG, "任务结果")
            self.assertTrue(entered.wait(2))
            def close():
                closing.set()
                notifier.close()
            closer = threading.Thread(target=close)
            closer.start()
            self.assertTrue(closing.wait(2))
            self.assertTrue(notifier.stopped.wait(2))
            release.set()
            closer.join(3)
            self.assertFalse(closer.is_alive())
            self.assertEqual(sender.call_count, 1)
            self.assertEqual(reports, [])


class TelegramEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI-telegram-")
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def wait(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail("通知未按时完成")

    def test_settings_persist_but_secrets_do_not_enter_jobs(self):
        self.assertEqual(self.store.settings()["telegram_bot_token"], "")
        self.store.save_settings(CONFIG)
        job = {**self.store.settings(), "root": str(self.root), "command": "echo test"}
        ident = self.engine.submit(job)
        spec = self.store.save_schedule({"job": job, "time": "09:00", "days": [0]})
        self.assertNotIn("telegram_bot_token", self.store.run(ident)["job"])
        self.assertNotIn("telegram_bot_token", self.store.schedules()[0]["job"])
        self.assertTrue(spec)
        self.store.db.close()
        self.store = Store(self.root / "data")
        self.engine.store = self.store
        self.assertEqual(self.store.settings()["telegram_bot_token"], CONFIG["telegram_bot_token"])
        self.store.save_settings(TELEGRAM_DEFAULTS)
        with patch("vtgui.telegram.send_message") as sender:
            self.engine.notify("disabled")
            self.assertIsNone(self.engine.telegram.thread)
            sender.assert_not_called()

    def test_real_run_retry_and_completion_notify_without_blocking(self):
        self.store.save_settings(CONFIG)
        entered, release = threading.Event(), threading.Event()
        def slow_send(*args):
            entered.set()
            release.wait(5)
        with patch("vtgui.telegram.send_message", side_effect=slow_send) as sender:
            job = {"root": str(self.root), "name": "通知测试", "command": "python test",
                   "argv": [sys.executable, "-c", "raise SystemExit(1)"], "retries": 1, "retry_delay": 1}
            first = self.engine.submit(job)
            second = self.engine.submit({**job, "argv": [sys.executable, "-c", "print('ok')"], "retries": 0})
            self.engine.start()
            try:
                self.assertTrue(entered.wait(4))
                self.wait(lambda: len(self.store.rows("SELECT * FROM runs WHERE status IN ('completed','failed')")) == 3)
                self.assertEqual(self.store.run(first)["status"], "failed")
                self.assertEqual(self.store.run(second)["status"], "completed")
            finally:
                release.set()
            self.engine.telegram.pending.join()
            self.assertEqual(sender.call_count, 6)
            texts = [c.args[2] for c in sender.call_args_list]
            self.assertTrue(any("第 2 次" in text for text in texts))
            self.assertTrue(any("已完成" in text for text in texts))
            self.assertEqual(len(self.store.rows("SELECT * FROM events WHERE source='Telegram 通知'")), 6)

    def test_queued_cancel_notifies_once(self):
        self.store.save_settings(CONFIG)
        with patch("vtgui.telegram.send_message") as sender:
            ident = self.engine.submit({"root": str(self.root), "command": "echo test"})
            self.engine.cancel(ident)
            self.engine.cancel(ident)
            self.engine.telegram.pending.join()
            self.assertEqual(sender.call_count, 2)
            self.assertIn("已取消", sender.call_args.args[2])

    def test_history_notifies_only_new_results(self):
        path = self.root / "history.json"
        self.store.save_settings({**CONFIG, "root": str(self.root), "history_file": str(path), "monitor_enabled": True})
        with patch("vtgui.telegram.send_message") as sender:
            entries = [{"id": "old", "status": "completed"}]
            path.write_text(json.dumps(entries))
            self.engine.poll_history()
            sender.assert_not_called()
            entries.extend([{"id": "new", "status": "completed"}, {"id": "failed", "status": "failed"}])
            path.write_text(json.dumps(entries))
            self.engine.poll_history()
            self.engine.poll_history()
            self.engine.telegram.pending.join()
            self.assertEqual(sender.call_count, 4)


if __name__ == "__main__":
    unittest.main()
