"""Test cho strategy/baseline_orb.py (PROMPTS P3.2) — viết TRƯỚC khi implement.

Đặc tả: biên độ = đỉnh/đáy các bar trong `range_minutes` đầu phiên liên tục sáng; khi một bar
1 phút ĐÓNG ngoài biên (và đang phẳng) ⇒ vào lệnh theo chiều phá; stop ở cạnh đối diện;
chốt lời = giá đóng bar tín hiệu ± `reward_risk` × R (R = khoảng cách tới stop);
tối đa `max_trades_per_day` lệnh vào mỗi ngày.
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.models import Bar, SignalAction
from core.settings import MissingSetting, Settings
from strategy.base import StrategyContext
from strategy.baseline_orb import OpeningRangeBreakout

SYM = "VN30F2611"
DAY = date(2026, 11, 9)


def settings(**over) -> Settings:
    orb = {"range_minutes": 15, "reward_risk": 1.5, "max_trades_per_day": 2, "qty": 1}
    orb.update(over)
    return Settings({"session": {"continuous_am_start": "09:00"},
                     "contract": {"tick_size_points": 0.1},
                     "strategy": {"orb": orb}})


def bar(hhmm, o, h, l, c, day=DAY, interval=60) -> Bar:
    return Bar(SYM, datetime.combine(day, time.fromisoformat(hhmm)), o, h, l, c, 10, interval)


def range_bars(day=DAY, low=1350.0, high=1352.0, skip=()):
    """15 bar 09:00–09:14 dao động trong [low, high]; bar 09:07 chạm high, 09:03 chạm low."""
    out = []
    for i in range(15):
        if i in skip:
            continue
        hh = f"09:{i:02d}"
        mid = (low + high) / 2
        h = high if i == 7 else mid + 0.5
        l = low if i == 3 else mid - 0.5
        out.append(bar(hh, mid, h, l, mid, day))
    return out


class Feed:
    """Đưa bar lần lượt vào strategy với ctx giống engine (bars_today gồm bar hiện tại)."""

    def __init__(self, strat):
        self.strat = strat
        self.today: list[Bar] = []
        self.day = None
        self.position = 0

    def push(self, b: Bar):
        if b.start.date() != self.day:
            self.day, self.today = b.start.date(), []
        self.today.append(b)
        return list(self.strat.on_bar(b, StrategyContext(self.position, None, tuple(self.today))))

    def push_all(self, bars):
        out = []
        for b in bars:
            out += self.push(b)
        return out


@pytest.fixture
def feed():
    return Feed(OpeningRangeBreakout.from_settings(settings()))


class TestConfig:
    def test_reads_params(self):
        s = OpeningRangeBreakout.from_settings(settings())
        assert (s.range_minutes, s.reward_risk, s.max_trades_per_day, s.qty) == (15, 1.5, 2, 1)
        assert s.session_start == time(9, 0)

    @pytest.mark.parametrize("key", ["range_minutes", "reward_risk", "max_trades_per_day", "qty"])
    def test_null_refuses(self, key):
        with pytest.raises(MissingSetting):
            OpeningRangeBreakout.from_settings(settings(**{key: None}))

    @pytest.mark.parametrize("over", [{"range_minutes": 0}, {"reward_risk": 0},
                                      {"max_trades_per_day": 0}, {"qty": 0}, {"range_minutes": 7.5}])
    def test_invalid(self, over):
        with pytest.raises(ValueError):
            OpeningRangeBreakout.from_settings(settings(**over))

    def test_repo_settings_have_orb_params(self):
        from core.settings import load_settings
        s = OpeningRangeBreakout.from_settings(load_settings())
        assert (s.range_minutes, s.reward_risk, s.max_trades_per_day) == (15, 1.5, 2)


class TestSignals:
    def test_no_signal_while_range_forming(self, feed):
        bars = range_bars()
        bars[5] = bar("09:05", 1351, 1360, 1351, 1359)     # vọt lên giữa lúc dựng biên ⇒ chỉ nới biên
        assert feed.push_all(bars) == []

    def test_long_breakout(self, feed):
        feed.push_all(range_bars())
        sigs = feed.push(bar("09:15", 1351, 1352.8, 1350.9, 1352.6))
        assert len(sigs) == 1
        s = sigs[0]
        assert s.action is SignalAction.ENTER_LONG and s.symbol == SYM and s.qty == 1
        assert s.stop_loss == pytest.approx(1350.0)                 # cạnh đối diện
        assert s.take_profit == pytest.approx(1352.6 + 1.5 * 2.6)   # 1.5R
        assert s.ts == datetime(2026, 11, 9, 9, 16)                 # lúc bar đóng
        assert "1350" in s.reason and "1352" in s.reason

    def test_short_breakout(self, feed):
        feed.push_all(range_bars())
        s = feed.push(bar("09:20", 1351, 1351.2, 1349.4, 1349.6))[0]
        assert s.action is SignalAction.ENTER_SHORT
        assert s.stop_loss == pytest.approx(1352.0)
        assert s.take_profit == pytest.approx(1349.6 - 1.5 * 2.4)

    def test_take_profit_rounded_to_tick(self):
        f = Feed(OpeningRangeBreakout.from_settings(settings(reward_risk=1.25)))
        f.push_all(range_bars())
        s = f.push(bar("09:15", 1351, 1352.8, 1350.9, 1352.7))[0]   # 1352.7 + 1.25×2.7 = 1356.075
        assert s.take_profit == pytest.approx(1356.1)

    @pytest.mark.parametrize("close", [1352.0, 1350.0, 1351.0])
    def test_close_on_or_inside_edge_is_not_breakout(self, feed, close):
        feed.push_all(range_bars())
        assert feed.push(bar("09:15", 1351, max(close, 1351) + 0.5, 1349.5, close)) == []

    def test_wick_outside_but_close_inside_is_not_breakout(self, feed):
        feed.push_all(range_bars())
        assert feed.push(bar("09:15", 1351, 1355, 1349, 1351.5)) == []

    def test_no_entry_when_not_flat(self, feed):
        feed.push_all(range_bars())
        feed.position = 1
        assert feed.push(bar("09:15", 1351, 1353, 1351, 1352.6)) == []

    def test_max_two_entries_per_day_then_reset(self, feed):
        feed.push_all(range_bars())
        up = lambda hh: bar(hh, 1352, 1353, 1352, 1352.6)       # noqa: E731
        assert len(feed.push(up("09:15"))) == 1
        assert len(feed.push(up("09:30"))) == 1
        assert feed.push(up("09:45")) == []                      # lệnh thứ 3 bị bỏ
        feed.push_all(range_bars(day=date(2026, 11, 10)))
        assert len(feed.push(bar("09:15", 1352, 1353, 1352, 1352.6, day=date(2026, 11, 10)))) == 1

    def test_incomplete_range_skips_day(self, feed):
        feed.push_all(range_bars(skip=(7,)))                     # thiếu bar 09:07
        assert feed.push(bar("09:15", 1351, 1353, 1351, 1352.6)) == []
        assert feed.push(bar("09:30", 1351, 1360, 1351, 1359)) == []

    def test_range_starting_late_skips_day(self, feed):
        """Dữ liệu bắt đầu 09:01 (mất bar đầu) ⇒ biên không đúng định nghĩa ⇒ không giao dịch."""
        feed.push_all(range_bars(skip=(0,)) + [bar("09:15", 1351, 1351.5, 1350.5, 1351)])
        assert feed.push(bar("09:16", 1351, 1353, 1351, 1352.6)) == []

    def test_ato_bars_before_session_start_ignored(self, feed):
        """Dữ liệu import có bar từ 08:45 (ATO) — không được tính vào biên."""
        ato = [bar(f"08:{m}", 1351, 1365, 1340, 1351) for m in range(45, 60)]
        feed.push_all(ato + range_bars())
        s = feed.push(bar("09:15", 1351, 1352.8, 1350.9, 1352.6))
        assert len(s) == 1 and s[0].stop_loss == pytest.approx(1350.0)

    def test_five_minute_bars(self):
        f = Feed(OpeningRangeBreakout.from_settings(settings()))
        bars = [bar("09:00", 1351, 1352, 1350.5, 1351, interval=300),
                bar("09:05", 1351, 1351.5, 1350, 1351, interval=300),
                bar("09:10", 1351, 1351.8, 1350.4, 1351, interval=300)]
        assert f.push_all(bars) == []
        s = f.push(bar("09:15", 1351, 1352.6, 1351, 1352.5, interval=300))[0]
        assert s.action is SignalAction.ENTER_LONG and s.stop_loss == 1350.0
        assert s.ts == datetime(2026, 11, 9, 9, 20)

    def test_deterministic_fresh_instances(self):
        bars = range_bars() + [bar("09:15", 1351, 1353, 1351, 1352.6)]
        a = Feed(OpeningRangeBreakout.from_settings(settings())).push_all(bars)
        b = Feed(OpeningRangeBreakout.from_settings(settings())).push_all(bars)
        assert a == b and len(a) == 1


def test_strategy_module_has_no_io_or_clock():
    """Luật kiến trúc: strategy không I/O, không đọc giờ hệ thống, không gọi API."""
    import strategy.baseline_orb as m
    src = open(m.__file__, encoding="utf-8").read()
    for banned in ("datetime.now", "time.time", "open(", "requests", "socket", "brokers",
                   "core.clock", "SystemClock", "os.environ"):
        assert banned not in src, banned


def test_full_pipeline_take_profit(session_cfg, cal):
    """Qua engine: phá biên 09:15 ⇒ vào ở tick mở 09:16, chốt lời 1.5R khi bar sau vọt lên."""
    from backtest.engine import BacktestEngine
    from tests.test_backtest_engine import bt_settings

    s = bt_settings(session_cfg)
    s.raw["strategy"] = {"orb": {"range_minutes": 15, "reward_risk": 1.5, "max_trades_per_day": 2,
                                 "qty": 1}}
    bars = range_bars() + [bar("09:15", 1351, 1352.8, 1350.9, 1352.6),
                           bar("09:16", 1352.6, 1353, 1352.4, 1352.8),
                           bar("09:17", 1352.8, 1357.5, 1352.7, 1357),
                           bar("09:18", 1357, 1357.2, 1356.8, 1357)]
    res = BacktestEngine(s, OpeningRangeBreakout.from_settings(s), calendar=cal).run_bars(bars)
    t = res.trades[0]
    assert t.side == "LONG" and t.exit_reason == "take_profit"
    assert t.entry_ts == datetime(2026, 11, 9, 9, 16, 1)
    assert t.entry_price == pytest.approx(1352.8)     # ask 1352.7 + trượt
    assert t.exit_price == pytest.approx(1356.5)      # 1352.6 + 1.5 × 2.6
