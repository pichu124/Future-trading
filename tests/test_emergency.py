"""Test cho live/emergency.py + phần risk liên quan (PROMPTS P4.3) — viết TRƯỚC.

Kịch bản: broker chính (dnse) mất kết nối khi đang có vị thế ⇒ lệnh đối ứng trên broker dự phòng
(ssi) để net = 0, khoá mở vị thế mới, cảnh báo. Gồm: mất kết nối giữa chừng, khớp một phần, broker
dự phòng từ chối/lỗi, cả hai broker đều lỗi, gỡ hedge khi broker chính sống lại, tới giờ flatten.
"""
import asyncio
from datetime import date, datetime, timedelta
from itertools import count

import pytest

from brokers.base import BrokerError
from brokers.paper import PaperBroker, PaperConfig
from core.clock import FakeClock, MarketClock, SessionSchedule
from core.execution import ExecutionManager
from core.models import (AccountState, Order, OrderStatus, OrderType, Position, Side, Signal,
                         SignalAction, Tick)
from live.emergency import EmergencyHandler
from risk.risk_manager import ActionKind, RejectCode, RiskManager
from tests.test_risk_manager import make_settings

SYM = "VN30F2611"
T0 = datetime(2026, 11, 9, 10, 0, 0)
_ids = count()


class FlakyBroker(PaperBroker):
    """Paper broker có thể ném lỗi hoặc từ chối mọi lệnh (giả lập broker dự phòng hỏng)."""

    def __init__(self, *a, mode=None, **kw):
        super().__init__(*a, **kw)
        self.mode = mode
        self.place_calls = 0

    async def place(self, order):
        self.place_calls += 1
        if self.mode == "raise":
            raise BrokerError("timeout")
        if self.mode == "reject":
            o = order.with_status(OrderStatus.REJECTED, reject_reason="hết sức mua", broker=self.name)
            self._emit_order(o)
            return o
        return await super().place(order)


class Rig:
    def __init__(self, session_cfg, cal, tmp_path, backup_mode=None, **risk_over):
        self.clock = FakeClock(T0)
        self.mclock = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, self.clock)
        self.risk = RiskManager.from_settings(make_settings(session_cfg, **risk_over), self.mclock,
                                              kill_file=tmp_path / "KILL")
        self.state = AccountState(trading_day=T0.date(), last_prices={SYM: 1350.0}, market_data_at=T0)
        cfg = PaperConfig(tick_size=0.1, slippage_ticks=0, fee_vnd_per_contract=0, latency_ms=0)
        self.dnse = PaperBroker(cfg, self.clock, name="dnse", session=self.mclock)
        self.ssi = FlakyBroker(cfg, self.clock, name="ssi", session=self.mclock, mode=backup_mode)
        self.notes: list[str] = []
        self.emg = EmergencyHandler(self.risk, self.state, {"dnse": self.dnse, "ssi": self.ssi},
                                    primary="dnse", backup="ssi", clock=self.mclock,
                                    notify=self.notes.append, max_attempts=3, alert_every_s=60)
        self.exe = ExecutionManager(self.dnse, self.risk, self.state, self.clock, 0.1,
                                    emergency=self.emg)
        self.alive("dnse", "ssi")

    def run(self, coro):
        return asyncio.run(coro)

    def alive(self, *brokers):
        for b in brokers:
            self.state.broker_last_seen[b] = self.clock.now()

    def advance(self, seconds, alive=("dnse", "ssi"), data=True):
        self.clock.advance(seconds=seconds)
        self.alive(*alive)
        if data:
            self.state.market_data_at = self.clock.now()

    def tick(self, broker, bid=1350.0, ask=1350.2, bid_qty=None, ask_qty=None):
        self.state.market_data_at = self.clock.now()         # feed dữ liệu vẫn chạy
        b = self.dnse if broker == "dnse" else self.ssi
        b.on_tick(Tick(SYM, self.clock.now(), last=(bid + ask) / 2, bid=bid, ask=ask,
                       bid_qty=bid_qty, ask_qty=ask_qty))

    async def step(self):
        await self.exe.drain()
        actions = self.risk.check(self.state)
        await self.exe.handle_actions(actions)
        await self.exe.drain()
        return actions

    def open_long(self, qty=1):
        async def go():
            await self.exe.on_signal(Signal(SignalAction.ENTER_LONG, self.clock.now(), SYM,
                                            stop_loss=1340.0, qty=qty))
            self.advance(1)
            self.tick("dnse")
            await self.exe.drain()
        self.run(go())
        assert self.state.net_qty(SYM) == qty

    def ssi_orders(self, tag="emergency"):
        return [o for o in self.state.working_orders.values() if o.broker == "ssi" and o.tag == tag]


@pytest.fixture
def rig(session_cfg, cal, tmp_path):
    return Rig(session_cfg, cal, tmp_path)


# ------------------------------------------------------------------ đối ứng ----

class TestHedge:
    def test_primary_disconnect_hedges_on_backup(self, rig):
        rig.open_long()
        rig.advance(11, alive=("ssi",))                    # dnse im > 10s
        actions = rig.run(rig.step())
        assert ActionKind.EMERGENCY_HEDGE in [a.kind for a in actions]
        hedges = rig.ssi_orders()
        assert len(hedges) == 1 and hedges[0].side is Side.SELL and hedges[0].qty == 1
        assert rig.state.emergency_reason and "dnse" in rig.state.emergency_reason
        assert any("đối ứng" in n for n in rig.notes)
        rig.advance(1, alive=("ssi",))
        rig.tick("ssi")
        rig.run(rig.step())
        assert rig.state.net_qty(SYM) == 0
        assert rig.state.positions["ssi:" + SYM].qty == -1

    def test_entries_blocked_during_emergency(self, rig):
        rig.open_long()
        rig.advance(11, alive=("ssi",))
        rig.run(rig.step())
        o = Order("NEWENTRY1", SYM, Side.BUY, 1, OrderType.MARKET, rig.clock.now(), broker="dnse",
                  stop_loss=1340.0)
        d = rig.risk.approve(o, rig.state)
        assert not d.ok and d.code is RejectCode.EMERGENCY

    def test_repeated_actions_do_not_duplicate(self, rig):
        rig.open_long()
        rig.advance(11, alive=("ssi",))
        for _ in range(5):
            rig.run(rig.step())
        assert len(rig.ssi_orders()) == 1 and rig.ssi.place_calls == 1
        rig.advance(1, alive=("ssi",))
        rig.tick("ssi")
        for _ in range(5):                                  # dnse vẫn im ⇒ risk vẫn báo, nhưng net = 0
            rig.advance(1, alive=("ssi",))
            rig.run(rig.step())
        assert rig.ssi.place_calls == 1

    def test_partial_fill_completes_without_duplicate(self, session_cfg, cal, tmp_path):
        r = Rig(session_cfg, cal, tmp_path, max_contracts=2)
        r.open_long(qty=2)
        r.advance(11, alive=("ssi",))
        r.run(r.step())
        r.advance(1, alive=("ssi",))
        r.tick("ssi", bid_qty=1)                            # chỉ khớp 1/2
        r.run(r.step())
        assert r.state.net_qty(SYM) == 1 and r.ssi.place_calls == 1
        r.advance(1, alive=("ssi",))
        r.run(r.step())
        assert r.ssi.place_calls == 1                       # phần dư vẫn đang chờ ⇒ không đặt thêm
        r.advance(1, alive=("ssi",))
        r.tick("ssi", bid=1349.0, ask=1349.2)               # giá rơi: LO bán phần dư (1349.9) KHÔNG khớp
        r.run(r.step())
        assert r.state.net_qty(SYM) == 1
        r.advance(4, alive=("ssi",))                        # chờ quá hedge_requote_s ⇒ huỷ, đặt lại MTL
        r.run(r.step())
        assert r.ssi.place_calls == 2 and len(r.ssi_orders()) == 1
        r.advance(1, alive=("ssi",))
        r.tick("ssi", bid=1348.5, ask=1348.7)
        r.run(r.step())
        assert r.state.net_qty(SYM) == 0

    def test_no_position_no_emergency(self, rig):
        rig.advance(30, alive=("ssi",))
        rig.run(rig.step())
        assert rig.ssi.place_calls == 0 and rig.state.emergency_reason is None and rig.notes == []


# ------------------------------------------------------------------ broker dự phòng lỗi ----

class TestBackupFailures:
    @pytest.mark.parametrize("mode", ["raise", "reject"])
    def test_backup_failure_retries_then_escalates(self, session_cfg, cal, tmp_path, mode):
        r = Rig(session_cfg, cal, tmp_path, backup_mode=mode)
        r.open_long()
        for _ in range(6):
            r.advance(11, alive=("ssi",))
            r.run(r.step())
        assert r.ssi.place_calls == 3                       # max_attempts
        assert any("XỬ LÝ TAY" in n.upper() for n in r.notes)
        assert r.state.net_qty(SYM) == 1 and r.state.emergency_reason

    def test_both_brokers_down(self, rig):
        rig.open_long()
        rig.advance(11, alive=())                           # cả hai im
        rig.run(rig.step())
        rig.advance(5, alive=())
        rig.run(rig.step())
        assert rig.ssi.place_calls == 0
        crit = [n for n in rig.notes if "CẢ HAI BROKER" in n]
        assert len(crit) == 1                               # giới hạn tần suất cảnh báo
        rig.advance(61, alive=())
        rig.run(rig.step())
        assert len([n for n in rig.notes if "CẢ HAI BROKER" in n]) == 2
        assert rig.state.emergency_reason

    def test_async_notifier_supported(self, session_cfg, cal, tmp_path):
        r = Rig(session_cfg, cal, tmp_path)
        got = []

        async def anotify(text):
            got.append(text)
        r.emg.notify = anotify
        r.open_long()
        r.advance(11, alive=("ssi",))
        r.run(r.step())
        assert got


# ------------------------------------------------------------------ gỡ hedge ----

class TestUnwind:
    def _hedged(self, rig):
        rig.open_long()
        rig.advance(11, alive=("ssi",))
        rig.run(rig.step())
        rig.advance(1, alive=("ssi",))
        rig.tick("ssi")
        rig.run(rig.step())
        assert rig.state.net_qty(SYM) == 0

    def test_primary_back_unwinds_both_legs(self, rig):
        self._hedged(rig)
        rig.advance(1)                                      # dnse sống lại
        assert rig.emg.needs_unwind()
        assert rig.run(rig.emg.unwind()) is True
        rig.advance(1)
        rig.tick("dnse")
        rig.tick("ssi")
        rig.run(rig.step())
        assert all(p.qty == 0 for p in rig.state.positions.values())
        assert not rig.emg.needs_unwind()
        assert rig.state.emergency_reason                  # vẫn khoá tới khi người kiểm tra
        assert any("gỡ" in n for n in rig.notes)
        assert not [o for o in rig.state.working_orders.values() if o.tag == "stop"]   # stop cũ đã huỷ

    def test_direct_unwind_twice_does_not_duplicate(self, rig):
        """Runner gọi unwind() mỗi vòng khi broker chính sống lại — lần sau không gửi trùng."""
        self._hedged(rig)
        rig.advance(1)
        rig.run(rig.emg.unwind())
        first = sorted(o.client_order_id for o in rig.state.working_orders.values() if o.tag == "unwind")
        rig.run(rig.emg.unwind())
        second = sorted(o.client_order_id for o in rig.state.working_orders.values() if o.tag == "unwind")
        assert len(first) == 2 and second == first          # không huỷ-đặt lại chân đang chờ

    def test_no_unwind_while_primary_dead(self, rig):
        self._hedged(rig)
        rig.advance(1, alive=("ssi",))
        assert rig.run(rig.emg.unwind()) is False
        assert rig.state.positions["ssi:" + SYM].qty == -1

    def test_flatten_time_with_primary_dead_keeps_hedge(self, rig):
        self._hedged(rig)
        rig.clock.set(datetime(2026, 11, 9, 14, 26))
        rig.alive("ssi")
        rig.tick("ssi")
        rig.run(rig.step())
        assert rig.state.positions["ssi:" + SYM].qty == -1   # không đóng chân hedge ⇒ không lộ vị thế
        assert any("XỬ LÝ TAY" in n.upper() for n in rig.notes)

    def test_flatten_time_with_primary_alive_unwinds(self, rig):
        self._hedged(rig)
        rig.clock.set(datetime(2026, 11, 9, 14, 26))
        rig.alive("dnse", "ssi")
        rig.run(rig.step())
        rig.advance(1)
        rig.tick("dnse")
        rig.tick("ssi")
        rig.run(rig.step())
        assert all(p.qty == 0 for p in rig.state.positions.values())


    def test_repeated_flatten_while_unwind_pending_is_safe(self, rig):
        """FLATTEN_ALL phát mỗi lần check(): không được huỷ chân gỡ đang chờ hay gửi trùng."""
        self._hedged(rig)
        rig.clock.set(datetime(2026, 11, 9, 14, 26))
        rig.alive("dnse", "ssi")
        rig.run(rig.step())
        legs = sorted(o.client_order_id for o in rig.state.working_orders.values() if o.tag == "unwind")
        assert len(legs) == 2
        for _ in range(3):
            rig.advance(0.2)
            rig.run(rig.step())
        again = sorted(o.client_order_id for o in rig.state.working_orders.values() if o.tag == "unwind")
        assert again == legs                                 # cùng hai lệnh, không huỷ, không thêm
        rig.advance(1)
        rig.tick("dnse")
        rig.tick("ssi")
        rig.run(rig.step())
        assert all(p.qty == 0 for p in rig.state.positions.values())


# ------------------------------------------------------------------ risk: unwind & khoá ----

def _o(broker, side, qty=1, cid=None):
    return Order(cid or f"U{next(_ids)}", SYM, side, qty, OrderType.MARKET, T0, broker=broker, tag="unwind")


class TestRiskUnwind:
    def _state(self, rig, dnse=1, ssi=-1):
        rig.state.positions = {"dnse:" + SYM: Position(SYM, dnse, 1350.0, broker="dnse"),
                               "ssi:" + SYM: Position(SYM, ssi, 1350.0, broker="ssi")}

    def test_valid_pair_approved_even_when_locked(self, rig):
        self._state(rig)
        rig.state.emergency_reason = "x"
        rig.state.kill_reason = "y"
        ds = rig.risk.approve_unwind([_o("dnse", Side.SELL), _o("ssi", Side.BUY)], rig.state)
        assert all(d.ok for d in ds) and len(rig.state.working_orders) == 2

    def test_unbalanced_rejected(self, rig):
        self._state(rig)
        ds = rig.risk.approve_unwind([_o("dnse", Side.SELL)], rig.state)
        assert not ds[0].ok and ds[0].code is RejectCode.UNWIND_INVALID
        assert rig.state.working_orders == {}

    def test_leg_must_reduce_its_own_broker(self, rig):
        self._state(rig)
        ds = rig.risk.approve_unwind([_o("dnse", Side.BUY), _o("ssi", Side.SELL)], rig.state)
        assert not any(d.ok for d in ds)

    def test_leg_larger_than_position_rejected(self, rig):
        self._state(rig)
        ds = rig.risk.approve_unwind([_o("dnse", Side.SELL, 2), _o("ssi", Side.BUY, 2)], rig.state)
        assert not any(d.ok for d in ds)

    def test_duplicate_id_rejected(self, rig):
        self._state(rig)
        rig.state.approved_order_ids.add("DUPX")
        ds = rig.risk.approve_unwind([_o("dnse", Side.SELL, cid="DUPX"), _o("ssi", Side.BUY)], rig.state)
        assert not any(d.ok for d in ds)

    def test_emergency_lock_survives_new_day_until_cleared(self, rig):
        rig.state.emergency_reason = "dnse mất kết nối"
        rig.risk.new_day(rig.state, date(2026, 11, 10))
        assert rig.state.emergency_reason
        rig.risk.clear_emergency(rig.state)
        assert rig.state.emergency_reason is None
