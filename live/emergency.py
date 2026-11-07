"""Quy trình khẩn cấp: broker chính mất kết nối khi đang có vị thế (CLAUDE.md mục 3, 5; PROMPTS P4.3).

Kích hoạt bởi `RiskAction(EMERGENCY_HEDGE, broker=<chính>)` từ `RiskManager.check()` (broker im quá
`broker_timeout_seconds` trong khi có vị thế). Các bước:

1. Khoá mở vị thế mới: `state.emergency_reason` (risk từ chối mọi lệnh mở; chỉ người xoá bằng
   `RiskManager.clear_emergency` sau khi đối soát — không tự mở khoá, kể cả sang ngày mới).
2. Đặt MTL ĐỐI ỨNG trên broker dự phòng để net (cộng mọi broker) về 0 — qua `risk.approve`.
   Idempotent: risk phát tín hiệu mỗi lần `check()` chừng nào broker chính còn im, nên luôn tính
   `cần = −net − phần đối ứng đang chờ`; = 0 ⇒ không làm gì.
3. Lệnh đối ứng chờ quá `hedge_requote_s` (vd MTL khớp một phần, phần dư thành LO không khớp
   khi giá chạy) ⇒ huỷ và đặt lại MTL cho phần còn thiếu.
4. Broker dự phòng từ chối / lỗi ⇒ thử lại tối đa `max_attempts` lần (id mới mỗi lần), rồi cảnh báo
   XỬ LÝ TAY. Broker dự phòng cũng mất kết nối ⇒ cảnh báo "CẢ HAI BROKER LỖI" (giới hạn tần suất).
5. Gỡ hedge (`unwind`) khi broker chính sống lại hoặc tới giờ flatten: đóng ĐỒNG THỜI từng chân trên
   broker của nó (`risk.approve_unwind` duyệt cả bộ). Broker chính vẫn chết lúc flatten ⇒ GIỮ hedge
   (đóng chân dự phòng sẽ để lộ vị thế trần) và cảnh báo xử lý tay.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Optional

from brokers.base import BrokerAdapter
from core.clock import MarketClock
from core.models import AccountState, Order, OrderStatus, OrderType, Side
from risk.risk_manager import ActionKind, OrderIdFactory, RiskAction, RiskManager

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

EMERGENCY_TAG = "emergency"
UNWIND_TAG = "unwind"
_LIVE = (OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)


class EmergencyHandler:
    def __init__(self, risk: RiskManager, state: AccountState, brokers: dict[str, BrokerAdapter],
                 primary: str, backup: str, clock: MarketClock,
                 notify: Optional[Callable[[str], Any]] = None, ids: Optional[OrderIdFactory] = None,
                 max_attempts: int = 3, alert_every_s: float = 60.0, hedge_requote_s: float = 5.0):
        if primary == backup or primary not in brokers or backup not in brokers:
            raise ValueError("Cần hai broker khác nhau: chính và dự phòng")
        self.risk, self.state, self.brokers = risk, state, brokers
        self.primary, self.backup = primary, backup
        self.clock = clock
        self.notify = notify
        self.ids = ids or OrderIdFactory("E")
        self.max_attempts = max_attempts
        self.alert_every_s = alert_every_s
        self.hedge_requote_s = hedge_requote_s
        self._attempts: Counter = Counter()
        self._placed_at: dict[str, datetime] = {}
        self._last_alert: dict[str, datetime] = {}
        b = brokers[backup]
        b.on_fill(lambda f: risk.record_fill(state, f))
        b.on_order_update(lambda o: risk.on_order_update(state, o))

    # ------------------------------------------------------------------ tiện ích ----

    def _alive(self, name: str) -> bool:
        return self.risk.broker_alive(self.state, name)

    async def _say(self, text: str, key: Optional[str] = None) -> None:
        now = self.clock.now()
        if key is not None:
            last = self._last_alert.get(key)
            if last is not None and (now - last).total_seconds() < self.alert_every_s:
                return
            self._last_alert[key] = now
        log.warning("emergency: %s", text)
        if self.notify is None:
            return
        try:
            res = self.notify(text)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("emergency: gửi cảnh báo lỗi")

    def _orders(self, symbol: str, broker: str, tag: Optional[str] = None) -> list[Order]:
        return [o for o in self.state.working_orders.values()
                if o.symbol == symbol and o.broker == broker and o.status in _LIVE
                and (tag is None or o.tag == tag)]

    def _gross_symbols(self) -> list[str]:
        return sorted({p.symbol for p in self.state.positions.values() if p.qty != 0})

    # ------------------------------------------------------------------ đối ứng ----

    async def on_action(self, action: RiskAction) -> None:
        if action.kind is not ActionKind.EMERGENCY_HEDGE:
            return
        if action.broker == self.primary:
            await self.hedge()
        else:
            await self._say(f"🚨 Broker {action.broker} (đang giữ chân đối ứng) mất kết nối — "
                            "KIỂM TRA VÀ XỬ LÝ TAY", key=f"down:{action.broker}")

    async def hedge(self) -> None:
        now = self.clock.now()
        if not self.state.emergency_reason:
            self.state.emergency_reason = f"{self.primary} mất kết nối lúc {now:%H:%M:%S} khi đang có vị thế"
            await self._say(f"🚨 Broker {self.primary} mất kết nối khi đang có vị thế — đặt lệnh đối ứng "
                            f"trên {self.backup}, khoá mở vị thế mới")
        if not self._alive(self.backup):
            await self._say(f"🚨🚨 CẢ HAI BROKER LỖI ({self.primary}, {self.backup}) khi đang có vị thế — "
                            "XỬ LÝ TAY NGAY trên app", key="both_down")
            return
        backup = self.brokers[self.backup]
        for symbol in self._gross_symbols():
            await self._requote_stale(symbol, now)
            net = self.state.net_qty(symbol)
            pending = sum(o.side.sign * (o.qty - o.filled_qty)
                          for o in self._orders(symbol, self.backup, EMERGENCY_TAG))
            needed = -net - pending
            if needed == 0:
                continue
            if self._attempts[symbol] >= self.max_attempts:
                await self._say(f"🚨 Đối ứng {symbol} thất bại {self._attempts[symbol]} lần, net {net:+d} — "
                                "XỬ LÝ TAY NGAY", key=f"hedge_fail:{symbol}")
                continue
            side = Side.BUY if needed > 0 else Side.SELL
            order = Order(self.ids.next(now), symbol, side, abs(needed), OrderType.MARKET, now,
                          broker=self.backup, tag=EMERGENCY_TAG)
            decision = self.risk.approve(order, self.state)
            if not decision.ok:
                self._attempts[symbol] += 1
                await self._say(f"⚠️ Risk từ chối lệnh đối ứng {symbol}: {decision}")
                continue
            try:
                placed = await backup.place(order)
            except Exception as exc:
                self._attempts[symbol] += 1
                self.risk.on_order_update(self.state, order.with_status(
                    OrderStatus.REJECTED, reject_reason=f"{type(exc).__name__}: {exc}"))
                await self._say(f"⚠️ {self.backup} lỗi khi đặt đối ứng {symbol} "
                                f"(lần {self._attempts[symbol]}/{self.max_attempts}): {exc}")
                continue
            if placed.status is OrderStatus.REJECTED:
                self._attempts[symbol] += 1
                self.risk.on_order_update(self.state, placed)
                await self._say(f"⚠️ {self.backup} từ chối đối ứng {symbol} "
                                f"(lần {self._attempts[symbol]}/{self.max_attempts}): {placed.reject_reason}")
                continue
            self._placed_at[order.client_order_id] = now
            await self._say(f"⚠️ Đã đặt đối ứng {side.value} {abs(needed)} {symbol} trên {self.backup} "
                            f"(net hiện tại {net:+d})")

    async def _requote_stale(self, symbol: str, now: datetime) -> None:
        backup = self.brokers[self.backup]
        for o in self._orders(symbol, self.backup, EMERGENCY_TAG):
            placed = self._placed_at.get(o.client_order_id)
            if placed is None or (now - placed).total_seconds() < self.hedge_requote_s:
                continue
            try:
                await backup.cancel(o.client_order_id)
                log.warning("emergency: huỷ đối ứng %s chờ quá %.0fs để đặt lại MTL",
                            o.client_order_id, self.hedge_requote_s)
            except Exception as exc:
                log.warning("emergency: không huỷ được %s: %s", o.client_order_id, exc)
            self._placed_at.pop(o.client_order_id, None)

    # ------------------------------------------------------------------ gỡ hedge ----

    def needs_unwind(self) -> bool:
        for s in self._gross_symbols():
            legs = {p.broker for p in self.state.positions.values() if p.symbol == s and p.qty != 0}
            if self.state.net_qty(s) == 0 and len(legs) >= 2:
                return True
        return False

    async def unwind(self, symbol: Optional[str] = None, at_flatten: bool = False) -> bool:
        if not self._alive(self.primary) or not self._alive(self.backup):
            if at_flatten:
                await self._say(f"🚨 Tới giờ flatten nhưng broker mất kết nối — GIỮ hedge (net = 0), "
                                "XỬ LÝ TAY trước khi hết phiên", key="unwind_blocked")
            return False
        now = self.clock.now()
        ok, sent = True, False
        for s in ([symbol] if symbol else self._gross_symbols()):
            legs = {p.broker: p.qty for p in self.state.positions.values() if p.symbol == s and p.qty != 0}
            if self.state.net_qty(s) != 0 or not legs:
                continue
            if any(self._orders(s, b, UNWIND_TAG) for b in self.brokers):
                continue                                         # đang gỡ dở — không gửi lặp
            for b in (self.primary, self.backup):
                for o in self._orders(s, b):
                    try:
                        await self.brokers[b].cancel(o.client_order_id)
                    except Exception as exc:
                        log.warning("emergency: không huỷ được %s trên %s: %s", o.client_order_id, b, exc)
            orders = [Order(self.ids.next(now), s, Side.SELL if q > 0 else Side.BUY, abs(q),
                            OrderType.MARKET, now, broker=b, tag=UNWIND_TAG) for b, q in sorted(legs.items())]
            decisions = self.risk.approve_unwind(orders, self.state)
            if not all(d.ok for d in decisions):
                ok = False
                await self._say(f"⚠️ Không gỡ được hedge {s}: {decisions[0]}")
                continue
            results = await asyncio.gather(*(self.brokers[o.broker].place(o) for o in orders),
                                           return_exceptions=True)
            for o, r in zip(orders, results):
                if isinstance(r, BaseException) or r.status is OrderStatus.REJECTED:
                    ok = False
                    reason = r if isinstance(r, BaseException) else r.reject_reason
                    self.risk.on_order_update(self.state, o.with_status(OrderStatus.REJECTED,
                                                                        reject_reason=str(reason)))
                    await self._say(f"🚨 Gỡ hedge {s}: chân {o.broker} lỗi ({reason}) — net có thể lệch, "
                                    "KIỂM TRA NGAY")
            sent = True
        if sent and ok:
            await self._say("✅ Đã gửi lệnh gỡ hedge trên cả hai broker. Bot vẫn KHOÁ mở vị thế tới khi "
                            "bạn đối soát và gọi clear_emergency.")
        return ok and sent
