"""Test cho stream DE_ORDER của TCBS (`brokers/tcbs.py` TcbsOrderStream) — viết TRƯỚC.

Giao thức theo tài liệu chính thức 7.3 (wss://openapi.tcbs.com.vn/ws/nesoi):
- gửi `authenticate|base64({"jwt":token})`, server trả `pingTimeout|45` rồi `authenticate|{"success":true,...}`;
  PHẢI đợi auth thành công rồi mới `subscribe|base64({"topic":"DE_ORDER"})`;
- heartbeat là TEXT frame `ping|1` mỗi 2 giây (tắt ping RFC 6455 của thư viện);
- data `message_proto|DE_ORDER|{json}` — matchVolume cộng dồn; không số thứ tự, không phát lại ⇒ mỗi lần
  (re)connect đồng bộ lại REST; backoff min(2^n, 30)s, thất bại liên tiếp 10 lần ⇒ cảnh báo.
"""
import asyncio
import base64
import json

import pytest

from brokers.tcbs import TcbsOrderStream, tcbs_ws_connector
from core.models import OrderStatus, Side
from tests.test_broker_tcbs import KRX, SYM, make, order, ref_id_for

DOC_DE_ORDER = "subscribe|eyJ0b3BpYyI6IkRFX09SREVSIn0="                 # nguyên văn trong tài liệu
DOC_DE_POSITION = "subscribe|eyJ0b3BpYyI6IkRFX09QRU5fUE9TSVRJT04ifQ=="
AUTH_OK = 'authenticate|{"success":true,"error":null}'


def run(coro):
    return asyncio.run(coro)


class FakeWs:
    def __init__(self, frames, hold_s=0.0):
        self.frames = list(frames)
        self.sent: list[tuple[int, str]] = []       # (số frame đã nhận lúc gửi, nội dung)
        self.delivered = 0
        self.hold_s = hold_s
        self.closed = False

    async def send(self, m):
        self.sent.append((self.delivered, m))

    async def recv(self):
        if self.frames:
            self.delivered += 1
            return self.frames.pop(0)
        if self.hold_s:
            await asyncio.sleep(self.hold_s)
        raise ConnectionError("đóng")

    async def close(self):
        self.closed = True


def de_order(no, match=0, avg=None, status="P", code="2", vol=2):
    d = {"subAccount": "334455A", "orderNo": str(no), "pkOrderNo": "7861546", "side": "B", "symbol": KRX,
         "volume": vol, "showPrice": "1350.0", "status": status, "orderStatus": code, "matchVolume": match}
    if avg is not None:
        d["matchPriceBQ"] = avg
    return "message_proto|DE_ORDER|" + json.dumps(d)


def stream_for(b, sessions, **kw):
    """sessions: list các FakeWs lần lượt cho mỗi lần connect; hết ⇒ dừng."""
    seq = list(sessions)
    st = None

    async def connector(url):
        if not seq:
            st.stopping = True
            raise ConnectionError("hết phiên giả")
        return seq.pop(0)

    async def no_sleep(_):
        return None
    st = TcbsOrderStream(b.tokens, b, connector=connector, backoff_sleep=no_sleep, heartbeat_s=3600, **kw)
    return st


# --------------------------------------------------------------------------- giao thức ----

def test_auth_then_subscribe_exact_frames():
    b, srv, *_ = make()
    ws = FakeWs(["pingTimeout|45", AUTH_OK])
    run(stream_for(b, [ws]).run())
    sent = [m for _, m in ws.sent]
    assert sent[0].startswith("authenticate|")
    assert json.loads(base64.b64decode(sent[0].split("|", 1)[1])) == {"jwt": b.tokens._token}
    assert DOC_DE_ORDER in sent and DOC_DE_POSITION in sent
    # subscribe chỉ gửi SAU khi đã nhận được frame xác thực thành công (frame thứ 2)
    assert all(n >= 2 for n, m in ws.sent if m.startswith("subscribe|"))


def test_auth_payload_compact_like_doc():
    b, *_ = make()
    ws = FakeWs([AUTH_OK])
    run(stream_for(b, [ws]).run())
    raw = base64.b64decode(ws.sent[0][1].split("|", 1)[1]).decode()
    assert raw == json.dumps({"jwt": b.tokens._token}, separators=(",", ":"))


def test_text_heartbeat_sent():
    b, *_ = make()
    ws = FakeWs([AUTH_OK], hold_s=0.05)

    async def no_sleep(_):
        return None
    seq = [ws]

    async def connector(url):
        if not seq:
            st.stopping = True
            raise ConnectionError("x")
        return seq.pop(0)
    st = TcbsOrderStream(b.tokens, b, connector=connector, backoff_sleep=no_sleep, heartbeat_s=0.01)
    run(st.run())
    assert any(m == "ping|1" for _, m in ws.sent)


def test_auth_failure_invalidates_token_and_reconnects():
    b, srv, clock, wall, fills, updates, notes = make()
    bad = FakeWs(["pingTimeout|45", 'authenticate|{"success":false,"error":"token expired"}'])
    run(stream_for(b, [bad]).run())
    assert not b.tokens.trading_ready
    assert not any(m.startswith("subscribe|") for _, m in bad.sent)


def test_alert_after_repeated_failures_once():
    b, srv, clock, wall, fills, updates, notes = make()
    sessions = [FakeWs([]) for _ in range(12)]                       # đóng ngay, không auth
    run(stream_for(b, sessions, max_failures_alert=10).run())
    hits = [n for n in notes if "stream" in n.lower() and "TCBS" in n]
    assert len(hits) == 1 and hits[0].startswith("🚨")


def test_backoff_doubles_and_caps_at_30():
    b, *_ = make()
    waits = []

    async def rec_sleep(s):
        waits.append(s)
    seq = [FakeWs([]) for _ in range(7)]
    st = None

    async def connector(url):
        if not seq:
            st.stopping = True
            raise ConnectionError("x")
        return seq.pop(0)
    st = TcbsOrderStream(b.tokens, b, connector=connector, backoff_sleep=rec_sleep, heartbeat_s=3600)
    run(st.run())
    assert waits[:7] == [1, 2, 4, 8, 16, 30, 30]


# --------------------------------------------------------------------------- áp sự kiện ----

def test_de_order_applies_cumulative_fill_and_dedupes_with_rest():
    b, srv, clock, wall, fills, updates, _ = make()
    o = run(b.place(order(qty=2)))
    ws = FakeWs([AUTH_OK, de_order(o.broker_order_id, 1, 1350.0, "PM", "2"),
                 de_order(o.broker_order_id, 1, 1350.0, "PM", "2")])
    run(stream_for(b, [ws]).run())
    assert [(f.qty, f.price, f.side) for f in fills] == [(1, 1350.0, Side.BUY)]
    assert b.order(o.client_order_id).status is OrderStatus.PARTIALLY_FILLED
    srv.fill(srv.by_ref(ref_id_for(o.client_order_id)), 1, 1350.0)
    run(b.sync())                                                        # REST cùng mức ⇒ không đếm đôi
    assert len(fills) == 1


def test_stale_frame_does_not_regress_status():
    b, srv, clock, wall, fills, updates, _ = make()
    o = run(b.place(order(qty=2)))
    ws = FakeWs([AUTH_OK, de_order(o.broker_order_id, 1, 1350.0, "PM", "2"),
                 de_order(o.broker_order_id, 0, None, "P", "2")])        # bản tin cũ đến muộn
    run(stream_for(b, [ws]).run())
    assert b.order(o.client_order_id).status is OrderStatus.PARTIALLY_FILLED and len(fills) == 1


def test_unknown_order_requests_resync_and_ping_obeys():
    b, srv, clock, wall, fills, updates, _ = make()
    b.config = b.config.__class__(**{**b.config.__dict__, "poll_s": 3600})
    run(b.ping())                                                        # lần đọc đầu
    n0 = len([c for c in srv.calls if c["path"] == "/khronos/v1/order/in-day"])
    ext = srv.add(refId="APP-000000001", side="S")
    ws = FakeWs([AUTH_OK, de_order(ext["orderNo"], 0, None, "P", "2", vol=1)])
    st = stream_for(b, [ws])
    run(st.run())
    assert b.resync_requested
    run(b.ping())                                                        # chưa tới poll_s nhưng phải đọc lại
    assert len([c for c in srv.calls if c["path"] == "/khronos/v1/order/in-day"]) > n0
    assert not b.resync_requested


def test_each_connect_requests_rest_resync():
    b, *_ = make()
    b.resync_requested = False
    run(stream_for(b, [FakeWs([AUTH_OK])]).run())
    assert b.resync_requested


def test_open_position_frame_recorded():
    b, *_ = make()
    frame = "message_proto|DE_OPEN_POSITION|" + json.dumps({"sub_account": "334455A", "symbol": KRX, "side": "S",
                                                             "net": -1, "avg_remain": 1350.0})
    run(stream_for(b, [FakeWs([AUTH_OK, frame])]).run())
    assert b.stream_net == {SYM: -1}


def test_junk_frames_ignored():
    b, srv, clock, wall, fills, *_ = make()
    for f in ("message_proto|DE_ORDER|không phải json", "message_proto|DE_ORDER", "lạ", 123, None,
              'message_proto|DE_ORDER|{"orderNo": "999999"}'):
        b.on_stream_frame(f)
    assert fills == []


# --------------------------------------------------------------------------- connector ----

def test_connector_disables_rfc6455_ping(monkeypatch):
    seen = {}

    class WS:
        async def send(self, m):
            pass

        async def recv(self):
            return ""

        async def close(self):
            pass

    async def fake_connect(url, **kw):
        seen.update(kw, url=url)
        return WS()
    import websockets
    monkeypatch.setattr(websockets, "connect", fake_connect)
    run(tcbs_ws_connector("wss://openapi.tcbs.com.vn/ws/nesoi"))
    assert seen["ping_interval"] is None and seen["ping_timeout"] is None
    assert seen["url"] == "wss://openapi.tcbs.com.vn/ws/nesoi"


def test_session_that_authenticated_resets_backoff_even_if_it_drops():
    """Phiên đã xác thực rồi mới rớt ⇒ không phải 'thất bại liên tiếp': backoff về 1s, không cảnh báo."""
    b, srv, clock, wall, fills, updates, notes = make()
    waits = []

    async def rec_sleep(s):
        waits.append(s)
    seq = [FakeWs([AUTH_OK]) for _ in range(12)]
    st = None

    async def connector(url):
        if not seq:
            st.stopping = True
            raise ConnectionError("x")
        return seq.pop(0)
    st = TcbsOrderStream(b.tokens, b, connector=connector, backoff_sleep=rec_sleep, heartbeat_s=3600,
                         max_failures_alert=10)
    run(st.run())
    assert waits[:12] == [1] * 12 and not any("Stream lệnh TCBS" in n for n in notes)


def test_unknown_order_frame_alone_requests_resync():
    b, srv, *_ = make()
    b.resync_requested = False
    b.on_stream_frame(de_order("424242", 0, None, "P", "2", vol=1))     # DE_ORDER không có refId ⇒ không nhận ra
    assert b.resync_requested
