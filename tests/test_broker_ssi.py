"""Test cho brokers/ssi.py (PROMPTS P4.2) — viết TRƯỚC khi implement.

Endpoint/field theo mã nguồn SDK chính thức `ssi-sdk` 3.3.2 (docs/broker_notes.md §2.6). Không có
mạng/khoá ⇒ mọi request đi qua server giả, kiểm cả chữ ký RSA bằng khoá công khai.

Luật được kiểm:
- ký RSA-SHA256 (PKCS#1 v1.5, hex) đúng CHUỖI body được gửi; Bearer token; không retry ẩn;
- lỗi có thể đến dưới dạng HTTP 200 + `{code, msg}` ⇒ vẫn là lỗi;
- refresh token CHỈ DÙNG MỘT LẦN: không bao giờ gửi lại, kể cả khi lần gửi trước lỗi mạng;
- `clientRequestId` = client_order_id (khoá idempotency của SSI); 409 ⇒ lệnh đã tồn tại, tìm trong sổ;
- chốt TRADING_MODE=live + --live + `api_verified` + token trước MỌI lệnh thay đổi; dry-run chỉ log;
- giá trị ❓ (định dạng mã hợp đồng, loại lệnh thị trường) còn null ⇒ live từ chối chạy;
- stream TRADING (`order.<tk>`) áp sự kiện cộng dồn — trùng `notifyId` hay trùng với REST không đếm đôi;
- vị thế phái sinh từ `derOpenPositions`.
"""
import asyncio
import base64
import hashlib
import json
import logging
import random
import urllib.parse
from datetime import datetime

import pytest

from brokers.base import BrokerError, LiveTradingNotAllowed
from brokers.ssi import (SsiApiError, SsiBroker, SsiConfig, SsiHttp, SsiOrderStream, SsiTokenManager,
                         SsiTransportError, build_place_payload, load_private_key, map_order_status,
                         rsa_sign)
from core.clock import FakeClock
from core.models import Order, OrderStatus, OrderType, Side, Tick
from core.settings import MissingSetting, Settings

API_KEY, API_SECRET = "ssi-key", "ssi-secret-very-private"
ACC = "0001238"                        # tài khoản phái sinh: đuôi 8
OTP = "445566"
T0 = datetime(2026, 11, 9, 10, 0, 0)
EPOCH0 = 1794193200.0                  # 2026-11-09 03:00 UTC = 10:00 VN
SYM, KRX = "VN30F2611", "41I1GB000"
LIVE_ENV = {"TRADING_MODE": "live"}


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- khoá RSA cho test ----

def _is_probable_prime(n, rnd):
    if n < 4:
        return n in (2, 3)
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(20):
        a = rnd.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits, rnd):
    while True:
        c = rnd.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(c, rnd):
            return c


def _make_key():
    rnd = random.Random(42)
    e = 65537
    while True:
        p, q = _prime(512, rnd), _prime(512, rnd)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            break
    n, d = p * q, pow(e, -1, (p - 1) * (q - 1))
    b = lambda x: base64.b64encode(x.to_bytes((x.bit_length() + 7) // 8, "big")).decode()  # noqa: E731
    xml = f"<RSAKeyValue><Modulus>{b(n)}</Modulus><Exponent>{b(e)}</Exponent><D>{b(d)}</D></RSAKeyValue>"
    return base64.b64encode(xml.encode()).decode(), n, e


PRIVATE_KEY, N, E = _make_key()
_DER = bytes.fromhex("3031300d060960864801650304020105000420")


def verify(data: str, sig_hex: str) -> bool:
    k = (N.bit_length() + 7) // 8
    di = _DER + hashlib.sha256(data.encode()).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(di) - 3) + b"\x00" + di
    return pow(int(sig_hex, 16), E, N).to_bytes(k, "big") == expected


# --------------------------------------------------------------------------- server giả ----

class FakeSsi:
    """Server SSI giả: token (refresh một lần), sổ lệnh có idempotency theo clientRequestId, vị thế."""

    def __init__(self):
        self.calls: list[dict] = []
        self.orders: list[dict] = []
        self.open_positions: list[dict] = []
        self.fail_next: list = []
        self.next_id = 5000
        self.n_tokens = 0
        self.valid_access: set[str] = set()
        self.live_refresh: set[str] = set()
        self.used_refresh: set[str] = set()
        self.revoked = False
        self.create_on_failure = False
        self.wrap_data = True
        self.ttl = 3600
        self.now = lambda: EPOCH0
        self.cancel_status = "CL"

    def add(self, **kw) -> dict:
        self.next_id += 1
        o = {"orderId": str(self.next_id), "clientRequestId": "", "accountNo": ACC, "instrumentId": KRX,
             "buySell": "B", "orderType": "LO", "price": "1350", "avgPrice": 0, "quantity": 1,
             "filledQty": 0, "cancelQty": 0, "orderStatus": "QU"}
        o.update(kw)
        self.orders.append(o)
        return o

    def by_cid(self, cid):
        return next(o for o in self.orders if o["clientRequestId"] == cid)

    def fill(self, o, qty, avg):
        o["filledQty"], o["avgPrice"] = qty, avg
        o["orderStatus"] = "FF" if qty >= o["quantity"] else "PF"

    def _issue(self):
        self.n_tokens += 1
        acc, ref = f"ACCESS-{self.n_tokens}", f"REFRESH-{self.n_tokens}"
        self.valid_access.add(acc)
        self.live_refresh.add(ref)
        now = int(self.now())
        return {"accessToken": acc, "expiresAt": now + self.ttl, "refreshToken": ref,
                "refreshExpiresAt": now + 8 * 3600}

    def _ok(self, data, wrap=None):
        wrap = self.wrap_data if wrap is None else wrap
        body = {"code": 200, "msg": "success", "data": data} if wrap else data
        return 200, {}, json.dumps(body)

    async def __call__(self, method, url, params, content, headers, timeout):
        await asyncio.sleep(0)                       # nhường lượt như request mạng thật
        path = urllib.parse.urlsplit(url).path
        body = json.loads(content) if content else None
        self.calls.append({"method": method, "path": path, "params": dict(params or {}), "body": body,
                           "content": content, "headers": dict(headers)})
        if self.fail_next:
            f = self.fail_next.pop(0)
            if self.create_on_failure and method == "POST" and path == "/api/v3/trading/order":
                self._place(body)
            if isinstance(f, Exception):
                raise f
            st, b = f
            return st, {}, json.dumps(b)
        return self._route(method, path, params or {}, body, headers)

    def _place(self, b):
        if any(o["clientRequestId"] == b["clientRequestId"] for o in self.orders):
            return None
        return self.add(clientRequestId=b["clientRequestId"], instrumentId=b["symbol"], buySell=b["side"],
                        orderType=b["orderType"], price=b["price"], quantity=b["quantity"], orderStatus="PD")

    def _route(self, method, path, params, b, headers):
        if path == "/api/v3/auth/requestOtp":
            return 200, {}, json.dumps({"code": 200, "msg": "OTP sent"})
        if path == "/api/v3/auth/token":
            if b.get("apiKey") != API_KEY or b.get("apiSecret") != API_SECRET or b.get("otp") != OTP:
                return 401, {}, json.dumps({"code": 401001, "msg": "invalid otp"})
            self.revoked = False
            return self._ok(self._issue())
        if path == "/api/v3/auth/refresh":
            r = b.get("refreshToken")
            if r in self.used_refresh or self.revoked:
                self.revoked = True                       # dùng lại ⇒ thu hồi TOÀN BỘ phiên
                self.valid_access.clear()
                self.live_refresh.clear()
                return 401, {}, json.dumps({"code": 401103, "msg": "refresh token reused"})
            if r not in self.live_refresh:
                return 401, {}, json.dumps({"code": 401101, "msg": "invalid refresh"})
            self.live_refresh.discard(r)
            self.used_refresh.add(r)
            return self._ok(self._issue())
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.valid_access:
            return 401, {}, json.dumps({"code": 401002, "msg": "unauthorized"})
        if path == "/api/v3/trading/order":
            if not verify(json.dumps(b), headers.get("X-Signature", "")):
                return 401, {}, json.dumps({"code": 401013, "msg": "bad signature"})
            if method == "POST":
                o = self._place(b)
                if o is None:
                    return 409, {}, json.dumps({"code": 409001, "msg": "duplicate clientRequestId"})
                return self._ok({"orderId": o["orderId"], "clientRequestId": o["clientRequestId"],
                                 "orderStatus": "PD"})
            o = next((x for x in self.orders if x["clientRequestId"] == b.get("clientRequestId")
                      or x["orderId"] == b.get("orderId")), None)
            if o is None:
                return 400, {}, json.dumps({"code": 400404, "msg": "order not found"})
            if method == "DELETE":
                if self.cancel_status == "CL":
                    o["cancelQty"] = o["quantity"] - o["filledQty"]
                o["orderStatus"] = self.cancel_status
                return self._ok({"orderId": o["orderId"], "clientRequestId": o["clientRequestId"],
                                 "orderStatus": "WC", "clientCancelId": b["clientCancelId"]})
            if method == "PUT":
                if "price" in b:
                    o["price"] = b["price"]
                if "quantity" in b:
                    o["quantity"] = b["quantity"]
                return self._ok({"orderId": o["orderId"], "clientRequestId": o["clientRequestId"],
                                 "orderStatus": "QU", "clientModifyId": b["clientModifyId"]})
        if path == "/api/v3/trading/orderBook":
            page, size = int(params["pageIndex"]), int(params["pageSize"])
            chunk = self.orders[(page - 1) * size: page * size]
            return self._ok({"accountNo": ACC, "orderList": chunk, "totalRecord": len(self.orders)})
        if path == "/api/v3/trading/position":
            return self._ok({"derivative": {"derOpenPositions": self.open_positions, "derClosePositions": []}})
        return 404, {}, json.dumps({"code": 404, "msg": "no route"})


class Wall:
    def __init__(self):
        self.t = EPOCH0

    def __call__(self):
        return self.t


def cfg(**kw) -> SsiConfig:
    base = dict(account_no=ACC, client_id="cid-1", order_symbol_format="krx", market_order_type="MTL",
                api_verified=True, poll_s=0, uncertain_resolve_s=15, fee_vnd_per_contract=20_000,
                tick_size=0.1, device_id="dev-1", page_size=1000)
    base.update(kw)
    return SsiConfig(**base)


def make(server=None, *, env=LIVE_ENV, live_flag=True, dry_run=False, config=None, login=True):
    srv = server or FakeSsi()
    clock, wall, notes = FakeClock(T0), Wall(), []
    srv.now = wall
    http = SsiHttp(transport=srv)
    tokens = SsiTokenManager(http, API_KEY, API_SECRET, now=wall, notify=notes.append)
    b = SsiBroker(http, tokens, config or cfg(), PRIVATE_KEY, clock=clock, live_flag=live_flag, env=env,
                  dry_run=dry_run, notify=notes.append)
    fills, updates = [], []
    b.on_fill(fills.append)
    b.on_order_update(updates.append)
    if login:
        run(b.connect())
        assert run(b.submit_otp(OTP))
        srv.calls.clear()
        notes.clear()
    return b, srv, clock, wall, fills, updates, notes


def order(cid="E1", side=Side.BUY, qty=1, otype=OrderType.LIMIT, price=1350.0, tag="entry"):
    return Order(cid, SYM, side, qty, otype, T0, price=price, broker="ssi", tag=tag)


def posts(srv, path="/api/v3/trading/order", method="POST"):
    return [c for c in srv.calls if c["path"] == path and c["method"] == method]


# --------------------------------------------------------------------------- ký + HTTP ----

class TestSigningHttp:
    def test_rsa_signature_verifies_with_public_key(self):
        data = json.dumps({"a": 1, "b": "x"})
        assert verify(data, rsa_sign(data, PRIVATE_KEY))
        assert not verify(data + " ", rsa_sign(data, PRIVATE_KEY))

    def test_bad_key_never_echoed(self):
        with pytest.raises(ValueError) as e:
            rsa_sign("x", base64.b64encode(b"<RSAKeyValue>secret-junk</RSAKeyValue>").decode())
        assert "secret-junk" not in str(e.value)

    def test_load_private_key_strips_whitespace(self, tmp_path):
        p = tmp_path / "k.txt"
        p.write_text("\n" + PRIVATE_KEY + "\n", encoding="utf-8")
        assert load_private_key(p) == PRIVATE_KEY

    def test_signed_request_sends_exact_signed_bytes(self):
        srv = FakeSsi()
        http = SsiHttp(transport=srv)
        run(http.request("POST", "/api/v3/trading/order", body={"z": 1, "a": "b"}, token="T",
                         private_key=PRIVATE_KEY))
        c = srv.calls[0]
        assert verify(c["content"], c["headers"]["X-Signature"])
        assert c["headers"]["Authorization"] == "Bearer T" and c["headers"]["Content-Type"] == "application/json"

    def test_error_inside_http_200(self):
        srv = FakeSsi()
        srv.fail_next = [(200, {"code": 400123, "msg": "Số lượng không hợp lệ"})]
        r = run(SsiHttp(transport=srv).request("GET", "/x"))
        assert r.error is not None and r.error.code == "400123" and "Số lượng" in str(r.error)

    def test_success_code_and_no_content(self):
        srv = FakeSsi()
        srv.fail_next = [(200, {"code": 200, "msg": "ok", "data": {"v": 1}}), (204, {})]
        h = SsiHttp(transport=srv)
        assert run(h.request("GET", "/x")).error is None
        r = run(h.request("GET", "/y"))
        assert r.error is None and r.status == 204

    @pytest.mark.parametrize("fail", [(429, {"code": 429, "msg": "slow"}), (503, {"msg": "down"})])
    def test_no_retry(self, fail):
        srv = FakeSsi()
        srv.fail_next = [fail]
        r = run(SsiHttp(transport=srv).request("GET", "/x"))
        assert r.error is not None and len(srv.calls) == 1

    def test_network_error(self):
        srv = FakeSsi()
        srv.fail_next = [TimeoutError("t")]
        with pytest.raises(SsiTransportError):
            run(SsiHttp(transport=srv).request("GET", "/x"))
        assert len(srv.calls) == 1

    def test_error_message_is_safe(self):
        e = SsiApiError(400, {"code": 1, "msg": "m" * 900, "accessToken": "ACCESS-SECRET"})
        assert "ACCESS-SECRET" not in str(e) and len(str(e)) < 300


# --------------------------------------------------------------------------- token ----

class TestTokens:
    def mgr(self, srv=None):
        srv = srv or FakeSsi()
        wall, notes = Wall(), []
        srv.now = wall
        return SsiTokenManager(SsiHttp(transport=srv), API_KEY, API_SECRET, now=wall, notify=notes.append), \
            srv, wall, notes

    def test_otp_login_and_get(self):
        m, srv, wall, notes = self.mgr()
        assert run(m.request_otp())
        assert m.awaiting_otp and any("/otp ssi" in n for n in notes)
        assert run(m.submit_otp("000000")) is False
        assert run(m.submit_otp(OTP)) is True and not m.awaiting_otp
        assert run(m.get()) == "ACCESS-1"
        login = [c for c in srv.calls if c["path"] == "/api/v3/auth/token"][-1]
        assert login["body"] == {"apiKey": API_KEY, "apiSecret": API_SECRET, "otp": OTP}

    def test_no_token_raises(self):
        m, *_ = self.mgr()
        with pytest.raises(BrokerError, match="OTP"):
            run(m.get())

    def test_refresh_before_expiry_uses_refresh_token_once(self):
        m, srv, wall, _ = self.mgr()
        run(m.submit_otp(OTP))
        wall.t += 3600 - 10                                         # sắp hết hạn (lệch an toàn 30s)
        assert run(m.get()) == "ACCESS-2"
        assert run(m.get()) == "ACCESS-2"
        refreshes = [c for c in srv.calls if c["path"] == "/api/v3/auth/refresh"]
        assert [c["body"]["refreshToken"] for c in refreshes] == ["REFRESH-1"]

    def test_concurrent_callers_single_refresh(self):
        m, srv, wall, _ = self.mgr()
        run(m.submit_otp(OTP))
        wall.t += 3590

        async def both():
            return await asyncio.gather(m.get(), m.get(), m.get(force_refresh=False))
        assert set(run(both())) == {"ACCESS-2"}
        assert len([c for c in srv.calls if c["path"] == "/api/v3/auth/refresh"]) == 1

    def test_concurrent_forced_refresh_single_request(self):
        """Hai request cùng bị 401 cùng lúc ⇒ chỉ MỘT lần refresh."""
        m, srv, wall, _ = self.mgr()
        run(m.submit_otp(OTP))

        async def both():
            return await asyncio.gather(m.get(force_refresh=True), m.get(force_refresh=True))
        assert set(run(both())) == {"ACCESS-2"}
        assert len([c for c in srv.calls if c["path"] == "/api/v3/auth/refresh"]) == 1

    def test_otp_format_checked_locally(self):
        m, srv, *_ = self.mgr()
        assert run(m.submit_otp("12ab")) is False and run(m.submit_otp("123")) is False
        assert not [c for c in srv.calls if c["path"] == "/api/v3/auth/token"]

    def test_refresh_network_error_never_reuses_token(self):
        """Lỗi mạng khi refresh: KHÔNG biết server đã tiêu refresh token chưa ⇒ coi như đã tiêu."""
        m, srv, wall, notes = self.mgr()
        run(m.submit_otp(OTP))
        wall.t += 3590
        srv.fail_next = [TimeoutError("t")]
        with pytest.raises(BrokerError):
            run(m.get())
        assert m.awaiting_otp and any("/otp ssi" in n for n in notes)      # xin OTP ngay, không chờ
        with pytest.raises(BrokerError, match="OTP"):
            run(m.get(force_refresh=True))
        sent = [c["body"]["refreshToken"] for c in srv.calls if c["path"] == "/api/v3/auth/refresh"]
        assert sent == ["REFRESH-1"]                                  # không bao giờ gửi lại
        assert m.awaiting_otp and any("/otp ssi" in n for n in notes)

    def test_reauth_code_clears_and_requests_otp(self):
        m, srv, wall, notes = self.mgr()
        run(m.submit_otp(OTP))
        srv.live_refresh.clear()                                      # server đã thu hồi
        wall.t += 3590
        with pytest.raises(BrokerError, match="OTP"):
            run(m.get())
        assert m.awaiting_otp

    def test_refresh_token_expired_needs_otp(self):
        m, srv, wall, _ = self.mgr()
        run(m.submit_otp(OTP))
        wall.t += 9 * 3600
        with pytest.raises(BrokerError, match="OTP"):
            run(m.get())
        assert not [c for c in srv.calls if c["path"] == "/api/v3/auth/refresh"]

    def test_force_refresh_after_401(self):
        m, srv, wall, _ = self.mgr()
        run(m.submit_otp(OTP))
        assert run(m.get(force_refresh=True)) == "ACCESS-2"

    def test_secrets_never_logged(self, caplog):
        caplog.set_level(logging.DEBUG)
        m, srv, wall, notes = self.mgr()
        run(m.request_otp())
        run(m.submit_otp(OTP))
        wall.t += 3590
        run(m.get())
        text = caplog.text + repr(m) + "".join(notes)
        for s in (API_SECRET, OTP, "ACCESS-1", "REFRESH-1", "ACCESS-2", "REFRESH-2"):
            assert s not in text


# --------------------------------------------------------------------------- cấu hình ----

class TestConfig:
    ENV = {"SSI_ACCOUNT": ACC, "SSI_CLIENT_ID": "cid-1"}

    def settings(self, **ssi):
        d = {"order_symbol_format": None, "market_order_type": None, "api_verified": False}
        d.update(ssi)
        return Settings({"brokers": {"ssi": d}, "contract": {"tick_size_points": 0.1},
                         "fees": {"broker_fee_vnd_per_contract": 2700, "exchange_fee_vnd_per_contract": 2700,
                                  "clearing_fee_vnd_per_contract": 2550, "tax_vnd_per_contract": 0}})

    def test_dry_run_tolerates_nulls(self):
        c = SsiConfig.from_settings(self.settings(), env={}, live=False)
        assert c.account_no is None and c.order_symbol_format is None

    @pytest.mark.parametrize("missing", ["order_symbol_format", "market_order_type"])
    def test_live_requires_verified_unknowns(self, missing):
        vals = {"order_symbol_format": "krx", "market_order_type": "MTL", "api_verified": True}
        vals[missing] = None
        with pytest.raises(MissingSetting):
            SsiConfig.from_settings(self.settings(**vals), env=self.ENV, live=True)

    def test_live_requires_api_verified(self):
        s = self.settings(order_symbol_format="krx", market_order_type="MTL")
        with pytest.raises(LiveTradingNotAllowed, match="api_verified"):
            SsiConfig.from_settings(s, env=self.ENV, live=True)

    def test_live_requires_derivative_account(self):
        s = self.settings(order_symbol_format="krx", market_order_type="MTL", api_verified=True)
        with pytest.raises(MissingSetting, match="SSI_ACCOUNT"):
            SsiConfig.from_settings(s, env={"SSI_CLIENT_ID": "x"}, live=True)
        with pytest.raises(ValueError, match="8"):
            SsiConfig.from_settings(s, env={"SSI_ACCOUNT": "0001231", "SSI_CLIENT_ID": "x"}, live=True)
        c = SsiConfig.from_settings(s, env=self.ENV, live=True)
        assert c.account_no == ACC and c.fee_vnd_per_contract == 7950 and c.device_id

    @pytest.mark.parametrize("field,bad", [("order_symbol_format", "vn30"), ("market_order_type", "LO")])
    def test_invalid_values_rejected(self, field, bad):
        vals = {"order_symbol_format": "krx", "market_order_type": "MTL", "api_verified": True}
        vals[field] = bad
        with pytest.raises(ValueError):
            SsiConfig.from_settings(self.settings(**vals), env=self.ENV, live=True)


# --------------------------------------------------------------------------- payload / trạng thái ----

class TestPayload:
    def test_limit(self):
        p = build_place_payload(order(), cfg())
        assert p == {"accountNo": ACC, "symbol": KRX, "side": "B", "quantity": 1, "price": "1350.0",
                     "orderType": "LO", "clientRequestId": "E1", "deviceId": "dev-1",
                     "userAgent": p["userAgent"]}

    def test_market_uses_configured_type_and_zero_price(self):
        p = build_place_payload(order(side=Side.SELL, otype=OrderType.MARKET, price=None), cfg(market_order_type="MP"))
        assert p["side"] == "S" and p["orderType"] == "MP" and p["price"] == "0"

    def test_symbol_name_format(self):
        assert build_place_payload(order(), cfg(order_symbol_format="name"))["symbol"] == SYM

    def test_unknown_format_refused(self):
        with pytest.raises(ValueError):
            build_place_payload(order(), cfg(order_symbol_format=None))

    def test_off_tick_and_stop(self):
        with pytest.raises(ValueError, match="bước giá"):
            build_place_payload(order(price=1350.05), cfg())
        with pytest.raises(ValueError):
            build_place_payload(order(otype=OrderType.STOP, price=1340.0), cfg())


@pytest.mark.parametrize("code,qty,filled,canc,expected", [
    ("PD", 2, 0, 0, OrderStatus.PENDING), ("WA", 2, 0, 0, OrderStatus.PENDING),
    ("SD", 2, 0, 0, OrderStatus.PENDING), ("QU", 2, 0, 0, OrderStatus.PENDING),
    ("WM", 2, 1, 0, OrderStatus.PARTIALLY_FILLED), ("WC", 2, 0, 0, OrderStatus.PENDING),
    ("PF", 2, 1, 0, OrderStatus.PARTIALLY_FILLED), ("FF", 2, 2, 0, OrderStatus.FILLED),
    ("FFPC", 2, 1, 1, OrderStatus.CANCELLED), ("CL", 2, 0, 2, OrderStatus.CANCELLED),
    ("EX", 2, 0, 2, OrderStatus.CANCELLED), ("RJ", 2, 0, 0, OrderStatus.REJECTED),
    ("ERR", 2, 0, 0, OrderStatus.REJECTED), ("IAV", 2, 0, 0, OrderStatus.PENDING),
    ("ZZZ", 2, 2, 0, OrderStatus.FILLED), ("ZZZ", 2, 0, 0, OrderStatus.PENDING),
])
def test_status_mapping(code, qty, filled, canc, expected):
    assert map_order_status(code, qty, filled, canc) is expected


# --------------------------------------------------------------------------- chốt an toàn ----

class TestGuards:
    @pytest.mark.parametrize("env,flag", [({"TRADING_MODE": "paper"}, True), (LIVE_ENV, False), ({}, True)])
    def test_requires_live_mode_and_flag(self, env, flag):
        b, srv, *_ = make(env=env, live_flag=flag)
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order()))
        assert not posts(srv)

    def test_requires_api_verified(self):
        b, srv, *_ = make(config=cfg(api_verified=False))
        with pytest.raises(LiveTradingNotAllowed, match="api_verified"):
            run(b.place(order()))
        assert not posts(srv)

    def test_no_token(self):
        b, srv, *_ = make(login=False)
        with pytest.raises(BrokerError, match="OTP"):
            run(b.place(order()))
        assert not posts(srv)

    def test_guard_every_call(self):
        b, srv, *_ = make()
        run(b.place(order("E1")))
        b.env = {}
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order("E2")))
        with pytest.raises(LiveTradingNotAllowed):
            run(b.cancel("E1"))
        assert len(posts(srv)) == 1 and not posts(srv, method="DELETE")

    def test_stop_guarded(self):
        b, *_ = make(env={})
        with pytest.raises(LiveTradingNotAllowed):
            run(b.place(order("S1", side=Side.SELL, otype=OrderType.STOP, price=1340.0, tag="stop")))

    def test_dry_run_logs_only(self, caplog):
        caplog.set_level(logging.INFO)
        b, srv, *_ = make(dry_run=True, env={}, live_flag=False, login=False)
        o = run(b.place(order()))
        assert o.status is OrderStatus.REJECTED and "dry-run" in o.reject_reason
        assert not [c for c in srv.calls if c["method"] in ("POST", "PUT", "DELETE")]
        assert b.dry_log[-1]["body"]["clientRequestId"] == "E1" and KRX in caplog.text


# --------------------------------------------------------------------------- đặt lệnh ----

class TestPlace:
    def test_signed_with_client_request_id(self):
        b, srv, clock, wall, fills, updates, _ = make()
        o = run(b.place(order()))
        c = posts(srv)[0]
        assert verify(c["content"], c["headers"]["X-Signature"])
        assert c["body"]["clientRequestId"] == "E1" and c["headers"]["Authorization"] == "Bearer ACCESS-1"
        assert o.status is OrderStatus.PENDING and o.broker_order_id == srv.by_cid("E1")["orderId"]
        assert updates[-1].status is OrderStatus.PENDING

    def test_top_level_response_also_accepted(self):
        srv = FakeSsi()
        srv.wrap_data = False
        b, srv, *_ = make(srv)
        assert run(b.place(order())).broker_order_id == srv.by_cid("E1")["orderId"]

    def test_idempotent(self):
        b, srv, *_ = make()
        run(b.place(order()))
        run(b.place(order()))
        assert len(posts(srv)) == 1

    def test_business_reject(self):
        b, srv, clock, wall, fills, updates, _ = make()
        srv.fail_next = [(200, {"code": 400201, "msg": "Vượt sức mua"})]
        o = run(b.place(order()))
        assert o.status is OrderStatus.REJECTED and "400201" in o.reject_reason
        assert updates[-1].status is OrderStatus.REJECTED

    def test_unauthorized_rejects_and_next_call_refreshes_token(self):
        b, srv, *_, notes = make()
        srv.valid_access.clear()                                         # access token bị server bỏ
        o = run(b.place(order()))
        assert o.status is OrderStatus.REJECTED and "401" in o.reject_reason
        assert len(posts(srv)) == 1                                      # không tự gửi lại lệnh
        assert run(b.ping()) is True
        assert srv.calls[-1]["headers"]["Authorization"] == "Bearer ACCESS-2"

    def test_unauthorized_order_then_next_order_refreshes(self):
        b, srv, *_ = make()
        srv.valid_access.clear()
        run(b.place(order("E1")))
        o = run(b.place(order("E2")))
        assert o.status is OrderStatus.PENDING and posts(srv)[-1]["headers"]["Authorization"] == "Bearer ACCESS-2"

    @pytest.mark.parametrize("failure", [TimeoutError("t"), ConnectionError("x"), (502, {"msg": "bad gw"})])
    def test_uncertain_found_by_client_request_id(self, failure):
        b, srv, clock, wall, fills, updates, notes = make()
        srv.create_on_failure = True
        srv.fail_next = [failure]
        o = run(b.place(order()))
        assert o.status is OrderStatus.PENDING and o.broker_order_id is None
        run(b.sync())
        assert b.order("E1").broker_order_id == srv.by_cid("E1")["orderId"]
        assert len(posts(srv)) == 1

    def test_uncertain_waits_for_window_before_rejecting(self):
        b, srv, clock, wall, fills, updates, _ = make()
        srv.fail_next = [TimeoutError("t")]
        run(b.place(order()))
        clock.advance(seconds=5)
        run(b.sync())
        assert b.order("E1").status is OrderStatus.PENDING              # chưa hết cửa sổ: còn chờ
        srv.add(clientRequestId="E1")                                    # hiện ra muộn
        run(b.sync())
        assert b.order("E1").broker_order_id == srv.by_cid("E1")["orderId"]

    def test_uncertain_not_found_rejected_after_window(self):
        b, srv, clock, wall, fills, updates, _ = make()
        srv.fail_next = [TimeoutError("t")]
        run(b.place(order()))
        clock.advance(seconds=16)
        run(b.sync())
        assert b.order("E1").status is OrderStatus.REJECTED and "sổ lệnh" in b.order("E1").reject_reason
        assert len(posts(srv)) == 1

    def test_late_appearance_after_reject_is_still_counted(self):
        """Bị coi là chưa tới sàn nhưng sau đó vẫn hiện trong sổ ⇒ theo dõi như lệnh ngoài, net position đúng."""
        b, srv, clock, wall, fills, updates, notes = make()
        srv.fail_next = [TimeoutError("t")]
        run(b.place(order()))
        clock.advance(seconds=16)
        run(b.sync())
        late = srv.add(clientRequestId="E1")
        srv.fill(late, 1, 1350.0)
        run(b.sync())
        assert [(f.qty, f.price, f.side) for f in fills] == [(1, 1350.0, Side.BUY)]
        assert any("E1" in n for n in notes)

    def test_duplicate_409_means_it_exists(self):
        b, srv, clock, wall, fills, updates, _ = make()
        srv.add(clientRequestId="E1")                                    # đã tới sàn từ lần gửi trước
        o = run(b.place(order()))
        assert o.status is OrderStatus.PENDING
        run(b.sync())
        assert b.order("E1").broker_order_id == srv.by_cid("E1")["orderId"]


# --------------------------------------------------------------------------- đồng bộ ----

class TestSync:
    def test_fills_from_cumulative_book(self):
        b, srv, clock, wall, fills, updates, _ = make()
        run(b.place(order(qty=2)))
        o = srv.by_cid("E1")
        srv.fill(o, 1, 1350.0)
        run(b.sync())
        srv.fill(o, 2, 1350.5)
        run(b.sync())
        run(b.sync())
        assert [(f.qty, f.price, f.fee_vnd) for f in fills] == [(1, 1350.0, 20_000), (1, 1351.0, 20_000)]
        assert [u.status for u in updates] == [OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED]

    def test_book_paging(self):
        b, srv, clock, *_ = make(config=cfg(page_size=2))
        for i in range(5):
            srv.add(clientRequestId=f"X{i}")
        run(b.sync())
        pages = [c["params"]["pageIndex"] for c in srv.calls if c["path"] == "/api/v3/trading/orderBook"]
        assert pages == [1, 2, 3]
        assert len(run(b.open_orders())) == 5

    def test_book_query_params(self):
        b, srv, *_ = make()
        run(b.sync())
        c = [c for c in srv.calls if c["path"] == "/api/v3/trading/orderBook"][0]
        assert c["params"]["accountNo"] == ACC and c["params"]["from"] == "2026/11/09" == c["params"]["to"]

    def test_external_and_preexisting(self):
        srv = FakeSsi()
        pre = srv.add(clientRequestId="APP1", quantity=2)
        srv.fill(pre, 1, 1350.0)                                          # có từ trước khi bot chạy
        b, srv, clock, wall, fills, updates, notes = make(srv)
        assert fills == []
        srv.fill(pre, 2, 1350.5)
        ext = srv.add(clientRequestId="APP2", buySell="S")
        srv.fill(ext, 1, 1349.0)                                          # đặt tay, khớp giữa hai lần đọc
        run(b.sync())
        assert sorted((f.side.value, f.qty, f.price) for f in fills) == [("BUY", 1, 1351.0), ("SELL", 1, 1349.0)]

    def test_ping(self):
        b, srv, clock, *_ = make(config=cfg(poll_s=2))
        assert run(b.ping()) is True
        run(b.ping())
        assert len([c for c in srv.calls if c["path"].endswith("orderBook")]) == 1
        srv.fail_next = [ConnectionError("x")]
        clock.advance(seconds=2)
        assert run(b.ping()) is False


# --------------------------------------------------------------------------- huỷ / sửa ----

class TestCancelModify:
    def test_cancel_by_client_request_id(self):
        b, srv, clock, wall, fills, updates, _ = make()
        run(b.place(order()))
        c = run(b.cancel("E1"))
        d = posts(srv, method="DELETE")[0]
        assert d["body"]["clientRequestId"] == "E1" and d["body"]["accountNo"] == ACC and d["body"]["clientCancelId"]
        assert verify(d["content"], d["headers"]["X-Signature"])
        assert c.status is OrderStatus.CANCELLED                       # xác nhận từ sổ lệnh (CL), không từ "WC"

    def test_cancel_not_confirmed_yet_stays_working(self):
        b, srv, clock, wall, fills, updates, _ = make()
        srv.cancel_status = "WC"
        run(b.place(order()))
        assert run(b.cancel("E1")).status is OrderStatus.PENDING          # sàn chưa xác nhận huỷ

    def test_dry_run_cancel_logged_not_sent(self):
        b, srv, *_ = make(dry_run=True, env={}, live_flag=False)
        srv.add(clientRequestId="APP9")
        cid = [o.client_order_id for o in run(b.open_orders())][0]
        with pytest.raises(BrokerError, match="dry-run"):
            run(b.cancel(cid))
        assert not posts(srv, method="DELETE") and b.dry_log[-1]["method"] == "DELETE"
        assert b.dry_log[-1]["body"]["orderId"] == srv.orders[0]["orderId"]   # lệnh ngoài bot: theo orderId

    def test_cancel_errors(self):
        b, srv, *_ = make()
        with pytest.raises(BrokerError):
            run(b.cancel("NOPE"))
        run(b.place(order()))
        srv.fail_next = [(200, {"code": 400301, "msg": "không huỷ trong ATC"})]
        with pytest.raises(SsiApiError, match="400301"):
            run(b.cancel("E1"))

    def test_modify_price_and_qty_are_separate_calls(self):
        b, srv, clock, wall, fills, updates, _ = make()
        run(b.place(order(qty=2)))
        m = run(b.modify("E1", price=1351.0, qty=3))
        puts = posts(srv, method="PUT")
        assert [("price" in p["body"], "quantity" in p["body"]) for p in puts] == [(True, False), (False, True)]
        assert puts[0]["body"]["price"] == "1351.0" and puts[1]["body"]["quantity"] == 3
        assert all(p["body"]["clientRequestId"] == "E1" for p in puts)
        assert m.price == 1351.0 and m.qty == 3 and m.client_order_id == "E1"


# --------------------------------------------------------------------------- stop do bot giữ ----

class TestLocalStop:
    def test_trigger_once(self):
        b, srv, clock, wall, fills, updates, _ = make()
        s = run(b.place(order("S1", side=Side.SELL, otype=OrderType.STOP, price=1340.0, tag="stop")))
        assert s.broker_order_id.startswith("local") and not posts(srv)
        run(b.on_tick(Tick(SYM, T0, last=1341.0)))
        run(b.on_tick(Tick(SYM, T0, last=1340.0)))
        run(b.on_tick(Tick(SYM, T0, last=1339.0)))
        p = posts(srv)
        assert len(p) == 1 and p[0]["body"]["side"] == "S" and p[0]["body"]["orderType"] == "MTL"
        assert p[0]["body"]["clientRequestId"] == "S1"

    def test_cancel_local(self):
        b, srv, *_ = make()
        run(b.place(order("S1", side=Side.SELL, otype=OrderType.STOP, price=1340.0, tag="stop")))
        assert run(b.cancel("S1")).status is OrderStatus.CANCELLED and not posts(srv, method="DELETE")


# --------------------------------------------------------------------------- vị thế ----

class TestPositions:
    def test_open_derivative_positions(self):
        b, srv, *_ = make()
        srv.open_positions = [{"symbol": KRX, "long": 0, "short": 2, "net": -2, "bidAvgPrice": 0,
                               "askAvgPrice": 1352.0}]
        p = run(b.positions())[SYM]
        assert p.qty == -2 and p.avg_price == 1352.0 and p.broker == "ssi"
        c = [c for c in srv.calls if c["path"] == "/api/v3/trading/position"][0]
        assert c["params"] == {"clientId": "cid-1", "querySummary": "true", "accountNo": ACC}

    def test_net_mismatch_raises(self):
        b, srv, *_ = make()
        srv.open_positions = [{"symbol": KRX, "long": 1, "short": 0, "net": -1}]
        with pytest.raises(BrokerError, match="net"):
            run(b.positions())

    def test_flat_and_unknown_symbol(self):
        b, srv, *_ = make()
        srv.open_positions = [{"symbol": KRX, "long": 1, "short": 1, "net": 0, "bidAvgPrice": 1, "askAvgPrice": 1},
                              {"symbol": "VN100F2611", "long": 1, "short": 0, "net": 1}]
        assert run(b.positions()) == {}


# --------------------------------------------------------------------------- stream TRADING ----

class TestStream:
    def frame(self, cid, oid, filled, avg, status, notify, event="orderEvent", qty=2):
        return {"channel": "TRADING", "topic": f"order.{ACC}",
                "data": {"eventType": event, "notifyId": notify, "accountNo": ACC, "clientRequestId": cid,
                         "orderId": oid, "symbol": KRX, "side": "B", "orderType": "LO", "price": "1350",
                         "quantity": qty, "filledQty": filled, "cancelQty": 0, "avgPrice": avg,
                         "orderStatus": status}}

    def test_event_applies_immediately_and_dedupes_with_rest(self):
        b, srv, clock, wall, fills, updates, _ = make()
        run(b.place(order(qty=2)))
        o = srv.by_cid("E1")
        f = self.frame("E1", o["orderId"], 1, "1350.0", "PF", "N1")
        b.on_stream_frame(f)
        b.on_stream_frame(f)                                             # trùng notifyId
        assert [(x.qty, x.price) for x in fills] == [(1, 1350.0)]
        srv.fill(o, 1, 1350.0)
        run(b.sync())                                                    # REST thấy đúng mức đó ⇒ không đếm đôi
        assert len(fills) == 1
        assert b.last_notify_id == "N1"

    def test_match_event_triggers_resync_only(self):
        b, srv, clock, wall, fills, updates, _ = make(config=cfg(poll_s=60))
        run(b.place(order()))
        run(b.ping())
        o = srv.by_cid("E1")
        srv.fill(o, 1, 1350.0)
        b.on_stream_frame({"channel": "TRADING", "topic": f"order.{ACC}",
                           "data": {"eventType": "orderMatchEvent", "notifyId": "N2", "clientRequestId": "E1",
                                    "orderId": o["orderId"], "matchQty": 1, "matchPrice": "1350"}})
        assert fills == []                                               # delta không cộng thẳng
        run(b.ping())                                                    # chưa tới poll_s nhưng phải đọc lại ngay
        assert [(x.qty, x.price) for x in fills] == [(1, 1350.0)]

    def test_portfolio_event_recorded(self):
        b, *_ = make()
        b.on_stream_frame({"channel": "TRADING", "topic": f"portfolio.{ACC}",
                           "data": {"eventType": "clientPortfolioEvent", "notifyId": "P1", "accountNo": ACC,
                                    "symbol": KRX, "longQty": 0, "shortQty": 1, "net": -1}})
        assert b.stream_net == {SYM: -1}

    def test_duplicates_counted_and_stale_event_cannot_revive(self):
        b, srv, clock, wall, fills, updates, _ = make()
        run(b.place(order(qty=2)))
        oid = srv.by_cid("E1")["orderId"]
        b.on_stream_frame(self.frame("E1", oid, 0, "0", "QU", "N1"))
        b.on_stream_frame(self.frame("E1", oid, 0, "0", "CL", "N2"))
        b.on_stream_frame(self.frame("E1", oid, 0, "0", "QU", "N1"))     # phát lại
        b.on_stream_frame(self.frame("E1", oid, 0, "0", "QU", "N0"))     # sự kiện cũ đến muộn
        assert b.stream_duplicates == 1 and b.order("E1").status is OrderStatus.CANCELLED

    def test_foreign_account_and_junk_ignored(self):
        b, srv, clock, wall, fills, updates, _ = make()
        run(b.place(order(qty=2)))
        f = self.frame("E1", srv.by_cid("E1")["orderId"], 1, "1350", "PF", "F1")
        f["topic"] = "order.9999998"                                     # tài khoản khác
        b.on_stream_frame(f)
        b.on_stream_frame({"channel": "TRADING", "topic": "order.9999998", "data": {"eventType": "orderEvent"}})
        b.on_stream_frame({"weird": 1})
        b.on_stream_frame("not a dict")
        assert fills == []

    def test_stream_subscribes_and_resyncs_on_reconnect(self):
        b, srv, clock, wall, fills, updates, _ = make(config=cfg(poll_s=60))
        run(b.ping())
        sent, sessions = [], []

        class Tr:
            def __init__(self, frames):
                self.frames = list(frames)

            async def send(self, m):
                sent.append(json.loads(m))

            async def recv(self):
                if self.frames:
                    return self.frames.pop(0)
                raise ConnectionError("đóng")

            async def close(self):
                pass

        async def connector(url, headers):
            sessions.append(headers["Authorization"])
            if len(sessions) > 2:
                st.stopping = True
            return Tr([json.dumps({"method": "subscribe", "channel": "TRADING", "status": "success"})])

        async def no_sleep(_):
            return None
        st = SsiOrderStream(b.tokens, b, connector=connector, backoff_sleep=no_sleep, heartbeat_s=3600)
        run(st.run())
        assert sent[0] == {"method": "subscribe", "channel": "TRADING",
                           "topics": [f"order.{ACC}", f"portfolio.{ACC}"]}
        assert sessions[0] == "Bearer ACCESS-1" and len(sessions) >= 2
        assert b.resync_requested                                        # sau reconnect phải đọc lại sổ lệnh REST


def test_awaiting_otp_and_submit_routes_to_tokens():
    b, srv, clock, wall, fills, updates, notes = make(login=False)
    run(b.connect())
    assert b.awaiting_otp and any("/otp ssi" in n for n in notes)
    assert run(b.submit_otp(OTP)) and not b.awaiting_otp


def test_connect_rejects_wrong_account_suffix():
    b, *_ = make(config=cfg(account_no="0001231"), login=False)
    with pytest.raises(BrokerError, match="8"):
        run(b.connect())


def test_no_secret_in_logs_full_flow(caplog):
    caplog.set_level(logging.DEBUG)
    b, srv, clock, wall, fills, updates, notes = make(login=False)
    run(b.connect())
    run(b.submit_otp(OTP))
    run(b.place(order()))
    run(b.sync())
    text = caplog.text + repr(b) + repr(b.tokens) + "".join(notes)
    for s in (API_SECRET, OTP, "ACCESS-1", "REFRESH-1", PRIVATE_KEY[:40]):
        assert s not in text
