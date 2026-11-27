"""Đo khả năng dự báo của MỘT tín hiệu trên toàn bộ nến (`backtest/signal_study.py`) — viết TRƯỚC khi implement.

Tách "tín hiệu có dự báo được không" khỏi "chiến lược có lãi không" (research/log.md, "Giai đoạn đo tín hiệu"):
- Tín hiệu = hàm thuần `(bars_today, history) -> số | None`, nhận ĐÚNG dữ liệu một chiến lược nhận từ engine
  (`StrategyContext`): nến đã đóng hôm nay gồm nến hiện tại + lịch sử back-adjust. Engine gọi ⇒ không nhìn được tương lai.
- Quan sát: tín hiệu ở nến i ⇒ vào ở giá MỞ nến i+1, ra ở giá ĐÓNG nến i+h cùng ngày (không dùng giá đóng nến tín hiệu).
- Thống kê theo NGÀY (các quan sát trong ngày chồng lấn, không độc lập): IC Spearman mỗi ngày, rồi kiểm định sign-flip
  trên chuỗi IC ngày theo CHIỀU đã đăng ký, nhân n_trials.
- Độ lớn kinh tế: lợi thế thô (điểm) khi |tín hiệu| ≥ ngưỡng đăng ký, so với chi phí một lượt.
"""
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from backtest.signal_study import (CHUA_DU, DU_LON, KHONG, NHO, Observation, SignalProbe, StudyRules, collect,
                                   daily_ic, forward_observations, main, placebo_signal, quantile_table, render,
                                   round_trip_cost_points, spearman, study, tradable_window, triggered_edge)
from core.clock import FakeClock, MarketClock
from core.models import Bar
from core.settings import Settings

SYM = "VN30F2611"
DAY = date(2026, 11, 9)
FAST = StudyRules(mc_sims=400)


def bar(day, hhmm, o, c, symbol=SYM):
    return Bar(symbol, datetime.combine(day, time.fromisoformat(hhmm)), o, max(o, c), min(o, c), c, 10)


def day_rows(day, opens_closes, values, start="09:00", symbol=SYM):
    t0 = datetime.combine(day, time.fromisoformat(start))
    rows = []
    for i, ((o, c), v) in enumerate(zip(opens_closes, values)):
        ts = t0 + timedelta(minutes=i)
        rows.append((Bar(symbol, ts, o, max(o, c), min(o, c), c, 10), v))
    return rows


def planted(n_days=60, per_day=100, rho=0.1, sd_fwd=2.0, seed=3, start=date(2026, 1, 5)):
    """Quan sát giả: tín hiệu tương quan `rho` với lợi suất kỳ sau."""
    rng = np.random.default_rng(seed)
    out = []
    for d in range(n_days):
        day = start + timedelta(days=d)
        fwd = rng.normal(0, sd_fwd, per_day)
        sig = rho * fwd / sd_fwd + np.sqrt(1 - rho ** 2) * rng.normal(0, 1, per_day)
        ts0 = datetime.combine(day, time(9, 15))
        out += [Observation(day, ts0 + timedelta(minutes=i), float(sig[i]), float(fwd[i])) for i in range(per_day)]
    return out


# ------------------------------------------------- quan sát kỳ sau ----
PRICES = [(100.0, 101.0), (101.5, 102.0), (102.0, 104.0), (104.5, 103.0), (103.0, 105.0)]


def test_forward_is_next_open_to_close_h_bars_later():
    rows = day_rows(DAY, PRICES, [1.0, 2.0, 3.0, 4.0, 5.0])
    obs = forward_observations(rows, horizon=1)
    assert [o.signal for o in obs] == [1.0, 2.0, 3.0, 4.0]                 # nến cuối không còn nến kế tiếp
    assert [o.fwd for o in obs] == pytest.approx([0.5, 2.0, -1.5, 2.0])    # đóng[i+1] − mở[i+1]
    obs2 = forward_observations(rows, horizon=2)
    assert [o.fwd for o in obs2] == pytest.approx([104.0 - 101.5, 103.0 - 102.0, 105.0 - 104.5])
    assert obs[0].day == DAY and obs[0].ts == rows[0][0].start             # ts = nến SINH tín hiệu


def test_forward_never_uses_signal_bars_own_close():
    spike = [(100.0, 100.0), (100.0, 999.0), (100.0, 100.0), (100.0, 100.0)]
    obs = forward_observations(day_rows(DAY, spike, [0.0, 1.0, 0.0, 0.0]), horizon=1)
    assert obs[1].signal == 1.0 and obs[1].fwd == 0.0                      # 999 là giá đóng của chính nến tín hiệu


def test_forward_skips_missing_and_non_finite_signals():
    rows = day_rows(DAY, PRICES, [None, float("nan"), 3.0, float("inf"), 5.0])
    assert [o.signal for o in forward_observations(rows, horizon=1)] == [3.0]


def test_forward_does_not_cross_days():
    d2 = DAY + timedelta(days=1)
    rows = day_rows(DAY, PRICES[:2], [1.0, 2.0]) + day_rows(d2, PRICES[:3], [7.0, 8.0, 9.0])
    obs = forward_observations(rows, horizon=1)
    assert [(o.day, o.signal) for o in obs] == [(DAY, 1.0), (d2, 7.0), (d2, 8.0)]


def test_forward_time_filters_follow_risk_rules():
    # tín hiệu nến 09:13 ⇒ vào 09:14 (< 09:15: cấm); nến 09:14 ⇒ vào 09:15 (được)
    rows = day_rows(DAY, [(100.0, 100.0)] * 6, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], start="09:13")
    obs = forward_observations(rows, 1, entry_from=time(9, 15))
    assert [o.signal for o in obs] == [2.0, 3.0, 4.0, 5.0]
    # vào đúng 09:17 bị cấm khi last_entry_time = 09:17 (risk: now >= last_entry_time ⇒ từ chối)
    obs = forward_observations(rows, 1, entry_from=time(9, 15), entry_until=time(9, 17))
    assert [o.signal for o in obs] == [2.0, 3.0]
    # nến ra phải ĐÓNG trước/đúng flatten: nến 09:17 đóng 09:18 ⇒ exit_by 09:18 được, 09:17 thì không
    assert [o.signal for o in forward_observations(rows, 2, exit_by=time(9, 18))] == [1.0, 2.0, 3.0]
    assert [o.signal for o in forward_observations(rows, 2, exit_by=time(9, 17))] == [1.0, 2.0]


def test_forward_skip_days_and_bad_horizon():
    d2 = DAY + timedelta(days=1)
    rows = day_rows(DAY, PRICES, [1.0] * 5) + day_rows(d2, PRICES, [2.0] * 5)
    assert {o.day for o in forward_observations(rows, 1, skip_days={DAY})} == {d2}
    with pytest.raises(ValueError):
        forward_observations(rows, 0)


def test_forward_rejects_mixed_contracts_in_one_day():
    rows = day_rows(DAY, PRICES[:2], [1.0, 2.0]) + day_rows(DAY, PRICES[:2], [1.0, 2.0], start="09:02",
                                                           symbol="VN30F2612")
    with pytest.raises(ValueError):
        forward_observations(rows, 1)


# ------------------------------------------------------------ Spearman ----
def test_spearman_basic_and_ties():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4], [1, 100, 2, 3]) == pytest.approx(0.4)     # hạng [1,4,2,3]: 1 − 6×6/(4×15)
    # hạng trung bình khi hoà: x = [1, 1, 2] ⇒ [1.5, 1.5, 3]
    assert spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(np.corrcoef([1.5, 1.5, 3], [1, 2, 3])[0, 1])
    # hoà ở giữa: hạng trung bình [1, 2.5, 2.5, 4, 5] (không phải hạng lớn nhất [1, 3, 3, 4, 5])
    assert spearman([1, 2, 2, 3, 10], [1, 2, 3, 5, 4]) == pytest.approx(
        np.corrcoef([1, 2.5, 2.5, 4, 5], [1, 2, 3, 5, 4])[0, 1])


def test_spearman_none_when_undefined():
    assert spearman([1, 1, 1], [1, 2, 3]) is None
    assert spearman([1, 2], [1, 2]) is None


def test_daily_ic_skips_thin_days():
    obs = planted(n_days=3, per_day=30) + [Observation(date(2026, 3, 2), datetime(2026, 3, 2, 9, 15), 1.0, 1.0)] * 5
    ics = daily_ic(obs, min_obs=20)
    assert len(ics) == 3 and all(-1 <= v <= 1 for _, v in ics)
    assert [d for d, _ in ics] == sorted(d for d, _ in ics)


def test_daily_ic_keeps_day_with_exactly_min_obs():
    obs = planted(n_days=1, per_day=20)
    assert len(daily_ic(obs, min_obs=20)) == 1 and daily_ic(obs, min_obs=21) == []


# -------------------------------------------------- lợi thế vượt ngưỡng ----
def test_triggered_edge_signs_by_signal_and_expected_direction():
    ts = datetime(2026, 1, 5, 9, 15)
    d = date(2026, 1, 5)
    obs = [Observation(d, ts, 2.0, 1.0), Observation(d, ts, -3.0, -4.0), Observation(d, ts, 0.5, 100.0),
           Observation(d, ts, 0.0, 9.0)]
    # |tín hiệu| ≥ 1: (+)×1 và (−)×(−4) ⇒ trung bình 2,5 khi kỳ vọng thuận chiều
    assert triggered_edge(obs, threshold=1.0, expected_sign=1) == [(d, pytest.approx(2.5), 2)]
    assert triggered_edge(obs, threshold=1.0, expected_sign=-1) == [(d, pytest.approx(-2.5), 2)]
    assert triggered_edge(obs, threshold=2.0, expected_sign=1)[0][2] == 2          # đúng bằng ngưỡng vẫn tính
    # ngưỡng 0: tín hiệu đúng bằng 0 không có chiều ⇒ không tính
    assert triggered_edge(obs, threshold=0.0, expected_sign=1)[0][2] == 3


def test_quantile_table_orders_buckets_by_signal():
    tab = quantile_table(planted(rho=0.3), q=5)
    assert len(tab) == 5 and sum(r["n"] for r in tab) == 6000
    assert [r["mean_signal"] for r in tab] == sorted(r["mean_signal"] for r in tab)
    assert tab[-1]["mean_fwd"] > tab[0]["mean_fwd"]                         # tín hiệu cao ⇒ lợi suất kỳ sau cao hơn


# ------------------------------------------------------------- kết luận ----
def test_study_detects_planted_signal_but_too_small_to_pay_costs():
    r = study(planted(), expected_sign=1, n_trials=10, cost_points=0.87, rules=FAST)
    assert r["n_days"] == 60 and r["n_obs"] == 6000
    assert r["mean_ic"] == pytest.approx(0.1, abs=0.03)
    assert r["p_adj"] < 0.10 and r["walk_forward"]["positive_frac"] >= 0.5
    assert 0 < r["gross_edge"] < 0.87 and r["net_edge"] == pytest.approx(r["gross_edge"] - 0.87)
    assert r["verdict"] == NHO


def test_study_large_enough_when_edge_beats_cost():
    r = study(planted(), expected_sign=1, n_trials=10, cost_points=0.05, rules=FAST)
    assert r["verdict"] == DU_LON and r["net_edge"] > 0


def test_study_threshold_raises_edge_per_trigger():
    low = study(planted(), expected_sign=1, n_trials=1, cost_points=0.0, rules=FAST)
    high = study(planted(), expected_sign=1, n_trials=1, cost_points=0.0, threshold=1.5, rules=FAST)
    assert high["gross_edge"] > low["gross_edge"]
    assert high["triggers_per_day"] < low["triggers_per_day"]


def test_gross_edge_does_not_credit_market_drift_to_a_one_sided_signal():
    """Tín hiệu luôn dương trên thị trường trôi lên: 'luôn mua' ăn theo trôi giá, không phải nhờ dự báo."""
    rng = np.random.default_rng(2)
    obs = []
    for d in range(40):
        day = date(2026, 1, 5) + timedelta(days=d)
        ts = datetime.combine(day, time(9, 15))
        obs += [Observation(day, ts, 1.0, 1.0 + float(x)) for x in rng.normal(0, 2, 50)]
    r = study(obs, expected_sign=1, n_trials=1, cost_points=0.0, rules=FAST)
    assert r["drift"] == pytest.approx(1.0, abs=0.15)                       # trôi giá trung bình mỗi kỳ
    assert r["gross_edge"] == pytest.approx(0.0, abs=1e-9)                  # đã trừ trôi giá từng ngày
    assert r["verdict"] != DU_LON


def test_drift_neutral_keeps_edge_of_a_two_sided_signal():
    base = planted(rho=0.3)
    shifted = [Observation(o.day, o.ts, o.signal, o.fwd + 5.0) for o in base]
    a = study(base, expected_sign=1, n_trials=1, cost_points=0.0, rules=FAST)
    b = study(shifted, expected_sign=1, n_trials=1, cost_points=0.0, rules=FAST)
    assert b["gross_edge"] == pytest.approx(a["gross_edge"], abs=1e-9) and b["drift"] == pytest.approx(a["drift"] + 5.0)


def test_study_wrong_direction_is_no_signal():
    r = study(planted(), expected_sign=-1, n_trials=1, cost_points=0.0, rules=FAST)
    assert r["verdict"] == KHONG and r["mean_ic_signed"] < 0
    assert any("chiều" in x for x in r["reasons"])


def test_study_noise_is_no_signal():
    r = study(planted(rho=0.0), expected_sign=1, n_trials=1, cost_points=0.0, rules=FAST)
    assert r["verdict"] == KHONG


def test_study_bonferroni_can_kill_a_marginal_signal():
    obs = planted(rho=0.022, seed=11)
    one = study(obs, expected_sign=1, n_trials=1, cost_points=0.0, rules=FAST)
    many = study(obs, expected_sign=1, n_trials=50, cost_points=0.0, rules=FAST)
    assert one["p"] == many["p"] and many["p_adj"] == pytest.approx(min(1.0, one["p"] * 50))
    assert one["p_adj"] < 0.10 <= many["p_adj"]
    assert one["verdict"] != KHONG and many["verdict"] == KHONG


def test_study_needs_enough_days():
    r = study(planted(n_days=29), expected_sign=1, n_trials=1, cost_points=0.0, rules=FAST)
    assert r["verdict"] == CHUA_DU and any("29" in x for x in r["reasons"])
    assert study(planted(n_days=30, rho=0.3), expected_sign=1, n_trials=1, cost_points=0.0,
                 rules=FAST)["verdict"] == DU_LON


def test_study_needs_enough_trigger_days_for_the_money_question():
    r = study(planted(), expected_sign=1, n_trials=1, cost_points=0.0, threshold=50.0, rules=FAST)
    assert r["trigger_days"] == 0 and r["gross_edge"] is None
    assert r["verdict"] == CHUA_DU and any("ngưỡng" in x for x in r["reasons"])


def test_study_fails_when_signal_lives_in_one_period_only():
    early = planted(n_days=15, per_day=400, rho=0.3, seed=5)
    late = planted(n_days=45, per_day=400, rho=-0.03, seed=6, start=date(2026, 3, 1))
    r = study(early + late, expected_sign=1, n_trials=1, cost_points=0.0, rules=FAST)
    assert r["p_adj"] < 0.10                                                # tổng thể vẫn "có ý nghĩa"
    assert r["walk_forward"]["positive_frac"] < 0.5
    assert r["verdict"] == KHONG and any("đoạn" in x for x in r["reasons"])


def test_study_unreachable_test_is_reported():
    r = study(planted(rho=0.5), expected_sign=1, n_trials=101, cost_points=0.0)
    assert r["verdict"] == KHONG and any("mc_sims" in x for x in r["reasons"])


@pytest.mark.parametrize("kw", [dict(expected_sign=0), dict(expected_sign=2), dict(n_trials=0),
                                dict(cost_points=-0.1), dict(threshold=-1.0)])
def test_study_rejects_bad_inputs(kw):
    with pytest.raises(ValueError):
        study(planted(n_days=5), **({"expected_sign": 1, "n_trials": 1, "cost_points": 0.5} | kw))


def test_study_handles_no_observations():
    r = study([], expected_sign=1, n_trials=1, cost_points=0.5)
    assert r["verdict"] == CHUA_DU and r["n_obs"] == 0


def test_render_has_the_numbers():
    r = study(planted(), expected_sign=1, n_trials=10, cost_points=0.87, threshold=0.5, rules=FAST)
    out = render(r, "Tín hiệu thử", horizon=5)
    for needle in ("Tín hiệu thử", "5 nến", "60 ngày", "0,87", "× 10", NHO, "0,5", "trôi giá"):
        assert needle in out, needle


# ------------------------------------------------------ chi phí, cửa sổ ----
def cfg(session_cfg, **bt):
    backtest = {"slippage_ticks": 1, "synthetic_spread_ticks": 1} | bt
    return Settings({
        "contract": {"multiplier_vnd_per_point": 100000, "tick_size_points": 0.1,
                     "roll_days_before_expiry": 1, "roll_gap_median_minutes": 1},
        "session": session_cfg,
        "risk": dict(max_contracts=1, max_daily_loss_points=50, max_trades_per_day=5, max_consecutive_losses=3,
                     no_entry_open_minutes=15, last_entry_time="14:15", flatten_time="14:25",
                     trade_on_expiry_day=False, broker_timeout_seconds=10, stop_grace_seconds=5,
                     flatten_on_feed_silence_seconds=30),
        "brokers": {"primary": "dnse", "backup": "ssi"},
        "fees": {"broker_fee_vnd_per_contract": 1000, "exchange_fee_vnd_per_contract": 2700,
                 "clearing_fee_vnd_per_contract": 2550, "tax_vnd_per_contract": 17100},
        "backtest": backtest,
        "paper": {"latency_ms": 300, "latency_jitter_ms": 0, "seed": 0},
    })


def test_round_trip_cost_matches_engine_conventions(session_cfg):
    # phí 23.350đ × 2 chiều = 0,467 điểm; mỗi chiều: trượt 1 bước + nửa spread làm tròn lên 1 bước = 0,2 điểm
    assert round_trip_cost_points(cfg(session_cfg)) == pytest.approx(0.467 + 0.4)
    assert round_trip_cost_points(cfg(session_cfg, slippage_ticks=2)) == pytest.approx(0.467 + 0.6)
    assert round_trip_cost_points(cfg(session_cfg, synthetic_spread_ticks=0)) == pytest.approx(0.467 + 0.2)
    assert round_trip_cost_points(cfg(session_cfg, synthetic_spread_ticks=3)) == pytest.approx(0.467 + 0.6)


def test_tradable_window_from_settings(session_cfg):
    assert tradable_window(cfg(session_cfg)) == (time(9, 15), time(14, 15), time(14, 25))


# ----------------------------------------------------- chạy qua engine ----
def flat_day(day, n, symbol, start="09:00", price=1350.0):
    t0 = datetime.combine(day, time.fromisoformat(start))
    return [Bar(symbol, t0 + timedelta(minutes=i), price + i, price + i + 0.5, price + i, price + i + 0.5, 10)
            for i in range(n)]


def test_probe_gets_exactly_what_a_strategy_gets(session_cfg, cal):
    from backtest.engine import BacktestEngine
    seen = []

    def fn(bars_today, history):
        seen.append((len(bars_today), bars_today[-1].start, len(history)))
        return float(len(bars_today))

    probe = SignalProbe(fn)
    bars = flat_day(DAY, 4, SYM) + flat_day(DAY + timedelta(days=1), 3, SYM)
    res = BacktestEngine(cfg(session_cfg), probe, calendar=cal).run_bars(bars)
    assert res.trades == [] and res.signals == []                          # đầu dò không bao giờ ra lệnh
    assert [v for _, v in probe.rows] == [1.0, 2.0, 3.0, 4.0, 1.0, 2.0, 3.0]
    assert [b.start for b, _ in probe.rows] == [b.start for b in bars]
    assert seen[3] == (4, bars[3].start, 0)                                # nến hiện tại là nến cuối của bars_today


def test_probe_passes_history_days_to_engine(session_cfg, cal):
    from backtest.engine import BacktestEngine

    def fn(bars_today, history):
        return float(len({b.start.date() for b in history}))

    fn.history_days = 1
    probe = SignalProbe(fn)
    assert probe.history_days == 1
    bars = flat_day(DAY, 3, SYM) + flat_day(DAY + timedelta(days=1), 3, SYM)
    BacktestEngine(cfg(session_cfg), probe, calendar=cal).run_bars(bars)
    assert [v for _, v in probe.rows] == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]


def test_collect_applies_tradable_window_and_skips_expiry_day(session_cfg, cal):
    s = cfg(session_cfg)
    mclock = MarketClock.from_settings(s, clock=FakeClock(datetime(2000, 1, 3)), calendar=cal)
    expiry = date(2026, 11, 19)
    assert mclock.is_expiry_day(expiry)
    days = [date(2026, 11, 17), expiry]
    bars = []
    for d in days:
        bars += flat_day(d, 20, mclock.trading_contract(d), start="09:10")   # 09:10 … 09:29
    obs, info = collect(s, lambda today, hist: 1.0, bars, horizon=2, calendar=cal)
    assert {o.day for o in obs} == {date(2026, 11, 17)}                     # ngày đáo hạn không mở vị thế
    assert min(o.ts for o in obs).time() == time(9, 14)                     # vào 09:15 là sớm nhất
    assert info["days"] == 2 and info["skipped_expiry_days"] == 1 and info["n_bars"] == 40
    assert all(o.fwd == pytest.approx(1.5) for o in obs)                    # mở[i+1] → đóng[i+2] = 1 + 0,5


def test_collect_from_date_keeps_earlier_bars_only_as_history(session_cfg, cal):
    s = cfg(session_cfg)
    bars = flat_day(DAY, 20, SYM, start="09:10") + flat_day(DAY + timedelta(days=1), 20, SYM, start="09:10")
    obs, _ = collect(s, lambda today, hist: 1.0, bars, horizon=1, calendar=cal, date_from=DAY + timedelta(days=1))
    assert {o.day for o in obs} == {DAY + timedelta(days=1)}


# -------------------------------------------------------------- giả dược ----
def test_placebo_is_deterministic_bounded_and_blind():
    b1, b2 = bar(DAY, "09:15", 100.0, 101.0), bar(DAY, "09:16", 101.0, 50.0)
    v1, v2 = placebo_signal([b1], ()), placebo_signal([b1, b2], ())
    assert v1 == placebo_signal([b1], ()) and v1 != v2 and -1 <= v1 <= 1 and -1 <= v2 <= 1
    assert v2 == placebo_signal([b1, bar(DAY, "09:16", 500.0, 900.0)], ())    # không phụ thuộc giá


def test_placebo_shows_no_signal_on_random_walk():
    rng = np.random.default_rng(0)
    rows = []
    for d in range(40):
        day = date(2026, 1, 5) + timedelta(days=d)
        px = 1300 + np.cumsum(rng.normal(0, 0.5, 80))
        pcs = [(float(px[i]), float(px[i + 1])) for i in range(79)]
        day_r = day_rows(day, pcs, [0.0] * 79)
        rows += [(b, placebo_signal([b], ())) for b, _ in day_r]
    r = study(forward_observations(rows, 5), expected_sign=1, n_trials=1, cost_points=0.87, rules=FAST)
    assert r["verdict"] == KHONG


# -------------------------------------------------------------------- CLI ----
def write_bars(tmp_path, bars):
    df = pd.DataFrame([{"symbol": b.symbol, "start": b.start, "open": b.open, "high": b.high, "low": b.low,
                        "close": b.close, "volume": b.volume} for b in bars])
    path = tmp_path / "bars.parquet"
    df.to_parquet(path)
    return path


def test_cli_runs_placebo_end_to_end(tmp_path, capsys):
    from core.settings import load_settings
    mclock = MarketClock.from_settings(load_settings(), clock=FakeClock(datetime(2000, 1, 3)))
    days = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    bars = []
    for d in days:
        bars += flat_day(d, 40, mclock.trading_contract(d), start="09:10")
    path = write_bars(tmp_path, bars)
    code = main(["--bars", str(path), "--signal", "backtest.signal_study:placebo_signal", "--horizon", "5",
                 "--expected-sign", "1", "--n-trials", "3", "--title", "Giả dược"])
    out = capsys.readouterr().out
    assert code == 0 and "Giả dược" in out and CHUA_DU in out and "3 ngày" in out


def test_cli_rejects_unknown_signal(tmp_path, capsys):
    path = write_bars(tmp_path, flat_day(date(2026, 6, 1), 5, "VN30F2606"))
    assert main(["--bars", str(path), "--signal", "backtest.signal_study:khong_co", "--horizon", "5",
                 "--expected-sign", "1"]) == 2
    assert "❌" in capsys.readouterr().out
