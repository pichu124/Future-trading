"""Adapter TCBS iFlash Open API — broker THỨ 3, tuỳ chọn, KHÔNG bật mặc định (CLAUDE.md mục 3).

Theo tài liệu chính thức developers.tcbs.com.vn v1.0.0 + đặc tả OpenAPI `openapi-v1.0.0.json`
(docs/broker_notes.md §2b). CLAUDE.md: chỉ bật (`brokers.tcbs_enabled: true`) sau khi DNSE + SSI ổn định.

An toàn:
- Token = API Key + **iOTP người dùng đọc trong app TCInvest** (API không gửi mã) ⇒ `/otp tcbs <mã>`; sống tối
  đa 8 giờ (hoặc `exp` trong JWT nếu sớm hơn), không có refresh ⇒ hết hạn là `trading_ready = False`.
- `refId` KHÔNG chống trùng (gửi lại = lệnh mới) ⇒ adapter KHÔNG BAO GIỜ gửi lại. Timeout / 5xx không có mã
  nghiệp vụ ⇒ "không rõ": tra sổ lệnh theo `refId` (đúng cách tài liệu chỉ), quá `uncertain_resolve_s` (≥ 15s
  theo tài liệu) không thấy ⇒ REJECTED. Mã lỗi nghiệp vụ trong bảng của TCBS (kể cả HTTP 500, vd -9999) ⇒
  chắc chắn chưa tiếp nhận.
- Lệnh thị trường: API đặt lệnh không mô tả MTL (❓) ⇒ LO giá TRẦN (mua) / SÀN (bán) lấy từ
  `/tartarus/v1/derivatives` — hợp lệ theo tài liệu và khớp như lệnh thị trường.
- Sửa lệnh: một request cho giá, một cho khối lượng (TCBS cấm sửa đồng thời, -8020); `nvol` là tổng.
- Trước MỌI request thay đổi: TRADING_MODE=live + `--live` + `brokers.tcbs_enabled` + `brokers.tcbs.api_verified` +
  token còn hạn. `dry_run` chỉ log payload.
- Khớp lệnh: đọc sổ lệnh trong ngày mỗi `poll_s` (`matchVolume` cộng dồn, `matchPriceBQ` bình quân) ⇒ Fill rồi
  cập nhật lệnh. `TcbsOrderStream` (wss …/ws/nesoi, DE_ORDER + DE_OPEN_POSITION) áp cùng dữ liệu cộng dồn ngay
  khi có (trùng không đếm đôi, bản tin cũ không kéo lùi trạng thái); stream không số thứ tự, không phát lại ⇒
  mỗi lần (re)connect và mỗi lệnh lạ ⇒ đọc lại REST ngay — REST vẫn là nguồn sự thật.
- Stop: bot giữ (như DNSE/SSI); TCBS có lệnh điều kiện SOU/SOL nhưng liên kết lệnh kích hoạt với client id ❓.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import time as _time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Optional

from brokers.base import BrokerAdapter, BrokerError, LiveTradingNotAllowed, assert_live_trading_allowed
from brokers.dnse import _on_tick, to_krx
from brokers.paper import FEE_KEYS
from core import contracts
from core.clock import Clock
from core.models import MAX_CLIENT_ORDER_ID_LEN, Fill, Order, OrderStatus, OrderType, Position, Side, Tick
from core.settings import MissingSetting, Settings

log = logging.getLogger(__name__)

BASE_URL = "https://openapi.tcbs.com.vn"
EP_TOKEN = "/gaia/v1/oauth2/openapi/token"
EP_KEY_INFO = "/gaia/v1/oauth2/openapi/apikey/info"
EP_PLACE = "/khronos/v1/order/place"
EP_CHANGE = "/khronos/v1/order/change"
EP_CANCEL = "/khronos/v1/order/cancel"
EP_BOOK = "/khronos/v1/order/in-day"
EP_POSITIONS = "/khronos/v1/account/portfolio/status"
EP_DERIVATIVES = "/tartarus/v1/derivatives"
WS_URL = "wss://openapi.tcbs.com.vn/ws/nesoi"
TOKEN_MAX_S = 8 * 3600            # tài liệu: access token hiệu lực tối đa 8 giờ
TOKEN_SKEW_S = 30.0
API_KEY_WARN_DAYS = 7
EXTERNAL_PREFIX = "EXT"
LOCAL_PREFIX = "local:"

# Mã lỗi nghiệp vụ trong tài liệu (8.1) ⇒ lệnh/yêu cầu CHƯA được tiếp nhận, kể cả khi đi kèm HTTP 500.
KNOWN_REJECT_CODES = frozenset({
    "-508", "-6012", "-6016", "-6017", "-6018", "-6019", "-6021", "-6023", "-6027", "-6029", "-6030", "-6033",
    "-6034", "-6035", "-6036", "-6041", "-6051", "-6052", "-6053", "-6054", "-6673", "-6674", "-6675",
    "-8001", "-8002", "-8021", "-8022", "-8026", "-8024", "-8025", "-8027", "-9999",
    "-6004", "-6014", "-8004", "-8023", "-8018", "-8020", "-8003", "-9998"})
ALREADY_DONE_CODES = frozenset({"-6004", "-8004"})      # huỷ lệnh đã huỷ / đã khớp hết

_SIDE_CODE = {Side.BUY: "B", Side.SELL: "S"}
_SIDE_FROM = {"B": Side.BUY, "S": Side.SELL}

Notify = Callable[[str], Any]
# (method, url, params, json_body, headers, timeout) -> (status, headers, text). Không được tự retry.
Transport = Callable[[str, str, Optional[dict], Optional[dict], dict, float],
                     Awaitable[tuple[int, Mapping[str, str], str]]]


# ------------------------------------------------------------------ HTTP ----

class TcbsTransportError(BrokerError):
    """Lỗi mạng/timeout — KHÔNG biết server đã xử lý request hay chưa."""


class TcbsApiError(BrokerError):
    """Server báo lỗi. Chỉ giữ `code` + `message` cắt ngắn."""

    def __init__(self, status: int, body: Any):
        self.status = int(status)
        b = body if isinstance(body, dict) else {}
        code = b.get("code")
        if code is None and "rc" in b:
            code = b.get("rc")
        self.code = "" if code is None else str(code)
        self.message = str(b.get("message") or b.get("rs") or b.get("msg") or "")[:200]
        super().__init__(f"HTTP {self.status} [{self.code}] {self.message}".strip())

    @property
    def known_reject(self) -> bool:
        return self.code in KNOWN_REJECT_CODES

    @property
    def uncertain(self) -> bool:
        """5xx/408 KHÔNG kèm mã nghiệp vụ ⇒ có thể đã tiếp nhận (tài liệu 8.1)."""
        return (self.status >= 500 or self.status == 408) and not self.known_reject

    @property
    def unauthorized(self) -> bool:
        return self.status in (401, 403)


@dataclass(frozen=True)
class TcbsResponse:
    status: int
    body: Any
    error: Optional[TcbsApiError] = None


def _requests_transport() -> Transport:
    state: dict = {}

    async def transport(method, url, params, json_body, headers, timeout):
        def go():
            import requests
            sess = state.get("s")
            if sess is None:
                sess = state["s"] = requests.Session()
            r = sess.request(method, url, params=params, json=json_body, headers=headers, timeout=timeout)
            return r.status_code, dict(r.headers), r.text
        return await asyncio.to_thread(go)

    return transport


class TcbsHttp:
    """REST TCBS. MỘT request = MỘT lần gửi: không retry."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 10.0, transport: Optional[Transport] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport or _requests_transport()

    def __repr__(self) -> str:
        return f"TcbsHttp(base_url={self.base_url!r})"

    async def request(self, method: str, path: str, *, params: Optional[dict] = None, body: Optional[dict] = None,
                      token: Optional[str] = None) -> TcbsResponse:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            status, _h, text = await self._transport(method.upper(), self.base_url + path, params, body, headers,
                                                     self.timeout)
        except Exception as exc:
            raise TcbsTransportError(f"{method} {path}: {type(exc).__name__}: {str(exc)[:120]}") from exc
        status = int(status)
        parsed: Any = None
        if text and text.strip():
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = {"message": text[:200]}
        log.debug("tcbs %s %s → %s", method, path, status)
        err = None
        if status >= 400:
            err = TcbsApiError(status, parsed)
        elif isinstance(parsed, dict) and "rc" in parsed:
            try:
                rc = int(str(parsed.get("rc")))
            except ValueError:
                rc = 0
            if rc < 1:
                err = TcbsApiError(status, parsed)
        return TcbsResponse(status, parsed, err)


# ------------------------------------------------------------------ token ----

def _jwt_exp(token: str) -> Optional[float]:
    """`exp` trong JWT (không kiểm chữ ký — chỉ để biết hạn). Không đọc được ⇒ None."""
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        exp = data.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


class TcbsTokenManager:
    name = "tcbs"                                     # tên trong `/otp tcbs <mã>`

    def __init__(self, http: TcbsHttp, api_key: Optional[str], *, now: Callable[[], float] = _time.time,
                 notify: Optional[Notify] = None):
        self.http, self._key, self._now, self.notify = http, api_key, now, notify
        self._token: Optional[str] = None
        self._expires = 0.0
        self.awaiting_otp = False

    def __repr__(self) -> str:
        return f"TcbsTokenManager(token={'có' if self._token else 'không'}, awaiting_otp={self.awaiting_otp})"

    async def _say(self, text: str) -> None:
        log.info("tcbs: %s", text)
        if self.notify is None:
            return
        try:
            res = self.notify(text)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("tcbs: gửi thông báo lỗi")

    @property
    def trading_ready(self) -> bool:
        return bool(self._token) and self._now() < self._expires - TOKEN_SKEW_S

    def invalidate(self) -> None:
        self._token, self._expires = None, 0.0

    async def connect(self) -> None:
        if not self.trading_ready and not self.awaiting_otp:
            await self._ask("")

    async def _ask(self, why: str) -> None:
        self.awaiting_otp = True
        await self._say(f"🔐 TCBS{why}: mở app TCInvest lấy mã iOTP, trả lời /otp tcbs <mã> "
                        "(token sống tối đa 8 giờ)")

    async def submit_otp(self, code: str) -> bool:
        code = str(code or "").strip()
        if not (4 <= len(code) <= 8 and code.isdigit()):
            await self._say("❌ iOTP TCBS phải là 4–8 chữ số")
            return False
        if not self._key:
            await self._say("❌ Thiếu TCBS_API_KEY (.env)")
            return False
        try:
            r = await self.http.request("POST", EP_TOKEN, body={"apiKey": self._key, "otp": code})
        except TcbsTransportError as exc:
            await self._say(f"❌ TCBS: không gửi được iOTP ({exc}) — thử lại /otp tcbs")
            return False
        token = r.body.get("token") if r.error is None and isinstance(r.body, dict) else None
        if not token:
            await self._say(f"❌ TCBS từ chối iOTP ({r.error or 'không có token'}) — lấy mã mới rồi /otp tcbs lại")
            return False
        now = self._now()
        exp = _jwt_exp(str(token))
        self._token = str(token)
        self._expires = min(now + TOKEN_MAX_S, exp) if exp else now + TOKEN_MAX_S
        self.awaiting_otp = False
        await self._say(f"✅ Đã có token TCBS (hết hạn {datetime.fromtimestamp(self._expires):%H:%M})")
        return True

    async def get(self) -> str:
        if self.trading_ready:
            return self._token  # type: ignore[return-value]
        self.invalidate()
        if not self.awaiting_otp:
            await self._ask(" (token hết hạn)")
        raise BrokerError("TCBS cần iOTP mới — /otp tcbs <mã>")

    async def check_api_key(self) -> Optional[float]:
        """Số ngày còn lại của API Key (cảnh báo khi < 7 ngày); lỗi ⇒ None."""
        if not self._key:
            return None
        try:
            r = await self.http.request("POST", EP_KEY_INFO, body={"apiKey": self._key})
        except TcbsTransportError:
            return None
        exp = r.body.get("expireDate") if r.error is None and isinstance(r.body, dict) else None
        if not exp:
            return None
        try:
            days = (datetime.fromisoformat(str(exp)).timestamp() - self._now()) / 86400
        except ValueError:
            return None
        if days < API_KEY_WARN_DAYS:
            await self._say(f"⚠️ TCBS: API Key hết hạn sau {days:.1f} ngày ({exp}) — gia hạn trong TCInvest")
        return days


# ------------------------------------------------------------------ cấu hình ----

@dataclass(frozen=True)
class TcbsConfig:
    api_key: Optional[str]
    custody_code: Optional[str]
    sub_account: Optional[str]
    enabled: bool = False
    api_verified: bool = False
    base_url: str = BASE_URL
    timeout_s: float = 10.0
    poll_s: float = 1.0
    uncertain_resolve_s: float = 15.0
    fee_vnd_per_contract: float = 0.0
    tick_size: float = 0.1
    page_size: int = 100

    def __repr__(self) -> str:                               # không in API key
        return (f"TcbsConfig(custody={self.custody_code!r}, sub={self.sub_account!r}, enabled={self.enabled}, "
                f"verified={self.api_verified})")

    @classmethod
    def from_settings(cls, s: Settings, env: Optional[Mapping[str, str]] = None, *, live: bool) -> "TcbsConfig":
        env = os.environ if env is None else env
        g = lambda k, d=None: s.get(f"brokers.tcbs.{k}", d)  # noqa: E731
        e = lambda k: str(env.get(k) or "").strip() or None  # noqa: E731
        enabled = bool(s.get("brokers.tcbs_enabled", False))
        verified = bool(g("api_verified", False))
        fee_vals = [s.get(f"fees.{k}") for k in FEE_KEYS]
        if live:
            if not enabled:
                raise LiveTradingNotAllowed("brokers.tcbs_enabled=false — TCBS chỉ bật sau khi DNSE + SSI ổn định "
                                            "(CLAUDE.md mục 3)")
            if not verified:
                raise LiveTradingNotAllowed("brokers.tcbs.api_verified=false — chạy scripts/tcbs_trading_probe.py, "
                                            "đối chiếu docs/broker_notes.md §2b rồi mới đặt true")
            for k, what in (("TCBS_API_KEY", "API Key iFlash Open API"), ("TCBS_CUSTODY_CODE", "số lưu ký"),
                            ("TCBS_DERIVATIVE_ACCOUNT", "tiểu khoản phái sinh")):
                if not e(k):
                    raise MissingSetting(f"{k} (.env) — {what}")
            fee_vals = [s.require(f"fees.{k}") for k in FEE_KEYS]
        fee = sum(float(v) for v in fee_vals) if all(v is not None for v in fee_vals) else 0.0
        return cls(
            api_key=e("TCBS_API_KEY"), custody_code=e("TCBS_CUSTODY_CODE"), sub_account=e("TCBS_DERIVATIVE_ACCOUNT"),
            enabled=enabled, api_verified=verified, base_url=str(g("base_url") or BASE_URL).rstrip("/"),
            timeout_s=float(g("request_timeout_seconds", 10)), poll_s=float(g("poll_seconds", 1)),
            uncertain_resolve_s=float(g("uncertain_resolve_seconds", 15)), fee_vnd_per_contract=fee,
            tick_size=float(s.require("contract.tick_size_points")))


# ------------------------------------------------------------------ hàm thuần ----

def ref_id_for(client_order_id: str, n: int = 0) -> str:
    """refId 10–100 ký tự, suy ra từ client id (tra sổ lệnh theo refId sau timeout). `n` > 0: lần sửa thứ n."""
    r = f"FT-{client_order_id}" + (f".m{n}" if n else "")
    if len(r) < 10:
        r += "." + "0" * (9 - len(r))
    return r[:100]


def build_place_payload(order: Order, config: TcbsConfig, band: Optional[tuple[float, float]]) -> dict:
    """Body `POST /khronos/v1/order/place`. `band` = (trần, sàn) — bắt buộc cho lệnh thị trường."""
    if order.qty <= 0:
        raise ValueError("khối lượng phải > 0")
    if order.order_type is OrderType.LIMIT:
        if order.price is None:
            raise ValueError("lệnh LO cần giá")
        price = _on_tick(float(order.price), config.tick_size)
    elif order.order_type is OrderType.MARKET:
        if band is None:
            raise ValueError("lệnh thị trường cần giá trần/sàn (LO giá trần/sàn) — chưa lấy được")
        price = float(band[0] if order.side is Side.BUY else band[1])
    else:
        raise ValueError(f"loại lệnh {order.order_type.value} không gửi được qua API TCBS")
    return {"accountId": config.custody_code, "subAccountId": config.sub_account, "side": _SIDE_CODE[order.side],
            "symbol": to_krx(order.symbol), "price": price, "volume": int(order.qty), "orderType": "0",
            "refId": ref_id_for(order.client_order_id)}


def map_order_status(code: Any, status: Any, volume: Any, matched: Any) -> OrderStatus:
    """`orderStatus` 1–7 (tài liệu 8.1) + chuỗi `status` (P/M/C/W/X/J); khối lượng là chốt chặn cuối."""
    c = str(code or "").strip()
    s = str(status or "").strip().upper()
    v, m = _int(volume) or 0, _int(matched) or 0
    if v and m >= v:
        return OrderStatus.FILLED
    if c == "7":
        return OrderStatus.REJECTED if m == 0 else OrderStatus.CANCELLED
    if c in ("4", "5"):
        return OrderStatus.CANCELLED
    if c not in ("1", "2", "3", "6") and s.endswith("X"):
        return OrderStatus.REJECTED if ("J" in s and m == 0) else OrderStatus.CANCELLED
    return OrderStatus.PARTIALLY_FILLED if m > 0 else OrderStatus.PENDING


def _int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except ValueError:
        return None


def _float(v: Any) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ adapter ----

@dataclass
class _Track:
    order: Order
    ref: str
    order_no: Optional[str] = None
    filled: int = 0
    avg: float = 0.0
    local_stop: bool = False
    triggered: bool = False
    uncertain_since: Optional[datetime] = None
    modifies: int = 0


class TcbsBroker(BrokerAdapter):
    def __init__(self, http: TcbsHttp, tokens: TcbsTokenManager, config: TcbsConfig, *, clock: Clock,
                 name: str = "tcbs", live_flag: bool = False, dry_run: bool = False,
                 env: Optional[Mapping[str, str]] = None, notify: Optional[Notify] = None):
        super().__init__(name)
        self.http, self.tokens, self.config, self.clock = http, tokens, config, clock
        self.live_flag, self.dry_run, self.env, self.notify = live_flag, dry_run, env, notify
        self.dry_log: list[dict] = []
        self._tracks: dict[str, _Track] = {}
        self._by_no: dict[str, str] = {}
        self._by_ref: dict[str, str] = {}
        self._seen: set[str] = set()
        self._primed = False
        self._last_sync: Optional[datetime] = None
        self._sync_ok = False
        self._bands: dict[tuple, tuple[float, float]] = {}
        self._warned: set[str] = set()
        self.resync_requested = False                   # stream: lệnh lạ / vừa (re)connect ⇒ đọc lại REST ngay
        self.stream_net: dict[str, int] = {}

    def __repr__(self) -> str:
        return f"TcbsBroker(name={self.name!r}, custody={self.config.custody_code!r}, dry_run={self.dry_run})"

    # ------------------------------------------------------------ tiện ích ----

    def order(self, client_order_id: str) -> Optional[Order]:
        t = self._tracks.get(client_order_id)
        return None if t is None else t.order

    @property
    def trading_ready(self) -> bool:
        return self.dry_run or self.tokens.trading_ready

    @property
    def awaiting_otp(self) -> bool:
        return self.tokens.awaiting_otp

    async def submit_otp(self, code: str) -> bool:
        ok = await self.tokens.submit_otp(code)
        if ok and not self._primed:
            try:
                await self.sync()
            except Exception as exc:
                log.warning("tcbs: đọc sổ lệnh sau đăng nhập lỗi: %s", exc)
        return ok

    async def _say(self, text: str) -> None:
        log.info("tcbs: %s", text)
        if self.notify is None:
            return
        try:
            res = self.notify(text)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("tcbs: gửi thông báo lỗi")

    def _set(self, cid: str, new: Order) -> Order:
        t = self._tracks[cid]
        if new != t.order:
            t.order = new
            self._emit_order(new)
        return new

    def _reject(self, cid: str, reason: str) -> Order:
        t = self._tracks[cid]
        t.uncertain_since = None
        log.warning("tcbs: %s bị từ chối: %s", cid, reason)
        return self._set(cid, t.order.with_status(OrderStatus.REJECTED, reject_reason=reason))

    def _dry(self, path: str, body: dict) -> None:
        self.dry_log.append({"path": path, "body": body})
        log.info("tcbs DRY-RUN (không gửi): POST %s %s", path, json.dumps(body, ensure_ascii=False))

    def _guard(self) -> None:
        assert_live_trading_allowed(self.live_flag, self.env)
        if not self.config.enabled:
            raise LiveTradingNotAllowed("brokers.tcbs_enabled=false — TCBS không bật (CLAUDE.md mục 3)")
        if not self.config.api_verified:
            raise LiveTradingNotAllowed("brokers.tcbs.api_verified=false — chưa kiểm API TCBS, không gửi lệnh thật")
        if not self.tokens.trading_ready:
            raise BrokerError("TCBS chưa có token — mở TCInvest lấy iOTP, gửi /otp tcbs <mã>")

    def _acc(self) -> tuple[str, str]:
        if not self.config.custody_code or not self.config.sub_account:
            raise BrokerError("thiếu TCBS_CUSTODY_CODE / TCBS_DERIVATIVE_ACCOUNT (.env)")
        return self.config.custody_code, self.config.sub_account

    async def _get(self, path: str, params: dict) -> TcbsResponse:
        r = await self.http.request("GET", path, params=params, token=await self.tokens.get())
        if r.error is not None:
            if r.error.unauthorized:
                self.tokens.invalidate()
            raise r.error
        return r

    async def _post(self, path: str, body: dict) -> TcbsResponse:
        r = await self.http.request("POST", path, body=body, token=await self.tokens.get())
        if r.error is not None and r.error.unauthorized:
            self.tokens.invalidate()
        return r

    async def _band(self, symbol: str) -> Optional[tuple[float, float]]:
        """(trần, sàn) của hợp đồng trong ngày, cache theo ngày."""
        key = (self.clock.now().date(), symbol)
        if key in self._bands:
            return self._bands[key]
        r = await self._get(EP_DERIVATIVES, {})
        rows = r.body.get("data") if isinstance(r.body, dict) else None
        for row in rows or []:
            if isinstance(row, dict) and contracts.symbol_contract(row.get("symbol")) == symbol:
                ceil, floor = _float(row.get("ceilPrice")), _float(row.get("floorPrice"))
                if ceil > 0 and floor > 0:
                    self._bands[key] = (ceil, floor)
                    return self._bands[key]
        return None

    # ------------------------------------------------------------ vòng đời ----

    async def connect(self) -> None:
        if not self.dry_run:
            self._acc()
        await self.tokens.connect()
        if self.tokens.trading_ready:
            await self.sync()
            await self.tokens.check_api_key()

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
                log.warning("tcbs: đọc sổ lệnh lỗi: %s", exc)
        return self._sync_ok and self.trading_ready

    # ------------------------------------------------------------ đặt lệnh ----

    async def place(self, order: Order) -> Order:
        cid = order.client_order_id
        t = self._tracks.get(cid)
        if t is not None:
            return t.order                                   # idempotent: KHÔNG gửi lại
        if len(cid) > MAX_CLIENT_ORDER_ID_LEN:
            raise ValueError("client_order_id > 20 ký tự")
        if order.order_type is OrderType.STOP:
            if order.price is None or order.qty <= 0:
                self._tracks[cid] = _Track(order.with_status(OrderStatus.NEW, broker=self.name), ref_id_for(cid))
                return self._reject(cid, "stop cần giá kích hoạt và khối lượng > 0")
            if not self.dry_run:
                self._guard()
            self._tracks[cid] = _Track(order.with_status(OrderStatus.NEW, broker=self.name), ref_id_for(cid),
                                       local_stop=True)
            return self._set(cid, order.with_status(OrderStatus.PENDING, broker=self.name,
                                                    broker_order_id=f"{LOCAL_PREFIX}{cid}"))
        if not self.dry_run:
            self._guard()
        self._tracks[cid] = _Track(order.with_status(OrderStatus.NEW, broker=self.name), ref_id_for(cid))
        self._by_ref[ref_id_for(cid)] = cid
        return await self._send(cid, order)

    async def _send(self, cid: str, order: Order) -> Order:
        t = self._tracks[cid]
        try:
            band = await self._band(order.symbol) if order.order_type is OrderType.MARKET else None
            payload = build_place_payload(order, self.config, band)
        except ValueError as exc:
            return self._reject(cid, f"payload không hợp lệ: {exc}")
        except BrokerError as exc:
            return self._reject(cid, f"không lấy được giá trần/sàn: {exc}")
        if self.dry_run:
            self._dry(EP_PLACE, payload)
            return self._reject(cid, "dry-run: không gửi lệnh (chỉ log payload)")
        try:
            r = await self._post(EP_PLACE, payload)
        except TcbsTransportError as exc:
            return await self._uncertain(cid, str(exc))
        except BrokerError as exc:                           # token mất trước khi gửi: chưa gửi gì
            return self._reject(cid, str(exc))
        if r.error is not None:
            if r.error.uncertain:
                return await self._uncertain(cid, str(r.error))
            return self._reject(cid, str(r.error))
        rows = r.body.get("data") if isinstance(r.body, dict) else None
        no = rows[0].get("orderNo") if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if no is None:
            return await self._uncertain(cid, "response không có orderNo")
        self._attach(cid, str(no))
        return self._set(cid, t.order.with_status(OrderStatus.PENDING, broker_order_id=str(no)))

    async def _uncertain(self, cid: str, why: str) -> Order:
        t = self._tracks[cid]
        t.uncertain_since = self.clock.now()
        await self._say(f"⚠️ TCBS: không rõ lệnh {cid} ({why}) — KHÔNG gửi lại (refId không chống trùng), tra sổ "
                        f"lệnh theo refId tối đa {self.config.uncertain_resolve_s:.0f}s")
        return self._set(cid, t.order.with_status(OrderStatus.PENDING))

    def _attach(self, cid: str, order_no: str) -> None:
        t = self._tracks[cid]
        t.order_no, t.uncertain_since = order_no, None
        self._by_no[order_no] = cid
        self._seen.add(order_no)

    def _apply(self, cid: str, row: Mapping) -> None:
        t = self._tracks[cid]
        o = t.order
        vol = _int(row.get("volume"))
        matched = _int(row.get("matchVolume")) or 0
        avg = _float(row.get("matchPriceBQ"))
        status = map_order_status(row.get("orderStatus"), row.get("status"), vol, matched)
        delta = matched - t.filled
        if delta > 0:
            if avg > 0:
                px = (avg * matched - t.avg * t.filled) / delta
            else:
                px = _float(row.get("showPrice")) or float(o.price or 0.0)
                log.warning("tcbs: %s khớp %d nhưng thiếu matchPriceBQ — tạm dùng %.1f", cid, delta, px)
            t.filled, t.avg = matched, avg
            self._emit_fill(Fill(cid, o.symbol, o.side, delta, round(px, 4), self.clock.now(),
                                 fee_vnd=self.config.fee_vnd_per_contract * delta, broker=self.name))
        if o.status.is_terminal:
            return
        st = OrderStatus.FILLED if t.filled >= o.qty else status
        if st is OrderStatus.PENDING and t.filled > 0:
            st = OrderStatus.PARTIALLY_FILLED             # bản tin cũ đến muộn không kéo lùi trạng thái
        reason = o.reject_reason
        if st is OrderStatus.REJECTED and not reason:
            reason = f"TCBS từ chối ({row.get('info') or row.get('status')})"
        self._set(cid, replace(o, status=st, filled_qty=t.filled, broker_order_id=t.order_no, reject_reason=reason))

    # ------------------------------------------------------------ sổ lệnh ----

    async def _book(self, ref: Optional[str] = None) -> list[dict]:
        acc, _ = self._acc()
        out: list[dict] = []
        for page in range(1, 51):
            params = {"accountId": acc, "pageNo": page, "pageSize": self.config.page_size, "symbol": "ALL,ALL",
                      "orderType": "", "status": 0}
            if ref:
                params["refId"] = ref
            r = await self._get(EP_BOOK, params)
            rows = r.body.get("data") if isinstance(r.body, dict) else None
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                raise BrokerError("sổ lệnh TCBS: 'data' không phải danh sách")
            out += [x for x in rows if isinstance(x, dict)]
            if len(rows) < self.config.page_size:
                return out
        raise BrokerError("sổ lệnh TCBS quá 50 trang — không đọc tiếp")

    async def sync(self) -> None:
        now = self.clock.now()
        for cid, t in list(self._tracks.items()):          # lệnh "không rõ": tra đúng theo refId (tài liệu 8.1)
            if t.uncertain_since is None:
                continue
            found = [r for r in await self._book(t.ref) if str(r.get("refId") or "") == t.ref]
            if found:
                self._attach(cid, str(found[0].get("orderNo")))
                await self._say(f"✅ TCBS: đã thấy lệnh {cid} trong sổ lệnh (orderNo {t.order_no})")
            elif (now - t.uncertain_since).total_seconds() >= self.config.uncertain_resolve_s:
                self._reject(cid, f"không thấy refId {t.ref} trong sổ lệnh sau {self.config.uncertain_resolve_s:.0f}s "
                                  "— coi như chưa tới sàn")
        rows = await self._book()
        by_no = {str(r["orderNo"]): r for r in rows if r.get("orderNo") not in (None, "")}
        for no, row in by_no.items():
            cid = self._by_no.get(no)
            if cid is None:
                rid = self._by_ref.get(str(row.get("refId") or ""))
                t = self._tracks.get(rid) if rid else None
                if t is not None and t.order_no is None and not t.order.status.is_terminal:
                    self._attach(rid, no)
                    cid = rid
            if cid is not None:
                self._apply(cid, row)
        await self._adopt(by_no)
        self._seen.update(by_no)
        self._primed = True

    async def _adopt(self, by_no: dict[str, dict]) -> None:
        for no, row in by_no.items():
            if no in self._by_no:
                continue
            is_new = self._primed and no not in self._seen
            vol, matched = _int(row.get("volume")) or 0, _int(row.get("matchVolume")) or 0
            status = map_order_status(row.get("orderStatus"), row.get("status"), vol, matched)
            if status.is_terminal and not (is_new and matched > 0):
                continue
            sym = contracts.symbol_contract(row.get("symbol"))
            side = _SIDE_FROM.get(str(row.get("side") or "").upper())
            if sym is None or side is None or vol <= 0:
                if f"adopt:{no}" not in self._warned:
                    self._warned.add(f"adopt:{no}")
                    log.warning("tcbs: bỏ qua lệnh %s không nhận ra", no)
                continue
            cid = f"{EXTERNAL_PREFIX}{no}"[-MAX_CLIENT_ORDER_ID_LEN:]
            price = _float(row.get("showPrice")) or None
            o = Order(cid, sym, side, vol, OrderType.LIMIT if price else OrderType.MARKET, self.clock.now(),
                      price=price, status=OrderStatus.NEW, broker=self.name, broker_order_id=no, tag="external")
            t = _Track(o, str(row.get("refId") or ""))
            self._tracks[cid] = t
            self._attach(cid, no)
            if not is_new:                                   # có từ trước: phần đã khớp đã nằm trong vị thế
                t.filled, t.avg = matched, _float(row.get("matchPriceBQ"))
                t.order = replace(o, filled_qty=matched)
            self._apply(cid, row)
            await self._say(f"⚠️ TCBS: lệnh ngoài bot orderNo {no} ({side.value} {vol} {sym}) — bot theo dõi khớp "
                            f"lệnh (id {cid})")

    # ------------------------------------------------------------ huỷ / sửa ----

    def _live(self, cid: str) -> _Track:
        t = self._tracks.get(cid)
        if t is None:
            raise BrokerError(f"không biết lệnh {cid}")
        if t.order.status.is_terminal:
            raise BrokerError(f"lệnh {cid} đã kết thúc ({t.order.status.value})")
        return t

    async def cancel(self, client_order_id: str) -> Order:
        cid = client_order_id
        t = self._live(cid)
        if t.local_stop and not t.triggered:
            return self._set(cid, t.order.with_status(OrderStatus.CANCELLED))
        if t.order_no is None:
            raise BrokerError(f"lệnh {cid} chưa biết orderNo — đang đối soát sổ lệnh")
        acc, _ = self._acc()
        body = {"accountId": acc, "orderNo": t.order_no, "cmd": "Web.cancelOrder", "pin": "F", "refId": ""}
        if self.dry_run:
            self._dry(EP_CANCEL, body)
            raise BrokerError("dry-run: không huỷ lệnh thật")
        self._guard()
        try:
            r = await self._post(EP_CANCEL, body)
        except TcbsTransportError as exc:
            raise BrokerError(f"huỷ {cid}: không rõ kết quả ({exc}) — huỷ lại an toàn (-6004)") from exc
        if r.error is not None and r.error.code not in ALREADY_DONE_CODES:
            raise r.error
        try:
            await self.sync()                                 # chỉ CANCELLED khi sổ lệnh báo đã huỷ
        except Exception as exc:
            log.warning("tcbs: đọc sổ lệnh sau huỷ lỗi: %s", exc)
        return self._tracks[cid].order

    async def modify(self, client_order_id: str, *, price: Optional[float] = None,
                     qty: Optional[int] = None) -> Order:
        """Giá và khối lượng sửa bằng HAI request (TCBS cấm sửa đồng thời, -8020); `nvol` là tổng."""
        cid = client_order_id
        t = self._live(cid)
        o = t.order
        if price is not None:
            price = _on_tick(float(price), self.config.tick_size)
        if qty is not None and qty <= o.filled_qty:
            raise BrokerError(f"khối lượng mới {qty} phải > phần đã khớp {o.filled_qty}")
        if t.local_stop and not t.triggered:
            return self._set(cid, replace(o, price=o.price if price is None else price,
                                          qty=o.qty if qty is None else int(qty)))
        if o.order_type is not OrderType.LIMIT:
            raise BrokerError("chỉ sửa được lệnh LO (TCBS không sửa ATO/ATC/MTL; lệnh thị trường của bot là LO trần/sàn)")
        if t.order_no is None:
            raise BrokerError(f"lệnh {cid} chưa biết orderNo — đang đối soát sổ lệnh")
        acc, sub = self._acc()
        steps = ([("price", price)] if price is not None and price != o.price else []) + \
                ([("qty", int(qty))] if qty is not None and qty != o.qty else [])
        for kind, val in steps:
            cur = self._tracks[cid].order
            t.modifies += 1
            body = {"accountId": acc, "subAccountId": sub, "orderNo": t.order_no, "refId": ref_id_for(cid, t.modifies),
                    "nvol": int(cur.qty) if kind == "price" else val,
                    "nprice": float(val) if kind == "price" else float(cur.price)}
            if self.dry_run:
                self._dry(EP_CHANGE, body)
                raise BrokerError("dry-run: không sửa lệnh thật")
            self._guard()
            try:
                r = await self._post(EP_CHANGE, body)
            except TcbsTransportError as exc:
                raise BrokerError(f"sửa {cid}: không rõ kết quả ({exc}) — sổ lệnh sẽ cho biết") from exc
            if r.error is not None:
                raise r.error
            self._set(cid, replace(cur, price=float(val) if kind == "price" else cur.price,
                                   qty=val if kind == "qty" else cur.qty))
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
                t.triggered = True                             # trước khi gửi: không bao giờ gửi đôi
                o = t.order
                mkt = replace(o, order_type=OrderType.MARKET, price=None, qty=o.qty - o.filled_qty)
                log.warning("tcbs: stop %s chạm %.1f ⇒ gửi LO trần/sàn %s %d", cid, trig, o.side.value, mkt.qty)
                if not self.dry_run:
                    try:
                        self._guard()
                    except Exception as exc:
                        self._reject(cid, f"stop chạm nhưng không gửi được: {exc}")
                        await self._say(f"🚨 TCBS: stop {cid} chạm nhưng KHÔNG gửi được ({exc})")
                        continue
                self._by_ref[t.ref] = cid
                await self._send(cid, mkt)

    # ------------------------------------------------------------ stream DE_ORDER ----

    def on_stream_frame(self, frame: Any) -> None:
        """Một frame `message_proto|TOPIC|{json}` của stream TCBS. Không bao giờ ném lỗi."""
        try:
            self._on_stream_frame(frame)
        except Exception:
            log.exception("tcbs: xử lý frame stream lỗi")

    def _on_stream_frame(self, frame: Any) -> None:
        if not isinstance(frame, str) or not frame.startswith("message_proto|"):
            return
        parts = frame.split("|", 2)
        if len(parts) < 3:
            return
        topic, raw = parts[1], parts[2]
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if topic == "DE_OPEN_POSITION":
            sym = contracts.symbol_contract(data.get("symbol"))
            net = _int(data.get("net"))
            if sym is not None and net is not None:
                self.stream_net[sym] = net
            return
        if topic != "DE_ORDER":
            return
        no = str(data.get("orderNo") or "")
        cid = self._by_no.get(no)
        if cid is None:                                  # DE_ORDER không có refId ⇒ lệnh lạ: để sync REST nhận
            self.resync_requested = True
            return
        if not data.get("volume") and not data.get("orderStatus"):
            return                                       # bản tin thiếu thông tin: bỏ, REST sẽ bù
        self._apply(cid, data)

    # ------------------------------------------------------------ vị thế / lệnh chờ ----

    async def positions(self) -> dict[str, Position]:
        acc, sub = self._acc()
        r = await self._get(EP_POSITIONS, {"accountId": acc, "subAccountId": sub})
        data = r.body.get("data") if isinstance(r.body, dict) else None
        rows = data.get("detailData") if isinstance(data, dict) else None
        out: dict[str, Position] = {}
        for p in rows or []:
            if not isinstance(p, dict):
                continue
            net = _int(p.get("net")) or 0
            if net == 0:
                continue
            side = str(p.get("side") or "").upper()
            if side and side != ("L" if net > 0 else "S"):
                raise BrokerError(f"vị thế TCBS {p.get('symbol')}: net {net} nhưng side {side!r} — không đoán")
            sym = contracts.symbol_contract(p.get("symbol"))
            if sym is None:
                log.warning("tcbs: bỏ qua vị thế mã %r (không phải VN30F)", p.get("symbol"))
                continue
            avg = _float(p.get("avg_remain")) or _float(p.get("wapb" if net > 0 else "wasp"))
            out[sym] = Position(sym, net, avg, broker=self.name)
        return out

    async def open_orders(self) -> list[Order]:
        await self.sync()
        return [t.order for t in self._tracks.values() if not t.order.status.is_terminal]


# ------------------------------------------------------------------ stream ----

class _WsTransport:
    def __init__(self, ws):
        self._ws = ws

    async def send(self, data: str) -> None:
        await self._ws.send(data)

    async def recv(self):
        return await self._ws.recv()

    async def close(self) -> None:
        await self._ws.close()


async def tcbs_ws_connector(url: str):
    """TCBS chỉ nhận heartbeat TEXT `ping|1` ⇒ BẮT BUỘC tắt ping RFC 6455 của thư viện (tài liệu 7.3)."""
    import websockets
    ws = await websockets.connect(url, open_timeout=10, ping_interval=None, ping_timeout=None, max_size=2 ** 20)
    return _WsTransport(ws)


def _b64json(d: dict) -> str:
    return base64.b64encode(json.dumps(d, separators=(",", ":")).encode()).decode()


class TcbsStreamAuthError(BrokerError):
    """Server từ chối xác thực stream (token hết hạn/sai)."""


class TcbsOrderStream:
    """Stream thay đổi lệnh phái sinh TCBS (DE_ORDER + DE_OPEN_POSITION) → `TcbsBroker.on_stream_frame`.

    Bản tin không có số thứ tự và không phát lại ⇒ mỗi lần (re)connect: `broker.resync_requested = True`
    (vòng `ping()` kế tiếp đọc lại sổ lệnh REST). Backoff min(2^n, 30)s; `max_failures_alert` lần thất bại
    liên tiếp ⇒ cảnh báo một lần (REST vẫn chạy).
    """

    TOPICS = ("DE_ORDER", "DE_OPEN_POSITION")

    def __init__(self, tokens: TcbsTokenManager, broker: TcbsBroker, *, connector=None, url: str = WS_URL,
                 heartbeat_s: float = 2.0, auth_timeout_s: float = 10.0, recv_timeout_s: float = 45.0,
                 backoff_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, max_failures_alert: int = 10):
        self.tokens, self.broker = tokens, broker
        self._connector = connector or tcbs_ws_connector
        self.url, self.heartbeat_s = url, heartbeat_s
        self.auth_timeout_s, self.recv_timeout_s = auth_timeout_s, recv_timeout_s
        self._sleep, self.max_failures_alert = backoff_sleep, max_failures_alert
        self.stopping = False
        self.connects = 0
        self.failures = 0
        self._alerted = False
        self._authed = False

    async def stop(self) -> None:
        self.stopping = True

    async def run(self) -> None:
        attempt = 0
        while not self.stopping:
            self._authed = False
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except TcbsStreamAuthError as exc:
                self.tokens.invalidate()                     # token bị từ chối ⇒ cần iOTP mới
                log.warning("tcbs stream: %s", exc)
            except Exception as exc:
                log.warning("tcbs stream: mất kết nối (%s: %s)", type(exc).__name__, exc)
            if self._authed:                                   # phiên đã chạy được (dù sau đó rớt) ⇒ không tính thất bại
                attempt, self.failures, self._alerted = 0, 0, False
            else:
                self.failures += 1
                if self.failures >= self.max_failures_alert and not self._alerted:
                    self._alerted = True
                    await self.broker._say(f"🚨 Stream lệnh TCBS thất bại {self.failures} lần liên tiếp — khớp lệnh "
                                           "vẫn đọc qua sổ lệnh REST; kiểm tra token / mạng")
            if self.stopping:
                break
            await self._sleep(min(2 ** attempt, 30))
            attempt += 1

    async def _session(self) -> bool:
        tr = await self._connector(self.url)
        hb = None
        authed = False
        try:
            token = await self.tokens.get()
            await tr.send("authenticate|" + _b64json({"jwt": token}))
            deadline = asyncio.get_running_loop().time() + self.auth_timeout_s
            while not authed:                                # PHẢI đợi auth thành công rồi mới subscribe
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0:
                    raise ConnectionError("hết thời gian chờ xác thực stream")
                msg = await asyncio.wait_for(tr.recv(), left)
                if isinstance(msg, str) and msg.startswith("authenticate|"):
                    try:
                        res = json.loads(msg.split("|", 1)[1])
                    except ValueError:
                        res = {}
                    if not isinstance(res, dict) or not res.get("success"):
                        raise TcbsStreamAuthError(f"xác thực stream bị từ chối: {str(res.get('error') if isinstance(res, dict) else res)[:100]}")
                    authed = self._authed = True
            for topic in self.TOPICS:
                await tr.send("subscribe|" + _b64json({"topic": topic}))
            self.connects += 1
            self.broker.resync_requested = True                # bù bản tin lỡ trong lúc mất kết nối
            hb = asyncio.create_task(self._heartbeat(tr))
            while not self.stopping:
                msg = await asyncio.wait_for(tr.recv(), self.recv_timeout_s)
                if isinstance(msg, str) and msg.startswith("authenticate|") and '"success":false' in msg.replace(" ", ""):
                    raise TcbsStreamAuthError("server huỷ xác thực stream")
                self.broker.on_stream_frame(msg)
        finally:
            if hb is not None:
                hb.cancel()
            try:
                await tr.close()
            except Exception:
                pass
        return authed

    async def _heartbeat(self, tr) -> None:
        while True:
            try:
                await tr.send("ping|1")
            except Exception:
                return
            await asyncio.sleep(self.heartbeat_s)
