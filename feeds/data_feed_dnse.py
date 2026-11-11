"""Feed realtime DNSE Open API V2 (LightSpeed V2) — WebSocket, JSON.

Giao thức (docs/broker_notes.md §1.4) — theo SDK CHÍNH THỨC `dnse-sdk-openapi` 2.4.1 (`dnse/websocket/*`,
đọc 11/11/2026). SDK cũ `dnse` 0.5.0 KHÔNG còn khớp server: probe thật 11/11/2026 trả
`{"action":"error","code":"AUTH_FAILED","message":"authentication failed: nonce is required"}` khi nonce là số.
- URL `wss://ws-openapi.dnse.com.vn/v1/stream?encoding=json`; server gửi lời chào (`welcome`, `session_id`) trước ✅.
- Xác thực: `{"action":"auth","api_key","signature": hex(HMAC-SHA256(secret, f"{api_key}:{timestamp}:{nonce}")),
  "timestamp": giây (số), "nonce": micro-giây dạng CHUỖI}`. Thành công ⇔ `{"action":"auth_success"}`; `auth_error`
  hoặc `error` (vd `code: AUTH_FAILED` ✅ thật) ⇒ lỗi CỐ ĐỊNH, không thử lại; phản hồi lạ ⇒ cũng dừng (như SDK).
- Đăng ký: mỗi kênh một message `{"action":"subscribe","channels":[{"name": kênh, "symbols":[mã KRX…]}]}`:
  khớp lệnh `tick.<board>.json` (board mặc định SDK: G1 G3 G4 G7 T1 T2 T3 T4 T6), giá tốt nhất
  `top_price.<board>.json`. VN30F ở board G1 ✅ (probe thật 11/11/2026) ⇒ chỉ đăng ký G1.
- Message dữ liệu phân loại theo `T` ✅ (thật): `t` khớp lệnh (`matchPrice`, `matchQtty`, `totalVolumeTraded`,
  `time` = `{"Seconds","Nanos"}` epoch UTC), `q` sổ lệnh 10 bước (`bid`/`offer` = danh sách `{price, qtty}`). Server `{"action":"error"}` giữa phiên ⇒ đếm + log.
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
# VN30F trên board G1 ✅ (probe thật 11/11/2026: mọi message `boardId: "G1"`). SDK mặc định đăng ký 9 + 7 board —
# không cần; server giới hạn `subscriptions_max: 100`, `messages_per_second: 100` (trong `auth_success`).
TICK_BOARDS = ("G1",)
QUOTE_BOARDS = ("G1",)
TIME_TOLERANCE_S = 600          # `time` không kèm múi giờ: chọn cách hiểu (giờ VN / UTC) lệch ≤ 10′ so với lúc nhận
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


def _vn_from_naive(dt: datetime, tz) -> datetime:
    return to_vn(dt.replace(tzinfo=tz))


def message_time(raw: Any, received: datetime) -> Optional[datetime]:
    """`time`/`timestamp` của message → giờ VN naive; None nếu không hiểu hoặc lệch quá xa lúc nhận.

    Nhận: epoch số (s/ms/µs/ns), protobuf `{Seconds, Nanos}`, chuỗi ISO có múi giờ, chuỗi KHÔNG múi giờ (REST trả
    giờ VN — thử giờ VN rồi UTC, lấy cách hiểu gần `received` trong `TIME_TOLERANCE_S`)."""
    from core.clock import VN_TZ
    cands: list[datetime] = []
    try:
        if isinstance(raw, dict):
            sec = float(raw.get("Seconds", raw.get("seconds", 0))) + float(raw.get("Nanos", raw.get("nanos", 0))) / 1e9
            cands = [epoch_to_vn(sec)]
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            cands = [epoch_to_vn(raw)]
        elif isinstance(raw, str) and raw.strip():
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            cands = [to_vn(dt)] if dt.tzinfo is not None else [_vn_from_naive(dt, VN_TZ),
                                                                 _vn_from_naive(dt, timezone.utc)]
    except (ValueError, TypeError, OverflowError):
        return None
    ok = [c for c in cands if abs((c - received).total_seconds()) <= TIME_TOLERANCE_S]
    return min(ok, key=lambda c: abs((c - received).total_seconds())) if ok else None


def _best_level(levels: Any, best_is_max: bool) -> Optional[tuple[float, Optional[int]]]:
    """Mức giá tốt nhất trong danh sách `{price, qtty}` (không dựa vào thứ tự server gửi); bỏ giá ≤ 0."""
    if not isinstance(levels, list):
        return None
    good = []
    for lv in levels:
        if not isinstance(lv, dict) or lv.get("price") is None:
            continue
        px = float(lv["price"])
        if px > 0:
            q = _first(lv, "qtty", "quantity", "qty")
            good.append((px, None if q is None else int(q)))
    if not good:
        return None
    return max(good, key=lambda x: x[0]) if best_is_max else min(good, key=lambda x: x[0])


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
        self.names = {contracts.symbol_contract(k): k for k in self.krx}     # server có thể gửi tên VN30FYYMM
        self._quote: dict[str, tuple] = {}
        self.counts: Counter = Counter()

    def normalize(self, msg: dict, received: datetime) -> Optional[Tick]:
        kind = str(_first(msg, "T", "t") or "")
        raw_sym = str(_first(msg, "symbol", "s") or "").upper()
        if kind not in ("t", "te", "q") or (raw_sym not in self.krx and raw_sym not in self.names):
            self.counts["ignored"] += 1
            return None
        symbol = raw_sym if raw_sym in self.names else contracts.symbol_contract(raw_sym)
        ts_raw = _first(msg, "time", "timestamp")
        ts = message_time(ts_raw, received) if ts_raw is not None else received
        if ts is None:
            self.counts["bad_timestamp"] += 1
            ts = received

        if kind == "q":
            if "bid" in msg or "offer" in msg:                        # SDK 2.4.1: danh sách mức giá
                b, a = _best_level(msg.get("bid"), True), _best_level(msg.get("offer"), False)
                bid, bq = b if b else (None, None)
                ask, aq = a if a else (None, None)
            else:                                                     # dạng cũ (SDK 0.5.0) — giữ để tương thích
                bid, ask = _first(msg, "bidPrice", "bid_price"), _first(msg, "askPrice", "ask_price")
                bq, aq = _first(msg, "bidVolume", "bid_volume"), _first(msg, "askVolume", "ask_volume")
            if bid is None and ask is None:
                self.counts["ignored"] += 1
                return None
            if any(p is not None and not PRICE_MIN <= float(p) <= PRICE_MAX for p in (bid, ask)):
                self.counts["rejected_price"] += 1
                return None
            q = (None if bid is None else float(bid), None if ask is None else float(ask),
                 None if bq is None else int(bq), None if aq is None else int(aq))
            self._quote[symbol] = q
            self.counts["quotes"] += 1
            return Tick(symbol, ts, bid=q[0], ask=q[1], bid_qty=q[2], ask_qty=q[3], source="dnse")

        price = _first(msg, "matchPrice", "price")
        if price is None or not PRICE_MIN <= float(price) <= PRICE_MAX:
            self.counts["rejected_price"] += 1
            return None
        vol = _first(msg, "matchQtty", "volume")
        total = _first(msg, "totalVolumeTraded", "totalVolume", "total_volume")
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
                 epoch: Callable[[], float] = time.time,
                 extra_contracts: Iterable[str] = ()):
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
        # HĐ ghi thêm (recorder ngày trước roll): cùng kết nối; KHÔNG tính vào độ "im lặng" của feed
        self._extra_wanted = [str(c).upper() for c in extra_contracts]
        self.extra_contracts: list[str] = []
        self.extra_krx: list[str] = []
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
        listed_set = {str(x).strip().upper() for x in listed}
        self.extra_contracts, self.extra_krx = [], []
        for name in self._extra_wanted:
            if name == self.symbol or name in self.extra_contracts:
                continue
            krx = contracts.krx_symbol(*contracts.contract_ym(name))
            if krx not in listed_set:
                log.warning("dnse: HĐ ghi thêm %s (KRX %s) không có trong danh sách broker — bỏ, chỉ ghi %s",
                            name, krx, self.symbol)
                continue
            self.extra_contracts.append(name)
            self.extra_krx.append(krx)
        self._normalizer = DnseNormalizer([self.krx_symbol, *self.extra_krx])
        log.info("dnse: theo dõi %s (KRX %s)%s", self.symbol, self.krx_symbol,
                 f" + ghi thêm {', '.join(self.extra_contracts)}" if self.extra_contracts else "")
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
            delay = min(self.backoff_base_s * (2 ** min(attempt, 30)), self.backoff_max_s)   # chặn tràn số
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
            action = resp.get("action") if isinstance(resp, dict) else None
            if action != "auth_success":
                detail = (f"{resp.get('code', '')} {resp.get('message') or resp.get('msg') or ''}".strip()
                          if isinstance(resp, dict) else str(resp))
                raise FeedAuthError(f"DNSE từ chối xác thực (action={action!r}): {detail}")
            for msg in self.subscribe_messages():
                await tr.send(json.dumps(msg))
            self._counts["connects"] += 1
            log.info("dnse: đã xác thực, đăng ký %d kênh (tick × %d board, top_price × %d board) cho %s",
                     len(TICK_BOARDS) + len(QUOTE_BOARDS), len(TICK_BOARDS), len(QUOTE_BOARDS),
                     ", ".join([self.krx_symbol, *self.extra_krx]))
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

    def subscribe_messages(self) -> list[dict]:
        syms = [self.krx_symbol, *self.extra_krx]
        return ([{"action": "subscribe", "channels": [{"name": f"tick.{b}.json", "symbols": syms}]}
                 for b in TICK_BOARDS]
                + [{"action": "subscribe", "channels": [{"name": f"top_price.{b}.json", "symbols": syms}]}
                   for b in QUOTE_BOARDS])

    def _auth_message(self) -> dict:
        now = self._epoch()
        ts, nonce = int(now), str(int(now * 1_000_000))       # nonce CHUỖI (server từ chối số — 11/11/2026)
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
            elif action == "error":
                self._counts["server_errors"] += 1
                log.warning("dnse: server báo lỗi %s %s", msg.get("code", ""), msg.get("message") or msg.get("msg"))
            return False
        tick = self._normalizer.normalize(msg, received=self.clock.now()) if self._normalizer else None
        if tick is None:
            return False
        if tick.symbol == self.symbol:                 # HĐ ghi thêm không làm feed "còn sống"
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
        return {"symbol": self.symbol, "krx_symbol": self.krx_symbol, "extra_contracts": list(self.extra_contracts),
                "connects": self._counts["connects"], "disconnects": self._counts["disconnects"],
                "frames": self._counts["frames"], "ticks": self._counts["ticks"],
                "bad_frames": self._counts["bad_frames"], "server_errors": self._counts["server_errors"],
                "callback_errors": self._counts["callback_errors"],
                "ignored": n["ignored"], "rejected_price": n["rejected_price"],
                "last_message_at": self._last_message_at,
                "per_minute": dict(self._per_minute)}


async def _done(value):
    return value
