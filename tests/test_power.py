"""Lực thống kê của kiểm định edge (`backtest/power.py`) — viết TRƯỚC khi implement.

Câu hỏi module trả lời TRƯỚC khi đăng ký một ý tưởng: "nếu ý tưởng có lợi thế thật `edge` điểm/lệnh sau phí, với
`n_trades` lệnh OOS gộp và `n_trials` cấu hình đã thử, kiểm định đang dùng (p sign-flip × n_trials < 0,1 —
`backtest.pooled`) bắt được nó với xác suất bao nhiêu?". Lực thấp ⇒ thử cũng không kết luận được ⇒ không đăng ký.

- Công thức chuẩn (xấp xỉ): lực = Φ(edge/sd × √n − z₁₋α), α = 0,1 / n_trials (Bonferroni, một phía).
- Mô phỏng: lấy mẫu lại từ P&L tham chiếu ĐÃ TRỪ TRUNG BÌNH (chỉ mượn độ phân tán, không nhìn edge của chiến lược
  tham chiếu), cộng `edge`, rồi chạy ĐÚNG `monte_carlo_pvalue` của repo.
- p nhỏ nhất của kiểm định = 1/(mc_sims+1): n_trials đủ lớn thì KHÔNG BAO GIỜ đạt, dù edge lớn cỡ nào.
"""
import numpy as np
import pytest

from backtest.power import (DU_LUC, KHONG_DU, PowerRules, alpha_per_test, main, min_detectable_edge, power_gate,
                            power_normal, reachable, reference_pnls, render, simulated_power, trades_needed)

FAST = PowerRules(mc_sims=400, n_rep=300)


def normal_pnls(n=400, sd=10.0, seed=1):
    return np.random.default_rng(seed).normal(0.0, sd, n)


# ------------------------------------------------------------- mức ý nghĩa ----
def test_alpha_is_split_across_trials():
    assert alpha_per_test(1) == pytest.approx(0.10)
    assert alpha_per_test(10) == pytest.approx(0.01)
    assert alpha_per_test(0) == pytest.approx(0.10)      # như apply_selection_penalty: n_trials ≤ 1 không hiệu chỉnh


def test_reachable_boundary_follows_smallest_possible_p():
    # mc_sims = 1000 ⇒ p nhỏ nhất 1/1001; cần p × n_trials < 0,1
    assert reachable(100) is True        # 100/1001 = 0,0999 < 0,1
    assert reachable(101) is False       # 101/1001 = 0,1009 ≥ 0,1
    assert reachable(101, PowerRules(mc_sims=5000)) is True
    assert reachable(100, PowerRules(mc_sims=999)) is False   # đúng bằng ngưỡng: p × n_trials = 0,1 KHÔNG < 0,1


# ------------------------------------------------------- công thức chuẩn ----
def test_power_at_zero_edge_equals_alpha():
    assert power_normal(0.0, 10.0, 80, 0.05) == pytest.approx(0.05, abs=1e-9)


def test_power_known_value():
    # edge/sd × √n = 0,25 × 10 = 2,5; z₀.₉₅ = 1,6449 ⇒ Φ(0,8551) = 0,8038
    assert power_normal(2.5, 10.0, 100, 0.05) == pytest.approx(0.8038, abs=5e-4)


def test_power_monotone():
    assert power_normal(1.0, 10.0, 200, 0.05) > power_normal(1.0, 10.0, 100, 0.05)     # nhiều lệnh hơn
    assert power_normal(2.0, 10.0, 100, 0.05) > power_normal(1.0, 10.0, 100, 0.05)     # edge lớn hơn
    assert power_normal(1.0, 5.0, 100, 0.05) > power_normal(1.0, 10.0, 100, 0.05)      # ít nhiễu hơn
    assert power_normal(1.0, 10.0, 100, 0.01) < power_normal(1.0, 10.0, 100, 0.05)     # α chặt hơn


def test_min_detectable_edge_known_value_and_inverse():
    mde = min_detectable_edge(10.0, 100, 0.05, 0.80)
    assert mde == pytest.approx(2.4865, abs=5e-4)                 # (1,6449 + 0,8416) × 10/√100
    assert power_normal(mde, 10.0, 100, 0.05) == pytest.approx(0.80, abs=1e-6)


def test_min_detectable_edge_shrinks_with_sqrt_n():
    assert min_detectable_edge(10.0, 400, 0.05, 0.8) == pytest.approx(min_detectable_edge(10.0, 100, 0.05, 0.8) / 2)


def test_trades_needed_is_smallest_n_reaching_power():
    n = trades_needed(1.0, 10.0, 0.05, 0.80)
    assert n == 619                                               # (2,4865 × 10)² = 618,3
    assert power_normal(1.0, 10.0, n, 0.05) >= 0.80
    assert power_normal(1.0, 10.0, n - 1, 0.05) < 0.80


def test_trades_needed_none_without_positive_edge():
    assert trades_needed(0.0, 10.0, 0.05, 0.8) is None
    assert trades_needed(-1.0, 10.0, 0.05, 0.8) is None


@pytest.mark.parametrize("bad", [dict(sd=0.0), dict(sd=-1.0), dict(n_trades=0), dict(alpha=0.0), dict(alpha=1.0)])
def test_power_normal_rejects_nonsense(bad):
    kw = dict(edge=1.0, sd=10.0, n_trades=50, alpha=0.05) | bad
    with pytest.raises(ValueError):
        power_normal(**kw)


# ------------------------------------------------------------- mô phỏng ----
def test_simulated_power_at_zero_edge_is_about_alpha():
    got = simulated_power(normal_pnls(), 0.0, 60, rules=FAST)
    assert 0.04 <= got <= 0.17                                    # α = 0,1; 300 lần lặp ⇒ sai số chuẩn ~0,017


def test_simulated_power_large_edge_near_one():
    assert simulated_power(normal_pnls(), 10.0, 60, rules=FAST) > 0.99


def test_simulated_power_agrees_with_formula_on_normal_pnls():
    pnls = normal_pnls()
    sim = simulated_power(pnls, 3.0, 60, rules=FAST)
    assert sim == pytest.approx(power_normal(3.0, float(pnls.std()), 60, 0.10), abs=0.08)


def test_simulated_power_ignores_reference_mean():
    """Chỉ mượn độ phân tán của chiến lược tham chiếu: nó lãi hay lỗ không được đổi kết quả."""
    pnls = np.round(normal_pnls(), 1)
    assert simulated_power(pnls + 50.0, 2.0, 60, rules=FAST) == pytest.approx(
        simulated_power(pnls, 2.0, 60, rules=FAST), abs=0.011)


def test_simulated_power_deterministic_and_seeded():
    pnls = normal_pnls()
    assert simulated_power(pnls, 2.0, 60, rules=FAST) == simulated_power(pnls, 2.0, 60, rules=FAST)


def test_simulated_power_bonferroni_costs_power():
    pnls = normal_pnls()
    assert simulated_power(pnls, 3.0, 60, n_trials=10, rules=FAST) < simulated_power(pnls, 3.0, 60, rules=FAST)


def test_simulated_power_zero_when_unreachable():
    assert reachable(200, FAST) is False
    assert simulated_power(normal_pnls(), 50.0, 60, n_trials=200, rules=FAST) == 0.0


def test_simulated_power_none_without_dispersion():
    assert simulated_power([1.0, 1.0, 1.0, 1.0], 1.0, 30, rules=FAST) is None
    assert simulated_power([1.0, -1.0], 1.0, 30, rules=FAST) is None


def test_simulated_power_does_not_mutate_input():
    pnls = normal_pnls()
    before = pnls.copy()
    simulated_power(pnls, 1.0, 40, rules=FAST)
    assert np.array_equal(pnls, before)


# ------------------------------------------------------------------ cổng ----
def test_gate_passes_when_power_enough():
    g = power_gate(edge=5.0, n_trades=100, n_trials=1, sd=10.0)
    assert g["verdict"] == DU_LUC and g["reasons"] == []
    assert g["power"] == pytest.approx(power_normal(5.0, 10.0, 100, 0.10))
    assert g["method"] == "normal"


def test_gate_fails_on_low_power_and_says_what_would_be_enough():
    g = power_gate(edge=1.0, n_trades=60, n_trials=10, sd=10.0)
    assert g["verdict"] == KHONG_DU
    assert g["alpha_per_test"] == pytest.approx(0.01)
    assert g["min_detectable_edge"] == pytest.approx(min_detectable_edge(10.0, 60, 0.01, 0.80))
    assert g["trades_needed"] == trades_needed(1.0, 10.0, 0.01, 0.80)
    text = " ".join(g["reasons"])
    assert "lực" in text and str(g["trades_needed"]) in text.replace(".", "")


def test_gate_power_boundary_is_inclusive():
    edge = min_detectable_edge(10.0, 100, 0.10, 0.80)
    assert power_gate(edge=edge * 1.0001, n_trades=100, n_trials=1, sd=10.0)["verdict"] == DU_LUC
    assert power_gate(edge=edge * 0.99, n_trades=100, n_trials=1, sd=10.0)["verdict"] == KHONG_DU


def test_gate_fails_below_min_trades_even_with_huge_edge():
    g = power_gate(edge=50.0, n_trades=29, n_trials=1, sd=10.0)
    assert g["verdict"] == KHONG_DU and any("30" in r for r in g["reasons"])
    assert power_gate(edge=50.0, n_trades=30, n_trials=1, sd=10.0)["verdict"] == DU_LUC


def test_gate_fails_when_test_can_never_pass():
    g = power_gate(edge=50.0, n_trades=500, n_trials=101, sd=10.0)
    assert g["verdict"] == KHONG_DU and g["reachable"] is False
    assert any("mc_sims" in r for r in g["reasons"])


def test_gate_uses_simulation_when_reference_pnls_given():
    pnls = normal_pnls()
    g = power_gate(edge=3.0, n_trades=60, n_trials=1, pnls=pnls, rules=FAST)
    assert g["method"] == "simulation"
    assert g["sd"] == pytest.approx(float(pnls.std()))
    assert g["power"] == simulated_power(pnls, 3.0, 60, rules=FAST)
    assert g["power_normal"] == pytest.approx(power_normal(3.0, float(pnls.std()), 60, 0.10))
    assert 0.0 <= g["false_positive_rate"] <= 0.2                  # tỉ lệ báo động giả đo được khi edge = 0
    assert g["n_reference"] == len(pnls)


def test_gate_table_lists_detectable_edge_per_trade_count():
    g = power_gate(edge=1.0, n_trades=60, n_trials=10, sd=10.0)
    ns = [row["n_trades"] for row in g["mde_table"]]
    assert ns == sorted(ns) and 60 in ns                           # luôn có dòng của chính n_trades
    for row in g["mde_table"]:
        assert row["min_detectable_edge"] == pytest.approx(min_detectable_edge(10.0, row["n_trades"], 0.01, 0.80))


@pytest.mark.parametrize("kw", [dict(edge=0.0, sd=10.0), dict(edge=-1.0, sd=10.0), dict(edge=1.0),
                                dict(edge=1.0, sd=0.0), dict(edge=1.0, sd=10.0, n_trials=0),
                                dict(edge=1.0, pnls=[1.0, 1.0, 1.0]), dict(edge=1.0, pnls=list(range(5)))])
def test_gate_rejects_bad_inputs(kw):
    with pytest.raises(ValueError):
        power_gate(**({"n_trades": 60, "n_trials": 1} | kw))


def test_render_simulation_line_uses_decimal_comma():
    g = power_gate(edge=3.0, n_trades=60, n_trials=1, pnls=normal_pnls(), rules=FAST)
    line = next(ln for ln in render(g).splitlines() if "mô phỏng" in ln)
    assert f"{g['power']:.0%}" in line and "." not in line.split("báo động giả")[1]


def test_render_shows_the_numbers_a_person_needs():
    g = power_gate(edge=1.0, n_trades=60, n_trials=10, sd=10.0)
    out = render(g, "Ý tưởng thử")
    assert "Ý tưởng thử" in out and KHONG_DU in out
    assert "60" in out and "10" in out and "0,01" in out            # số lệnh, n_trials, α mỗi phép thử
    assert f"{g['power']:.0%}" in out


# ------------------------------------------------------- P&L tham chiếu ----
def test_reference_pnls_takes_closed_trades_in_points():
    from datetime import datetime

    from backtest.report import TradeRecord

    def tr(day, pts, closed=True):
        ts = datetime(2026, 6, day, 9, 30)
        return TradeRecord("VN30F2606", "LONG", 1, ts, 1300.0, ts.replace(hour=14) if closed else None, 1301.0,
                           "exit" if closed else "open", pts + 0.47, 46700.0, pts * 100000.0, pts)

    trades = [tr(3, 1.5), tr(1, -2.0), tr(2, 0.25, closed=False)]
    assert reference_pnls(trades) == [-2.0, 1.5]                   # theo thời gian vào lệnh; bỏ lệnh còn mở


# -------------------------------------------------------------------- CLI ----
def test_cli_with_sd(capsys):
    assert main(["--edge", "1.0", "--n-trades", "60", "--sd", "10", "--n-trials", "10", "--title", "X"]) == 0
    out = capsys.readouterr().out
    assert KHONG_DU in out and "X" in out


def test_cli_needs_sd_or_reference(capsys):
    assert main(["--edge", "1.0", "--n-trades", "60", "--n-trials", "10"]) == 2
    assert "❌" in capsys.readouterr().out


def test_cli_rejects_bad_numbers(capsys):
    assert main(["--edge", "-1", "--n-trades", "60", "--sd", "10", "--n-trials", "3"]) == 2
    assert "❌" in capsys.readouterr().out
