"""Test cho backtest/engine.py, core/execution.py, backtest/report.py (PROMPTS P3.1).
Viết TRƯỚC khi implement.

Pipeline phải giống live: feed giả → strategy → risk_manager → paper broker.
Dữ liệu bar không có bid/ask ⇒ engine dựng tick giả: bid = giá − 1 bước, ask = giá + 1 bước (spread 1),
đường đi O → cực trị gần O → cực trị kia → C, mỗi cực trị lặp hai tick (giá chạm cực trị thì
"tick kế tiếp" vẫn ở cực trị ⇒ stop khớp bi quan). Bar không quyết được thứ tự cực trị ⇒ đi về
phía bất lợi cho vị thế đang giữ (SL thắng).
"""
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from backtest.engine import (BacktestEngine, bars_from_ticks, detect_lookahead, load_bars_parquet)
from backtest.report import TradeRecord, build_report, compute_metrics, render_html
from core.models import Bar, Signal, SignalAction, Tick
from core.settings import MissingSetting, Settings, load_settings
from risk.risk_manager import RejectCode

SYM = "VN30F2611"
DAY = date(2026, 11, 9)          # thứ Hai
DAY2 = date(2026, 11, 10)
FEE = 10_000                     # VNĐ / HĐ / chiều = 0,1 điểm


def bt_settings(session_cfg, **risk_over) -> Settings:
    risk = dict(max_contracts=1, max_daily_loss_points=50, max_trades_per_day=5,
                max_consecutive_losses=3, no_entry_open_minutes=15, last_entry_time="14:15",
                flatten_time="14:25", trade_on_expiry_day=False, broker_timeout_seconds=10,
                stop_grace_seconds=5, flatten_on_feed_silence_seconds=30)
    risk.update(risk_over)
    return Settings({
        "contract": {"multiplier_vnd_per_point": 100000, "tick_size_points": 0.1,
                     "roll_days_before_expiry": 1, "roll_gap_median_minutes": 1},
        "session": session_cfg,
        "risk": risk,
        "brokers": {"primary": "dnse", "backup": "ssi"},
        "fees": {"broker_fee_vnd_per_contract": FEE, "exchange_fee_vnd_per_contract": 0,
                 "clearing_fee_vnd_per_contract": 0, "tax_vnd_per_contract": 0},
        "backtest": {"slippage_ticks": 1, "synthetic_spread_ticks": 1},
        "paper": {"latency_ms": 300, "latency_jitter_ms": 0, "seed": 0},
    })


def mk(hhmm, o, h=None, l=None, c=None, day=DAY, symbol=SYM, reliable=True) -> Bar:
    c = o if c is None else c
    h = max(o, c) if h is None else h
    l = min(o, c) if l is None else l
    return Bar(symbol, datetime.combine(day, time.fromisoformat(hhmm)), o, h, l, c, 10, 60, reliable)


def flat(start_hhmm, n, price=1350.0, day=DAY, symbol=SYM) -> list[Bar]:
    t0 = datetime.combine(day, time.fromisoformat(start_hhmm))
    return [Bar(symbol, t0 + timedelta(minutes=i), price, price, price, price, 10)
            for i in range(n)]


class Script:
    """Strategy kịch bản: phát tín hiệu khi bar có giờ mở `HH:MM` (và ngày, nếu ghi) đóng."""

    def __init__(self, plan):
        self.plan = plan
        self.seen: list[tuple[Bar, int]] = []

    def on_bar(self, bar, ctx):
        self.seen.append((bar, ctx.position_qty))
        key = bar.start.strftime("%H:%M")
        p = self.plan.get((bar.start.date(), key), self.plan.get(key))
        if not p:
            return []
        ts = bar.start + timedelta(seconds=bar.interval_s)
        if p[0] == "exit":
            return [Signal(SignalAction.EXIT, ts, bar.symbol, reason="script")]
        action = SignalAction.ENTER_LONG if p[0] == "long" else SignalAction.ENTER_SHORT
        return [Signal(action, ts, bar.symbol, stop_loss=p[1],
                       take_profit=p[2] if len(p) > 2 else None, reason="script")]


@pytest.fixture
def settings(session_cfg):
    return bt_settings(session_cfg)


def run(settings, cal, strategy, bars):
    return BacktestEngine(settings, strategy, calendar=cal).run_bars(bars)


# ------------------------------------------------------------------ cấu hình ----

def test_repo_settings_with_nulls_refuse(cal):
    with pytest.raises(MissingSetting):
        BacktestEngine(load_settings(), Script({}), calendar=cal)


def test_spread_setting_required(session_cfg, cal):
    s = bt_settings(session_cfg)
    s.raw["backtest"]["synthetic_spread_ticks"] = None
    with pytest.raises(MissingSetting):
        BacktestEngine(s, Script({}), calendar=cal)


# ---------------------------------------------------------- chống look-ahead ----

class TestNoLookAhead:
    def test_signal_fills_at_next_bar_open_not_signal_close(self, settings, cal):
        bars = [mk("10:00", 1349, c=1350), mk("10:01", 1352, c=1352)] + flat("10:02", 3, 1352)
        res = run(settings, cal, Script({"10:00": ("long", 1340.0)}), bars)
        t = res.open_trades[0]                                      # chưa đóng khi hết dữ liệu
        assert t.entry_ts == datetime(2026, 11, 9, 10, 1, 1)        # tick mở của bar kế tiếp
        assert t.entry_price == pytest.approx(1352.2)               # ask (1352 + 1 bước) + trượt 1 bước
        assert t.entry_price != 1350.0                              # KHÔNG phải giá đóng bar tín hiệu

    def test_bars_delivered_only_after_close(self, settings, cal):
        bars = flat("10:00", 5)
        res = run(settings, cal, Script({}), bars)
        assert [ts for ts, _ in res.bar_log] == [b.start + timedelta(minutes=1) for b in bars]

    def test_events_going_back_in_time_rejected(self, settings, cal):
        b = mk("10:00", 1350)
        tick_inside = Tick(SYM, datetime(2026, 11, 9, 10, 0, 30), last=1350, bid=1350, ask=1350.1)
        with pytest.raises(ValueError, match="lùi"):
            BacktestEngine(settings, Script({}), calendar=cal).run_events([b, tick_inside])

    def test_cheater_using_future_prices_is_detected(self, settings, cal):
        bars = random_walk(DAY, "10:00", 60, seed=3)
        cut = datetime(2026, 11, 9, 10, 30)
        rep = detect_lookahead(settings, lambda data: Cheater(data), bars, cut, calendar=cal)
        assert rep.detected
        assert rep.first_difference is not None

    def test_honest_strategy_passes_lookahead_probe(self, settings, cal):
        bars = random_walk(DAY, "10:00", 60, seed=3)
        cut = datetime(2026, 11, 9, 10, 30)
        rep = detect_lookahead(settings, lambda data: Momentum(), bars, cut, calendar=cal)
        assert not rep.detected
        assert rep.n_signals_compared > 0


def random_walk(day, start, n, seed=0, p0=1350.0) -> list[Bar]:
    rng = np.random.default_rng(seed)
    t0 = datetime.combine(day, time.fromisoformat(start))
    out, price = [], p0
    for i in range(n):
        step = float(rng.choice([-3, -2, -1, 1, 2, 3])) / 10
        o, c = price, round(price + step, 1)
        out.append(Bar(SYM, t0 + timedelta(minutes=i), o, round(max(o, c) + 0.1, 1),
                       round(min(o, c) - 0.1, 1), c, 10))
        price = c
    return out


class Cheater:
    """GIAN LẬN: nhìn trước bar kế tiếp trong bộ dữ liệu đưa vào engine."""

    def __init__(self, data):
        self.next_close = {b.start: n.close for b, n in zip(data, data[1:])}

    def on_bar(self, bar, ctx):
        nxt = self.next_close.get(bar.start)
        ts = bar.start + timedelta(minutes=1)
        if nxt is None:
            return []
        if ctx.position_qty == 0 and nxt > bar.close:
            return [Signal(SignalAction.ENTER_LONG, ts, bar.symbol, stop_loss=bar.close - 5)]
        if ctx.position_qty > 0 and nxt < bar.close:
            return [Signal(SignalAction.EXIT, ts, bar.symbol)]
        return [Signal(SignalAction.HOLD, ts, bar.symbol)]


class Momentum:
    """Trung thực: chỉ dùng bar đã đóng."""

    def on_bar(self, bar, ctx):
        ts = bar.start + timedelta(minutes=1)
        prev = ctx.bars_today[-2] if len(ctx.bars_today) >= 2 else None
        if prev is None:
            return []
        if ctx.position_qty == 0 and bar.close > prev.close:
            return [Signal(SignalAction.ENTER_LONG, ts, bar.symbol, stop_loss=bar.close - 5)]
        if ctx.position_qty > 0 and bar.close < prev.close:
            return [Signal(SignalAction.EXIT, ts, bar.symbol)]
        return []


# ------------------------------------------------------ stop / chốt lời / phí ----

class TestExits:
    def test_stop_fills_pessimistically_and_fees_deducted(self, settings, cal):
        bars = [mk("10:00", 1350), mk("10:01", 1350, h=1350.4, l=1349.8, c=1350),
                mk("10:02", 1350, h=1350.5, l=1344, c=1346)] + flat("10:03", 3, 1346)
        strat = Script({"10:00": ("long", 1345.0)})
        res = run(settings, cal, strat, bars)
        t = res.trades[0]
        assert t.exit_reason == "stop"
        assert t.entry_price == pytest.approx(1350.2)
        assert t.exit_price == pytest.approx(1343.8)                # bid (đáy − 1 bước) − trượt, không phải 1345
        assert t.gross_points == pytest.approx(-6.4)
        assert t.fees_vnd == pytest.approx(2 * FEE)
        assert t.pnl_vnd == pytest.approx(-6.4 * 100_000 - 2 * FEE)
        assert t.pnl_points == pytest.approx(-6.6)
        assert [q for b, q in strat.seen if b.start.minute == 1] == [1]   # ctx thấy vị thế
        assert res.open_orders_at_end == []

    def test_take_profit_when_high_comes_first(self, settings, cal):
        bars = [mk("10:00", 1350), mk("10:01", 1350, h=1350.4, l=1349.8, c=1350),
                mk("10:02", 1350, h=1356, l=1343, c=1350)] + flat("10:03", 2)
        res = run(settings, cal, Script({"10:00": ("long", 1345.0, 1355.0)}), bars)
        t = res.trades[0]
        assert t.exit_reason == "take_profit" and t.exit_price == pytest.approx(1355.0)
        assert res.open_orders_at_end == []                          # stop đã được huỷ

    def test_undecidable_bar_stop_wins(self, settings, cal):
        bars = [mk("10:00", 1350), mk("10:01", 1350, h=1350.4, l=1349.8, c=1350),
                mk("10:02", 1350, h=1356, l=1344, c=1350)] + flat("10:03", 2)
        res = run(settings, cal, Script({"10:00": ("long", 1345.0, 1355.0)}), bars)
        t = res.trades[0]
        assert t.exit_reason == "stop"
        assert t.exit_price == pytest.approx(1343.8)    # khớp ở đáy (bid − trượt), không phải ở đỉnh sau đó

    def test_undecidable_bar_stop_wins_for_short(self, settings, cal):
        bars = [mk("10:00", 1350), mk("10:01", 1350, h=1350.2, l=1349.6, c=1350),
                mk("10:02", 1350, h=1356, l=1344, c=1350)] + flat("10:03", 2)
        res = run(settings, cal, Script({"10:00": ("short", 1355.0, 1345.0)}), bars)
        assert res.trades[0].side == "SHORT" and res.trades[0].exit_reason == "stop"

    def test_undecidable_entry_bar_counts_pending_entry(self, settings, cal):
        """Bar mà lệnh vào khớp ngay ở tick mở: chiều bất lợi tính theo lệnh vào đang chờ."""
        bars = [mk("10:00", 1350), mk("10:01", 1350, h=1356, l=1344, c=1350)] + flat("10:02", 2)
        res = run(settings, cal, Script({"10:00": ("short", 1355.0, 1345.0)}), bars)
        t = res.trades[0]
        assert t.exit_reason == "stop" and t.exit_price == pytest.approx(1356.2)

    def test_exit_signal(self, settings, cal):
        bars = flat("10:00", 2) + [mk("10:02", 1351, c=1352)] + flat("10:03", 3, 1352)
        res = run(settings, cal, Script({"10:00": ("long", 1340.0), "10:02": ("exit",)}), bars)
        t = res.trades[0]
        assert t.exit_reason == "exit" and t.exit_ts == datetime(2026, 11, 9, 10, 3, 1)
        assert t.exit_price == pytest.approx(1351.8)                 # bid (1352 − 1 bước) − trượt

    def test_opposite_entry_only_closes(self, settings, cal):
        bars = flat("10:00", 6)
        res = run(settings, cal, Script({"10:00": ("short", 1360.0), "10:02": ("long", 1340.0)}), bars)
        assert [(t.side, t.exit_reason) for t in res.trades] == [("SHORT", "exit")]
        assert res.open_trades == [] and res.rejections == []

    def test_flatten_before_atc(self, settings, cal):
        bars = flat("14:00", 30)
        res = run(settings, cal, Script({"14:00": ("long", 1300.0)}), bars)
        t = res.trades[0]
        assert t.exit_reason == "flatten"
        assert datetime(2026, 11, 9, 14, 25) <= t.exit_ts < datetime(2026, 11, 9, 14, 30)
        assert res.warnings == []


# -------------------------------------------------------------- risk trong pipeline ----

class TestRiskInPipeline:
    def test_rejections_recorded_with_reason(self, settings, cal):
        bars = flat("09:00", 8) + flat("10:00", 3)
        res = run(settings, cal, Script({"09:05": ("long", 1340.0), "10:00": ("long", None)}), bars)
        codes = [r.code for r in res.rejections]
        assert codes == [RejectCode.OPENING_WINDOW, RejectCode.NO_STOP_LOSS]
        assert res.trades == []

    def test_max_trades_resets_next_day(self, session_cfg, cal):
        s = bt_settings(session_cfg, max_trades_per_day=1)
        bars = flat("10:00", 6) + flat("10:00", 4, day=DAY2)
        plan = {"10:00": ("long", 1340.0), "10:02": ("exit",), "10:04": ("long", 1340.0)}
        res = run(s, cal, Script(plan), bars)
        assert [r.code for r in res.rejections] == [RejectCode.MAX_TRADES]
        assert [t.entry_ts.date() for t in res.trades] == [DAY, DAY2]

    def test_only_trading_contract_is_fed(self, settings, cal):
        strat = Script({})
        bars = flat("10:00", 3) + flat("10:00", 3, price=1400.0, symbol="VN30F2612")
        bars.sort(key=lambda b: (b.start, b.symbol))
        run(settings, cal, strat, bars)
        assert {b.symbol for b, _ in strat.seen} == {SYM}

    def test_open_position_at_data_end_is_flagged(self, settings, cal):
        res = run(settings, cal, Script({"10:00": ("long", 1300.0)}), flat("10:00", 5))
        assert res.warnings and "còn vị thế" in res.warnings[0]
        assert res.trades == [] and len(res.open_trades) == 1

    def test_deterministic(self, session_cfg, cal):
        s = bt_settings(session_cfg, max_trades_per_day=100, max_consecutive_losses=100,
                        max_daily_loss_points=1000)
        bars = random_walk(DAY, "10:00", 120, seed=9)
        a = run(s, cal, Momentum(), bars)
        b = run(s, cal, Momentum(), bars)
        assert [(t.entry_ts, t.pnl_vnd) for t in a.trades] == [(t.entry_ts, t.pnl_vnd) for t in b.trades]
        assert len(a.trades) > 3


# ------------------------------------------------------------------ nguồn dữ liệu ----

class TestSources:
    def test_bars_from_ticks(self, settings, cal):
        ticks = [Tick(SYM, datetime(2026, 11, 9, 10, 0, 5), last=1350.0, last_qty=2, bid=1349.9, ask=1350.0),
                 Tick(SYM, datetime(2026, 11, 9, 10, 0, 40), last=1351.0, last_qty=1, bid=1350.9, ask=1351.0),
                 Tick(SYM, datetime(2026, 11, 9, 10, 0, 50), bid=1350.8, ask=1351.0),     # chỉ báo giá
                 Tick(SYM, datetime(2026, 11, 9, 10, 1, 10), last=1349.0, last_qty=1, bid=1348.9, ask=1349.0)]
        events = list(bars_from_ticks(ticks))
        kinds = [type(e).__name__ for e in events]
        assert kinds == ["Tick", "Tick", "Tick", "Bar", "Tick", "Bar"]
        b = events[3]
        assert (b.start, b.open, b.high, b.low, b.close, b.volume) == \
            (datetime(2026, 11, 9, 10, 0), 1350.0, 1351.0, 1350.0, 1351.0, 3)
        res = BacktestEngine(settings, Script({}), calendar=cal).run_events(events)
        assert [ts for ts, _ in res.bar_log] == [datetime(2026, 11, 9, 10, 1), datetime(2026, 11, 9, 10, 2)]

    def test_tick_mode_fills_on_real_quotes(self, settings, cal):
        ticks = [Tick(SYM, datetime(2026, 11, 9, 10, 0, 30), last=1350.0, bid=1349.9, ask=1350.0),
                 Tick(SYM, datetime(2026, 11, 9, 10, 1, 0, 100000), last=1350.0, bid=1349.9, ask=1350.0),
                 Tick(SYM, datetime(2026, 11, 9, 10, 1, 2), last=1351.0, bid=1350.8, ask=1351.1),
                 Tick(SYM, datetime(2026, 11, 9, 10, 1, 31), last=1351.0, bid=1350.8, ask=1351.1),
                 Tick(SYM, datetime(2026, 11, 9, 10, 2, 0, 500000), last=1351.0, bid=1350.8, ask=1351.1)]
        res = BacktestEngine(settings, Script({"10:00": ("long", 1340.0)}), calendar=cal) \
            .run_events(bars_from_ticks(ticks))
        # tick 10:01:00.1 còn trong độ trễ 300 ms ⇒ khớp ở tick 10:01:02
        assert res.open_trades[0].entry_price == pytest.approx(1351.2)

    def test_load_bars_parquet_import_schema(self, tmp_path):
        df = pd.DataFrame({
            "symbol": [SYM, SYM, SYM], "start": pd.to_datetime(["2026-11-09 10:01", "2026-11-09 10:00",
                                                                  "2026-11-09 10:02"]),
            "open": [1350.0, 1349.0, 1351.0], "high": [1351.0, 1350.0, 1350.0],
            "low": [1349.5, 1348.5, 1349.0], "close": [1350.5, 1349.5, 1349.5],
            "volume": [0, 0, 0], "volume_reliable": [False, False, False],
            "source": ["x"] * 3, "source_symbol": ["x"] * 3, "price_rescaled": [False] * 3})
        df.loc[2, "high"] = 1350.0          # dòng 3 hỏng (high < open) ⇒ bỏ
        df.to_parquet(tmp_path / "2026-11-09.parquet")
        bars, skipped = load_bars_parquet(tmp_path)
        assert [b.start.minute for b in bars] == [0, 1]
        assert skipped == 1 and not bars[0].volume_reliable


# -------------------------------------------------------------------- báo cáo ----

def tr(pnl, entry, reason="exit", side="LONG"):
    return TradeRecord(symbol=SYM, side=side, qty=1, entry_ts=entry, entry_price=1350.0,
                       exit_ts=entry + timedelta(minutes=5), exit_price=1350.0, exit_reason=reason,
                       gross_points=pnl / 100_000, fees_vnd=0.0, pnl_vnd=pnl, pnl_points=pnl / 100_000)


class TestReport:
    def test_metrics(self):
        trades = [tr(100, datetime(2026, 11, 9, 9, 30)), tr(-50, datetime(2026, 11, 9, 10, 30)),
                  tr(200, datetime(2026, 11, 10, 9, 45)), tr(-50, datetime(2026, 11, 10, 13, 15))]
        m = compute_metrics(trades)
        assert m["n_trades"] == 4 and m["win_rate"] == pytest.approx(0.5)
        assert m["avg_win_vnd"] == pytest.approx(150) and m["avg_loss_vnd"] == pytest.approx(-50)
        assert m["profit_factor"] == pytest.approx(3.0)
        assert m["total_pnl_vnd"] == pytest.approx(200)
        assert m["max_drawdown_vnd"] == pytest.approx(50)
        assert m["by_hour"][9] == {"n": 2, "pnl_vnd": 300}
        assert m["by_hour"][13]["pnl_vnd"] == -50
        assert m["by_weekday"]["Thứ 2"]["pnl_vnd"] == 50
        assert m["by_weekday"]["Thứ 3"]["pnl_vnd"] == 150

    def test_metrics_empty_and_no_losses(self):
        assert compute_metrics([])["n_trades"] == 0
        assert compute_metrics([tr(100, datetime(2026, 11, 9, 9, 30))])["profit_factor"] is None

    def test_drawdown_from_first_trade_loss(self):
        trades = [tr(-100, datetime(2026, 11, 9, 9, 30)), tr(50, datetime(2026, 11, 9, 10, 0))]
        assert compute_metrics(trades)["max_drawdown_vnd"] == pytest.approx(100)

    def test_report_splits_and_selection_penalty(self, settings, cal):
        res = run(settings, cal, Momentum(), random_walk(DAY, "10:00", 200, seed=1)
                  + random_walk(DAY2, "10:00", 200, seed=2))
        rep = build_report(res, split_date=DAY2, n_trials=5)
        assert set(rep["sections"]) == {"all", "in_sample", "out_of_sample"}
        ins, oos = rep["sections"]["in_sample"], rep["sections"]["out_of_sample"]
        assert ins["metrics"]["n_trades"] + oos["metrics"]["n_trades"] == len(res.trades)
        assert all(t.entry_ts.date() < DAY2 for t in rep["sections"]["in_sample"]["trades"])
        assert rep["n_trials"] == 5
        assert "walk_forward" in rep["sections"]["all"]["validation"]
        assert rep["sections"]["all"]["validation"]["passed"] is False   # 1 ngày, ít lệnh

    def test_html_contains_required_sections_and_data_warning(self, settings, cal):
        bars = [Bar(b.symbol, b.start, b.open, b.high, b.low, b.close, 0, 60, False)
                for b in random_walk(DAY, "10:00", 120, seed=4)]
        res = run(settings, cal, Momentum(), bars)
        html = render_html(build_report(res, split_date=None, n_trials=1, title="thử"))
        for needle in ("Số lệnh", "Win rate", "Profit factor", "Max drawdown", "sau phí",
                       "Theo giờ", "Theo ngày trong tuần", "p-value", "Walk-forward",
                       "Lệnh bị risk từ chối", "không dùng để kết luận", "tick giả"):
            assert needle in html, needle
        assert html.lstrip().startswith("<!doctype html>")


def test_data_gap_with_position_flattens(settings, cal):
    """golive #9: dữ liệu thủng khi đang giữ vị thế ⇒ đóng (stop do bot giữ đang mù)."""
    bars = flat("10:00", 3) + flat("10:05", 3)                # mất 10:03–10:04 (≈ 2 phút không tick)
    res = run(settings, cal, Script({"10:00": ("long", 1300.0)}), bars)
    t = res.trades[0]
    assert t.exit_reason == "flatten"
    assert t.exit_ts.time() >= datetime(2026, 11, 9, 10, 5).time()


# ------------------------------------------------------------------ golive #14: bớt lạc quan ----

class TestGolive14:
    def test_odd_spread_costs_both_sides(self):
        """Spread 1 bước trước đây: bid = last ⇒ bán không tốn gì. Giờ mỗi phía cách last ≥ 1 bước."""
        from backtest.engine import synthetic_ticks
        ticks = synthetic_ticks(mk("10:00", 1350), 0, 0.1, 1)
        assert all(t.bid == pytest.approx(t.last - 0.1) and t.ask == pytest.approx(t.last + 0.1) for t in ticks)

    def test_even_spread_unchanged(self):
        from backtest.engine import synthetic_ticks
        ticks = synthetic_ticks(mk("10:00", 1350), 0, 0.1, 4)
        assert all(t.bid == pytest.approx(t.last - 0.2) and t.ask == pytest.approx(t.last + 0.2) for t in ticks)

    def test_extreme_held_longer_than_latency(self):
        from backtest.engine import synthetic_ticks
        ticks = synthetic_ticks(mk("10:00", 1350, h=1353, l=1345, c=1351), 1, 0.1, 1, hold_s=4)
        lows = [t for t in ticks if t.last == 1345]
        assert len(lows) == 2 and (lows[1].ts - lows[0].ts) == timedelta(seconds=4)

    def test_hold_never_reaches_other_extreme(self):
        from backtest.engine import synthetic_ticks
        ticks = synthetic_ticks(mk("10:00", 1350, h=1353, l=1345, c=1351), 1, 0.1, 1, hold_s=60)
        assert [t.ts for t in ticks] == sorted(t.ts for t in ticks)
        assert len({t.ts for t in ticks}) == len(ticks)

    def test_long_latency_stop_fills_at_stop_extreme_not_the_other(self, session_cfg, cal):
        """Độ trễ 2 giây > 1 giây giữ cực trị cũ ⇒ trước đây stop khớp ở ĐỈNH (lạc quan)."""
        s = bt_settings(session_cfg)
        s.raw["paper"]["latency_ms"] = 2000
        bars = flat("10:00", 2) + [mk("10:02", 1350, h=1356, l=1338, c=1350)] + flat("10:03", 3)
        res = run(s, cal, Script({"10:00": ("long", 1340.0)}), bars)
        t = res.trades[0]
        assert t.exit_reason == "stop" and t.exit_price < 1340.0              # quanh đáy, không phải 1356

    def test_open_trade_at_data_end_counted_with_exit_fee(self, settings, cal):
        res = run(settings, cal, Script({"10:00": ("long", 1300.0)}), flat("10:00", 5))
        rep = build_report(res)
        m = rep["sections"]["all"]["metrics"]
        assert m["n_trades"] == 1                                             # không còn bị bỏ ra ngoài
        ot = res.open_trades[0]
        assert ot.fees_vnd == pytest.approx(2 * FEE)                          # phí vào + ƯỚC phí đóng
        assert any("còn mở cuối dữ liệu" in w for w in rep["warnings"])
