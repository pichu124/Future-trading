"""Test TRỰC TIẾP cho core/execution.py (golive checklist §3: "không có tests/test_execution.py").

Broker giả `ScriptBroker` cho điều khiển từng bước: khớp (một phần) theo ý, huỷ có/không xác nhận
ngay, đặt lệnh ném lỗi mạng hoặc bị từ chối. Risk thật (`RiskManager`) — không có đường tắt.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from brokers.base import BrokerAdapter
from core.execution import ExecutionManager
from core.models import Fill, OrderStatus, OrderType, Side, Signal, SignalAction
from risk.risk_manager import ActionKind, RejectCode, RiskAction
from tests.test_risk_manager import MARK, SYM, Env


def run(coro):
    return asyncio.run(coro)


class ScriptBroker(BrokerAdapter):
    def __init__(self, clock, name="dnse"):
        super().__init__(name)
        self.clock = clock
        self.placed, self.cancelled, self.orders = [], [], {}
        self.place_exc = None
        self.reject = None
        self.auto_cancel = True
        self.n = 0

    async def place(self, order):
        if self.place_exc is not None:
            raise self.place_exc
        self.n += 1
        self.placed.append(order)
        if self.reject:
            o = order.with_status(OrderStatus.REJECTED, reject_reason=self.reject)
        else:
            o = order.with_status(OrderStatus.PENDING, broker_order_id=f"X{self.n}")
        self.orders[o.client_order_id] = o
        self._emit_order(o)
        return o

    async def cancel(self, cid):
        self.cancelled.append(cid)
        o = self.orders[cid]
        if self.auto_cancel:
            o = o.with_status(OrderStatus.CANCELLED)
            self.orders[cid] = o
            self._emit_order(o)
        return o

    def confirm_cancels(self):
        for cid in self.cancelled:
            o = self.orders[cid]
            if not o.status.is_terminal:
                o = o.with_status(OrderStatus.CANCELLED)
                self.orders[cid] = o
                self._emit_order(o)

    async def modify(self, cid, *, price=None, qty=None):
        raise AssertionError("ExecutionManager không dùng modify")

    async def positions(self):
        return {}

    async def open_orders(self):
        return []

    def fill(self, cid, qty=None, price=MARK):
        o = self.orders[cid]
        qty = o.qty - o.filled_qty if qty is None else qty
        self._emit_fill(Fill(cid, o.symbol, o.side, qty, price, self.clock.now(), broker=self.name))
        filled = o.filled_qty + qty
        o = replace(o, status=OrderStatus.FILLED if filled >= o.qty else OrderStatus.PARTIALLY_FILLED,
                    filled_qty=filled)
        self.orders[cid] = o
        self._emit_order(o)

    def last(self, tag):
        return [o for o in self.placed if o.tag == tag][-1]


class Rig:
    def __init__(self, session_cfg, cal, tmp_path, emergency=None, **risk_over):
        self.env = Env(session_cfg, cal, tmp_path, **risk_over)
        self.broker = ScriptBroker(self.env.clock)
        self.ex = ExecutionManager(self.broker, self.env.rm, self.env.state, self.env.clock, 0.1,
                                   emergency=emergency)

    def signal(self, action, stop=MARK - 5, tp=None, qty=1):
        s = Signal(action, self.env.now, SYM, stop_loss=stop, take_profit=tp, qty=qty, reason="t")

        async def go():
            await self.ex.on_signal(s)
            await self.ex.drain()
        run(go())

    def fill(self, cid, qty=None, price=MARK):
        self.broker.fill(cid, qty, price)
        run(self.ex.drain())

    def maintain(self):
        async def go():
            await self.ex.maintain()
            await self.ex.drain()
        run(go())

    def working(self, tag=None):
        return [o for o in self.env.state.working_orders.values() if tag is None or o.tag == tag]

    @property
    def net(self):
        return self.env.state.net_qty(SYM)


@pytest.fixture
def rig(session_cfg, cal, tmp_path):
    return Rig(session_cfg, cal, tmp_path)


# --------------------------------------------------------------------------- vào lệnh + bảo vệ ----

class TestEntryAndProtection:
    def test_entry_is_market_with_rounded_stop(self, rig):
        rig.signal(SignalAction.ENTER_LONG, stop=1344.97)
        e = rig.broker.last("entry")
        assert e.order_type is OrderType.MARKET and e.side is Side.BUY and e.qty == 1
        assert e.stop_loss == pytest.approx(1345.0) and e.broker == "dnse"

    def test_stop_and_take_profit_after_fill(self, rig):
        rig.signal(SignalAction.ENTER_LONG, stop=1345.0, tp=1360.0)
        rig.fill(rig.broker.last("entry").client_order_id)
        stop, tp = rig.working("stop"), rig.working("take_profit")
        assert len(stop) == 1 and stop[0].order_type is OrderType.STOP and stop[0].side is Side.SELL
        assert stop[0].price == pytest.approx(1345.0) and stop[0].qty == 1
        assert len(tp) == 1 and tp[0].order_type is OrderType.LIMIT and tp[0].price == pytest.approx(1360.0)

    def test_partial_entry_fill_protects_only_filled_then_requotes(self, session_cfg, cal, tmp_path):
        r = Rig(session_cfg, cal, tmp_path, max_contracts=2)
        r.signal(SignalAction.ENTER_LONG, qty=2)
        cid = r.broker.last("entry").client_order_id
        r.fill(cid, 1)
        assert [o.qty for o in r.working("stop")] == [1]
        first_stop = r.working("stop")[0].client_order_id
        r.fill(cid, 1)
        assert first_stop in r.broker.cancelled
        assert [o.qty for o in r.working("stop")] == [2]

    def test_stop_fill_cancels_take_profit(self, rig):
        rig.signal(SignalAction.ENTER_LONG, stop=1345.0, tp=1360.0)
        rig.fill(rig.broker.last("entry").client_order_id)
        rig.fill(rig.working("stop")[0].client_order_id, price=1345.0)
        assert rig.net == 0 and rig.working() == []
        assert rig.broker.last("take_profit").client_order_id in rig.broker.cancelled

    def test_stop_and_tp_both_fill_flip_is_closed_not_protected(self, rig):
        """golive #4: cả stop lẫn TP khớp ⇒ net −1 ngoài ý muốn ⇒ đóng ngay, không đặt stop sai phía."""
        rig.signal(SignalAction.ENTER_LONG, stop=1345.0, tp=1360.0)
        rig.fill(rig.broker.last("entry").client_order_id)
        stop, tp = rig.working("stop")[0], rig.working("take_profit")[0]
        rig.broker.fill(stop.client_order_id, price=1345.0)
        rig.broker.fill(tp.client_order_id, price=1360.0)
        run(rig.ex.drain())
        assert rig.net == -1
        close = rig.broker.last("flatten")
        assert close.side is Side.BUY and close.qty == 1
        assert not [o for o in rig.working("stop") if o.side is Side.BUY]


# --------------------------------------------------------------------------- đóng lệnh ----

class TestExit:
    def test_exit_cancels_protection_then_closes_once(self, rig):
        rig.signal(SignalAction.ENTER_LONG)
        rig.fill(rig.broker.last("entry").client_order_id)
        rig.signal(SignalAction.EXIT)
        rig.signal(SignalAction.EXIT)                                  # lần hai: đã có lệnh đóng chờ
        exits = [o for o in rig.broker.placed if o.tag == "exit"]
        assert len(exits) == 1 and exits[0].side is Side.SELL and exits[0].order_type is OrderType.MARKET
        assert rig.working("stop") == []

    def test_opposite_entry_only_closes(self, rig):
        rig.signal(SignalAction.ENTER_LONG)
        rig.fill(rig.broker.last("entry").client_order_id)
        rig.signal(SignalAction.ENTER_SHORT, stop=MARK + 5)
        assert [o.tag for o in rig.broker.placed if o.side is Side.SELL and o.tag != "stop"] == ["exit"]
        assert len([o for o in rig.broker.placed if o.tag == "entry"]) == 1

    def test_stale_close_is_requoted_as_market(self, rig):
        """golive #2: MTL khớp một phần ⇒ phần dư thành LO kẹt ⇒ quá 3s: huỷ, đóng lại phần còn lại."""
        r = rig
        r.env.rm.limits = replace(r.env.rm.limits, max_contracts=2)
        r.signal(SignalAction.ENTER_LONG, qty=2)
        r.fill(r.broker.last("entry").client_order_id)
        r.signal(SignalAction.EXIT)
        first = r.broker.last("exit")
        r.fill(first.client_order_id, 1)
        r.env.at(r.env.now + timedelta(seconds=4))
        r.maintain()
        assert first.client_order_id in r.broker.cancelled
        again = r.broker.last("exit")
        assert again.client_order_id != first.client_order_id and again.qty == 1


# --------------------------------------------------------------------------- lỗi broker / risk ----

class TestFailures:
    def test_risk_rejection_recorded_and_not_sent(self, session_cfg, cal, tmp_path):
        r = Rig(session_cfg, cal, tmp_path)
        r.env.at(r.env.now.replace(hour=9, minute=5))                 # trong 15 phút đầu phiên
        r.signal(SignalAction.ENTER_LONG)
        assert r.broker.placed == [] and r.ex.rejections[0].code is RejectCode.OPENING_WINDOW
        assert any(e.kind == "rejected" for e in r.ex.events)

    def test_entry_without_stop_rejected(self, rig):
        rig.signal(SignalAction.ENTER_LONG, stop=None)
        assert rig.broker.placed == [] and rig.ex.rejections[0].code is RejectCode.NO_STOP_LOSS

    def test_place_network_error_leaves_no_ghost_and_stop_is_retried(self, rig):
        """golive #3: lỗi mạng khi đặt stop ⇒ không có lệnh 'ma'; vòng maintain sau đặt lại stop."""
        rig.signal(SignalAction.ENTER_LONG)
        rig.broker.place_exc = ConnectionError("reset")
        rig.fill(rig.broker.last("entry").client_order_id)
        assert rig.working("stop") == [] and any(e.kind == "broker_error" for e in rig.ex.events)
        rig.broker.place_exc = None
        rig.maintain()
        assert len(rig.working("stop")) == 1

    def test_broker_rejection_cleans_book(self, rig):
        rig.broker.reject = "không đủ ký quỹ"
        rig.signal(SignalAction.ENTER_LONG)
        assert rig.working() == [] and any(e.kind == "broker_rejected" for e in rig.ex.events)

    def test_dead_broker_gets_no_orders_and_event_is_rate_limited(self, rig):
        rig.env.state.broker_last_seen["dnse"] = rig.env.now - timedelta(seconds=60)
        rig.signal(SignalAction.ENTER_LONG)
        rig.signal(SignalAction.ENTER_LONG)
        assert rig.broker.placed == []
        assert [e.kind for e in rig.ex.events].count("broker_down") == 1

    def test_unconfirmed_cancel_never_leaves_two_stops(self, session_cfg, cal, tmp_path):
        """golive #8: huỷ stop cũ chưa xác nhận ⇒ KHÔNG đặt stop mới cho tới khi xác nhận."""
        r = Rig(session_cfg, cal, tmp_path, max_contracts=2)
        r.signal(SignalAction.ENTER_LONG, qty=2)
        cid = r.broker.last("entry").client_order_id
        r.fill(cid, 1)
        r.broker.auto_cancel = False
        r.fill(cid, 1)
        assert len(r.working("stop")) == 1                             # chỉ stop cũ (đang chờ huỷ)
        # chính ExecutionManager chờ — không dựa vào risk chặn hộ (DUPLICATE_CLOSE)
        assert len([o for o in r.broker.placed if o.tag == "stop"]) == 1
        assert not any(x.code is RejectCode.DUPLICATE_CLOSE for x in r.ex.rejections)
        r.broker.confirm_cancels()
        r.maintain()
        assert [o.qty for o in r.working("stop")] == [2]

    def test_position_without_plan_is_closed(self, rig):
        """Vị thế có sẵn lúc khởi động (bot không có kế hoạch stop) ⇒ đóng."""
        from core.models import Position
        rig.env.state.positions["dnse:" + SYM] = Position(SYM, 1, MARK, broker="dnse")
        rig.maintain()
        assert rig.broker.last("flatten").side is Side.SELL


# --------------------------------------------------------------------------- hành động risk ----

class TestActions:
    def act(self, rig, kind, **kw):
        async def go():
            await rig.ex.handle_actions([RiskAction(kind, "t", **kw)])
            await rig.ex.drain()
        run(go())

    def long(self, rig):
        rig.signal(SignalAction.ENTER_LONG)
        rig.fill(rig.broker.last("entry").client_order_id)

    def test_kill_flattens_and_halts(self, rig):
        self.long(rig)
        self.act(rig, ActionKind.KILL)
        assert rig.ex.halted and rig.broker.last("flatten").side is Side.SELL
        n, nrej = len(rig.broker.placed), len(rig.ex.rejections)
        rig.signal(SignalAction.ENTER_LONG)
        assert len(rig.broker.placed) == n                              # đã dừng: bỏ qua tín hiệu
        assert len(rig.ex.rejections) == nrej                           # bỏ qua TRƯỚC risk, không phải risk chặn
        assert any("đã dừng" in e.message for e in rig.ex.events)

    def test_flatten_symbol(self, rig):
        self.long(rig)
        self.act(rig, ActionKind.FLATTEN_SYMBOL, symbol=SYM)
        assert rig.broker.last("flatten").qty == 1 and rig.working("stop") == []

    def test_emergency_without_handler_flattens_primary(self, rig):
        self.long(rig)
        self.act(rig, ActionKind.EMERGENCY_HEDGE, broker="dnse")
        assert rig.broker.last("flatten").side is Side.SELL

    def test_emergency_with_handler_delegates(self, session_cfg, cal, tmp_path):
        got = []

        class H:
            async def on_action(self, a):
                got.append(a.kind)
        r = Rig(session_cfg, cal, tmp_path, emergency=H())
        self.long(r)
        n = len(r.broker.placed)
        self.act(r, ActionKind.EMERGENCY_HEDGE, broker="dnse")
        assert got == [ActionKind.EMERGENCY_HEDGE] and len(r.broker.placed) == n

    def test_hold_signal_ignored(self, rig):
        rig.signal(SignalAction.HOLD, stop=None)
        assert rig.broker.placed == []
