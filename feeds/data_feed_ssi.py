"""Feed realtime SSI FastConnect V3 — WebSocket JSON (feed DỰ PHÒNG / đối chiếu).

Giao thức (docs/broker_notes.md §2.4, đối chiếu mã nguồn SDK chính thức `ssi-sdk` 3.3.2):
- URL `wss://stream.ssi.com.vn/ws/v3`; xác thực bằng header `Authorization: Bearer <access
  token>` ngay lúc bắt tay. Token lấy qua REST `/api/v3/auth/token` — việc đó (kèm luật refresh
  token CHỈ DÙNG MỘT LẦN) thuộc adapter SSI (P4.2); feed nhận `token_provider(force_refresh)`.
- Đăng ký: `{"method":"subscribe","channel":"DATA","topics":["trade.<mã>","quote.<mã>"]}`;
  server trả ack `{"method","channel","status","message"}` — `status:"error"` ⇒ lỗi cấu hình,
  không thử lại (`FeedSubscribeError`).
- Ping: `{"method":"ping_pong","channel":"HEARTBEAT","topics":[]}` định kỳ.
- Dữ liệu: `{"channel":"DATA","topic":"trade.<mã>","data":{t,s,p,q,si,v}}` (q = KL lần khớp,
  v = KL luỹ kế) và `{"topic":"quote.<mã>","data":{t,s,bids:[[giá,KL],…],asks:[…]}}` — nhiều
  bước giá, tốt nhất đứng đầu; Tick lấy mức tốt nhất.
- Server từ chối kết nối: `{"code","msg"}` rồi đóng; 401/403 ⇒ lấy token mới MỘT lần, lần nữa
  ⇒ `FeedAuthError`; 429 ⇒ chờ max(30s, retryAfter).
- Backoff: lũy thừa, chặn 60s, jitter [50%, 100%] (như SDK); có dữ liệu ⇒ về lại đầu.

❓ còn phải kiểm: định dạng `t` (parser nhận epoch s/ms/µs/ns, "HH:MM:SS[.fff]", ISO),
đơn vị giá phái sinh (giá ngoài [300, 5000] điểm bị bỏ, không tự chia), số bước giá của quote,
và có cần OTP để lấy token chỉ-dữ-liệu hay không.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections import Counter
from datetime import datetime, time
from typing import Awaitable, Callable, Iterable, Optional

from core import contracts
from core.clock import MarketClock, SessionState
from core.models import Tick
from feeds.base import DataFeed
from feeds.data_feed_dnse import (PRICE_MAX, PRICE_MIN, ContractNotListed, FeedAuthError,  # noqa: F401
                                  epoch_to_vn, resolve_contract)

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

WS_URL = "wss://stream.ssi.com.vn/ws/v3"
RATE_LIMIT_MIN_WAIT_S = 30.0
_HHMMSS = re.compile(r"^\d{1,2}:\d{2}:\d{2}(\.\d+)?$")


class FeedSubscribeError(RuntimeError):
    """Server từ chối đăng ký topic (mã sai / không có quyền) — không tự thử lại."""


class HandshakeRejected(Exception):
    """Bắt tay WebSocket bị từ chối với mã HTTP (401/403/429…)."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class _Rejected(Exception):
    def __init__(self, code: int, retry_after: Optional[float]):
        super().__init__(f"server từ chối kết nối (code {code})")
        self.code, self.retry_after = code, retry_after


def _num(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _int(value) -> Optional[int]:
    n = _num(value)
    return None if n is None else int(n)


class SsiNormalizer:
    def __init__(self, krx_symbols: Iterable[str]):
        self.krx = {s.upper() for s in krx_symbols}
        self._quote: dict[str, tuple] = {}
        self.counts: Counter = Counter()

    def _ts(self, raw, received: datetime) -> datetime:
        if raw is None or raw == "":
            return received
        try:
            if isinstance(raw, (int, float)) or re.fullmatch(r"\d{9,19}", str(raw)):
                return epoch_to_vn(float(raw))
            s = str(raw).strip()
            if _HHMMSS.match(s):
                return datetime.combine(received.date(), time.fromisoformat(s.zfill(8) if len(s) < 8 else s))
            return datetime.fromisoformat(s).replace(tzinfo=None)
        except ValueError:
            self.counts["bad_timestamp"] += 1
            return received

    def normalize(self, frame: dict, received: datetime) -> Optional[Tick]:
        topic = str(frame.get("topic") or "").lower()
        data = frame.get("data")
        if str(frame.get("channel") or "").upper() != "DATA" or not isinstance(data, dict):
            self.counts["ignored"] += 1
            return None
        kind, _, rest = topic.partition(".")
        code, _, interval = rest.partition("@")
        raw_sym = str(data.get("s") or code).upper()
        if kind not in ("trade", "quote") or interval not in ("", "tick") or raw_sym not in self.krx:
            self.counts["ignored"] += 1
            return None
        symbol = contracts.symbol_contract(raw_sym)
        ts = self._ts(data.get("t"), received)

        if kind == "quote":
            def best(levels):
                if isinstance(levels, list) and levels and isinstance(levels[0], (list, tuple)) \
                        and len(levels[0]) >= 2:
                    return _num(levels[0][0]), _int(levels[0][1])
                return None, None
            bid, bq = best(data.get("bids"))
            ask, aq = best(data.get("asks"))
            if bid is None and ask is None:
                self.counts["ignored"] += 1
                return None
            if any(p is not None and not PRICE_MIN <= p <= PRICE_MAX for p in (bid, ask)):
                self.counts["rejected_price"] += 1
                return None
            self._quote[symbol] = (bid, ask, bq, aq)
            self.counts["quotes"] += 1
            return Tick(symbol, ts, bid=bid, ask=ask, bid_qty=bq, ask_qty=aq, source="ssi")

        price = _num(data.get("p"))
        if price is None or not PRICE_MIN <= price <= PRICE_MAX:
            self.counts["rejected_price"] += 1           # gồm giá chữ ("ATO") và sai đơn vị
            return None
        bid, ask, bq, aq = self._quote.get(symbol, (None, None, None, None))
        self.counts["trades"] += 1
        return Tick(symbol, ts, last=price, last_qty=_int(data.get("q")), bid=bid, ask=ask,
                    bid_qty=bq, ask_qty=aq, total_volume=_int(data.get("v")), source="ssi")


# ------------------------------------------------------------- truyền tải ----

class _WsTransport:
    def __init__(self, ws):
        self._ws = ws

    async def send(self, data: str) -> None:
        await self._ws.send(data)

    async def recv(self):
        return await self._ws.recv()

    async def close(self) -> None:
        await self._ws.close()


async def websockets_connector(url: str, headers: dict):
    import websockets

    try:
        ws = await websockets.connect(url, additional_headers=headers, open_timeout=10,
                                      ping_interval=None, max_size=2 ** 20)
    except Exception as exc:                       # chuẩn hoá lỗi bắt tay có mã HTTP
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) or getattr(exc, "status_code", None)
        if status:
            raise HandshakeRejected(int(status)) from exc
        raise
    return _WsTransport(ws)


# -------------------------------------------------------------------- feed ----

class SsiFeed(DataFeed):
    name = "ssi"

    def __init__(self, token_provider: Callable[[bool], Awaitable[str]],
                 contract_lister: Callable[[], Awaitable[Iterable[str]]],
                 market_clock: MarketClock, *, connector=None, url: str = WS_URL,
                 heartbeat_s: float = 30.0, recv_timeout_s: float = 65.0,
                 backoff_base_s: float = 1.0, backoff_max_s: float = 60.0,
                 stale_seconds: float = 5.0,
                 backoff_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 jitter: Callable[[], float] = random.random):
        super().__init__()
        self._tokens = token_provider
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
        self._jitter = jitter
        self.symbol: Optional[str] = None
        self.krx_symbol: Optional[str] = None
        self._normalizer: Optional[SsiNormalizer] = None
        self._stopping = False
        self._conn_task: Optional[asyncio.Task] = None
        self._counts: Counter = Counter()

    @classmethod
    def from_settings(cls, settings, token_provider, contract_lister, market_clock, **kw) -> "SsiFeed":
        return cls(token_provider, contract_lister, market_clock,
                   stale_seconds=float(settings.require("feeds.feed_stale_seconds")), **kw)

    def __repr__(self) -> str:
        return f"SsiFeed(symbol={self.symbol!r}, krx={self.krx_symbol!r}, url={self.url!r})"

    def _backoff(self, attempt: int) -> float:
        base = min(self.backoff_base_s * (2 ** attempt), self.backoff_max_s)
        return base * (0.5 + 0.5 * self._jitter())

    # ------------------------------------------------------------------ vòng đời ----

    async def run(self) -> None:
        self._stopping = False
        listed = await self._lister()
        self.symbol, self.krx_symbol = resolve_contract(
            self.clock.now().date(), self.clock.calendar, self.clock.roll_days_before_expiry, listed)
        self._normalizer = SsiNormalizer([self.krx_symbol])
        log.info("ssi: theo dõi %s (KRX %s)", self.symbol, self.krx_symbol)
        attempt, auth_retried, force_refresh = 0, False, False
        while not self._stopping:
            wait: Optional[float] = None
            try:
                token = await self._tokens(force_refresh)
                self._conn_task = asyncio.create_task(self._session(token))
                got_data = await self._conn_task
                if self._stopping:
                    break
                raise ConnectionError("kết nối đóng")
            except asyncio.CancelledError:
                if self._stopping:
                    break
                raise
            except (FeedAuthError, FeedSubscribeError):
                raise
            except (HandshakeRejected, _Rejected) as exc:
                code = exc.status if isinstance(exc, HandshakeRejected) else exc.code
                self._counts["disconnects"] += 1
                if getattr(exc, "_ssi_got_data", False):      # kết nối trước đã chạy tốt
                    attempt, auth_retried = 0, False
                if code in (401, 403):
                    if auth_retried:
                        raise FeedAuthError(f"SSI từ chối token (HTTP {code}) cả sau khi lấy token mới")
                    auth_retried, force_refresh = True, True
                    log.warning("ssi: token bị từ chối (%s) — lấy token mới", code)
                    continue
                force_refresh = False
                if code == 429:
                    retry_after = getattr(exc, "retry_after", None) or 0.0
                    wait = max(RATE_LIMIT_MIN_WAIT_S, retry_after)
            except Exception as exc:
                self._counts["disconnects"] += 1
                force_refresh = False
                if getattr(exc, "_ssi_got_data", False):
                    attempt, auth_retried = 0, False
                log.warning("ssi: mất kết nối (%s: %s)", type(exc).__name__, exc)
            finally:
                self._conn_task = None
            if self._stopping:
                break
            if wait is None:
                wait = self._backoff(attempt)
                attempt += 1
            log.info("ssi: kết nối lại sau %.1fs", wait)
            await self._sleep(wait)

    async def stop(self) -> None:
        self._stopping = True
        task = self._conn_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _session(self, token: str) -> bool:
        got_data = False
        tr = await self._connector(self.url, {"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json",
                                              "Accept": "application/json"})
        hb: Optional[asyncio.Task] = None
        try:
            await tr.send(json.dumps({"method": "subscribe", "channel": "DATA",
                                      "topics": [f"trade.{self.krx_symbol}",
                                                 f"quote.{self.krx_symbol}"]}))
            self._counts["connects"] += 1
            log.info("ssi: đã kết nối, đăng ký trade/quote %s", self.krx_symbol)
            hb = asyncio.create_task(self._heartbeat(tr))
            while not self._stopping:
                if self._handle(await self._recv(tr)):
                    got_data = True
        except (FeedSubscribeError, asyncio.CancelledError):
            raise
        except Exception as exc:                                 # gồm cả _Rejected
            exc._ssi_got_data = got_data                         # type: ignore[attr-defined]
            raise
        finally:
            if hb is not None:
                hb.cancel()
            try:
                await tr.close()
            except Exception:
                pass
        return got_data

    async def _recv(self, tr):
        try:
            return await asyncio.wait_for(tr.recv(), self.recv_timeout_s)
        except asyncio.TimeoutError as exc:
            raise ConnectionError(f"im lặng > {self.recv_timeout_s:g}s") from exc

    async def _heartbeat(self, tr) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                await tr.send(json.dumps({"method": "ping_pong", "channel": "HEARTBEAT", "topics": []}))
            except Exception:
                return

    def _handle(self, raw) -> bool:
        self._counts["frames"] += 1
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            self._counts["bad_frames"] += 1
            return False
        if not isinstance(msg, dict):
            self._counts["bad_frames"] += 1
            return False
        if "channel" not in msg and "code" in msg and ("msg" in msg or "message" in msg):
            try:
                code = int(str(msg.get("code")).strip())
            except ValueError:
                code = 0
            ra = msg.get("retryAfter", msg.get("retry_after"))
            raise _Rejected(code, float(ra) if ra is not None else None)
        if "method" in msg and "status" in msg and "topic" not in msg:
            if str(msg.get("status")).lower() == "error" and msg.get("method") == "subscribe":
                raise FeedSubscribeError(f"SSI từ chối đăng ký: {msg.get('message', '')}")
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
                log.exception("ssi: lỗi ở bộ nhận tick")
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
                "bad_timestamp": n["bad_timestamp"], "last_message_at": self._last_message_at}
