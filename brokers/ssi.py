"""Adapter SSI FastConnect V3 (PROMPTS P4.2) — broker DỰ PHÒNG (đối ứng khẩn cấp / chiến lược độc lập).

Endpoint/field theo mã nguồn SDK chính thức `ssi-sdk` 3.3.2 (docs/broker_notes.md §2.6). Lớp HTTP
tự viết mỏng trên `requests` (chạy trong thread): KHÔNG có retry ẩn nào.

Khác DNSE:
- SSI CÓ khoá idempotency: `clientRequestId` (= `client_order_id`, ≤ 20 ký tự); gửi trùng trong
  ngày ⇒ HTTP 409. Lệnh "không rõ" (timeout/5xx) được tìm CHÍNH XÁC theo `clientRequestId` trong
  sổ lệnh; 409 nghĩa là lệnh đã tồn tại. Adapter vẫn KHÔNG tự gửi lại (để quy trình khẩn cấp
  quyết định — nó tính phần còn thiếu theo lệnh đang chờ).
- Lệnh thay đổi được ký RSA-SHA256 (PKCS#1 v1.5, hex) đúng chuỗi body gửi đi (`X-Signature`),
  khoá XML base64 đọc từ `SSI_PRIVATE_KEY_PATH`. Bearer token cho mọi request.
- Token: OTP (SMS/email) nhập tay `/otp ssi <mã>`. Refresh token CHỈ DÙNG MỘT LẦN — dùng lại ⇒ SSI
  thu hồi TOÀN BỘ phiên của apiKey. `SsiTokenManager` bỏ refresh token khỏi bộ nhớ TRƯỚC khi gửi,
  nên lỗi mạng giữa chừng không bao giờ dẫn tới gửi lại; mã 401101–401108 ⇒ đăng nhập OTP lại.
  Token chỉ nằm trong bộ nhớ (khởi động lại ⇒ OTP lại) — không ghi token xuống đĩa. Feed SSI dùng
  CHUNG manager này (`token_provider`).
- Lỗi có thể đến dưới dạng HTTP 200 + `{code, msg}` (code ≠ 200/204) ⇒ vẫn là lỗi.
- Mã trạng thái lệnh có định nghĩa trong SDK (PD, QU, PF, FF, CL, RJ…) ⇒ ánh xạ thẳng, khối lượng
  là chốt chặn cuối.

Chốt an toàn trước MỌI request thay đổi: TRADING_MODE=live + `--live` + `brokers.ssi.api_verified`
+ token. `dry_run` chỉ log payload. Hai mục ❓ phải điền trước khi live: `order_symbol_format`
(mã KRX hay VN30FYYMM khi đặt lệnh) và `market_order_type` (MTL hay MP cho phái sinh).

Stream TRADING (`order.<tk>`, `portfolio.<tk>` trên `wss://stream.ssi.com.vn/ws/v3`): sự kiện
`orderEvent` mang khối lượng CỘNG DỒN ⇒ áp như sổ lệnh REST (trùng không đếm đôi; trùng `notifyId`
bị bỏ); `orderMatchEvent` (delta) chỉ yêu cầu đọc lại sổ lệnh ngay. SDK không có cơ chế phát lại
theo `notifyId` ⇒ "không mất sự kiện khi reconnect" = mỗi lần (re)connect đọc lại sổ lệnh REST.

Stop: như DNSE — bot giữ (`local:…`), chạm giá ⇒ MTL/MP cùng client id. (SSI có lệnh điều kiện
FCO stop; hỗ trợ phái sinh ❓ — chưa dùng.)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import math
import os
import random
import secrets
import socket
import sys
import time as _time
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional
from xml.etree.ElementTree import fromstring

from brokers.base import BrokerAdapter, BrokerError, LiveTradingNotAllowed, assert_live_trading_allowed
from brokers.dnse import _on_tick, to_krx
from brokers.paper import FEE_KEYS
from core import contracts
from core.clock import Clock
from core.models import MAX_CLIENT_ORDER_ID_LEN, Fill, Order, OrderStatus, OrderType, Position, Side, Tick
from core.settings import MissingSetting, Settings

log = logging.getLogger(__name__)

BASE_URL = "https://api.ssi.com.vn"
STREAM_URL = "wss://stream.ssi.com.vn/ws/v3"
EP_TOKEN = "/api/v3/auth/token"
EP_REFRESH = "/api/v3/auth/refresh"
EP_REQUEST_OTP = "/api/v3/auth/requestOtp"
EP_ORDER = "/api/v3/trading/order"
EP_ORDER_BOOK = "/api/v3/trading/orderBook"
EP_POSITIONS = "/api/v3/trading/position"
REAUTH_CODES = frozenset({"401101", "401102", "401103", "401104", "401106", "401107", "401108"})
TOKEN_SKEW_S = 30.0
USER_AGENT = "future-trading-bot"
EXTERNAL_PREFIX = "EXT"
LOCAL_PREFIX = "local:"

_SIDE_CODE = {Side.BUY: "B", Side.SELL: "S"}
_SIDE_FROM = {"B": Side.BUY, "BUY": Side.BUY, "S": Side.SELL, "SELL": Side.SELL}
_ST_PENDING = frozenset({"PD", "WA", "RS", "SD", "QU", "WM", "WC", "IAV", "SOI", "PAS", "RQ"})
_ST_PARTIAL = frozenset({"PF"})
_ST_FILLED = frozenset({"FF"})
_ST_CANCELLED = frozenset({"FFPC", "CL", "EX"})
_ST_REJECTED = frozenset({"RJ", "ERR"})
KNOWN_STATUS = _ST_PENDING | _ST_PARTIAL | _ST_FILLED | _ST_CANCELLED | _ST_REJECTED
_OTP_CHARS = frozenset("0123456789")

Notify = Callable[[str], Any]
# (method, url, params, content, headers, timeout) -> (status, headers, text). Không được tự retry.
Transport = Callable[[str, str, Optional[dict], Optional[str], dict, float],
                     Awaitable[tuple[int, Mapping[str, str], str]]]


# ------------------------------------------------------------------ ký RSA ----

_SHA256_DER = bytes.fromhex("3031300d060960864801650304020105000420")


def _rsa_parts(private_key: str) -> tuple[int, Optional[int], int]:
    root = fromstring(base64.b64decode(private_key.encode()).decode())

    def num(tag: str) -> Optional[int]:
        node = root.find(tag)
        return None if node is None or not node.text else int.from_bytes(base64.b64decode(node.text), "big")
    n, e, d = num("Modulus"), num("Exponent"), num("D")
    if n is None or d is None:
        raise ValueError("thiếu Modulus/D")
    return n, e, d


def rsa_sign(data: str, private_key: str) -> str:
    """RSA PKCS#1 v1.5 SHA-256, hex — như `ssi_sdk.utils.crypto.sign` (REST `X-Signature`).

    Có làm mù (blinding) và tự kiểm chữ ký bằng số mũ công khai khi khoá có Exponent.
    """
    try:
        n, e, d = _rsa_parts(private_key)
    except Exception:                                    # không bao giờ in nội dung khoá
        raise ValueError("SSI private key không phải khoá RSA XML base64 hợp lệ") from None
    di = _SHA256_DER + hashlib.sha256(data.encode("utf-8")).digest()
    k = (n.bit_length() + 7) // 8
    m = int.from_bytes(b"\x00\x01" + b"\xff" * (k - len(di) - 3) + b"\x00" + di, "big")
    if e is None:
        s = pow(m, d, n)
    else:
        while True:
            r = secrets.randbelow(n - 3) + 2
            if math.gcd(r, n) == 1:
                break
        s = (pow((m * pow(r, e, n)) % n, d, n) * pow(r, -1, n)) % n
        if pow(s, e, n) != m:
            raise ValueError("ký RSA thất bại (kiểm tra nội bộ)")
    return s.to_bytes(k, "big").hex()


def load_private_key(path: Path | str) -> str:
    key = Path(path).read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"file khoá SSI rỗng: {path}")
    return key


def device_id() -> str:
    """Mã máy gửi kèm lệnh (SDK: machine id của hệ điều hành; lùi về hostname)."""
    try:
        if sys.platform in ("win32", "cygwin"):
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        mid = Path("/etc/machine-id")
        if mid.exists() and mid.read_text().strip():
            return mid.read_text().strip()
    except Exception:
        pass
    return socket.gethostname()


# ------------------------------------------------------------------ HTTP ----

class SsiTransportError(BrokerError):
    """Lỗi mạng/timeout — KHÔNG biết server đã xử lý request hay chưa."""


class SsiApiError(BrokerError):
    """Server báo lỗi (HTTP ≥ 400, hoặc 2xx kèm `{code, msg}` khác 200/204). Chỉ giữ code + msg."""

    def __init__(self, status: int, body: Any):
        self.status = int(status)
        b = body if isinstance(body, dict) else {}
        self.code = str(b.get("code") if b.get("code") is not None else status)
        self.message = str(b.get("msg") or b.get("message") or "")[:200]
        super().__init__(f"HTTP {self.status} [{self.code}] {self.message}".strip())

    @property
    def unauthorized(self) -> bool:
        return self.status in (401, 403)

    @property
    def duplicate(self) -> bool:
        return self.status == 409

    @property
    def reauth(self) -> bool:
        return self.code in REAUTH_CODES

    @property
    def uncertain(self) -> bool:
        return self.status == 408 or self.status >= 500


@dataclass(frozen=True)
class SsiResponse:
    status: int
    body: Any
    error: Optional[SsiApiError] = None

    @property
    def data(self) -> Any:
        """Phần dữ liệu: `{"data": …}` nếu có bọc, không thì cả body (❓ server bọc hay không)."""
        b = self.body
        if isinstance(b, dict) and isinstance(b.get("data"), (dict, list)):
            return b["data"]
        return b


def _requests_transport() -> Transport:
    state: dict = {}

    async def transport(method, url, params, content, headers, timeout):
        def go():
            import requests
            sess = state.get("s")
            if sess is None:
                sess = state["s"] = requests.Session()
            r = sess.request(method, url, params=params,
                             data=None if content is None else content.encode("utf-8"),
                             headers=headers, timeout=timeout)
            return r.status_code, dict(r.headers), r.text
        return await asyncio.to_thread(go)

    return transport


class SsiHttp:
    """REST SSI. MỘT request = MỘT lần gửi: không retry 429/5xx/timeout."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 10.0, transport: Optional[Transport] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport or _requests_transport()

    def __repr__(self) -> str:
        return f"SsiHttp(base_url={self.base_url!r})"

    async def request(self, method: str, path: str, *, params: Optional[dict] = None,
                      body: Optional[dict] = None, token: Optional[str] = None,
                      private_key: Optional[str] = None) -> SsiResponse:
        method = method.upper()
        content = None if body is None else json.dumps(body)     # đúng chuỗi được ký và gửi
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if private_key is not None:
            if content is None:
                raise ValueError("request ký phải có body")
            headers["X-Signature"] = rsa_sign(content, private_key)
        try:
            status, _h, text = await self._transport(method, self.base_url + path, params, content,
                                                     headers, self.timeout)
        except Exception as exc:
            raise SsiTransportError(f"{method} {path}: {type(exc).__name__}: {str(exc)[:120]}") from exc
        status = int(status)
        parsed: Any = None
        if text and text.strip():
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = {"raw_text": text[:300]}
        log.debug("ssi %s %s → %s", method, path, status)
        err = None
        if status >= 400:
            err = SsiApiError(status, parsed)
        elif isinstance(parsed, dict) and ("msg" in parsed or "message" in parsed) and \
                parsed.get("code") is not None and str(parsed.get("code")) not in ("200", "204"):
            err = SsiApiError(status, parsed)
        elif status != 204 and parsed is None:
            err = SsiApiError(status, {"msg": "response rỗng / không phải JSON"})
        return SsiResponse(status, parsed, err)


# ------------------------------------------------------------------ token ----

def _epoch_s(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x / 1000 if x >= 1e10 else max(x, 0.0)


class SsiTokenManager:
    """Access/refresh token SSI cho broker VÀ feed (một phiên mỗi apiKey).

    `get(force_refresh)` trả access token còn hạn; sắp hết hạn ⇒ refresh (một luồng, khoá).
    Refresh token bị BỎ khỏi bộ nhớ trước khi gửi ⇒ không bao giờ gửi lại cùng một refresh token.
    """

    def __init__(self, http: SsiHttp, api_key: str, api_secret: str, *,
                 now: Callable[[], float] = _time.time, notify: Optional[Notify] = None):
        if not api_key or not api_secret:
            raise ValueError("Thiếu SSI_API_KEY / SSI_API_SECRET")
        self.http, self._key, self._secret = http, api_key, api_secret
        self._now, self.notify = now, notify
        self._access: Optional[str] = None
        self._expires = 0.0
        self._refresh: Optional[str] = None
        self._refresh_expires = 0.0
        self._lock = asyncio.Lock()
        self._lock_loop = None
        self.awaiting_otp = False

    def __repr__(self) -> str:
        return f"SsiTokenManager(access={'có' if self._access else 'không'}, awaiting_otp={self.awaiting_otp})"

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:                     # asyncio.run mới (test/CLI) ⇒ khoá mới
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def _say(self, text: str) -> None:
        log.info("ssi: %s", text)
        if self.notify is None:
            return
        try:
            res = self.notify(text)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("ssi: gửi thông báo lỗi")

    def valid(self) -> bool:
        return bool(self._access) and self._now() < self._expires - TOKEN_SKEW_S

    def _store(self, data: Any) -> bool:
        p = data.get("data", data) if isinstance(data, dict) else None
        if not isinstance(p, dict) or not p.get("accessToken"):
            return False
        self._access = str(p["accessToken"])
        self._expires = _epoch_s(p.get("expiresAt"))
        self._refresh = str(p["refreshToken"]) if p.get("refreshToken") else None
        self._refresh_expires = _epoch_s(p.get("refreshExpiresAt"))
        self.awaiting_otp = False
        return True

    def clear(self) -> None:
        self._access, self._expires, self._refresh, self._refresh_expires = None, 0.0, None, 0.0

    def invalidate_access(self) -> None:
        """Server từ chối access token (401) ⇒ lần `get` sau sẽ refresh (nếu còn refresh token)."""
        self._expires = 0.0

    async def request_otp(self, reason: str = "") -> bool:
        try:
            r = await self.http.request("POST", EP_REQUEST_OTP, body={"apiKey": self._key, "apiSecret": self._secret})
        except SsiTransportError as exc:
            await self._say(f"⚠️ SSI: không xin được OTP ({exc})")
            return False
        if r.error is not None:
            await self._say(f"⚠️ SSI: xin OTP lỗi ({r.error})")
            return False
        self.awaiting_otp = True
        await self._say(f"📱 SSI đã gửi OTP{reason} — trả lời /otp ssi <mã>")
        return True

    async def submit_otp(self, code: str) -> bool:
        code = str(code or "").strip()
        if not (4 <= len(code) <= 8 and set(code) <= _OTP_CHARS):
            await self._say("❌ OTP SSI phải là 4–8 chữ số")
            return False
        try:
            r = await self.http.request("POST", EP_TOKEN, body={"apiKey": self._key, "apiSecret": self._secret,
                                                                 "otp": code})
        except SsiTransportError as exc:
            await self._say(f"❌ SSI: không gửi được OTP ({exc}) — thử lại /otp ssi")
            return False
        if r.error is not None or not self._store(r.body):
            await self._say(f"❌ SSI từ chối OTP ({r.error or 'không có token'}) — xin mã mới rồi /otp ssi lại")
            return False
        await self._say(f"✅ Đã có token SSI (hết hạn {datetime.fromtimestamp(self._expires):%H:%M})")
        return True

    async def _need_otp(self, why: str) -> None:
        self.clear()
        if not self.awaiting_otp:
            await self.request_otp(f" ({why})")
        raise BrokerError(f"SSI cần OTP ({why}) — /otp ssi <mã>")

    async def get(self, force_refresh: bool = False) -> str:
        if not force_refresh and self.valid():
            return self._access  # type: ignore[return-value]
        observed = self._access
        async with self._get_lock():
            if self._access and self._access != observed and self.valid():
                return self._access                          # người khác vừa refresh xong
            if not force_refresh and self.valid():
                return self._access  # type: ignore[return-value]
            if not self._refresh:
                await self._need_otp("chưa đăng nhập" if observed is None else "hết refresh token")
            if self._refresh_expires and self._now() >= self._refresh_expires - TOKEN_SKEW_S:
                await self._need_otp("refresh token hết hạn")
            spent, self._refresh = self._refresh, None       # BỎ trước khi gửi: không bao giờ gửi lại
            try:
                r = await self.http.request("POST", EP_REFRESH, body={"refreshToken": spent})
            except SsiTransportError as exc:
                self._access, self._expires = None, 0.0
                await self.request_otp(" (refresh lỗi mạng — refresh token coi như đã dùng)")
                raise BrokerError(f"SSI refresh token lỗi mạng ({exc}); không gửi lại — cần OTP") from exc
            if r.error is not None:
                if r.error.reauth or r.error.unauthorized:
                    await self._need_otp(f"refresh bị từ chối [{r.error.code}]")
                self._access, self._expires = None, 0.0
                await self.request_otp(" (refresh lỗi)")
                raise BrokerError(f"SSI refresh lỗi ({r.error}) — cần OTP")
            if not self._store(r.body):
                await self._need_otp("refresh không trả token")
            log.info("ssi: đã refresh token")
            return self._access  # type: ignore[return-value]


# ------------------------------------------------------------------ cấu hình ----

@dataclass(frozen=True)
class SsiConfig:
    account_no: Optional[str]
    client_id: Optional[str]
    order_symbol_format: Optional[str] = None      # "krx" | "name" (❓)
    market_order_type: Optional[str] = None        # "MTL" | "MP" (❓)
    base_url: str = BASE_URL
    stream_url: str = STREAM_URL
    api_verified: bool = False
    timeout_s: float = 10.0
    poll_s: float = 1.0
    uncertain_resolve_s: float = 15.0
    fee_vnd_per_contract: float = 0.0
    tick_size: float = 0.1
    device_id: str = ""
    page_size: int = 1000

    @classmethod
    def from_settings(cls, s: Settings, env: Optional[Mapping[str, str]] = None, *, live: bool) -> "SsiConfig":
        env = os.environ if env is None else env
        g = lambda k, d=None: s.get(f"brokers.ssi.{k}", d)  # noqa: E731
        account = str(env.get("SSI_ACCOUNT") or "").strip() or None
        client_id = str(env.get("SSI_CLIENT_ID") or "").strip() or None
        fmt, mkt = g("order_symbol_format"), g("market_order_type")
        fee_vals = [s.get(f"fees.{k}") for k in FEE_KEYS]
        verified = bool(g("api_verified", False))
        if live:
            if not account:
                raise MissingSetting("SSI_ACCOUNT (.env) — tài khoản phái sinh SSI (đuôi 8)")
            if not client_id:
                raise MissingSetting("SSI_CLIENT_ID (.env)")
            fmt = s.require("brokers.ssi.order_symbol_format")
            mkt = s.require("brokers.ssi.market_order_type")
            fee_vals = [s.require(f"fees.{k}") for k in FEE_KEYS]
            if not account.endswith("8"):
                raise ValueError(f"SSI_ACCOUNT {account} không phải tài khoản phái sinh (phải kết thúc bằng 8)")
            if not verified:
                raise LiveTradingNotAllowed("brokers.ssi.api_verified=false — chạy scripts/ssi_trading_probe.py, "
                                            "đối chiếu các mục ❓ trong docs/broker_notes.md §2.6 rồi mới đặt true")
        if fmt is not None and str(fmt) not in ("krx", "name"):
            raise ValueError(f"brokers.ssi.order_symbol_format phải là krx hoặc name, không phải {fmt!r}")
        if mkt is not None and str(mkt).upper() not in ("MTL", "MP"):
            raise ValueError(f"brokers.ssi.market_order_type phải là MTL hoặc MP, không phải {mkt!r}")
        fee = sum(float(v) for v in fee_vals) if all(v is not None for v in fee_vals) else 0.0
        return cls(
            account_no=account, client_id=client_id,
            order_symbol_format=None if fmt is None else str(fmt),
            market_order_type=None if mkt is None else str(mkt).upper(),
            base_url=str(g("base_url") or BASE_URL).rstrip("/"),
            stream_url=str(g("stream_url") or STREAM_URL),
            api_verified=verified,
            timeout_s=float(g("request_timeout_seconds", 10)),
            poll_s=float(g("poll_seconds", 1)),
            uncertain_resolve_s=float(g("uncertain_resolve_seconds", 15)),
            fee_vnd_per_contract=fee,
            tick_size=float(s.require("contract.tick_size_points")),
            device_id=device_id(),
        )


# ------------------------------------------------------------------ hàm thuần ----

def _order_symbol(symbol: str, fmt: Optional[str]) -> str:
    if fmt == "krx":
        return to_krx(symbol)
    if fmt == "name":
        s = contracts.symbol_contract(symbol)
        if s is None:
            raise ValueError(f"không đổi được {symbol!r} sang VN30FYYMM")
        return s
    raise ValueError("brokers.ssi.order_symbol_format chưa điền (krx|name) — ❓ chưa kiểm")


def _price_text(price: float) -> str:
    return format(Decimal(str(price)), "f")


def build_place_payload(order: Order, config: SsiConfig) -> dict:
    """Body `POST /api/v3/trading/order` đúng khuôn SDK `PlaceOrderRequest.to_dict` (thứ tự khoá giữ nguyên)."""
    if order.qty <= 0:
        raise ValueError("khối lượng phải > 0")
    if order.order_type is OrderType.LIMIT:
        if order.price is None:
            raise ValueError("lệnh LO cần giá")
        otype, price = "LO", _price_text(_on_tick(float(order.price), config.tick_size))
    elif order.order_type is OrderType.MARKET:
        if not config.market_order_type:
            raise ValueError("brokers.ssi.market_order_type chưa điền (MTL|MP) — ❓ chưa kiểm")
        otype, price = config.market_order_type, "0"
    elif order.order_type in (OrderType.ATO, OrderType.ATC):
        otype, price = order.order_type.value, "0"
    else:
        raise ValueError(f"loại lệnh {order.order_type.value} không gửi được qua API SSI (stop do bot giữ)")
    return {"accountNo": config.account_no, "symbol": _order_symbol(order.symbol, config.order_symbol_format),
            "side": _SIDE_CODE[order.side], "quantity": int(order.qty), "price": price, "orderType": otype,
            "clientRequestId": order.client_order_id, "deviceId": config.device_id, "userAgent": USER_AGENT}


def map_order_status(code: Any, qty: Optional[int], filled: Optional[int], canceled: Optional[int]) -> OrderStatus:
    c = str(code or "").strip().upper()
    f = int(filled or 0)
    if c in _ST_REJECTED:
        return OrderStatus.REJECTED
    if qty and f >= int(qty):
        return OrderStatus.FILLED
    if c in _ST_CANCELLED:
        return OrderStatus.CANCELLED
    if c in _ST_FILLED and f > 0:
        return OrderStatus.FILLED
    if f > 0:
        return OrderStatus.PARTIALLY_FILLED
    return OrderStatus.PENDING


def _g(d: Mapping, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return None


def _int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    return int(float(str(v).replace(",", "")))


def _float(v: Any) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ adapter ----

@dataclass
class _Leg:
    order_id: str
    filled: int = 0
    avg: float = 0.0
    status: OrderStatus = OrderStatus.PENDING


@dataclass
class _Track:
    order: Order
    legs: dict = field(default_factory=dict)          # orderId → _Leg
    local_stop: bool = False
    triggered: bool = False
    uncertain_since: Optional[datetime] = None


class SsiBroker(BrokerAdapter):
    def __init__(self, http: SsiHttp, tokens: SsiTokenManager, config: SsiConfig, private_key: Optional[str], *,
                 clock: Clock, name: str = "ssi", live_flag: bool = False, dry_run: bool = False,
                 env: Optional[Mapping[str, str]] = None, notify: Optional[Notify] = None):
        super().__init__(name)
        self.http, self.tokens, self.config = http, tokens, config
        self._key = private_key
        self.clock, self.live_flag, self.dry_run, self.env, self.notify = clock, live_flag, dry_run, env, notify
        self.dry_log: list[dict] = []
        self._tracks: dict[str, _Track] = {}
        self._owner: dict[str, str] = {}                # orderId → client id
        self._seen: set[str] = set()
        self._primed = False
        self._last_sync: Optional[datetime] = None
        self._sync_ok = False
        self.resync_requested = False
        self._notify_seen: set[str] = set()
        self.last_notify_id: Optional[str] = None
        self.stream_net: dict[str, int] = {}
        self.stream_duplicates = 0
        self._warned: set[str] = set()

    def __repr__(self) -> str:
        return f"SsiBroker(name={self.name!r}, account={self.config.account_no!r}, dry_run={self.dry_run})"

    # ------------------------------------------------------------ tiện ích ----

    def order(self, client_order_id: str) -> Optional[Order]:
        t = self._tracks.get(client_order_id)
        return None if t is None else t.order

    @property
    def awaiting_otp(self) -> bool:
        return self.tokens.awaiting_otp

    async def submit_otp(self, code: str) -> bool:
        ok = await self.tokens.submit_otp(code)
        if ok and not self._primed:
            try:
                await self.sync()                         # mốc sổ lệnh ngay khi có token
            except Exception as exc:
                log.warning("ssi: đọc sổ lệnh sau đăng nhập lỗi: %s", exc)
        return ok

    async def _say(self, text: str) -> None:
        log.info("ssi: %s", text)
        if self.notify is None:
            return
        try:
            res = self.notify(text)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("ssi: gửi thông báo lỗi")

    def _acc(self) -> str:
        if not self.config.account_no:
            raise BrokerError("thiếu SSI_ACCOUNT (.env) — tài khoản phái sinh SSI")
        return self.config.account_no

    def _set(self, cid: str, new: Order) -> Order:
        t = self._tracks[cid]
        if new != t.order:
            t.order = new
            self._emit_order(new)
        return new

    def _reject(self, cid: str, reason: str) -> Order:
        t = self._tracks[cid]
        t.uncertain_since = None
        log.warning("ssi: %s bị từ chối: %s", cid, reason)
        return self._set(cid, t.order.with_status(OrderStatus.REJECTED, reject_reason=reason))

    def _dry(self, method: str, path: str, body: dict) -> None:
        self.dry_log.append({"method": method, "path": path, "body": body})
        log.info("ssi DRY-RUN (không gửi): %s %s %s", method, path, json.dumps(body, ensure_ascii=False))

    def _guard(self) -> None:
        assert_live_trading_allowed(self.live_flag, self.env)
        if not self.config.api_verified:
            raise LiveTradingNotAllowed("brokers.ssi.api_verified=false — chưa kiểm API SSI, không gửi lệnh thật")
        if not self._key:
            raise BrokerError("thiếu khoá ký SSI (SSI_PRIVATE_KEY_PATH)")

    async def _mutate(self, method: str, body: dict) -> SsiResponse:
        """Gửi một request thay đổi lệnh (đã qua `_guard`). 401 ⇒ đánh dấu access token hỏng."""
        token = await self.tokens.get()
        r = await self.http.request(method, EP_ORDER, body=body, token=token, private_key=self._key)
        if r.error is not None and r.error.unauthorized:
            self.tokens.invalidate_access()
        return r

    async def _get(self, path: str, params: dict) -> SsiResponse:
        r = await self.http.request("GET", path, params=params, token=await self.tokens.get())
        if r.error is not None and r.error.unauthorized:
            self.tokens.invalidate_access()
            r = await self.http.request("GET", path, params=params, token=await self.tokens.get(force_refresh=True))
        if r.error is not None:
            raise r.error
        return r

    # ------------------------------------------------------------ vòng đời ----

    async def connect(self) -> None:
        acc = self.config.account_no
        if acc is None:
            if not self.dry_run:
                raise BrokerError("thiếu SSI_ACCOUNT (.env)")
            return
        if not acc.endswith("8"):
            raise BrokerError(f"tài khoản SSI {acc} không phải phái sinh (phải kết thúc bằng 8)")
        if self.tokens.valid():
            await self.sync()
        elif not self.tokens.awaiting_otp:
            await self.tokens.request_otp()

    async def ping(self) -> bool:
        now = self.clock.now()
        due = self._last_sync is None or (now - self._last_sync).total_seconds() >= self.config.poll_s
        if due or self.resync_requested:
            self._last_sync, self.resync_requested = now, False
            try:
                await self.sync()
                self._sync_ok = True
            except Exception as exc:
                self._sync_ok = False
                log.warning("ssi: đọc sổ lệnh lỗi: %s", exc)
        return self._sync_ok

    # ------------------------------------------------------------ đặt lệnh ----

    async def place(self, order: Order) -> Order:
        cid = order.client_order_id
        t = self._tracks.get(cid)
        if t is not None:
            return t.order                                   # idempotent
        if len(cid) > MAX_CLIENT_ORDER_ID_LEN:
            raise ValueError("client_order_id > 20 ký tự (giới hạn clientRequestId của SSI)")
        if order.order_type is OrderType.STOP:
            if order.price is None or order.qty <= 0:
                self._tracks[cid] = _Track(order.with_status(OrderStatus.NEW, broker=self.name))
                return self._reject(cid, "stop cần giá kích hoạt và khối lượng > 0")
            if not self.dry_run:
                self._guard()
            self._tracks[cid] = _Track(order.with_status(OrderStatus.NEW, broker=self.name), local_stop=True)
            return self._set(cid, order.with_status(OrderStatus.PENDING, broker=self.name,
                                                    broker_order_id=f"{LOCAL_PREFIX}{cid}"))
        self._tracks[cid] = _Track(order.with_status(OrderStatus.NEW, broker=self.name))
        try:
            payload = build_place_payload(order, self.config)
        except ValueError as exc:
            return self._reject(cid, f"payload không hợp lệ: {exc}")
        if self.dry_run:
            self._dry("POST", EP_ORDER, payload)
            return self._reject(cid, "dry-run: không gửi lệnh (chỉ log payload)")
        try:
            self._guard()
            await self.tokens.get()
        except Exception:
            del self._tracks[cid]                             # chưa gửi gì
            raise
        return await self._send_new(cid, payload)

    async def _send_new(self, cid: str, payload: dict) -> Order:
        t = self._tracks[cid]
        try:
            r = await self._mutate("POST", payload)
        except SsiTransportError as exc:
            return await self._uncertain(cid, str(exc))
        except BrokerError as exc:                           # token mất trước khi gửi: chưa gửi gì
            return self._reject(cid, str(exc))
        if r.error is not None:
            if r.error.uncertain:
                return await self._uncertain(cid, str(r.error))
            if r.error.duplicate:                            # clientRequestId đã có ⇒ lệnh đã tới sàn
                return await self._uncertain(cid, "409 trùng clientRequestId — lệnh đã tồn tại")
            return self._reject(cid, str(r.error))
        d = r.data if isinstance(r.data, dict) else {}
        oid = _g(d, "orderId")
        if oid is None:
            return await self._uncertain(cid, "response không có orderId")
        self._attach(cid, str(oid))
        self._apply(cid, str(oid), d)
        if t.order.status is OrderStatus.NEW:
            self._set(cid, t.order.with_status(OrderStatus.PENDING, broker_order_id=str(oid)))
        return t.order

    async def _uncertain(self, cid: str, why: str) -> Order:
        t = self._tracks[cid]
        t.uncertain_since = self.clock.now()
        await self._say(f"⚠️ SSI: không rõ lệnh {cid} ({why}) — KHÔNG gửi lại, tìm theo clientRequestId "
                        f"trong sổ lệnh tối đa {self.config.uncertain_resolve_s:.0f}s")
        return self._set(cid, t.order.with_status(OrderStatus.PENDING))

    def _attach(self, cid: str, oid: str) -> None:
        t = self._tracks[cid]
        if oid not in t.legs:
            t.legs[oid] = _Leg(oid)
        self._owner[oid] = cid
        self._seen.add(oid)
        t.uncertain_since = None

    def _apply(self, cid: str, oid: str, item: Mapping) -> None:
        """Áp trạng thái CỘNG DỒN của một orderId (REST hoặc stream): Fill trước, cập nhật lệnh sau."""
        t = self._tracks[cid]
        leg = t.legs.get(oid)
        if leg is None or not isinstance(item, Mapping):
            return
        o = t.order
        raw = _g(item, "orderStatus", "status")
        code = str(raw or "").upper()
        if code and code not in KNOWN_STATUS and code not in self._warned:
            self._warned.add(code)
            log.warning("ssi: orderStatus lạ %r — suy từ khối lượng", raw)
        qty = _int(_g(item, "quantity"))
        filled = _int(_g(item, "filledQty", "filledQuantity")) or 0
        canc = _int(_g(item, "cancelQty", "cancelQuantity"))
        avg = _float(_g(item, "avgPrice"))
        status = map_order_status(raw, qty, filled, canc)
        delta = filled - leg.filled
        if delta > 0:
            if avg > 0:
                px = (avg * filled - leg.avg * leg.filled) / delta
            else:
                px = _float(_g(item, "price")) or float(o.price or 0.0)
                log.warning("ssi: %s khớp %d nhưng thiếu avgPrice — tạm dùng %.1f", cid, delta, px)
            leg.filled, leg.avg = filled, avg
            self._emit_fill(Fill(cid, o.symbol, o.side, delta, round(px, 4), self.clock.now(),
                                 fee_vnd=self.config.fee_vnd_per_contract * delta, broker=self.name))
        leg.status = status
        total = sum(lg.filled for lg in t.legs.values())
        if o.status.is_terminal:
            # sự kiện cũ đến muộn (stream không theo thứ tự) không kéo lệnh đã kết thúc về "còn sống"
            if total != o.filled_qty:
                self._set(cid, replace(o, filled_qty=total))
            return
        st = status
        if total >= o.qty:
            st = OrderStatus.FILLED
        elif st in (OrderStatus.PENDING, OrderStatus.FILLED) and total > 0:
            st = OrderStatus.PARTIALLY_FILLED
        reason = o.reject_reason
        if st is OrderStatus.REJECTED and not reason:
            reason = f"SSI từ chối: {_g(item, 'message', 'rejectReason', 'errorMessage') or code}"
        self._set(cid, replace(o, status=st, filled_qty=total, broker_order_id=oid, reject_reason=reason))

    # ------------------------------------------------------------ đồng bộ ----

    async def _book(self) -> list[dict]:
        day = self.clock.now().strftime("%Y/%m/%d")
        out: list[dict] = []
        for page in range(1, 51):
            r = await self._get(EP_ORDER_BOOK, {"accountNo": self._acc(), "from": day, "to": day,
                                                "pageIndex": page, "pageSize": self.config.page_size})
            d = r.data
            if isinstance(d, dict) and str(d.get("code")) == "204":
                break
            rows = d.get("orderList") if isinstance(d, dict) else None
            if rows is None and isinstance(d, dict) and not d:
                rows = []
            if not isinstance(rows, list):
                raise BrokerError("sổ lệnh SSI: response không có 'orderList'")
            out += [x for x in rows if isinstance(x, dict)]
            if len(rows) < self.config.page_size:
                return out
        raise BrokerError("sổ lệnh SSI quá 50 trang — không đọc tiếp")

    async def sync(self) -> None:
        items = await self._book()
        now = self.clock.now()
        by_oid = {str(x["orderId"]): x for x in items if x.get("orderId") not in (None, "")}
        for oid, it in by_oid.items():
            cid = self._owner.get(oid)
            if cid is None:
                rid = str(it.get("clientRequestId") or "")
                t = self._tracks.get(rid)
                if t is not None and not t.order.status.is_terminal and (not t.local_stop or t.triggered):
                    if t.uncertain_since is not None:
                        await self._say(f"✅ SSI: đã thấy lệnh {rid} trong sổ lệnh (orderId {oid})")
                    self._attach(rid, oid)
                    cid = rid
            if cid is not None:
                self._apply(cid, oid, it)
        for cid, t in list(self._tracks.items()):
            if t.uncertain_since is not None and \
                    (now - t.uncertain_since).total_seconds() >= self.config.uncertain_resolve_s:
                self._reject(cid, f"không thấy clientRequestId {cid} trong sổ lệnh sau "
                                  f"{self.config.uncertain_resolve_s:.0f}s — coi như chưa tới sàn")
        await self._adopt(by_oid)
        self._seen.update(by_oid)
        self._primed = True

    async def _adopt(self, by_oid: dict[str, dict]) -> None:
        for oid, it in by_oid.items():
            if oid in self._owner:
                continue
            is_new = self._primed and oid not in self._seen
            qty = _int(_g(it, "quantity")) or 0
            filled = _int(_g(it, "filledQty", "filledQuantity")) or 0
            status = map_order_status(_g(it, "orderStatus"), qty, filled, _int(_g(it, "cancelQty")))
            if status.is_terminal and not (is_new and filled > 0):
                continue
            sym = contracts.symbol_contract(_g(it, "symbol", "instrumentId"))
            side = _SIDE_FROM.get(str(_g(it, "side", "buySell") or "").upper())
            if sym is None or side is None or qty <= 0:
                if f"adopt:{oid}" not in self._warned:
                    self._warned.add(f"adopt:{oid}")
                    log.warning("ssi: bỏ qua lệnh %s không nhận ra", oid)
                continue
            cid = f"{EXTERNAL_PREFIX}{oid}"[-MAX_CLIENT_ORDER_ID_LEN:]
            otype = {"LO": OrderType.LIMIT, "ATO": OrderType.ATO, "ATC": OrderType.ATC}.get(
                str(_g(it, "orderType") or "").upper(), OrderType.MARKET)
            price = _float(_g(it, "price")) if otype is OrderType.LIMIT else None
            o = Order(cid, sym, side, qty, otype, self.clock.now(), price=price, status=OrderStatus.NEW,
                      broker=self.name, broker_order_id=oid, tag="external")
            self._tracks[cid] = _Track(o)
            self._attach(cid, oid)
            if not is_new:
                leg = self._tracks[cid].legs[oid]
                leg.filled, leg.avg = filled, _float(_g(it, "avgPrice"))
                self._tracks[cid].order = replace(o, filled_qty=filled)
            self._apply(cid, oid, it)
            rid = str(it.get("clientRequestId") or "")
            await self._say(f"⚠️ SSI: lệnh ngoài bot orderId {oid} (clientRequestId {rid or '-'}, "
                            f"{side.value} {qty} {sym}) — bot theo dõi khớp lệnh (id {cid})")

    # ------------------------------------------------------------ huỷ / sửa ----

    def _live_track(self, cid: str) -> _Track:
        t = self._tracks.get(cid)
        if t is None:
            raise BrokerError(f"không biết lệnh {cid}")
        if t.order.status.is_terminal:
            raise BrokerError(f"lệnh {cid} đã kết thúc ({t.order.status.value})")
        return t

    def _ref(self, cid: str, t: _Track) -> dict:
        """Lệnh bot đặt: tham chiếu bằng clientRequestId; lệnh ngoài bot: bằng orderId."""
        if t.order.tag == "external":
            return {"orderId": t.order.broker_order_id}
        return {"clientRequestId": cid}

    async def cancel(self, client_order_id: str) -> Order:
        cid = client_order_id
        t = self._live_track(cid)
        if t.local_stop and not t.triggered:
            return self._set(cid, t.order.with_status(OrderStatus.CANCELLED))
        body = {"accountNo": self._acc(), "clientCancelId": _new_id(), "deviceId": self.config.device_id,
                "userAgent": USER_AGENT, **self._ref(cid, t)}
        if self.dry_run:
            self._dry("DELETE", EP_ORDER, body)
            raise BrokerError("dry-run: không huỷ lệnh thật")
        self._guard()
        try:
            r = await self._mutate("DELETE", body)
        except SsiTransportError as exc:
            raise BrokerError(f"huỷ {cid}: không rõ kết quả ({exc}) — sổ lệnh sẽ cho biết") from exc
        if r.error is not None:
            raise r.error
        try:
            await self.sync()                                 # chỉ CANCELLED khi sổ lệnh báo CL
        except Exception as exc:
            log.warning("ssi: đọc sổ lệnh sau huỷ lỗi: %s", exc)
        return self._tracks[cid].order

    async def modify(self, client_order_id: str, *, price: Optional[float] = None,
                     qty: Optional[int] = None) -> Order:
        """SSI sửa giá HOẶC khối lượng mỗi lần ⇒ hai request khi đổi cả hai (giá trước).
        `quantity` gửi là tổng khối lượng lệnh (❓ broker_notes §2.6)."""
        cid = client_order_id
        t = self._live_track(cid)
        o = t.order
        if price is not None:
            price = _on_tick(float(price), self.config.tick_size)
        if qty is not None and qty <= o.filled_qty:
            raise BrokerError(f"khối lượng mới {qty} phải > phần đã khớp {o.filled_qty}")
        if t.local_stop and not t.triggered:
            return self._set(cid, replace(o, price=o.price if price is None else price,
                                          qty=o.qty if qty is None else int(qty)))
        changes = ([{"price": _price_text(price)}] if price is not None else []) + \
                  ([{"quantity": int(qty)}] if qty is not None else [])
        for ch in changes:
            body = {"accountNo": self._acc(), "clientModifyId": _new_id(), "deviceId": self.config.device_id,
                    "userAgent": USER_AGENT, **self._ref(cid, t), **ch}
            if self.dry_run:
                self._dry("PUT", EP_ORDER, body)
                raise BrokerError("dry-run: không sửa lệnh thật")
            self._guard()
            try:
                r = await self._mutate("PUT", body)
            except SsiTransportError as exc:
                raise BrokerError(f"sửa {cid}: không rõ kết quả ({exc}) — sổ lệnh sẽ cho biết") from exc
            if r.error is not None:
                raise r.error
            cur = self._tracks[cid].order
            self._set(cid, replace(cur, price=float(ch["price"]) if "price" in ch else cur.price,
                                   qty=ch.get("quantity", cur.qty)))
        return self._tracks[cid].order

    # ------------------------------------------------------------ stop do bot giữ ----

    async def on_tick(self, tick: Tick) -> None:
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
        t.triggered = True                                   # trước khi gửi: không bao giờ gửi đôi
        o = t.order
        mkt = replace(o, order_type=OrderType.MARKET, price=None, qty=o.qty - o.filled_qty)
        log.warning("ssi: stop %s chạm %.1f ⇒ gửi lệnh thị trường %s %d", cid, float(o.price), o.side.value, mkt.qty)
        try:
            payload = build_place_payload(mkt, self.config)
        except ValueError as exc:
            self._reject(cid, f"stop chạm nhưng payload lỗi: {exc}")
            return
        if self.dry_run:
            self._dry("POST", EP_ORDER, payload)
            self._reject(cid, "dry-run: stop chạm, không gửi lệnh")
            return
        try:
            self._guard()
        except Exception as exc:
            self._reject(cid, f"stop chạm nhưng không gửi được: {exc}")
            await self._say(f"🚨 SSI: stop {cid} chạm nhưng KHÔNG gửi được ({exc})")
            return
        await self._send_new(cid, payload)

    # ------------------------------------------------------------ stream TRADING ----

    def on_stream_frame(self, frame: Any) -> None:
        """Một frame kênh TRADING (`order.<tk>` / `portfolio.<tk>`). Không ném lỗi."""
        try:
            self._on_stream_frame(frame)
        except Exception:
            log.exception("ssi: xử lý frame stream lỗi")

    def _on_stream_frame(self, frame: Any) -> None:
        if not isinstance(frame, dict) or frame.get("channel") != "TRADING":
            return
        topic = str(frame.get("topic") or "")
        data = frame.get("data")
        if not isinstance(data, dict) or topic not in (f"order.{self.config.account_no}",
                                                       f"portfolio.{self.config.account_no}"):
            return
        nid = str(data.get("notifyId") or "")
        if nid:
            if nid in self._notify_seen:
                self.stream_duplicates += 1
                return
            self._notify_seen.add(nid)
            self.last_notify_id = nid
        event = data.get("eventType") or ("orderEvent" if topic.startswith("order.") else "clientPortfolioEvent")
        if event == "clientPortfolioEvent":
            sym = contracts.symbol_contract(data.get("symbol"))
            if sym is not None and data.get("net") is not None:
                self.stream_net[sym] = _int(data["net"]) or 0
            return
        if event == "orderMatchEvent":
            self.resync_requested = True                     # delta: để sổ lệnh REST cộng dồn
            return
        if event != "orderEvent":
            return
        oid = str(data.get("orderId") or "")
        cid = self._owner.get(oid)
        if cid is None:
            rid = str(data.get("clientRequestId") or "")
            t = self._tracks.get(rid)
            if t is None or t.order.status.is_terminal or not oid:
                self.resync_requested = True                 # lạ ⇒ để sync nhận theo dõi
                return
            self._attach(rid, oid)
            cid = rid
        self._apply(cid, oid, data)

    # ------------------------------------------------------------ vị thế / lệnh chờ ----

    async def positions(self) -> dict[str, Position]:
        r = await self._get(EP_POSITIONS, {"clientId": self.config.client_id or "", "querySummary": "true",
                                           "accountNo": self._acc()})
        d = r.data if isinstance(r.data, dict) else {}
        der = d.get("derivative")
        rows = der.get("derOpenPositions") if isinstance(der, dict) else der if isinstance(der, list) else []
        if not isinstance(rows, list):
            raise BrokerError("vị thế SSI: 'derOpenPositions' không phải danh sách")
        out: dict[str, Position] = {}
        for p in rows:
            if not isinstance(p, dict):
                continue
            long_, short, net = _int(p.get("long")) or 0, _int(p.get("short")) or 0, _int(p.get("net"))
            if net is None:
                net = long_ - short
            if net != long_ - short:
                raise BrokerError(f"vị thế SSI {p.get('symbol')}: net {net} ≠ long {long_} − short {short} — không đoán")
            sym = contracts.symbol_contract(p.get("symbol"))
            if sym is None:
                log.warning("ssi: bỏ qua vị thế mã %r (không phải VN30F)", p.get("symbol"))
                continue
            if net == 0:
                continue
            avg = _float(p.get("bidAvgPrice") if net > 0 else p.get("askAvgPrice"))
            prev = out.get(sym)
            out[sym] = Position(sym, net + (prev.qty if prev else 0), avg, broker=self.name)
        return out

    async def open_orders(self) -> list[Order]:
        await self.sync()
        return [t.order for t in self._tracks.values() if not t.order.status.is_terminal]


def _new_id(size: int = 20) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(size))


# ------------------------------------------------------------------ stream ----

class SsiOrderStream:
    """Kết nối kênh TRADING của stream SSI, chuyển frame cho `SsiBroker.on_stream_frame`.

    Mỗi lần (re)connect ⇒ `broker.resync_requested = True`: vòng `ping()` kế tiếp đọc lại sổ lệnh
    REST, nên sự kiện lỡ trong lúc mất kết nối không bị mất (SDK không có phát lại theo notifyId).
    """

    def __init__(self, tokens: SsiTokenManager, broker: SsiBroker, *, connector=None, url: Optional[str] = None,
                 heartbeat_s: float = 30.0, recv_timeout_s: float = 65.0, backoff_base_s: float = 1.0,
                 backoff_max_s: float = 60.0, backoff_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 jitter: Callable[[], float] = random.random):
        if connector is None:
            from feeds.data_feed_ssi import websockets_connector
            connector = websockets_connector
        self.tokens, self.broker, self._connector = tokens, broker, connector
        self.url = url or broker.config.stream_url
        self.heartbeat_s, self.recv_timeout_s = heartbeat_s, recv_timeout_s
        self.backoff_base_s, self.backoff_max_s = backoff_base_s, backoff_max_s
        self._sleep, self._jitter = backoff_sleep, jitter
        self.stopping = False
        self.connects = 0

    async def run(self) -> None:
        attempt, force = 0, False
        while not self.stopping:
            try:
                token = await self.tokens.get(force_refresh=force)
                force = False
                got = await self._session(token)
                if got:
                    attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = getattr(exc, "status", None) or getattr(exc, "code", None)
                force = status in (401, 403)
                log.warning("ssi stream lệnh: mất kết nối (%s: %s)", type(exc).__name__, exc)
            if self.stopping:
                break
            wait = min(self.backoff_base_s * (2 ** attempt), self.backoff_max_s) * (0.5 + 0.5 * self._jitter())
            attempt += 1
            await self._sleep(wait)

    async def stop(self) -> None:
        self.stopping = True

    async def _session(self, token: str) -> bool:
        tr = await self._connector(self.url, {"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json", "Accept": "application/json"})
        acc = self.broker.config.account_no
        hb = None
        got = False
        try:
            await tr.send(json.dumps({"method": "subscribe", "channel": "TRADING",
                                      "topics": [f"order.{acc}", f"portfolio.{acc}"]}))
            self.connects += 1
            self.broker.resync_requested = True               # đọc lại REST: bù sự kiện lỡ khi mất kết nối
            hb = asyncio.create_task(self._heartbeat(tr))
            while not self.stopping:
                raw = await asyncio.wait_for(tr.recv(), self.recv_timeout_s)
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(msg, dict) and "channel" not in msg and "code" in msg:
                    raise BrokerError(f"stream SSI từ chối: [{msg.get('code')}] {str(msg.get('msg'))[:100]}")
                if isinstance(msg, dict) and msg.get("status") == "error":
                    raise BrokerError(f"stream SSI từ chối đăng ký: {str(msg.get('message'))[:100]}")
                if isinstance(msg, dict) and msg.get("channel") == "TRADING" and "data" in msg:
                    got = True
                    self.broker.on_stream_frame(msg)
        finally:
            if hb is not None:
                hb.cancel()
            try:
                await tr.close()
            except Exception:
                pass
        return got

    async def _heartbeat(self, tr) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                await tr.send(json.dumps({"method": "ping_pong", "channel": "HEARTBEAT", "topics": []}))
            except Exception:
                return
