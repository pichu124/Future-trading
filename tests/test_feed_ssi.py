"""Test cho feeds/data_feed_ssi.py (PROMPTS P1.3) — viết TRƯỚC khi implement.

Frame mẫu ở tests/fixtures/ssi_stream.json dựng theo mã nguồn SDK ssi-sdk 3.3.2 (chưa phải bản
ghi server thật). Kết nối mạng thay bằng transport giả.
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from core.clock import FakeClock, MarketClock, SessionSchedule
from feeds.base import DataFeed
from feeds.data_feed_dnse import ContractNotListed, FeedAuthError
from feeds.data_feed_ssi import (FeedSubscribeError, HandshakeRejected, SsiFeed, SsiNormalizer)

FIX = json.loads((Path(__file__).parent / "fixtures" / "ssi_stream.json").read_text(encoding="utf-8"))
KRX, SYM = "41I1GB000", "VN30F2611"
NOW = datetime(2026, 11, 9, 11, 15, 0)
TOKEN = "eyJ-very-secret-token"


def f(name, **data_over):
    d = json.loads(json.dumps(FIX[name]))
    d.get("data", {}).update(data_over)
    return d


# ------------------------------------------------------------- chuẩn hoá Tick ----

class TestNormalizer:
    def setup_method(self):
        self.n = SsiNormalizer({KRX})

    def test_quote_best_level(self):
        t = self.n.normalize(f("quote"), received=NOW)
        assert (t.symbol, t.bid, t.bid_qty, t.ask, t.ask_qty) == (SYM, 1352.3, 12, 1352.5, 7)
        assert t.last is None and t.source == "ssi"
        assert t.ts == datetime(2026, 11, 9, 11, 15, 0, 123000)

    def test_trade_with_total_volume_and_quote(self):
        self.n.normalize(f("quote"), received=NOW)
        t = self.n.normalize(f("trade"), received=NOW)
        assert (t.last, t.last_qty, t.total_volume, t.bid, t.ask) == (1352.5, 3, 15234, 1352.3, 1352.5)

    def test_string_numbers_and_hhmmss_time(self):
        t = self.n.normalize(f("trade_strings"), received=NOW)
        assert (t.last, t.last_qty, t.total_volume) == (1352.4, 2, 15236)
        assert t.ts == datetime(2026, 11, 9, 11, 15, 1)            # ngày lấy theo lúc nhận

    def test_iso_time(self):
        assert self.n.normalize(f("trade_iso"), received=NOW).ts == datetime(2026, 11, 9, 11, 15, 2)

    def test_unparseable_time_uses_receive_time(self):
        t = self.n.normalize(f("trade", t="hôm qua"), received=NOW)
        assert t.ts == NOW and self.n.counts["bad_timestamp"] == 1

    def test_ignores_candles_other_symbols_and_trading_channel(self):
        for name in ("candle", "other_symbol", "trading_event", "sub_ok", "pong"):
            assert self.n.normalize(f(name), received=NOW) is None, name
        assert self.n.counts["ignored"] == 5 and self.n.counts["rejected_price"] == 0

    def test_candle_with_price_field_still_ignored(self):
        assert self.n.normalize(f("candle", p=1352.5, q=1), received=NOW) is None

    def test_word_price_and_implausible_price_rejected(self):
        assert self.n.normalize(f("trade_ato"), received=NOW) is None
        assert self.n.normalize(f("bad_price"), received=NOW) is None
        assert self.n.counts["rejected_price"] == 2

    def test_empty_quote_ignored(self):
        assert self.n.normalize(f("quote", bids=[], asks=[]), received=NOW) is None


# ------------------------------------------------------------------- feed ----

class Closed(Exception):
    pass


HANG = object()


class FakeTransport:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
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
    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    async def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if not self.items:
            raise Closed("hết kết nối giả")
        it = self.items.pop(0)
        if isinstance(it, BaseException):
            raise it
        return it


class Tokens:
    def __init__(self):
        self.calls = []

    async def __call__(self, force_refresh):
        self.calls.append(force_refresh)
        return TOKEN + ("-new" if force_refresh else "")


def make_feed(cal, session_cfg, connector, listed=(KRX,), **kw):
    clock = kw.pop("clock", FakeClock(NOW))
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        await asyncio.sleep(0)

    async def lister():
        return list(listed)

    tokens = kw.pop("tokens", Tokens())
    mclock = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, clock)
    feed = SsiFeed(tokens, lister, mclock, connector=connector, backoff_sleep=fake_sleep,
                   jitter=lambda: 1.0, recv_timeout_s=kw.pop("recv_timeout_s", 0.05),
                   heartbeat_s=kw.pop("heartbeat_s", 3600), **kw)
    return feed, tokens, sleeps


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


def frames(*names):
    return [FIX[n] if isinstance(n, str) else n for n in names]


def test_is_data_feed(cal, session_cfg):
    feed, _, _ = make_feed(cal, session_cfg, Connector())
    assert isinstance(feed, DataFeed) and feed.name == "ssi"


def test_bearer_header_subscribe_and_ticks(cal, session_cfg):
    tr = FakeTransport(frames("sub_ok", "quote", "trade"))
    con = Connector(tr)
    feed, tokens, _ = make_feed(cal, session_cfg, con)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 2))
    url, headers = con.calls[0]
    assert url == "wss://stream.ssi.com.vn/ws/v3"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert tokens.calls[0] is False
    assert tr.sent[0] == {"method": "subscribe", "channel": "DATA",
                          "topics": [f"trade.{KRX}", f"quote.{KRX}"]}
    assert [t.last for t in ticks] == [None, 1352.5]
    assert feed.symbol == SYM


def test_handshake_401_refreshes_token_once_then_fatal(cal, session_cfg):
    con = Connector(HandshakeRejected(401), HandshakeRejected(401), FakeTransport(frames("trade")))
    feed, tokens, sleeps = make_feed(cal, session_cfg, con)
    with pytest.raises(FeedAuthError):
        asyncio.run(feed.run())
    assert tokens.calls == [False, True] and sleeps == []


def test_handshake_401_then_success(cal, session_cfg):
    tr = FakeTransport(frames("trade"))
    con = Connector(HandshakeRejected(403), tr)
    feed, tokens, sleeps = make_feed(cal, session_cfg, con)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert tokens.calls[:2] == [False, True] and con.calls[1][1]["Authorization"].endswith("-new")
    assert sleeps == []                                   # đổi token: không chờ backoff


def test_rejection_frame_auth(cal, session_cfg):
    con = Connector(FakeTransport(frames("reject_auth", Closed("đóng"))),
                    FakeTransport(frames("reject_auth", Closed("đóng"))))
    feed, tokens, _ = make_feed(cal, session_cfg, con)
    with pytest.raises(FeedAuthError):
        asyncio.run(feed.run())
    assert tokens.calls == [False, True]


def test_rate_limited_waits_retry_after(cal, session_cfg):
    con = Connector(FakeTransport(frames("reject_rate", Closed("đóng"))), FakeTransport(frames("trade")))
    feed, _, sleeps = make_feed(cal, session_cfg, con)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert sleeps == [45.0]


def test_rate_limited_handshake_waits_at_least_30s(cal, session_cfg):
    con = Connector(HandshakeRejected(429), FakeTransport(frames("trade")))
    feed, _, sleeps = make_feed(cal, session_cfg, con)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert sleeps == [30.0]


def test_subscribe_error_is_fatal(cal, session_cfg):
    feed, _, _ = make_feed(cal, session_cfg, Connector(FakeTransport(frames("sub_error"))))
    with pytest.raises(FeedSubscribeError):
        asyncio.run(feed.run())


def test_contract_not_listed(cal, session_cfg):
    con = Connector(FakeTransport(frames("trade")))
    feed, _, _ = make_feed(cal, session_cfg, con, listed=("41I1GC000",))
    with pytest.raises(ContractNotListed):
        asyncio.run(feed.run())
    assert con.calls == []


def test_reconnect_backoff_resubscribe(cal, session_cfg):
    t1 = FakeTransport(frames("trade", Closed("rớt")))
    t2 = FakeTransport(frames("quote"))
    con = Connector(t1, Closed("DNS"), t2)
    feed, _, sleeps = make_feed(cal, session_cfg, con)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 2))
    assert sleeps == [1.0, 2.0]
    assert t2.sent[0]["method"] == "subscribe"
    assert feed.stats()["connects"] == 2


def test_silent_connection_reconnects(cal, session_cfg):
    t1 = FakeTransport(frames(HANG))
    t2 = FakeTransport(frames("trade"))
    feed, _, sleeps = make_feed(cal, session_cfg, Connector(t1, t2), recv_timeout_s=0.02)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert len(ticks) == 1 and t1.closed and sleeps == [1.0]


def test_heartbeat(cal, session_cfg):
    tr = FakeTransport(frames())
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr), heartbeat_s=0.01, recv_timeout_s=5)

    async def go():
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.05)
        await feed.stop()
        await asyncio.wait_for(task, 1)
    asyncio.run(go())
    assert {"method": "ping_pong", "channel": "HEARTBEAT", "topics": []} in tr.sent


def test_stale_only_in_continuous(cal, session_cfg):
    clock = FakeClock(NOW)
    feed, _, _ = make_feed(cal, session_cfg, Connector(), clock=clock, stale_seconds=5)
    assert feed.is_stale()
    feed._last_message_at = NOW
    clock.set(datetime(2026, 11, 9, 11, 15, 6))
    assert feed.is_stale()
    clock.set(datetime(2026, 11, 9, 12, 0))
    assert not feed.is_stale()


def test_token_never_logged(cal, session_cfg, caplog):
    tr = FakeTransport(frames("trade", Closed("x")))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr, FakeTransport(frames("quote"))))
    ticks = []
    feed.on_tick(ticks.append)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(run_until(feed, ticks, 2))
    text = "\n".join(r.getMessage() for r in caplog.records) + repr(feed) + str(feed.stats())
    assert TOKEN not in text


def test_token_expiry_later_in_day_is_not_fatal(cal, session_cfg):
    """401 lúc sáng (đã đổi token) rồi chạy tốt; token hết hạn giữa phiên ⇒ đổi token tiếp, không dừng."""
    con = Connector(HandshakeRejected(401),
                    FakeTransport(frames("trade", "reject_auth", Closed("đóng"))),
                    FakeTransport(frames("quote")))
    feed, tokens, _ = make_feed(cal, session_cfg, con)
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 2))
    assert len(ticks) == 2
    assert tokens.calls[:3] == [False, True, True]


def test_backoff_jitter_and_cap(cal, session_cfg):
    feed, _, _ = make_feed(cal, session_cfg, Connector())
    feed._jitter = lambda: 0.0
    assert feed._backoff(0) == pytest.approx(0.5)        # jitter ⇒ 50%…100% của mức lũy thừa
    feed._jitter = lambda: 1.0
    assert feed._backoff(3) == pytest.approx(8.0)
    assert feed._backoff(20) == pytest.approx(60.0)      # chặn trên 60 giây


def test_real_websocket_bearer_header_and_401_mapping():
    """Đường kết nối thật (websockets): header Bearer tới server; bắt tay 401 ⇒ HandshakeRejected(401)."""
    websockets = pytest.importorskip("websockets")
    from websockets.datastructures import Headers
    from websockets.http11 import Response
    from feeds.data_feed_ssi import websockets_connector

    seen = []

    def process_request(connection, request):
        auth = request.headers.get("Authorization", "")
        seen.append(auth)
        if auth != f"Bearer {TOKEN}":
            return Response(401, "Unauthorized", Headers(), b"no")
        return None

    async def handler(ws):
        await ws.send(json.dumps(FIX["trade"]))
        await asyncio.sleep(0.5)

    async def go():
        async with websockets.serve(handler, "127.0.0.1", 0, process_request=process_request) as srv:
            url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/ws/v3"
            tr = await websockets_connector(url, {"Authorization": f"Bearer {TOKEN}"})
            frame = json.loads(await tr.recv())
            await tr.close()
            with pytest.raises(HandshakeRejected) as exc:
                await websockets_connector(url, {"Authorization": "Bearer wrong"})
            return frame, exc.value.status

    frame, status = asyncio.run(go())
    assert frame["topic"] == f"trade.{KRX}" and status == 401
    assert seen == [f"Bearer {TOKEN}", "Bearer wrong"]
