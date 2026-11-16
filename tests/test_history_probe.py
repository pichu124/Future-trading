"""Test cho scripts/dnse_history_probe.py (bước "gọi thử bằng tay" của PROMPTS P1.6) — viết TRƯỚC.

Script ký request REST đúng như SDK `dnse` 0.5.0, thử nhiều định dạng from/to cho
/price/{symbol}/trades và /price/ohlc, ghi response thô và tóm tắt để chốt các mục ❓.
"""
import base64
import hashlib
import hmac
import json
import urllib.parse
from datetime import date, datetime

import pytest

from scripts.dnse_history_probe import (build_signature_headers, run_probe, summarize, time_candidates)

KEY, SECRET = "key-abc", "s3cr3t-very-private"
DATE = "Sun, 08 Nov 2026 03:00:00 +0000"
NONCE = "0123456789abcdef0123456789abcdef"


def expected_sig(target: str) -> str:
    s = f"(request-target): {target}\ndate: {DATE}\nnonce: {NONCE}"
    raw = hmac.new(SECRET.encode(), s.encode(), hashlib.sha256).digest()
    return urllib.parse.quote(base64.b64encode(raw).decode(), safe="")


class TestSignature:
    def test_matches_sdk_algorithm_path_only(self):
        h = build_signature_headers("GET", "/price/41I1GB000/trades", KEY, SECRET, date_value=DATE, nonce=NONCE)
        assert h["x-api-key"] == KEY and h["date"] == DATE
        assert h["X-Signature"] == (
            f'Signature keyId="{KEY}",algorithm="hmac-sha256",headers="(request-target) date",'
            f'signature="{expected_sig("get /price/41I1GB000/trades")}",nonce="{NONCE}"')

    def test_query_variant_signs_full_target(self):
        h = build_signature_headers("GET", "/price/ohlc?symbol=X&type=DERIVATIVE", KEY, SECRET,
                                    date_value=DATE, nonce=NONCE)
        assert expected_sig("get /price/ohlc?symbol=X&type=DERIVATIVE") in h["X-Signature"]

    def test_secret_not_in_headers(self):
        h = build_signature_headers("GET", "/x", KEY, SECRET, date_value=DATE, nonce=NONCE)
        assert SECRET not in json.dumps(h)


def test_time_candidates_cover_units():
    start, end = datetime(2026, 11, 6, 10, 0), datetime(2026, 11, 6, 10, 5)     # giờ VN
    c = dict(time_candidates(start, end))
    assert c["epoch_s"] == ("1793934000", "1793934300")                          # 03:00 UTC
    assert c["epoch_ms"] == ("1793934000000", "1793934300000")
    assert c["iso_vn"] == ("2026-11-06T10:00:00", "2026-11-06T10:05:00")
    assert c["date"] == ("2026-11-06", "2026-11-06")


class TestSummarize:
    def test_list_of_trades(self):
        body = [{"symbol": "41I1GB000", "matchPrice": 1352.5, "matchQtty": 3, "time": "2026-11-06T03:00:01Z"},
                {"symbol": "41I1GB000", "matchPrice": 1352.6, "matchQtty": 1, "time": "2026-11-06T03:04:59Z"}]
        s = summarize(body)
        assert s["count"] == 2 and s["first_time"] == "2026-11-06T03:00:01Z"
        assert s["last_time"] == "2026-11-06T03:04:59Z" and "matchPrice" in s["keys"]

    @pytest.mark.parametrize("wrap", ["data", "items", "result"])
    def test_wrapped_list(self, wrap):
        s = summarize({wrap: [{"t": 1794193200, "o": 1}], "next": "abc"})
        assert s["count"] == 1 and s["container"] == wrap and s["first_time"] == 1794193200
        assert "next" in s["top_keys"]

    def test_columnar_ohlc(self):
        """Kiểu TradingView: {"t":[...],"o":[...],...}."""
        s = summarize({"t": [1, 2, 3], "o": [1, 1, 1], "c": [2, 2, 2]})
        assert s["count"] == 3 and s["container"] == "columnar" and s["first_time"] == 1

    def test_empty_and_error(self):
        assert summarize([])["count"] == 0
        assert summarize({"code": "BAD", "message": "x"})["count"] == 0


# --------------------------------------------------------------------------- chạy ----

class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """401 khi ký không kèm query cho /price/ohlc (giả lập server cần ký cả query); 429 một lần."""

    def __init__(self):
        self.calls = []
        self.rate_limited = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        sig = headers["X-Signature"]
        if "/price/ohlc" in url and "?" not in url:
            return Resp(401, {"message": "invalid signature"})
        if not self.rate_limited:
            self.rate_limited = True
            return Resp(429, {"message": "slow down"}, {"Retry-After": "2"})
        if "/trades" in url:
            return Resp(200, [{"time": "2026-11-06T03:00:01Z", "matchPrice": 1352.5}],
                        {"X-RateLimit-Remaining": "99"})
        return Resp(200, {"t": [1794193200], "c": [1352.5]})


def test_run_probe_records_raw_and_summary_without_secrets(tmp_path, cal, capsys):
    sess = FakeSession()
    sleeps = []
    out = run_probe(KEY, SECRET, out_dir=tmp_path, calendar=cal, today=date(2026, 11, 9),
                    session=sess, sleep=sleeps.append, months_back=(1,), base_url="https://x.test")
    assert sleeps == [2.0]                                   # tôn trọng Retry-After
    names = sorted(p.name for p in tmp_path.glob("*.json"))
    assert names and any("trades" in n for n in names) and any("ohlc" in n for n in names)
    blob = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json")) + capsys.readouterr().out
    assert SECRET not in blob and "X-Signature" not in blob and "signature=" not in blob
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    ohlc_ok = [r for r in summary if r["endpoint"] == "ohlc" and r["status"] == 200]
    assert ohlc_ok and all(r["sign_query"] for r in ohlc_ok)  # phát hiện cần ký cả query
    assert out == tmp_path / "summary.json"


def test_run_probe_uses_contract_active_on_each_day(tmp_path, cal):
    sess = FakeSession()
    sess.rate_limited = True
    run_probe(KEY, SECRET, out_dir=tmp_path, calendar=cal, today=date(2026, 11, 9), session=sess,
              sleep=lambda s: None, months_back=(1, 6), base_url="https://x.test")
    urls = {u for u, _, _ in sess.calls if "/trades" in u}
    assert any("41I1GB000" in u for u in urls)               # hôm nay: VN30F2611
    assert any("41I1GA000" in u for u in urls)               # ~1 tháng trước: VN30F2610 (đã đáo hạn)
    assert any("41I1G5000" in u for u in urls)               # ~6 tháng trước: VN30F2605
