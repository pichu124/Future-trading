"""Test cho monitoring/logger.py — logger JSON có cấu trúc (CLAUDE.md mục 2 `monitoring/`) — viết TRƯỚC.

Yêu cầu:
- mỗi bản ghi = MỘT dòng JSON (JSONL), giờ Asia/Ho_Chi_Minh, file theo NGÀY (đổi file khi sang ngày);
- field có cấu trúc qua `extra=` / `log_event(...)`; kiểu lạ (datetime, Enum, dataclass) vẫn ghi được;
- KHÔNG BAO GIỜ ghi token / OTP / private key (CLAUDE.md mục 7): khoá nhạy cảm ⇒ "***", giá trị bí
  mật đã đăng ký (từ .env) bị thay trong cả message lẫn field lẫn traceback;
- lỗi ghi log không được làm chết bot; thư mục log không được nằm trong Dropbox (mục 3b);
- `read_events` đọc lại để dựng timeline sau phiên (prompt "Debug sau phiên").
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import pytest

from monitoring import logger as jl
from monitoring.logger import (DailyJsonlHandler, JsonFormatter, log_event, read_events, register_secret,
                               register_secrets_from_env, scrub, setup_logging)


@pytest.fixture(autouse=True)
def clean_registry():
    jl._SECRETS.clear()
    yield
    jl._SECRETS.clear()
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_ft_json", False):
            root.removeHandler(h)
            h.close()


def record(msg="xin chào %s", args=("bạn",), level=logging.INFO, name="test", exc=None, **extra):
    r = logging.LogRecord(name, level, __file__, 10, msg, args, exc)
    for k, v in extra.items():
        setattr(r, k, v)
    r.created = datetime(2026, 11, 9, 3, 0, 1, 500000, tzinfo=timezone.utc).timestamp()
    return r


# --------------------------------------------------------------------------- định dạng ----

class Side(Enum):
    BUY = "BUY"


@dataclass(frozen=True)
class Thing:
    a: int
    b: str


def test_str_enum_logged_as_value():
    from core.models import Side as RealSide
    assert json.loads(JsonFormatter().format(record(side=RealSide.SELL)))["side"] == "SELL"


def test_one_json_line_with_vn_time_and_fields():
    line = JsonFormatter().format(record(event="order", symbol="VN30F2611", qty=2, side=Side.BUY,
                                         thing=Thing(1, "x"), when=datetime(2026, 11, 9, 10, 0)))
    assert "\n" not in line
    d = json.loads(line)
    assert d["ts"] == "2026-11-09T10:00:01.500+07:00"
    assert d["level"] == "INFO" and d["logger"] == "test" and d["msg"] == "xin chào bạn"
    assert d["event"] == "order" and d["symbol"] == "VN30F2611" and d["qty"] == 2
    assert d["side"] == "BUY" and d["thing"] == {"a": 1, "b": "x"} and d["when"] == "2026-11-09T10:00:00"


def test_standard_record_attributes_not_dumped():
    d = json.loads(JsonFormatter().format(record()))
    for k in ("args", "msecs", "relativeCreated", "pathname", "processName", "levelno"):
        assert k not in d


def test_exception_included_as_type_message_traceback():
    try:
        raise ValueError("hỏng")
    except ValueError:
        import sys
        d = json.loads(JsonFormatter().format(record(exc=sys.exc_info())))
    assert d["exc_type"] == "ValueError" and d["exc_msg"] == "hỏng" and "Traceback" in d["exc"]


def test_unserializable_value_does_not_raise():
    class Weird:
        def __repr__(self):
            return "<weird>"
    d = json.loads(JsonFormatter().format(record(obj=Weird(), s={1, 2})))
    assert d["obj"] == "<weird>" and sorted(d["s"]) == [1, 2]


def test_bad_format_args_still_logged():
    d = json.loads(JsonFormatter().format(record(msg="%d lệnh", args=("không phải số",))))
    assert "lệnh" in d["msg"]


# --------------------------------------------------------------------------- bí mật ----

def test_sensitive_keys_masked_recursively():
    d = json.loads(JsonFormatter().format(record(
        payload={"apiKey": "k1", "apiSecret": "s1", "otp": "123456", "symbol": "41I1GB000",
                 "nested": [{"accessToken": "a", "refreshToken": "r", "price": 1}]},
        trading_token="t", passcode="p", headers={"Authorization": "Bearer abc", "X-Signature": "sig"})))
    p = d["payload"]
    assert p["apiSecret"] == p["otp"] == p["apiKey"] == "***" and p["symbol"] == "41I1GB000"
    assert p["nested"][0] == {"accessToken": "***", "refreshToken": "***", "price": 1}
    assert d["trading_token"] == d["passcode"] == "***"
    assert d["headers"] == {"Authorization": "***", "X-Signature": "***"}


def test_registered_secret_scrubbed_everywhere():
    register_secret("SUPERSECRET-123456")
    try:
        raise RuntimeError("401 với key SUPERSECRET-123456")
    except RuntimeError:
        import sys
        line = JsonFormatter().format(record(msg="gửi %s", args=("SUPERSECRET-123456",), exc=sys.exc_info(),
                                             note="x SUPERSECRET-123456 y"))
    assert "SUPERSECRET-123456" not in line and line.count("<secret>") >= 3


def test_patterns_scrubbed_without_registration():
    s = scrub('GET https://api.telegram.org/bot123456:ABCdef_ghi/sendMessage Authorization: Bearer eyJhbGciOi.x.y '
              '"tradingToken": "TT-abc"')
    assert "ABCdef_ghi" not in s and "eyJhbGciOi" not in s and "TT-abc" not in s


def test_short_values_not_registered():
    """Giá trị quá ngắn (vd '1') mà đăng ký sẽ xoá nhầm mọi chữ số trong log."""
    register_secret("1")
    register_secret("")
    assert scrub("lệnh 1 hợp đồng") == "lệnh 1 hợp đồng"


def test_register_from_env_by_name():
    n = register_secrets_from_env({"DNSE_API_SECRET": "dnse-secret-value", "SSI_API_KEY": "ssi-key-value",
                                   "TELEGRAM_TRADING_BOT_TOKEN": "123:tg-token-value",
                                   "SSI_PRIVATE_KEY_PATH": "/khoa/ssi.txt", "TRADING_MODE": "paper",
                                   "DNSE_ACCOUNT_NO": "0001234567"})
    assert n == 3
    out = scrub("dnse-secret-value ssi-key-value 123:tg-token-value paper 0001234567 /khoa/ssi.txt")
    assert out == "<secret> <secret> <secret> paper 0001234567 /khoa/ssi.txt"


# --------------------------------------------------------------------------- file theo ngày ----

class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t


def test_daily_file_rolls_over_at_vn_midnight(tmp_path):
    clock = Clock(datetime(2026, 11, 9, 23, 59, 59))
    h = DailyJsonlHandler(tmp_path, prefix="bot", clock=clock)
    h.setFormatter(JsonFormatter())
    h.emit(record(msg="a", args=()))
    clock.t = datetime(2026, 11, 10, 0, 0, 1)
    h.emit(record(msg="b", args=()))
    h.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bot_2026-11-09.jsonl", "bot_2026-11-10.jsonl"]
    assert [e["msg"] for e in read_events(tmp_path / "bot_2026-11-10.jsonl")] == ["b"]


def test_appends_across_restarts(tmp_path):
    clock = Clock(datetime(2026, 11, 9, 10, 0))
    for m in ("trước", "sau"):
        h = DailyJsonlHandler(tmp_path, prefix="bot", clock=clock)
        h.setFormatter(JsonFormatter())
        h.emit(record(msg=m, args=()))
        h.close()
    assert [e["msg"] for e in read_events(tmp_path / "bot_2026-11-09.jsonl")] == ["trước", "sau"]


def test_refuses_sync_folder(tmp_path):
    with pytest.raises(ValueError, match="đồng bộ"):
        DailyJsonlHandler(tmp_path / "Dropbox" / "logs", prefix="bot")


def test_write_error_does_not_raise(tmp_path, monkeypatch):
    h = DailyJsonlHandler(tmp_path, prefix="bot", clock=Clock(datetime(2026, 11, 9, 10, 0)))
    h.setFormatter(JsonFormatter())
    errors = []
    monkeypatch.setattr(h, "handleError", lambda r: errors.append(r))
    monkeypatch.setattr(h, "_open", lambda day: (_ for _ in ()).throw(OSError("đĩa đầy")))
    h.emit(record())                                                # không ném
    assert len(errors) == 1


# --------------------------------------------------------------------------- setup + log_event ----

def test_setup_logging_writes_json_and_is_idempotent(tmp_path):
    clock = Clock(datetime(2026, 11, 9, 10, 0))
    setup_logging(tmp_path, prefix="bot", console=False, clock=clock,
                  env={"DNSE_API_SECRET": "very-secret-dnse"})
    setup_logging(tmp_path, prefix="bot", console=False, clock=clock)  # gọi lại: không nhân đôi handler
    log = logging.getLogger("live.runner")
    log.info("đặt lệnh %s", "E1", extra={"event": "order", "client_order_id": "E1"})
    log_event(log, "fill", symbol="VN30F2611", qty=1, price=1350.5, secret_note="very-secret-dnse")
    logging.getLogger("noisy").debug("không ghi ở mức INFO")
    events = read_events(tmp_path / "bot_2026-11-09.jsonl")
    assert [e.get("event") for e in events] == ["order", "fill"]
    assert events[1]["price"] == 1350.5 and events[1]["secret_note"] == "<secret>"
    assert sum(1 for h in logging.getLogger().handlers if getattr(h, "_ft_json", False)) == 1


def test_read_events_filters_and_skips_bad_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"event": "fill", "qty": 1}\nkhông phải json\n{"event": "order"}\n\n', encoding="utf-8")
    assert read_events(p) == [{"event": "fill", "qty": 1}, {"event": "order"}]
    assert read_events(p, event="order") == [{"event": "order"}]


def test_log_event_level_and_message():
    recs = []

    class H(logging.Handler):
        def emit(self, r):
            recs.append(r)
    lg = logging.getLogger("t.event")
    lg.addHandler(H())
    lg.setLevel(logging.DEBUG)
    log_event(lg, "risk_reject", level=logging.WARNING, code="MAX_CONTRACTS")
    assert recs[0].levelno == logging.WARNING and recs[0].event == "risk_reject" and recs[0].code == "MAX_CONTRACTS"
    assert "risk_reject" in recs[0].getMessage()


# --------------------------------------------------------------------------- sự kiện từ bot ----

def test_execution_and_runner_emit_structured_events(session_cfg, cal, tmp_path, caplog):
    """Một lệnh vào + khớp qua runner paper ⇒ có các sự kiện signal / order / fill có field."""
    from tests.test_runner import Rig, T0
    from datetime import timedelta
    caplog.set_level(logging.INFO)
    r = Rig(session_cfg, cal, tmp_path, plan={"10:00": (1340.0,)})
    r.start()
    r.open_position()
    ev = {}
    for rec in caplog.records:
        e = getattr(rec, "event", None)
        if e:
            ev.setdefault(e, []).append(rec)
    assert {"signal", "order", "fill", "notify"} <= set(ev)
    f = ev["fill"][0]
    assert f.symbol == "VN30F2611" and f.qty == 1 and f.net_after == 1 and f.side.value == "BUY"
    assert any(o.tag == "entry" for o in ev["order"]) and any(o.tag == "stop" for o in ev["order"])
    line = JsonFormatter().format(f)
    d = json.loads(line)
    assert d["event"] == "fill" and d["side"] == "BUY" and d["price"] == f.price


def test_risk_rejection_has_code(session_cfg, cal, tmp_path, caplog):
    from tests.test_runner import Rig
    caplog.set_level(logging.INFO)
    r = Rig(session_cfg, cal, tmp_path, plan={"10:00": (1360.0,)})     # stop phía trên giá vào Long ⇒ sai
    r.start()
    from datetime import timedelta
    from tests.test_runner import T0
    r.at(T0 + timedelta(seconds=5))
    r.trade(1350.0)
    r.step()
    r.at(T0 + timedelta(seconds=60, milliseconds=600))
    r.step()
    rej = [x for x in caplog.records if getattr(x, "event", None) == "exec_rejected"]
    assert rej and rej[0].code is not None and rej[0].client_order_id
