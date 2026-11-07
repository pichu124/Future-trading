"""Thực thi: Signal → Order qua risk manager → broker; quản lý stop/chốt lời; xử lý RiskAction.

Dùng CHUNG cho backtest và live (chỉ đổi BrokerAdapter) để hành vi đặt lệnh không lệch nhau.

Luồng:
- `on_signal(sig)`: ENTER_* ⇒ lệnh MTL `tag="entry"` kèm `stop_loss`; EXIT ⇒ huỷ lệnh chờ của
  hợp đồng rồi MTL đóng `tag="exit"`; ENTER ngược chiều vị thế ⇒ chỉ đóng.
- Mỗi lần khớp: `RiskManager.record_fill` NGAY trong callback (sổ sách luôn đúng), còn việc
  đặt/sửa lệnh bảo vệ được xếp hàng và chạy ở `drain()` (callback broker là đồng bộ).
- Bảo vệ: sau mỗi lần khớp, đảm bảo đúng một STOP (`tag="stop"`) và, nếu tín hiệu có, một LO
  chốt lời (`tag="take_profit"`) ngược chiều với khối lượng = |net|. Net về 0 ⇒ huỷ cả hai.
  Khối lượng lệch ⇒ huỷ rồi đặt lại (khoảng hở ngắn được `stop_grace_seconds` của risk che).
- `handle_actions`: FLATTEN_* / KILL ⇒ huỷ lệnh chờ rồi MTL đóng `tag="flatten"`; KILL còn
  dừng nhận tín hiệu. EMERGENCY_HEDGE: hiện chỉ đóng trên chính broker (P4.3 sẽ đối ứng qua
  broker dự phòng).
- Mọi lệnh đi qua `risk.approve` — không có đường tắt.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

from brokers.base import BrokerAdapter, BrokerError
from core.clock import Clock
from core.models import (AccountState, Fill, Order, OrderStatus, OrderType, Side, Signal,
                         SignalAction)
from risk.risk_manager import ActionKind, OrderIdFactory, RiskAction, RiskManager, Rejected

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())   # thư viện: runner/CLI tự cấu hình logging

CLOSE_TAGS = ("exit", "flatten")
PROTECT_TAGS = ("stop", "take_profit")


@dataclass
class ExecEvent:
    ts: datetime
    kind: str            # "rejected" | "broker_rejected" | "cancel_failed" | "action" | "info"
    message: str
    symbol: str = ""


@dataclass
class _Targets:
    stop: Optional[float]
    take_profit: Optional[float]


class ExecutionManager:
    def __init__(self, broker: BrokerAdapter, risk: RiskManager, state: AccountState,
                 clock: Clock, tick_size: float, ids: Optional[OrderIdFactory] = None):
        self.broker = broker
        self.risk = risk
        self.state = state
        self.clock = clock
        self.tick_size = tick_size
        self.ids = ids or OrderIdFactory("B")
        self.orders: dict[str, Order] = {}           # mọi lệnh đã gửi, trạng thái mới nhất
        self.rejections: list[Rejected] = []
        self.events: list[ExecEvent] = []
        self.halted = False
        self._targets: dict[str, _Targets] = {}
        self._queue: deque[Callable[[], Awaitable[None]]] = deque()
        broker.on_fill(self._on_fill)
        broker.on_order_update(self._on_update)

    # ------------------------------------------------------------ callback broker ----

    def _on_update(self, order: Order) -> None:
        self.orders[order.client_order_id] = order
        self.risk.on_order_update(self.state, order)
        if order.status is OrderStatus.REJECTED:
            self._event("broker_rejected", f"{order.client_order_id} ({order.tag}): "
                                           f"{order.reject_reason}", order.symbol)

    def _on_fill(self, fill: Fill) -> None:
        self.risk.record_fill(self.state, fill)
        self._queue.append(lambda s=fill.symbol: self._ensure_protection(s))

    async def drain(self) -> None:
        """Chạy hết việc đã xếp hàng (kể cả việc mới phát sinh trong lúc chạy)."""
        while self._queue:
            await self._queue.popleft()()

    # ------------------------------------------------------------------ tín hiệu ----

    async def on_signal(self, sig: Signal) -> None:
        if sig.action is SignalAction.HOLD:
            return
        if self.halted:
            self._event("info", f"Bỏ qua tín hiệu {sig.action.value}: đã dừng (kill)", sig.symbol)
            return
        if sig.action is SignalAction.EXIT:
            await self._close(sig.symbol, "exit")
            return
        side = Side.BUY if sig.action is SignalAction.ENTER_LONG else Side.SELL
        net = self.state.net_qty(sig.symbol)
        if net != 0 and (net > 0) != (side is Side.BUY):
            await self._close(sig.symbol, "exit")      # ngược chiều ⇒ chỉ đóng
            return
        order = self._new_order(sig.symbol, side, sig.qty, OrderType.MARKET, "entry",
                                stop_loss=self._round(sig.stop_loss))
        previous = self._targets.get(sig.symbol)
        self._targets[sig.symbol] = _Targets(self._round(sig.stop_loss), self._round(sig.take_profit))
        placed = await self._submit(order)
        if placed is None or placed.status is OrderStatus.REJECTED:
            if previous is None:
                self._targets.pop(sig.symbol, None)
            else:
                self._targets[sig.symbol] = previous

    # ------------------------------------------------------------ hành động risk ----

    async def handle_actions(self, actions: list[RiskAction]) -> None:
        for a in actions:
            if a.kind is ActionKind.KILL:
                if not self.halted:
                    self._event("action", f"KILL: {a.reason}")
                self.halted = True
                await self._flatten_everything()
            elif a.kind is ActionKind.FLATTEN_ALL:
                self._event("action", f"FLATTEN_ALL: {a.reason}")
                await self._flatten_everything()
            elif a.kind is ActionKind.FLATTEN_SYMBOL:
                self._event("action", f"FLATTEN_SYMBOL: {a.reason}", a.symbol or "")
                await self._close(a.symbol, "flatten")
            elif a.kind is ActionKind.EMERGENCY_HEDGE:
                # TODO(P4.3): đối ứng trên broker dự phòng; tạm thời đóng trên chính broker.
                self._event("action", f"EMERGENCY_HEDGE ({a.broker}): {a.reason}")
                if a.broker == self.broker.name:
                    await self._flatten_everything()

    async def _flatten_everything(self) -> None:
        symbols = {p.symbol for p in self.state.positions.values() if p.qty != 0}
        symbols |= {o.symbol for o in self.state.working_orders.values()}
        for s in sorted(symbols):
            await self._close(s, "flatten")

    # ------------------------------------------------------------------ nội bộ ----

    def _working(self, symbol: str, tags: Optional[tuple[str, ...]] = None) -> list[Order]:
        return [o for o in self.state.working_orders.values()
                if o.symbol == symbol and o.broker == self.broker.name
                and (tags is None or o.tag in tags)]

    async def _close(self, symbol: str, tag: str) -> None:
        """Huỷ mọi lệnh chờ của hợp đồng rồi MTL đóng net. Đang có lệnh đóng chờ ⇒ không làm gì."""
        if self._working(symbol, CLOSE_TAGS):
            return
        for o in self._working(symbol):
            await self._cancel(o)
        self._targets.pop(symbol, None)
        net = self.state.net_qty(symbol)
        if net == 0:
            return
        side = Side.SELL if net > 0 else Side.BUY
        await self._submit(self._new_order(symbol, side, abs(net), OrderType.MARKET, tag))

    async def _ensure_protection(self, symbol: str) -> None:
        net = self.state.net_qty(symbol)
        protective = self._working(symbol, PROTECT_TAGS)
        if net == 0:
            for o in protective:
                await self._cancel(o)
            return
        if self._working(symbol, CLOSE_TAGS):
            return                                      # đang đóng: không đặt thêm bảo vệ
        targets = self._targets.get(symbol)
        if targets is None or targets.stop is None:
            self._event("info", f"Net {net:+d} nhưng không có giá stop — risk sẽ yêu cầu đóng", symbol)
            return
        side = Side.SELL if net > 0 else Side.BUY
        await self._sync(symbol, "stop", side, abs(net), OrderType.STOP, targets.stop)
        if targets.take_profit is not None:
            await self._sync(symbol, "take_profit", side, abs(net), OrderType.LIMIT,
                             targets.take_profit)

    async def _sync(self, symbol: str, tag: str, side: Side, qty: int, otype: OrderType,
                    price: float) -> None:
        existing = self._working(symbol, (tag,))
        good = [o for o in existing if o.side is side and o.qty - o.filled_qty == qty
                and o.price == price]
        if len(good) == 1 and len(existing) == 1:
            return
        for o in existing:
            await self._cancel(o)
        await self._submit(self._new_order(symbol, side, qty, otype, tag, price=price))

    async def _submit(self, order: Order) -> Optional[Order]:
        decision = self.risk.approve(order, self.state)
        if isinstance(decision, Rejected):
            self.rejections.append(decision)
            self._event("rejected", str(decision), order.symbol)
            return None
        placed = await self.broker.place(order)
        self.orders[placed.client_order_id] = placed
        if placed.status is OrderStatus.REJECTED:
            # adapter có thể không phát callback cho lệnh bị từ chối ⇒ tự dọn sổ sách
            self.risk.on_order_update(self.state, placed)
        return placed

    async def _cancel(self, order: Order) -> None:
        try:
            await self.broker.cancel(order.client_order_id)
        except BrokerError as exc:
            self._event("cancel_failed", f"{order.client_order_id}: {exc}", order.symbol)

    def _new_order(self, symbol: str, side: Side, qty: int, otype: OrderType, tag: str,
                   price: Optional[float] = None, stop_loss: Optional[float] = None) -> Order:
        now = self.clock.now()
        return Order(self.ids.next(now), symbol, side, qty, otype, now, price=price,
                     broker=self.broker.name, tag=tag, stop_loss=stop_loss)

    def _round(self, price: Optional[float]) -> Optional[float]:
        if price is None:
            return None
        return round(round(price / self.tick_size) * self.tick_size, 6)

    def _event(self, kind: str, message: str, symbol: str = "") -> None:
        self.events.append(ExecEvent(self.clock.now(), kind, message, symbol))
        level = logging.WARNING if kind in ("rejected", "broker_rejected", "cancel_failed") else logging.INFO
        log.log(level, "%s %s", kind, message)
