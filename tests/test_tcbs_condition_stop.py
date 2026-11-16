"""Test cho stop ĐIỀU KIỆN phía broker của TCBS (SOU/SOL) — viết TRƯỚC. Cờ `broker_side_stops` (mặc định TẮT).

Tài liệu 6.5/6.7/6.9/6.11/7.3 (docs/broker_notes.md §2b):
- đặt: POST /khronos/v1/order/condition/place {accountId, subAccountId, side, symbol, price, volume,
  orderType SOU (giá ≤ soPrice) | SOL (giá ≥ soPrice), soPrice, refId}; giá lệnh sinh ra = sàn (bán) / trần (mua);
- sổ: GET /khronos/v1/order/condition/detail — status 0 chờ / 1 đã kích hoạt / 2 đã huỷ / 3 từ chối; KHÔNG có refId
  và KHÔNG có số lệnh đã kích hoạt ⇒ liên kết qua stream DE_CONDITIONAL_ORDER.details, hoặc lệnh MỚI cùng mã/chiều/KL;
- huỷ: POST …/condition/cancel {accountId, subAccountId, orderNo = pk_orderNo}; sửa giá kích hoạt:
  POST /khronos/v2/order/condition/change {accountId, pkOrderNo, refId, soPrice, type "", cmd} ⇒ pkOrderNo MỚI.
Kích hoạt mà chưa liên kết được ⇒ stop vẫn PENDING (không coi là huỷ ⇒ không đóng hai lần).
"""
import asyncio
import json
from datetime import timedelta

import pytest

from brokers.base import BrokerError, LiveTradingNotAllowed
from core.models import OrderStatus, OrderType, Side, Tick
from tests.test_broker_tcbs import CUSTODY, KRX, SUB, SYM, T0, FakeTcbs, cfg, make, order, ref_id_for

EP_CPLACE = "/khronos/v1/order/condition/place"
EP_CBOOK = "/khronos/v1/order/condition/detail"
EP_CCANCEL = "/khronos/v1/order/condition/cancel"
EP_CCHANGE = "/khronos/v2/order/condition/change"


def run(coro):
    return asyncio.run(coro)


class CondTcbs(FakeTcbs):
    def __init__(self):
        super().__init__()
        self.conds = []
        self.cpk = 900000
        self.cond_cancel_final = True

    def add_cond(self, **kw):
        self.cpk += 1
        c = {"orderNo": str(len(self.conds) + 1), "pk_orderNo": str(self.cpk), "side": "S", "symbol": KRX,
             "showPrice": str(self.floor), "volume": "1", "soPrice": "1340.0", "orderType": "SU", "status": "0",
             "notes": "Stop order"}
        c.update(kw)
        self.conds.append(c)
        return c

    def trigger(self, c):
        c["status"] = "1"
        o = self.add(refId="", side=c["side"], symbol=c["symbol"], volume=int(c["volume"]),
                     showPrice=c["showPrice"], orderStatus="2", source="SYS")
        c["_details"] = o["orderNo"]
        return o

    def _route(self, method, path, params, b, headers):
        if path in (EP_CPLACE, EP_CBOOK, EP_CCANCEL, EP_CCHANGE) and \
                headers.get("Authorization") is None:
            return 401, {}, json.dumps({"code": "401"})
        if path == EP_CPLACE:
            c = self.add_cond(side=b["side"], symbol=b["symbol"], showPrice=str(b["price"]), volume=str(b["volume"]),
                              soPrice=str(b["soPrice"]), orderType="SU" if b["orderType"] == "SOU" else "SL")
            return 200, {}, json.dumps({"cmd": "Web.newOrder", "rc": "1", "data": [
                {"orderNo": int(c["orderNo"]), "pk_orderNo": c["pk_orderNo"], "status": "P"}]})
        if path == EP_CBOOK:
            page = int(params["pageNo"])
            rows = [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.conds] if page == 1 else []
            return 200, {}, json.dumps({"cmd": "Web.Order.ConditionOrder", "rc": "1", "data": rows})
        if path == EP_CCANCEL:
            c = next((x for x in self.conds if x["pk_orderNo"] == b["orderNo"]), None)
            if c is None or c["status"] != "0":
                return 400, {}, json.dumps({"status": 400, "code": "-6004", "message": "không tồn tại/đã xử lý"})
            if self.cond_cancel_final:
                c["status"] = "2"
            return 200, {}, json.dumps({"cmd": "Web.cancelActivatedOrder", "rc": "1", "data": []})
        if path == EP_CCHANGE:
            c = next(x for x in self.conds if x["pk_orderNo"] == b["pkOrderNo"])
            self.cpk += 1
            c["pk_orderNo"], c["soPrice"] = str(self.cpk), str(b["soPrice"])
            return 200, {}, json.dumps({"rc": "1", "data": [{"showPrice": str(b["soPrice"]), "pkOrderNo": c["pk_orderNo"],
                                                             "notes": "Stop order"}]})
        return super()._route(method, path, params, b, headers)


def bmake(**kw):
    return make(CondTcbs(), config=cfg(broker_side_stops=True), **kw)


def stop(cid="S1000001", side=Side.SELL, trigger=1340.0, qty=1):
    return order(cid, side=side, otype=OrderType.STOP, price=trigger, qty=qty, tag="stop")


def cposts(srv, path=EP_CPLACE):
    return [c for c in srv.calls if c["path"] == path]


# --------------------------------------------------------------------------- đặt ----

def test_flag_off_keeps_bot_held_stop():
    b, srv, *_ = make(CondTcbs())
    s = run(b.place(stop()))
    assert s.broker_order_id.startswith("local") and not cposts(srv)


def test_sell_stop_is_sou_at_floor_buy_stop_is_sol_at_ceiling():
    b, srv, *_ = bmake()
    s = run(b.place(stop()))
    p = cposts(srv)[0]["body"]
    assert p == {"accountId": CUSTODY, "subAccountId": SUB, "side": "S", "symbol": KRX, "price": srv.floor,
                 "volume": 1, "orderType": "SOU", "soPrice": 1340.0, "refId": ref_id_for("S1000001")}
    assert s.status is OrderStatus.PENDING and s.broker_order_id == f"cond:{srv.conds[0]['pk_orderNo']}"
    run(b.place(stop("S1000002", side=Side.BUY, trigger=1360.0)))
    p2 = cposts(srv)[1]["body"]
    assert p2["orderType"] == "SOL" and p2["price"] == srv.ceil and p2["side"] == "B"


def test_broker_stop_not_triggered_by_bot_ticks():
    b, srv, *_ = bmake()
    run(b.place(stop()))
    run(b.on_tick(Tick(SYM, T0, last=1300.0)))
    assert not [c for c in srv.calls if c["path"] == "/khronos/v1/order/place"]   # sàn/TCBS tự kích hoạt


def test_guarded_like_any_order():
    b, srv, *_ = bmake(env={})
    with pytest.raises(LiveTradingNotAllowed):
        run(b.place(stop()))
    assert not cposts(srv)


def test_business_reject():
    b, srv, *_ = bmake()
    srv.fail_next = [(400, {"status": 400, "code": "-6035", "message": "Giá ngoài biên độ"})]
    assert run(b.place(stop())).status is OrderStatus.REJECTED


def test_uncertain_place_found_in_condition_book():
    b, srv, clock, *_ = bmake()
    run(b._band(SYM))                                                   # trần/sàn đã có trong cache
    srv.fail_next = [TimeoutError("t")]
    s = run(b.place(stop()))
    assert s.status is OrderStatus.PENDING and s.broker_order_id is None
    srv.add_cond(soPrice="1340.0", volume="1", side="S")              # thực ra đã tới TCBS
    run(b.sync())
    assert b.order("S1000001").broker_order_id == f"cond:{srv.conds[0]['pk_orderNo']}"
    assert len(cposts(srv)) == 1


def test_uncertain_place_not_found_rejected():
    b, srv, clock, *_ = bmake()
    run(b._band(SYM))
    srv.fail_next = [TimeoutError("t")]
    run(b.place(stop()))
    clock.advance(seconds=16)
    run(b.sync())
    assert b.order("S1000001").status is OrderStatus.REJECTED and len(cposts(srv)) == 1


# --------------------------------------------------------------------------- sổ lệnh điều kiện ----

@pytest.mark.parametrize("st,expected", [("2", OrderStatus.CANCELLED), ("3", OrderStatus.REJECTED)])
def test_condition_cancelled_or_rejected_at_broker(st, expected):
    b, srv, *_ = bmake()
    run(b.place(stop()))
    srv.conds[0]["status"] = st
    run(b.sync())
    assert b.order("S1000001").status is expected


def test_activation_linked_by_stream_details_and_fills_tracked():
    b, srv, clock, wall, fills, updates, notes = bmake()
    run(b.place(stop()))
    c = srv.conds[0]
    o = srv.trigger(c)
    b.on_stream_frame("message_proto|DE_CONDITIONAL_ORDER|" + json.dumps(
        {"pk_order_no": c["pk_orderNo"], "order_no": c["orderNo"], "status": "1", "details": o["orderNo"],
         "symbol": KRX, "side": "S", "volume": 1}))
    srv.fill(o, 1, 1339.5)
    run(b.sync())
    assert [(f.client_order_id, f.qty, f.price) for f in fills] == [("S1000001", 1, 1339.5)]
    assert b.order("S1000001").status is OrderStatus.FILLED
    assert not any(x.client_order_id.startswith("EXT") for x in run(b.open_orders()))
    assert not any("ngoài bot" in n for n in notes)


def test_activation_linked_from_rest_without_stream():
    b, srv, clock, wall, fills, updates, notes = bmake()
    run(b.place(stop()))
    run(b.sync())
    o = srv.trigger(srv.conds[0])
    srv.fill(o, 1, 1339.0)
    run(b.sync())
    assert [(f.client_order_id, f.price) for f in fills] == [("S1000001", 1339.0)]
    assert not any("ngoài bot" in n for n in notes)


def test_activated_but_not_yet_linked_stays_pending():
    """Đã kích hoạt nhưng lệnh thường chưa thấy ⇒ KHÔNG coi là huỷ (tránh bot đặt stop/đóng lần hai)."""
    b, srv, clock, wall, fills, updates, notes = bmake()
    run(b.place(stop()))
    srv.conds[0]["status"] = "1"
    run(b.sync())
    assert b.order("S1000001").status is OrderStatus.PENDING


def test_unrelated_new_order_not_claimed():
    b, srv, clock, wall, fills, updates, notes = bmake()
    run(b.place(stop()))
    run(b.sync())
    srv.conds[0]["status"] = "1"
    srv.add(refId="APP-0000000009", side="B", volume=1)              # khác chiều ⇒ không phải lệnh kích hoạt
    run(b.sync())
    assert b.order("S1000001").status is OrderStatus.PENDING
    assert any("ngoài bot" in n for n in notes)


# --------------------------------------------------------------------------- huỷ / sửa ----

def test_cancel_pending_condition():
    b, srv, *_ = bmake()
    run(b.place(stop()))
    pk = srv.conds[0]["pk_orderNo"]
    c = run(b.cancel("S1000001"))
    body = cposts(srv, EP_CCANCEL)[0]["body"]
    assert body == {"accountId": CUSTODY, "subAccountId": SUB, "orderNo": pk}
    assert c.status is OrderStatus.CANCELLED


def test_cancel_after_activation_cancels_triggered_order():
    b, srv, *_ = bmake()
    run(b.place(stop()))
    c = srv.conds[0]
    o = srv.trigger(c)
    b.on_stream_frame("message_proto|DE_CONDITIONAL_ORDER|" + json.dumps(
        {"pk_order_no": c["pk_orderNo"], "status": "1", "details": o["orderNo"]}))
    run(b.cancel("S1000001"))
    assert [x["body"]["orderNo"] for x in srv.calls if x["path"] == "/khronos/v1/order/cancel"] == [o["orderNo"]]
    assert not cposts(srv, EP_CCANCEL)


def test_modify_trigger_price_tracks_new_pk():
    b, srv, *_ = bmake()
    run(b.place(stop()))
    old_pk = srv.conds[0]["pk_orderNo"]
    m = run(b.modify("S1000001", price=1338.0))
    ch = cposts(srv, EP_CCHANGE)[0]["body"]
    assert ch["accountId"] == CUSTODY and ch["pkOrderNo"] == old_pk and ch["soPrice"] == 1338.0
    assert ch["type"] == "" and ch["cmd"] == "Web.Order.ChangeCondition" and ch["refId"] != ref_id_for("S1000001")
    assert m.price == 1338.0 and m.broker_order_id == f"cond:{srv.conds[0]['pk_orderNo']}" != f"cond:{old_pk}"
    run(b.cancel("S1000001"))
    assert cposts(srv, EP_CCANCEL)[0]["body"]["orderNo"] == srv.conds[0]["pk_orderNo"]


def test_modify_quantity_refused():
    b, *_ = bmake()
    run(b.place(stop()))
    with pytest.raises(BrokerError, match="khối lượng"):
        run(b.modify("S1000001", qty=2))


# --------------------------------------------------------------------------- khởi động ----

def test_preexisting_pending_conditions_reported_as_working_stops():
    """Lệnh điều kiện còn chờ từ trước (vd bot chết, lệnh còn ở TCBS) ⇒ báo là lệnh STOP đang sống để bot xử lý."""
    srv = CondTcbs()
    srv.add_cond(soPrice="1335.0")
    b, srv, clock, wall, fills, updates, notes = make(srv, config=cfg(broker_side_stops=True))
    working = [o for o in run(b.open_orders()) if o.order_type is OrderType.STOP]
    assert len(working) == 1 and working[0].tag == "external" and working[0].price == 1335.0
    run(b.cancel(working[0].client_order_id))
    assert cposts(srv, EP_CCANCEL)[0]["body"]["orderNo"] == srv.conds[0]["pk_orderNo"]


def test_settings_flag():
    from core.settings import Settings
    from brokers.tcbs import TcbsConfig
    s = Settings({"brokers": {"tcbs_enabled": False, "tcbs": {"broker_side_stops": True}},
                  "contract": {"tick_size_points": 0.1}})
    assert TcbsConfig.from_settings(s, env={}, live=False).broker_side_stops is True
    s2 = Settings({"brokers": {"tcbs": {}}, "contract": {"tick_size_points": 0.1}})
    assert TcbsConfig.from_settings(s2, env={}, live=False).broker_side_stops is False



# --------------------------------------------------------------------------- ca biên (thêm sau thử phá) ----

def test_identical_old_condition_not_claimed_when_uncertain():
    """Lệnh điều kiện y hệt đã có (đã huỷ) từ trước ⇒ không được nhận là lệnh 'không rõ' vừa gửi."""
    srv = CondTcbs()
    srv.add_cond(soPrice="1340.0", volume="1", side="S", status="2")
    b, srv, clock, *_ = make(srv, config=cfg(broker_side_stops=True))
    run(b._band(SYM))
    srv.fail_next = [TimeoutError("t")]
    run(b.place(stop()))
    run(b.sync())
    assert b.order("S1000001").broker_order_id is None


def test_uncertain_condition_waits_for_window():
    b, srv, clock, *_ = bmake()
    run(b._band(SYM))
    srv.fail_next = [TimeoutError("t")]
    run(b.place(stop()))
    clock.advance(seconds=5)
    run(b.sync())
    assert b.order("S1000001").status is OrderStatus.PENDING


def test_activated_then_reported_cancelled_stays_pending_until_linked():
    b, srv, *_ = bmake()
    run(b.place(stop()))
    srv.conds[0]["status"] = "1"
    run(b.sync())
    srv.conds[0]["status"] = "2"
    run(b.sync())
    assert b.order("S1000001").status is OrderStatus.PENDING


def test_child_order_seen_before_activation_is_held_then_linked():
    """Lệnh thường do stop sinh ra hiện trong sổ TRƯỚC khi sổ điều kiện báo kích hoạt ⇒ không nhận là lệnh tay."""
    b, srv, clock, wall, fills, updates, notes = bmake()
    run(b.place(stop()))
    run(b.sync())
    c = srv.conds[0]
    o = srv.trigger(c)
    c["status"] = "0"                                                 # sổ điều kiện chưa cập nhật
    run(b.sync())
    assert not any("ngoài bot" in n for n in notes)
    c["status"] = "1"
    srv.fill(o, 1, 1339.0)
    run(b.sync())
    assert [(f.client_order_id, f.price) for f in fills] == [("S1000001", 1339.0)]


def test_cancel_condition_not_confirmed_stays_pending():
    b, srv, *_ = bmake()
    srv.cond_cancel_final = False
    run(b.place(stop()))
    assert run(b.cancel("S1000001")).status is OrderStatus.PENDING
