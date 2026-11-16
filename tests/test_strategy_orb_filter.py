"""ORB cấu hình #2: lọc ngày theo độ rộng biên so với biên độ ngày trung bình — viết TRƯỚC khi implement.

Đặc tả (chốt TRƯỚC khi xem dữ liệu; research/orb_range_filter.md, research/log.md dòng #2):
- `strategy.orb.range_filter.enabled = false` ⇒ y hệt baseline (#1), `history_days = 0`.
- Bật ⇒ `history_days = adr_days`. ADR = trung bình (đỉnh − đáy) của đúng `adr_days` phiên GẦN NHẤT trong
  `ctx.history` (mọi bar của phiên). Back-adjust cộng điểm không đổi đỉnh − đáy ⇒ roll không ảnh hưởng ADR.
- Biên 15′ hoàn tất ⇒ tỉ lệ = (đỉnh − đáy biên) / ADR. Chỉ giao dịch hôm đó khi
  `min_ratio ≤ tỉ lệ ≤ max_ratio` (biên quá hẹp = nhiễu; quá rộng = stop xa, R/R xấu). Ngoài khoảng ⇒ cả ngày
  không vào lệnh. Thiếu lịch sử (< adr_days phiên) hoặc ADR = 0 ⇒ không giao dịch (không đoán).
- Vào lệnh / stop / chốt lời / giới hạn số lệnh: y hệt baseline.
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.models import Bar, SignalAction
from core.settings import MissingSetting, Settings
from strategy.base import StrategyContext, history_days
from strategy.baseline_orb import OpeningRangeBreakout
from tests.test_strategy_orb import DAY, SYM, bar, range_bars

FILTER = {"enabled": True, "adr_days": 3, "min_ratio": 0.1, "max_ratio": 0.6}


def settings(rf=None, **over) -> Settings:
    orb = {"range_minutes": 15, "reward_risk": 1.5, "max_trades_per_day": 2, "qty": 1}
    orb.update(over)
    if rf is not None:
        orb["range_filter"] = rf
    return Settings({"session": {"continuous_am_start": "09:00"},
                     "contract": {"tick_size_points": 0.1},
                     "strategy": {"orb": orb}})


def hist_day(d, low, high, sym=SYM):
    t0 = datetime.combine(d, time(9, 0))
    mid = (low + high) / 2
    return [Bar(sym, t0, mid, high, mid, mid, 5), Bar(sym, t0 + timedelta(minutes=1), mid, mid, low, mid, 5)]


def history(n=3, rng=10.0, base=1350.0, end=DAY):
    out, d = [], end
    days = []
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    for d in sorted(days):
        out += hist_day(d, base - rng / 2, base + rng / 2)
    return tuple(out)


def run_day(strat, hist, low=1350.0, high=1352.0, breakout=1353.0):
    """Biên [low, high] rồi một bar đóng ở `breakout`; trả tín hiệu."""
    today, sigs = [], []
    for b in range_bars(low=low, high=high) + [bar("09:15", high, breakout + 0.2, high - 0.1, breakout)]:
        today.append(b)
        sigs += list(strat.on_bar(b, StrategyContext(0, None, tuple(today), hist)))
    return sigs


# ------------------------------------------------------------------ cấu hình ----

def test_disabled_or_absent_is_baseline():
    for s in (settings(), settings({**FILTER, "enabled": False})):
        strat = OpeningRangeBreakout.from_settings(s)
        assert history_days(strat) == 0
        assert len(run_day(strat, ())) == 1                         # không cần lịch sử


def test_enabled_declares_history_days():
    assert history_days(OpeningRangeBreakout.from_settings(settings(FILTER))) == 3


@pytest.mark.parametrize("bad", [
    {"adr_days": 0}, {"adr_days": 2.5}, {"min_ratio": -0.1}, {"max_ratio": 0}, {"min_ratio": 0.6, "max_ratio": 0.6},
    {"min_ratio": 0.7}, {"enabled": "yes"},
])
def test_invalid_filter_config_rejected(bad):
    with pytest.raises(ValueError):
        OpeningRangeBreakout.from_settings(settings({**FILTER, **bad}))


def test_enabled_with_null_value_refused():
    with pytest.raises(MissingSetting):
        OpeningRangeBreakout.from_settings(settings({**FILTER, "max_ratio": None}))


# ------------------------------------------------------------------ lọc ----

def test_range_within_band_trades_and_reason_has_ratio():
    strat = OpeningRangeBreakout.from_settings(settings(FILTER))
    sigs = run_day(strat, history(rng=10.0), low=1350.0, high=1352.0)   # 2 / 10 = 0.2
    assert len(sigs) == 1 and sigs[0].action is SignalAction.ENTER_LONG
    assert sigs[0].stop_loss == 1350.0                               # y hệt baseline
    assert "0.20" in sigs[0].reason and "ADR" in sigs[0].reason


@pytest.mark.parametrize("low,high,ok", [      # ADR = 20 (range_bars luôn rộng ≥ 1 điểm)
    (1350.0, 1351.0, False),     # 0.05 < 0.1: quá hẹp
    (1350.0, 1352.0, True),      # = 0.1: biên dưới tính là trong
    (1350.0, 1362.0, True),      # = 0.6: biên trên tính là trong
    (1350.0, 1364.0, False),     # 0.7 > 0.6: quá rộng
])
def test_band_edges(low, high, ok):
    strat = OpeningRangeBreakout.from_settings(settings(FILTER))
    sigs = run_day(strat, history(rng=20.0), low=low, high=high, breakout=high + 1.0)
    assert (len(sigs) == 1) is ok


def test_adr_uses_last_n_sessions_only():
    strat = OpeningRangeBreakout.from_settings(settings(FILTER))
    old = history(n=5, rng=100.0)[:4]                                # 2 phiên rất cũ, biên độ 100
    hist = tuple(sorted(old + history(n=3, rng=10.0), key=lambda b: b.start))
    assert len({b.start.date() for b in hist}) == 5
    assert len(run_day(strat, hist, low=1350.0, high=1352.0)) == 1   # ADR = 10 (3 phiên cuối) ⇒ 0.2


def test_not_enough_history_no_trade():
    strat = OpeningRangeBreakout.from_settings(settings(FILTER))
    assert run_day(strat, history(n=2, rng=10.0)) == []
    assert run_day(strat, ()) == []


def test_zero_adr_no_trade():
    strat = OpeningRangeBreakout.from_settings(settings(FILTER))
    flat_hist = tuple(Bar(SYM, b.start, 1350.0, 1350.0, 1350.0, 1350.0, 5) for b in history(rng=10.0))
    assert run_day(strat, flat_hist) == []


def test_back_adjusted_history_same_decision():
    strat_a = OpeningRangeBreakout.from_settings(settings(FILTER))
    strat_b = OpeningRangeBreakout.from_settings(settings(FILTER))
    a = run_day(strat_a, history(rng=10.0, base=1350.0))
    b = run_day(strat_b, history(rng=10.0, base=1300.0))             # cùng biên độ, mức giá khác (dịch roll)
    assert [(s.action, s.stop_loss, s.take_profit) for s in a] == [(s.action, s.stop_loss, s.take_profit) for s in b]


def test_filtered_day_stays_filtered_after_more_bars():
    strat = OpeningRangeBreakout.from_settings(settings(FILTER))
    hist = history(rng=10.0)
    today, sigs = [], []
    bars = range_bars(low=1350.0, high=1357.0)                       # 0.7: quá rộng
    bars += [bar(f"09:{m}", 1357.0, 1360.0, 1356.9, 1359.0) for m in range(15, 30)]
    for b in bars:
        today.append(b)
        sigs += list(strat.on_bar(b, StrategyContext(0, None, tuple(today), hist)))
    assert sigs == []


# ------------------------------------------------------------------ tích hợp ----

def test_engine_supplies_history_and_filter_applies(session_cfg, cal):
    """Qua BacktestEngine: ngày đủ lịch sử + biên vừa ⇒ có lệnh; ngày đầu (chưa có lịch sử) ⇒ không."""
    from backtest.engine import BacktestEngine
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg, max_trades_per_day=5)
    s._data["strategy"] = {"orb": {"range_minutes": 15, "reward_risk": 1.5, "max_trades_per_day": 1, "qty": 1,
                                   "range_filter": {"enabled": True, "adr_days": 2, "min_ratio": 0.1,
                                                    "max_ratio": 0.6}}}
    s._data["session"]["continuous_am_start"] = session_cfg["continuous_am_start"]
    days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
    bars = []
    for d in days:
        bars += [Bar(SYM, b.start.replace(year=d.year, month=d.month, day=d.day), b.open, b.high, b.low, b.close, 10)
                 for b in range_bars(low=1350.0, high=1352.0)]
        t = datetime.combine(d, time(9, 15))
        bars += [Bar(SYM, t, 1352.0, 1353.2, 1351.9, 1353.0, 10)]
        bars += [Bar(SYM, t + timedelta(minutes=i), 1353.0, 1353.1, 1352.9, 1353.0, 10) for i in range(1, 6)]
        bars += [Bar(SYM, datetime.combine(d, time(13, 0)), 1345.0, 1360.0, 1345.0, 1350.0, 10)]   # biên độ ngày 15
    strat = OpeningRangeBreakout.from_settings(s)
    res = BacktestEngine(s, strat, calendar=cal).run_bars(sorted(bars, key=lambda b: b.start))
    entry_days = sorted({ts.date() for ts, sig in res.signals if sig.action is SignalAction.ENTER_LONG})
    assert entry_days == [date(2026, 11, 4)]                         # 2/11, 3/11: chưa đủ 2 phiên lịch sử


def test_repo_settings_have_filter_off():
    from core.settings import load_settings
    rf = load_settings().get("strategy.orb.range_filter")
    assert rf["enabled"] is False and rf["adr_days"] and rf["min_ratio"] < rf["max_ratio"]


def test_robustness_perturbs_filter_params_only_when_enabled():
    from backtest.robustness import params_for
    assert params_for(settings()) == ("strategy.orb.range_minutes", "strategy.orb.reward_risk",
                                      "strategy.orb.max_trades_per_day")
    assert params_for(settings(FILTER))[-3:] == ("strategy.orb.range_filter.adr_days",
                                                 "strategy.orb.range_filter.min_ratio",
                                                 "strategy.orb.range_filter.max_ratio")


def test_today_bars_in_history_are_ignored():
    """Phòng xa: nếu lịch sử lẫn bar hôm nay (biên độ lớn) thì không được tính vào ADR."""
    strat = OpeningRangeBreakout.from_settings(settings(FILTER))
    leak = tuple(hist_day(DAY, 1300.0, 1400.0))                     # hôm nay, biên độ 100
    assert len(run_day(strat, history(rng=10.0) + leak, low=1350.0, high=1352.0)) == 1


def test_robustness_cli_uses_filter_params(monkeypatch, tmp_path):
    import backtest.robustness as R
    s = settings(FILTER)
    monkeypatch.setattr(R, "load_settings", lambda: s)
    monkeypatch.setattr(R, "BacktestEngine", lambda *a, **k: None)
    monkeypatch.setattr(R, "load_bars_parquet", lambda *a, **k: ([], 0))
    captured = {}

    def fake_run(settings, make, bars, split, params, **kw):
        captured["params"] = params
        raise SystemExit(0)
    monkeypatch.setattr(R, "run_robustness", fake_run)
    with pytest.raises(SystemExit):
        R.main(["--bars", str(tmp_path), "--split", "2026-11-01", "--n-trials", "2"])
    assert captured["params"] == R.params_for(s) and "strategy.orb.range_filter.max_ratio" in captured["params"]
