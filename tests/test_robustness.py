"""Test cho backtest/robustness.py (PROMPTS P3.3) — viết TRƯỚC khi implement.

Luật kết luận được chốt TRONG CODE trước khi có dữ liệu thật, để không ai chỉnh luật sau khi
thấy kết quả.
"""
from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from backtest.robustness import (CHUA_DU, CO_EDGE, KHONG_EDGE, VerdictRules, count_logged_trials,
                                 perturbations, render_html, run_robustness, verdict,
                                 with_overrides)
from core import contracts
from core.models import Bar
from core.settings import Settings
from strategy.baseline_orb import OpeningRangeBreakout
from tests.test_backtest_engine import bt_settings

ORB_PARAMS = ["strategy.orb.range_minutes", "strategy.orb.reward_risk",
              "strategy.orb.max_trades_per_day"]


def orb_settings(session_cfg) -> Settings:
    s = bt_settings(session_cfg)
    s.raw["strategy"] = {"orb": {"range_minutes": 15, "reward_risk": 1.5,
                                 "max_trades_per_day": 2, "qty": 1}}
    return s


def trading_days(cal, start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if cal.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def day_bars(d: date, symbol: str, rng, planted: bool) -> list[Bar]:
    """09:00–10:29 và 14:20–14:29. planted=True: sau 09:15 giá chạy một mạch 25 điểm theo chiều phá biên."""
    t0 = datetime.combine(d, time(9, 0))
    p = 1350.0
    out = []
    direction = 1 if rng.random() < 0.5 else -1
    for i in range(90):
        if i < 15:
            step = float(rng.choice([-2, -1, 1, 2])) / 10           # dựng biên ~±1 điểm
            if abs(p + step - 1350.0) > 1.2:
                step = -step
        elif planted and i < 45:
            step = direction * (0.9 if i < 44 else 0.0)            # xu hướng mạnh
        else:
            step = float(rng.choice([-3, -1, 1, 3])) / 10 if not planted else 0.0
        o, c = p, round(p + step, 1)
        out.append(Bar(symbol, t0 + timedelta(minutes=i), o, round(max(o, c) + 0.1, 1),
                       round(min(o, c) - 0.1, 1), c, 10))
        p = c
    t1 = datetime.combine(d, time(14, 20))                     # để risk flatten 14:25 chạy được
    out += [Bar(symbol, t1 + timedelta(minutes=i), p, p, p, p, 10) for i in range(10)]
    return out


def dataset(cal, n_days: int, planted: bool, seed=0, reliable=True) -> list[Bar]:
    rng = np.random.default_rng(seed)
    bars = []
    for d in trading_days(cal, date(2026, 9, 7), n_days):
        sym = contracts.trading_contract(d, cal, 1)
        for b in day_bars(d, sym, rng, planted):
            bars.append(b if reliable else Bar(b.symbol, b.start, b.open, b.high, b.low, b.close,
                                               0, 60, False))
    return bars


# ------------------------------------------------------------- tham số ±20% ----

class TestPerturbations:
    def test_float(self):
        assert perturbations(1.5, integer=False) == pytest.approx([1.2, 1.8])

    def test_int(self):
        assert perturbations(15, integer=True) == [12, 18]

    def test_small_int_moves_at_least_one_step(self):
        assert perturbations(2, integer=True) == [1, 3]

    def test_int_never_below_minimum(self):
        assert perturbations(1, integer=True) == [2]

    def test_with_overrides_does_not_mutate(self, session_cfg):
        s = orb_settings(session_cfg)
        s2 = with_overrides(s, {"strategy.orb.reward_risk": 1.8, "backtest.slippage_ticks": 2})
        assert s2.get("strategy.orb.reward_risk") == 1.8 and s2.get("backtest.slippage_ticks") == 2
        assert s.get("strategy.orb.reward_risk") == 1.5 and s.get("backtest.slippage_ticks") == 1

    def test_override_unknown_key_rejected(self, session_cfg):
        with pytest.raises(KeyError):
            with_overrides(orb_settings(session_cfg), {"strategy.orb.typo": 1})


def test_count_logged_trials(tmp_path):
    p = tmp_path / "log.md"
    p.write_text("# x\n\n| Ngày | a |\n|---|---|\n| 2026-11-08 | ORB #1 |\n| 2026-11-09 | ORB #2 |\n",
                 encoding="utf-8")
    assert count_logged_trials(p) == 2
    assert count_logged_trials(tmp_path / "missing.md") == 1


# ------------------------------------------------------------ luật kết luận ----

def summary(**over):
    s = {"data_reliable": True,
         "oos": {"n_trades": 60, "pnl_vnd": 5_000_000, "passed": True, "reasons": []},
         "oos_slippage_stress_pnl_vnd": 2_000_000,
         "sensitivity_positive_frac": 1.0,
         "folds_positive_frac": 0.75}
    s.update(over)
    return s


class TestVerdict:
    def test_all_good_is_edge(self):
        label, reasons = verdict(summary())
        assert label == CO_EDGE and reasons

    def test_snapshot_data_never_concludes_edge(self):
        assert verdict(summary(data_reliable=False))[0] == CHUA_DU

    def test_too_few_oos_trades(self):
        s = summary(oos={"n_trades": 12, "pnl_vnd": 9e6, "passed": False, "reasons": []})
        assert verdict(s)[0] == CHUA_DU

    def test_negative_oos_is_no_edge(self):
        s = summary(oos={"n_trades": 60, "pnl_vnd": -1, "passed": True, "reasons": []})
        label, reasons = verdict(s)
        assert label == KHONG_EDGE and reasons == ["P&L out-of-sample sau phí = -1 VNĐ ≤ 0."]

    def test_failed_validation_is_no_edge(self):
        s = summary(oos={"n_trades": 60, "pnl_vnd": 1e6, "passed": False, "reasons": ["p 0.4"]})
        assert verdict(s)[0] == KHONG_EDGE

    def test_dies_with_two_tick_slippage(self):
        assert verdict(summary(oos_slippage_stress_pnl_vnd=-10))[0] == KHONG_EDGE

    def test_fragile_parameters(self):
        assert verdict(summary(sensitivity_positive_frac=0.5))[0] == KHONG_EDGE

    def test_walk_forward_inconsistent(self):
        assert verdict(summary(folds_positive_frac=0.25))[0] == KHONG_EDGE

    def test_rules_are_explicit_constants(self):
        r = VerdictRules()
        assert r.min_oos_trades == 30 and r.sensitivity_min_positive_frac == 0.75
        assert r.folds_min_positive_frac == 0.5


# -------------------------------------------------------------- chạy thật ----

def make_orb(settings):
    return OpeningRangeBreakout.from_settings(settings)


@pytest.fixture(scope="module")
def planted_result(request):
    from core.calendar import TradingCalendar
    cal = TradingCalendar.from_dates([date(2026, 9, 2), date(2027, 1, 1)])
    cfg = {"pre_open_start": "08:30", "ato_start": "08:45", "continuous_am_start": "09:00",
           "lunch_start": "11:30", "continuous_pm_start": "13:00", "atc_start": "14:30",
           "close": "14:45"}
    bars = dataset(cal, 44, planted=True, seed=1)
    split = sorted({b.start.date() for b in bars})[22]
    return run_robustness(orb_settings(cfg), make_orb, bars, split, ORB_PARAMS, folds=4,
                          n_trials=1, calendar=cal), split


class TestRunRobustness:
    def test_planted_edge_is_found(self, planted_result):
        rob, split = planted_result
        assert rob["verdict"] == CO_EDGE, rob["verdict_reasons"]
        assert rob["sections"]["out_of_sample"]["metrics"]["n_trades"] >= 30
        assert all(t.entry_ts.date() >= split for t in rob["sections"]["out_of_sample"]["trades"])

    def test_structure(self, planted_result):
        rob, _ = planted_result
        assert len(rob["folds"]) == 4
        assert [v["param"] for v in rob["sensitivity"]] == [
            "strategy.orb.range_minutes", "strategy.orb.range_minutes",
            "strategy.orb.reward_risk", "strategy.orb.reward_risk",
            "strategy.orb.max_trades_per_day", "strategy.orb.max_trades_per_day"]
        assert [v["value"] for v in rob["sensitivity"]][:2] == [12, 18]
        assert rob["slippage_stress_ticks"] == 2
        oos = rob["sections"]["out_of_sample"]["metrics"]["total_pnl_vnd"]
        slip = rob["slippage_sections"]["out_of_sample"]["metrics"]["total_pnl_vnd"]
        assert slip < oos                                      # trượt nhiều hơn ⇒ lãi ít hơn
        # độ nhạy chỉ chạy trên in-sample: không lệnh nào của các biến thể nằm trong OOS
        split = rob["split_date"]
        assert all(t.entry_ts.date() < split for v in rob["sensitivity"] for t in v["trades"])

    def test_random_data_has_no_edge(self, session_cfg, cal):
        bars = dataset(cal, 30, planted=False, seed=5)
        split = sorted({b.start.date() for b in bars})[15]
        rob = run_robustness(orb_settings(session_cfg), make_orb, bars, split, ORB_PARAMS,
                             folds=3, n_trials=1, calendar=cal)
        assert rob["verdict"] != CO_EDGE

    def test_snapshot_data_cannot_conclude(self, session_cfg, cal):
        bars = dataset(cal, 30, planted=True, seed=1, reliable=False)
        split = sorted({b.start.date() for b in bars})[15]
        rob = run_robustness(orb_settings(session_cfg), make_orb, bars, split, ORB_PARAMS[:1],
                             folds=3, n_trials=1, calendar=cal)
        assert rob["verdict"] == CHUA_DU
        assert rob["summary"]["data_reliable"] is False
        assert "snapshot" in rob["verdict_reasons"][0]

    def test_html(self, planted_result):
        rob, _ = planted_result
        html = render_html(rob, title="ORB")
        for needle in ("Kết luận", CO_EDGE, "Out-of-sample", "Walk-forward", "Độ nhạy ±20%",
                       "Trượt giá 2 tick", "range_minutes", "luật kết luận"):
            assert needle in html, needle


# ------------------------------------------------------------------ luật dữ liệu ≤ 1% (người dùng chọn 09/11/2026) ----

class TestDataReliableFraction:
    """Dữ liệu thật (khớp lệnh DNSE) có vài phút ATO/ATC thiếu lệnh (~0,4% nến) ⇒ luật cũ "0 nến" luôn ra CHƯA ĐỦ.
    Người dùng chọn (TRƯỚC khi xem kết quả): tin được khi tỉ lệ nến volume không tin được ≤ 1%."""

    def test_rule_constant(self):
        assert VerdictRules().max_unreliable_volume_frac == 0.01

    @pytest.mark.parametrize("bad,total,ok", [(0, 1000, True), (4, 1000, True), (10, 1000, True), (11, 1000, False),
                                              (1000, 1000, False), (0, 0, False)])
    def test_data_reliable_fraction(self, bad, total, ok):
        from backtest.robustness import data_reliable
        assert data_reliable({"unreliable_volume_bars": bad, "n_bars": total}) is ok

    def test_small_fraction_does_not_block_verdict(self, session_cfg, cal):
        bars = dataset(cal, 44, planted=True, seed=1)
        n_bad = len(bars) // 300                                   # ~0,33% nến đánh dấu
        bars = [b if i >= n_bad else Bar(b.symbol, b.start, b.open, b.high, b.low, b.close, b.volume, b.interval_s,
                                         False) for i, b in enumerate(bars)]
        split = sorted({b.start.date() for b in bars})[22]
        rob = run_robustness(orb_settings(session_cfg), make_orb, bars, split, ORB_PARAMS[:1], folds=4, n_trials=1,
                             calendar=cal)
        assert rob["summary"]["data_reliable"] is True


@pytest.mark.parametrize("bad,expect", [(4, "dưới ngưỡng"), (500, "không dùng để kết luận")])
def test_report_warning_follows_threshold(bad, expect):
    from backtest.report import build_report
    from tests.test_report_capital import Result
    r = Result([])
    r.data_quality = {"unreliable_volume_bars": bad, "n_bars": 1000}
    rep = build_report(r)
    assert any(expect in w for w in rep["warnings"])
