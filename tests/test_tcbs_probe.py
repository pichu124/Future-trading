"""Test cho scripts/tcbs_trading_probe.py — chỉ đọc, không lộ bí mật."""
import asyncio
import json

from brokers.tcbs import TcbsHttp, TcbsTokenManager
from scripts.tcbs_trading_probe import Recorder, read_only, redact, sample_payloads, summarize
from tests.test_broker_tcbs import API_KEY, KRX, OTP, SYM, FakeTcbs, Wall, cfg, jwt


def test_redact():
    assert redact({"apiKey": "k", "otp": "1", "x": [{"token": "t"}], "symbol": "S"}) == \
        {"apiKey": "***", "otp": "***", "x": [{"token": "***"}], "symbol": "S"}


def test_read_only_never_places_and_hides_secrets(tmp_path):
    srv, wall = FakeTcbs(), Wall()
    srv.add(refId="APP-0000001")
    srv.positions = [{"symbol": KRX, "net": 1, "side": "L", "avg_remain": 1350.0}]
    orig = srv._route

    def route(method, path, params, body, headers):
        if path.startswith("/eros/"):
            return 200, {}, json.dumps({"bankSubAccounts": [{"accountNo": "105C334455A", "accountType": "DERIVATIVE"}]})
        return orig(method, path, params, body, headers)
    srv._route = route
    rec = Recorder(srv, tmp_path)
    http = TcbsHttp(transport=rec)
    tokens = TcbsTokenManager(http, API_KEY, now=wall)
    asyncio.run(read_only(tokens, http, cfg(), lambda: OTP))
    assert not [c for c in srv.calls if c["method"] == "POST" and "khronos" in c["path"]]
    blob = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json"))
    for s in (API_KEY, OTP, jwt(srv.token_exp)):
        assert s not in blob
    s = summarize(rec.records)
    assert KRX in s["symbols_seen"] and "2/P" in s["order_status_seen"]
    assert set(s["book_rows_by_pageNo"]) == {"0", "1", "2"}


def test_sample_payloads_offline():
    p = sample_payloads(cfg(), SYM)
    assert p[0]["body"]["symbol"] == KRX and p[1]["body"]["price"] == 1255.5 and p[1]["body"]["side"] == "S"
