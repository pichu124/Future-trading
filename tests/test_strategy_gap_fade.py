"""Chiến lược #6 — lấp gap mở cửa (`strategy/gap_fade.py`). Viết TRƯỚC khi implement.

Đặc tả (đăng ký trước 12/11/2026, research/log.md mục "Đăng ký trước #6"):
- C₋₁ = đóng nến CUỐI của đúng phiên giao dịch liền trước (`prev_trading_day`, theo lịch nghỉ) trong `ctx.history`
  (back-adjust). Phiên đó không có trong lịch sử ⇒ không giao dịch (không lấy phiên cũ hơn).
- ADR = `adr_before(history, ngày, adr_days)`; thiếu ⇒ không giao dịch.
- Quyết định MỘT lần ở nến có giờ mở ĐÚNG `decision_bar` (09:15) khi nến đóng; thiếu nến đó ⇒ không giao dịch cả
  ngày. g = đóng nến − C₋₁; |g| ≥ min_gap_adr × ADR và đang phẳng ⇒ vào NGƯỢC dấu g; chốt lời = C₋₁;
  stop = đóng ± stop_adr × ADR (xa thêm về phía gap), làm tròn bước giá. Tối đa 1 tín hiệu vào / ngày.
- Đang có vị thế và nến có giờ mở ≥ `exit_bar` (11:24) đóng ⇒ EXIT (một lần / ngày).
- `history_days` = adr_days; `ROBUSTNESS_PARAMS` = adr_days, min_gap_adr, stop_adr; `TITLE` ghi #6.
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.models import SignalAction
from core.settings import MissingSetting, Settings
from strategy.base import StrategyContext, history_days
from strategy.gap_fade import GapFade
from tests.test_strategy_morning_momentum import DAY, b, history

GF = {"adr_days": 3, "min_gap_adr": 0.30, "stop_adr": 0.30, "decision_bar": "09:15", "exit_bar": "11:24", "qty": 1}
FRI = date(2026, 11, 6)                      # phiên liền trước DAY (thứ Hai 09/11/2026)


def settings(**over) -> Settings:
    return Settings({"session": {"continuous_am_start": "09:00"}, "contract": {"tick_size_points": 0.1},
                     "strategy": {"gap_fade": {**GF, **over}}})


def weekdays(d: date) -> date:
    """Phiên liền trước theo lịch chỉ bỏ cuối tuần (test không phụ thuộc holidays.yaml)."""
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def gf(prev=weekdays, **over):
    return GapFade.from_settings(settings(**over), prev_trading_day=prev)


def day_bars(at_0915=1357.0, later=None, day=DAY, skip=()):
    """09:00 … 09:15 ở at_0915 (mở cửa đã gap), 09:16 … 11:29 ở `later` (mặc định giữ nguyên)."""
    later = at_0915 if later is None else later
    out, t = [], datetime.combine(day, time(9, 0))
    for i in range(150):
        hhmm = (t + timedelta(minutes=i)).strftime("%H:%M")
        if hhmm not in skip:
            out.append(b(hhmm, at_0915 if i <= 15 else later, day=day))
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
    s = gf()
    assert history_days(s) == 3 and "#6" in GapFade.TITLE
    assert GapFade.ROBUSTNESS_PARAMS == ("strategy.gap_fade.adr_days", "strategy.gap_fade.min_gap_adr",
                                         "strategy.gap_fade.stop_adr")


@pytest.mark.parametrize("bad", [{"adr_days": 0}, {"adr_days": 1.5}, {"min_gap_adr": 0}, {"stop_adr": -0.1},
                                 {"qty": 0}, {"decision_bar": "9h15"}, {"exit_bar": "24:01"},
                                 {"decision_bar": "08:59"}, {"exit_bar": "09:15"}])
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError):
        gf(**bad)


def test_null_value_refused():
    with pytest.raises(MissingSetting):
        gf(min_gap_adr=None)


def test_repo_settings_load_and_default_calendar():
    from core.settings import load_settings
    s = load_settings()
    g = s.get("strategy.gap_fade")
    assert (g["adr_days"], g["min_gap_adr"], g["stop_adr"], g["decision_bar"], g["exit_bar"], g["qty"]) == \
        (10, 0.30, 0.30, "09:15", "11:24", 1)
    strat = GapFade.from_settings(s)                                    # lịch mặc định = config/holidays.yaml
    assert history_days(strat) == 10
    assert strat.prev_trading_day(date(2026, 11, 9)) == FRI


# ------------------------------------------------------------------ vào lệnh ----

def test_gap_up_shorts_at_0916_with_tp_at_previous_close():
    """history: C₋₁ = 1350, ADR 20 ⇒ ngưỡng 6 điểm; 09:15 ở 1357 ⇒ g = +7 ⇒ Short."""
    (at, s), = run(gf(), day_bars(1357.0), history(rng=20.0))
    assert at == "09:15" and s.action is SignalAction.ENTER_SHORT
    assert s.ts == datetime.combine(DAY, time(9, 16))
    assert s.take_profit == pytest.approx(1350.0) and s.stop_loss == pytest.approx(1363.0) and s.qty == 1
    assert "g=+7.0" in s.reason and "ADR3 20.0" in s.reason


def test_gap_down_goes_long():
    (_, s), = run(gf(), day_bars(1343.0), history(rng=20.0))
    assert s.action is SignalAction.ENTER_LONG
    assert s.take_profit == pytest.approx(1350.0) and s.stop_loss == pytest.approx(1337.0)


@pytest.mark.parametrize("gap,n", [(5.9, 0), (6.0, 1), (-6.0, 1), (-5.9, 0), (0.0, 0)])
def test_threshold_edges(gap, n):
    assert len(entries(run(gf(), day_bars(1350.0 + gap), history(rng=20.0)))) == n


def test_previous_close_is_last_bar_of_previous_session():
    """Nến cuối (vd ATC 14:45) của phiên trước là mốc, không phải nến sớm hơn."""
    hist = history(rng=20.0) + (b("14:45", 1352.0, day=FRI),)
    (_, s), = run(gf(), day_bars(1358.0), hist)                      # g = 1358 − 1352 = +6
    assert s.take_profit == pytest.approx(1352.0) and "g=+6.0" in s.reason
    assert run(gf(), day_bars(1357.0), hist) == []                    # g = +5 < 6


def test_missing_previous_session_no_trade():
    """Mất dữ liệu phiên liền trước ⇒ không lấy phiên cũ hơn làm mốc."""
    hist = tuple(x for x in history(n=4, rng=20.0) if x.start.date() != FRI)
    assert run(gf(), day_bars(1357.0), hist) == []


def test_previous_session_follows_calendar_holiday():
    """Thứ Sáu là ngày nghỉ theo lịch ⇒ phiên liền trước là thứ Năm."""
    thu = date(2026, 11, 5)
    prev = lambda d: thu if d == DAY else weekdays(d)                 # noqa: E731
    hist = tuple(x for x in history(n=4, rng=20.0) if x.start.date() != FRI)
    (_, s), = run(gf(prev=prev), day_bars(1357.0), hist)
    assert s.take_profit == pytest.approx(1350.0)


def test_no_or_short_history_no_trade():
    assert run(gf(), day_bars(1357.0), ()) == [] and run(gf(), day_bars(1357.0), history(n=2)) == []


def test_missing_0915_no_trade_all_day():
    bars = day_bars(1357.0, skip=("09:15",))
    assert run(gf(), bars, history(rng=20.0)) == []


def test_already_in_position_no_entry_and_once_per_day():
    assert entries(run(gf(), day_bars(1357.0), history(), pos=lambda bar: -1)) == []
    strat = gf()
    assert len(entries(run(strat, day_bars(1357.0), history()))) == 1
    assert entries(run(strat, day_bars(1357.0), history())) == []


def test_decides_only_at_0915_not_later():
    """Gap nhỏ lúc 09:15 rồi giá chạy xa sau đó ⇒ không vào (chỉ quyết định một lần)."""
    assert run(gf(), day_bars(1352.0, later=1370.0), history(rng=20.0)) == []


# ------------------------------------------------------------------ thoát ----

def test_exit_at_1124_once_when_holding():
    held = lambda bar: -1 if bar.start.time() >= time(9, 16) else 0   # noqa: E731
    sigs = run(gf(), day_bars(1357.0), history(), pos=held)
    ex = [(at, s) for at, s in sigs if s.action is SignalAction.EXIT]
    assert [at for at, _ in ex] == ["11:24"] and ex[0][1].ts == datetime.combine(DAY, time(11, 25))


# ------------------------------------------------------------------ tích hợp ----

def _engine(session_cfg, cal, bars):
    from backtest.engine import BacktestEngine
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg, max_trades_per_day=5, last_entry_time="14:15")
    s._data["strategy"] = {"gap_fade": {**GF, "adr_days": 2}}
    return BacktestEngine(s, GapFade.from_settings(s, prev_trading_day=cal.previous_trading_day),
                          calendar=cal).run_bars(sorted(bars, key=lambda x: x.start))


def _flat_day(d, px=1350.0):
    """Phiên phẳng ở px, biên độ ngày 20 (đỉnh 11:00 — trước đó stop không bị chạm)."""
    out = day_bars(px, day=d)
    return [x if x.start.time() != time(11, 0) else b("11:00", px, px, px + 20.0, px, day=d) for x in out]


def test_engine_fills_gap_and_takes_profit(session_cfg, cal):
    d1, d2, d3 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)
    gap = [x if x.start.time() < time(9, 40) else b(x.start.strftime("%H:%M"), 1349.0, day=d3)
           for x in day_bars(1357.0, day=d3)]                           # 09:40 lấp gap về dưới 1350
    res = _engine(session_cfg, cal, _flat_day(d1) + _flat_day(d2) + gap)
    (tr,) = res.trades
    assert tr.side == "SHORT" and time(9, 16) <= tr.entry_ts.time() < time(9, 17)
    assert tr.exit_reason == "take_profit" and tr.exit_price == pytest.approx(1350.0, abs=0.2)


def test_engine_exits_at_1125_when_gap_not_filled(session_cfg, cal):
    d1, d2, d3 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)
    res = _engine(session_cfg, cal, _flat_day(d1) + _flat_day(d2) + day_bars(1357.0, day=d3))
    (tr,) = res.trades
    assert tr.exit_reason == "exit" and time(11, 25) <= tr.exit_ts.time() < time(11, 26)


def test_robustness_cli_runs_gap_fade(monkeypatch, tmp_path):
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
        R.main(["--bars", str(tmp_path), "--split", "2026-09-01", "--strategy", "strategy.gap_fade:GapFade"])
    assert isinstance(captured["strat"], GapFade) and captured["params"] == GapFade.ROBUSTNESS_PARAMS
