"""Adapter DNSE Open API V2 (PROMPTS P4.1) — broker thực thi chính.

Endpoint/field theo SDK chính thức `dnse` 0.5.0 (docs/broker_notes.md §1.3, §1.5). Lớp HTTP tự
viết mỏng trên `requests` (chạy trong thread) để kiểm soát hoàn toàn: KHÔNG có retry ẩn nào.

An toàn (CLAUDE.md mục 3, 5, 7):
- DNSE KHÔNG có khoá idempotency khi đặt lệnh ⇒ adapter không bao giờ gửi lại một lệnh. Timeout,
  lỗi mạng, HTTP 408/5xx ⇒ lệnh "không rõ": giữ PENDING, đối soát sổ lệnh (lệnh mới xuất hiện
  sau lúc gửi, khớp mã/chiều/loại/khối lượng/giá) — thấy thì nhận, quá `uncertain_resolve_s`
  không thấy ⇒ REJECTED. Gọi `place` lại cùng `client_order_id` chỉ trả lệnh cũ.
- Trước MỌI request đặt/sửa/huỷ: `assert_live_trading_allowed` (TRADING_MODE=live + `--live`),
  `brokers.dnse.api_verified` (bắt buộc ở production — các mục ❓ đã được kiểm bằng
  `scripts/dnse_trading_probe.py`), và trading token còn hạn.
- `dry_run=True`: chỉ log payload các lệnh thay đổi, KHÔNG gọi API thay đổi (đọc vẫn gọi).
- Trading token lấy bằng OTP email nhập tay: adapter gọi DNSE gửi OTP rồi nhắn người dùng trả lời
  `/otp <mã>` (Telegram → runner → `submit_otp`). Không có token ⇒ `ping()` = False: risk chặn mở
  vị thế, quá `broker_timeout_seconds` mà đang có vị thế ⇒ quy trình khẩn cấp. Sắp hết hạn
  (`trading_token_ttl_seconds` − `token_renew_before_s`) ⇒ tự xin OTP mới.
- Không bao giờ log api_secret, trading token, OTP; lỗi server chỉ giữ `code` + `message` cắt ngắn.

Trạng thái lệnh: chuỗi `orderStatus` chưa được kiểm (❓) ⇒ suy từ khối lượng
(fill/leave/canceled) trước, chuỗi sau (`map_order_status`); chuỗi lạ ⇒ coi còn sống và cảnh báo.
Khớp lệnh: thăm sổ lệnh mỗi `poll_s` (trong `ping()`), so `fillQuantity`/`averagePrice` từng
orderId ⇒ `on_fill` (trước) rồi `on_order_update`. Sửa lệnh = huỷ-đặt (orderId mới) ⇒ một
`client_order_id` có nhiều "leg"; khớp được cộng dồn qua các leg. Lệnh đặt tay trên app được
nhận theo dõi (`EXT<orderId>`) để net position của risk luôn đúng.

Stop: API lệnh thường của DNSE không có loại stop (broker_notes §4) ⇒ `OrderType.STOP` được bot
GIỮ (`broker_order_id = "local:…"`), `on_tick` thấy giá chạm thì gửi MTL cùng client id. Stop do
bot giữ mất feed = stop mù ⇒ đã có luật `flatten_on_feed_silence_seconds` (CLAUDE.md mục 5).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import urllib.parse
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

from brokers.base import BrokerAdapter, BrokerError, LiveTradingNotAllowed, assert_live_trading_allowed
from brokers.paper import FEE_KEYS
from core import contracts
from core.clock import Clock
from core.models import MAX_CLIENT_ORDER_ID_LEN, Fill, Order, OrderStatus, OrderType, Position, Side, Tick
from core.settings import MissingSetting, Settings

log = logging.getLogger(__name__)

BASE_URL_PROD = "https://openapi.dnse.com.vn"
BASE_URL_UAT = "https://openapi-uat.dnse.com.vn"
MARKET_TYPE = "DERIVATIVE"
ORDER_CATEGORY = "NORMAL"
ORDER_PARAMS = {"marketType": MARKET_TYPE, "orderCategory": ORDER_CATEGORY}
TOKEN_EXPIRED_CODES = frozenset({"INVALID_TRADING_TOKEN"})     # SDK `_http.TOKEN_EXPIRY_CODES`
EXTERNAL_PREFIX = "EXT"
LOCAL_PREFIX = "local:"

_SIDE_CODE = {Side.BUY: "NB", Side.SELL: "NS"}
_SIDE_FROM = {"NB": Side.BUY, "BUY": Side.BUY, "LONG": Side.BUY,
              "NS": Side.SELL, "SELL": Side.SELL, "SHORT": Side.SELL}
_TYPE_CODE = {OrderType.LIMIT: "LO", OrderType.MARKET: "MTL", OrderType.ATO: "ATO", OrderType.ATC: "ATC"}
_TYPE_FROM = {"LO": OrderType.LIMIT, "MTL": OrderType.MARKET, "MOK": OrderType.MARKET,
              "MAK": OrderType.MARKET, "ATO": OrderType.ATO, "ATC": OrderType.ATC}

# ❓ Giá trị orderStatus thật chưa được kiểm — chuẩn hoá: chữ thường, bỏ ký tự không phải chữ.
_ST_PENDING = frozenset({"new", "pending", "pendingnew", "pendingreplace", "pendingcancel", "sent",
                         "queued", "received", "accepted", "open", "waiting", "replacing", "cancelling"})
_ST_PARTIAL = frozenset({"partiallyfilled", "partialfilled", "partial", "partiallymatched"})
_ST_FILLED = frozenset({"filled", "fullyfilled", "matched", "fullymatched", "done"})
_ST_CANCELLED = frozenset({"cancelled", "canceled", "expired", "donefordday", "replaced", "killed"})
_ST_REJECTED = frozenset({"rejected", "error", "failed", "refused", "invalid"})
KNOWN_STATUS_WORDS = _ST_PENDING | _ST_PARTIAL | _ST_FILLED | _ST_CANCELLED | _ST_REJECTED

# Server DNSE nhét JSON chưa escape vào chuỗi `metadata` (SDK `_base_client._sanitize_response`).
_RE_UNESCAPED_METADATA = re.compile(r'"metadata":\s*"(\{.*?\})"', re.DOTALL)
_OTP_RE = re.compile(r"\d{4,8}")

Notify = Callable[[str], Any]
# (method, url, params, json, headers, timeout) -> (status, headers, text). Không được tự retry.
Transport = Callable[[str, str, Optional[dict], Optional[dict], dict, float],
                     Awaitable[tuple[int, Mapping[str, str], str]]]


# ------------------------------------------------------------------ ký + HTTP ----

def sign_headers(method: str, path: str, api_key: str, api_secret: str, *,
                 date_value: Optional[str] = None, nonce: Optional[str] = None) -> dict:
    """Header HMAC-SHA256 như `dnse._hmac_signer.build_signature_headers` (SDK 0.5.0).

    Ký ĐƯỜNG DẪN KHÔNG kèm query (SDK gửi query riêng) — ❓ server chấp nhận cách nào được chốt
    bằng `scripts/dnse_history_probe.py` / `dnse_trading_probe.py`.
    """
    date_value = date_value or datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    nonce = nonce or uuid.uuid4().hex
    sig_string = f"(request-target): {method.lower()} {path}\ndate: {date_value}\nnonce: {nonce}"
    raw = hmac.new(api_secret.encode(), sig_string.encode(), hashlib.sha256).digest()
    sig = urllib.parse.quote(base64.b64encode(raw).decode(), safe="")
    return {
        "x-api-key": api_key,
        "date": date_value,
        "X-Signature": (f'Signature keyId="{api_key}",algorithm="hmac-sha256",'
                        f'headers="(request-target) date",signature="{sig}",nonce="{nonce}"'),
        "User-Agent": "future-trading-bot",
        "Accept": "application/json",
    }


def sanitize_metadata(text: str) -> str:
    return _RE_UNESCAPED_METADATA.sub(lambda m: '"metadata": ' + json.dumps(m.group(1)), text)


class DnseTransportError(BrokerError):
    """Lỗi mạng/timeout — KHÔNG biết server đã xử lý request hay chưa."""


class DnseApiError(BrokerError):
    """Server trả mã lỗi. Chỉ giữ `code` + `message` cắt ngắn (không chép nguyên body)."""

    def __init__(self, status: int, body: Any):
        self.status = int(status)
        b = body if isinstance(body, dict) else {}
        self.code = str(b.get("code") or b.get("errorCode") or "")
        self.message = str(b.get("message") or b.get("msg") or b.get("error") or "")[:200]
        super().__init__(f"HTTP {self.status} {self.code}: {self.message}".strip())

    @property
    def token_expired(self) -> bool:
        return self.status == 401 and self.code in TOKEN_EXPIRED_CODES


@dataclass(frozen=True)
class DnseResponse:
    status: int
    body: Any
    headers: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _requests_transport(timeout_default: float) -> Transport:
    state: dict = {}

    async def transport(method, url, params, json_body, headers, timeout):
        def go():
            import requests
            sess = state.get("s")
            if sess is None:
                sess = state["s"] = requests.Session()
            r = sess.request(method, url, params=params, json=json_body, headers=headers,
                             timeout=timeout or timeout_default)
            return r.status_code, dict(r.headers), r.text
        return await asyncio.to_thread(go)

    return transport


class DnseHttp:
    """Client REST ký HMAC. MỘT request = MỘT lần gửi: không retry 429/5xx/timeout."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL_PROD,
                 timeout: float = 10.0, transport: Optional[Transport] = None):
        if not api_key or not api_secret:
            raise ValueError("Thiếu DNSE api_key/api_secret")
        self._key, self._secret = api_key, api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport or _requests_transport(self.timeout)

    def __repr__(self) -> str:
        return f"DnseHttp(base_url={self.base_url!r})"

    async def request(self, method: str, path: str, *, params: Optional[dict] = None,
                      json_body: Optional[dict] = None, trading_token: Optional[str] = None) -> DnseResponse:
        method = method.upper()
        headers = sign_headers(method, path, self._key, self._secret)
        if trading_token:
            headers["trading-token"] = trading_token
        try:
            status, rheaders, text = await self._transport(method, self.base_url + path, params,
                                                           json_body, headers, self.timeout)
        except Exception as exc:
            raise DnseTransportError(f"{method} {path}: {type(exc).__name__}: {str(exc)[:120]}") from exc
        body: Any = None
        if text and text.strip():
            try:
                body = json.loads(sanitize_metadata(text))
            except ValueError:
                body = {"raw_text": text[:300]}
        log.debug("dnse %s %s → %s", method, path, status)
        return DnseResponse(int(status), body, dict(rheaders or {}))


# ------------------------------------------------------------------ cấu hình ----

@dataclass(frozen=True)
class DnseConfig:
    account_no: Optional[str]
    loan_package_id: Optional[int]
    base_url: str = BASE_URL_PROD
    otp_type: str = "email_otp"
    token_ttl_s: Optional[float] = None
    token_renew_before_s: float = 300.0
    otp_resend_s: float = 120.0
    api_verified: bool = False
    timeout_s: float = 10.0
    poll_s: float = 1.0
    uncertain_resolve_s: float = 15.0
    fee_vnd_per_contract: float = 0.0
    tick_size: float = 0.1
    positions_page_size: int = 100

    @property
    def is_production(self) -> bool:
        return self.base_url.rstrip("/") != BASE_URL_UAT

    @classmethod
    def from_settings(cls, s: Settings, env: Optional[Mapping[str, str]] = None, *,
                      live: bool) -> "DnseConfig":
        """`live=True`: mọi giá trị phải được điền (null ⇒ MissingSetting) và production phải
        `api_verified: true` (CLAUDE.md mục 2 + mục 8: ❓ phải được kiểm trước khi dựa vào)."""
        env = os.environ if env is None else env
        g = lambda k, d=None: s.get(f"brokers.dnse.{k}", d)  # noqa: E731
        account = str(env.get("DNSE_ACCOUNT_NO") or "").strip() or None
        base_url = str(g("base_url") or BASE_URL_PROD).rstrip("/")
        loan, ttl = g("loan_package_id"), g("trading_token_ttl_seconds")
        fee_vals = [s.get(f"fees.{k}") for k in FEE_KEYS]
        verified = bool(g("api_verified", False))
        if live:
            if not account:
                raise MissingSetting("DNSE_ACCOUNT_NO (.env) — số tiểu khoản phái sinh DNSE")
            loan = s.require("brokers.dnse.loan_package_id")
            ttl = s.require("brokers.dnse.trading_token_ttl_seconds")
            fee_vals = [s.require(f"fees.{k}") for k in FEE_KEYS]
            if base_url != BASE_URL_UAT and not verified:
                raise LiveTradingNotAllowed(
                    "brokers.dnse.api_verified=false — chạy scripts/dnse_trading_probe.py, đối chiếu "
                    "các mục ❓ trong docs/broker_notes.md rồi mới đặt true")
        fee = sum(float(v) for v in fee_vals) if all(v is not None for v in fee_vals) else 0.0
        return cls(
            account_no=account,
            loan_package_id=None if loan is None else int(loan),
            base_url=base_url,
            otp_type=str(g("otp_type", "email_otp")),
            token_ttl_s=None if ttl is None else float(ttl),
            token_renew_before_s=float(g("token_renew_before_seconds", 300)),
            otp_resend_s=float(g("otp_resend_seconds", 120)),
            api_verified=verified,
            timeout_s=float(g("request_timeout_seconds", 10)),
            poll_s=float(g("poll_seconds", 1)),
            uncertain_resolve_s=float(g("uncertain_resolve_seconds", 15)),
            fee_vnd_per_contract=fee,
            tick_size=float(s.require("contract.tick_size_points")),
        )


# ------------------------------------------------------------------ hàm thuần ----

def to_krx(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if s.startswith("41I1"):
        return s
    return contracts.krx_symbol(*contracts.contract_ym(s))


def _on_tick(price: float, tick: float) -> float:
    n = round(price / tick)
    if abs(n * tick - price) > 1e-6:
        raise ValueError(f"giá {price} lệch bước giá {tick}")
    return round(n * tick, 6)


def build_place_payload(order: Order, config: DnseConfig) -> dict:
    """Body `POST /accounts/orders` đúng khuôn SDK `PlaceOrderRequest` (camelCase, có cả null)."""
    code = _TYPE_CODE.get(order.order_type)
    if code is None:
        raise ValueError(f"loại lệnh {order.order_type.value} không gửi được qua API DNSE "
                         "(stop do bot giữ, gửi MTL khi chạm giá)")
    if order.qty <= 0:
        raise ValueError("khối lượng phải > 0")
    price = None
    if order.order_type is OrderType.LIMIT:
        if order.price is None:
            raise ValueError("lệnh LO cần giá")
        price = _on_tick(float(order.price), config.tick_size)
    return {"accountNo": config.account_no, "symbol": to_krx(order.symbol), "side": _SIDE_CODE[order.side],
            "orderType": code, "quantity": int(order.qty), "price": price,
            "loanPackageId": config.loan_package_id}


def _norm_status(raw: Any) -> str:
    return re.sub(r"[^a-z]", "", str(raw or "").lower())


def map_order_status(raw: Any, qty: Optional[int], filled: Optional[int], leave: Optional[int],
                     canceled: Optional[int]) -> OrderStatus:
    """Trạng thái một orderId. Khối lượng đáng tin hơn chuỗi trạng thái (chưa kiểm ❓)."""
    s = _norm_status(raw)
    f = int(filled or 0)
    if s in _ST_REJECTED:
        return OrderStatus.REJECTED
    if qty and f >= int(qty):
        return OrderStatus.FILLED
    if s in _ST_CANCELLED or (leave == 0 and int(canceled or 0) > 0):
        return OrderStatus.CANCELLED
    if s in _ST_FILLED and leave in (0, None) and f > 0:
        return OrderStatus.FILLED
    if f > 0:
        return OrderStatus.PARTIALLY_FILLED
    return OrderStatus.PENDING


def _g(d: Mapping, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _item_id(item: Any) -> Optional[str]:
    if not isinstance(item, Mapping):
        return None
    v = _g(item, "id", "orderId", "order_id")
    return None if v is None else str(v)


def _int(v: Any) -> Optional[int]:
    return None if v is None else int(float(v))


def _order_list(body: Any) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("orders", "data", "items"):
            if isinstance(body.get(k), list):
                return body[k]
    raise BrokerError(f"sổ lệnh DNSE: không nhận ra khuôn response (khoá: {sorted(body) if isinstance(body, dict) else type(body).__name__})")


# ------------------------------------------------------------------ adapter ----

@dataclass
class _Leg:
    broker_id: str
    filled: int = 0
    avg: float = 0.0
    status: OrderStatus = OrderStatus.PENDING


@dataclass
class _Track:
    order: Order
    legs: list[_Leg] = field(default_factory=list)
    local_stop: bool = False           # STOP do bot giữ
    triggered: bool = False            # stop đã chạm giá và được gửi thành MTL
    uncertain_since: Optional[datetime] = None
    known_before: frozenset = frozenset()
    expect: Optional[dict] = None      # payload cần tìm khi đối soát lệnh "không rõ"
    replacing: bool = False            # sửa lệnh không rõ kết quả: chỉ gắn leg mới, không từ chối


class DnseBroker(BrokerAdapter):
    def __init__(self, http: DnseHttp, config: DnseConfig, *, clock: Clock, name: str = "dnse",
                 live_flag: bool = False, dry_run: bool = False,
                 env: Optional[Mapping[str, str]] = None, notify: Optional[Notify] = None):
        super().__init__(name)
        self.http, self.config, self.clock = http, config, clock
        self.live_flag, self.dry_run, self.env = live_flag, dry_run, env
        self.notify = notify
        self.dry_log: list[dict] = []
        self._tracks: dict[str, _Track] = {}
        self._leg_owner: dict[str, str] = {}
        self._seen_ids: set[str] = set()
        self._primed = False
        self._token: Optional[str] = None
        self._token_at: Optional[datetime] = None
        self._otp_requested_at: Optional[datetime] = None
        self._last_sync: Optional[datetime] = None
        self._sync_ok = False
        self._last_px: dict[str, float] = {}
        self._warned: set[str] = set()

    def __repr__(self) -> str:
        return (f"DnseBroker(name={self.name!r}, account={self.config.account_no!r}, "
                f"dry_run={self.dry_run}, token={'có' if self._token else 'không'})")

    # ------------------------------------------------------------ tiện ích ----

    def order(self, client_order_id: str) -> Optional[Order]:
        t = self._tracks.get(client_order_id)
        return None if t is None else t.order

    async def _say(self, text: str) -> None:
        log.info("dnse: %s", text)
        if self.notify is None:
            return
        try:
            res = self.notify(text)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("dnse: gửi thông báo lỗi")

    def _now(self) -> datetime:
        return self.clock.now()

    def _acc(self) -> str:
        if not self.config.account_no:
            raise BrokerError("thiếu DNSE_ACCOUNT_NO (.env) — số tiểu khoản phái sinh")
        return self.config.account_no

    def _set(self, cid: str, new: Order) -> Order:
        t = self._tracks[cid]
        if new != t.order:
            t.order = new
            self._emit_order(new)
        return new

    def _reject(self, cid: str, reason: str) -> Order:
        t = self._tracks[cid]
        t.uncertain_since, t.expect = None, None
        log.warning("dnse: %s bị từ chối: %s", cid, reason)
        return self._set(cid, t.order.with_status(OrderStatus.REJECTED, reject_reason=reason))

    def _dry(self, method: str, path: str, params: Optional[dict], body: Optional[dict]) -> None:
        entry = {"method": method, "path": path, "params": dict(params or {}), "json": body}
        self.dry_log.append(entry)
        log.info("dnse DRY-RUN (không gửi): %s %s %s %s", method, path, entry["params"],
                 json.dumps(body, ensure_ascii=False))

    # ------------------------------------------------------------ token ----

    @property
    def trading_ready(self) -> bool:
        """Đặt/huỷ lệnh được chưa (có trading token còn hạn). Runner chờ OTP tới khi True."""
        return self.dry_run or self.token_valid()

    @property
    def awaiting_otp(self) -> bool:
        """Đã xin OTP và đang chờ người dùng gửi `/otp dnse <mã>`."""
        return self._otp_requested_at is not None and not self.token_valid()

    def token_valid(self, now: Optional[datetime] = None) -> bool:
        if not self._token or self._token_at is None:
            return False
        if self.config.token_ttl_s is None:
            return True
        return (now or self._now()) < self._token_at + timedelta(seconds=self.config.token_ttl_s)

    def _guard(self) -> None:
        """Gọi NGAY trước mọi request thay đổi lệnh (không dùng cho dry-run)."""
        assert_live_trading_allowed(self.live_flag, self.env)
        if self.config.is_production and not self.config.api_verified:
            raise LiveTradingNotAllowed("brokers.dnse.api_verified=false — chưa kiểm API DNSE, "
                                        "không gửi lệnh thật (scripts/dnse_trading_probe.py)")
        if not self.token_valid():
            raise BrokerError("chưa có trading token DNSE hợp lệ — trả lời /otp dnse <mã> trong email")

    async def request_otp(self, reason: str = "") -> bool:
        self._otp_requested_at = self._now()
        try:
            r = await self.http.request("POST", "/registration/send-email-otp")
        except DnseTransportError as exc:
            await self._say(f"⚠️ DNSE: không xin được OTP ({exc})")
            return False
        if not r.ok:
            await self._say(f"⚠️ DNSE: xin OTP lỗi ({DnseApiError(r.status, r.body)})")
            return False
        await self._say(f"📧 DNSE đã gửi OTP về email{reason} — trả lời /otp dnse <mã> (OTP sống ~2 phút) "
                        "để bot đặt/huỷ lệnh được")
        return True

    async def submit_otp(self, code: str) -> bool:
        code = str(code or "").strip()
        if not _OTP_RE.fullmatch(code):
            await self._say("❌ OTP phải là 4–8 chữ số")
            return False
        try:
            r = await self.http.request("POST", "/registration/trading-token",
                                        json_body={"otpType": self.config.otp_type, "passcode": code})
        except DnseTransportError as exc:
            await self._say(f"❌ DNSE: không gửi được OTP ({exc}) — thử lại /otp")
            return False
        token = r.body.get("tradingToken") if r.ok and isinstance(r.body, dict) else None
        if not token:
            await self._say(f"❌ DNSE từ chối OTP ({DnseApiError(r.status, r.body)}) — xin mã mới rồi /otp lại")
            return False
        self._token, self._token_at = str(token), self._now()
        until = ""
        if self.config.token_ttl_s is not None:
            until = f", hết hạn khoảng {self._token_at + timedelta(seconds=self.config.token_ttl_s):%H:%M}"
        await self._say(f"✅ Đã có trading token DNSE{until}")
        return True

    async def _token_lost(self) -> None:
        self._token, self._token_at = None, None
        await self.request_otp(" (trading token hết hạn)")

    async def _maybe_renew(self, now: datetime) -> None:
        ttl = self.config.token_ttl_s
        if self.dry_run or ttl is None or self._token_at is None:
            return
        if now < self._token_at + timedelta(seconds=ttl - self.config.token_renew_before_s):
            return
        if self._otp_requested_at is not None and \
                (now - self._otp_requested_at).total_seconds() < self.config.otp_resend_s:
            return
        await self.request_otp(" (trading token sắp hết hạn)")

    # ------------------------------------------------------------ vòng đời ----

    async def connect(self) -> None:
        r = await self.http.request("GET", "/accounts")
        if not r.ok:
            raise DnseApiError(r.status, r.body)
        acc = self.config.account_no
        if acc is None:
            if not self.dry_run:
                raise BrokerError("thiếu DNSE_ACCOUNT_NO (.env) — số tiểu khoản phái sinh")
            log.warning("dnse: chưa điền DNSE_ACCOUNT_NO — dry-run chỉ log payload")
            return
        accounts = (r.body or {}).get("accounts") if isinstance(r.body, dict) else None
        match = [a for a in (accounts or []) if isinstance(a, Mapping) and str(a.get("id")) == acc]
        if not match:
            raise BrokerError(f"tiểu khoản {acc} không có trong GET /accounts của API key này")
        if match[0].get("derivativeAccount") is False:
            raise BrokerError(f"tiểu khoản {acc} không phải tài khoản phái sinh (derivativeAccount=false)")
        await self.sync()                         # mốc sổ lệnh: lệnh "không rõ" chỉ khớp lệnh MỚI
        if not self.dry_run and not self.token_valid():
            await self.request_otp()

    async def ping(self) -> bool:
        now = self._now()
        if self._last_sync is None or (now - self._last_sync).total_seconds() >= self.config.poll_s:
            self._last_sync = now
            try:
                await self.sync()
                self._sync_ok = True
            except Exception as exc:
                self._sync_ok = False
                log.warning("dnse: đọc sổ lệnh lỗi: %s", exc)
        await self._maybe_renew(now)
        if not self._sync_ok:
            return False
        return True if self.dry_run else self.token_valid(now)

    # ------------------------------------------------------------ đặt lệnh ----

    async def place(self, order: Order) -> Order:
        cid = order.client_order_id
        t = self._tracks.get(cid)
        if t is not None:
            return t.order                                    # idempotent: KHÔNG gửi lại
        if order.order_type is OrderType.STOP:
            return await self._place_local_stop(order)
        self._tracks[cid] = _Track(order.with_status(OrderStatus.NEW, broker=self.name))
        try:
            payload = build_place_payload(order, self.config)
        except ValueError as exc:
            return self._reject(cid, f"payload không hợp lệ: {exc}")
        if self.dry_run:
            self._dry("POST", "/accounts/orders", ORDER_PARAMS, payload)
            return self._reject(cid, "dry-run: không gửi lệnh (chỉ log payload)")
        try:
            self._guard()
        except Exception:
            del self._tracks[cid]                             # chưa gửi gì: để lần sau quyết lại
            raise
        return await self._send_new(cid, payload)

    async def _place_local_stop(self, order: Order) -> Order:
        cid = order.client_order_id
        if order.price is None or order.qty <= 0:
            self._tracks[cid] = _Track(order.with_status(OrderStatus.NEW, broker=self.name))
            return self._reject(cid, "stop cần giá kích hoạt và khối lượng > 0")
        if not self.dry_run:
            self._guard()                                     # stop vô dụng nếu lúc chạm không gửi được
        self._tracks[cid] = _Track(order.with_status(OrderStatus.NEW, broker=self.name), local_stop=True)
        return self._set(cid, order.with_status(OrderStatus.PENDING, broker=self.name,
                                                broker_order_id=f"{LOCAL_PREFIX}{cid}"))

    async def _send_new(self, cid: str, payload: dict) -> Order:
        t = self._tracks[cid]
        known_before = frozenset(self._seen_ids)
        try:
            r = await self.http.request("POST", "/accounts/orders", params=ORDER_PARAMS,
                                        json_body=payload, trading_token=self._token)
        except DnseTransportError as exc:
            return await self._uncertain(cid, known_before, payload, str(exc))
        if r.status == 408 or r.status >= 500:
            return await self._uncertain(cid, known_before, payload, f"HTTP {r.status}")
        if r.ok:
            bid = _item_id(r.body)
            if bid is None:
                return await self._uncertain(cid, known_before, payload, "response không có orderId")
            self._attach(cid, bid)
            if not self._apply(cid, bid, r.body):
                self._set(cid, t.order.with_status(OrderStatus.PENDING, broker_order_id=bid))
            return t.order
        err = DnseApiError(r.status, r.body)
        if err.token_expired:
            await self._token_lost()
        return self._reject(cid, str(err))

    async def _uncertain(self, cid: str, known_before: frozenset, payload: dict, why: str) -> Order:
        t = self._tracks[cid]
        t.uncertain_since, t.known_before, t.expect = self._now(), known_before, payload
        await self._say(f"⚠️ DNSE: không rõ lệnh {cid} đã tới sàn chưa ({why}) — KHÔNG gửi lại, "
                        f"đang đối soát sổ lệnh tối đa {self.config.uncertain_resolve_s:.0f}s")
        return self._set(cid, t.order.with_status(OrderStatus.PENDING))

    def _attach(self, cid: str, bid: str) -> None:
        t = self._tracks[cid]
        t.legs.append(_Leg(bid))
        self._leg_owner[bid] = cid
        self._seen_ids.add(bid)
        t.uncertain_since, t.expect = None, None

    # ------------------------------------------------------------ áp trạng thái ----

    def _apply(self, cid: str, bid: str, item: Any) -> bool:
        """Áp một OrderItem vào leg `bid` của lệnh `cid`: phát Fill (trước) rồi cập nhật lệnh."""
        if not isinstance(item, Mapping):
            return False
        t = self._tracks[cid]
        leg = next((lg for lg in t.legs if lg.broker_id == bid), None)
        if leg is None:
            return False
        o = t.order
        raw = _g(item, "orderStatus", "order_status", "status")
        norm = _norm_status(raw)
        if norm and norm not in KNOWN_STATUS_WORDS and norm not in self._warned:
            self._warned.add(norm)
            log.warning("dnse: orderStatus lạ %r — suy trạng thái từ khối lượng (cập nhật broker_notes ❓)", raw)
        qty = _int(_g(item, "quantity"))
        filled = _int(_g(item, "fillQuantity", "fill_quantity", "filledQuantity")) or 0
        leave = _int(_g(item, "leaveQuantity", "leave_quantity"))
        canc = _int(_g(item, "canceledQuantity", "canceled_quantity", "cancelledQuantity"))
        avg = float(_g(item, "averagePrice", "average_price") or 0.0)
        status = map_order_status(raw, qty, filled, leave, canc)
        delta = filled - leg.filled
        if delta > 0:
            if avg > 0:
                px = (avg * filled - leg.avg * leg.filled) / delta
            else:
                px = float(_g(item, "price") or o.price or self._last_px.get(o.symbol) or 0.0)
                log.warning("dnse: %s khớp %d nhưng không có averagePrice — tạm dùng %.1f", cid, delta, px)
            leg.filled, leg.avg = filled, avg
            self._emit_fill(Fill(cid, o.symbol, o.side, delta, round(px, 4), self._now(),
                                 fee_vnd=self.config.fee_vnd_per_contract * delta, broker=self.name))
        elif delta < 0:
            log.warning("dnse: %s leg %s fillQuantity giảm %d → %d (bỏ qua)", cid, bid, leg.filled, filled)
        leg.status = status
        total = sum(lg.filled for lg in t.legs)
        cur = t.legs[-1]
        st = cur.status
        if total >= o.qty:
            st = OrderStatus.FILLED
        elif st in (OrderStatus.PENDING, OrderStatus.FILLED) and total > 0:
            st = OrderStatus.PARTIALLY_FILLED
        reason = o.reject_reason
        if st is OrderStatus.REJECTED and not reason:
            reason = f"DNSE từ chối (orderStatus={raw})"
        self._set(cid, replace(o, status=st, filled_qty=total, broker_order_id=cur.broker_id,
                               reject_reason=reason))
        return True

    # ------------------------------------------------------------ đồng bộ sổ lệnh ----

    async def sync(self) -> None:
        """Đọc sổ lệnh trong ngày, phát khớp/cập nhật, đối soát lệnh "không rõ", nhận lệnh ngoài bot."""
        acc = self._acc()
        r = await self.http.request("GET", f"/accounts/{acc}/orders", params=ORDER_PARAMS)
        if not r.ok:
            raise DnseApiError(r.status, r.body)
        by_id: dict[str, dict] = {}
        for it in _order_list(r.body):
            bid = _item_id(it)
            if bid is not None:
                by_id[bid] = it
        now = self._now()
        for bid, it in by_id.items():
            cid = self._leg_owner.get(bid)
            if cid is not None:
                self._apply(cid, bid, it)
        for cid, t in list(self._tracks.items()):
            if t.order.status.is_terminal or not t.legs:
                continue
            bid = t.legs[-1].broker_id
            if bid not in by_id:
                await self._fetch_one(cid, bid)
        for cid, t in list(self._tracks.items()):
            if t.uncertain_since is None:
                continue
            cands = [(bid, it) for bid, it in by_id.items()
                     if bid not in t.known_before and bid not in self._leg_owner and self._matches(t, it)]
            if cands:
                bid, it = min(cands, key=lambda x: (len(x[0]), x[0]))
                was_replacing = t.replacing
                t.replacing = False
                self._attach(cid, bid)
                self._apply(cid, bid, it)
                await self._say(f"✅ DNSE: đã thấy lệnh {cid} trong sổ lệnh (orderId {bid})"
                                + (" sau khi sửa" if was_replacing else ""))
            elif (now - t.uncertain_since).total_seconds() >= self.config.uncertain_resolve_s:
                if t.replacing:
                    t.uncertain_since, t.expect, t.replacing = None, None, False
                    await self._say(f"⚠️ DNSE: không thấy lệnh thay thế của {cid} — giữ theo dõi orderId cũ")
                else:
                    self._reject(cid, f"không thấy lệnh trong sổ lệnh sau {self.config.uncertain_resolve_s:.0f}s "
                                      "— coi như chưa tới sàn")
        await self._adopt(by_id)
        self._seen_ids.update(by_id)
        self._primed = True

    async def _fetch_one(self, cid: str, bid: str) -> None:
        try:
            r = await self.http.request("GET", f"/accounts/{self._acc()}/orders/{bid}", params=ORDER_PARAMS)
        except DnseTransportError as exc:
            log.warning("dnse: đọc lệnh %s lỗi: %s", bid, exc)
            return
        if r.ok:
            self._apply(cid, bid, r.body)
        else:
            log.warning("dnse: đọc lệnh %s lỗi: %s", bid, DnseApiError(r.status, r.body))

    def _matches(self, t: _Track, it: Mapping) -> bool:
        e = t.expect or {}
        if str(_g(it, "symbol") or "").upper() != e.get("symbol") or str(_g(it, "side") or "").upper() != e.get("side"):
            return False
        if t.replacing:
            return True
        if str(_g(it, "orderType", "order_type") or "").upper() != e.get("orderType"):
            return False
        if _int(_g(it, "quantity")) != e.get("quantity"):
            return False
        if e.get("price") is not None:
            p = _g(it, "price")
            return p is not None and abs(float(p) - float(e["price"])) < 1e-6
        return True

    async def _adopt(self, by_id: dict[str, dict]) -> None:
        for bid, it in by_id.items():
            if bid in self._leg_owner:
                continue
            is_new = self._primed and bid not in self._seen_ids
            qty = _int(_g(it, "quantity")) or 0
            filled = _int(_g(it, "fillQuantity", "fill_quantity")) or 0
            status = map_order_status(_g(it, "orderStatus", "order_status"), qty,
                                      filled, _int(_g(it, "leaveQuantity")), _int(_g(it, "canceledQuantity")))
            if status.is_terminal and not (is_new and filled > 0):
                continue                     # lệnh đã xong trước khi bot chạy: vị thế đã phản ánh
            sym = contracts.symbol_contract(_g(it, "symbol"))
            side = _SIDE_FROM.get(str(_g(it, "side") or "").upper())
            if sym is None or side is None or qty <= 0:
                key = f"adopt:{bid}"
                if key not in self._warned:
                    self._warned.add(key)
                    log.warning("dnse: bỏ qua lệnh %s không nhận ra (symbol=%r side=%r)",
                                bid, _g(it, "symbol"), _g(it, "side"))
                continue
            cid = f"{EXTERNAL_PREFIX}{bid}"[-MAX_CLIENT_ORDER_ID_LEN:]
            otype = _TYPE_FROM.get(str(_g(it, "orderType") or "").upper(), OrderType.LIMIT)
            price = _g(it, "price")
            o = Order(cid, sym, side, qty, otype, self._now(), price=None if price is None else float(price),
                      status=OrderStatus.NEW, broker=self.name, broker_order_id=bid, tag="external")
            self._tracks[cid] = _Track(o)
            self._attach(cid, bid)
            if not is_new:                    # có từ trước: phần đã khớp đã nằm trong vị thế
                leg = self._tracks[cid].legs[-1]
                leg.filled, leg.avg = filled, float(_g(it, "averagePrice") or 0.0)
                o = replace(o, filled_qty=filled)
                self._tracks[cid].order = o
            self._apply(cid, bid, it)
            await self._say(f"⚠️ DNSE: lệnh ngoài bot orderId {bid} ({side.value} {qty} {sym}) — bot theo dõi "
                            f"khớp lệnh để net position đúng (id {cid})")

    # ------------------------------------------------------------ huỷ / sửa ----

    def _live_track(self, cid: str) -> _Track:
        t = self._tracks.get(cid)
        if t is None:
            raise BrokerError(f"không biết lệnh {cid}")
        if t.order.status.is_terminal:
            raise BrokerError(f"lệnh {cid} đã kết thúc ({t.order.status.value})")
        return t

    def _real_leg(self, cid: str, t: _Track) -> str:
        if t.uncertain_since is not None or not t.legs:
            raise BrokerError(f"lệnh {cid} chưa biết orderId — đang đối soát sổ lệnh")
        return t.legs[-1].broker_id

    async def cancel(self, client_order_id: str) -> Order:
        cid = client_order_id
        t = self._live_track(cid)
        if t.local_stop and not t.triggered:
            return self._set(cid, t.order.with_status(OrderStatus.CANCELLED))
        bid = self._real_leg(cid, t)
        path = f"/accounts/{self._acc()}/orders/{bid}"
        if self.dry_run:
            self._dry("DELETE", path, ORDER_PARAMS, None)
            raise BrokerError("dry-run: không huỷ lệnh thật")
        self._guard()
        try:
            r = await self.http.request("DELETE", path, params=ORDER_PARAMS, trading_token=self._token)
        except DnseTransportError as exc:
            raise BrokerError(f"huỷ {cid}: không rõ kết quả ({exc}) — sổ lệnh sẽ cho biết") from exc
        if not r.ok:
            err = DnseApiError(r.status, r.body)
            if err.token_expired:
                await self._token_lost()
            raise err
        await self._fetch_one(cid, bid)          # xác nhận: chỉ CANCELLED khi sàn báo đã huỷ
        return self._tracks[cid].order

    async def modify(self, client_order_id: str, *, price: Optional[float] = None,
                     qty: Optional[int] = None) -> Order:
        cid = client_order_id
        t = self._live_track(cid)
        o = t.order
        if price is not None:
            price = _on_tick(float(price), self.config.tick_size)
        if qty is not None and qty < o.filled_qty + 1:
            raise BrokerError(f"khối lượng mới {qty} phải > phần đã khớp {o.filled_qty}")
        if t.local_stop and not t.triggered:
            return self._set(cid, replace(o, price=o.price if price is None else price,
                                          qty=o.qty if qty is None else int(qty)))
        bid = self._real_leg(cid, t)
        body: dict = {}
        if price is not None:
            body["price"] = price
        if qty is not None:
            body["quantity"] = int(qty) - o.filled_qty    # huỷ-đặt ⇒ lệnh mới chỉ cho phần còn lại (❓)
        if not body:
            return o
        path = f"/accounts/{self._acc()}/orders/{bid}"
        if self.dry_run:
            self._dry("PUT", path, ORDER_PARAMS, body)
            raise BrokerError("dry-run: không sửa lệnh thật")
        self._guard()
        known_before = frozenset(self._seen_ids)
        expect = {"symbol": to_krx(o.symbol), "side": _SIDE_CODE[o.side]}
        try:
            r = await self.http.request("PUT", path, params=ORDER_PARAMS, json_body=body, trading_token=self._token)
        except DnseTransportError as exc:
            t.uncertain_since, t.known_before, t.expect, t.replacing = self._now(), known_before, expect, True
            raise BrokerError(f"sửa {cid}: không rõ kết quả ({exc}) — đang đối soát") from exc
        if not r.ok:
            err = DnseApiError(r.status, r.body)
            if err.token_expired:
                await self._token_lost()
            raise err
        new_bid = _item_id(r.body)
        if new_bid is None:
            t.uncertain_since, t.known_before, t.expect, t.replacing = self._now(), known_before, expect, True
            raise BrokerError(f"sửa {cid}: response không có orderId mới — đang đối soát")
        t.order = replace(o, price=o.price if price is None else price, qty=o.qty if qty is None else int(qty))
        self._attach(cid, new_bid)
        self._apply(cid, new_bid, r.body)
        return self._tracks[cid].order

    # ------------------------------------------------------------ stop do bot giữ ----

    async def on_tick(self, tick: Tick) -> None:
        mark = tick.last if tick.last is not None else tick.mid
        if mark is not None:
            self._last_px[tick.symbol] = float(mark)
        for cid, t in list(self._tracks.items()):
            if not t.local_stop or t.triggered or t.order.status.is_terminal or t.order.symbol != tick.symbol:
                continue
            trig = float(t.order.price)
            if t.order.side is Side.SELL:
                hit = (tick.last is not None and tick.last <= trig) or (tick.bid is not None and tick.bid <= trig)
            else:
                hit = (tick.last is not None and tick.last >= trig) or (tick.ask is not None and tick.ask >= trig)
            if hit:
                await self._trigger(cid, t)

    async def _trigger(self, cid: str, t: _Track) -> None:
        t.triggered = True                                   # đánh dấu TRƯỚC khi gửi: không bao giờ gửi đôi
        o = t.order
        mtl = replace(o, order_type=OrderType.MARKET, price=None, qty=o.qty - o.filled_qty)
        log.warning("dnse: stop %s chạm %.1f ⇒ gửi MTL %s %d", cid, float(o.price), o.side.value, mtl.qty)
        try:
            payload = build_place_payload(mtl, self.config)
        except ValueError as exc:
            self._reject(cid, f"stop chạm nhưng payload lỗi: {exc}")
            return
        if self.dry_run:
            self._dry("POST", "/accounts/orders", ORDER_PARAMS, payload)
            self._reject(cid, "dry-run: stop chạm, không gửi lệnh")
            return
        try:
            self._guard()
        except Exception as exc:
            self._reject(cid, f"stop chạm nhưng không gửi được: {exc}")
            await self._say(f"🚨 DNSE: stop {cid} chạm nhưng KHÔNG gửi được ({exc})")
            return
        await self._send_new(cid, payload)

    # ------------------------------------------------------------ vị thế / lệnh chờ ----

    async def positions(self) -> dict[str, Position]:
        r = await self.http.request("GET", f"/accounts/{self._acc()}/positions",
                                    params={"marketType": MARKET_TYPE, "pageSize": self.config.positions_page_size})
        if not r.ok:
            raise DnseApiError(r.status, r.body)
        # Server thật (probe 09/11/2026): {"positions": [...], pageIndex, pageSize, pageNumber, total};
        # SDK dnse 0.5.0 dùng "deals" — nhận cả hai, khuôn khác ⇒ không đoán.
        body = r.body if isinstance(r.body, dict) else {}
        deals = body.get("positions") if isinstance(body.get("positions"), list) else body.get("deals")
        if not isinstance(deals, list):
            raise BrokerError(f"vị thế DNSE: response không có danh sách 'positions'/'deals' (khoá: {sorted(body)})")
        total = r.body.get("total")
        if total is not None and int(total) > len(deals):
            raise BrokerError(f"vị thế DNSE có {total} deal, mới đọc {len(deals)} (thiếu trang) — không đoán")
        acc: dict[str, list[tuple[int, float]]] = {}
        for d in deals:
            if not isinstance(d, Mapping):
                continue
            q = _int(_g(d, "openQuantity", "open_quantity")) or 0
            status = str(_g(d, "status") or "OPEN").upper()
            if q <= 0:
                continue
            if status not in ("OPEN", "ACTIVE"):
                # trạng thái đóng mà còn khối lượng mở (❓): không đoán — sai một chiều là lệnh thật sai
                raise BrokerError(f"vị thế DNSE: deal {_g(d, 'id')} status={status} nhưng openQuantity={q}")
            side = _SIDE_FROM.get(str(_g(d, "side") or "").upper())
            if side is None:
                raise BrokerError(f"vị thế DNSE: chiều lạ {_g(d, 'side')!r} (deal {_g(d, 'id')})")
            sym = contracts.symbol_contract(_g(d, "symbol"))
            if sym is None:
                log.warning("dnse: bỏ qua vị thế mã %r (không phải VN30F)", _g(d, "symbol"))
                continue
            acc.setdefault(sym, []).append((side.sign * q, float(_g(d, "costPrice", "cost_price") or 0.0)))
        out: dict[str, Position] = {}
        for sym, rows in acc.items():
            net = sum(q for q, _ in rows)
            same = [(q, p) for q, p in rows if net and (q > 0) == (net > 0)]
            avg = sum(abs(q) * p for q, p in same) / sum(abs(q) for q, _ in same) if same else 0.0
            out[sym] = Position(sym, net, avg, broker=self.name)
        return out

    async def open_orders(self) -> list[Order]:
        await self.sync()
        return [t.order for t in self._tracks.values() if not t.order.status.is_terminal]


# ------------------------------------------------------------------ danh sách hợp đồng (CLAUDE.md §4) ----

class ContractMismatch(BrokerError):
    """Hợp đồng DNSE báo khác hợp đồng bot tự tính (lịch nghỉ / ngày đáo hạn sai?) ⇒ không giao dịch."""


async def fetch_vn30_futures(http: DnseHttp, d, calendar) -> list[dict]:
    """`GET /instruments?symbol=…&marketId=DVX&securityGroupId=FU` (đặc tả OpenAPI DNSE; `symbol` bắt buộc).

    Hỏi đúng các mã bot tự tính (F1M/F2M/F1Q/F2Q) và tháng trước F1M; mỗi dòng có `symbolType`
    (`VN30F1M`…) để đối chiếu. Field ngoài đặc tả ❓ — khuôn lạ ⇒ lỗi, không đoán.
    """
    names = list(contracts.listed_contracts(d, calendar).values())
    y, m = contracts.contract_ym(names[0])
    prev = (y - 1, 12) if m == 1 else (y, m - 1)
    krx = [contracts.krx_symbol(*prev)] + [to_krx(n) for n in names]
    r = await http.request("GET", "/instruments", params={"symbol": ",".join(krx), "marketId": "DVX",
                                                          "securityGroupId": "FU", "limit": 100, "page": 1})
    if not r.ok:
        raise DnseApiError(r.status, r.body)
    rows = r.body.get("data") if isinstance(r.body, dict) else None
    if not isinstance(rows, list):
        raise BrokerError("danh sách hợp đồng DNSE: response không có 'data' (khuôn lạ — kiểm broker_notes §1.3)")
    return [x for x in rows if isinstance(x, dict)]


def verify_contracts(rows: list[dict], d, calendar) -> list[str]:
    """Đối chiếu danh sách DNSE với lịch đáo hạn của bot; trả các mã KRX VN30F được niêm yết.

    `symbolType` DNSE báo (VN30F1M/F2M/F1Q/F2Q) phải trùng mã bot tính; thiếu F1M ⇒ lỗi.
    """
    expected = {label: to_krx(name) for label, name in contracts.listed_contracts(d, calendar).items()}
    listed: list[str] = []
    by_type: dict[str, str] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not contracts.is_vn30_future(sym):
            continue
        listed.append(sym)
        t = str(row.get("symbolType") or "").strip().upper()
        if t:
            by_type[t] = sym
    for label, krx in expected.items():
        if label in by_type and by_type[label] != krx:
            raise ContractMismatch(
                f"DNSE báo {label} = {by_type[label]}, bot tính {krx} — kiểm config/holidays.yaml và "
                "lịch đáo hạn (core/contracts.py) trước khi chạy")
    if "VN30F1M" not in by_type and expected["VN30F1M"] not in listed:
        raise ContractMismatch(f"DNSE không trả hợp đồng F1M ({expected['VN30F1M']}) — danh sách: {sorted(listed)}")
    if not by_type:
        log.warning("dnse: /instruments không có symbolType — chỉ đối chiếu được mã có niêm yết hay không")
    return listed
