"""Logger JSON có cấu trúc (CLAUDE.md mục 2 `monitoring/`).

Mỗi bản ghi là MỘT dòng JSON trong `<log_dir>/<prefix>_YYYY-MM-DD.jsonl` (ngày theo giờ VN, đổi file
lúc 00:00), gồm `ts` (ISO, +07:00), `level`, `logger`, `msg` và mọi field truyền qua `extra=` hoặc
`log_event(logger, "fill", symbol=…, qty=…)`. Console vẫn in dạng người đọc.

Bảo mật (CLAUDE.md mục 7 — "không bao giờ log token, OTP, private key"), áp cho message, field và
traceback:
- khoá nhạy cảm (apiSecret, otp, passcode, *token, *secret, password, Authorization, X-Signature,
  privateKey…) ⇒ "***", ở mọi độ sâu;
- giá trị bí mật đăng ký bằng `register_secret` / `register_secrets_from_env` (biến .env tên
  *_KEY, *_SECRET, *_TOKEN, *_PASSWORD) ⇒ "<secret>" ở bất cứ đâu xuất hiện;
- mẫu nhận ra được không cần đăng ký: URL bot Telegram, `Bearer …`, `"tradingToken": "…"`.

Ghi log lỗi (đĩa đầy, file bị khoá trên Windows…) không bao giờ ném ra ngoài: `handleError`.
Đọc lại bằng `read_events(path, event=…)` để dựng timeline sau phiên.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sys
import traceback
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from core.clock import VN_TZ

_SECRETS: set[str] = set()
MIN_SECRET_LEN = 6
_MASK = "***"
_SENSITIVE_EXACT = frozenset({"otp", "passcode", "password", "authorization", "xsignature", "signature",
                              "privatekey", "apikey", "transactionid", "cookie"})
_SENSITIVE_SUFFIX = ("token", "secret", "password", "passcode")
_ENV_SUFFIX = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD")
_PATTERNS = (
    (re.compile(r"/bot\d+:[A-Za-z0-9_-]+"), "/bot<token>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1<token>"),
    (re.compile(r'("?(?:tradingToken|trading-token|accessToken|refreshToken|passcode|otp|apiSecret)"?\s*[:=]\s*"?)'
                r'([^",\s}\]]+)', re.IGNORECASE), r"\1***"),
)
_STD_ATTRS = frozenset(vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None))) | {
    "message", "asctime", "taskName"}


# ------------------------------------------------------------------ bí mật ----

def register_secret(value: Optional[str]) -> bool:
    v = str(value or "").strip()
    if len(v) < MIN_SECRET_LEN:
        return False
    _SECRETS.add(v)
    return True


def register_secrets_from_env(env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    n = 0
    for name, value in env.items():
        up = name.upper()
        if up.endswith(_ENV_SUFFIX) and register_secret(value):
            n += 1
    return n


def scrub(text: Any) -> str:
    s = str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):     # dài trước: không để sót phần đuôi
        if secret in s:
            s = s.replace(secret, "<secret>")
    for rx, repl in _PATTERNS:
        s = rx.sub(repl, s)
    return s


def _sensitive(key: Any) -> bool:
    k = re.sub(r"[^a-z]", "", str(key).lower())
    return k in _SENSITIVE_EXACT or k.endswith(_SENSITIVE_SUFFIX)


def _plain(obj: Any, depth: int = 0) -> Any:
    """Đổi về kiểu JSON + che khoá nhạy cảm + xoá giá trị bí mật."""
    if depth > 8:
        return scrub(repr(obj))
    if isinstance(obj, Enum):                          # TRƯỚC str/int: Side(str, Enum) ⇒ "BUY", không "Side.BUY"
        return _plain(obj.value, depth + 1)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Path):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): (_MASK if _sensitive(k) else _plain(v, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v, depth + 1) for v in obj]
    return scrub(repr(obj))


# ------------------------------------------------------------------ định dạng ----

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = record.getMessage()
        except Exception:
            msg = f"{record.msg} {record.args}"
        out: dict = {
            "ts": datetime.fromtimestamp(record.created, VN_TZ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(msg),
        }
        for k, v in record.__dict__.items():
            if k in _STD_ATTRS or k.startswith("_") or k in out:
                continue
            out[k] = _MASK if _sensitive(k) else _plain(v)
        if record.exc_info and record.exc_info[0] is not None:
            etype, evalue, tb = record.exc_info
            out["exc_type"] = etype.__name__
            out["exc_msg"] = scrub(evalue)
            out["exc"] = scrub("".join(traceback.format_exception(etype, evalue, tb)))
        return json.dumps(out, ensure_ascii=False, default=lambda o: scrub(repr(o)))


class _VnNow:
    def now(self) -> datetime:
        return datetime.now(VN_TZ).replace(tzinfo=None)


class DailyJsonlHandler(logging.Handler):
    """Ghi JSONL vào file theo ngày giờ VN; mở chế độ append (khởi động lại giữa ngày ghi tiếp)."""

    _ft_json = True

    def __init__(self, log_dir: Path | str, prefix: str = "bot", clock=None, level: int = logging.NOTSET):
        super().__init__(level)
        from recorder.tick_recorder import assert_not_in_sync_folder
        self.log_dir = Path(log_dir)
        assert_not_in_sync_folder(self.log_dir)
        self.prefix = prefix
        self.clock = clock or _VnNow()
        self._day: Optional[date] = None
        self._fh = None
        self.setFormatter(JsonFormatter())

    def path_for(self, day: date) -> Path:
        return self.log_dir / f"{self.prefix}_{day.isoformat()}.jsonl"

    def _open(self, day: date):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        return open(self.path_for(day), "a", encoding="utf-8", newline="\n")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = self.clock.now().date()
            if day != self._day or self._fh is None:
                if self._fh is not None:
                    self._fh.close()
                    self._fh = None
                self._fh = self._open(day)
                self._day = day
            self._fh.write(self.format(record) + "\n")
            self._fh.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
        finally:
            super().close()


class _Console(logging.StreamHandler):
    _ft_json = True

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def setup_logging(log_dir: Path | str, *, prefix: str = "bot", level: int = logging.INFO, console: bool = True,
                  clock=None, env: Optional[Mapping[str, str]] = None) -> DailyJsonlHandler:
    """Cài logger gốc: file JSONL theo ngày + (tuỳ chọn) console dạng người đọc. Gọi lại an toàn."""
    register_secrets_from_env(env)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_ft_json", False):
            root.removeHandler(h)
            h.close()
    fh = DailyJsonlHandler(log_dir, prefix=prefix, clock=clock)
    root.addHandler(fh)
    if console:
        ch = _Console(sys.stderr)
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(ch)
    root.setLevel(level)
    return fh


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Một sự kiện có cấu trúc: `log_event(log, "fill", symbol=…, qty=…, price=…)`.

    Không bật mức log đó ⇒ không làm gì (backtest gọi hàng nghìn lần)."""
    if not logger.isEnabledFor(level):
        return
    extra = {"event": event}
    for k, v in fields.items():
        extra[f"f_{k}" if k in _STD_ATTRS or k == "event" else k] = v
    shown = " ".join(f"{k}={v.value if isinstance(v, Enum) else v}" for k, v in fields.items()
                     if not _sensitive(k))
    logger.log(level, "%s %s", event, shown, extra=extra)


def read_events(path: Path | str, event: Optional[str] = None) -> list[dict]:
    """Đọc lại file JSONL (bỏ dòng hỏng — vd dòng cuối bị cắt khi mất điện)."""
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and (event is None or d.get("event") == event):
                out.append(d)
    return out


def iter_files(log_dir: Path | str, prefix: str = "bot") -> Iterable[Path]:
    return sorted(Path(log_dir).glob(f"{prefix}_*.jsonl"))
