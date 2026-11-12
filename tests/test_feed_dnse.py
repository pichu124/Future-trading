"""Test cho feeds/base.py + feeds/data_feed_dnse.py (PROMPTS P1.2) — viết TRƯỚC khi implement.

Message mẫu ở tests/fixtures/dnse_stream.json dựng theo model SDK chính thức (chưa phải bản ghi
server thật). Kết nối mạng được thay bằng FakeTransport — test không cần mạng/khoá API.
"""
import asyncio
import hashlib
import hmac
import json
import logging
from datetime import date, datetime
from pathlib import Path

import pytest

from core.clock import FakeClock, MarketClock, SessionSchedule
from feeds.base import DataFeed
from feeds.data_feed_dnse import (ContractNotListed, DnseFeed, DnseNormalizer, FeedAuthError,
                                  epoch_to_vn, resolve_contract)

FIX = json.loads((Path(__file__).parent / "fixtures" / "dnse_stream.json").read_text(encoding="utf-8"))
KRX = "41I1GB000"           # VN30F2611
SYM = "VN30F2611"
NOW = datetime(2026, 11, 9, 11, 15, 0)
KEY, SECRET = "key-abc", "s3cr3t-very-private"


# Đăng ký theo SDK chính thức dnse-sdk-openapi 2.4.1: mỗi kênh một message {"action":"subscribe","channels":[…]};
# khớp lệnh `tick.<board>.json` trên DEFAULT_BOARDS, giá tốt nhất `top_price.<board>.json` trên G1…G7.
# VN30F nằm trên board G1 (✅ probe thật 11/11/2026: mọi message `boardId: "G1"`) ⇒ chỉ đăng ký G1.
TICK_BOARDS = ["G1"]
QUOTE_BOARDS = ["G1"]


def subs(*krx):
    syms = list(krx or [KRX])
    return ([{"action": "subscribe", "channels": [{"name": f"tick.{b}.json", "symbols": syms}]} for b in TICK_BOARDS]
            + [{"action": "subscribe", "channels": [{"name": f"top_price.{b}.json", "symbols": syms}]}
               for b in QUOTE_BOARDS])


def m(name, **over):
    d = dict(FIX[name])
    d.update(over)
    return d


# ------------------------------------------------------------------ thời gian ----

@pytest.mark.parametrize("raw", [1794197700, 1794197700_000, 1794197700_000_000, 1794197700_000_000_000])
def test_epoch_units_detected(raw):
    assert epoch_to_vn(raw) == NOW            # UTC 04:15 → 11:15 giờ VN


def test_epoch_rejects_garbage():
    with pytest.raises(ValueError):
        epoch_to_vn(12345)


# ------------------------------------------------------------- chuẩn hoá Tick ----

class TestNormalizer:
    def setup_method(self):
        self.n = DnseNormalizer({KRX})

    def test_quote(self):
        t = self.n.normalize(m("quote"), received=NOW)
        assert (t.symbol, t.bid, t.ask, t.bid_qty, t.ask_qty) == (SYM, 1352.3, 1352.5, 12, 7)
        assert t.last is None and t.last_qty is None and t.source == "dnse"
        assert t.ts == datetime(2026, 11, 9, 11, 15, 0, 123000)

    def test_trade_carries_latest_quote(self):
        self.n.normalize(m("quote"), received=NOW)
        t = self.n.normalize(m("trade"), received=NOW)
        assert (t.last, t.last_qty, t.bid, t.ask) == (1352.5, 3, 1352.3, 1352.5)

    def test_trade_extra_total_volume(self):
        t = self.n.normalize(m("trade_extra"), received=NOW)
        assert t.total_volume == 15234 and t.last_qty == 2

    def test_snake_case_and_s_key(self):
        t = self.n.normalize(m("quote_snake"), received=NOW)
        assert (t.bid, t.ask, t.bid_qty, t.ask_qty) == (1352.2, 1352.4, 4, 9)
        t2 = self.n.normalize(m("trade_s_key"), received=NOW)
        assert t2.symbol == SYM and t2.last == 1352.6

    def test_missing_timestamp_uses_receive_time(self):
        t = self.n.normalize(m("trade", timestamp=None), received=NOW)
        assert t.ts == NOW

    def test_ignores_other_symbols_and_types(self):
        assert self.n.normalize(m("other_symbol"), received=NOW) is None
        assert self.n.normalize(m("unknown"), received=NOW) is None
        assert self.n.normalize(m("secdef"), received=NOW) is None
        assert self.n.counts["ignored"] == 3

    def test_implausible_price_rejected_not_rescaled(self):
        """Không tự đoán đơn vị giá: ngoài khoảng hợp lý ⇒ bỏ + đếm, KHÔNG chia 1000."""
        assert self.n.normalize(m("bad_price"), received=NOW) is None
        assert self.n.counts["rejected_price"] == 1

    def test_quote_without_prices_ignored(self):
        assert self.n.normalize({"T": "q", "symbol": KRX, "timestamp": 1794197700}, received=NOW) is None


# -------------------------------------------------------------- chọn hợp đồng ----

class TestResolveContract:
    def test_front_month_checked_against_broker_list(self, cal):
        assert resolve_contract(date(2026, 11, 9), cal, 1, ["41I1GC000", KRX]) == (SYM, KRX)

    def test_roll_day_uses_next_contract(self, cal):
        assert resolve_contract(date(2026, 11, 19), cal, 1, ["41I1GC000"]) == ("VN30F2612", "41I1GC000")

    def test_missing_from_broker_list_refuses(self, cal):
        with pytest.raises(ContractNotListed):
            resolve_contract(date(2026, 11, 9), cal, 1, ["41I1GC000"])

    def test_empty_broker_list_refuses(self, cal):
        with pytest.raises(ContractNotListed):
            resolve_contract(date(2026, 11, 9), cal, 1, [])


# ------------------------------------------------------------------- feed ----

class Closed(Exception):
    """Giả lập WebSocket đóng."""


HANG = object()


class FakeTransport:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        if not self.incoming:
            await asyncio.sleep(3600)
        item = self.incoming.pop(0)
        if item is HANG:
            await asyncio.sleep(3600)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    async def close(self):
        self.closed = True


class Connector:
    def __init__(self, *transports):
        self.transports = list(transports)
        self.urls = []

    async def __call__(self, url):
        self.urls.append(url)
        if not self.transports:
            raise Closed("không còn kết nối giả")
        t = self.transports.pop(0)
        if isinstance(t, BaseException):
            raise t
        return t


def session(cal, session_cfg, clock):
    return MarketClock(SessionSchedule.from_mapping(session_cfg), cal, clock)


def make_feed(cal, session_cfg, connector, listed=(KRX,), **kw):
    clock = kw.pop("clock", FakeClock(NOW))
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        await asyncio.sleep(0)

    async def lister():
        return list(listed)

    feed = DnseFeed(KEY, SECRET, lister, session(cal, session_cfg, clock), connector=connector,
                    backoff_sleep=fake_sleep, epoch=lambda: 1794197700.5,
                    recv_timeout_s=kw.pop("recv_timeout_s", 0.05),
                    heartbeat_s=kw.pop("heartbeat_s", 3600), **kw)
    return feed, clock, sleeps


def stream(*names):
    return [FIX["welcome"], FIX["auth_ok"]] + [FIX[n] if isinstance(n, str) else n for n in names]


async def run_until(feed, ticks, n, timeout=2.0):
    task = asyncio.create_task(feed.run())
    try:
        for _ in range(int(timeout / 0.005)):
            if len(ticks) >= n or task.done():
                break
            await asyncio.sleep(0.005)
    finally:
        await feed.stop()
        await asyncio.wait_for(task, 1.0)


def test_is_data_feed(cal, session_cfg):
    feed, _, _ = make_feed(cal, session_cfg, Connector())
    assert isinstance(feed, DataFeed) and feed.name == "dnse"


def test_handshake_auth_subscribe_and_ticks(cal, session_cfg):
    tr = FakeTransport(stream("quote", "trade", "trade_extra"))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr))
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 3))
    assert [t.last for t in ticks] == [None, 1352.5, 1352.4]
    auth = tr.sent[0]
    assert auth["action"] == "auth" and auth["api_key"] == KEY
    # server thật 11/11/2026: nonce dạng SỐ ⇒ "authentication failed: nonce is required"; SDK 2.4.1 gửi CHUỖI
    assert isinstance(auth["nonce"], str) and auth["nonce"] == "1794197700500000"
    expected = hmac.new(SECRET.encode(), f"{KEY}:{auth['timestamp']}:{auth['nonce']}".encode(),
                        hashlib.sha256).hexdigest()
    assert auth["signature"] == expected and auth["timestamp"] == 1794197700
    assert tr.sent[1:1 + len(subs())] == subs()
    assert feed.symbol == SYM and feed.krx_symbol == KRX
    assert tr.closed


def test_url_uses_json_encoding(cal, session_cfg):
    con = Connector(FakeTransport(stream("trade")))
    feed, _, _ = make_feed(cal, session_cfg, con)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert con.urls[0] == "wss://ws-openapi.dnse.com.vn/v1/stream?encoding=json"


@pytest.mark.parametrize("err", ["auth_error", "real_auth_failed"])
def test_auth_error_is_fatal_no_retry(cal, session_cfg, err):
    con = Connector(FakeTransport([FIX["welcome"], FIX[err]]), FakeTransport(stream()))
    feed, _, sleeps = make_feed(cal, session_cfg, con)
    with pytest.raises(FeedAuthError):
        asyncio.run(feed.run())
    assert len(con.urls) == 1 and sleeps == []


def test_contract_not_listed_refuses_to_connect(cal, session_cfg):
    con = Connector(FakeTransport(stream("trade")))
    feed, _, _ = make_feed(cal, session_cfg, con, listed=("41I1GC000",))
    with pytest.raises(ContractNotListed):
        asyncio.run(feed.run())
    assert con.urls == []


def test_server_ping_gets_pong(cal, session_cfg):
    tr = FakeTransport(stream("ping", "trade"))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr))
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert {"action": "pong"} in tr.sent


def test_client_heartbeat_ping(cal, session_cfg):
    tr = FakeTransport(stream())
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr), heartbeat_s=0.01, recv_timeout_s=5)

    async def go():
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.05)
        await feed.stop()
        await asyncio.wait_for(task, 1.0)
    asyncio.run(go())
    assert {"action": "ping"} in tr.sent


def test_reconnect_with_backoff_and_resubscribe(cal, session_cfg):
    t1 = FakeTransport(stream("trade", Closed("rớt mạng")))
    t2 = FakeTransport(stream("trade_extra"))
    con = Connector(t1, Closed("DNS lỗi"), t2)
    feed, _, sleeps = make_feed(cal, session_cfg, con)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 2))
    assert [t.last for t in ticks] == [1352.5, 1352.4]
    assert sleeps == [1.0, 2.0]                         # backoff 1s rồi 2s
    assert t2.sent[1:1 + len(subs())] == subs()
    assert feed.stats()["disconnects"] == 2 and feed.stats()["connects"] == 2


def test_backoff_capped_and_reset_after_data(cal, session_cfg):
    con = Connector(*[Closed(f"lỗi {i}") for i in range(8)],
                    FakeTransport(stream("trade", Closed("x"))), FakeTransport(stream("trade_extra")))
    feed, _, sleeps = make_feed(cal, session_cfg, con, backoff_max_s=10)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 2))
    assert sleeps[:8] == [1, 2, 4, 8, 10, 10, 10, 10]
    assert sleeps[-1] == 1                              # có dữ liệu ⇒ backoff về lại 1s


def test_silent_connection_triggers_reconnect(cal, session_cfg):
    t1 = FakeTransport(stream(HANG))
    t2 = FakeTransport(stream("trade"))
    feed, _, sleeps = make_feed(cal, session_cfg, Connector(t1, t2), recv_timeout_s=0.02)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert len(ticks) == 1 and t1.closed and sleeps == [1.0]


def test_callback_error_does_not_kill_feed(cal, session_cfg, caplog):
    tr = FakeTransport(stream("trade", "trade_extra"))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr))
    got = []

    def cb(t):
        got.append(t)
        if len(got) == 1:
            raise RuntimeError("bug ở người nhận")
    feed.on_tick(cb)
    asyncio.run(run_until(feed, got, 2))
    assert len(got) == 2
    assert feed.stats()["callback_errors"] == 1


def test_stale_detection_only_in_continuous_session(cal, session_cfg):
    feed, clock, _ = make_feed(cal, session_cfg, Connector(), stale_seconds=5)
    feed._last_message_at = NOW
    clock.set(datetime(2026, 11, 9, 11, 15, 5))
    assert not feed.is_stale()
    clock.set(datetime(2026, 11, 9, 11, 15, 5, 1))
    assert feed.is_stale()
    clock.set(datetime(2026, 11, 9, 12, 0))             # nghỉ trưa: im lặng là bình thường
    assert not feed.is_stale()


def test_never_received_is_stale_in_session(cal, session_cfg):
    feed, _, _ = make_feed(cal, session_cfg, Connector())
    assert feed.is_stale()


def test_messages_per_minute_logged(cal, session_cfg, caplog):
    clock = FakeClock(NOW)
    tr = FakeTransport(stream("quote", "trade", "trade_extra"))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr), clock=clock)
    ticks = []

    def cb(t):
        ticks.append(t)
        if len(ticks) == 2:
            clock.set(datetime(2026, 11, 9, 11, 16, 1))   # sang phút mới trước message thứ 3
    feed.on_tick(cb)
    with caplog.at_level(logging.INFO, logger="feeds.data_feed_dnse"):
        asyncio.run(run_until(feed, ticks, 3))
    s = feed.stats()
    assert s["per_minute"][datetime(2026, 11, 9, 11, 15)] == 2 + 2   # welcome/auth + quote + trade
    assert any("msg/phút" in r.getMessage() for r in caplog.records)


def test_secrets_never_logged_or_repr(cal, session_cfg, caplog):
    tr = FakeTransport(stream("trade", Closed("x")))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr, FakeTransport(stream("trade_extra"))))
    ticks = []
    feed.on_tick(ticks.append)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(run_until(feed, ticks, 2))
    sig = tr.sent[0]["signature"]
    text = "\n".join(r.getMessage() for r in caplog.records) + repr(feed) + str(feed.stats())
    assert SECRET not in text and sig not in text


def test_real_websocket_transport_against_local_server(cal, session_cfg):
    """Đường kết nối thật (thư viện websockets) với một server giả lập DNSE chạy cục bộ."""
    websockets = pytest.importorskip("websockets")
    from feeds.data_feed_dnse import websockets_connector

    received = []

    async def handler(ws):
        await ws.send(json.dumps(FIX["welcome"]))
        received.append(json.loads(await ws.recv()))              # auth
        await ws.send(json.dumps(FIX["auth_ok"]))
        received.append(json.loads(await ws.recv()))              # subscribe
        for name in ("quote", "trade"):
            await ws.send(json.dumps(FIX[name]))
        await asyncio.sleep(1)

    async def go():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            async def lister():
                return [KRX]
            feed = DnseFeed(KEY, SECRET, lister, session(cal, session_cfg, FakeClock(NOW)),
                            url=f"ws://127.0.0.1:{port}/v1/stream?encoding=json",
                            connector=websockets_connector, recv_timeout_s=2)
            ticks = []
            feed.on_tick(ticks.append)
            await run_until(feed, ticks, 2)
            return ticks

    ticks = asyncio.run(go())
    assert [t.last for t in ticks] == [None, 1352.5] and ticks[1].bid == 1352.3
    assert received[0]["action"] == "auth" and received[1] == subs()[0]



# ------------------------------------------------- giao thức theo SDK chính thức 2.4.1 (11/11/2026) ----

def test_real_auth_failed_message_reported(cal, session_cfg):
    """Bản ghi THẬT: {"action":"error","code":"AUTH_FAILED",…} — trước đây bị coi là thành công ⇒ vòng lặp kết nối."""
    con = Connector(FakeTransport([FIX["real_welcome"], FIX["real_auth_failed"]]))
    feed, _, _ = make_feed(cal, session_cfg, con)
    with pytest.raises(FeedAuthError, match="nonce is required"):
        asyncio.run(feed.run())
    assert feed.stats()["connects"] == 0


def test_unexpected_auth_response_is_fatal(cal, session_cfg):
    con = Connector(FakeTransport([FIX["welcome"], {"action": "hello"}]))
    feed, _, sleeps = make_feed(cal, session_cfg, con)
    with pytest.raises(FeedAuthError, match="hello"):
        asyncio.run(feed.run())
    assert sleeps == []


def test_v2_quote_and_trade_flow(cal, session_cfg):
    tr = FakeTransport(stream("subscribed", "quote_v2", "trade_v2", "trade_extra_v2"))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr))
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 3))
    q, t, te = ticks
    assert (q.bid, q.bid_qty, q.ask, q.ask_qty, q.last) == (1352.3, 12, 1352.5, 7, None)
    assert (t.last, t.last_qty, t.total_volume, t.bid, t.ask) == (1352.5, 3, 15234, 1352.3, 1352.5)
    assert t.ts == datetime(2026, 11, 9, 11, 15, 0, 456000) and te.total_volume == 15236


def test_server_error_after_subscribe_is_counted_not_fatal(cal, session_cfg, caplog):
    tr = FakeTransport(stream("server_error", "trade_v2"))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr))
    ticks = []
    feed.on_tick(ticks.append)
    with caplog.at_level(logging.WARNING):
        asyncio.run(run_until(feed, ticks, 1))
    assert len(ticks) == 1 and feed.stats()["server_errors"] == 1
    assert "unknown channel" in caplog.text


class TestNormalizerV2:
    def setup_method(self):
        self.n = DnseNormalizer({KRX})

    def test_best_levels_from_unsorted_arrays_skip_zero(self):
        t = self.n.normalize(m("quote_v2"), received=NOW)
        assert (t.bid, t.bid_qty, t.ask, t.ask_qty) == (1352.3, 12, 1352.5, 7)

    def test_empty_book_side_kept_as_none(self):
        t = self.n.normalize(m("quote_v2", offer=[]), received=NOW)
        assert t.bid == 1352.3 and t.ask is None
        assert self.n.normalize(m("quote_v2", bid=[], offer=[]), received=NOW) is None

    @pytest.mark.parametrize("raw,expect", [
        ("2026-11-09 11:15:00.456", datetime(2026, 11, 9, 11, 15, 0, 456000)),      # giờ VN không múi giờ (như REST)
        ("2026-11-09 04:15:00.456", datetime(2026, 11, 9, 11, 15, 0, 456000)),      # UTC không múi giờ
        ("2026-11-09T04:15:00.456Z", datetime(2026, 11, 9, 11, 15, 0, 456000)),     # ISO UTC
        (1794197700456, datetime(2026, 11, 9, 11, 15, 0, 456000)),                  # epoch ms
        ({"Seconds": 1794197700, "Nanos": 456000000}, datetime(2026, 11, 9, 11, 15, 0, 456000)),
    ])
    def test_time_formats_checked_against_receive_time(self, raw, expect):
        t = self.n.normalize(m("trade_v2", time=raw), received=NOW)
        assert t.ts == expect

    def test_time_far_from_receive_uses_receive_and_counts(self):
        t = self.n.normalize(m("trade_v2", time="2026-11-08 11:15:00"), received=NOW)
        assert t.ts == NOW and self.n.counts["bad_timestamp"] == 1

    def test_contract_name_as_symbol_accepted(self):
        t = self.n.normalize(m("trade_v2", symbol="VN30F2611"), received=NOW)
        assert t.symbol == SYM and t.last == 1352.5

    def test_bad_price_rejected(self):
        assert self.n.normalize(m("trade_v2", matchPrice=1352500), received=NOW) is None
        assert self.n.counts["rejected_price"] == 1



# ------------------------------------------------- bản ghi THẬT từ server (probe 11/11/2026 09:34) ----

def test_real_messages_end_to_end(cal, session_cfg):
    clock = FakeClock(datetime(2026, 11, 11, 9, 34, 40))
    tr = FakeTransport([FIX["real_welcome"], FIX["real_auth_success"], FIX["real_ping"], FIX["real_subscribed"],
                        FIX["real_quote"], FIX["real_trade"]])
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr), clock=clock)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 2))
    q, t = ticks
    assert (q.symbol, q.bid, q.bid_qty, q.ask, q.ask_qty) == (SYM, 1853.4, 3, 1853.5, 3)
    assert q.ts == datetime(2026, 11, 11, 9, 34, 38, 120000)            # time = {Seconds, Nanos} (UTC epoch)
    assert (t.last, t.last_qty, t.total_volume, t.bid, t.ask) == (1853.5, 1, 33911, 1853.4, 1853.5)
    assert t.ts == datetime(2026, 11, 11, 9, 34, 38, 582000)
    assert {"action": "pong"} in tr.sent and feed.stats()["server_errors"] == 0
    assert tr.sent[1:] [:2] == subs()                                     # 2 kênh: tick.G1 + top_price.G1
