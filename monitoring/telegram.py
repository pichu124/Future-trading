"""Gửi cảnh báo Telegram cho bot.

Port gọn từ `vnstock-dashboard/vnstock_telegram.send_message`:
- tin > 4096 ký tự được cắt theo DÒNG, đánh số (1/n);
- HTML được escape rồi mở lại whitelist tag; Telegram vẫn trả 400 ⇒ gửi lại dạng plain;
- group bị nâng thành supergroup (400 kèm `migrate_to_chat_id`) ⇒ đi theo id mới và nhắc sửa .env;
- kênh: `channel="trading"` đọc TELEGRAM_TRADING_BOT_TOKEN / _CHAT_ID, thiếu thì lấy
  TELEGRAM_BOT_TOKEN / _CHAT_ID mặc định.

Gửi cảnh báo KHÔNG được làm sập vòng giao dịch: `notify()` nuốt lỗi và trả False;
`send_message()` ném lỗi để công cụ chạy tay thấy rõ.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

MAX_MSG_LEN = 4096
_PART_SUFFIX_RESERVE = 16          # chỗ cho "\n<i>(12/12)</i>"
_API = "https://api.telegram.org/bot{token}/{method}"
_ALLOWED_TAGS = ("b", "i", "u", "s", "code", "pre")
_TAG_RE = re.compile(r"</?(?:%s)>" % "|".join(_ALLOWED_TAGS))


class TelegramConfigError(RuntimeError):
    pass


def channel_env(channel: str) -> tuple[str, str]:
    up = channel.upper()
    return f"TELEGRAM_{up}_BOT_TOKEN", f"TELEGRAM_{up}_CHAT_ID"


def credentials(channel: Optional[str] = None, env: Optional[dict] = None) -> tuple[str, str]:
    env = os.environ if env is None else env
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if channel:
        tok_var, chat_var = channel_env(channel)
        token = (env.get(tok_var) or "").strip() or token
        chat = (env.get(chat_var) or "").strip() or chat
    return token, chat


def sanitize_html(text: str) -> str:
    """Escape & < > rồi trả lại các tag whitelist. Idempotent với tag hợp lệ."""
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for tag in _ALLOWED_TAGS:
        out = out.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return out


def strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text)


def split_lines(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    """Cắt theo dòng thành các phần ≤ limit; dòng dài hơn một tin thì cắt cứng."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur += "\n" + line
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return chunks


def _migrated_to(resp: Any) -> Optional[Any]:
    try:
        return (resp.json().get("parameters") or {}).get("migrate_to_chat_id") or None
    except Exception:
        return None


def _error_detail(resp: Any) -> str:
    try:
        return str(resp.json().get("description") or resp.text[:200])
    except Exception:
        return str(getattr(resp, "text", ""))[:200]


def send_message(text: str, channel: Optional[str] = "trading", parse_mode: str = "HTML",
                 session: Any = None, env: Optional[dict] = None, timeout: float = 10.0) -> dict:
    """Gửi tin; ném lỗi nếu thiếu cấu hình hoặc Telegram từ chối. Trả JSON phản hồi cuối."""
    if session is None:
        import requests
        session = requests
    token, chat = credentials(channel, env)
    if not token or not chat:
        tok_var, chat_var = channel_env(channel) if channel else ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        raise TelegramConfigError(f"Thiếu {tok_var} hoặc {chat_var} trong .env")
    url = _API.format(token=token, method="sendMessage")
    if parse_mode == "HTML":
        text = sanitize_html(text)
    parts = split_lines(text, MAX_MSG_LEN - _PART_SUFFIX_RESERVE)
    n = len(parts)
    result: dict = {}
    for i, part in enumerate(parts):
        body = part if n == 1 else f"{part}\n<i>({i + 1}/{n})</i>"
        payload = {"chat_id": chat, "text": body, "parse_mode": parse_mode,
                   "disable_web_page_preview": True}
        resp = session.post(url, json=payload, timeout=timeout)
        if resp.status_code == 400:
            new_id = _migrated_to(resp)
            if new_id:
                log.warning("Group Telegram đã thành supergroup — gửi tới %s; sửa chat id trong .env", new_id)
                chat = new_id
                resp = session.post(url, json={**payload, "chat_id": chat}, timeout=timeout)
        if resp.status_code == 400 and parse_mode:
            log.warning("Telegram từ chối HTML (%s) — gửi lại dạng plain", _error_detail(resp))
            resp = session.post(url, json={"chat_id": chat, "text": strip_tags(body),
                                           "disable_web_page_preview": True}, timeout=timeout)
        if not resp.ok:
            raise RuntimeError(f"Telegram {resp.status_code}: {_error_detail(resp)}")
        result = resp.json()
    return result


def notify(text: str, channel: Optional[str] = "trading", **kwargs: Any) -> bool:
    """Bản an toàn cho vòng giao dịch: không bao giờ ném lỗi, trả True khi gửi được."""
    try:
        send_message(text, channel=channel, **kwargs)
        return True
    except Exception as exc:          # cảnh báo hỏng không được làm sập bot
        log.error("Gửi Telegram thất bại: %s", exc)
        return False
