"""Chiến lược #4 — ngày xu hướng (`strategy/trend_day.py`). Viết TRƯỚC khi implement.

Đặc tả (đăng ký trước 10/11/2026, research/log.md mục "Đăng ký trước #3, #4, #5"):
- ADR = `strategy.indicators.adr_before(history, ngày, adr_days)`; thiếu ⇒ không giao dịch.
- Mở = giá mở nến ĐẦU TIÊN có giờ mở trong [09:00, 09:05). Thiếu ⇒ không giao dịch hôm đó.
- Quyết định MỘT lần ở nến đầu tiên có giờ mở trong [decision_bar, decision_bar + 5′) (mặc định 09:59) khi nến
  đó đóng: m = đóng nến quyết định − Mở; |m| ≥ min_move_adr × ADR và đang phẳng ⇒ ENTER theo dấu m;
  stop = đóng nến quyết định ∓ stop_adr × ADR (làm tròn bước giá); không chốt lời; tối đa 1 tín hiệu vào / ngày.
- Đang có vị thế và nến có giờ mở ≥ `exit_bar` (14:19) đóng ⇒ EXIT (một lần / ngày).
- `history_days` = adr_days; `ROBUSTNESS_PARAMS` = adr_days, min_move_adr, stop_adr; `TITLE` ghi #4.
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.models import SignalAction
from core.settings import MissingSetting, Settings
from strategy.base import StrategyContext, history_days
from strategy.trend_day import TrendDay
from tests.test_strategy_morning_momentum import DAY, SYM, b, history

TD = {"adr_days": 3, "min_move_adr": 0.35, "stop_adr": 0.30, "decision_bar": "09:59", "exit_bar": "14:19", "qty": 1}


def settings(**over) -> Settings:
    return Settings({"session": {"continuous_am_start": "09:00"}, "contract": {"tick_size_points": 0.1},
                     "strategy": {"trend_day": {**TD, **over}}})


def day_bars(open_=1350.0, at_1000=1357.0, day=DAY, skip=()):
    """09:00 … 09:59 đi thẳng từ open_ tới at_1000, rồi 10:00 … 14:20 đứng yên."""
    out, t = [], datetime.combine(day, time(9, 0))
    for i in range(60):
        hhmm = (t + timedelta(minutes=i)).strftime("%H:%M")
        if hhmm not in skip:
            o = open_ + (at_1000 - open_) * i / 60
            c = open_ + (at_1000 - open_) * (i + 1) / 60
            out.append(b(hhmm, round(o, 1), round(c, 1), day=day))
    for i in range(60, 60 + 90):                                          # 10:00 … 11:29
        hhmm = (t + timedelta(minutes=i)).strftime("%H:%M")
        if hhmm not in skip:
            out.append(b(hhmm, at_1000, day=day))
    t = datetime.combine(day, time(13, 0))
    for i in range(81):                                                    # 13:00 … 14:20
        hhmm = (t + timedelta(minutes=i)).strftime("%H:%M")
        if hhmm not in skip:
            out.append(b(hhmm, at_1000, day=day))
    return out


def run(strat, bars, hist, pos=lambda bar: 0):
    today, sigs = [], []
    for bar in bars:
        today.append(bar)
        for s in strat.on_bar(bar, StrategyContext(pos(bar), None, tuple(today), hist)):
            sigs.append((bar.start.strftime("%H:%M"), s))
    return sigs


def entries(sigs):
    return [(at, s) for at, s in sigs if s.action is not SignalAction.EXIT]


# ------------------------------------------------------------------ cấu hình ----

def test_from_settings_history_days_title_params():
    s = TrendDay.from_settings(settings())
    assert history_days(s) == 3 and "#4" in TrendDay.TITLE
    assert TrendDay.ROBUSTNESS_PARAMS == ("strategy.trend_day.adr_days", "strategy.trend_day.min_move_adr",
                                          "strategy.trend_day.stop_adr")


@pytest.mark.parametrize("bad", [{"adr_days": 0}, {"adr_days": 1.5}, {"min_move_adr": 0}, {"stop_adr": -0.1},
                                 {"qty": 0}, {"decision_bar": "9h59"}, {"exit_bar": "24:01"},
                                 {"decision_bar": "08:59"}])
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError):
        TrendDay.from_settings(settings(**bad))


def test_null_value_refused():
    with pytest.raises(MissingSetting):
        TrendDay.from_settings(settings(min_move_adr=None))


def test_repo_settings_load():
    from core.settings import load_settings
    s = load_settings()
    td = s.get("strategy.trend_day")
    assert (td["adr_days"], td["min_move_adr"], td["stop_adr"], td["decision_bar"], td["exit_bar"]) == \
        (10, 0.35, 0.30, "09:59", "14:19")
    assert history_days(TrendDay.from_settings(s)) == 10


# ------------------------------------------------------------------ vào lệnh ----

def test_strong_first_hour_up_enters_long_at_1000():
    (at, s), = run(TrendDay.from_settings(settings()), day_bars(1350.0, 1357.0), history(rng=20.0))
    assert at == "09:59" and s.action is SignalAction.ENTER_LONG
    assert s.ts == datetime.combine(DAY, time(10, 0))
    assert s.stop_loss == pytest.approx(1357.0 - 6.0) and s.take_profit is None and s.qty == 1
    assert "m=+7.0" in s.reason and "ADR3 20.0" in s.reason


def test_strong_first_hour_down_enters_short():
    (at, s), = run(TrendDay.from_settings(settings()), day_bars(1350.0, 1343.0), history(rng=20.0))
    assert s.action is SignalAction.ENTER_SHORT and s.stop_loss == pytest.approx(1349.0)


@pytest.mark.parametrize("move,n", [(6.9, 0), (7.0, 1), (-7.0, 1), (-6.9, 0)])
def test_threshold_edges(move, n):
    """ADR 20 × 0,35 = 7 điểm."""
    sigs = run(TrendDay.from_settings(settings()), day_bars(1350.0, 1350.0 + move), history(rng=20.0))
    assert len(entries(sigs)) == n


def test_no_or_short_history_no_trade():
    strat = TrendDay.from_settings(settings())
    assert run(strat, day_bars(), ()) == [] and run(strat, day_bars(), history(n=2)) == []


def test_missing_open_window_no_trade():
    assert run(TrendDay.from_settings(settings()), day_bars(skip=tuple(f"09:0{i}" for i in range(5))),
               history()) == []


def test_missing_0959_uses_next_bar_in_window_then_never_again():
    """Thiếu nến 09:59 ⇒ quyết định ở 10:00 (trong cửa sổ 5′); bỏ cả cửa sổ ⇒ không vào cả ngày."""
    sigs = run(TrendDay.from_settings(settings()), day_bars(skip=("09:59",)), history())
    assert [at for at, _ in entries(sigs)] == ["10:00"]
    skip = ("09:59", "10:00", "10:01", "10:02", "10:03")
    assert run(TrendDay.from_settings(settings()), day_bars(skip=skip), history()) == []


def test_already_in_position_no_entry_and_once_per_day():
    assert entries(run(TrendDay.from_settings(settings()), day_bars(), history(), pos=lambda bar: 1)) == []
    strat = TrendDay.from_settings(settings())
    assert len(entries(run(strat, day_bars(), history()))) == 1
    assert entries(run(strat, day_bars(), history())) == []


def test_weak_day_never_trades_later():
    """Giờ đầu yếu ⇒ không vào, kể cả khi giá chạy mạnh sau 10:00 (chỉ quyết định một lần)."""
    bars = day_bars(1350.0, 1352.0)
    late = [b(x.start.strftime("%H:%M"), 1370.0, day=DAY) if x.start.time() >= time(10, 30) else x for x in bars]
    assert run(TrendDay.from_settings(settings()), late, history(rng=20.0)) == []


# ------------------------------------------------------------------ thoát ----

def test_exit_at_1419_once_when_holding():
    held = lambda bar: 1 if bar.start.time() >= time(10, 0) else 0   # noqa: E731
    sigs = run(TrendDay.from_settings(settings()), day_bars(), history(), pos=held)
    ex = [(at, s) for at, s in sigs if s.action is SignalAction.EXIT]
    assert [at for at, _ in ex] == ["14:19"] and ex[0][1].ts == datetime.combine(DAY, time(14, 20))


# ------------------------------------------------------------------ tích hợp ----

def test_engine_trades_at_1000_and_exits_at_1420(session_cfg, cal):
    from backtest.engine import BacktestEngine
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg, max_trades_per_day=5, last_entry_time="14:15")
    s._data["strategy"] = {"trend_day": {**TD, "adr_days": 2}}
    bars = []
    for d in (date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)):
        bars += [x if x.start.time() != time(11, 0) else b("11:00", x.open, x.close, 1370.0, None, day=d)
                 for x in day_bars(1350.0, 1357.0, day=d)]   # biên độ ngày 20 (đỉnh, không chạm stop)
    bars.sort(key=lambda x: x.start)
    res = BacktestEngine(s, TrendDay.from_settings(s), calendar=cal).run_bars(bars)
    assert [ts.date() for ts, sig in res.signals if sig.action is SignalAction.ENTER_LONG] == [date(2026, 11, 4)]
    (tr,) = res.trades
    assert tr.side == "LONG" and time(10, 0) <= tr.entry_ts.time() < time(10, 1)
    assert tr.exit_reason == "exit" and time(14, 20) <= tr.exit_ts.time() < time(14, 21)


def test_robustness_cli_runs_trend_day(monkeypatch, tmp_path):
    import backtest.robustness as R
    s = settings()
    monkeypatch.setattr(R, "load_settings", lambda: s)
    monkeypatch.setattr(R, "BacktestEngine", lambda *a, **k: None)
    monkeypatch.setattr(R, "load_bars_parquet", lambda *a, **k: ([], 0))
    captured = {}

    def fake_run(settings_, make, bars, split, params, **kw):
        captured.update(params=params, strat=make(settings_))
        raise SystemExit(0)
    monkeypatch.setattr(R, "run_robustness", fake_run)
    with pytest.raises(SystemExit):
        R.main(["--bars", str(tmp_path), "--split", "2026-09-01", "--strategy", "strategy.trend_day:TrendDay"])
    assert isinstance(captured["strat"], TrendDay) and captured["params"] == TrendDay.ROBUSTNESS_PARAMS
