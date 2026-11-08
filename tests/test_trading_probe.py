"""Test cho scripts/dnse_trading_probe.py — công cụ chốt các mục ❓ của adapter DNSE (P4.1)."""
import asyncio
import json
from datetime import datetime

from brokers.dnse import BASE_URL_UAT, DnseBroker, DnseHttp
from core.clock import FakeClock
from scripts.dnse_trading_probe import (RecordingTransport, main, order_test, redact, sample_payloads,
                                        summarize)
from test_broker_dnse import ACC, KEY, OTP, SECRET, SYM, TOKEN, FakeTransport, cfg


def test_redact_nested():
    assert redact({"a": 1, "passcode": "1", "x": [{"tradingToken": "t"}]}) == \
        {"a": 1, "passcode": "***", "x": [{"tradingToken": "***"}]}


def test_recording_strips_secrets_and_headers(tmp_path):
    rec = RecordingTransport(FakeTransport(), tmp_path)
    http = DnseHttp(KEY, SECRET, transport=rec)
    asyncio.run(http.request("POST", "/registration/trading-token",
                             json_body={"otpType": "email_otp", "passcode": OTP}))
    asyncio.run(http.request("GET", f"/accounts/{ACC}/orders", params={"marketType": "DERIVATIVE"}))
    blob = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json"))
    for secret in (SECRET, TOKEN, OTP, "X-Signature"):
        assert secret not in blob
    assert len(rec.records) == 2 and rec.records[1]["status"] == 200


def test_summarize_collects_status_values():
    recs = [{"method": "GET", "path": f"/accounts/{ACC}/orders", "status": 200,
             "body": {"orders": [{"id": 1, "orderStatus": "New"}, {"id": 2, "orderStatus": "Filled"}]}},
            {"method": "GET", "path": f"/accounts/{ACC}/orders/2", "status": 200,
             "body": {"id": 2, "orderStatus": "PendingCancel", "leaveQuantity": 0}},
            {"method": "GET", "path": f"/accounts/{ACC}/positions", "status": 200,
             "body": {"deals": [{"id": 9, "openQuantity": 1}]}}]
    s = summarize(recs)
    assert s["order_status_values"] == ["Filled", "New", "PendingCancel"]
    assert "leaveQuantity" in s["order_item_keys"] and "openQuantity" in s["deal_keys"]
    assert s["endpoints"][f"GET /accounts/{ACC}/orders"] == [200]


def test_sample_payloads_offline():
    p = sample_payloads(cfg(), SYM)
    assert p[0]["json"]["side"] == "NB" and p[1]["json"]["orderType"] == "MTL" and p[3]["method"] == "DELETE"


def test_order_test_place_modify_cancel_on_fake_uat(tmp_path):
    tr = FakeTransport()
    rec = RecordingTransport(tr, tmp_path)
    http = DnseHttp(KEY, SECRET, base_url=BASE_URL_UAT, transport=rec)
    b = DnseBroker(http, cfg(base_url=BASE_URL_UAT, api_verified=False), clock=FakeClock(datetime(2026, 11, 9, 10)),
                   live_flag=True, env={"TRADING_MODE": "live"})

    async def no_sleep(_):
        return None
    asyncio.run(order_test(b, SYM, 1250.0, with_modify=True, read_otp=lambda: OTP, sleep=no_sleep, polls=1))
    methods = [(r["method"], r["path"]) for r in rec.records]
    assert ("POST", "/accounts/orders") in methods
    assert any(m == "PUT" for m, _ in methods) and any(m == "DELETE" for m, _ in methods)
    assert "Cancelled" in summarize(rec.records)["order_status_values"]


def test_order_test_refused_outside_uat(capsys):
    assert main(["--order-test"]) == 2
    assert "UAT" in capsys.readouterr().out
