from datetime import datetime, timedelta

import numpy as np
import pytest

from backtest import validation as v


def _trades(pnls, start=datetime(2026, 1, 5, 9, 30)):
    return [{"pnl": p, "entry_ts": start + timedelta(days=i)} for i, p in enumerate(pnls)]


class TestPValue:
    def test_none_for_tiny_or_flat_samples(self):
        assert v.monte_carlo_pvalue([1.0, 2.0]) is None
        assert v.monte_carlo_pvalue([1.0, 1.0, 1.0]) is None          # độ lệch chuẩn 0
        assert v.monte_carlo_pvalue([0.0, 0.0, 0.0, 0.0]) is None

    def test_strong_edge_has_small_p(self):
        rng = np.random.default_rng(0)
        pnls = rng.normal(1.0, 1.0, 200)
        assert v.monte_carlo_pvalue(pnls) < 0.01

    def test_no_edge_has_large_p(self):
        rng = np.random.default_rng(1)
        pnls = rng.normal(0.0, 1.0, 200)
        assert v.monte_carlo_pvalue(pnls) > 0.05

    def test_deterministic_with_seed(self):
        pnls = [1.0, -0.5, 2.0, -1.0, 0.7, 0.3]
        assert v.monte_carlo_pvalue(pnls, seed=7) == v.monte_carlo_pvalue(pnls, seed=7)

    def test_p_never_zero(self):
        assert v.monte_carlo_pvalue([5.0] * 50 + [4.9], n_sim=200) > 0


class TestBootstrap:
    def test_ci_ordered_and_positive_for_edge(self):
        rng = np.random.default_rng(2)
        lo, hi = v.bootstrap_sharpe_ci(rng.normal(1.0, 1.0, 300))
        assert 0 < lo < hi

    def test_none_for_tiny_sample(self):
        assert v.bootstrap_sharpe_ci([1.0, 2.0]) is None


class TestWalkForward:
    def test_orders_by_time_not_input_order(self):
        trades = _trades([-1, -1, 5, 5])
        shuffled = [trades[2], trades[0], trades[3], trades[1]]
        wf = v.walk_forward_consistency(shuffled, n_folds=2)
        assert wf["fold_pnls"] == [-2.0, 10.0]
        assert wf["positive_frac"] == 0.5

    def test_none_when_fewer_trades_than_folds(self):
        assert v.walk_forward_consistency(_trades([1, 2, 3]), n_folds=4) is None


class TestValidateTrades:
    def test_good_strategy_passes(self):
        rng = np.random.default_rng(3)
        report = v.validate_trades(_trades(rng.normal(1.0, 1.0, 120)))
        assert report["passed"], report["reasons"]
        assert report["n_trades"] == 120

    def test_too_few_trades_never_passes(self):
        report = v.validate_trades(_trades([3.0, 2.0, 4.0, 3.5, 2.5]))
        assert not report["passed"]
        assert any("chưa đủ mẫu" in r for r in report["reasons"])

    def test_edge_concentrated_in_one_period_fails(self):
        pnls = [-0.2] * 90 + [8.0] * 30          # chỉ đoạn cuối lãi
        report = v.validate_trades(_trades(pnls))
        assert not report["passed"]
        assert any("dồn cục" in r for r in report["reasons"])

    def test_empty_trades_returns_report(self):
        report = v.validate_trades([])
        assert report["n_trades"] == 0 and not report["passed"]


class TestSelectionPenalty:
    def test_penalty_flips_marginal_winner_without_mutating(self):
        report = {"p_value_sharpe": 0.04, "passed": True, "reasons": []}
        out = v.apply_selection_penalty(report, n_trials=10)
        assert out["p_value_adj"] == pytest.approx(0.4)
        assert not out["passed"] and out["reasons"]
        assert report == {"p_value_sharpe": 0.04, "passed": True, "reasons": []}

    def test_penalty_never_loosens(self):
        report = {"p_value_sharpe": 0.001, "passed": False, "reasons": ["wf"]}
        out = v.apply_selection_penalty(report, n_trials=5)
        assert not out["passed"]

    def test_noop_for_single_trial(self):
        report = {"p_value_sharpe": 0.05, "passed": True, "reasons": []}
        assert v.apply_selection_penalty(report, 1) == report


class TestBarHighFirst:
    def test_nearest_extreme_first(self):
        assert v.bar_high_first(100.0, 100.5, 98.0) is True
        assert v.bar_high_first(100.0, 103.0, 99.8) is False

    def test_undecidable_returns_none(self):
        assert v.bar_high_first(100.0, 101.0, 99.0) is None      # cách đều
        assert v.bar_high_first(float("nan"), 101.0, 99.0) is None
        assert v.bar_high_first(None, 101.0, 99.0) is None
