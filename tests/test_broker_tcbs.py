"""Test cho brokers/tcbs.py (broker thứ 3, tuỳ chọn) — viết TRƯỚC khi implement.

Theo tài liệu chính thức developers.tcbs.com.vn v1.0.0 (docs/broker_notes.md §2b). Không có mạng/khoá ⇒
server giả. Luật được kiểm:
- token = API Key + iOTP (người dùng đọc trong app TCInvest) — sống tối đa 8 giờ, không refresh;
- `refId` KHÔNG chống trùng ⇒ không bao giờ gửi lại; timeout/5xx ⇒ tra sổ lệnh theo refId; mã lỗi nghiệp vụ
  (kể cả trong HTTP 500, vd -9999) ⇒ chắc chắn chưa tiếp nhận ⇒ REJECTED;
- lệnh thị trường = LO giá trần (mua) / sàn (bán) (API không mô tả MTL — ❓);
- sửa lệnh: không đồng thời giá và KL, `nvol` là tổng;
- chốt TRADING_MODE=live + --live + `brokers.tcbs_enabled` + `api_verified` + token trước MỌI lệnh thay đổi.
"""
import asyncio
import base64
import json
import logging
import urllib.parse
from datetime import datetime, timedelta

import pytest

from brokers.base import BrokerError, LiveTradingNotAllowed
from brokers.tcbs import (TcbsApiError, TcbsBroker, TcbsConfig, TcbsHttp, TcbsTokenManager, TcbsTransportError,
                          build_place_payload, map_order_status, ref_id_for)
from core.clock import FakeClock
from core.models import Order, OrderStatus, OrderType, Side, Tick
from core.settings import MissingSetting, Settings

API_KEY, OTP = "tcbs-api-key-very-secret", "246810"
CUSTODY, SUB = "105C334455", "105C334455A"
T0 = datetime(2026, 11, 9, 10, 0)
EPOCH0 = 1794193200.0
SYM, KRX = "VN30F2611", "41I1GB000"
LIVE_ENV = {"TRADING_MODE": "live"}


def run(coro):
    return asyncio.run(coro)


def jwt(exp: int) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{b({'alg': 'RS256'})}.{b({'exp': exp, 'sub': 'x'})}.sig"


class Wall:
    def __init__(self):
        self.t = EPOCH0

    def __call__(self):
        return self.t


class FakeTcbs:
    def __init__(self):
        self.calls, self.orders, self.positions = [], [], None
        self.fail_next: list = []
        self.create_on_failure = False
        self.n = 0
        self.token_exp = int(EPOCH0) + 8 * 3600
        self.cancel_final = True
        self.ceil, self.floor = 1444.5, 1255.5

    def add(self, **kw):
        self.n += 1
        o = {"orderNo": str(self.n), "pk_orderNo": str(7600000 + self.n), "refId": "", "side": "B",
             "symbol": KRX, "volume": 1, "showPrice": "1350.0", "matchVolume": 0, "status": "P",
             "orderStatus": "2", "isCancel": "1", "isAmend": "1"}
        o.update(kw)
        self.orders.append(o)
        return o

    def by_ref(self, ref):
        return next(o for o in self.orders if o["refId"] == ref)

    def fill(self, o, qty, avg):
        o["matchVolume"], o["matchPriceBQ"] = qty, avg
        o["status"] = "PM"
        o["orderStatus"] = "4" if qty >= o["volume"] else "2"

    async def __call__(self, method, url, params, body, headers, timeout):
        await asyncio.sleep(0)
        path = urllib.parse.urlsplit(url).path
        self.calls.append({"method": method, "path": path, "params": dict(params or {}), "body": body,
                           "headers": dict(headers)})
        if self.fail_next:
            f = self.fail_next.pop(0)
            if self.create_on_failure and path == "/khronos/v1/order/place":
                self._place(body)
            if isinstance(f, Exception):
                raise f
            st, b = f
            return st, {}, json.dumps(b)
        return self._route(method, path, params or {}, body, headers)

    def _place(self, b):
        return self.add(refId=b["refId"], side=b["side"], symbol=b["symbol"], volume=b["volume"],
                        showPrice=str(b["price"]), orderStatus="1")

    def _route(self, method, path, params, b, headers):
        ok = lambda body: (200, {}, json.dumps(body))  # noqa: E731
        err = lambda st, code, msg: (st, {}, json.dumps({"status": st, "code": code, "message": msg, "traceId": "t"}))  # noqa: E731
        if path == "/gaia/v1/oauth2/openapi/token":
            if b == {"apiKey": API_KEY, "otp": OTP}:
                return ok({"token": jwt(self.token_exp)})
            return err(400, "-1", "OTP không hợp lệ")
        if path == "/gaia/v1/oauth2/openapi/apikey/info":
            return ok({"expireDate": "2026-11-12T10:15:30+07:00"})
        if headers.get("Authorization") != f"Bearer {jwt(self.token_exp)}":
            return err(401, "401", "unauthorized")
        if path == "/tartarus/v1/derivatives":
            return ok({"data": [{"symbol": KRX, "ceilPrice": self.ceil, "floorPrice": self.floor, "refPrice": 1350.0}]})
        if path == "/khronos/v1/order/place":
            o = self._place(b)
            return ok({"cmd": "Web.placeOrder", "rc": "1", "rs": "", "data": [{"orderNo": o["orderNo"],
                                                                                "pk_orderNo": o["pk_orderNo"]}]})
        if path == "/khronos/v1/order/in-day":
            rows = [o for o in self.orders if not params.get("refId") or o["refId"] == params["refId"]]
            page, size = int(params["pageNo"]), int(params["pageSize"])
            return ok({"cmd": "Web.Order.IndayOrder", "rc": "1", "data": rows[(page - 1) * size: page * size]})
        if path == "/khronos/v1/order/cancel":
            o = next((x for x in self.orders if x["orderNo"] == b["orderNo"]), None)
            if o is None or o["orderStatus"] in ("4", "5", "7"):
                return err(400, "-6004", "Lệnh không tồn tại hoặc đã được huỷ")
            o["status"], o["orderStatus"] = ("PWX", "5") if self.cancel_final else ("PW", "3")
            return ok({"rc": "1", "data": [{"orderNo": o["orderNo"], "status": "PW"}]})
        if path == "/khronos/v1/order/change":
            o = next(x for x in self.orders if x["orderNo"] == b["orderNo"])
            if float(b["nprice"]) != float(o["showPrice"]) and int(b["nvol"]) != int(o["volume"]):
                return err(400, "-8020", "Không được sửa đồng thời giá và khối lượng")
            o["showPrice"], o["volume"], o["status"] = str(b["nprice"]), int(b["nvol"]), "PC"
            return ok({"rc": "1", "data": [{"orderNo": o["orderNo"], "status": "PC"}]})
        if path == "/khronos/v1/account/portfolio/status":
            data = {"totalVmValue": 0, "totalMrRequire": 0, "totalPercentage": 0}
            if self.positions is not None:
                data["detailData"] = self.positions
            return ok({"cmd": "Web.Portfolio.PortfolioStatus2", "rc": "1", "data": data})
        return err(404, "404", "no route")


def cfg(**kw):
    base = dict(api_key=API_KEY, custody_code=CUSTODY, sub_account=SUB, enabled=True, api_verified=True,
                poll_s=0, uncertain_resolve_s=15, fee_vnd_per_contract=20_000, tick_size=0.1)
    base.update(kw)
    return TcbsConfig(**base)


def make(srv=None, *, env=LIVE_ENV, live_flag=True, dry_run=False, config=None, login=True):
    srv = srv or FakeTcbs()
    clock, wall, notes = FakeClock(T0), Wall(), []
    http = TcbsHttp(transport=srv)
    c = config or cfg()
    tokens = TcbsTokenManager(http, c.api_key, now=wall, notify=notes.append)
    b = TcbsBroker(http, tokens, c, clock=clock, live_flag=live_flag, env=env, dry_run=dry_run, notify=notes.append)
    fills, updates = [], []
    b.on_fill(fills.append)
    b.on_order_update(updates.append)
    if login:
        run(b.connect())
        assert run(b.submit_otp(OTP))
        srv.calls.clear()
        notes.clear()
    return b, srv, clock, wall, fills, updates, notes


def order(cid="E1000001", side=Side.BUY, qty=1, otype=OrderType.LIMIT, price=1350.0, tag="entry"):
    return Order(cid, SYM, side, qty, otype, T0, price=price, broker="tcbs", tag=tag)


def posts(srv, path="/khronos/v1/order/place"):
    return [c for c in srv.calls if c["path"] == path]


# --------------------------------------------------------------------------- token ----

class TestToken:
    def test_connect_asks_for_iotp_from_app(self):
        b, srv, clock, wall, *_, notes = make(login=False)
        run(b.connect())
        assert b.awaiting_otp and not b.trading_ready
        assert any("/otp tcbs" in n and "TCInvest" in n for n in notes)
        assert not srv.calls or all("token" not in c["path"] for c in srv.calls)   # API không tự gửi mã

    def test_submit_otp_body_and_bearer(self):
        b, srv, clock, wall, *_ = make(login=False)
        assert run(b.submit_otp("000000")) is False
        assert run(b.submit_otp(OTP)) is True and b.trading_ready
        tok = [c for c in srv.calls if c["path"].endswith("/token")][-1]
        assert tok["body"] == {"apiKey": API_KEY, "otp": OTP}

    def test_otp_format_checked_locally(self):
        b, srv, *_ = make(login=False)
        assert run(b.submit_otp("12a")) is False and not srv.calls

    def test_token_expires_after_8h_or_jwt_exp_whichever_first(self):
        srv = FakeTcbs()
        srv.token_exp = int(EPOCH0) + 3600                               # JWT nói 1 giờ
        b, srv, clock, wall, *_ = make(srv)
        wall.t += 3600 - 60
        assert b.trading_ready
        wall.t += 120
        assert not b.trading_ready
        srv2 = FakeTcbs()
        srv2.token_exp = int(EPOCH0) + 24 * 3600                          # JWT 24h nhưng tài liệu: tối đa 8h
        b2, srv2, clock2, wall2, *_ = make(srv2)
        wall2.t += 8 * 3600 + 1
        assert not b2.trading_ready

    def test_api_key_expiry_warning(self):
        b, srv, clock, wall, *_, notes = make()
        days = run(b.tokens.check_api_key())
        assert days is not None and days < 7 and any("API Key" in n and "hết hạn" in n for n in notes)

    def test_secrets_never_logged(self, caplog):
        caplog.set_level(logging.DEBUG)
        b, srv, clock, wall, fills, updates, notes = make(login=False)
        run(b.connect())
        run(b.submit_otp(OTP))
        run(b.place(order()))
        text = caplog.text + repr(b) + repr(b.tokens) + "".join(notes)
        for s in (API_KEY, OTP, jwt(srv.token_exp)):
            assert s not in text


# --------------------------------------------------------------------------- cấu hình ----

class TestConfig:
    ENV = {"TCBS_API_KEY": API_KEY, "TCBS_CUSTODY_CODE": CUSTODY, "TCBS_DERIVATIVE_ACCOUNT": SUB}

    def settings(self, enabled=True, verified=True, **fees):
        f = {"broker_fee_vnd_per_contract": 2700, "exchange_fee_vnd_per_contract": 2700,
             "clearing_fee_vnd_per_contract": 2550, "tax_vnd_per_contract": 0}
        f.update(fees)
        return Settings({"brokers": {"tcbs_enabled": enabled, "tcbs": {"api_verified": verified}},
                         "contract": {"tick_size_points": 0.1}, "fees": f})

    def test_live_requires_enabled_flag(self):
        with pytest.raises(LiveTradingNotAllowed, match="tcbs_enabled"):
            TcbsConfig.from_settings(self.settings(enabled=False), env=self.ENV, live=True)

    def test_live_requires_verified(self):
        with pytest.raises(LiveTradingNotAllowed, match="api_verified"):
            TcbsConfig.from_settings(self.settings(verified=False), env=self.ENV, live=True)

    @pytest.mark.parametrize("var", ["TCBS_API_KEY", "TCBS_CUSTODY_CODE", "TCBS_DERIVATIVE_ACCOUNT"])
    def test_live_requires_env(self, var):
        env = dict(self.ENV)
        del env[var]
        with pytest.raises(MissingSetting, match=var):
            TcbsConfig.from_settings(self.settings(), env=env, live=True)

    def test_live_requires_fees(self):
        with pytest.raises(MissingSetting):
            TcbsConfig.from_settings(self.settings(tax_vnd_per_contract=None), env=self.ENV, live=True)

    def test_ok_and_dry_run_tolerant(self):
        c = TcbsConfig.from_settings(self.settings(), env=self.ENV, live=True)
        assert c.custody_code == CUSTODY and c.sub_account == SUB and c.fee_vnd_per_contract == 7950
        d = TcbsConfig.from_settings(self.settings(enabled=False, verified=False), env={}, live=False)
        assert d.api_key is None and not d.enabled


# --------------------------------------------------------------------------- payload / trạng thái ----

class TestPayload:
    def test_ref_id_deterministic_and_10_to_100_chars(self):
        assert ref_id_for("B1") == ref_id_for("B1") and 10 <= len(ref_id_for("B1")) <= 100
        assert ref_id_for("B2611091001001") != ref_id_for("B2611091001002")
        assert ref_id_for("E1000001", 2) != ref_id_for("E1000001")      # refId cho lần sửa: khác lệnh gốc

    def test_limit(self):
        p = build_place_payload(order(), cfg(), band=None)
        assert p == {"accountId": CUSTODY, "subAccountId": SUB, "side": "B", "symbol": KRX, "price": 1350.0,
                     "volume": 1, "orderType": "0", "refId": ref_id_for("E1000001")}

    def test_market_is_marketable_limit_at_band(self):
        buy = build_place_payload(order(otype=OrderType.MARKET, price=None), cfg(), band=(1444.5, 1255.5))
        sell = build_place_payload(order(side=Side.SELL, otype=OrderType.MARKET, price=None), cfg(),
                                   band=(1444.5, 1255.5))
        assert buy["price"] == 1444.5 and sell["price"] == 1255.5

    def test_market_without_band_refused(self):
        with pytest.raises(ValueError, match="trần"):
            build_place_payload(order(otype=OrderType.MARKET, price=None), cfg(), band=None)

    @pytest.mark.parametrize("otype", [OrderType.ATO, OrderType.ATC, OrderType.STOP])
    def test_unsupported_types(self, otype):
        with pytest.raises(ValueError):
            build_place_payload(order(otype=otype), cfg(), band=None)

    def test_off_tick(self):
        with pytest.raises(ValueError, match="bước giá"):
            build_place_payload(order(price=1350.05), cfg(), band=None)


@pytest.mark.parametrize("code,status,vol,match,expected", [
    ("1", "P", 2, 0, OrderStatus.PENDING), ("2", "P", 2, 0, OrderStatus.PENDING),
    ("2", "PM", 2, 1, OrderStatus.PARTIALLY_FILLED), ("3", "PW", 2, 0, OrderStatus.PENDING),
    ("6", "PC", 2, 0, OrderStatus.PENDING), ("4", "PM", 2, 2, OrderStatus.FILLED),
    ("4", "PMWX", 2, 1, OrderStatus.CANCELLED), ("5", "PWX", 2, 0, OrderStatus.CANCELLED),
    ("7", "PJX", 2, 0, OrderStatus.REJECTED),
    ("7", "P", 2, 0, OrderStatus.REJECTED),                 # mã số là chuẩn, kể cả khi chuỗi chưa kết thúc
    ("7", "PMX", 2, 1, OrderStatus.CANCELLED),              # khớp một phần rồi bị hệ thống huỷ phần còn lại
    (None, "PJX", 2, 0, OrderStatus.REJECTED), (None, "PWX", 2, 0, OrderStatus.CANCELLED),
    (None, "PM", 2, 2, OrderStatus.FILLED), ("9", "P", 2, 0, OrderStatus.PENDING),
])
def test_status_mapping(code, status, vol, match, expected):
    assert map_order_status(code, status, vol, match) is expected


# --------------------------------------------------------------------------- chốt an toàn ----

class TestGuards:
    @pytest.mark.parametrize("env,flag", [({"TRADING_MODE": "paper"}, True), (LIVE_ENV, False)])
    def test_live_switches(self, env, flag):
        b, srv, *_ = make(env=env, live_flag=flag)
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order()))
        assert not posts(srv)

    @pytest.mark.parametrize("field", ["enabled", "api_verified"])
    def test_enabled_and_verified(self, field):
        b, srv, *_ = make(config=cfg(**{field: False}))
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order()))
        assert not posts(srv)

    def test_no_token(self):
        b, srv, *_ = make(login=False)
        with pytest.raises(BrokerError, match="iOTP"):
            run(b.place(order()))
        assert not posts(srv)

    def test_every_call_checked(self):
        b, srv, *_ = make()
        run(b.place(order("E1000001")))
        b.env = {}
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order("E1000002")))
        with pytest.raises(LiveTradingNotAllowed):
            run(b.cancel("E1000001"))
        assert len(posts(srv)) == 1 and not posts(srv, "/khronos/v1/order/cancel")

    def test_dry_run(self, caplog):
        caplog.set_level(logging.INFO)
        b, srv, *_ = make(dry_run=True, env={}, live_flag=False, login=False)
        o = run(b.place(order()))
        assert o.status is OrderStatus.REJECTED and "dry-run" in o.reject_reason
        assert not posts(srv) and b.dry_log[-1]["body"]["symbol"] == KRX and KRX in caplog.text


# --------------------------------------------------------------------------- đặt lệnh ----

class TestPlace:
    def test_success(self):
        b, srv, clock, wall, fills, updates, _ = make()
        o = run(b.place(order()))
        c = posts(srv)[0]
        assert c["headers"]["Authorization"].startswith("Bearer ") and c["body"]["refId"] == ref_id_for("E1000001")
        assert o.status is OrderStatus.PENDING and o.broker_order_id == "1" and updates[-1].status is OrderStatus.PENDING

    def test_idempotent(self):
        b, srv, *_ = make()
        run(b.place(order()))
        run(b.place(order()))
        assert len(posts(srv)) == 1

    def test_market_fetches_band(self):
        b, srv, *_ = make()
        run(b.place(order(side=Side.SELL, otype=OrderType.MARKET, price=None)))
        assert posts(srv)[0]["body"]["price"] == srv.floor
        assert any(c["path"] == "/tartarus/v1/derivatives" for c in srv.calls)

    @pytest.mark.parametrize("st,code", [(400, "-6012"), (400, "-6036"), (500, "-9999"), (500, "-8002")])
    def test_business_code_means_not_accepted(self, st, code):
        b, srv, clock, wall, fills, updates, _ = make()
        srv.fail_next = [(st, {"status": st, "code": code, "message": "x", "traceId": "t"})]
        o = run(b.place(order()))
        assert o.status is OrderStatus.REJECTED and code in o.reject_reason
        run(b.sync())
        assert len(posts(srv)) == 1

    def test_rc_zero_is_error(self):
        b, srv, *_ = make()
        srv.fail_next = [(200, {"rc": "0", "rs": "Lỗi gì đó"})]
        assert run(b.place(order())).status is OrderStatus.REJECTED

    @pytest.mark.parametrize("failure", [TimeoutError("t"), ConnectionError("x"), (502, {"message": "bad gw"}),
                                         (500, {"status": 500, "message": "oops"})])
    def test_uncertain_found_by_ref_id(self, failure):
        b, srv, clock, wall, fills, updates, notes = make()
        srv.create_on_failure = True
        srv.fail_next = [failure]
        o = run(b.place(order()))
        assert o.status is OrderStatus.PENDING and o.broker_order_id is None
        run(b.place(order()))                                             # gọi lại cùng id: không gửi lại
        run(b.sync())
        assert b.order("E1000001").broker_order_id == srv.by_ref(ref_id_for("E1000001"))["orderNo"]
        assert len(posts(srv)) == 1
        q = [c for c in srv.calls if c["path"] == "/khronos/v1/order/in-day" and c["params"].get("refId")]
        assert q and q[0]["params"]["refId"] == ref_id_for("E1000001")

    def test_uncertain_not_found_rejected_after_window(self):
        b, srv, clock, wall, fills, updates, _ = make()
        srv.fail_next = [TimeoutError("t")]
        run(b.place(order()))
        clock.advance(seconds=5)
        run(b.sync())
        assert b.order("E1000001").status is OrderStatus.PENDING
        clock.advance(seconds=11)
        run(b.sync())
        assert b.order("E1000001").status is OrderStatus.REJECTED and "sổ lệnh" in b.order("E1000001").reject_reason
        assert len(posts(srv)) == 1


# --------------------------------------------------------------------------- đồng bộ ----

class TestSync:
    def test_cumulative_fills(self):
        b, srv, clock, wall, fills, updates, _ = make()
        run(b.place(order(qty=2)))
        o = srv.by_ref(ref_id_for("E1000001"))
        srv.fill(o, 1, 1350.0)
        run(b.sync())
        srv.fill(o, 2, 1350.5)
        run(b.sync())
        run(b.sync())
        assert [(f.qty, f.price, f.fee_vnd) for f in fills] == [(1, 1350.0, 20_000), (1, 1351.0, 20_000)]
        assert [u.status for u in updates] == [OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED]

    def test_paging_and_query(self):
        b, srv, *_ = make(config=cfg(page_size=2))
        for i in range(5):
            srv.add(refId=f"APP-{i:08d}")
        run(b.sync())
        q = [c for c in srv.calls if c["path"] == "/khronos/v1/order/in-day"]
        assert [c["params"]["pageNo"] for c in q] == [1, 2, 3]
        p = q[0]["params"]
        assert p["accountId"] == CUSTODY and p["symbol"] == "ALL,ALL" and p["status"] == 0 and p["orderType"] == ""
        assert len(run(b.open_orders())) == 5

    def test_external_and_preexisting(self):
        srv = FakeTcbs()
        pre = srv.add(refId="APP-pre-0001", volume=2)
        srv.fill(pre, 1, 1350.0)
        b, srv, clock, wall, fills, updates, notes = make(srv)
        assert fills == []
        srv.fill(pre, 2, 1350.5)
        ext = srv.add(refId="APP-new-0001", side="S")
        srv.fill(ext, 1, 1349.0)
        run(b.sync())
        assert sorted((f.side.value, f.qty, f.price) for f in fills) == [("BUY", 1, 1351.0), ("SELL", 1, 1349.0)]

    def test_ping(self):
        b, srv, clock, *_ = make(config=cfg(poll_s=2))
        assert run(b.ping()) is True
        run(b.ping())
        assert len([c for c in srv.calls if c["path"] == "/khronos/v1/order/in-day"]) == 1
        srv.fail_next = [ConnectionError("x")]
        clock.advance(seconds=2)
        assert run(b.ping()) is False


# --------------------------------------------------------------------------- huỷ / sửa ----

class TestCancelModify:
    def test_cancel_body_and_confirmation(self):
        b, srv, *_ = make()
        o = run(b.place(order()))
        c = run(b.cancel("E1000001"))
        d = posts(srv, "/khronos/v1/order/cancel")[0]
        assert d["body"] == {"accountId": CUSTODY, "orderNo": o.broker_order_id, "cmd": "Web.cancelOrder",
                             "pin": "F", "refId": ""}
        assert c.status is OrderStatus.CANCELLED

    def test_cancel_pending_not_reported_cancelled(self):
        b, srv, *_ = make()
        srv.cancel_final = False
        run(b.place(order()))
        assert run(b.cancel("E1000001")).status is OrderStatus.PENDING

    def test_cancel_already_done_is_safe(self):
        b, srv, *_ = make()
        run(b.place(order()))
        o = srv.by_ref(ref_id_for("E1000001"))
        o["status"], o["orderStatus"] = "PWX", "5"                       # đã huỷ ở chỗ khác
        c = run(b.cancel("E1000001"))
        assert c.status is OrderStatus.CANCELLED

    def test_modify_price_and_qty_separately_with_new_ref_ids(self):
        b, srv, *_ = make(config=cfg())
        run(b.place(order(qty=2)))
        m = run(b.modify("E1000001", price=1351.0, qty=3))
        ch = posts(srv, "/khronos/v1/order/change")
        assert [(c["body"]["nprice"], c["body"]["nvol"]) for c in ch] == [(1351.0, 2), (1351.0, 3)]
        refs = {c["body"]["refId"] for c in ch}
        assert len(refs) == 2 and ref_id_for("E1000001") not in refs
        assert all(c["body"]["subAccountId"] == SUB for c in ch)
        assert m.price == 1351.0 and m.qty == 3

    def test_market_order_cannot_be_modified_locally(self):
        b, srv, *_ = make()
        run(b.place(order(otype=OrderType.MARKET, price=None)))
        with pytest.raises(BrokerError):
            run(b.modify("E1000001", price=1300.0))


# --------------------------------------------------------------------------- stop do bot giữ ----

def test_local_stop_triggers_marketable_limit_once():
    b, srv, *_ = make()
    run(b.place(order("S1000001", side=Side.SELL, otype=OrderType.STOP, price=1340.0, tag="stop")))
    assert not posts(srv)
    run(b.on_tick(Tick(SYM, T0, last=1341.0)))
    run(b.on_tick(Tick(SYM, T0, last=1340.0)))
    run(b.on_tick(Tick(SYM, T0, last=1339.0)))
    p = posts(srv)
    assert len(p) == 1 and p[0]["body"]["side"] == "S" and p[0]["body"]["price"] == srv.floor
    assert p[0]["body"]["refId"] == ref_id_for("S1000001")


# --------------------------------------------------------------------------- vị thế ----

class TestPositions:
    def test_open_positions(self):
        b, srv, *_ = make()
        srv.positions = [{"symbol": KRX, "net": -2, "side": "S", "avg_remain": 1352.0, "deliver": "2", "receive": "0"}]
        p = run(b.positions())[SYM]
        assert p.qty == -2 and p.avg_price == 1352.0 and p.broker == "tcbs"
        c = [x for x in srv.calls if x["path"] == "/khronos/v1/account/portfolio/status"][0]
        assert c["params"] == {"accountId": CUSTODY, "subAccountId": SUB}

    def test_flat_has_no_detail_data(self):
        b, srv, *_ = make()
        assert run(b.positions()) == {}

    def test_side_mismatch_raises(self):
        b, srv, *_ = make()
        srv.positions = [{"symbol": KRX, "net": 1, "side": "S", "avg_remain": 1.0}]
        with pytest.raises(BrokerError, match="side"):
            run(b.positions())


def test_api_error_safe():
    e = TcbsApiError(400, {"status": 400, "code": "-6012", "message": "m" * 900, "token": "TOK"})
    assert "TOK" not in str(e) and len(str(e)) < 300 and e.code == "-6012"
