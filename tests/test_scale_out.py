"""Chốt lời từng phần (scale-out) — `Signal.take_profits` + ExecutionManager + luật risk. Viết TRƯỚC khi implement.

Mục đích: vào N hợp đồng, chốt từng phần ở nhiều mức giá, phần còn lại ("runner") chạy tới stop / EXIT / flatten.

Đặc tả:
- `Signal.take_profits = ((giá, KL), …)`: mỗi mức một lệnh LO `take_profit`; tổng KL các mức ≤ `qty` (phần dư = runner).
  Không dùng chung với `take_profit` (một mức, toàn bộ KL — giữ nguyên hành vi cũ). Mức nào phải ở phía LÃI so với stop
  (Long: giá > stop; Short: giá < stop), sai ⇒ không vào lệnh (sự kiện "rejected").
- Bất biến an toàn:
  * stop LUÔN đúng một lệnh, KL = |net| (đặt lại khi net đổi);
  * tổng KL các lệnh chốt lời đang chờ ≤ |net| — phân bổ theo thứ tự mức (gần trước): vào lệnh khớp một phần ⇒ chỉ đặt
    các mức đủ KL; stop khớp một phần ⇒ cắt bớt mức xa ⇒ không bao giờ đảo chiều vì chốt lời;
  * mức đã khớp xong không đặt lại; mức khớp một phần chỉ đặt phần còn lại; lệnh mức không đổi thì GIỮ (không huỷ-đặt lại).
- `breakeven_after_first_tp=True`: mức chốt lời đầu tiên khớp ⇒ dời stop về giá vốn (làm tròn bước giá).
- Risk: lệnh `take_profit` cùng chiều được nhiều lệnh nếu tổng KL chờ (gồm lệnh mới) ≤ |net|; `stop` vẫn chỉ một;
  lệnh đóng (`exit`/`flatten`…) vẫn chỉ một (golive #7b).
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.models import Order, OrderType, Position, Side, Signal, SignalAction
from risk.risk_manager import RejectCode
from tests.test_execution import Rig, run
from tests.test_risk_manager import MARK, SYM

LEGS = ((1360.0, 1), (1370.0, 1))


def enter(rig, qty=3, legs=LEGS, stop=1345.0, side=SignalAction.ENTER_LONG, breakeven=True, **kw):
    s = Signal(side, rig.env.now, SYM, stop_loss=stop, take_profits=legs, qty=qty,
               breakeven_after_first_tp=breakeven, reason="t", **kw)

    async def go():
        await rig.ex.on_signal(s)
        await rig.ex.drain()
    run(go())


def tps(rig):
    return sorted((o.price, o.qty - o.filled_qty) for o in rig.working("take_profit"))


def stop(rig):
    (s,) = rig.working("stop")
    return s.price, s.qty - s.filled_qty


@pytest.fixture
def rig3(session_cfg, cal, tmp_path):
    return Rig(session_cfg, cal, tmp_path, max_contracts=3)


# ------------------------------------------------------------------ Signal ----

@pytest.mark.parametrize("kw", [
    {"take_profits": ((1360.0, 2), (1370.0, 2)), "qty": 3},          # tổng KL mức > qty
    {"take_profits": ((1360.0, 0),), "qty": 3},                      # KL 0
    {"take_profits": ((1360.0, 1.5),), "qty": 3},                    # KL không nguyên
    {"take_profits": ((0, 1),), "qty": 3},                           # giá ≤ 0
    {"take_profits": ((1360.0, 1),), "take_profit": 1360.0, "qty": 3},  # dùng cả hai kiểu
])
def test_signal_rejects_bad_legs(kw):
    with pytest.raises(ValueError):
        Signal(SignalAction.ENTER_LONG, datetime(2026, 11, 9, 10), SYM, stop_loss=1345.0, **kw)


def test_signal_legacy_fields_still_work():
    s = Signal(SignalAction.ENTER_LONG, datetime(2026, 11, 9, 10), SYM, stop_loss=1345.0, take_profit=1360.0)
    assert s.take_profits == () and s.breakeven_after_first_tp is False


# ------------------------------------------------------------------ thực thi ----

def test_full_entry_one_stop_for_all_and_one_order_per_leg(rig3):
    enter(rig3)
    rig3.fill(rig3.broker.last("entry").client_order_id)
    assert rig3.net == 3 and stop(rig3) == (1345.0, 3)
    assert tps(rig3) == [(1360.0, 1), (1370.0, 1)]


def test_first_leg_fill_shrinks_stop_moves_to_breakeven_keeps_second_leg(rig3):
    enter(rig3)
    rig3.fill(rig3.broker.last("entry").client_order_id, price=MARK)
    leg2 = next(o for o in rig3.working("take_profit") if o.price == 1370.0).client_order_id
    leg1 = next(o for o in rig3.working("take_profit") if o.price == 1360.0).client_order_id
    rig3.fill(leg1, price=1360.0)
    assert rig3.net == 2 and stop(rig3) == (MARK, 2)                  # hoà vốn
    assert tps(rig3) == [(1370.0, 1)]
    assert leg2 not in rig3.broker.cancelled                           # mức 2 không bị huỷ-đặt lại


def test_without_breakeven_stop_price_unchanged(rig3):
    enter(rig3, breakeven=False)
    rig3.fill(rig3.broker.last("entry").client_order_id)
    leg1 = next(o for o in rig3.working("take_profit") if o.price == 1360.0).client_order_id
    rig3.fill(leg1, price=1360.0)
    assert stop(rig3) == (1345.0, 2)


def test_both_legs_then_runner_only_stop(rig3):
    enter(rig3)
    rig3.fill(rig3.broker.last("entry").client_order_id)
    for px in (1360.0, 1370.0):
        cid = next(o for o in rig3.working("take_profit") if o.price == px).client_order_id
        rig3.fill(cid, price=px)
    assert rig3.net == 1 and stop(rig3) == (MARK, 1) and tps(rig3) == []
    rig3.maintain()
    assert tps(rig3) == []                                             # mức đã xong không đặt lại


def test_partial_entry_places_only_affordable_legs_then_tops_up(rig3):
    enter(rig3)
    cid = rig3.broker.last("entry").client_order_id
    rig3.fill(cid, 1)
    assert stop(rig3) == (1345.0, 1) and tps(rig3) == [(1360.0, 1)]
    leg1 = rig3.working("take_profit")[0].client_order_id
    rig3.fill(cid, 2)
    assert stop(rig3) == (1345.0, 3) and tps(rig3) == [(1360.0, 1), (1370.0, 1)]
    assert leg1 not in rig3.broker.cancelled                           # mức 1 giữ nguyên
    # ExecutionManager TỰ giữ tổng TP ≤ |net| — không gửi lệnh thừa để risk chặn hộ
    assert not any(x.code is RejectCode.DUPLICATE_CLOSE for x in rig3.ex.rejections)


def test_partial_leg_fill_keeps_remainder(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path, max_contracts=3)
    enter(r, legs=((1360.0, 2),))
    r.fill(r.broker.last("entry").client_order_id)
    leg = r.working("take_profit")[0].client_order_id
    r.fill(leg, 1, price=1360.0)
    assert r.net == 2 and tps(r) == [(1360.0, 1)] and leg not in r.broker.cancelled
    assert stop(r) == (MARK, 2)                                        # khớp một phần cũng là "đã chốt" ⇒ hoà vốn


def test_stop_partial_fill_cuts_far_legs_never_flips(rig3):
    enter(rig3)
    rig3.fill(rig3.broker.last("entry").client_order_id)
    s = rig3.working("stop")[0].client_order_id
    rig3.fill(s, 2, price=1345.0)                                      # stop khớp 2/3 ⇒ net 1
    assert rig3.net == 1 and stop(rig3) == (1345.0, 1)
    assert tps(rig3) == [(1360.0, 1)]                                  # mức xa bị cắt: tổng TP ≤ |net|
    assert not any(x.code is RejectCode.DUPLICATE_CLOSE for x in rig3.ex.rejections)


def test_short_legs_mirror(rig3):
    enter(rig3, side=SignalAction.ENTER_SHORT, stop=1355.0, legs=((1340.0, 1), (1330.0, 1)))
    rig3.fill(rig3.broker.last("entry").client_order_id)
    assert rig3.net == -3 and stop(rig3) == (1355.0, 3)
    assert tps(rig3) == [(1330.0, 1), (1340.0, 1)]
    assert all(o.side is Side.BUY for o in rig3.working("take_profit"))


def test_leg_on_wrong_side_of_stop_no_entry(rig3):
    enter(rig3, legs=((1340.0, 1),), stop=1345.0)                     # Long mà chốt lời dưới stop
    assert [o for o in rig3.broker.placed if o.tag == "entry"] == []
    assert any(e.kind == "rejected" and "chốt lời" in e.message for e in rig3.ex.events)


def test_exit_cancels_every_leg_and_stop(rig3):
    enter(rig3)
    rig3.fill(rig3.broker.last("entry").client_order_id)
    rig3.signal(SignalAction.EXIT)
    assert rig3.working("take_profit") == [] and rig3.working("stop") == []
    assert rig3.broker.last("exit").qty == 3


def test_legacy_single_take_profit_behaviour_kept(rig3):
    rig3.signal(SignalAction.ENTER_LONG, stop=1345.0, tp=1360.0, qty=2)
    rig3.fill(rig3.broker.last("entry").client_order_id)
    assert tps(rig3) == [(1360.0, 2)] and stop(rig3) == (1345.0, 2)


# ------------------------------------------------------------------ risk ----

def test_risk_allows_tp_legs_up_to_net_only(rig3):
    env = rig3.env
    env.state.positions = {"dnse:" + SYM: Position(SYM, 3, MARK, broker="dnse")}

    def tp(cid, qty, px):
        return Order(cid, SYM, Side.SELL, qty, OrderType.LIMIT, env.now, price=px, broker="dnse", tag="take_profit")
    assert env.rm.approve(tp("TP1", 1, 1360.0), env.state).ok
    assert env.rm.approve(tp("TP2", 2, 1370.0), env.state).ok           # tổng 3 = net
    d = env.rm.approve(tp("TP3", 1, 1380.0), env.state)                 # tổng 4 > net
    assert not d.ok and d.code is RejectCode.DUPLICATE_CLOSE


def test_risk_still_single_stop(rig3):
    env = rig3.env
    env.state.positions = {"dnse:" + SYM: Position(SYM, 3, MARK, broker="dnse")}

    def st(cid, qty):
        return Order(cid, SYM, Side.SELL, qty, OrderType.STOP, env.now, price=1345.0, broker="dnse", tag="stop")
    assert env.rm.approve(st("S1", 1), env.state).ok
    assert not env.rm.approve(st("S2", 1), env.state).ok


def test_risk_single_contract_second_tp_still_rejected(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path)
    env = r.env
    env.state.positions = {"dnse:" + SYM: Position(SYM, 1, MARK, broker="dnse")}
    a = Order("A", SYM, Side.SELL, 1, OrderType.LIMIT, env.now, price=1360.0, broker="dnse", tag="take_profit")
    b = Order("B", SYM, Side.SELL, 1, OrderType.LIMIT, env.now, price=1370.0, broker="dnse", tag="take_profit")
    assert env.rm.approve(a, env.state).ok and not env.rm.approve(b, env.state).ok


# ------------------------------------------------------------------ backtest ----

class ScaleOutOnce:
    """Vào Long 3 HĐ ở nến 10:00, chốt 1 ở +10, 1 ở +20, runner thoát nến 14:19."""
    history_days = 0

    def __init__(self):
        self.done = set()

    def on_bar(self, bar, ctx):
        d = bar.start.date()
        hm = bar.start.strftime("%H:%M")
        if hm == "10:00" and d not in self.done and ctx.position_qty == 0:
            self.done.add(d)
            return [Signal(SignalAction.ENTER_LONG, bar.start + timedelta(minutes=1), bar.symbol, stop_loss=bar.close - 10,
                           take_profits=((bar.close + 10, 1), (bar.close + 20, 1)), qty=3,
                           breakeven_after_first_tp=True, reason="scale-out")]
        if hm == "14:19" and ctx.position_qty != 0:
            return [Signal(SignalAction.EXIT, bar.start + timedelta(minutes=1), bar.symbol, reason="hết giờ")]
        return []


def test_engine_scale_out_round_trip(session_cfg, cal):
    from backtest.engine import BacktestEngine
    from core.models import Bar
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg, max_contracts=3, max_trades_per_day=5, max_daily_loss_points=100)
    d = date(2026, 11, 4)

    def bar(hm, o, h, l, c):
        return Bar(SYM, datetime.combine(d, time.fromisoformat(hm)), o, h, l, c, 10)
    bars = []                                    # nến LIÊN TỤC từng phút (thủng > 30s khi có vị thế ⇒ risk đóng hết)
    for start, end in ((time(10, 0), time(11, 30)), (time(13, 0), time(14, 21))):
        t = datetime.combine(d, start)
        while t.time() < end:
            hm = t.strftime("%H:%M")
            if hm == "10:30":
                bars.append(bar(hm, 1350, 1362, 1350, 1361))          # chạm mức 1 (1360)
            elif hm == "11:00":
                bars.append(bar(hm, 1361, 1372, 1360, 1371))          # chạm mức 2 (1370)
            else:
                px = 1350 if t.time() < time(10, 30) else 1361 if t.time() < time(11, 0) else 1371
                bars.append(bar(hm, px, px, px, px))
            t += timedelta(minutes=1)
    res = BacktestEngine(s, ScaleOutOnce(), calendar=cal).run_bars(bars)
    (tr,) = res.trades
    assert tr.qty == 3 and tr.side == "LONG" and tr.exit_reason == "exit"
    assert len(res.fills) == 4                                          # vào 3, chốt 1, chốt 1, thoát 1
    sells = sorted(f.price for f in res.fills if f.side is Side.SELL)
    assert sells[:2] == [1360.0, 1370.0]                               # hai mức chốt lời khớp đúng giá LO
    assert tr.exit_price == pytest.approx((1360.0 + 1370.0 + sells[2]) / 3)
    assert any("giá vốn" in e.message for e in res.events)             # đã dời stop về hoà vốn


def test_leg_bigger_than_partial_position_is_trimmed(rig3):
    """Mức 1 KL 2 nhưng mới khớp 1 HĐ ⇒ TP mức 1 chỉ 1 HĐ (cắt trong mức, không chỉ bỏ mức sau)."""
    enter(rig3, legs=((1360.0, 2), (1370.0, 1)))
    rig3.fill(rig3.broker.last("entry").client_order_id, 1)
    assert tps(rig3) == [(1360.0, 1)] and stop(rig3) == (1345.0, 1)
    assert not any(x.code is RejectCode.DUPLICATE_CLOSE for x in rig3.ex.rejections)
