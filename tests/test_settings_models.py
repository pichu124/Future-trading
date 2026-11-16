from datetime import datetime

import pytest

from core.models import (AccountState, Bar, Fill, Order, OrderStatus, OrderType, Position,
                         Side, Tick)
from core.settings import MissingSetting, Settings, load_settings

T = datetime(2026, 11, 9, 9, 30)


class TestSettings:
    def test_repo_settings_load_and_report_missing(self):
        s = load_settings()
        assert s.get("contract.multiplier_vnd_per_point") == 100000
        assert s.get("live.allowed_machine") == "CFA-BINHLD-LT"           # máy DUY NHẤT chạy bot (laptop)
        import copy
        raw = copy.deepcopy(s.raw)
        raw["live"]["allowed_machine"] = None
        assert "live.allowed_machine" in Settings(raw).missing()          # khoá còn null được liệt kê
        assert "fees.broker_fee_vnd_per_contract" not in s.missing()   # đã điền theo biểu phí DNSE

    def test_require_raises_on_null(self):
        s = Settings({"fees": {"broker": None}, "risk": {"max_contracts": 1}})
        assert s.require("risk.max_contracts") == 1
        with pytest.raises(MissingSetting):
            s.require("fees.broker")
        with pytest.raises(MissingSetting):
            s.require("does.not.exist")

    def test_get_default(self):
        s = Settings({"a": {"b": None}})
        assert s.get("a.b", 5) == 5
        assert s.get("a.c.d", "x") == "x"


class TestModels:
    def test_tick_mid(self):
        assert Tick("VN30F2611", T, bid=1350.0, ask=1350.2).mid == pytest.approx(1350.1)
        assert Tick("VN30F2611", T, bid=1350.0).mid is None

    def test_bar_validates_ohlc(self):
        Bar("VN30F2611", T, 1350, 1352, 1349, 1351, 10)
        with pytest.raises(ValueError):
            Bar("VN30F2611", T, 1350, 1349, 1348, 1351, 10)   # high < close
        with pytest.raises(ValueError):
            Bar("VN30F2611", T, 1350, 1352, 1349, 1351, -1)

    def test_order_validation(self):
        with pytest.raises(ValueError):
            Order("x", "VN30F2611", Side.BUY, 0, OrderType.MARKET, T)
        with pytest.raises(ValueError):
            Order("x", "VN30F2611", Side.BUY, 1, OrderType.LIMIT, T)       # LO thiếu giá
        with pytest.raises(ValueError):
            Order("", "VN30F2611", Side.BUY, 1, OrderType.MARKET, T)
        with pytest.raises(ValueError):
            Order("x" * 21, "VN30F2611", Side.BUY, 1, OrderType.MARKET, T)   # SSI: ≤ 20 ký tự
        Order("x" * 20, "VN30F2611", Side.BUY, 1, OrderType.MARKET, T)
        o = Order("x", "VN30F2611", Side.BUY, 1, OrderType.LIMIT, T, price=1350.0)
        filled = o.with_status(OrderStatus.FILLED, filled_qty=1)
        assert o.status is OrderStatus.NEW and filled.status.is_terminal

    def _fill(self, side, qty, price):
        return Fill("id", "VN30F2611", side, qty, price, T)

    def test_position_long_then_close_realizes_points(self):
        p = Position("VN30F2611")
        p = p.apply(self._fill(Side.BUY, 2, 1350.0))
        p = p.apply(self._fill(Side.BUY, 1, 1353.0))
        assert p.qty == 3 and p.avg_price == pytest.approx(1351.0)
        p = p.apply(self._fill(Side.SELL, 3, 1355.0))
        assert p.is_flat and p.realized_points == pytest.approx(12.0)

    def test_position_short_and_reverse(self):
        p = Position("VN30F2611").apply(self._fill(Side.SELL, 1, 1350.0))
        assert p.unrealized_points(1348.0) == pytest.approx(2.0)
        p = p.apply(self._fill(Side.BUY, 3, 1348.0))           # đóng 1, mở Long 2
        assert p.qty == 2 and p.avg_price == 1348.0
        assert p.realized_points == pytest.approx(2.0)

    def test_position_rejects_other_symbol(self):
        with pytest.raises(ValueError):
            Position("VN30F2612").apply(self._fill(Side.BUY, 1, 1350.0))

    def test_net_qty_across_brokers(self):
        st = AccountState(positions={
            "dnse:VN30F2611": Position("VN30F2611", qty=2, broker="dnse"),
            "ssi:VN30F2611": Position("VN30F2611", qty=-2, broker="ssi"),
            "ssi:VN30F2612": Position("VN30F2612", qty=1, broker="ssi"),
        })
        assert st.net_qty("VN30F2611") == 0
        assert st.net_qty("VN30F2612") == 1
