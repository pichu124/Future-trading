"""Test cho brokers/base.py và brokers/paper.py (PROMPTS P2.2) — viết TRƯỚC khi implement.

Luật khớp (CLAUDE.md mục 6): mua khớp ask, bán khớp bid, cộng trượt giá `slippage_ticks`;
lệnh chỉ khớp ở tick KẾ TIẾP (ts > lúc đặt + độ trễ), không bao giờ ở tick sinh tín hiệu.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import count

import pytest

from brokers.base import (BrokerAdapter, BrokerError, LiveTradingNotAllowed,
                          assert_live_trading_allowed)
from brokers.paper import PaperBroker, PaperConfig
from core.clock import FakeClock, MarketClock, SessionSchedule
from core.models import (AccountState, Fill, Order, OrderStatus, OrderType, Side, Tick)
from core.settings import MissingSetting, Settings, load_settings

SYM = "VN30F2611"
T0 = datetime(2026, 11, 9, 10, 0)
FEE = 20_000.0
_ids = count()


def run(coro):
    return asyncio.run(coro)


def config(**over) -> PaperConfig:
    kw = dict(tick_size=0.1, slippage_ticks=1, fee_vnd_per_contract=FEE, latency_ms=0)
    kw.update(over)
    return PaperConfig(**kw)


class Rig:
    def __init__(self, session=None, **cfg):
        self.clock = FakeClock(T0)
        self.broker = PaperBroker(config(**cfg), self.clock, name="paper", session=session)
        self.fills: list[Fill] = []
        self.updates: list[Order] = []
        self.broker.on_fill(self.fills.append)
        self.broker.on_order_update(self.updates.append)

    def order(self, side=Side.BUY, qty=1, otype=OrderType.MARKET, price=None, cid=None, tag="entry"):
        return Order(cid or f"P{next(_ids)}", SYM, side, qty, otype, self.clock.now(),
                     price=price, tag=tag)

    def place(self, *a, **kw) -> Order:
        o = a[0] if a and isinstance(a[0], Order) else self.order(*a, **kw)
        return run(self.broker.place(o))

    def tick(self, seconds: float, bid=None, ask=None, bid_qty=None, ask_qty=None, last=None,
             symbol=SYM) -> None:
        ts = T0 + timedelta(seconds=seconds)
        self.clock.set(ts)
        self.broker.on_tick(Tick(symbol, ts, last=last, bid=bid, ask=ask, bid_qty=bid_qty,
                                 ask_qty=ask_qty, source="test"))

    def status(self, cid) -> OrderStatus:
        return self.broker.get_order(cid).status


@pytest.fixture
def rig():
    return Rig()


# ------------------------------------------------------------------ cấu hình ----

class TestConfig:
    def test_repo_settings_with_null_fees_refuse(self):
        """Settings của repo nhưng một khoản phí bị xoá về null ⇒ paper/backtest từ chối chạy."""
        import copy
        d = copy.deepcopy(load_settings()._data)
        d["fees"]["tax_vnd_per_contract"] = None
        with pytest.raises(MissingSetting):
            PaperConfig.from_settings(Settings(d))

    def test_repo_fees_match_dnse_schedule(self):
        """Biểu phí DNSE 01/05/2026: 1.000 + 2.700 + 2.550 + thuế ~17.100 (giá ~1.850, ký quỹ 18,48%)."""
        f = load_settings().get("fees")
        assert (f["broker_fee_vnd_per_contract"], f["exchange_fee_vnd_per_contract"],
                f["clearing_fee_vnd_per_contract"]) == (1000, 2700, 2550)
        assert 15_000 <= f["tax_vnd_per_contract"] <= 19_000

    def test_from_settings_sums_fees_per_contract_side(self):
        s = Settings({
            "contract": {"tick_size_points": 0.1},
            "backtest": {"slippage_ticks": 2},
            "fees": {"broker_fee_vnd_per_contract": 5000, "exchange_fee_vnd_per_contract": 2700,
                     "clearing_fee_vnd_per_contract": 2550, "tax_vnd_per_contract": 0},
            "paper": {"latency_ms": 300, "latency_jitter_ms": 50, "seed": 7},
        })
        c = PaperConfig.from_settings(s)
        assert c.fee_vnd_per_contract == pytest.approx(10_250)
        assert c.slippage_ticks == 2 and c.latency_ms == 300 and c.latency_jitter_ms == 50

    def test_zero_tax_is_not_missing(self):
        """0 là giá trị hợp lệ (đã điền), khác null."""
        s = Settings({"contract": {"tick_size_points": 0.1}, "backtest": {"slippage_ticks": 1},
                      "fees": {"broker_fee_vnd_per_contract": 0, "exchange_fee_vnd_per_contract": 0,
                               "clearing_fee_vnd_per_contract": 0, "tax_vnd_per_contract": 0},
                      "paper": {"latency_ms": 0, "latency_jitter_ms": 0}})
        assert PaperConfig.from_settings(s).fee_vnd_per_contract == 0

    @pytest.mark.parametrize("over", [{"tick_size": 0}, {"slippage_ticks": -1},
                                      {"fee_vnd_per_contract": -1}, {"latency_ms": -1},
                                      {"latency_jitter_ms": -5}])
    def test_invalid_values(self, over):
        with pytest.raises(ValueError):
            config(**over)


# ------------------------------------------------------------- interface ----

def test_paper_implements_broker_adapter(rig):
    assert isinstance(rig.broker, BrokerAdapter)
    for name in ("place", "cancel", "modify", "positions", "open_orders", "on_fill",
                 "on_order_update"):
        assert callable(getattr(rig.broker, name))


class TestTradingModeGuard:
    def test_paper_mode_blocks_live(self):
        with pytest.raises(LiveTradingNotAllowed):
            assert_live_trading_allowed(live_flag=True, env={"TRADING_MODE": "paper"})

    def test_missing_env_blocks_live(self):
        with pytest.raises(LiveTradingNotAllowed):
            assert_live_trading_allowed(live_flag=True, env={})

    def test_env_without_flag_blocks_live(self):
        with pytest.raises(LiveTradingNotAllowed):
            assert_live_trading_allowed(live_flag=False, env={"TRADING_MODE": "live"})

    def test_env_and_flag_allow_live(self):
        assert_live_trading_allowed(live_flag=True, env={"TRADING_MODE": "live"})
        assert_live_trading_allowed(live_flag=True, env={"TRADING_MODE": " LIVE "})


# ------------------------------------------------------------------ đặt lệnh ----

class TestPlace:
    def test_accepted_order_is_pending_with_ids(self, rig):
        o = rig.place(Side.BUY, cid="A1")
        assert o.status is OrderStatus.PENDING
        assert o.broker == "paper" and o.broker_order_id
        assert rig.updates[-1].client_order_id == "A1"
        assert rig.updates[-1].status is OrderStatus.PENDING

    def test_duplicate_client_order_id_is_idempotent(self, rig):
        o1 = rig.place(Side.BUY, cid="DUP")
        o2 = rig.place(Side.BUY, cid="DUP")
        assert o2.broker_order_id == o1.broker_order_id
        rig.tick(1, bid=1350.0, ask=1350.2)
        assert len(rig.fills) == 1                                    # không thành hai lệnh

    def test_limit_price_off_tick_rejected(self, rig):
        o = rig.place(Side.BUY, otype=OrderType.LIMIT, price=1350.05)
        assert o.status is OrderStatus.REJECTED and "bước giá" in o.reject_reason

    @pytest.mark.parametrize("otype", [OrderType.ATO, OrderType.ATC])
    def test_auction_orders_not_simulated(self, rig, otype):
        o = rig.place(Side.BUY, otype=otype)
        assert o.status is OrderStatus.REJECTED and o.reject_reason

    def test_rejected_order_never_fills(self, rig):
        rig.place(Side.BUY, otype=OrderType.LIMIT, price=1350.05, cid="R1")
        rig.tick(1, bid=1340.0, ask=1340.2)
        assert rig.fills == []


# ------------------------------------------------------------------ lệnh MTL ----

class TestMarketOrders:
    def test_buy_fills_at_next_tick_ask_plus_slippage(self, rig):
        rig.place(Side.BUY, cid="M1")
        rig.tick(1, bid=1350.0, ask=1350.2)
        f = rig.fills[-1]
        assert f.price == pytest.approx(1350.3) and f.qty == 1 and f.side is Side.BUY
        assert f.broker == "paper" and f.client_order_id == "M1"
        assert f.ts == T0 + timedelta(seconds=1)
        assert rig.status("M1") is OrderStatus.FILLED

    def test_sell_fills_at_bid_minus_slippage(self, rig):
        rig.place(Side.SELL, cid="M2")
        rig.tick(1, bid=1350.0, ask=1350.2)
        assert rig.fills[-1].price == pytest.approx(1349.9)

    def test_never_fills_on_tick_at_placement_time(self, rig):
        """Tick cùng thời điểm đặt lệnh = tick sinh tín hiệu ⇒ không được khớp."""
        rig.place(Side.BUY)
        rig.tick(0, bid=1350.0, ask=1350.2)
        assert rig.fills == []
        rig.tick(0.001, bid=1351.0, ask=1351.2)
        assert rig.fills[-1].price == pytest.approx(1351.3)

    def test_slippage_zero(self):
        r = Rig(slippage_ticks=0)
        r.place(Side.BUY)
        r.tick(1, bid=1350.0, ask=1350.2)
        assert r.fills[-1].price == pytest.approx(1350.2)

    def test_fee_charged_per_contract(self, make_qty_rig):
        r = make_qty_rig
        r.place(Side.BUY, qty=3)
        r.tick(1, bid=1350.0, ask=1350.2)
        assert r.fills[-1].fee_vnd == pytest.approx(3 * FEE)

    def test_waits_for_tick_with_needed_quote(self, rig):
        rig.place(Side.BUY)
        rig.tick(1, bid=1350.0, last=1350.1)                          # không có ask
        assert rig.fills == []
        rig.tick(2, bid=1350.0, ask=1350.4)
        assert rig.fills[-1].price == pytest.approx(1350.5)

    def test_other_symbol_ticks_ignored(self, rig):
        rig.place(Side.BUY)
        rig.tick(1, bid=1300.0, ask=1300.2, symbol="VN30F2612")
        assert rig.fills == []

    def test_partial_fill_by_quote_size_then_rest_as_limit(self, make_qty_rig):
        """MTL sau KRX: phần chưa khớp thành LO tại giá khớp gần nhất ± 1 bước giá."""
        r = make_qty_rig
        r.place(Side.BUY, qty=3, cid="PQ")
        r.tick(1, bid=1350.0, ask=1350.2, ask_qty=1)
        assert [f.qty for f in r.fills] == [1]
        o = r.broker.get_order("PQ")
        assert o.status is OrderStatus.PARTIALLY_FILLED and o.filled_qty == 1
        assert o.order_type is OrderType.LIMIT and o.price == pytest.approx(1350.4)
        r.tick(2, bid=1351.0, ask=1351.2, ask_qty=10)                 # ask > LO ⇒ chờ
        assert len(r.fills) == 1
        r.tick(3, bid=1350.0, ask=1350.2, ask_qty=10)                 # xuyên giá LO
        assert sum(f.qty for f in r.fills) == 3
        assert r.status("PQ") is OrderStatus.FILLED

    def test_quote_size_shared_between_orders(self, make_qty_rig):
        r = make_qty_rig
        r.place(Side.BUY, qty=1, cid="Q1")
        r.place(Side.BUY, qty=1, cid="Q2")
        r.tick(1, bid=1350.0, ask=1350.2, ask_qty=1)
        assert [f.client_order_id for f in r.fills] == ["Q1"]         # theo thứ tự đặt


@pytest.fixture
def make_qty_rig():
    return Rig()


# ------------------------------------------------------------------- độ trễ ----

class TestLatency:
    def test_order_not_active_before_latency(self):
        r = Rig(latency_ms=500)
        r.place(Side.BUY)
        r.tick(0.3, bid=1350.0, ask=1350.2)
        assert r.fills == []
        r.tick(0.5, bid=1352.0, ask=1352.2)
        assert r.fills[-1].price == pytest.approx(1352.3)

    def test_jitter_is_deterministic_with_seed(self):
        def activations(seed):
            r = Rig(latency_ms=100, latency_jitter_ms=200, seed=seed)
            return [r.broker.activation_time(r.place(Side.BUY).client_order_id) for _ in range(5)]
        a, b = activations(1), activations(1)
        assert a == b
        assert all(T0 + timedelta(milliseconds=100) <= t <= T0 + timedelta(milliseconds=300)
                   for t in a)


# ------------------------------------------------------------------- lệnh LO ----

class TestLimitOrders:
    def test_marketable_buy_fills_at_ask_plus_slip_capped_by_limit(self, rig):
        rig.place(Side.BUY, otype=OrderType.LIMIT, price=1351.0, cid="L1")
        rig.tick(1, bid=1350.0, ask=1350.5)
        assert rig.fills[-1].price == pytest.approx(1350.6)

    def test_marketable_buy_never_worse_than_limit(self, rig):
        rig.place(Side.BUY, otype=OrderType.LIMIT, price=1351.0)
        rig.tick(1, bid=1350.8, ask=1351.0)
        assert rig.fills[-1].price == pytest.approx(1351.0)

    def test_resting_buy_needs_trade_through(self, rig):
        """Chạm giá chưa chắc đến lượt mình trong hàng chờ ⇒ chỉ khớp khi giá xuyên qua."""
        rig.place(Side.BUY, otype=OrderType.LIMIT, price=1351.0, cid="L2")
        rig.tick(1, bid=1351.5, ask=1352.0)                           # không khớp được ⇒ nằm chờ
        rig.tick(2, bid=1350.8, ask=1351.0)                           # chỉ chạm
        assert rig.fills == []
        rig.tick(3, bid=1350.7, ask=1350.9)                           # xuyên
        assert rig.fills[-1].price == pytest.approx(1351.0)

    def test_resting_sell_needs_trade_through(self, rig):
        rig.place(Side.SELL, otype=OrderType.LIMIT, price=1355.0)
        rig.tick(1, bid=1354.0, ask=1354.2)
        rig.tick(2, bid=1355.0, ask=1355.2)
        assert rig.fills == []
        rig.tick(3, bid=1355.1, ask=1355.3)
        assert rig.fills[-1].price == pytest.approx(1355.0)

    def test_marketable_sell(self, rig):
        rig.place(Side.SELL, otype=OrderType.LIMIT, price=1349.0)
        rig.tick(1, bid=1350.0, ask=1350.2)
        assert rig.fills[-1].price == pytest.approx(1349.9)


# ----------------------------------------------------------------- lệnh STOP ----

class TestStopOrders:
    def test_sell_stop_triggers_on_bid_then_fills_next_tick(self, rig):
        rig.place(Side.SELL, otype=OrderType.STOP, price=1345.0, cid="S1", tag="stop")
        rig.tick(1, bid=1346.0, ask=1346.2)
        rig.tick(2, bid=1345.0, ask=1345.2)                           # kích hoạt
        assert rig.fills == []
        rig.tick(3, bid=1344.5, ask=1344.7)
        assert rig.fills[-1].price == pytest.approx(1344.4)

    def test_triggered_stop_ignores_ticks_with_trigger_timestamp(self, rig):
        """Tick khác cùng thời điểm kích hoạt vẫn là 'tick kích hoạt' ⇒ chưa được khớp."""
        rig.place(Side.SELL, otype=OrderType.STOP, price=1345.0)
        rig.tick(1, bid=1345.0, ask=1345.2)
        rig.tick(1, bid=1344.9, ask=1345.1)
        assert rig.fills == []

    def test_buy_stop_triggers_on_ask(self, rig):
        rig.place(Side.BUY, otype=OrderType.STOP, price=1355.0)
        rig.tick(1, bid=1354.8, ask=1355.0)
        rig.tick(2, bid=1355.0, ask=1355.2)
        assert rig.fills[-1].price == pytest.approx(1355.3)

    def test_gap_through_stop_fills_at_gap_price(self, rig):
        """Không ảo tưởng khớp đúng giá stop khi giá nhảy cóc."""
        rig.place(Side.SELL, otype=OrderType.STOP, price=1345.0)
        rig.tick(1, bid=1340.0, ask=1340.2)
        rig.tick(2, bid=1338.0, ask=1338.2)
        assert rig.fills[-1].price == pytest.approx(1337.9)

    def test_stop_not_triggered_by_tick_before_activation(self):
        r = Rig(latency_ms=500)
        r.place(Side.SELL, otype=OrderType.STOP, price=1345.0)
        r.tick(0.2, bid=1344.0, ask=1344.2)
        r.tick(0.6, bid=1346.0, ask=1346.2)
        r.tick(0.9, bid=1346.0, ask=1346.2)
        assert r.fills == []


# ----------------------------------------------------------- huỷ / sửa lệnh ----

class TestCancelModify:
    def test_cancel_working_order(self, rig):
        rig.place(Side.BUY, otype=OrderType.LIMIT, price=1340.0, cid="C1")
        o = run(rig.broker.cancel("C1"))
        assert o.status is OrderStatus.CANCELLED
        assert rig.updates[-1].status is OrderStatus.CANCELLED
        rig.tick(1, bid=1330.0, ask=1330.2)
        assert rig.fills == []

    def test_cancel_unknown_raises(self, rig):
        with pytest.raises(BrokerError):
            run(rig.broker.cancel("NOPE"))

    def test_cancel_filled_raises(self, rig):
        rig.place(Side.BUY, cid="C2")
        rig.tick(1, bid=1350.0, ask=1350.2)
        with pytest.raises(BrokerError):
            run(rig.broker.cancel("C2"))

    def test_modify_price_gets_new_broker_id_and_latency(self):
        r = Rig(latency_ms=500)
        o = r.place(Side.BUY, otype=OrderType.LIMIT, price=1340.0, cid="MD")
        r.tick(1, bid=1345.0, ask=1345.2)
        m = run(r.broker.modify("MD", price=1346.0))
        assert m.client_order_id == "MD" and m.broker_order_id != o.broker_order_id
        assert m.price == 1346.0
        r.tick(1.2, bid=1345.0, ask=1345.2)                           # còn trong độ trễ
        assert r.fills == []
        r.tick(1.6, bid=1345.0, ask=1345.2)
        assert r.fills[-1].price == pytest.approx(1345.3)

    def test_modify_qty_below_filled_raises(self, make_qty_rig):
        r = make_qty_rig
        r.place(Side.BUY, qty=3, cid="MQ")
        r.tick(1, bid=1350.0, ask=1350.2, ask_qty=2)
        with pytest.raises(BrokerError):
            run(r.broker.modify("MQ", qty=1))

    def test_modify_off_tick_price_raises(self, rig):
        rig.place(Side.BUY, otype=OrderType.LIMIT, price=1340.0, cid="MT")
        with pytest.raises(BrokerError):
            run(rig.broker.modify("MT", price=1340.05))

    def test_modify_market_order_raises(self, rig):
        rig.place(Side.BUY, cid="MM")
        with pytest.raises(BrokerError):
            run(rig.broker.modify("MM", price=1340.0))


# -------------------------------------------------------- vị thế & lệnh mở ----

class TestPositions:
    def test_round_trip_positions(self, rig):
        rig.place(Side.BUY)
        rig.tick(1, bid=1350.0, ask=1350.2)                           # mua 1350.3
        rig.place(Side.SELL)
        rig.tick(2, bid=1352.0, ask=1352.2)                           # bán 1351.9
        p = run(rig.broker.positions())[SYM]
        assert p.is_flat and p.broker == "paper"
        assert p.realized_points == pytest.approx(1.6)

    def test_open_orders(self, rig):
        rig.place(Side.BUY, otype=OrderType.LIMIT, price=1340.0, cid="O1")
        rig.place(Side.BUY, cid="O2")
        rig.tick(1, bid=1350.0, ask=1350.2)
        assert [o.client_order_id for o in run(rig.broker.open_orders())] == ["O1"]


# --------------------------------------------------------------- phiên giao dịch ----

@pytest.fixture
def session_rig(session_cfg, cal):
    r = Rig()
    r.broker.session = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, r.clock)
    return r


class TestSession:
    def test_place_rejected_when_market_closed(self, session_rig):
        session_rig.clock.set(datetime(2026, 11, 9, 15, 0))
        o = session_rig.place(Side.BUY)
        assert o.status is OrderStatus.REJECTED

    def test_market_order_only_in_continuous(self, session_rig):
        session_rig.clock.set(datetime(2026, 11, 9, 12, 0))
        assert session_rig.place(Side.BUY).status is OrderStatus.REJECTED
        lo = session_rig.place(Side.BUY, otype=OrderType.LIMIT, price=1340.0)
        assert lo.status is OrderStatus.PENDING                       # LO được nhập lúc nghỉ trưa

    def test_cancel_not_allowed_in_atc(self, session_rig):
        session_rig.place(Side.BUY, otype=OrderType.LIMIT, price=1340.0, cid="ATC1")
        session_rig.clock.set(datetime(2026, 11, 9, 14, 35))
        with pytest.raises(BrokerError):
            run(session_rig.broker.cancel("ATC1"))

    def test_no_matching_outside_continuous(self, session_rig):
        """Paper không mô phỏng khớp định kỳ ATO/ATC hay nghỉ trưa."""
        session_rig.place(Side.BUY, otype=OrderType.LIMIT, price=1360.0)
        session_rig.clock.set(datetime(2026, 11, 9, 14, 35))
        ts = datetime(2026, 11, 9, 14, 35)
        session_rig.broker.on_tick(Tick(SYM, ts, bid=1350.0, ask=1350.2))
        assert session_rig.fills == []


# ------------------------------------------------- nối với risk manager ----

def test_paper_callbacks_drive_risk_manager(session_cfg, cal, tmp_path):
    """Lệnh vào khớp → stop được paper xác nhận → risk manager thấy vị thế đã được bảo vệ."""
    from risk.risk_manager import RiskManager
    from tests.test_risk_manager import make_settings

    clock = FakeClock(T0)
    mclock = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, clock)
    rm = RiskManager.from_settings(make_settings(session_cfg), mclock, kill_file=tmp_path / "KILL")
    rm.limits = replace(rm.limits, primary_broker="paper")
    state = AccountState(trading_day=T0.date(), last_prices={SYM: 1350.0},
                         broker_last_seen={"paper": T0}, market_data_at=T0)
    broker = PaperBroker(config(), clock, name="paper", session=mclock)
    broker.on_fill(lambda f: rm.record_fill(state, f))
    broker.on_order_update(lambda o: rm.on_order_update(state, o))

    entry = Order("E1", SYM, Side.BUY, 1, OrderType.MARKET, T0, broker="paper", tag="entry",
                  stop_loss=1345.0)
    assert rm.approve(entry, state).ok
    run(broker.place(entry))
    clock.set(T0 + timedelta(seconds=1))
    state.broker_last_seen["paper"] = clock.now()
    broker.on_tick(Tick(SYM, clock.now(), bid=1350.0, ask=1350.2))
    assert state.net_qty(SYM) == 1 and "E1" not in state.working_orders

    stop = Order("S1", SYM, Side.SELL, 1, OrderType.STOP, clock.now(), price=1345.0,
                 broker="paper", tag="stop")
    assert rm.approve(stop, state).ok
    run(broker.place(stop))
    assert state.working_orders["S1"].status is OrderStatus.PENDING
    clock.set(T0 + timedelta(seconds=30))
    state.broker_last_seen["paper"] = clock.now()
    state.market_data_at = clock.now()
    assert rm.check(state) == []


def test_limit_placed_away_from_market_rests_from_placement(rig):
    """LO đặt khi giá chưa tới phải nằm chờ ngay từ lúc đặt: giá nhảy qua ⇒ khớp đúng giá LO,
    không được hưởng giá tốt hơn (không thì chốt lời trong backtest đẹp hơn thật)."""
    rig.tick(1, bid=1350.3, ask=1350.5)                       # giá thị trường đã biết
    rig.place(Side.SELL, otype=OrderType.LIMIT, price=1351.0, cid="TP")
    rig.tick(20, bid=1351.9, ask=1352.1)                      # nhảy qua giá LO
    assert rig.fills[-1].price == pytest.approx(1351.0)


def test_marketable_limit_at_placement_still_fills_at_quote(rig):
    rig.tick(1, bid=1350.3, ask=1350.5)
    rig.place(Side.BUY, otype=OrderType.LIMIT, price=1351.0)  # ask 1350.5 ≤ LO ⇒ khớp được ngay
    rig.tick(2, bid=1350.4, ask=1350.6)
    assert rig.fills[-1].price == pytest.approx(1350.7)       # ask + 1 bước trượt, ≤ LO
