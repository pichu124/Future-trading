"""Lượt đóng bằng lệnh đối ứng khẩn cấp LUÔN tính là THUA (chuỗi thua) — viết TRƯỚC khi implement.

Quyết định của người dùng: một lượt kết thúc nhờ lệnh đối ứng khẩn cấp ở broker dự phòng (broker chính mất
kết nối) tính là MỘT lượt thua, bất kể P&L sau phí (P&L thật chưa chốt được: hai chân còn mở trên hai broker).
- nhận ra qua tag lệnh `emergency`, hoặc dự phòng theo cấu trúc: net (mọi broker) về 0 trong khi broker của
  lệnh khớp vẫn còn vị thế (đóng chéo broker);
- gỡ hedge (`unwind`, đóng đồng thời hai chân) KHÔNG mở/kết thúc lượt mới, KHÔNG tăng `trades_today`, KHÔNG
  đổi chuỗi thua — cả đợt khẩn cấp = đúng một lượt thua. Lãi/lỗ thực hiện của các chân vẫn vào lãi/lỗ ngày.
"""
from datetime import timedelta

import pytest

from core.models import Fill, OrderType, Side
from tests.test_risk_manager import MARK, SYM, env  # noqa: F401  (fixture)


def hedge(env, side=Side.SELL, qty=1, price=MARK, cid="H1", fee=0.0, tag="emergency"):
    o = env.order(side, qty, stop=None, broker="ssi", tag=tag, cid=cid)
    if tag is not None:
        env.state.working_orders[cid] = o                    # như risk.approve đã đăng ký
    return env.rm.record_fill(env.state, Fill(cid, SYM, side, qty, price, env.now, fee_vnd=fee, broker="ssi"))


def unwind_legs(env, price=MARK):
    from live.emergency import UNWIND_TAG
    from core.models import Order
    orders = [Order("U1", SYM, Side.SELL, 1, OrderType.MARKET, env.now, broker="dnse", tag=UNWIND_TAG),
              Order("U2", SYM, Side.BUY, 1, OrderType.MARKET, env.now, broker="ssi", tag=UNWIND_TAG)]
    assert all(d.ok for d in env.rm.approve_unwind(orders, env.state))
    for o in orders:
        env.rm.record_fill(env.state, Fill(o.client_order_id, SYM, o.side, 1, price, env.now, broker=o.broker))


def test_emergency_close_counts_as_loss_even_if_price_better(env):
    env.open_position(Side.BUY, price=1350.0, protect=False)
    hedge(env, price=1360.0)                                  # "lãi" trên giấy, không phí
    assert env.state.net_qty(SYM) == 0
    assert env.state.consecutive_losses == 1


def test_emergency_close_after_wins_breaks_nothing_else(env):
    env.state.consecutive_losses = 0
    env.open_position(Side.BUY, price=1350.0, protect=False)
    hedge(env, price=1350.0)
    assert env.state.consecutive_losses == 1 and env.state.trades_today == 1


def test_cross_broker_close_without_tag_also_loss(env):
    env.open_position(Side.SELL, price=1350.0, protect=False)
    hedge(env, side=Side.BUY, price=1340.0, tag=None)         # lệnh lạ, không có trong working_orders
    assert env.state.consecutive_losses == 1


def test_counted_once_not_twice_when_also_losing(env):
    env.open_position(Side.BUY, price=1350.0, protect=False)
    hedge(env, price=1340.0, fee=50_000)                      # vừa đối ứng vừa lỗ sau phí ⇒ vẫn +1
    assert env.state.consecutive_losses == 1


def test_unwind_does_not_open_or_close_a_round_trip(env):
    env.open_position(Side.BUY, price=1350.0, protect=False)
    hedge(env, price=1345.0)
    n_trades, streak = env.state.trades_today, env.state.consecutive_losses
    unwind_legs(env, price=1355.0)                            # chân dnse "lãi", chân ssi "lỗ"
    assert env.state.trades_today == n_trades == 1
    assert env.state.consecutive_losses == streak == 1
    assert env.state.net_qty(SYM) == 0
    assert all(p.qty == 0 for p in env.state.positions.values())
    assert SYM not in env.state.round_trip_points


def test_unwind_realized_pnl_still_counts_in_daily_pnl(env):
    env.open_position(Side.BUY, price=1350.0, protect=False)
    hedge(env, price=1345.0)                                  # khoá lỗ 5 điểm
    unwind_legs(env, price=1355.0)                            # dnse +5, ssi −10 ⇒ tổng −5
    assert env.rm.daily_pnl_points(env.state) == pytest.approx(-5.0)


def test_untagged_first_unwind_leg_from_hedged_state_not_new_round_trip(env):
    """Phòng xa: chân gỡ đầu tiên mất tag (vd lệnh đã bị xoá khỏi sổ) vẫn không được đếm là lượt mới."""
    env.open_position(Side.BUY, price=1350.0, protect=False)
    hedge(env, price=1345.0)
    env.rm.record_fill(env.state, Fill("X1", SYM, Side.SELL, 1, 1355.0, env.now, broker="dnse"))
    assert env.state.trades_today == 1


def test_normal_round_trips_unchanged(env):
    env.open_position(Side.BUY, price=1350.0, protect=False)
    env.fill(Side.SELL, price=1349.0)
    assert env.state.consecutive_losses == 1
    env.open_position(Side.BUY, price=1350.0, protect=False)
    env.fill(Side.SELL, price=1352.0)
    assert env.state.consecutive_losses == 0
    env.open_position(Side.BUY, price=1350.0, protect=False)
    env.fill(Side.SELL, price=1350.0)                          # hoà, không phí ⇒ giữ nguyên
    assert env.state.consecutive_losses == 0 and env.state.trades_today == 3


def test_same_broker_flip_is_not_cross_broker(env):
    env.open_position(Side.BUY, price=1350.0, protect=False)
    env.fill(Side.SELL, qty=2, price=1352.0)                   # đảo chiều trên CÙNG broker, lãi
    assert env.state.consecutive_losses == 0 and env.state.net_qty(SYM) == -1


def test_streak_blocks_entries_after_emergency_cleared(env):
    """Sau khi người dùng clear_emergency, lượt khẩn cấp vẫn tính vào max_consecutive_losses."""
    from risk.risk_manager import RejectCode
    from tests.test_risk_manager import assert_rejected
    env.state.consecutive_losses = env.rm.limits.max_consecutive_losses - 1
    env.open_position(Side.BUY, price=1350.0, protect=False)
    hedge(env, price=1360.0)
    unwind_legs(env)
    env.rm.clear_emergency(env.state)
    env.at(env.now + timedelta(minutes=1))
    assert_rejected(env.approve(env.order()), RejectCode.CONSECUTIVE_LOSSES)


def test_emergency_tag_alone_counts_even_on_same_broker(env):
    """Lệnh mang tag `emergency` kết thúc lượt trên CHÍNH broker giữ vị thế (không chéo) ⇒ vẫn là thua."""
    env.open_position(Side.BUY, price=1350.0, protect=False, broker="ssi")
    hedge(env, price=1360.0)                                  # chân ssi đóng hẳn ⇒ không phải đóng chéo
    assert env.state.net_qty(SYM) == 0 and env.state.consecutive_losses == 1


def test_both_unwind_legs_untagged_still_no_ghost_round_trip(env):
    """Cả hai chân gỡ đều mất tag: risk nhớ HĐ đang gỡ dở (state.unwinding) ⇒ chân thứ hai không kết thúc lượt."""
    env.open_position(Side.BUY, price=1350.0, protect=False)
    hedge(env, price=1345.0)
    streak = env.state.consecutive_losses
    env.rm.record_fill(env.state, Fill("X1", SYM, Side.SELL, 1, 1340.0, env.now, broker="dnse"))   # chân 1 lỗ
    assert SYM in env.state.unwinding
    env.rm.record_fill(env.state, Fill("X2", SYM, Side.BUY, 1, 1340.0, env.now, broker="ssi"))     # chân 2
    assert env.state.trades_today == 1 and env.state.consecutive_losses == streak
    assert SYM not in env.state.unwinding                         # mọi chân phẳng ⇒ hết trạng thái gỡ


def test_new_round_trip_after_unwind_counts_normally(env):
    env.open_position(Side.BUY, price=1350.0, protect=False)
    hedge(env, price=1345.0)
    unwind_legs(env)
    env.open_position(Side.BUY, price=1350.0, protect=False)
    env.fill(Side.SELL, price=1352.0)                             # lượt mới, lãi ⇒ đặt lại chuỗi
    assert env.state.trades_today == 2 and env.state.consecutive_losses == 0


def test_new_day_keeps_unwinding_only_for_open_legs(env):
    from datetime import date
    env.state.unwinding = {SYM, "VN30F2612"}                      # 2612 đã phẳng, SYM còn chân mở
    env.open_position(Side.BUY, price=1350.0, protect=False)
    env.rm.new_day(env.state, date(2026, 11, 10))
    assert env.state.unwinding == {SYM}
