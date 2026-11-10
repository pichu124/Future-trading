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
  dừng nhận tín hiệu. EMERGENCY_HEDGE ⇒ `live.emergency.EmergencyHandler` (đối ứng trên broker
  dự phòng); không có handler thì đóng trên chính broker. Đóng vị thế mà net = 0 nhưng còn vị thế
  chéo broker (sau đối ứng) ⇒ nhờ handler gỡ cả hai chân.
- Ngoài phiên liên tục (nghỉ trưa, ATO/ATC, ngoài giờ) MTL không đặt được ⇒ `_close` HOÃN: không huỷ stop/TP,
  không xoá kế hoạch, không gửi gì; báo MỘT lần (`deferred`); vào phiên liên tục kế tiếp mới đóng. `maintain()`
  đứng yên ngoài phiên. (Lỗi 10/11/2026: huỷ stop rồi MTL bị từ chối ⇒ vị thế trần cả giờ trưa + gửi MTL mỗi vòng.)
- Hành động risk lặp lại (vd FLATTEN_ALL mỗi vòng khi đã khoá) chỉ ghi sự kiện tối đa mỗi phút một lần.
- Mọi lệnh đi qua `risk.approve` — không có đường tắt.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Awaitable, Callable, Optional

from brokers.base import BrokerAdapter, BrokerError
from core.clock import Clock, SessionState
from core.models import (AccountState, Fill, Order, OrderStatus, OrderType, Side, Signal,
                         SignalAction)
from monitoring.logger import log_event
from risk.risk_manager import ActionKind, OrderIdFactory, RiskAction, RiskManager, Rejected

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())   # thư viện: runner/CLI tự cấu hình logging

CLOSE_TAGS = ("exit", "flatten", "unwind")
PROTECT_TAGS = ("stop", "take_profit")


@dataclass
class ExecEvent:
    ts: datetime
    kind: str            # "rejected" | "broker_rejected" | "cancel_failed" | "action" | "info" | "deferred"
    message: str
    symbol: str = ""


@dataclass
class _Targets:
    stop: Optional[float]
    take_profit: Optional[float]
    side: Optional[Side] = None          # chiều của lệnh vào — net khác chiều ⇒ đảo chiều ngoài ý muốn


class ExecutionManager:
    def __init__(self, broker: BrokerAdapter, risk: RiskManager, state: AccountState,
                 clock: Clock, tick_size: float, ids: Optional[OrderIdFactory] = None,
                 emergency=None, close_requote_s: float = 3.0):
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
        self.emergency = emergency                       # live.emergency.EmergencyHandler | None
        self.close_requote_s = close_requote_s
        self._sent_at: dict[str, datetime] = {}
        self._last_down_event: Optional[datetime] = None
        self._targets: dict[str, _Targets] = {}
        self._queue: deque[Callable[[], Awaitable[None]]] = deque()
        self._deferred: dict[str, tuple] = {}         # symbol → (ngày, trạng thái phiên) đã báo hoãn
        self._action_logged: dict[tuple, datetime] = {}  # (loại, lý do) → lần ghi gần nhất
        broker.on_fill(self._on_fill)
        broker.on_order_update(self._on_update)

    # ------------------------------------------------------------ callback broker ----

    def _on_update(self, order: Order) -> None:
        self.orders[order.client_order_id] = order
        self.risk.on_order_update(self.state, order)
        log_event(log, "order", client_order_id=order.client_order_id, broker=order.broker,
                  broker_order_id=order.broker_order_id, symbol=order.symbol, side=order.side,
                  qty=order.qty, filled_qty=order.filled_qty, order_type=order.order_type, price=order.price,
                  status=order.status, tag=order.tag, reject_reason=order.reject_reason or None)
        if order.status is OrderStatus.REJECTED:
            self._event("broker_rejected", f"{order.client_order_id} ({order.tag}): "
                                           f"{order.reject_reason}", order.symbol)

    def _on_fill(self, fill: Fill) -> None:
        self.risk.record_fill(self.state, fill)
        log_event(log, "fill", client_order_id=fill.client_order_id, broker=fill.broker, symbol=fill.symbol,
                  side=fill.side, qty=fill.qty, price=fill.price, fee_vnd=fill.fee_vnd, fill_ts=fill.ts,
                  net_after=self.state.net_qty(fill.symbol))
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
        self._targets[sig.symbol] = _Targets(self._round(sig.stop_loss), self._round(sig.take_profit), side)
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
                self._action_event(a, f"FLATTEN_ALL: {a.reason}")
                await self._flatten_everything()
            elif a.kind is ActionKind.FLATTEN_SYMBOL:
                self._action_event(a, f"FLATTEN_SYMBOL: {a.reason}", a.symbol or "")
                await self._close(a.symbol, "flatten")
            elif a.kind is ActionKind.EMERGENCY_HEDGE:
                self._event("action", f"EMERGENCY_HEDGE ({a.broker}): {a.reason}")
                if self.emergency is not None:
                    await self.emergency.on_action(a)
                elif a.broker == self.broker.name:
                    await self._flatten_everything()

    def _action_event(self, a: RiskAction, message: str, symbol: str = "") -> None:
        """risk.check phát lại cùng hành động mỗi vòng tới khi phẳng ⇒ ghi tối đa mỗi phút một lần."""
        now = self.clock.now()
        key = (a.kind, a.reason, symbol)
        last = self._action_logged.get(key)
        if last is None or (now - last).total_seconds() >= 60:
            self._action_logged[key] = now
            self._event("action", message, symbol)

    # ------------------------------------------------------------------ phiên ----

    def _continuous(self) -> bool:
        session_state = getattr(self.risk.clock, "session_state", None)
        return session_state is None or session_state(self.clock.now()) is SessionState.CONTINUOUS

    def _next_continuous_open(self) -> Optional[time]:
        sched = getattr(self.risk.clock, "schedule", None)
        if sched is None:
            return None
        t = self.clock.now().time()
        if t < sched.continuous_am_start:
            return sched.continuous_am_start
        if t < sched.continuous_pm_start:
            return sched.continuous_pm_start
        return None                                     # đã qua phiên liên tục cuối ngày

    def _defer_close(self, symbol: str, tag: str) -> None:
        now = self.clock.now()
        state = self.risk.clock.session_state(now)
        key = (now.date(), state)
        if self._deferred.get(symbol) == key:
            return
        self._deferred[symbol] = key
        nxt = self._next_continuous_open()
        when = (f"đóng lúc {nxt:%H:%M}" if nxt is not None
                else "KHÔNG còn phiên liên tục hôm nay — CÒN VỊ THẾ, xử lý tay")
        self._event("deferred", f"Chưa đóng được ({tag}): đang {state.value}, MTL chỉ đặt trong phiên liên tục — "
                                f"giữ nguyên stop/chốt lời, {when}", symbol)

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
        """Huỷ mọi lệnh chờ của hợp đồng rồi MTL đóng net. Đang có lệnh đóng chờ ⇒ không làm gì.

        Ngoài phiên liên tục ⇒ hoãn (không huỷ, không gửi) — xem docstring module."""
        if self._working(symbol, CLOSE_TAGS):
            return
        if not self._continuous():
            if self.state.net_qty(symbol) != 0 or self._working(symbol):
                self._defer_close(symbol, tag)
            return
        self._deferred.pop(symbol, None)
        for o in self._working(symbol):
            await self._cancel(o)
        self._targets.pop(symbol, None)
        net = self.state.net_qty(symbol)
        if net == 0:
            gross = any(p.qty != 0 for p in self.state.positions.values() if p.symbol == symbol)
            if gross and self.emergency is not None:
                await self.emergency.unwind(symbol, at_flatten=(tag == "flatten"))
            return
        side = Side.SELL if net > 0 else Side.BUY
        await self._submit(self._new_order(symbol, side, abs(net), OrderType.MARKET, tag))

    async def maintain(self) -> None:
        """Gọi mỗi vòng (runner/engine): đặt lại lệnh đóng bị kẹt, giữ stop cho vị thế đang mở.

        - Lệnh đóng chờ quá `close_requote_s` (vd MTL khớp một phần ⇒ phần dư thành LO, giá chạy
          mất) ⇒ huỷ và đóng lại bằng MTL cho phần còn lại (golive #2).
        - Vị thế đang mở mà không có lệnh đóng ⇒ đảm bảo bảo vệ (đặt lại stop nếu lần trước lỗi).
        """
        if not self._continuous():
            return                                      # nghỉ trưa/ATC: không huỷ, không đặt lại gì
        now = self.clock.now()
        symbols = {p.symbol for p in self.state.positions.values() if p.qty != 0}
        symbols |= {o.symbol for o in self.state.working_orders.values() if o.broker == self.broker.name}
        for symbol in sorted(symbols):
            closes = self._working(symbol, CLOSE_TAGS)
            stale = [o for o in closes if o.tag in ("exit", "flatten")
                     and (now - self._sent_at.get(o.client_order_id, now)).total_seconds()
                     >= self.close_requote_s]
            if stale:
                for o in stale:
                    await self._cancel(o)
                if not self._working(symbol, CLOSE_TAGS):
                    self._event("info", f"Lệnh đóng chờ > {self.close_requote_s:g}s ⇒ đặt lại MTL", symbol)
                    await self._close(symbol, stale[0].tag)
            elif not closes and self.state.net_qty(symbol) != 0:
                await self._ensure_protection(symbol)

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
        if targets is not None and targets.side is not None and (net > 0) != (targets.side is Side.BUY):
            # vd stop và TP cùng khớp ⇒ đảo chiều ngoài ý muốn: ĐÓNG, không bảo vệ (golive #4)
            self._event("info", f"Đảo chiều ngoài ý muốn (net {net:+d}) ⇒ đóng ngay", symbol)
            await self._close(symbol, "flatten")
            return
        if targets is None or targets.stop is None:
            # vị thế không có kế hoạch (nhận lúc khởi động, hoặc sau khi đã đóng mà còn sót) ⇒ đóng
            self._event("info", f"Net {net:+d} không có stop/kế hoạch ⇒ đóng (CLAUDE.md mục 5)", symbol)
            await self._close(symbol, "flatten")
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
        if self._working(symbol, (tag,)):
            return            # huỷ chưa được xác nhận ⇒ đợi vòng sau, không để hai lệnh cùng sống (golive #8)
        await self._submit(self._new_order(symbol, side, qty, otype, tag, price=price))

    async def _submit(self, order: Order) -> Optional[Order]:
        now = self.clock.now()
        if not self.risk.broker_alive(self.state, self.broker.name, now):
            # không gửi lệnh tới broker đang mất kết nối — quy trình khẩn cấp lo đối ứng (golive #5)
            if self._last_down_event is None or (now - self._last_down_event).total_seconds() >= 60:
                self._last_down_event = now
                self._event("broker_down", f"{self.broker.name} mất kết nối ⇒ không gửi {order.tag}",
                            order.symbol)
            return None
        decision = self.risk.approve(order, self.state)
        if isinstance(decision, Rejected):
            self.rejections.append(decision)
            self._event("rejected", str(decision), order.symbol, code=getattr(decision, "code", None),
                        client_order_id=order.client_order_id, tag=order.tag)
            return None
        try:
            placed = await self.broker.place(order)
        except Exception as exc:
            # lỗi mạng/không rõ: không để lệnh "ma" trong sổ; adapter thật phải đối soát sổ lệnh
            # trước khi coi là chưa gửi (DNSE không có idempotency) — golive #3
            self.risk.on_order_update(self.state, order.with_status(
                OrderStatus.REJECTED, reject_reason=f"{type(exc).__name__}: {exc}"))
            self._event("broker_error", f"{order.client_order_id} ({order.tag}): {type(exc).__name__}: {exc}",
                        order.symbol)
            return None
        self.orders[placed.client_order_id] = placed
        self._sent_at[placed.client_order_id] = now
        if placed.status is OrderStatus.REJECTED:
            # adapter có thể không phát callback cho lệnh bị từ chối ⇒ tự dọn sổ sách
            self.risk.on_order_update(self.state, placed)
        return placed

    async def _cancel(self, order: Order) -> None:
        try:
            await self.broker.cancel(order.client_order_id)
        except Exception as exc:
            self._event("cancel_failed", f"{order.client_order_id}: {type(exc).__name__}: {exc}",
                        order.symbol)

    def _new_order(self, symbol: str, side: Side, qty: int, otype: OrderType, tag: str,
                   price: Optional[float] = None, stop_loss: Optional[float] = None) -> Order:
        now = self.clock.now()
        return Order(self.ids.next(now), symbol, side, qty, otype, now, price=price,
                     broker=self.broker.name, tag=tag, stop_loss=stop_loss)

    def _round(self, price: Optional[float]) -> Optional[float]:
        if price is None:
            return None
        return round(round(price / self.tick_size) * self.tick_size, 6)

    def _event(self, kind: str, message: str, symbol: str = "", **fields) -> None:
        self.events.append(ExecEvent(self.clock.now(), kind, message, symbol))
        level = logging.WARNING if kind in ("rejected", "broker_rejected", "cancel_failed", "broker_error",
                                            "broker_down", "deferred") else logging.INFO
        log.log(level, "%s %s", kind, message,
                extra={"event": f"exec_{kind}", "symbol": symbol or None, "detail": message, **fields})
