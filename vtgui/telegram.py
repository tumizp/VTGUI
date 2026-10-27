"""Telegram result notifications, isolated from the task and Tk threads."""
from __future__ import annotations

import json
import queue
import re
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TELEGRAM_DEFAULTS = {"telegram_bot_token": "", "telegram_chat_ids": ""}


def validate_telegram(values: dict) -> dict:
    token = values.get("telegram_bot_token", "").strip()
    raw = values.get("telegram_chat_ids", "").strip()
    if not token and not raw:
        return dict(TELEGRAM_DEFAULTS)
    if not token or not raw:
        raise ValueError("Telegram 通知需要同时填写 Bot Token 和聊天 ID；两项均清空可关闭通知。")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Telegram Bot Token 格式无效，应为 BotFather 提供的 数字:密钥。")
    ids = []
    for item in raw.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if re.fullmatch(r"-?[0-9]+", item) and int(item) != 0:
            item = str(int(item))
        elif not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,}", item):
            raise ValueError("Telegram 聊天 ID 应为非零整数（群组通常为负数）或 @频道用户名，多个用逗号分隔。")
        if item not in ids:
            ids.append(item)
    if not ids:
        raise ValueError("请至少填写一个 Telegram 聊天 ID。")
    return {"telegram_bot_token": token, "telegram_chat_ids": ",".join(ids)}


def send_message(token: str, chat_id: str, text: str):
    # Plain text avoids interpreting task names or output as Telegram markup.
    # Limit UTF-16 units conservatively, including non-BMP characters.
    text = text.encode("utf-16-le", errors="replace")[:8000].decode("utf-16-le", errors="ignore")
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage",
                      data=json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=10) as response:
            result = json.load(response)
    except HTTPError as exc:
        # Exception strings include the request URL, which contains the token.
        exc.close()
        raise RuntimeError(f"Telegram HTTP {exc.code}（请检查 Token、聊天 ID、发送权限或限流）") from None
    except (URLError, OSError):
        raise RuntimeError("无法连接 Telegram，请检查网络或系统代理。") from None
    except (ValueError, UnicodeError):
        raise RuntimeError("Telegram 返回了无效响应。") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram 未确认发送成功，请检查配置和发送权限。")


class TelegramNotifier:
    def __init__(self, report):
        self.report = report
        self.pending = queue.Queue(maxsize=1000)
        self.stopped = threading.Event()
        self.guard = threading.RLock()
        self.thread = None

    def submit(self, settings: dict, text: str):
        config = validate_telegram(settings)
        if not config["telegram_bot_token"]:
            return
        with self.guard:
            if self.stopped.is_set():
                return
            try:
                self.pending.put_nowait((config, text))
            except queue.Full:
                self.report("Telegram 通知", "failed", "通知队列已满，本条通知未发送。")
                return
            if not self.thread:
                self.thread = threading.Thread(target=self._loop, name="vtgui-telegram", daemon=True)
                self.thread.start()

    def _loop(self):
        while not self.stopped.is_set():
            try:
                config, text = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                for chat_id in config["telegram_chat_ids"].split(","):
                    if self.stopped.is_set():
                        break
                    try:
                        send_message(config["telegram_bot_token"], chat_id, text)
                        status, detail = "completed", "发送成功"
                    except RuntimeError as exc:
                        status, detail = "failed", str(exc)
                    except Exception:
                        status, detail = "failed", "发送异常，请检查网络和配置。"
                    with self.guard:
                        if not self.stopped.is_set():
                            self.report("Telegram 通知", status, f"聊天 {chat_id}：{detail}")
            finally:
                self.pending.task_done()

    def close(self):
        with self.guard:
            self.stopped.set()
        if self.thread:
            self.thread.join(timeout=11)
