"""Test cho scripts/ssi_trading_probe.py (P4.2) — chỉ đọc, không đặt lệnh, không lộ bí mật."""
import asyncio
import json

from brokers.ssi import SsiHttp, SsiTokenManager
from scripts.ssi_trading_probe import EP_MASTER, Recorder, read_only, redact, sample_payloads, summarize
from test_broker_ssi import ACC, API_KEY, API_SECRET, KRX, OTP, SYM, FakeSsi, Wall, cfg


def test_redact():
    assert redact({"apiSecret": "s", "x": [{"otp": "1", "accessToken": "a"}]}) == \
        {"apiSecret": "***", "x": [{"otp": "***", "accessToken": "***"}]}


def test_read_only_flow_records_without_secrets(tmp_path):
    srv = FakeSsi()
    wall = Wall()
    srv.now = wall
    srv.add(clientRequestId="APP1")
    orig = srv._route

    def route(method, path, params, body, headers):
        if path == EP_MASTER:
            return 200, {}, json.dumps({"code": 200, "msg": "ok", "data": [{"symbol": KRX}, {"symbol": "SSI"}]})
        return orig(method, path, params, body, headers)
    srv._route = route
    rec = Recorder(srv, tmp_path)
    http = SsiHttp(transport=rec)
    tokens = SsiTokenManager(http, API_KEY, API_SECRET, now=wall)
    asyncio.run(read_only(tokens, http, cfg(), lambda: OTP))
    assert not [c for c in srv.calls if c["path"] == "/api/v3/trading/order"]      # không đặt lệnh
    blob = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json"))
    for s in (API_SECRET, OTP, "ACCESS-1", "REFRESH-1", "Bearer"):
        assert s not in blob
    s = summarize(rec.records)
    assert s["derivative_symbols_seen"] == [KRX] and s["order_status_values"] == ["QU"]
    assert s["wrapped_in_data"]["/api/v3/trading/orderBook"] is True


def test_sample_payloads_show_both_symbol_formats():
    p = sample_payloads(cfg(order_symbol_format=None, market_order_type=None, account_no=None), SYM)
    assert [x["body"]["symbol"] for x in p[:2]] == [KRX, SYM]
    assert p[2]["body"]["orderType"] == "MTL" and p[0]["body"]["accountNo"] == "<SSI_ACCOUNT>"
    assert ACC not in json.dumps(p)
