"""Test cho scripts/download_history.py (PROMPTS P1.6 bước 2) — viết TRƯỚC.

Theo đặc tả OpenAPI chính thức DNSE (`docs/dnseopenapi-en.yaml` của repo dnse-tech/dnse-py, broker_notes §1.3):
- `GET /price/ohlc?symbol&type=DERIVATIVE&resolution=1&from&to` — from/to epoch GIÂY; response dạng cột
  `{t, o, h, l, c, v, nextTime}` (`nextTime` = mốc nến kế tiếp, 0 nếu hết);
- `GET /price/{symbol}/trades?boardId=G1&from&to&limit` — `{trades: [...], nextPageToken}`, mới nhất trước,
  `time` dạng "YYYY-MM-DD HH:MM:SS" (theo ví dụ: giờ UTC — ❓, được KIỂM chứ không giả định).
Còn ❓ ⇒ tự dò, không đoán: cách ký (path / path+query), tham số phân trang trades (không dùng: chia đôi cửa sổ khi
đầy `limit`), múi giờ `time`.
"""
import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from scripts.download_history import (HistoryClient, HistoryError, bars_frame, download, fetch_ohlc_day,
                                      fetch_trades_day, parse_trade_times, plan_days, ticks_table, volume_gap)

KEY, SECRET = "hist-key", "hist-secret-very-private"
KRX = "41I1GB000"
DAY = date(2026, 11, 9)
VN = timezone(timedelta(hours=7))


def ep(h, m, s=0, d=DAY):
    return int(datetime(d.year, d.month, d.day, h, m, s, tzinfo=VN).timestamp())


def utc_str(h, m, s=0, d=DAY):
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=VN).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._b


class Server:
    """Server giả: ký path-only bị 401 nếu need_query_sign; 429 khi được lập trình; OHLC phân trang theo nextTime."""

    def __init__(self, bars=None, trades=None, need_query_sign=False, limit_cap=None):
        self.calls = []
        self.bars = bars or []                 # list (t, o, h, l, c, v)
        self.trades = trades or []             # list dict (time UTC str…)
        self.need_query_sign = need_query_sign
        self.fail_next = []
        self.page = 3                          # số nến mỗi trang OHLC
        self.max_limit = None                  # limit lớn hơn ⇒ 400 (giả lập server giới hạn limit)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if self.fail_next:
            st, body, h = self.fail_next.pop(0)
            return Resp(st, body, h)
        signed_query = "?" in url
        if self.need_query_sign and not signed_query:
            return Resp(401, {"status": 401, "code": "OA-001", "message": "invalid signature"})
        p = dict(params or {})
        if signed_query:
            from urllib.parse import parse_qsl, urlsplit
            p.update(dict(parse_qsl(urlsplit(url).query)))
        if "/price/ohlc" in url:
            f, t = int(p["from"]), int(p["to"])
            rows = [b for b in self.bars if f <= b[0] <= t][: self.page]
            rest = [b for b in self.bars if f <= b[0] <= t][self.page:]
            body = {k: [r[i] for r in rows] for i, k in enumerate("tohlcv")}
            body["nextTime"] = rest[0][0] if rest else 0
            return Resp(200, body)
        if "/trades" in url:
            f, t, lim = int(p["from"]), int(p["to"]), int(p.get("limit", 100))

            def ts(x):
                return int(datetime.strptime(x["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
            if self.max_limit is not None and lim > self.max_limit:
                return Resp(400, {"status": 400, "code": "BAD_REQUEST", "message": "limit too large"})
            # như server thật: MỚI NHẤT trước, kể cả trong cùng một giây (KL luỹ kế giảm dần — probe 09/11/2026)
            rows = sorted([x for x in self.trades if f <= ts(x) <= t],
                          key=lambda x: (ts(x), x.get("totalVolumeTraded") or 0), reverse=True)[:lim]
            return Resp(200, {"trades": rows, "nextPageToken": "tok" if len(rows) == lim else ""})
        return Resp(404, {"code": "OA-404", "message": "no route"})


def client(srv, **kw):
    return HistoryClient(KEY, SECRET, base_url="https://x.test", session=srv, sleep=kw.pop("sleep", lambda s: None),
                         min_interval_s=0, **kw)


def trade(h, m, s, price, qty, total, side="BUY"):
    return {"marketId": "DVX", "boardId": "G1", "symbol": KRX, "matchPrice": price, "matchQtty": qty, "side": side,
            "totalVolumeTraded": total, "time": utc_str(h, m, s)}


# --------------------------------------------------------------------------- HTTP ----

class TestClient:
    def test_signs_path_without_query_first(self):
        srv = Server(bars=[(ep(9, 0), 1350, 1351, 1349, 1350.5, 10)])
        c = client(srv)
        c.get("/price/ohlc", {"symbol": KRX, "type": "DERIVATIVE", "resolution": "1", "from": 1, "to": 2})
        h = srv.calls[0]["headers"]
        assert "?" not in srv.calls[0]["url"] and h["x-api-key"] == KEY and "X-Signature" in h
        assert c.sign_query is False

    def test_falls_back_to_query_signing_and_remembers(self):
        srv = Server(need_query_sign=True)
        c = client(srv)
        c.get("/price/ohlc", {"symbol": KRX, "type": "DERIVATIVE", "resolution": "1", "from": 1, "to": 2})
        c.get("/price/ohlc", {"symbol": KRX, "type": "DERIVATIVE", "resolution": "1", "from": 1, "to": 2})
        assert c.sign_query is True
        assert ["?" in x["url"] for x in srv.calls] == [False, True, True]     # chỉ thử kiểu cũ đúng một lần

    def test_429_respects_retry_after_then_gives_up(self):
        srv = Server()
        sleeps = []
        c = client(srv, sleep=sleeps.append, max_retries_429=2)
        srv.fail_next = [(429, {"message": "slow"}, {"Retry-After": "3"})]
        c.get("/price/ohlc", {"symbol": KRX, "type": "DERIVATIVE", "resolution": "1", "from": 1, "to": 2})
        assert sleeps == [3.0]
        srv.fail_next = [(429, {}, {})] * 3
        with pytest.raises(HistoryError, match="429"):
            c.get("/price/ohlc", {"symbol": KRX, "type": "DERIVATIVE", "resolution": "1", "from": 1, "to": 2})

    def test_other_error_raised_with_code(self):
        srv = Server()
        srv.fail_next = [(400, {"status": 400, "code": "OA-003", "message": "Thông tin nhập không hợp lệ"}, {})]
        with pytest.raises(HistoryError, match="OA-003"):
            client(srv).get("/price/ohlc", {"from": 1, "to": 2})

    def test_secret_never_in_error_or_repr(self):
        srv = Server()
        srv.fail_next = [(500, {"code": "OA-009", "message": "Lỗi hệ thống"}, {})]
        c = client(srv)
        with pytest.raises(HistoryError) as e:
            c.get("/price/ohlc", {"from": 1, "to": 2})
        assert SECRET not in str(e.value) and SECRET not in repr(c)


# --------------------------------------------------------------------------- OHLC ----

class TestOhlc:
    def test_columnar_parse_paging_and_vn_time(self):
        bars = [(ep(9, m), 1350 + m, 1351 + m, 1349 + m, 1350.5 + m, 10 + m) for m in range(7)]
        srv = Server(bars=bars)
        rows = fetch_ohlc_day(client(srv), KRX, DAY)
        assert len(rows) == 7 and rows[0]["start"] == datetime(2026, 11, 9, 9, 0) and rows[6]["volume"] == 16
        reqs = [x["params"] for x in srv.calls]
        assert reqs[0]["from"] == str(ep(8, 30)) and reqs[0]["to"] == str(ep(15, 0))
        assert reqs[0]["type"] == "DERIVATIVE" and reqs[0]["resolution"] == "1" and reqs[0]["symbol"] == KRX
        assert [r["from"] for r in reqs[1:]] == [str(ep(9, 3)), str(ep(9, 6))]   # theo nextTime
        assert len(srv.calls) == 3

    def test_dedupe_sort_and_bad_prices_dropped(self):
        bars = [(ep(9, 1), 1350, 1351, 1349, 1350, 5), (ep(9, 0), 1350, 1351, 1349, 1350, 4),
                (ep(9, 1), 1350, 1351, 1349, 1350, 5), (ep(9, 2), 13.5, 13.6, 13.4, 13.5, 1)]   # 13.5 điểm: sai đơn vị
        srv = Server(bars=bars)
        srv.page = 10
        rows = fetch_ohlc_day(client(srv), KRX, DAY)
        assert [r["start"].minute for r in rows] == [0, 1]

    def test_next_time_not_advancing_stops(self):
        srv = Server()
        srv.fail_next = [(200, {"t": [ep(9, 0)], "o": [1], "h": [1], "l": [1], "c": [1], "v": [1], "nextTime": ep(8, 0)}, {})]
        with pytest.raises(HistoryError, match="nextTime"):
            fetch_ohlc_day(client(srv), KRX, DAY)

    def test_next_time_equal_to_from_is_not_progress(self):
        srv = Server()
        bar = {"t": [ep(9, 0)], "o": [1350], "h": [1350], "l": [1350], "c": [1350], "v": [1]}
        srv.fail_next = [(200, {**bar, "nextTime": ep(8, 30)}, {})]          # = from của lần gọi đầu
        with pytest.raises(HistoryError, match="nextTime"):
            fetch_ohlc_day(client(srv), KRX, DAY)

    def test_next_time_beyond_window_stops_paging(self):
        srv = Server()
        bar = {"t": [ep(9, 0)], "o": [1350], "h": [1350], "l": [1350], "c": [1350], "v": [1]}
        srv.fail_next = [(200, {**bar, "nextTime": ep(15, 1)}, {}), (200, {**bar, "nextTime": 0}, {})]
        rows = fetch_ohlc_day(client(srv), KRX, DAY)
        assert len(rows) == 1 and len(srv.calls) == 1                         # không gọi trang ngoài cửa sổ ngày

    def test_unexpected_shape_raises(self):
        srv = Server()
        srv.fail_next = [(200, {"data": []}, {})]
        with pytest.raises(HistoryError, match="khuôn"):
            fetch_ohlc_day(client(srv), KRX, DAY)

    def test_bars_frame_matches_backtest_loader(self, tmp_path):
        from backtest.engine import load_bars_parquet
        rows = [{"start": datetime(2026, 11, 9, 9, 0), "open": 1350.0, "high": 1351.0, "low": 1349.0,
                 "close": 1350.5, "volume": 12}]
        p = tmp_path / "x.parquet"
        bars_frame(rows, "VN30F2611").to_parquet(p)
        bars, skipped = load_bars_parquet(p)
        assert skipped == 0 and bars[0].symbol == "VN30F2611" and bars[0].volume_reliable is True
        assert bars[0].interval_s == 60 and bars[0].close == 1350.5


# --------------------------------------------------------------------------- khớp lệnh ----

class TestTrades:
    def test_parse_utc_times_validated_against_window(self):
        rows = [{"time": utc_str(9, 0, 5)}, {"time": utc_str(10, 0, 0)}]
        assert parse_trade_times(rows, ep(8, 30), ep(15, 0)) == [datetime(2026, 11, 9, 9, 0, 5),
                                                                 datetime(2026, 11, 9, 10, 0, 0)]

    def test_vn_times_detected(self):
        rows = [{"time": "2026-11-09 09:00:05"}, {"time": "2026-11-09 14:40:00"}]   # đã là giờ VN
        assert parse_trade_times(rows, ep(8, 30), ep(15, 0))[1] == datetime(2026, 11, 9, 14, 40)

    def test_neither_timezone_fits_raises(self):
        with pytest.raises(HistoryError, match="múi giờ"):
            parse_trade_times([{"time": "2026-11-01 09:00:05"}], ep(8, 30), ep(15, 0))

    def test_bisects_saturated_windows_and_dedupes(self):
        trades = [trade(9, 0, s, 1350.0, 1, 100 + s) for s in range(0, 50)]
        srv = Server(trades=trades)
        rows = fetch_trades_day(client(srv), KRX, DAY, limit=8)
        assert len(rows) == 50 and len({r["totalVolumeTraded"] for r in rows}) == 50
        assert rows[0]["ts"] < rows[-1]["ts"]                                    # tăng dần thời gian
        assert all(int(c["params"]["limit"]) == 8 for c in srv.calls)

    def test_completeness_report(self):
        trades = [trade(9, 0, 1, 1350.0, 2, 2), trade(9, 0, 2, 1350.1, 1, 3), trade(9, 0, 3, 1350.2, 1, 9)]
        srv = Server(trades=trades)
        rows = fetch_trades_day(client(srv), KRX, DAY, limit=100)
        from scripts.download_history import volume_gap
        assert volume_gap(rows) == 5                                             # 9 − 2 − (1 + 1) = 5 HĐ bị thiếu

    def test_ticks_table_schema(self):
        from recorder.tick_recorder import SCHEMA
        rows = [{"ts": datetime(2026, 11, 9, 9, 0, 1), "matchPrice": 1350.0, "matchQtty": 2, "totalVolumeTraded": 2,
                 "side": "BUY"}]
        t = ticks_table(rows, "VN30F2611")
        assert t.schema == SCHEMA and t.column("source").to_pylist() == ["dnse_history"]
        assert t.column("last").to_pylist() == [1350.0] and t.column("last_qty").to_pylist() == [2]


# --------------------------------------------------------------------------- kế hoạch + tải ----

def test_plan_days_uses_trading_contract_and_skips_holidays(cal):
    plan = plan_days(date(2026, 11, 6), date(2026, 11, 10), cal, roll_days_before_expiry=1)
    assert [(d.isoformat(), n) for d, n, _ in plan] == [("2026-11-06", "VN30F2611"), ("2026-11-09", "VN30F2611"),
                                                         ("2026-11-10", "VN30F2611")]
    plan2 = plan_days(date(2026, 11, 19), date(2026, 11, 19), cal, roll_days_before_expiry=1, all_listed=True)
    # ngày đáo hạn 19/11: hợp đồng giao dịch (sau roll) = 2612 đứng đầu, kèm F1M 2611 + F1Q/F2Q
    assert [n for _, n, _ in plan2] == ["VN30F2612", "VN30F2611", "VN30F2703", "VN30F2706"]


def test_download_writes_skips_and_records_empty(tmp_path, cal):
    srv = Server(bars=[(ep(9, 0), 1350, 1351, 1349, 1350.5, 10)], trades=[trade(9, 0, 1, 1350.0, 1, 1)])
    srv.page = 10
    c = client(srv)
    plan = [(DAY, "VN30F2611", KRX), (date(2026, 11, 10), "VN30F2611", KRX)]     # 10/11: server không có dữ liệu
    s1 = download(c, plan, tmp_path, kinds=("ohlc", "trades"))
    assert (tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet").exists()
    assert (tmp_path / "trades" / "VN30F2611" / "2026-11-09.parquet").exists()
    assert not (tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-10.parquet").exists()
    assert s1["written"] == 2 and s1["empty"] == 2
    n = len(srv.calls)
    s2 = download(c, plan, tmp_path, kinds=("ohlc", "trades"))
    assert len(srv.calls) == n and s2["skipped"] == 4                           # chạy lại: không tải trùng
    s3 = download(c, plan, tmp_path, kinds=("ohlc",), force=True)
    assert len(srv.calls) > n and s3["written"] == 1
    df = pd.read_parquet(tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet")
    assert list(df["close"]) == [1350.5]


def test_download_error_on_one_day_does_not_stop_others(tmp_path):
    srv = Server(bars=[(ep(9, 0, d=date(2026, 11, 10)), 1350, 1351, 1349, 1350, 1)])
    srv.page = 10
    srv.fail_next = [(500, {"code": "OA-009", "message": "Lỗi hệ thống"}, {})]
    s = download(client(srv), [(DAY, "VN30F2611", KRX), (date(2026, 11, 10), "VN30F2611", KRX)], tmp_path,
                 kinds=("ohlc",))
    assert s["errors"] == 1 and s["written"] == 1
    assert (tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-10.parquet").exists()


# ------------------------------------------------------------------ định dạng thật (probe 09/11/2026) ----

def test_real_time_format_with_milliseconds_in_vn_time():
    """Server thật: `time` = "2026-11-06 10:04:58.705" — GIỜ VN, có mili-giây (cửa sổ hỏi 10:00–10:05 VN)."""
    d = date(2026, 11, 6)
    f = int(datetime(2026, 11, 6, 10, 0, tzinfo=timezone(timedelta(hours=7))).timestamp())
    t = f + 300
    rows = [{"time": "2026-11-06 10:04:58.705"}, {"time": "2026-11-06 10:04:16.809"}, {"time": "2026-11-06 10:04:30"}]
    ts = parse_trade_times(rows, f, t)
    assert ts == [datetime(2026, 11, 6, 10, 4, 58, 705000), datetime(2026, 11, 6, 10, 4, 16, 809000),
                  datetime(2026, 11, 6, 10, 4, 30)]


def test_garbage_time_still_rejected():
    with pytest.raises(HistoryError, match="thời gian lạ"):
        parse_trade_times([{"time": "10:04:58"}], 0, 10)


# ------------------------------------------------------------------ OHLC thật trả rỗng; nến dựng từ khớp lệnh ----

def test_ohlc_empty_page_with_next_time_equal_from_is_empty_not_error():
    """Server thật: {"t": [], …, "nextTime": <from>} cho cửa sổ không có nến ⇒ ngày rỗng, KHÔNG phải lỗi."""
    srv = Server()
    srv.fail_next = [(200, {"t": [], "o": [], "h": [], "l": [], "c": [], "v": [], "nextTime": ep(8, 30)}, {})]
    assert fetch_ohlc_day(client(srv), KRX, DAY) == [] and len(srv.calls) == 1


def test_default_kinds_is_trades_only():
    import inspect
    assert inspect.signature(download).parameters["kinds"].default == ("trades",)


def two_minute_trades(gap=False):
    """09:00: 1350 (2 HĐ), 1352 (1), 1349 (1); 09:01: 1351 (3). KL luỹ kế liên tục (gap ⇒ thiếu 1 lệnh)."""
    rows = [trade(9, 0, 5, 1350.0, 2, 2), trade(9, 0, 20, 1352.0, 1, 3), trade(9, 0, 50, 1349.0, 1, 4),
            trade(9, 1, 10, 1351.0, 3, 7 + (2 if gap else 0))]
    return rows


def test_trades_download_also_writes_bars_built_from_trades(tmp_path):
    from backtest.engine import load_bars_parquet
    srv = Server(trades=two_minute_trades())
    s = download(client(srv), [(DAY, "VN30F2611", KRX)], tmp_path)
    p = tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet"
    assert p.exists() and s["bars"] == 1
    bars, skipped = load_bars_parquet(p)
    assert skipped == 0
    assert [(b.start.minute, b.open, b.high, b.low, b.close, b.volume) for b in bars] == \
        [(0, 1350.0, 1352.0, 1349.0, 1349.0, 4), (1, 1351.0, 1351.0, 1351.0, 1351.0, 3)]
    assert all(b.volume_reliable for b in bars) and {b.symbol for b in bars} == {"VN30F2611"}
    assert pd.read_parquet(p)["source"].unique().tolist() == ["dnse_trades"]


def test_bars_volume_unreliable_when_trades_missing(tmp_path):
    from backtest.engine import load_bars_parquet
    srv = Server(trades=two_minute_trades(gap=True))
    download(client(srv), [(DAY, "VN30F2611", KRX)], tmp_path)
    bars, _ = load_bars_parquet(tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet")
    assert {b.start.minute: b.volume_reliable for b in bars} == {0: False, 1: False}  # thiếu giữa 09:00 và 09:01


def test_existing_trades_file_backfills_missing_bars_without_http(tmp_path):
    srv = Server(trades=two_minute_trades())
    c = client(srv)
    download(c, [(DAY, "VN30F2611", KRX)], tmp_path)
    p = tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet"
    p.unlink()
    n = len(srv.calls)
    s = download(c, [(DAY, "VN30F2611", KRX)], tmp_path)
    assert p.exists() and len(srv.calls) == n and s["bars"] == 1


def test_existing_bars_not_overwritten_unless_force(tmp_path):
    srv = Server(trades=two_minute_trades())
    c = client(srv)
    download(c, [(DAY, "VN30F2611", KRX)], tmp_path)
    p = tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet"
    mtime = p.stat().st_mtime_ns
    s = download(c, [(DAY, "VN30F2611", KRX)], tmp_path)
    assert p.stat().st_mtime_ns == mtime and s["bars"] == 0


# ------------------------------------------------------------------ giây khớp định kỳ ATO/ATC (probe 09/11/2026) ----

def auction_second(h, m, n, start_total, price=1350.0):
    """n lệnh 1 HĐ cùng giây (khớp định kỳ); KL luỹ kế start_total+1 … start_total+n."""
    return [trade(h, m, 0, price, 1, start_total + i + 1) for i in range(n)]


def test_saturated_second_retried_with_bigger_limit_gets_everything():
    rows = auction_second(9, 0, 600, 0) + [trade(9, 1, 10, 1351.0, 2, 602)]
    srv = Server(trades=rows)
    got = fetch_trades_day(client(srv), KRX, DAY, limit=500)
    assert len(got) == 601 and volume_gap(got) == 0
    assert any(c["params"].get("limit") == 2000 for c in srv.calls)
    assert not any(c["params"].get("limit") == 10000 for c in srv.calls)   # đủ ở 2000 ⇒ không xin thêm


def test_bigger_limit_rejected_keeps_partial_and_flags_only_that_minute(tmp_path):
    from backtest.engine import load_bars_parquet
    rows = (auction_second(9, 0, 600, 0) + [trade(9, 1, 10, 1351.0, 2, 602), trade(9, 2, 10, 1352.0, 1, 603)])
    srv = Server(trades=rows)
    srv.max_limit = 500                                            # server không cho limit lớn hơn
    download(client(srv), [(DAY, "VN30F2611", KRX)], tmp_path, trades_limit=500)
    bars, _ = load_bars_parquet(tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet")
    by_min = {b.start.minute: b.volume_reliable for b in bars}
    assert by_min == {0: False, 1: True, 2: True}                  # chỉ phút ATO thiếu lệnh


def test_gap_in_middle_flags_minute_of_the_trade_after_gap(tmp_path):
    from backtest.engine import load_bars_parquet
    rows = [trade(9, 0, 5, 1350.0, 1, 1), trade(9, 1, 5, 1351.0, 1, 2), trade(9, 2, 5, 1352.0, 1, 5),  # thiếu 2 HĐ
            trade(9, 3, 5, 1353.0, 1, 6)]
    srv = Server(trades=rows)
    download(client(srv), [(DAY, "VN30F2611", KRX)], tmp_path)
    bars, _ = load_bars_parquet(tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet")
    # lệnh thiếu nằm GIỮA 09:01 và 09:02 ⇒ đánh dấu cả hai phút (thận trọng)
    assert {b.start.minute: b.volume_reliable for b in bars} == {0: True, 1: False, 2: False, 3: True}


def test_first_trade_total_above_its_qty_flags_first_minute(tmp_path):
    """Cửa sổ cả ngày ⇒ lệnh đầu tiên phải có KL luỹ kế = KL của nó; lớn hơn ⇒ đã thiếu lệnh trước đó."""
    from backtest.engine import load_bars_parquet
    rows = [trade(9, 0, 5, 1350.0, 1, 40), trade(9, 1, 5, 1351.0, 1, 41)]
    srv = Server(trades=rows)
    download(client(srv), [(DAY, "VN30F2611", KRX)], tmp_path)
    bars, _ = load_bars_parquet(tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet")
    assert {b.start.minute: b.volume_reliable for b in bars} == {0: False, 1: True}


def test_trades_without_running_total_make_all_bars_unreliable(tmp_path):
    from backtest.engine import load_bars_parquet
    rows = [trade(9, 0, 5, 1350.0, 1, None), trade(9, 1, 5, 1351.0, 1, None)]
    srv = Server(trades=rows)
    download(client(srv), [(DAY, "VN30F2611", KRX)], tmp_path)
    bars, _ = load_bars_parquet(tmp_path / "ohlc1m" / "VN30F2611" / "2026-11-09.parquet")
    assert bars and not any(b.volume_reliable for b in bars)          # không kiểm được KL ⇒ không tin
    man = json.loads((tmp_path / "_manifest.json").read_text(encoding="utf-8"))
    assert man["ohlc1m/VN30F2611/2026-11-09"]["unreliable_minutes"] is None
