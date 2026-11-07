"""Feed realtime DNSE Open API V2 (LightSpeed V2) — WebSocket, JSON.

Giao thức (docs/broker_notes.md §1.4, đối chiếu mã nguồn SDK chính thức `dnse` 0.5.0):
- URL `wss://ws-openapi.dnse.com.vn/v1/stream?encoding=json`; server gửi lời chào trước.
- Xác thực: `{"action":"auth","api_key",…,"signature": hex(HMAC-SHA256(secret,
  f"{api_key}:{timestamp}:{nonce}")),"timestamp": giây,"nonce": micro-giây}`; lỗi ⇒
  `{"action":"auth_error"}` — lỗi CỐ ĐỊNH (sai khoá), không thử lại.
- Đăng ký: `{"name": "tick.G1.json", "symbols": [mã KRX]}` — kênh này mang khớp lệnh
  (`T`=`t`/`te`) và giá tốt nhất (`T`=`q`). KHÔNG có sổ lệnh nhiều bước giá trong SDK.
- Ping/pong: client gửi `{"action":"ping"}` định kỳ; server gửi ping ⇒ trả `{"action":"pong"}`.

Khác SDK (thận trọng hơn):
- Không bỏ cuộc sau 10 lần: thử lại mãi với backoff 1s → `backoff_max_s`; trong lúc mất kết
  nối `is_stale()` = True ⇒ composite/risk ngừng mở vị thế. Có dữ liệu lại ⇒ backoff về 1s.
- Kết nối im lặng quá `recv_timeout_s` (không cả ping) ⇒ coi như chết, kết nối lại.
- Bắt buộc đối chiếu hợp đồng với danh sách broker trả về TRƯỚC khi kết nối (CLAUDE.md mục 4).
- Không đoán đơn vị giá: giá ngoài [300, 5000] điểm bị bỏ và đếm (`rejected_price`).
- Không có số thứ tự message trong giao thức ⇒ không phát hiện gap theo sequence; dùng phát
  hiện im lặng theo thời gian (`is_stale`). ❓ kiểm lại khi có message thật.

❓ còn phải kiểm bằng gọi thử: tên field trên dây (camelCase hay snake_case — parser nhận cả
hai), đơn vị `timestamp` (tự nhận s/ms/µs/ns theo độ lớn), đơn vị giá, ý nghĩa `side`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional, Protocol

from core import contracts
from core.calendar import TradingCalendar
from core.clock import MarketClock, SessionState, to_vn
from core.models import Tick
from feeds.base import DataFeed

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

WS_URL = "wss://ws-openapi.dnse.com.vn/v1/stream?encoding=json"
TICK_CHANNEL = "tick.G1.json"
PRICE_MIN, PRICE_MAX = 300.0, 5000.0           # điểm VN30F hợp lý; ngoài khoảng ⇒ nghi sai đơn vị


class FeedAuthError(RuntimeError):
    """Server từ chối xác thực — sai khoá/chữ ký, không tự thử lại."""


class ContractNotListed(RuntimeError):
    """Hợp đồng bot định theo dõi không có trong danh sách broker trả về."""


# ------------------------------------------------------------------- tiện ích ----

def epoch_to_vn(raw: float) -> datetime:
    """Epoch (giây / ms / µs / ns, nhận theo độ lớn) → datetime naive giờ VN."""
    x = float(raw)
    for scale in (1, 1e3, 1e6, 1e9):
        sec = x / scale
        if 946684800 <= sec <= 4102444800:      # 2000-01-01 .. 2100-01-01
            return to_vn(datetime.fromtimestamp(sec, timezone.utc))
    raise ValueError(f"timestamp không hợp lệ: {raw!r}")


def resolve_contract(d: date, cal: TradingCalendar, roll_days_before_expiry: int,
                     listed_krx: Iterable[str]) -> tuple[str, str]:
    """(tên VN30FYYMM, mã KRX) bot giao dịch ngày d — phải có trong danh sách broker."""
    name = contracts.trading_contract(d, cal, roll_days_before_expiry)
    y, mth = contracts.contract_ym(name)
    krx = contracts.krx_symbol(y, mth)
    listed = {str(s).strip().upper() for s in listed_krx}
    if krx not in listed:
        raise ContractNotListed(
            f"{name} (KRX {krx}) không có trong danh sách hợp đồng broker trả về "
            f"({sorted(listed) or 'rỗng'}) — kiểm lại bảng mã KRX/lịch đáo hạn trước khi chạy")
    return name, krx


def _first(msg: dict, *keys: str) -> Any:
    for k in keys:
        v = msg.get(k)
        if v is not None:
            return v
    return None


class DnseNormalizer:
    """Message DNSE → Tick. Giữ giá tốt nhất gần nhất để gắn vào tick khớp lệnh."""

    def __init__(self, krx_symbols: Iterable[str]):
        self.krx = {s.upper() for s in krx_symbols}
        self._quote: dict[str, tuple] = {}
        self.counts: Counter = Counter()

    def normalize(self, msg: dict, received: datetime) -> Optional[Tick]:
        kind = str(_first(msg, "T", "t") or "")
        raw_sym = str(_first(msg, "symbol", "s") or "").upper()
        if kind not in ("t", "te", "q") or raw_sym not in self.krx:
            self.counts["ignored"] += 1
            return None
        symbol = contracts.symbol_contract(raw_sym)
        ts_raw = msg.get("timestamp")
        try:
            ts = epoch_to_vn(ts_raw) if ts_raw is not None else received
        except ValueError:
            self.counts["bad_timestamp"] += 1
            ts = received

        if kind == "q":
            bid = _first(msg, "bidPrice", "bid_price")
            ask = _first(msg, "askPrice", "ask_price")
            if bid is None and ask is None:
                self.counts["ignored"] += 1
                return None
            if any(p is not None and not PRICE_MIN <= float(p) <= PRICE_MAX for p in (bid, ask)):
                self.counts["rejected_price"] += 1
                return None
            bq = _first(msg, "bidVolume", "bid_volume")
            aq = _first(msg, "askVolume", "ask_volume")
            q = (None if bid is None else float(bid), None if ask is None else float(ask),
                 None if bq is None else int(bq), None if aq is None else int(aq))
            self._quote[symbol] = q
            self.counts["quotes"] += 1
            return Tick(symbol, ts, bid=q[0], ask=q[1], bid_qty=q[2], ask_qty=q[3], source="dnse")

        price = msg.get("price")
        if price is None or not PRICE_MIN <= float(price) <= PRICE_MAX:
            self.counts["rejected_price"] += 1
            return None
        vol = msg.get("volume")
        total = _first(msg, "totalVolume", "total_volume")
        bid, ask, bq, aq = self._quote.get(symbol, (None, None, None, None))
        self.counts["trades"] += 1
        return Tick(symbol, ts, last=float(price), last_qty=None if vol is None else int(vol),
                    bid=bid, ask=ask, bid_qty=bq, ask_qty=aq,
                    total_volume=None if total is None else int(total), source="dnse")


# ------------------------------------------------------------- truyền tải ----

class Transport(Protocol):
    async def send(self, data: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[Transport]]


class _WsTransport:
    def __init__(self, ws):
        self._ws = ws

    async def send(self, data: str) -> None:
        await self._ws.send(data)

    async def recv(self):
        return await self._ws.recv()

    async def close(self) -> None:
        await self._ws.close()


async def websockets_connector(url: str) -> Transport:
    import websockets   # nhập muộn: test dùng transport giả, không cần thư viện

    ws = await websockets.connect(url, open_timeout=10, ping_interval=None, max_size=2 ** 20)
    return _WsTransport(ws)


# -------------------------------------------------------------------- feed ----

class DnseFeed(DataFeed):
    name = "dnse"

    def __init__(self, api_key: str, api_secret: str,
                 contract_lister: Callable[[], Awaitable[Iterable[str]]],
                 market_clock: MarketClock, *, connector: Optional[Connector] = None,
                 url: str = WS_URL, heartbeat_s: float = 30.0, recv_timeout_s: float = 65.0,
                 backoff_base_s: float = 1.0, backoff_max_s: float = 60.0,
                 stale_seconds: float = 5.0,
                 backoff_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 epoch: Callable[[], float] = time.time):
        super().__init__()
        if not api_key or not api_secret:
            raise ValueError("Thiếu DNSE api_key/api_secret (đặt trong .env)")
        self._api_key = api_key
        self._api_secret = api_secret
        self._lister = contract_lister
        self.clock = market_clock
        self._connector = connector or websockets_connector
        self.url = url
        self.heartbeat_s = heartbeat_s
        self.recv_timeout_s = recv_timeout_s
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.stale_seconds = stale_seconds
        self._sleep = backoff_sleep
        self._epoch = epoch
        self.symbol: Optional[str] = None
        self.krx_symbol: Optional[str] = None
        self._normalizer: Optional[DnseNormalizer] = None
        self._stopping = False
        self._conn_task: Optional[asyncio.Task] = None
        self._transport: Optional[Transport] = None
        self._counts: Counter = Counter()
        self._per_minute: "OrderedDict[datetime, int]" = OrderedDict()
        self._cur_minute: Optional[datetime] = None

    @classmethod
    def from_settings(cls, settings, api_key: str, api_secret: str,
                      contract_lister: Callable[[], Awaitable[Iterable[str]]],
                      market_clock: MarketClock, **kw) -> "DnseFeed":
        return cls(api_key, api_secret, contract_lister, market_clock,
                   stale_seconds=float(settings.require("feeds.feed_stale_seconds")), **kw)

    def __repr__(self) -> str:                       # không bao giờ in secret
        return f"DnseFeed(symbol={self.symbol!r}, krx={self.krx_symbol!r}, url={self.url!r})"

    # ------------------------------------------------------------------ vòng đời ----

    async def run(self) -> None:
        self._stopping = False
        listed = await self._lister()
        today = self.clock.now().date()
        self.symbol, self.krx_symbol = resolve_contract(
            today, self.clock.calendar, self.clock.roll_days_before_expiry, listed)
        self._normalizer = DnseNormalizer([self.krx_symbol])
        log.info("dnse: theo dõi %s (KRX %s)", self.symbol, self.krx_symbol)
        attempt = 0
        while not self._stopping:
            got_data = False
            self._conn_task = asyncio.create_task(self._session())
            try:
                got_data = await self._conn_task
            except asyncio.CancelledError:
                if self._stopping:
                    break
                raise
            except FeedAuthError:
                raise
            except Exception as exc:                  # mất kết nối / lỗi giao thức
                got_data = getattr(exc, "_dnse_got_data", False)
                self._counts["disconnects"] += 1
                log.warning("dnse: mất kết nối (%s: %s)", type(exc).__name__, exc)
            else:
                if self._stopping:
                    break
                self._counts["disconnects"] += 1
            finally:
                self._conn_task = None
            if self._stopping:
                break
            if got_data:
                attempt = 0
            delay = min(self.backoff_base_s * (2 ** attempt), self.backoff_max_s)
            attempt += 1
            log.info("dnse: kết nối lại sau %.0fs (lần %d)", delay, attempt)
            await self._sleep(delay)

    async def stop(self) -> None:
        self._stopping = True
        task = self._conn_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _session(self) -> bool:
        """Một kết nối: chào → xác thực → đăng ký → nhận tới khi lỗi. Trả True nếu đã có tick."""
        got_data = False
        tr = await self._connector(self.url)
        self._transport = tr
        hb: Optional[asyncio.Task] = None
        try:
            await self._handle_frame(await self._recv(tr))        # lời chào
            await tr.send(json.dumps(self._auth_message()))
            resp = json.loads(await self._recv(tr))
            self._count_frame()
            if resp.get("action") == "auth_error":
                raise FeedAuthError(f"DNSE từ chối xác thực: {resp.get('message', '')}")
            await tr.send(json.dumps({"name": TICK_CHANNEL, "symbols": [self.krx_symbol]}))
            self._counts["connects"] += 1
            log.info("dnse: đã kết nối và đăng ký %s", TICK_CHANNEL)
            hb = asyncio.create_task(self._heartbeat(tr))
            while not self._stopping:
                frame = await self._recv(tr)
                if await self._handle_frame(frame, tr):
                    got_data = True
        except FeedAuthError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            exc._dnse_got_data = got_data                         # type: ignore[attr-defined]
            raise
        finally:
            if hb is not None:
                hb.cancel()
            self._transport = None
            try:
                await tr.close()
            except Exception:
                pass
        return got_data

    async def _recv(self, tr: Transport):
        try:
            return await asyncio.wait_for(tr.recv(), self.recv_timeout_s)
        except asyncio.TimeoutError as exc:
            raise ConnectionError(f"im lặng > {self.recv_timeout_s:g}s") from exc

    async def _heartbeat(self, tr: Transport) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                await tr.send(json.dumps({"action": "ping"}))
            except Exception:
                return

    def _auth_message(self) -> dict:
        now = self._epoch()
        ts, nonce = int(now), int(now * 1_000_000)
        sig = hmac.new(self._api_secret.encode(), f"{self._api_key}:{ts}:{nonce}".encode(),
                       hashlib.sha256).hexdigest()
        return {"action": "auth", "api_key": self._api_key, "signature": sig,
                "timestamp": ts, "nonce": nonce}

    # ------------------------------------------------------------------ message ----

    def _count_frame(self) -> None:
        now = self.clock.now()
        minute = now.replace(second=0, microsecond=0)
        if self._cur_minute is not None and minute != self._cur_minute:
            n = self._per_minute.get(self._cur_minute, 0)
            log.info("dnse: %d msg/phút lúc %s (tick %d, bỏ %d, giá lạ %d)", n,
                     self._cur_minute.strftime("%H:%M"), self._counts["ticks"],
                     self._normalizer.counts["ignored"] if self._normalizer else 0,
                     self._normalizer.counts["rejected_price"] if self._normalizer else 0)
        self._cur_minute = minute
        self._per_minute[minute] = self._per_minute.get(minute, 0) + 1
        while len(self._per_minute) > 600:
            self._per_minute.popitem(last=False)
        self._counts["frames"] += 1

    def _handle_frame(self, frame, tr: Optional[Transport] = None):
        """Đếm + giải mã một frame; trả awaitable → True nếu phát ra tick."""
        self._count_frame()
        try:
            msg = json.loads(frame)
        except (TypeError, ValueError):
            self._counts["bad_frames"] += 1
            return _done(False)
        if not isinstance(msg, dict):
            self._counts["bad_frames"] += 1
            return _done(False)
        return self._dispatch(msg, tr)

    async def _dispatch(self, msg: dict, tr: Optional[Transport]) -> bool:
        action = msg.get("action")
        if action is not None:
            if action == "ping" and tr is not None:
                await tr.send(json.dumps({"action": "pong"}))
            elif action == "auth_error":
                raise FeedAuthError(f"DNSE từ chối xác thực: {msg.get('message', '')}")
            return False
        tick = self._normalizer.normalize(msg, received=self.clock.now()) if self._normalizer else None
        if tick is None:
            return False
        self._last_message_at = self.clock.now()
        self._counts["ticks"] += 1
        for h in self._handlers:
            try:
                h(tick)
            except Exception:
                self._counts["callback_errors"] += 1
                log.exception("dnse: lỗi ở bộ nhận tick")
        return True

    # ------------------------------------------------------------------ giám sát ----

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        now = now or self.clock.now()
        if self.clock.session_state(now) is not SessionState.CONTINUOUS:
            return False
        if self._last_message_at is None:
            return True
        return (now - self._last_message_at).total_seconds() > self.stale_seconds

    def stats(self) -> dict:
        n = self._normalizer.counts if self._normalizer else Counter()
        return {"symbol": self.symbol, "krx_symbol": self.krx_symbol,
                "connects": self._counts["connects"], "disconnects": self._counts["disconnects"],
                "frames": self._counts["frames"], "ticks": self._counts["ticks"],
                "bad_frames": self._counts["bad_frames"],
                "callback_errors": self._counts["callback_errors"],
                "ignored": n["ignored"], "rejected_price": n["rejected_price"],
                "last_message_at": self._last_message_at,
                "per_minute": dict(self._per_minute)}


async def _done(value):
    return value
