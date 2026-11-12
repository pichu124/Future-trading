"""Paper broker — dùng CHUNG cho backtest và paper trading (CLAUDE.md mục 2, 6).

Quy tắc khớp (thận trọng, không tự lừa mình):
- Lệnh chỉ khớp ở tick có `ts > lúc đặt` VÀ `ts ≥ lúc đặt + độ trễ` ⇒ không bao giờ khớp ở
  tick sinh tín hiệu. Độ trễ = `latency_ms` + ngẫu nhiên [0, `latency_jitter_ms`] (có seed).
- MTL: mua khớp ask + `slippage_ticks`, bán khớp bid − `slippage_ticks`. Khối lượng khớp bị
  giới hạn bởi `ask_qty`/`bid_qty` nếu feed có (chia sẻ giữa các lệnh theo thứ tự đặt). Phần
  chưa khớp thành LO tại giá khớp ± 1 bước giá (quy tắc MTL sau KRX, broker_notes §0).
- LO đặt khi KHÔNG khớp được với báo giá đã biết ⇒ nằm chờ ngay từ lúc đặt. LO khớp được ngay ở
  tick đầu tiên đủ điều kiện ⇒ khớp min(giá LO, ask + trượt) (mua) /
  max(giá LO, bid − trượt) (bán). Không khớp ngay ⇒ nằm chờ; lệnh chờ chỉ khớp khi giá
  XUYÊN qua giá LO (ask < LO khi mua, bid > LO khi bán) và khớp đúng giá LO — chạm giá chưa
  chắc tới lượt trong hàng chờ.
- STOP (mô phỏng, xem core.models.OrderType.STOP): bán kích hoạt khi bid ≤ giá stop, mua khi
  ask ≥ giá stop (thiếu bid/ask thì dùng last). Kích hoạt ở tick T ⇒ thành MTL, khớp từ tick
  kế tiếp sau độ trễ — giá nhảy cóc thì khớp ở giá nhảy cóc.
- Tick thiếu bid (lệnh bán) hoặc ask (lệnh mua) bị bỏ qua cho lệnh đó.
- Có `session` (MarketClock): chỉ khớp trong phiên liên tục (không mô phỏng ATO/ATC); từ chối
  đặt lệnh khi đóng cửa, MTL ngoài phiên liên tục; không huỷ/sửa trong ATO/ATC.
- Phí mỗi lần khớp = số HĐ × `fee_vnd_per_contract` (tổng phí một chiều trong settings.fees).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Optional

from brokers.base import BrokerAdapter, BrokerError
from core.clock import Clock, MarketClock, SessionState
from core.models import Fill, Order, OrderStatus, OrderType, Position, Side, Tick
from core.settings import Settings

FEE_KEYS = ("broker_fee_vnd_per_contract", "exchange_fee_vnd_per_contract",
            "clearing_fee_vnd_per_contract", "tax_vnd_per_contract")
_EPS = 1e-9


@dataclass(frozen=True)
class PaperConfig:
    tick_size: float
    slippage_ticks: int
    fee_vnd_per_contract: float          # tổng phí + thuế MỘT chiều cho mỗi hợp đồng
    latency_ms: float
    latency_jitter_ms: float = 0.0
    seed: int = 0

    def __post_init__(self) -> None:
        if not self.tick_size > 0:
            raise ValueError("tick_size phải > 0")
        if isinstance(self.slippage_ticks, bool) or int(self.slippage_ticks) != self.slippage_ticks \
                or self.slippage_ticks < 0:
            raise ValueError("slippage_ticks phải là số nguyên ≥ 0")
        for name in ("fee_vnd_per_contract", "latency_ms", "latency_jitter_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} phải ≥ 0")

    @classmethod
    def from_settings(cls, s: Settings) -> "PaperConfig":
        """Phí còn null ⇒ MissingSetting: backtest bắt buộc dùng phí thật (CLAUDE.md mục 4)."""
        fees = sum(float(s.require(f"fees.{k}")) for k in FEE_KEYS)
        return cls(
            tick_size=float(s.require("contract.tick_size_points")),
            slippage_ticks=int(s.require("backtest.slippage_ticks")),
            fee_vnd_per_contract=fees,
            latency_ms=float(s.require("paper.latency_ms")),
            latency_jitter_ms=float(s.get("paper.latency_jitter_ms", 0)),
            seed=int(s.get("paper.seed", 0)),
        )


@dataclass
class _Working:
    placed_at: datetime
    active_at: datetime
    resting: bool = False        # LO đã nằm chờ trên sổ (chỉ khớp khi giá xuyên qua)
    triggered: bool = False      # STOP đã kích hoạt ⇒ xử lý như MTL


class PaperBroker(BrokerAdapter):
    def __init__(self, config: PaperConfig, clock: Clock, name: str = "paper",
                 session: Optional[MarketClock] = None):
        super().__init__(name)
        self.config = config
        self.clock = clock
        self.session = session
        self._rng = random.Random(config.seed)
        self._orders: dict[str, Order] = {}        # mọi lệnh đã nhận (kể cả đã kết thúc)
        self._book: dict[str, _Working] = {}       # lệnh còn sống, theo thứ tự đặt
        self._positions: dict[str, Position] = {}
        self._last_quote: dict[str, tuple] = {}     # symbol → (bid, ask) gần nhất đã thấy
        self._seq = 0

    @classmethod
    def from_settings(cls, settings: Settings, clock: Clock, name: str = "paper",
                      session: Optional[MarketClock] = None) -> "PaperBroker":
        return cls(PaperConfig.from_settings(settings), clock, name, session)

    # ------------------------------------------------------------------ API ----

    async def place(self, order: Order) -> Order:
        existing = self._orders.get(order.client_order_id)
        if existing is not None:
            return existing                                   # idempotent
        now = self.clock.now()
        o = replace(order, broker=self.name)
        reason = self._validate_new(o, now)
        if reason:
            o = o.with_status(OrderStatus.REJECTED, reject_reason=reason)
            self._orders[o.client_order_id] = o
            self._emit_order(o)
            return o
        o = o.with_status(OrderStatus.PENDING, broker_order_id=self._next_broker_id())
        self._orders[o.client_order_id] = o
        self._book[o.client_order_id] = _Working(placed_at=now, active_at=now + self._delay(),
                                                 resting=self._rests_on_arrival(o))
        self._emit_order(o)
        return o

    async def cancel(self, client_order_id: str) -> Order:
        o = self._live_order(client_order_id, "huỷ")
        o = o.with_status(OrderStatus.CANCELLED)
        self._orders[client_order_id] = o
        del self._book[client_order_id]
        self._emit_order(o)
        return o

    async def modify(self, client_order_id: str, *, price: Optional[float] = None,
                     qty: Optional[int] = None) -> Order:
        o = self._live_order(client_order_id, "sửa")
        if o.order_type is OrderType.MARKET:
            raise BrokerError("Không sửa được lệnh MTL")
        if price is not None and not self._on_tick(price):
            raise BrokerError(f"Giá {price} không đúng bước giá {self.config.tick_size}")
        if qty is not None and qty <= o.filled_qty:
            raise BrokerError(f"Khối lượng mới {qty} phải > đã khớp {o.filled_qty} (muốn dừng thì huỷ)")
        now = self.clock.now()
        o = replace(o, price=o.price if price is None else price, qty=o.qty if qty is None else qty,
                    broker_order_id=self._next_broker_id())   # như DNSE: huỷ-đặt ⇒ id mới
        self._orders[client_order_id] = o
        self._book[client_order_id] = _Working(placed_at=now, active_at=now + self._delay())
        self._emit_order(o)
        return o

    async def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    async def open_orders(self) -> list[Order]:
        return [self._orders[cid] for cid in self._book]

    # ------------------------------------------------------------- tiện ích ----

    def get_order(self, client_order_id: str) -> Order:
        return self._orders[client_order_id]

    def activation_time(self, client_order_id: str) -> datetime:
        return self._book[client_order_id].active_at

    # ---------------------------------------------------------------- khớp lệnh ----

    def on_tick(self, tick: Tick) -> None:
        """Gọi cho MỖI tick theo thứ tự thời gian (backtest: engine; paper: feed)."""
        prev_bid, prev_ask = self._last_quote.get(tick.symbol, (None, None))
        self._last_quote[tick.symbol] = (tick.bid if tick.bid is not None else prev_bid,
                                         tick.ask if tick.ask is not None else prev_ask)
        if self.session is not None and \
                self.session.session_state(tick.ts) is not SessionState.CONTINUOUS:
            return
        avail = {Side.BUY: math.inf if tick.ask_qty is None else tick.ask_qty,
                 Side.SELL: math.inf if tick.bid_qty is None else tick.bid_qty}
        for cid in list(self._book):
            w = self._book.get(cid)
            if w is None:
                continue
            o = self._orders[cid]
            if o.symbol != tick.symbol or tick.ts <= w.placed_at or tick.ts < w.active_at:
                continue
            if o.order_type is OrderType.STOP and not w.triggered:
                self._maybe_trigger(o, w, tick)
                continue
            quote = tick.ask if o.side is Side.BUY else tick.bid
            if quote is None or avail[o.side] <= 0:
                continue
            price = self._match_price(o, w, quote)
            if price is None:
                continue
            qty = int(min(o.qty - o.filled_qty, avail[o.side]))
            avail[o.side] -= qty
            self._fill(o, w, qty, price, tick.ts)

    def _rests_on_arrival(self, o: Order) -> bool:
        """LO không khớp được với báo giá ĐÃ BIẾT lúc đặt ⇒ nằm chờ ngay (chỉ khớp khi xuyên giá)."""
        if o.order_type is not OrderType.LIMIT:
            return False
        bid, ask = self._last_quote.get(o.symbol, (None, None))
        if o.side is Side.BUY:
            return ask is not None and ask > o.price + _EPS
        return bid is not None and bid < o.price - _EPS

    def _maybe_trigger(self, o: Order, w: _Working, tick: Tick) -> None:
        px = tick.bid if o.side is Side.SELL else tick.ask
        if px is None:
            px = tick.last
        if px is None:
            return
        hit = px <= o.price + _EPS if o.side is Side.SELL else px >= o.price - _EPS
        if hit:
            w.triggered = True
            w.placed_at = tick.ts                      # thành MTL: khớp từ tick kế tiếp
            w.active_at = tick.ts + self._delay()

    def _match_price(self, o: Order, w: _Working, quote: float) -> Optional[float]:
        slip = self.config.slippage_ticks * self.config.tick_size * o.side.sign
        if o.order_type is OrderType.MARKET or o.order_type is OrderType.STOP:
            return self._round(quote + slip)
        limit = o.price
        if not w.resting:
            if o.side is Side.BUY and quote <= limit + _EPS:
                return min(limit, self._round(quote + slip))
            if o.side is Side.SELL and quote >= limit - _EPS:
                return max(limit, self._round(quote + slip))
            w.resting = True
            return None
        if o.side is Side.BUY and quote < limit - _EPS:
            return limit
        if o.side is Side.SELL and quote > limit + _EPS:
            return limit
        return None

    def _fill(self, o: Order, w: _Working, qty: int, price: float, ts: datetime) -> None:
        filled = o.filled_qty + qty
        cid = o.client_order_id
        if filled >= o.qty:
            new = o.with_status(OrderStatus.FILLED, filled_qty=filled)
            del self._book[cid]
        else:
            new = o.with_status(OrderStatus.PARTIALLY_FILLED, filled_qty=filled)
            if o.order_type in (OrderType.MARKET, OrderType.STOP):
                # MTL sau KRX: phần dư thành LO tại giá khớp ± 1 bước giá
                lo = self._round(price + self.config.tick_size * o.side.sign)
                new = replace(new, order_type=OrderType.LIMIT, price=lo)
                w.resting, w.triggered = True, False
        self._orders[cid] = new
        fill = Fill(cid, o.symbol, o.side, qty, price, ts,
                    fee_vnd=qty * self.config.fee_vnd_per_contract, broker=self.name)
        pos = self._positions.get(o.symbol, Position(o.symbol, broker=self.name))
        self._positions[o.symbol] = pos.apply(fill)
        self._emit_fill(fill)
        self._emit_order(new)

    # ------------------------------------------------------------------ nội bộ ----

    def _validate_new(self, o: Order, now: datetime) -> str:
        if o.order_type in (OrderType.ATO, OrderType.ATC):
            return f"Paper broker không mô phỏng phiên khớp định kỳ ({o.order_type.value})"
        if o.order_type in (OrderType.LIMIT, OrderType.STOP) and not self._on_tick(o.price):
            return f"Giá {o.price} không đúng bước giá {self.config.tick_size}"
        if self.session is not None:
            st = self.session.session_state(now)
            if st is SessionState.CLOSED:
                return "Thị trường đóng cửa"
            if o.order_type is OrderType.MARKET and st is not SessionState.CONTINUOUS:
                return f"MTL chỉ đặt trong phiên liên tục (đang {st.value})"
        return ""

    def _live_order(self, cid: str, action: str) -> Order:
        o = self._orders.get(cid)
        if o is None:
            raise BrokerError(f"Không có lệnh {cid}")
        if cid not in self._book:
            raise BrokerError(f"Lệnh {cid} đã kết thúc ({o.status.value}) — không {action} được")
        if self.session is not None:
            st = self.session.session_state(self.clock.now())
            if st in (SessionState.ATO, SessionState.ATC):
                raise BrokerError(f"Không {action} lệnh trong phiên {st.value}")
        return o

    def _delay(self) -> timedelta:
        ms = self.config.latency_ms
        if self.config.latency_jitter_ms:
            ms += self._rng.uniform(0, self.config.latency_jitter_ms)
        return timedelta(milliseconds=ms)

    def _next_broker_id(self) -> str:
        self._seq += 1
        return f"{self.name.upper()}-{self._seq}"

    def _on_tick(self, price: Optional[float]) -> bool:
        if price is None:
            return False
        n = price / self.config.tick_size
        return abs(n - round(n)) < 1e-6

    def _round(self, price: float) -> float:
        t = self.config.tick_size
        return round(round(price / t) * t, 6)
