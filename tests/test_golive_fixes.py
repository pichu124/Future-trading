"""Test tái hiện các lỗi trong docs/golive_checklist.md (#1–#7, #10–#12, #14) — viết TRƯỚC khi sửa.

Mỗi test mang số lỗi trong checklist.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from brokers.paper import PaperBroker, PaperConfig
from core.models import (AccountState, Fill, Order, OrderStatus, OrderType, Position, Side, Signal,
                         SignalAction, Tick)
from risk.risk_manager import RejectCode
from tests.test_emergency import SYM, T0, Rig


class Steps:
    """Một vòng như runner: drain → maintain → check → hành động."""

    def __init__(self, rig):
        self.r = rig

    async def step(self):
        r = self.r
        await r.exe.drain()
        await r.exe.maintain()
        actions = r.risk.check(r.state)
        await r.exe.handle_actions(actions)
        await r.exe.drain()
        return actions


def open_with_tp(r, qty=1, stop=1340.0, tp=1360.0):
    async def go():
        await r.exe.on_signal(Signal(SignalAction.ENTER_LONG, r.clock.now(), SYM, stop_loss=stop,
                                     take_profit=tp, qty=qty))
        r.advance(1)
        r.tick("dnse")
        await r.exe.drain()
    r.run(go())
    tags = sorted(o.tag for o in r.state.working_orders.values())
    assert r.state.net_qty(SYM) == qty and tags == ["stop", "take_profit"]


@pytest.fixture
def rig(session_cfg, cal, tmp_path):
    return Rig(session_cfg, cal, tmp_path)


# #1 ---------------------------------------------------------------------------------

def test_1_emergency_hedge_works_with_stop_and_tp_working(rig):
    open_with_tp(rig)
    rig.advance(11, alive=("ssi",))
    rig.run(Steps(rig).step())
    assert rig.ssi.place_calls == 1
    assert [o.tag for o in rig.state.working_orders.values() if o.broker == "ssi"] == ["emergency"]
    assert not [r for r in rig.exe.rejections if r.code is RejectCode.MAX_CONTRACTS]


# #2 ---------------------------------------------------------------------------------

def test_2_partial_close_remainder_is_requoted(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path, max_contracts=2)
    s = Steps(r)
    open_with_tp(r, qty=2)
    r.clock.set(datetime(2026, 11, 9, 14, 25))
    r.alive("dnse", "ssi")
    r.run(s.step())                                          # FLATTEN_ALL ⇒ huỷ stop/TP, MTL bán 2
    r.advance(1)
    r.tick("dnse", bid_qty=1)                                # chỉ khớp 1; phần dư thành LO 1349.9
    r.run(s.step())
    r.advance(1)
    r.tick("dnse", bid=1340.0, ask=1340.2)                   # giá rơi: LO bán 1349.9 không khớp
    r.run(s.step())
    assert r.state.net_qty(SYM) == 1
    r.advance(3)                                             # quá close_requote_s ⇒ huỷ, đặt lại MTL
    r.run(s.step())
    r.advance(1)
    r.tick("dnse", bid=1339.0, ask=1339.2)
    r.run(s.step())
    assert r.state.net_qty(SYM) == 0


# #3 ---------------------------------------------------------------------------------

class FailingStopBroker(PaperBroker):
    fail = True

    async def place(self, order):
        if order.tag == "stop" and self.fail:
            raise ConnectionError("timeout")
        return await super().place(order)


def test_3_network_error_on_place_does_not_crash_or_leave_ghost(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path)
    cfg = PaperConfig(tick_size=0.1, slippage_ticks=0, fee_vnd_per_contract=0, latency_ms=0)
    from core.execution import ExecutionManager
    r.dnse = FailingStopBroker(cfg, r.clock, name="dnse", session=r.mclock)
    r.exe = ExecutionManager(r.dnse, r.risk, r.state, r.clock, 0.1, emergency=r.emg)

    async def go():
        await r.exe.on_signal(Signal(SignalAction.ENTER_LONG, r.clock.now(), SYM, stop_loss=1340.0))
        r.advance(1)
        r.tick("dnse")
        await r.exe.drain()                                  # không được ném
    r.run(go())
    assert r.state.net_qty(SYM) == 1
    assert not [o for o in r.state.working_orders.values() if o.tag == "stop"]   # không có lệnh ma
    assert any(e.kind == "broker_error" for e in r.exe.events)
    r.dnse.fail = False
    r.advance(1)
    r.run(Steps(r).step())                                   # vòng sau tự đặt lại stop
    assert [o.tag for o in r.state.working_orders.values()] == ["stop"]


# #4 ---------------------------------------------------------------------------------

def test_4_unintended_flip_is_closed_not_protected(rig):
    open_with_tp(rig, tp=1351.0)
    # ba tick tới cùng lúc trước khi kịp drain: TP khớp, rồi stop kích hoạt và khớp ⇒ net −1
    rig.advance(1)
    rig.tick("dnse", bid=1351.5, ask=1351.7)
    rig.advance(1)
    rig.tick("dnse", bid=1339.0, ask=1339.2)
    rig.advance(1)
    rig.tick("dnse", bid=1338.0, ask=1338.2)
    assert rig.state.net_qty(SYM) == -1
    rig.run(Steps(rig).step())
    working = list(rig.state.working_orders.values())
    assert not [o for o in working if o.tag == "stop"]       # không đặt stop sai phía cho vị thế Short
    assert [(o.side, o.tag) for o in working] == [(Side.BUY, "flatten")]
    rig.advance(1)
    rig.tick("dnse", bid=1338.0, ask=1338.2)
    rig.run(Steps(rig).step())
    assert rig.state.net_qty(SYM) == 0


# #5 ---------------------------------------------------------------------------------

def test_5_flatten_with_dead_primary_does_not_double_close(rig):
    open_with_tp(rig)
    rig.clock.set(datetime(2026, 11, 9, 14, 26))
    rig.alive("ssi")                                         # dnse im từ 10:00:01
    rig.run(Steps(rig).step())
    assert not [o for o in rig.state.working_orders.values()
                if o.broker == "dnse" and o.tag in ("flatten", "exit")]
    rig.advance(1, alive=("ssi",))
    rig.tick("ssi")
    rig.tick("dnse")                                         # nếu đã gửi lệnh tới dnse thì nó sẽ khớp
    rig.run(Steps(rig).step())
    assert rig.state.net_qty(SYM) == 0


# #7 ---------------------------------------------------------------------------------

class SlowCancelBroker(PaperBroker):
    """Huỷ được sàn nhận nhưng xác nhận về sau (như adapter thật)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pending_confirms = []

    async def cancel(self, cid):
        o = self._live_order(cid, "huỷ")
        del self._book[cid]
        done = o.with_status(OrderStatus.CANCELLED)
        self._orders[cid] = done
        self.pending_confirms.append(done)
        return done


def test_7_flatten_not_blocked_by_unconfirmed_cancels(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path)
    cfg = PaperConfig(tick_size=0.1, slippage_ticks=0, fee_vnd_per_contract=0, latency_ms=0)
    from core.execution import ExecutionManager
    r.dnse = SlowCancelBroker(cfg, r.clock, name="dnse", session=r.mclock)
    r.exe = ExecutionManager(r.dnse, r.risk, r.state, r.clock, 0.1, emergency=r.emg)
    open_with_tp(r)
    r.clock.set(datetime(2026, 11, 9, 14, 25))
    r.alive("dnse", "ssi")
    r.run(Steps(r).step())
    assert [o.tag for o in r.state.working_orders.values() if o.tag == "flatten"] == ["flatten"]
    assert not [x for x in r.exe.rejections if x.code is RejectCode.MAX_CONTRACTS]


def test_7b_duplicate_close_on_same_broker_rejected(rig):
    rig.state.positions = {"dnse:" + SYM: Position(SYM, 1, 1350.0, broker="dnse")}
    a = Order("CL1", SYM, Side.SELL, 1, OrderType.MARKET, T0, broker="dnse", tag="flatten")
    b = Order("CL2", SYM, Side.SELL, 1, OrderType.MARKET, T0, broker="dnse", tag="exit")
    assert rig.risk.approve(a, rig.state).ok
    d = rig.risk.approve(b, rig.state)
    assert not d.ok and d.code is RejectCode.DUPLICATE_CLOSE


# #10 --------------------------------------------------------------------------------

def test_10_hedge_attempts_reset_after_success(rig):
    open_with_tp(rig)
    rig.emg._attempts[SYM] = 2                               # hai lần lỗi trước đó
    rig.advance(11, alive=("ssi",))
    rig.run(Steps(rig).step())
    rig.advance(1, alive=("ssi",))
    rig.tick("ssi")
    rig.run(Steps(rig).step())
    assert rig.state.net_qty(SYM) == 0 and rig.emg._attempts[SYM] == 0


# #11 --------------------------------------------------------------------------------

def test_11_consecutive_losses_count_fees(rig):
    rig.risk.limits = rig.risk.limits.__class__(**{**rig.risk.limits.__dict__})
    rig.state.consecutive_losses = 1
    f1 = Fill("A", SYM, Side.BUY, 1, 1350.0, T0, fee_vnd=5_000, broker="dnse")
    f2 = Fill("B", SYM, Side.SELL, 1, 1350.2, T0, fee_vnd=20_000, broker="dnse")
    rig.risk.record_fill(rig.state, f1)
    rig.risk.record_fill(rig.state, f2)        # +0.2 trước phí; −0.05 sau phí (phí đóng quyết định)
    assert rig.state.consecutive_losses == 2


# #12 --------------------------------------------------------------------------------

def test_12_runner_saves_counters_right_after_fill(session_cfg, cal, tmp_path):
    import json
    from tests.test_runner import Rig as RunnerRig
    r = RunnerRig(session_cfg, cal, tmp_path, plan={"10:00": (1340.0,)})
    r.start()
    r.open_position()                                        # khớp lúc 10:01:02, heartbeat cuối 10:01:00.6
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["trades_today"] == 1


# #4 (runner) -------------------------------------------------------------------------

def test_4_runner_drains_after_each_tick(session_cfg, cal, tmp_path):
    from tests.test_runner import Rig as RunnerRig
    r = RunnerRig(session_cfg, cal, tmp_path, plan={"10:00": (1340.0, 1351.0)})
    r.start()
    r.open_position()                                        # Long 1, stop 1340, TP 1351
    r.at(T0 + timedelta(seconds=63))
    r.trade(1352.0)                                          # TP khớp
    r.at(T0 + timedelta(seconds=64))
    r.trade(1338.0, bid=1338.0, ask=1338.2)                  # stop kích hoạt …
    r.at(T0 + timedelta(seconds=65))
    r.trade(1337.0, bid=1337.0, ask=1337.2)                  # … và khớp — nếu chưa bị huỷ
    r.step()
    assert r.state.net_qty(SYM) == 0 and r.state.trades_today == 1


def test_3_runner_survives_step_errors_then_kills(session_cfg, cal, tmp_path):
    from live.runner import RunnerConfig
    from tests.test_runner import Rig as RunnerRig
    r = RunnerRig(session_cfg, cal, tmp_path)
    r.runner.config = RunnerConfig(tick_s=0.001, heartbeat_s=30, command_poll_s=0,
                                   max_consecutive_errors=3)

    def boom(now=None):
        raise RuntimeError("lỗi lạ")
    r.feed.is_stale = boom
    assert r.run(r.runner.run()) == 1                        # không treo/chết: kill ⇒ phẳng ⇒ thoát mã lỗi
    assert r.state.kill_reason and "lỗi" in r.state.kill_reason


# #6 ---------------------------------------------------------------------------------

class _Boom:
    def __init__(self, token):
        self.token = token

    def get(self, url, **kw):
        raise ConnectionError(f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: {url}")

    post = get


def test_6_telegram_errors_never_contain_token(caplog):
    from monitoring import telegram
    token = "123456:SECRET-TOKEN"
    env = {"TELEGRAM_TRADING_BOT_TOKEN": token, "TELEGRAM_TRADING_CHAT_ID": "1"}
    with pytest.raises(Exception) as exc:
        telegram.fetch_updates(None, "trading", session=_Boom(token), env=env)
    assert token not in str(exc.value)
    with caplog.at_level(logging.DEBUG):
        assert telegram.notify("x", "trading", session=_Boom(token), env=env) is False
    assert token not in caplog.text


# #14 --------------------------------------------------------------------------------

def test_14_bars_without_reliability_column_are_unreliable(tmp_path):
    from backtest.engine import load_bars_parquet
    pd.DataFrame({"symbol": [SYM], "start": [pd.Timestamp("2026-11-09 10:00")], "open": [1350.0],
                  "high": [1351.0], "low": [1349.0], "close": [1350.5], "volume": [3]}) \
        .to_parquet(tmp_path / "x.parquet")
    bars, _ = load_bars_parquet(tmp_path / "x.parquet")
    assert bars[0].volume_reliable is False
