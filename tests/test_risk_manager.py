"""Test cho risk/risk_manager.py — mỗi luật ở CLAUDE.md mục 5 có ít nhất một lớp test, gồm
cả trường hợp biên. Viết TRƯỚC khi implement (PROMPTS P2.1).

Quy ước test: ngày 09/11/2026 (thứ Hai, không phải ngày đáo hạn), hợp đồng VN30F2611,
broker chính dnse, dự phòng ssi. Giờ do FakeClock điều khiển.
"""
from datetime import date, datetime, timedelta
from itertools import count

import pytest

from core.clock import FakeClock, MarketClock, SessionSchedule
from core.models import AccountState, Fill, Order, OrderStatus, OrderType, Position, Side
from core.settings import MissingSetting, Settings
from risk.risk_manager import (ActionKind, Approved, OrderIdFactory, RejectCode, Rejected,
                               RiskLimits, RiskManager)

SYM = "VN30F2611"
DAY = date(2026, 11, 9)
EXPIRY = date(2026, 11, 19)          # thứ Năm tuần 3 tháng 11/2026
MARK = 1350.0
_ids = count()


def risk_cfg(**over) -> dict:
    cfg = {
        "max_contracts": 1,
        "max_daily_loss_points": 20,
        "max_trades_per_day": 3,
        "max_consecutive_losses": 2,
        "no_entry_open_minutes": 15,
        "last_entry_time": "14:15",
        "flatten_time": "14:25",
        "trade_on_expiry_day": False,
        "broker_timeout_seconds": 10,
        "stop_grace_seconds": 5,
    }
    cfg.update(over)
    return cfg


def make_settings(session_cfg, **risk_over) -> Settings:
    return Settings({
        "contract": {"multiplier_vnd_per_point": 100000, "tick_size_points": 0.1,
                     "roll_days_before_expiry": 1},
        "session": session_cfg,
        "risk": risk_cfg(**risk_over),
        "brokers": {"primary": "dnse", "backup": "ssi"},
    })


class Env:
    """Gói RiskManager + đồng hồ giả + state cho từng test."""

    def __init__(self, session_cfg, cal, tmp_path, start=datetime(2026, 11, 9, 10, 0), **risk_over):
        self.clock = FakeClock(start)
        self.mclock = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, self.clock)
        self.kill_file = tmp_path / "KILL"
        self.rm = RiskManager.from_settings(make_settings(session_cfg, **risk_over), self.mclock,
                                            kill_file=self.kill_file)
        self.state = AccountState(trading_day=start.date(), last_prices={SYM: MARK})
        self.touch_brokers()

    @property
    def now(self) -> datetime:
        return self.clock.now()

    def at(self, ts: datetime, touch: bool = True) -> None:
        self.clock.set(ts)
        if ts.date() != self.state.trading_day:
            self.rm.new_day(self.state, ts.date())
        if touch:
            self.touch_brokers()

    def touch_brokers(self, *brokers: str) -> None:
        for b in brokers or ("dnse", "ssi"):
            self.state.broker_last_seen[b] = self.now

    def order(self, side=Side.BUY, qty=1, stop="auto", broker="dnse", symbol=SYM, tag="entry",
              otype=OrderType.MARKET, price=None, status=OrderStatus.NEW, cid=None) -> Order:
        if stop == "auto":
            stop = MARK - 5 if side is Side.BUY else MARK + 5
        return Order(cid or f"T{next(_ids)}", symbol, side, qty, otype, self.now, price=price,
                     status=status, broker=broker, tag=tag, stop_loss=stop)

    def fill(self, side, qty=1, price=MARK, broker="dnse", symbol=SYM, fee_vnd=0.0, cid="F"):
        return self.rm.record_fill(self.state, Fill(cid, symbol, side, qty, price, self.now,
                                                    fee_vnd=fee_vnd, broker=broker))

    def open_position(self, side=Side.BUY, qty=1, broker="dnse", price=MARK, protect=True):
        """Mở vị thế qua record_fill và (tuỳ chọn) đặt stop đã được broker xác nhận."""
        self.fill(side, qty, price, broker)
        if protect:
            self.add_working(self.order(side.opposite(), qty, stop=None, broker=broker,
                                        tag="stop", status=OrderStatus.PENDING))

    def add_working(self, o: Order) -> None:
        """Runner báo trạng thái lệnh (vd broker xác nhận stop) qua on_order_update."""
        self.rm.on_order_update(self.state, o)

    def approve(self, o: Order):
        return self.rm.approve(o, self.state)

    def actions(self):
        return self.rm.check(self.state)


@pytest.fixture
def env(session_cfg, cal, tmp_path):
    return Env(session_cfg, cal, tmp_path)


@pytest.fixture
def make_env(session_cfg, cal, tmp_path):
    def _make(**kw):
        return Env(session_cfg, cal, tmp_path, **kw)
    return _make


def assert_rejected(decision, code: RejectCode):
    assert isinstance(decision, Rejected), f"đáng lẽ bị từ chối ({code}), nhận {decision}"
    assert decision.code is code, f"{decision.code} ≠ {code}: {decision.reason}"
    assert decision.reason            # lý do rõ ràng để log
    assert not decision.ok


def assert_approved(decision):
    assert isinstance(decision, Approved), f"đáng lẽ được duyệt, nhận {decision}"
    assert decision.ok


def kinds(actions) -> list[ActionKind]:
    return [a.kind for a in actions]


# ---------------------------------------------------------------- cấu hình ----

class TestConfig:
    def test_repo_settings_with_null_risk_values_refuse_to_start(self, cal):
        """settings.yaml hiện còn null ở max_daily_loss_points… ⇒ risk manager không được chạy."""
        from core.settings import load_settings
        mclock = MarketClock.from_settings(load_settings(), calendar=cal)
        with pytest.raises(MissingSetting):
            RiskManager.from_settings(load_settings(), mclock)

    @pytest.mark.parametrize("key", ["max_contracts", "max_daily_loss_points", "max_trades_per_day",
                                     "max_consecutive_losses", "no_entry_open_minutes",
                                     "last_entry_time", "flatten_time", "trade_on_expiry_day",
                                     "broker_timeout_seconds", "stop_grace_seconds"])
    def test_every_rule_key_is_required(self, session_cfg, key):
        with pytest.raises(MissingSetting):
            RiskLimits.from_settings(make_settings(session_cfg, **{key: None}))

    def test_primary_broker_required(self, session_cfg):
        s = make_settings(session_cfg)
        s.raw["brokers"]["primary"] = None
        with pytest.raises(MissingSetting):
            RiskLimits.from_settings(s)

    @pytest.mark.parametrize("over", [
        {"max_contracts": 0}, {"max_daily_loss_points": 0}, {"max_trades_per_day": 0},
        {"max_consecutive_losses": -1}, {"no_entry_open_minutes": -1},
        {"broker_timeout_seconds": 0}, {"stop_grace_seconds": -1},
        {"trade_on_expiry_day": "no"},
        {"last_entry_time": "14:25"},                  # phải TRƯỚC flatten_time
        {"flatten_time": "14:30"},                     # phải TRƯỚC ATC
    ])
    def test_invalid_values_rejected(self, session_cfg, over):
        with pytest.raises(ValueError):
            RiskLimits.from_settings(make_settings(session_cfg, **over))


# ------------------------------------------------------ luật: max_contracts ----

class TestMaxContracts:
    def test_entry_up_to_max_is_approved(self, env):
        assert_approved(env.approve(env.order(Side.BUY, 1)))

    def test_entry_above_max_rejected(self, env):
        assert_rejected(env.approve(env.order(Side.BUY, 2)), RejectCode.MAX_CONTRACTS)

    def test_boundary_with_max_two(self, make_env):
        e = make_env(max_contracts=2)
        e.open_position(Side.BUY, 1)
        assert_approved(e.approve(e.order(Side.BUY, 1)))               # 1 + 1 = 2 = max
        e2 = make_env(max_contracts=2)
        e2.open_position(Side.BUY, 2)
        assert_rejected(e2.approve(e2.order(Side.BUY, 1)), RejectCode.MAX_CONTRACTS)

    def test_net_position_counted_across_brokers(self, make_env):
        """Long 1 ở dnse + Long 1 ở ssi = net 2 ⇒ thêm lệnh mở bị chặn dù mỗi broker < max."""
        e = make_env(max_contracts=2)
        e.open_position(Side.BUY, 1, broker="dnse")
        e.open_position(Side.BUY, 1, broker="ssi")
        assert e.state.net_qty(SYM) == 2
        assert_rejected(e.approve(e.order(Side.BUY, 1)), RejectCode.MAX_CONTRACTS)

    def test_offsetting_positions_on_two_brokers_are_net_zero(self, make_env):
        e = make_env(max_contracts=1)
        e.fill(Side.BUY, 1, broker="dnse")
        e.fill(Side.SELL, 1, broker="ssi")                             # đối ứng khẩn cấp
        assert e.state.net_qty(SYM) == 0
        assert_approved(e.approve(e.order(Side.BUY, 1)))

    def test_working_orders_counted_as_worst_case(self, make_env):
        """Lệnh chờ cùng chiều tính như đã khớp (tình huống xấu nhất)."""
        e = make_env(max_contracts=2)
        e.add_working(e.order(Side.BUY, 1, cid="W1", status=OrderStatus.PENDING))
        assert_rejected(e.approve(e.order(Side.BUY, 2)), RejectCode.MAX_CONTRACTS)

    def test_reversal_beyond_max_rejected(self, env):
        env.open_position(Side.BUY, 1, protect=False)
        assert_rejected(env.approve(env.order(Side.SELL, 3)), RejectCode.MAX_CONTRACTS)

    def test_reducing_order_always_fits(self, env):
        env.open_position(Side.BUY, 1, protect=False)
        assert_approved(env.approve(env.order(Side.SELL, 1, stop=None, tag="exit")))


# ------------------------------------------------ luật: max_daily_loss_points ----

class TestDailyLoss:
    def test_realized_loss_at_threshold_blocks_entry_and_locks(self, env):
        env.state.realized_points_today = -20.0                        # đúng ngưỡng = chạm
        assert_rejected(env.approve(env.order()), RejectCode.DAILY_LOSS)
        assert env.state.locked_for_day

    def test_loss_just_above_threshold_still_allowed(self, env):
        env.state.realized_points_today = -19.9
        assert_approved(env.approve(env.order()))

    def test_unrealized_and_fees_count(self, env):
        """Lỗ chưa chốt + phí (quy ra điểm) cũng tính vào ngưỡng."""
        env.fill(Side.BUY, 1, price=1365.0, fee_vnd=500_000)          # phí 5 điểm
        env.state.last_prices[SYM] = 1350.0                            # lỗ chưa chốt 15 điểm
        assert env.rm.daily_pnl_points(env.state) == pytest.approx(-20.0)
        assert ActionKind.FLATTEN_ALL in kinds(env.actions())
        assert env.state.locked_for_day

    def test_check_flattens_and_locks_on_breach(self, env):
        env.open_position(Side.BUY, 1)
        env.state.last_prices[SYM] = MARK - 20
        acts = env.actions()
        assert kinds(acts) == [ActionKind.FLATTEN_ALL]
        assert "lỗ" in acts[0].reason.lower()
        assert env.state.locked_for_day

    def test_no_action_before_threshold(self, env):
        env.open_position(Side.BUY, 1)
        env.state.last_prices[SYM] = MARK - 19.9
        assert env.actions() == []
        assert not env.state.locked_for_day

    def test_locked_day_rejects_entries_but_allows_exits(self, env):
        env.open_position(Side.BUY, 1, protect=False)
        env.state.locked_for_day = True
        assert_rejected(env.approve(env.order(Side.BUY, 1)), RejectCode.LOCKED_FOR_DAY)
        assert_approved(env.approve(env.order(Side.SELL, 1, stop=None, tag="flatten")))

    def test_lock_lasts_until_next_day(self, env):
        env.state.realized_points_today = -25.0
        env.approve(env.order())
        env.at(datetime(2026, 11, 9, 13, 30))
        assert_rejected(env.approve(env.order()), RejectCode.LOCKED_FOR_DAY)
        env.at(datetime(2026, 11, 10, 10, 0))                          # sang ngày mới
        assert not env.state.locked_for_day
        assert env.state.realized_points_today == 0.0
        assert_approved(env.approve(env.order()))

    def test_entry_rejected_without_mark_for_open_position(self, make_env):
        e = make_env(max_contracts=2)
        e.open_position(Side.BUY, 1)
        del e.state.last_prices[SYM]
        assert_rejected(e.approve(e.order(Side.BUY, 1, otype=OrderType.LIMIT, price=1350.0)),
                        RejectCode.NO_MARK_PRICE)


# --------------------------------------------------- luật: max_trades_per_day ----

class TestMaxTradesPerDay:
    def test_below_max_allowed(self, env):
        env.state.trades_today = 2
        assert_approved(env.approve(env.order()))

    def test_at_max_rejected(self, env):
        env.state.trades_today = 3
        assert_rejected(env.approve(env.order()), RejectCode.MAX_TRADES)

    def test_trade_counted_once_when_net_leaves_flat(self, make_env):
        e = make_env(max_contracts=2)
        e.fill(Side.BUY, 1)
        e.fill(Side.BUY, 1)                       # cộng thêm: cùng lượt giao dịch
        assert e.state.trades_today == 1
        e.fill(Side.SELL, 2)                      # đóng
        e.fill(Side.SELL, 1)                      # mở short: lượt thứ hai
        assert e.state.trades_today == 2

    def test_reversal_in_one_fill_counts_new_trade(self, make_env):
        e = make_env(max_contracts=2)
        e.fill(Side.BUY, 1)
        e.fill(Side.SELL, 2)                      # đóng long + mở short
        assert e.state.trades_today == 2


# ----------------------------------------------- luật: max_consecutive_losses ----

class TestConsecutiveLosses:
    def test_below_max_allowed(self, env):
        env.state.consecutive_losses = 1
        assert_approved(env.approve(env.order()))

    def test_at_max_rejected(self, env):
        env.state.consecutive_losses = 2
        assert_rejected(env.approve(env.order()), RejectCode.CONSECUTIVE_LOSSES)

    def test_round_trips_update_counter(self, env):
        env.fill(Side.BUY, 1, price=1350.0)
        env.fill(Side.SELL, 1, price=1348.0)      # lỗ
        assert env.state.consecutive_losses == 1
        env.fill(Side.SELL, 1, price=1350.0)
        env.fill(Side.BUY, 1, price=1351.0)       # lỗ
        assert env.state.consecutive_losses == 2
        env.fill(Side.BUY, 1, price=1350.0)
        env.fill(Side.SELL, 1, price=1350.0)      # hoà: không đổi bộ đếm
        assert env.state.consecutive_losses == 2
        env.fill(Side.BUY, 1, price=1350.0)
        env.fill(Side.SELL, 1, price=1352.0)      # lãi: về 0
        assert env.state.consecutive_losses == 0

    def test_round_trip_closed_on_backup_broker_counts(self, env):
        """Đóng bằng lệnh đối ứng ở broker dự phòng vẫn là kết thúc một lượt (net về 0)."""
        env.fill(Side.BUY, 1, price=1350.0, broker="dnse")
        env.fill(Side.SELL, 1, price=1340.0, broker="ssi")
        assert env.state.net_qty(SYM) == 0
        # P&L lượt này chưa thực hiện trên từng broker (mỗi bên còn 1 vị thế) ⇒ chưa tính lỗ
        assert env.state.trades_today == 1


# --------------------------------------------------------- luật: thời gian ----

class TestEntryTimeWindows:
    @pytest.mark.parametrize("ts,ok", [
        (datetime(2026, 11, 9, 9, 0), False),
        (datetime(2026, 11, 9, 9, 14, 59), False),
        (datetime(2026, 11, 9, 9, 15), True),          # đúng N phút ⇒ được vào
        (datetime(2026, 11, 9, 14, 14, 59), True),
        (datetime(2026, 11, 9, 14, 15), False),        # từ last_entry_time trở đi ⇒ cấm
    ])
    def test_open_minutes_and_last_entry_boundaries(self, env, ts, ok):
        env.at(ts)
        d = env.approve(env.order())
        if ok:
            assert_approved(d)
        else:
            assert_rejected(d, RejectCode.OPENING_WINDOW if ts.hour == 9 else RejectCode.AFTER_LAST_ENTRY)

    @pytest.mark.parametrize("ts", [
        datetime(2026, 11, 9, 8, 50),                   # ATO
        datetime(2026, 11, 9, 12, 0),                   # nghỉ trưa
        datetime(2026, 11, 9, 14, 35),                  # ATC
        datetime(2026, 11, 9, 15, 0),                   # đóng cửa
        datetime(2026, 11, 7, 10, 0),                   # thứ Bảy
    ])
    def test_entries_only_in_continuous_session(self, env, ts):
        env.at(ts)
        assert_rejected(env.approve(env.order()), RejectCode.SESSION_CLOSED)

    def test_no_opening_window_after_lunch(self, env):
        env.at(datetime(2026, 11, 9, 13, 0))
        assert_approved(env.approve(env.order()))

    def test_expiry_day_blocked_by_default(self, env):
        env.at(datetime(2026, 11, 19, 10, 0))
        assert_rejected(env.approve(env.order()), RejectCode.EXPIRY_DAY)

    def test_expiry_day_allowed_with_flag(self, make_env):
        e = make_env(trade_on_expiry_day=True)
        e.at(datetime(2026, 11, 19, 10, 0))
        assert_approved(e.approve(e.order()))

    def test_exits_not_blocked_by_time_rules(self, env):
        env.open_position(Side.BUY, 1, protect=False)
        env.at(datetime(2026, 11, 9, 14, 20))
        assert_approved(env.approve(env.order(Side.SELL, 1, stop=None, tag="exit")))

    def test_stale_trading_day_rejects_entries(self, env):
        env.clock.set(datetime(2026, 11, 10, 10, 0))   # runner quên gọi new_day
        env.touch_brokers()
        assert_rejected(env.approve(env.order()), RejectCode.STALE_STATE)


# ---------------------------------------------------------- luật: flatten_time ----

class TestFlattenTime:
    def test_before_flatten_time_no_action(self, env):
        env.open_position(Side.BUY, 1)
        env.at(datetime(2026, 11, 9, 14, 24, 59))
        assert env.actions() == []

    def test_at_flatten_time_with_position(self, env):
        env.open_position(Side.BUY, 1)
        env.at(datetime(2026, 11, 9, 14, 25))
        acts = env.actions()
        assert kinds(acts) == [ActionKind.FLATTEN_ALL]

    def test_at_flatten_time_with_only_working_order(self, env):
        env.add_working(env.order(Side.BUY, 1, otype=OrderType.LIMIT, price=1340.0,
                                  status=OrderStatus.PENDING))
        env.at(datetime(2026, 11, 9, 14, 26))
        assert kinds(env.actions()) == [ActionKind.FLATTEN_ALL]

    def test_flat_and_no_orders_no_action(self, env):
        env.at(datetime(2026, 11, 9, 14, 26))
        assert env.actions() == []


# ----------------------------------------------------- luật: stop-loss bắt buộc ----

class TestStopLoss:
    def test_entry_without_stop_rejected(self, env):
        assert_rejected(env.approve(env.order(stop=None)), RejectCode.NO_STOP_LOSS)

    @pytest.mark.parametrize("side,stop", [(Side.BUY, 1350.0), (Side.BUY, 1351.0),
                                           (Side.SELL, 1350.0), (Side.SELL, 1349.0)])
    def test_stop_on_wrong_side_rejected(self, env, side, stop):
        assert_rejected(env.approve(env.order(side, stop=stop)), RejectCode.INVALID_STOP_LOSS)

    def test_limit_price_is_reference_for_stop(self, env):
        o = env.order(Side.BUY, otype=OrderType.LIMIT, price=1340.0, stop=1342.0)
        assert_rejected(env.approve(o), RejectCode.INVALID_STOP_LOSS)

    def test_entry_without_any_reference_price_rejected(self, env):
        env.state.last_prices.clear()
        assert_rejected(env.approve(env.order(Side.BUY)), RejectCode.NO_MARK_PRICE)

    def test_exit_needs_no_stop(self, env):
        env.open_position(Side.BUY, 1, protect=False)
        assert_approved(env.approve(env.order(Side.SELL, stop=None, tag="stop")))

    def test_unprotected_position_flattened_after_grace(self, env):
        env.open_position(Side.BUY, 1, protect=False)
        env.at(env.now + timedelta(seconds=4.9))
        assert env.actions() == []
        env.at(env.now + timedelta(seconds=0.1))                       # đúng 5 giây
        acts = env.actions()
        assert kinds(acts) == [ActionKind.FLATTEN_SYMBOL]
        assert acts[0].symbol == SYM

    def test_acknowledged_stop_protects(self, env):
        env.open_position(Side.BUY, 1, protect=True)
        env.at(env.now + timedelta(seconds=30))
        assert env.actions() == []

    def test_unacknowledged_stop_does_not_count(self, env):
        """Stop mới được duyệt (NEW) chưa chắc đã tới sàn ⇒ chưa tính là đã bảo vệ."""
        env.fill(Side.BUY, 1)
        assert_approved(env.approve(env.order(Side.SELL, stop=None, tag="stop", cid="STOP1")))
        env.at(env.now + timedelta(seconds=6))
        assert kinds(env.actions()) == [ActionKind.FLATTEN_SYMBOL]
        acked = env.state.working_orders["STOP1"].with_status(OrderStatus.PENDING)
        env.rm.on_order_update(env.state, acked)
        assert env.actions() == []

    def test_stop_covering_only_part_of_position(self, make_env):
        e = make_env(max_contracts=2)
        e.fill(Side.BUY, 2)
        e.add_working(e.order(Side.SELL, 1, stop=None, tag="stop", status=OrderStatus.PENDING))
        e.at(e.now + timedelta(seconds=5))
        assert kinds(e.actions()) == [ActionKind.FLATTEN_SYMBOL]

    def test_rejected_stop_restarts_unprotected(self, env):
        env.open_position(Side.BUY, 1, protect=True)
        stop = next(o for o in env.state.working_orders.values() if o.tag == "stop")
        env.at(env.now + timedelta(seconds=60))
        env.rm.on_order_update(env.state, stop.with_status(OrderStatus.REJECTED))
        assert stop.client_order_id not in env.state.working_orders
        assert env.actions() == []                                     # bắt đầu đếm grace
        env.at(env.now + timedelta(seconds=5))
        assert kinds(env.actions()) == [ActionKind.FLATTEN_SYMBOL]

    def test_hedged_net_zero_needs_no_stop(self, env):
        env.fill(Side.BUY, 1, broker="dnse")
        env.fill(Side.SELL, 1, broker="ssi")
        env.at(env.now + timedelta(seconds=30))
        assert env.actions() == []


# ------------------------------------------------------ luật: chống lệnh trùng ----

class TestDuplicateOrders:
    def test_same_client_order_id_rejected(self, env):
        o = env.order(cid="DUP1")
        assert_approved(env.approve(o))
        env.rm.on_order_update(env.state, o.with_status(OrderStatus.CANCELLED))
        assert_rejected(env.approve(o), RejectCode.DUPLICATE_ORDER_ID)

    def test_same_id_rejected_even_for_exit(self, env):
        env.open_position(Side.BUY, 1, protect=False)
        o = env.order(Side.SELL, stop=None, tag="exit", cid="EXIT1")
        assert_approved(env.approve(o))
        assert_rejected(env.approve(o), RejectCode.DUPLICATE_ORDER_ID)

    def test_second_entry_while_first_is_working_rejected(self, make_env):
        e = make_env(max_contracts=3)
        assert_approved(e.approve(e.order(Side.BUY, 1)))              # chưa khớp, còn chờ
        assert_rejected(e.approve(e.order(Side.BUY, 1)), RejectCode.DUPLICATE_ENTRY)

    def test_approved_order_registered_as_working(self, env):
        o = env.order(cid="REG1")
        env.approve(o)
        assert env.state.working_orders["REG1"].status is OrderStatus.NEW
        assert "REG1" in env.state.approved_order_ids

    def test_terminal_update_and_fill_remove_working(self, env):
        o = env.order(cid="E1")
        env.approve(o)
        env.fill(Side.BUY, 1, cid="E1")
        assert "E1" not in env.state.working_orders
        assert env.state.net_qty(SYM) == 1

    def test_partial_fill_keeps_remaining_working(self, make_env):
        e = make_env(max_contracts=2)
        o = e.order(Side.BUY, 2, cid="P1")
        e.approve(o)
        e.fill(Side.BUY, 1, cid="P1")
        assert e.state.working_orders["P1"].filled_qty == 1
        assert e.state.working_orders["P1"].status is OrderStatus.PARTIALLY_FILLED

    def test_id_factory_unique_and_short(self):
        f = OrderIdFactory(prefix="B")
        t = datetime(2026, 11, 9, 9, 30, 1)
        ids = {f.next(t) for _ in range(5000)}
        assert len(ids) == 5000
        assert all(1 <= len(i) <= 20 for i in ids)
        assert OrderIdFactory(prefix="B").next(t) != OrderIdFactory(prefix="E").next(t)

    def test_id_factory_rejects_long_prefix(self):
        with pytest.raises(ValueError):
            OrderIdFactory(prefix="TOOLONG")


# --------------------------------------------- luật: mất kết nối broker ----

class TestBrokerTimeout:
    def test_emergency_when_primary_silent_with_position(self, env):
        env.open_position(Side.BUY, 1)
        last = env.now
        env.at(last + timedelta(seconds=10), touch=False)
        assert env.actions() == []                                     # đúng 10s: chưa "> 10s"
        env.at(last + timedelta(seconds=10.1), touch=False)
        acts = env.actions()
        assert kinds(acts) == [ActionKind.EMERGENCY_HEDGE]
        assert acts[0].broker == "dnse"

    def test_no_emergency_without_position(self, env):
        env.at(env.now + timedelta(seconds=60), touch=False)
        assert env.actions() == []

    def test_never_seen_broker_with_position_is_emergency(self, env):
        env.open_position(Side.BUY, 1)
        del env.state.broker_last_seen["dnse"]
        assert kinds(env.actions()) == [ActionKind.EMERGENCY_HEDGE]

    def test_entry_rejected_when_broker_disconnected(self, env):
        env.at(env.now + timedelta(seconds=11), touch=False)
        assert_rejected(env.approve(env.order()), RejectCode.BROKER_DISCONNECTED)

    def test_hedge_on_backup_allowed_while_primary_down(self, env):
        env.open_position(Side.BUY, 1)
        env.at(env.now + timedelta(seconds=30), touch=False)
        env.touch_brokers("ssi")
        hedge = env.order(Side.SELL, 1, stop=None, broker="ssi", tag="emergency")
        assert_approved(env.approve(hedge))

    def test_entries_only_on_primary_broker(self, env):
        assert_rejected(env.approve(env.order(broker="ssi")), RejectCode.NOT_PRIMARY_BROKER)

    def test_order_without_broker_rejected(self, env):
        assert_rejected(env.approve(env.order(broker="")), RejectCode.UNKNOWN_BROKER)

    def test_unhealthy_feed_blocks_entries(self, env):
        env.state.feed_healthy = False
        assert_rejected(env.approve(env.order()), RejectCode.FEED_UNHEALTHY)


# ------------------------------------------------------------ luật: kill-switch ----

class TestKillSwitch:
    def test_kill_file_triggers_kill(self, env):
        env.open_position(Side.BUY, 1)
        env.kill_file.write_text("")
        acts = env.actions()
        assert ActionKind.KILL in kinds(acts)
        assert ActionKind.FLATTEN_SYMBOL not in kinds(acts)
        assert env.state.kill_reason

    def test_kill_rejects_entries_allows_exits(self, env):
        env.open_position(Side.BUY, 1, protect=False)
        env.kill_file.write_text("")
        assert_rejected(env.approve(env.order(Side.BUY)), RejectCode.KILLED)
        assert_approved(env.approve(env.order(Side.SELL, stop=None, tag="flatten")))

    def test_telegram_kill(self, env):
        env.rm.kill(env.state, "telegram /kill")
        assert kinds(env.actions()) == [ActionKind.KILL]
        assert_rejected(env.approve(env.order()), RejectCode.KILLED)

    def test_kill_reported_even_when_flat(self, env):
        env.kill_file.write_text("")
        assert kinds(env.actions()) == [ActionKind.KILL]

    def test_kill_persists_after_file_removed_and_new_day(self, env):
        env.kill_file.write_text("")
        env.actions()
        env.kill_file.unlink()
        env.at(datetime(2026, 11, 10, 10, 0))
        assert_rejected(env.approve(env.order()), RejectCode.KILLED)
        env.rm.clear_kill(env.state)
        assert_approved(env.approve(env.order()))

    def test_kill_still_reports_broker_emergency(self, env):
        env.open_position(Side.BUY, 1)
        env.rm.kill(env.state, "test")
        env.at(env.now + timedelta(seconds=30), touch=False)
        assert set(kinds(env.actions())) == {ActionKind.KILL, ActionKind.EMERGENCY_HEDGE}


# --------------------------------------------------------- sổ sách theo ngày ----

class TestBookkeeping:
    def test_record_fill_tracks_realized_and_fees(self, env):
        env.fill(Side.BUY, 1, price=1350.0, fee_vnd=30_000)
        env.fill(Side.SELL, 1, price=1353.0, fee_vnd=30_000)
        assert env.state.realized_points_today == pytest.approx(3.0)
        assert env.state.fees_points_today == pytest.approx(0.6)
        assert env.rm.daily_pnl_points(env.state) == pytest.approx(2.4)

    def test_fill_without_broker_rejected(self, env):
        with pytest.raises(ValueError):
            env.rm.record_fill(env.state, Fill("x", SYM, Side.BUY, 1, MARK, env.now))

    def test_new_day_resets_counters_keeps_positions(self, env):
        env.fill(Side.BUY, 1)
        env.state.consecutive_losses = 2
        env.state.approved_order_ids.add("OLD")
        env.rm.new_day(env.state, date(2026, 11, 10))
        assert env.state.trading_day == date(2026, 11, 10)
        assert env.state.trades_today == 0 and env.state.consecutive_losses == 0
        assert env.state.approved_order_ids == set()
        assert env.state.net_qty(SYM) == 1          # vị thế thật không được "quên"

    def test_rejected_order_not_registered(self, env):
        o = env.order(stop=None, cid="NOREG")
        env.approve(o)
        assert "NOREG" not in env.state.working_orders
        assert "NOREG" not in env.state.approved_order_ids
