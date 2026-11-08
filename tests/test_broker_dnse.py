"""Test cho brokers/dnse.py (PROMPTS P4.1) — viết TRƯỚC khi implement.

Không có mạng/khoá ⇒ mọi request đi qua transport giả. Field/endpoint theo SDK chính thức `dnse`
0.5.0 (docs/broker_notes.md §1.3, §1.5); giá trị `orderStatus` CHƯA được kiểm (❓) nên adapter
suy trạng thái từ khối lượng (fill/leave/canceled) trước, chuỗi trạng thái sau.

Luật được kiểm:
- ký HMAC như SDK; `trading-token` chỉ gửi kèm lệnh thay đổi; secret/token/OTP không bao giờ vào log;
- KHÔNG tự retry (DNSE không có idempotency): timeout/5xx ⇒ "không rõ", đối soát sổ lệnh, không gửi lại;
- chốt TRADING_MODE=live + cờ --live + `api_verified` + trading token trước MỌI lệnh thay đổi;
- `--dry-run` chỉ log payload, không gọi API thay đổi;
- idempotent theo client_order_id; sửa lệnh = huỷ-đặt (orderId mới) vẫn cùng client id;
- stop do bot giữ (DNSE không có lệnh stop qua API) ⇒ chạm giá thì gửi MTL.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
from datetime import datetime, timedelta

import pytest

from brokers.base import BrokerError, LiveTradingNotAllowed
from brokers.dnse import (BASE_URL_PROD, BASE_URL_UAT, DnseApiError, DnseBroker, DnseConfig, DnseHttp,
                          DnseTransportError, build_place_payload, map_order_status, sanitize_metadata,
                          sign_headers)
from core.clock import FakeClock
from core.models import Order, OrderStatus, OrderType, Side, Tick
from core.settings import MissingSetting, Settings

KEY, SECRET = "key-abc", "s3cr3t-very-private"
TOKEN = "TT-very-secret-trading-token"
OTP = "834521"
ACC = "0001234567"
LIVE_ENV = {"TRADING_MODE": "live"}
T0 = datetime(2026, 11, 9, 10, 0, 0)
SYM, KRX = "VN30F2611", "41I1GB000"


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- giả lập ----

class FakeTransport:
    """Server DNSE giả: sổ lệnh trong bộ nhớ + hàng đợi lỗi được lập trình sẵn."""

    def __init__(self):
        self.calls: list[dict] = []
        self.orders: dict[str, dict] = {}         # id → OrderItem (camelCase như trên dây)
        self.deals: list[dict] = []
        self.deals_total = None
        self.accounts = [{"id": ACC, "derivativeAccount": True}]
        self.fail_next: list = []                 # phần tử: Exception hoặc (status, body)
        self.next_id = 1000
        self.otp_ok = True
        self.place_creates_on_failure = False      # timeout/5xx nhưng lệnh VẪN tới sàn
        self.cancel_status = "Cancelled"

    def add_order(self, **kw) -> dict:
        self.next_id += 1
        item = {"id": self.next_id, "accountNo": ACC, "symbol": KRX, "side": "NB", "orderType": "LO",
                "orderStatus": "New", "quantity": 1, "fillQuantity": 0, "leaveQuantity": 1,
                "canceledQuantity": 0, "price": 1350.0, "averagePrice": 0.0}
        item.update(kw)
        self.orders[str(item["id"])] = item
        return item

    def fill(self, oid, qty, avg):
        it = self.orders[str(oid)]
        it["fillQuantity"], it["averagePrice"] = qty, avg
        it["leaveQuantity"] = it["quantity"] - qty
        it["orderStatus"] = "Filled" if qty >= it["quantity"] else "PartiallyFilled"

    def paths(self, method=None):
        return [c["path"] for c in self.calls if method is None or c["method"] == method]

    async def __call__(self, method, url, params, json_body, headers, timeout):
        path = urllib.parse.urlsplit(url).path
        self.calls.append({"method": method, "url": url, "path": path, "params": dict(params or {}),
                           "json": json_body, "headers": dict(headers)})
        if self.fail_next:
            f = self.fail_next.pop(0)
            if method == "POST" and path == "/accounts/orders" and self.place_creates_on_failure:
                self._create_from(json_body)
            if isinstance(f, Exception):
                raise f
            st, body = f
            return st, {}, json.dumps(body)
        return self._route(method, path, params or {}, json_body)

    def _create_from(self, b):
        return self.add_order(symbol=b["symbol"], side=b["side"], orderType=b["orderType"],
                              quantity=b["quantity"], leaveQuantity=b["quantity"], price=b["price"])

    def _route(self, method, path, params, b):
        ok = lambda body, st=200: (st, {}, json.dumps(body))  # noqa: E731
        if path == "/accounts" and method == "GET":
            return ok({"accounts": self.accounts, "investorId": "x"})
        if path == "/registration/send-email-otp":
            return 200, {}, ""
        if path == "/registration/trading-token":
            if self.otp_ok and b.get("passcode") == OTP:
                return ok({"tradingToken": TOKEN})
            return ok({"code": "INVALID_OTP", "message": "otp sai"}, 400)
        if path == "/accounts/orders" and method == "POST":
            return ok(self._create_from(b))
        if path == f"/accounts/{ACC}/orders" and method == "GET":
            return ok({"orders": list(self.orders.values())})
        if path.startswith(f"/accounts/{ACC}/orders/"):
            oid = path.rsplit("/", 1)[1]
            it = self.orders.get(oid)
            if it is None:
                return ok({"code": "NOT_FOUND"}, 404)
            if method == "GET":
                return ok(it)
            if method == "DELETE":
                it["orderStatus"] = self.cancel_status
                it["canceledQuantity"] = it["leaveQuantity"]
                it["leaveQuantity"] = 0
                return 204, {}, ""
            if method == "PUT":
                it["orderStatus"], it["canceledQuantity"], it["leaveQuantity"] = "Cancelled", it["leaveQuantity"], 0
                new = self.add_order(symbol=it["symbol"], side=it["side"], orderType=it["orderType"],
                                     quantity=b.get("quantity", it["quantity"]),
                                     leaveQuantity=b.get("quantity", it["quantity"]),
                                     price=b.get("price", it["price"]))
                return ok(new)
        if path == f"/accounts/{ACC}/positions":
            body = {"deals": self.deals}
            if self.deals_total is not None:
                body["total"] = self.deals_total
            return ok(body)
        return ok({"code": "NO_ROUTE"}, 404)


def cfg(**kw) -> DnseConfig:
    base = dict(account_no=ACC, loan_package_id=1036, base_url=BASE_URL_PROD, api_verified=True,
                token_ttl_s=8 * 3600, token_renew_before_s=300, uncertain_resolve_s=15,
                fee_vnd_per_contract=20_000, tick_size=0.1, poll_s=0)
    base.update(kw)
    return DnseConfig(**base)


def make(transport=None, *, live_flag=True, env=LIVE_ENV, dry_run=False, config=None, token=True,
         clock=None):
    tr = transport or FakeTransport()
    clock = clock or FakeClock(T0)
    notes: list[str] = []
    http = DnseHttp(KEY, SECRET, base_url=(config or cfg()).base_url, transport=tr)
    b = DnseBroker(http, config or cfg(), clock=clock, live_flag=live_flag, env=env, dry_run=dry_run,
                   notify=notes.append)
    fills, updates = [], []
    b.on_fill(fills.append)
    b.on_order_update(updates.append)
    if token:
        run(b.connect())
        assert run(b.submit_otp(OTP))
        tr.calls.clear()
        notes.clear()
    return b, tr, clock, fills, updates, notes


def order(cid="E1", side=Side.BUY, qty=1, otype=OrderType.LIMIT, price=1350.0, tag="entry"):
    return Order(cid, SYM, side, qty, otype, T0, price=price, broker="dnse", tag=tag)


def expected_sig(target: str, date_value: str, nonce: str) -> str:
    s = f"(request-target): {target}\ndate: {date_value}\nnonce: {nonce}"
    raw = hmac.new(SECRET.encode(), s.encode(), hashlib.sha256).digest()
    return urllib.parse.quote(base64.b64encode(raw).decode(), safe="")


# --------------------------------------------------------------------------- ký + HTTP ----

class TestSigningAndHttp:
    DATE = "Mon, 09 Nov 2026 03:00:00 +0000"
    NONCE = "0123456789abcdef0123456789abcdef"

    def test_signature_matches_sdk_path_without_query(self):
        h = sign_headers("POST", "/accounts/orders", KEY, SECRET, date_value=self.DATE, nonce=self.NONCE)
        assert h["x-api-key"] == KEY and h["date"] == self.DATE
        assert h["X-Signature"] == (
            f'Signature keyId="{KEY}",algorithm="hmac-sha256",headers="(request-target) date",'
            f'signature="{expected_sig("post /accounts/orders", self.DATE, self.NONCE)}",nonce="{self.NONCE}"')
        assert SECRET not in json.dumps(h)

    def test_request_signs_path_and_sends_query_separately(self):
        tr = FakeTransport()
        http = DnseHttp(KEY, SECRET, transport=tr)
        r = run(http.request("GET", f"/accounts/{ACC}/orders", params={"marketType": "DERIVATIVE"}))
        assert r.status == 200 and "orders" in r.body
        c = tr.calls[0]
        assert c["url"] == f"{BASE_URL_PROD}/accounts/{ACC}/orders" and c["params"] == {"marketType": "DERIVATIVE"}
        assert "trading-token" not in {k.lower() for k in c["headers"]}

    def test_trading_token_header_only_when_given(self):
        tr = FakeTransport()
        http = DnseHttp(KEY, SECRET, transport=tr)
        run(http.request("POST", "/accounts/orders", json_body={"symbol": KRX, "side": "NB", "orderType": "LO",
                                                                 "quantity": 1, "price": 1.0},
                         trading_token=TOKEN))
        assert tr.calls[0]["headers"]["trading-token"] == TOKEN

    @pytest.mark.parametrize("fail", [(429, {"message": "slow"}), (503, {"message": "down"})])
    def test_no_retry_on_error_status(self, fail):
        tr = FakeTransport()
        tr.fail_next = [fail]
        r = run(DnseHttp(KEY, SECRET, transport=tr).request("GET", "/accounts"))
        assert r.status == fail[0] and len(tr.calls) == 1

    def test_network_error_raises_transport_error_once(self):
        tr = FakeTransport()
        tr.fail_next = [TimeoutError("read timeout")]
        with pytest.raises(DnseTransportError):
            run(DnseHttp(KEY, SECRET, transport=tr).request("GET", "/accounts"))
        assert len(tr.calls) == 1

    def test_repr_hides_secret(self):
        assert SECRET not in repr(DnseHttp(KEY, SECRET, transport=FakeTransport()))

    def test_sanitize_unescaped_metadata(self):
        raw = '{"id": 1, "metadata": "{"a":"b"}", "symbol": "X"}'
        body = json.loads(sanitize_metadata(raw))
        assert body["metadata"] == '{"a":"b"}' and body["symbol"] == "X"


# --------------------------------------------------------------------------- cấu hình ----

class TestConfig:
    def settings(self, **dnse):
        d = {"account_no": None, "loan_package_id": None, "base_url": BASE_URL_PROD,
             "trading_token_ttl_seconds": None, "api_verified": False}
        d.update(dnse)
        return Settings({"brokers": {"dnse": d}, "contract": {"tick_size_points": 0.1},
                         "fees": {"broker_fee_vnd_per_contract": 2700, "exchange_fee_vnd_per_contract": 2700,
                                  "clearing_fee_vnd_per_contract": 2550, "tax_vnd_per_contract": 0}})

    def test_dry_run_tolerates_unfilled_values(self):
        c = DnseConfig.from_settings(self.settings(), env={}, live=False)
        assert c.account_no is None and c.api_verified is False

    def test_live_refuses_unverified_api(self):
        s = self.settings(loan_package_id=1036, trading_token_ttl_seconds=28800)
        with pytest.raises(LiveTradingNotAllowed, match="api_verified"):
            DnseConfig.from_settings(s, env={"DNSE_ACCOUNT_NO": ACC}, live=True)

    @pytest.mark.parametrize("missing", ["loan_package_id", "trading_token_ttl_seconds"])
    def test_live_requires_filled_values(self, missing):
        vals = {"loan_package_id": 1036, "trading_token_ttl_seconds": 28800, "api_verified": True}
        vals[missing] = None
        with pytest.raises(MissingSetting):
            DnseConfig.from_settings(self.settings(**vals), env={"DNSE_ACCOUNT_NO": ACC}, live=True)

    def test_live_requires_account_in_env(self):
        s = self.settings(loan_package_id=1036, trading_token_ttl_seconds=28800, api_verified=True)
        with pytest.raises(MissingSetting, match="DNSE_ACCOUNT_NO"):
            DnseConfig.from_settings(s, env={}, live=True)
        c = DnseConfig.from_settings(s, env={"DNSE_ACCOUNT_NO": ACC}, live=True)
        assert c.account_no == ACC and c.fee_vnd_per_contract == 7950 and c.api_verified

    def test_live_requires_fees(self):
        s = self.settings(loan_package_id=1036, trading_token_ttl_seconds=28800, api_verified=True)
        s._data["fees"]["tax_vnd_per_contract"] = None
        with pytest.raises(MissingSetting):
            DnseConfig.from_settings(s, env={"DNSE_ACCOUNT_NO": ACC}, live=True)


# --------------------------------------------------------------------------- payload ----

class TestPayload:
    def test_limit_buy(self):
        p = build_place_payload(order(), cfg())
        assert p == {"accountNo": ACC, "symbol": KRX, "side": "NB", "orderType": "LO", "quantity": 1,
                     "price": 1350.0, "loanPackageId": 1036}

    def test_market_sell_has_no_price(self):
        p = build_place_payload(order(side=Side.SELL, otype=OrderType.MARKET, price=None, qty=2), cfg())
        assert p["side"] == "NS" and p["orderType"] == "MTL" and p["price"] is None and p["quantity"] == 2

    def test_off_tick_price_rejected(self):
        with pytest.raises(ValueError, match="bước giá"):
            build_place_payload(order(price=1350.05), cfg())

    def test_limit_needs_price(self):
        with pytest.raises(ValueError):
            build_place_payload(order(price=None), cfg())

    def test_stop_is_not_an_api_order_type(self):
        with pytest.raises(ValueError):
            build_place_payload(order(otype=OrderType.STOP, price=1340.0), cfg())


@pytest.mark.parametrize("raw,qty,filled,leave,canc,expected", [
    ("New", 2, 0, 2, 0, OrderStatus.PENDING),
    ("pending_new", 2, 0, 2, 0, OrderStatus.PENDING),
    ("PartiallyFilled", 2, 1, 1, 0, OrderStatus.PARTIALLY_FILLED),
    ("Filled", 2, 2, 0, 0, OrderStatus.FILLED),
    ("Cancelled", 2, 0, 0, 2, OrderStatus.CANCELLED),
    ("canceled", 2, 1, 0, 1, OrderStatus.CANCELLED),
    ("Expired", 2, 0, 0, 2, OrderStatus.CANCELLED),
    ("Rejected", 2, 0, 0, 0, OrderStatus.REJECTED),
    ("SomethingNew", 2, 2, 0, 0, OrderStatus.FILLED),          # chuỗi lạ ⇒ suy từ khối lượng
    ("SomethingNew", 2, 0, 0, 2, OrderStatus.CANCELLED),
    ("SomethingNew", 2, 1, 1, 0, OrderStatus.PARTIALLY_FILLED),
    ("SomethingNew", 2, 0, 2, 0, OrderStatus.PENDING),          # không rõ ⇒ coi còn sống, theo dõi tiếp
    (None, 2, None, None, None, OrderStatus.PENDING),
])
def test_status_mapping(raw, qty, filled, leave, canc, expected):
    assert map_order_status(raw, qty, filled, leave, canc) is expected


# --------------------------------------------------------------------------- chốt an toàn ----

class TestGuards:
    def test_requires_trading_mode_live(self):
        b, tr, *_ = make(env={"TRADING_MODE": "paper"})
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order()))
        assert "/accounts/orders" not in tr.paths()

    def test_requires_live_flag(self):
        b, tr, *_ = make(live_flag=False)
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order()))
        assert "/accounts/orders" not in tr.paths()

    def test_production_requires_verified_api(self):
        b, tr, *_ = make(config=cfg(api_verified=False))
        with pytest.raises(LiveTradingNotAllowed, match="api_verified"):
            run(b.place(order()))
        assert "/accounts/orders" not in tr.paths()

    def test_uat_allowed_before_verification(self):
        b, tr, *_ = make(config=cfg(api_verified=False, base_url=BASE_URL_UAT))
        o = run(b.place(order()))
        assert o.status is OrderStatus.PENDING and tr.calls[-1]["url"].startswith(BASE_URL_UAT)

    def test_stop_placement_also_guarded(self):
        b, tr, *_ = make(env={})
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order(cid="S1", side=Side.SELL, otype=OrderType.STOP, price=1340.0, tag="stop")))

    def test_cancel_guarded(self):
        b, tr, *_ = make()
        run(b.place(order()))
        b.env = {"TRADING_MODE": "paper"}
        with pytest.raises(LiveTradingNotAllowed):
            run(b.cancel("E1"))
        assert not tr.paths("DELETE")

    def test_no_token_no_order(self):
        b, tr, *_ = make(token=False)
        run(b.connect())
        with pytest.raises(BrokerError, match="trading token"):
            run(b.place(order()))
        assert "/accounts/orders" not in tr.paths()

    def test_guard_checked_on_every_call_not_only_startup(self):
        b, tr, *_ = make()
        run(b.place(order("E1")))
        b.env = {}
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order("E2")))
        assert tr.paths("POST").count("/accounts/orders") == 1


class TestDryRun:
    def test_logs_payload_without_calling_mutation_api(self, caplog):
        b, tr, *_ = make(dry_run=True, env={}, live_flag=False, token=False)
        caplog.set_level(logging.INFO)
        o = run(b.place(order()))
        assert o.status is OrderStatus.REJECTED and "dry-run" in o.reject_reason
        assert not [c for c in tr.calls if c["method"] in ("POST", "PUT", "DELETE")]
        assert b.dry_log[-1]["json"]["symbol"] == KRX and b.dry_log[-1]["path"] == "/accounts/orders"
        assert KRX in caplog.text

    def test_dry_run_cancel_does_not_call_api(self):
        b, tr, *_ = make(dry_run=True, env={}, live_flag=False, token=False)
        tr.add_order()
        run(b.sync())                                          # nhận lệnh có sẵn trên sổ
        cid = run(b.open_orders())[0].client_order_id
        with pytest.raises(BrokerError, match="dry-run"):
            run(b.cancel(cid))
        assert not tr.paths("DELETE") and b.dry_log[-1]["method"] == "DELETE"


# --------------------------------------------------------------------------- đặt lệnh ----

class TestPlace:
    def test_sends_sdk_shape_with_token(self):
        b, tr, _, _, updates, _ = make()
        o = run(b.place(order()))
        c = [c for c in tr.calls if c["path"] == "/accounts/orders"][0]
        assert c["method"] == "POST" and c["params"] == {"marketType": "DERIVATIVE", "orderCategory": "NORMAL"}
        assert c["json"]["side"] == "NB" and c["json"]["symbol"] == KRX
        assert c["headers"]["trading-token"] == TOKEN
        assert o.status is OrderStatus.PENDING and o.broker_order_id == "1001" and o.client_order_id == "E1"
        assert updates[-1].status is OrderStatus.PENDING

    def test_idempotent_by_client_id(self):
        b, tr, *_ = make()
        a = run(b.place(order()))
        again = run(b.place(order()))
        assert again.broker_order_id == a.broker_order_id
        assert tr.paths("POST").count("/accounts/orders") == 1

    def test_business_reject_returned_not_raised(self):
        b, tr, _, _, updates, _ = make()
        tr.fail_next = [(400, {"code": "INSUFFICIENT_BALANCE", "message": "không đủ ký quỹ"})]
        o = run(b.place(order()))
        assert o.status is OrderStatus.REJECTED and "INSUFFICIENT_BALANCE" in o.reject_reason
        assert updates[-1].status is OrderStatus.REJECTED
        tr.calls.clear()
        run(b.sync())
        assert "/accounts/orders" not in tr.paths("POST")              # không gửi lại

    def test_rate_limited_is_rejected_not_retried(self):
        b, tr, *_ = make()
        tr.fail_next = [(429, {"message": "slow"})]
        o = run(b.place(order()))
        assert o.status is OrderStatus.REJECTED and "429" in o.reject_reason
        assert tr.paths("POST").count("/accounts/orders") == 1

    def test_expired_token_rejects_and_requests_otp(self):
        b, tr, clock, _, _, notes = make()
        tr.fail_next = [(401, {"code": "INVALID_TRADING_TOKEN", "message": "expired"})]
        o = run(b.place(order()))
        assert o.status is OrderStatus.REJECTED and "INVALID_TRADING_TOKEN" in o.reject_reason
        assert "/registration/send-email-otp" in tr.paths("POST")
        assert any("/otp" in n for n in notes)
        assert run(b.ping()) is False                                 # không thao tác được lệnh ⇒ coi như mất kết nối
        with pytest.raises(BrokerError, match="trading token"):
            run(b.place(order("E2")))


class TestUncertainPlacement:
    @pytest.mark.parametrize("failure", [TimeoutError("read timeout"), ConnectionError("reset"),
                                         (502, {"message": "bad gateway"}), (504, {})])
    def test_unknown_outcome_never_resent(self, failure):
        b, tr, *_ = make()
        tr.fail_next = [failure]
        o = run(b.place(order()))
        assert o.status is OrderStatus.PENDING and o.broker_order_id is None
        run(b.place(order()))                                           # execution gọi lại cùng id
        run(b.sync())
        assert tr.paths("POST").count("/accounts/orders") == 1

    def test_found_in_order_book_then_tracked(self):
        b, tr, clock, fills, updates, _ = make()
        old = tr.add_order(side="NS", price=1360.0)                     # lệnh có sẵn, không phải của ta
        run(b.sync())
        tr.place_creates_on_failure = True
        tr.fail_next = [TimeoutError("read timeout")]
        run(b.place(order()))
        run(b.sync())
        mine = [o for o in run(b.open_orders()) if o.client_order_id == "E1"][0]
        new_id = max(int(i) for i in tr.orders)
        assert mine.broker_order_id == str(new_id) != str(old["id"])
        tr.fill(new_id, 1, 1350.0)
        run(b.sync())
        assert [(f.client_order_id, f.qty, f.price) for f in fills] == [("E1", 1, 1350.0)]

    def test_identical_preexisting_order_is_not_claimed(self):
        """Lệnh giống hệt đã có TRƯỚC lúc gửi (vd đặt tay) không được nhận nhầm là lệnh "không rõ" của ta."""
        b, tr, clock, _, updates, _ = make()
        twin = tr.add_order()                                           # NB LO 1 @1350 — y hệt lệnh sắp gửi
        run(b.sync())
        tr.fail_next = [TimeoutError("read timeout")]                   # lệnh của ta KHÔNG tới sàn
        run(b.place(order()))
        run(b.sync())
        assert b.order("E1").broker_order_id is None
        clock.advance(seconds=16)
        run(b.sync())
        assert b.order("E1").status is OrderStatus.REJECTED
        assert b.order(f"EXT{twin['id']}").broker_order_id == str(twin["id"])

    def test_identical_finished_order_is_not_claimed(self):
        """Lệnh giống hệt đã KHỚP XONG từ trước (không được nhận theo dõi): nhận nhầm = Fill ma."""
        tr = FakeTransport()
        twin = tr.add_order()
        tr.fill(twin["id"], 1, 1350.0)                                  # xong trước khi bot khởi động
        b, tr, clock, fills, _, _ = make(tr)
        tr.fail_next = [TimeoutError("read timeout")]
        run(b.place(order()))
        run(b.sync())
        assert b.order("E1").broker_order_id is None and fills == []

    def test_not_found_after_window_is_rejected(self):
        b, tr, clock, _, updates, notes = make()
        tr.fail_next = [TimeoutError("read timeout")]
        run(b.place(order()))
        clock.advance(seconds=5)
        run(b.sync())
        assert updates[-1].status is OrderStatus.PENDING
        clock.advance(seconds=11)
        run(b.sync())
        assert updates[-1].client_order_id == "E1" and updates[-1].status is OrderStatus.REJECTED
        assert "sổ lệnh" in updates[-1].reject_reason
        assert tr.paths("POST").count("/accounts/orders") == 1

    def test_uncertain_order_cannot_be_cancelled_yet(self):
        b, tr, *_ = make()
        tr.fail_next = [TimeoutError("x")]
        run(b.place(order()))
        with pytest.raises(BrokerError, match="đối soát"):
            run(b.cancel("E1"))


# --------------------------------------------------------------------------- đồng bộ ----

class TestSync:
    def test_partial_then_full_fills(self):
        b, tr, clock, fills, updates, _ = make()
        o = run(b.place(order(qty=2)))
        bid = o.broker_order_id
        tr.fill(bid, 1, 1350.0)
        run(b.sync())
        tr.fill(bid, 2, 1350.5)                                         # avg 1350.5 ⇒ lần 2 khớp 1351.0
        run(b.sync())
        assert [(f.qty, f.price, f.fee_vnd, f.side, f.symbol) for f in fills] == [
            (1, 1350.0, 20_000, Side.BUY, SYM), (1, 1351.0, 20_000, Side.BUY, SYM)]
        sts = [u.status for u in updates if u.client_order_id == "E1"]
        assert sts == [OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED]
        assert updates[-1].filled_qty == 2

    def test_fill_emitted_before_order_update(self):
        b, tr, clock, fills, updates, _ = make()
        events = []
        b.on_fill(lambda f: events.append("fill"))
        b.on_order_update(lambda o: events.append(o.status.value))
        o = run(b.place(order()))
        tr.fill(o.broker_order_id, 1, 1350.0)
        run(b.sync())
        assert events[-2:] == ["fill", "FILLED"]

    def test_no_duplicate_events_on_repeated_sync(self):
        b, tr, clock, fills, updates, _ = make()
        o = run(b.place(order()))
        tr.fill(o.broker_order_id, 1, 1350.0)
        run(b.sync())
        n_f, n_u = len(fills), len(updates)
        run(b.sync())
        run(b.sync())
        assert (len(fills), len(updates)) == (n_f, n_u)

    def test_missing_from_book_is_fetched_by_id(self):
        b, tr, *_ = make()
        o = run(b.place(order()))
        item = tr.orders.pop(o.broker_order_id)                          # sổ lệnh không trả lệnh này
        item.update(orderStatus="Filled", fillQuantity=1, leaveQuantity=0, averagePrice=1350.0)
        tr._route_backup = item
        orig = tr._route

        def route(method, path, params, body):
            if path.endswith("/" + o.broker_order_id) and method == "GET":
                return 200, {}, json.dumps(item)
            return orig(method, path, params, body)
        tr._route = route
        run(b.sync())
        assert f"/accounts/{ACC}/orders/{o.broker_order_id}" in tr.paths("GET")
        assert b.order("E1").status is OrderStatus.FILLED

    def test_external_order_adopted(self):
        """Lệnh đặt tay trên app vẫn được theo dõi ⇒ net position của risk luôn đúng."""
        b, tr, clock, fills, updates, notes = make()
        ext = tr.add_order(side="NS", quantity=1)
        run(b.sync())
        adopted = [o for o in run(b.open_orders()) if o.broker_order_id == str(ext["id"])]
        assert adopted and len(adopted[0].client_order_id) <= 20 and adopted[0].side is Side.SELL
        assert any("ngoài bot" in n for n in notes)
        tr.fill(ext["id"], 1, 1349.0)
        run(b.sync())
        assert fills[-1].side is Side.SELL and fills[-1].price == 1349.0

    def test_preexisting_partial_fill_not_reemitted_at_startup(self):
        """Lệnh có sẵn đã khớp 1/2 trước khi bot chạy: phần đó đã nằm trong vị thế ⇒ không phát Fill lại."""
        tr = FakeTransport()
        ext = tr.add_order(quantity=2, leaveQuantity=1)
        tr.fill(ext["id"], 1, 1350.0)
        b, tr, clock, fills, updates, _ = make(tr)                      # connect() đọc sổ lệnh lần đầu
        assert fills == []
        tr.fill(ext["id"], 2, 1350.5)
        run(b.sync())
        assert [(f.qty, f.price) for f in fills] == [(1, 1351.0)]

    def test_new_external_order_filled_between_polls_is_counted(self):
        """Lệnh đặt tay xuất hiện lần đầu đã khớp xong (giữa hai lần đọc) vẫn phải phát Fill."""
        b, tr, clock, fills, updates, _ = make()
        ext = tr.add_order(side="NS")
        tr.fill(ext["id"], 1, 1349.0)
        run(b.sync())
        assert [(f.side, f.qty, f.price) for f in fills] == [(Side.SELL, 1, 1349.0)]

    def test_unrecognized_book_shape_fails_ping(self):
        b, tr, *_ = make()
        tr.fail_next = [(200, {"weird": True})]
        assert run(b.ping()) is False

    def test_ping_true_when_book_readable_and_token_valid(self):
        b, tr, *_ = make()
        assert run(b.ping()) is True
        tr.fail_next = [ConnectionError("down")]
        assert run(b.ping()) is False

    def test_poll_interval_respected(self):
        b, tr, clock, *_ = make(config=cfg(poll_s=2))
        run(b.ping())
        run(b.ping())
        assert tr.paths("GET").count(f"/accounts/{ACC}/orders") == 1
        clock.advance(seconds=2)
        run(b.ping())
        assert tr.paths("GET").count(f"/accounts/{ACC}/orders") == 2


# --------------------------------------------------------------------------- huỷ / sửa ----

class TestCancelModify:
    def test_cancel_confirmed(self):
        b, tr, clock, _, updates, _ = make()
        o = run(b.place(order()))
        c = run(b.cancel("E1"))
        d = [x for x in tr.calls if x["method"] == "DELETE"][0]
        assert d["path"] == f"/accounts/{ACC}/orders/{o.broker_order_id}"
        assert d["params"] == {"marketType": "DERIVATIVE", "orderCategory": "NORMAL"}
        assert d["headers"]["trading-token"] == TOKEN
        assert c.status is OrderStatus.CANCELLED and updates[-1].status is OrderStatus.CANCELLED

    def test_cancel_pending_confirmation_is_not_reported_cancelled(self):
        b, tr, clock, _, updates, _ = make()
        tr.cancel_status = "PendingCancel"
        orig = tr._route

        def route(method, path, params, body):            # sàn chưa xác nhận huỷ
            st, h, txt = orig(method, path, params, body)
            if method == "DELETE":
                it = tr.orders[path.rsplit("/", 1)[1]]
                it["leaveQuantity"], it["canceledQuantity"] = 1, 0
            return st, h, txt
        tr._route = route
        run(b.place(order()))
        c = run(b.cancel("E1"))
        assert c.status is OrderStatus.PENDING                         # chưa CANCELLED cho tới khi sàn xác nhận

    def test_cancel_errors(self):
        b, tr, *_ = make()
        with pytest.raises(BrokerError):
            run(b.cancel("NOPE"))
        o = run(b.place(order()))
        tr.fill(o.broker_order_id, 1, 1350.0)
        run(b.sync())
        with pytest.raises(BrokerError):
            run(b.cancel("E1"))                                        # đã kết thúc
        run(b.place(order("E2")))
        tr.fail_next = [(400, {"code": "ORDER_IN_ATC", "message": "không huỷ trong ATC"})]
        with pytest.raises(BrokerError, match="ORDER_IN_ATC"):
            run(b.cancel("E2"))

    def test_modify_tracks_new_broker_id_under_same_client_id(self):
        b, tr, clock, fills, updates, _ = make()
        o = run(b.place(order(qty=2)))
        tr.fill(o.broker_order_id, 1, 1350.0)
        run(b.sync())
        m = run(b.modify("E1", price=1351.0, qty=2))
        put = [x for x in tr.calls if x["method"] == "PUT"][0]
        # huỷ-đặt tạo lệnh MỚI ⇒ gửi phần CÒN LẠI (tổng 2 − đã khớp 1), gửi tổng sẽ khớp quá số (❓ §1.5)
        assert put["json"] == {"price": 1351.0, "quantity": 1} and put["headers"]["trading-token"] == TOKEN
        assert m.client_order_id == "E1" and m.broker_order_id != o.broker_order_id and m.price == 1351.0
        assert m.status is OrderStatus.PARTIALLY_FILLED and m.filled_qty == 1     # leg mới PENDING, tổng đã khớp 1
        tr.fill(m.broker_order_id, 1, 1351.0)
        run(b.sync())
        assert [(f.client_order_id, f.price) for f in fills] == [("E1", 1350.0), ("E1", 1351.0)]
        assert b.order("E1").filled_qty == 2


# --------------------------------------------------------------------------- stop do bot giữ ----

class TestLocalStop:
    def stop(self, side=Side.SELL, trigger=1340.0, qty=1, cid="S1"):
        return order(cid=cid, side=side, otype=OrderType.STOP, price=trigger, qty=qty, tag="stop")

    def test_held_locally_without_api_call(self):
        b, tr, clock, _, updates, _ = make()
        s = run(b.place(self.stop()))
        assert s.status is OrderStatus.PENDING and s.broker_order_id.startswith("local")
        assert not [c for c in tr.calls if c["method"] == "POST"]
        assert any(o.client_order_id == "S1" for o in run(b.open_orders()))

    def test_sell_stop_triggers_market_order_once(self):
        b, tr, clock, fills, _, _ = make()
        run(b.place(self.stop()))
        run(b.on_tick(Tick(SYM, T0, last=1341.0, bid=1340.9, ask=1341.1)))
        assert "/accounts/orders" not in tr.paths("POST")
        run(b.on_tick(Tick(SYM, T0, last=1340.0, bid=1339.9, ask=1340.1)))
        run(b.on_tick(Tick(SYM, T0, last=1339.0)))
        posts = [c for c in tr.calls if c["path"] == "/accounts/orders"]
        assert len(posts) == 1 and posts[0]["json"]["side"] == "NS" and posts[0]["json"]["orderType"] == "MTL"
        bid = b.order("S1").broker_order_id
        assert not bid.startswith("local")
        tr.fill(bid, 1, 1339.8)
        run(b.sync())
        assert fills[-1].client_order_id == "S1" and fills[-1].price == 1339.8

    def test_buy_stop_triggers_on_ask(self):
        b, tr, *_ = make()
        run(b.place(self.stop(side=Side.BUY, trigger=1360.0)))
        run(b.on_tick(Tick(SYM, T0, bid=1359.9, ask=1360.0)))
        posts = [c for c in tr.calls if c["path"] == "/accounts/orders"]
        assert len(posts) == 1 and posts[0]["json"]["side"] == "NB"

    def test_other_symbol_ignored(self):
        b, tr, *_ = make()
        run(b.place(self.stop()))
        run(b.on_tick(Tick("VN30F2612", T0, last=1000.0)))
        assert "/accounts/orders" not in tr.paths("POST")

    def test_trigger_rejected_is_reported(self):
        b, tr, clock, _, updates, _ = make()
        run(b.place(self.stop()))
        tr.fail_next = [(400, {"code": "X", "message": "từ chối"})]
        run(b.on_tick(Tick(SYM, T0, last=1339.0)))
        assert updates[-1].client_order_id == "S1" and updates[-1].status is OrderStatus.REJECTED

    def test_cancel_and_modify_local(self):
        b, tr, clock, _, updates, _ = make()
        run(b.place(self.stop()))
        m = run(b.modify("S1", price=1338.0))
        assert m.price == 1338.0
        run(b.on_tick(Tick(SYM, T0, last=1339.0)))
        assert "/accounts/orders" not in tr.paths("POST")              # trigger mới 1338
        c = run(b.cancel("S1"))
        assert c.status is OrderStatus.CANCELLED and not tr.paths("DELETE")
        run(b.on_tick(Tick(SYM, T0, last=1300.0)))
        assert "/accounts/orders" not in tr.paths("POST")


# --------------------------------------------------------------------------- vị thế ----

class TestPositions:
    def test_aggregates_open_deals(self):
        b, tr, *_ = make()
        tr.deals = [
            {"id": 1, "symbol": KRX, "side": "BUY", "status": "OPEN", "openQuantity": 2, "costPrice": 1350.0},
            {"id": 2, "symbol": KRX, "side": "NB", "status": "OPEN", "openQuantity": 1, "costPrice": 1353.0},
            {"id": 3, "symbol": KRX, "side": "SELL", "status": "CLOSED", "openQuantity": 0, "costPrice": 1340.0},
        ]
        pos = run(b.positions())
        p = pos[SYM]
        assert p.qty == 3 and p.avg_price == pytest.approx(1351.0) and p.broker == "dnse"
        call = [c for c in tr.calls if c["path"].endswith("/positions")][0]
        assert call["params"]["marketType"] == "DERIVATIVE"

    def test_contradictory_deal_raises(self):
        b, tr, *_ = make()
        tr.deals = [{"id": 1, "symbol": KRX, "side": "BUY", "status": "CLOSED", "openQuantity": 1, "costPrice": 1.0}]
        with pytest.raises(BrokerError, match="CLOSED"):
            run(b.positions())

    def test_short(self):
        b, tr, *_ = make()
        tr.deals = [{"id": 1, "symbol": KRX, "side": "SELL", "status": "OPEN", "openQuantity": 1, "costPrice": 1350.0}]
        assert run(b.positions())[SYM].qty == -1

    def test_incomplete_page_raises(self):
        b, tr, *_ = make()
        tr.deals = [{"id": 1, "symbol": KRX, "side": "BUY", "status": "OPEN", "openQuantity": 1, "costPrice": 1.0}]
        tr.deals_total = 5
        with pytest.raises(BrokerError, match="trang"):
            run(b.positions())

    def test_unknown_side_raises(self):
        b, tr, *_ = make()
        tr.deals = [{"id": 1, "symbol": KRX, "side": "??", "status": "OPEN", "openQuantity": 1, "costPrice": 1.0}]
        with pytest.raises(BrokerError):
            run(b.positions())


# --------------------------------------------------------------------------- token / kết nối ----

class TestTokenAndConnect:
    def test_connect_checks_account_and_requests_otp(self):
        b, tr, clock, _, _, notes = make(token=False)
        run(b.connect())
        assert "/accounts" in tr.paths("GET") and "/registration/send-email-otp" in tr.paths("POST")
        assert any("/otp" in n for n in notes)
        assert run(b.ping()) is False                                  # chưa có token

    def test_connect_rejects_unknown_account(self):
        tr = FakeTransport()
        tr.accounts = [{"id": "999", "derivativeAccount": True}]
        b, *_ = make(tr, token=False)
        with pytest.raises(BrokerError, match="tiểu khoản"):
            run(b.connect())

    def test_connect_rejects_non_derivative_account(self):
        tr = FakeTransport()
        tr.accounts = [{"id": ACC, "derivativeAccount": False}]
        b, *_ = make(tr, token=False)
        with pytest.raises(BrokerError, match="phái sinh"):
            run(b.connect())

    def test_submit_otp(self):
        b, tr, clock, _, _, notes = make(token=False)
        run(b.connect())
        assert run(b.submit_otp("000000")) is False
        assert run(b.submit_otp(OTP)) is True
        call = [c for c in tr.calls if c["path"] == "/registration/trading-token"][-1]
        assert call["json"] == {"otpType": "email_otp", "passcode": OTP}
        assert run(b.ping()) is True
        assert not any(OTP in n or TOKEN in n for n in notes)

    def test_otp_format_checked_locally(self):
        b, tr, *_ = make(token=False)
        assert run(b.submit_otp("abc")) is False
        assert "/registration/trading-token" not in tr.paths()

    def test_renewal_requested_before_expiry_once(self):
        b, tr, clock, _, _, notes = make(config=cfg(token_ttl_s=3600, token_renew_before_s=300, otp_resend_s=120))
        clock.advance(seconds=3600 - 400)
        run(b.ping())
        assert "/registration/send-email-otp" not in tr.paths()
        clock.advance(seconds=150)
        run(b.ping())
        run(b.ping())
        assert tr.paths("POST").count("/registration/send-email-otp") == 1
        assert run(b.ping()) is True                                   # vẫn còn hạn
        clock.advance(seconds=300)
        assert run(b.ping()) is False                                  # hết hạn ⇒ không thao tác được lệnh

    def test_secrets_never_logged(self, caplog):
        caplog.set_level(logging.DEBUG)
        b, tr, clock, *_ = make(token=False)
        run(b.connect())
        run(b.submit_otp(OTP))
        o = run(b.place(order()))
        tr.fail_next = [(401, {"code": "INVALID_TRADING_TOKEN"})]
        run(b.place(order("E2")))
        run(b.sync())
        text = caplog.text + repr(b)
        for secret in (SECRET, TOKEN, OTP):
            assert secret not in text


def test_api_error_message_is_safe():
    e = DnseApiError(400, {"code": "X", "message": "m" * 1000, "tradingToken": TOKEN})
    assert TOKEN not in str(e) and len(str(e)) < 400 and e.code == "X"
