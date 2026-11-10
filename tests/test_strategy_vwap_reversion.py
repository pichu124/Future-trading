"""Chiến lược #5 — đảo chiều về VWAP (`strategy/vwap_reversion.py`). Viết TRƯỚC khi implement.

Đặc tả (đăng ký trước 10/11/2026, research/log.md mục "Đăng ký trước #3, #4, #5"):
- VWAP trong ngày = Σ((H+L+C)/3 × KL) / ΣKL của các nến hôm nay có giờ mở ≥ 09:00 (gồm nến hiện tại), bỏ nến KL = 0;
  ΣKL = 0 ⇒ không có VWAP ⇒ không giao dịch.
- ADR = `adr_before(history, ngày, adr_days)`; thiếu ⇒ không giao dịch.
- Xét ở nến có giờ mở trong [window_start, window_end] (09:29 … 13:59), BỎ nến đóng đúng lúc nghỉ trưa
  (11:29 đóng 11:30 — risk chặn mở, phí một lượt). d = đóng − VWAP; |d| ≥ entry_dev_adr × ADR và đang phẳng ⇒
  vào NGƯỢC dấu d; chốt lời = VWAP (làm tròn bước giá); stop = đóng ± stop_adr × ADR (xa thêm về phía lệch).
- Tối đa `max_trades_per_day` (2) tín hiệu vào / ngày. Đang có vị thế và nến giờ mở ≥ `exit_bar` (14:19) đóng ⇒
  EXIT (một lần / ngày).
- `history_days` = adr_days; `ROBUSTNESS_PARAMS` = adr_days, entry_dev_adr, stop_adr; `TITLE` ghi #5.
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.models import Bar, SignalAction
from core.settings import MissingSetting, Settings
from strategy.base import StrategyContext, history_days
from strategy.vwap_reversion import VwapReversion, session_vwap
from tests.test_strategy_morning_momentum import DAY, SYM, history

VR = {"adr_days": 3, "entry_dev_adr": 0.40, "stop_adr": 0.25, "window_start": "09:29", "window_end": "13:59",
      "max_trades_per_day": 2, "exit_bar": "14:19", "qty": 1}
SESSION = {"continuous_am_start": "09:00", "lunch_start": "11:30", "continuous_pm_start": "13:00"}


def settings(**over) -> Settings:
    return Settings({"session": dict(SESSION), "contract": {"tick_size_points": 0.1},
                     "strategy": {"vwap_reversion": {**VR, **over}}})


def bar(hhmm, px, vol=10, h=None, l=None, day=DAY):
    return Bar(SYM, datetime.combine(day, time.fromisoformat(hhmm)), px, px if h is None else h,
               px if l is None else l, px, vol)


def flat_day(px=1350.0, day=DAY, vol=10):
    """Nến phẳng ở `px`, 09:00 … 11:29 và 13:00 … 14:20 ⇒ VWAP = px."""
    out = []
    for start, n in ((time(9, 0), 150), (time(13, 0), 81)):
        t = datetime.combine(day, start)
        out += [bar((t + timedelta(minutes=i)).strftime("%H:%M"), px, vol, day=day) for i in range(n)]
    return out


def with_spike(bars, hhmm, px, vol=1):
    """Thay nến `hhmm` bằng nến đóng ở `px` (KL nhỏ ⇒ VWAP gần như không đổi)."""
    return [bar(hhmm, px, vol) if b.start.strftime("%H:%M") == hhmm else b for b in bars]


def run(strat, bars, hist, pos=lambda b: 0):
    today, sigs = [], []
    for b in bars:
        today.append(b)
        for s in strat.on_bar(b, StrategyContext(pos(b), None, tuple(today), hist)):
            sigs.append((b.start.strftime("%H:%M"), s))
    return sigs


def entries(sigs):
    return [(at, s) for at, s in sigs if s.action is not SignalAction.EXIT]


# ------------------------------------------------------------------ VWAP ----

def test_session_vwap_typical_price_weighted_skips_zero_volume():
    bars = [Bar(SYM, datetime.combine(DAY, time(8, 59)), 1300.0, 1300.0, 1300.0, 1300.0, 100),   # trước 09:00
            Bar(SYM, datetime.combine(DAY, time(9, 0)), 1350.0, 1353.0, 1347.0, 1350.0, 10),     # TP 1350
            Bar(SYM, datetime.combine(DAY, time(9, 1)), 1360.0, 1360.0, 1360.0, 1360.0, 30),     # TP 1360
            Bar(SYM, datetime.combine(DAY, time(9, 2)), 1400.0, 1400.0, 1400.0, 1400.0, 0)]      # KL 0
    assert session_vwap(bars, time(9, 0)) == pytest.approx((1350 * 10 + 1360 * 30) / 40)
    assert session_vwap(bars[-1:], time(9, 0)) is None


# ------------------------------------------------------------------ cấu hình ----

def test_from_settings_history_days_title_params():
    s = VwapReversion.from_settings(settings())
    assert history_days(s) == 3 and "#5" in VwapReversion.TITLE
    assert VwapReversion.ROBUSTNESS_PARAMS == ("strategy.vwap_reversion.adr_days",
                                               "strategy.vwap_reversion.entry_dev_adr",
                                               "strategy.vwap_reversion.stop_adr")


@pytest.mark.parametrize("bad", [{"adr_days": 0}, {"entry_dev_adr": 0}, {"stop_adr": -1}, {"max_trades_per_day": 0},
                                 {"qty": 1.5}, {"window_start": "x"}, {"window_end": "09:00"}])
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError):
        VwapReversion.from_settings(settings(**bad))


def test_null_value_refused():
    with pytest.raises(MissingSetting):
        VwapReversion.from_settings(settings(entry_dev_adr=None))


def test_repo_settings_load():
    from core.settings import load_settings
    s = load_settings()
    v = s.get("strategy.vwap_reversion")
    assert (v["adr_days"], v["entry_dev_adr"], v["stop_adr"], v["window_start"], v["window_end"],
            v["max_trades_per_day"], v["exit_bar"]) == (10, 0.40, 0.25, "09:29", "13:59", 2, "14:19")
    assert history_days(VwapReversion.from_settings(s)) == 10


# ------------------------------------------------------------------ vào lệnh ----

def test_spike_above_vwap_enters_short_with_tp_at_vwap():
    bars = with_spike(flat_day(1350.0), "10:00", 1360.0)
    (at, s), = entries(run(VwapReversion.from_settings(settings()), bars, history(rng=20.0)))
    assert at == "10:00" and s.action is SignalAction.ENTER_SHORT
    assert s.ts == datetime.combine(DAY, time(10, 1))
    vw = (1350.0 * 10 * 60 + 1360.0 * 1) / (10 * 60 + 1)                # nến 10:00 bị THAY bằng nến lệch
    assert s.take_profit == pytest.approx(round(vw, 1)) and s.stop_loss == pytest.approx(1360.0 + 5.0)
    assert "ADR3 20.0" in s.reason


def test_spike_below_vwap_enters_long():
    bars = with_spike(flat_day(1350.0), "10:00", 1340.0)
    (at, s), = entries(run(VwapReversion.from_settings(settings()), bars, history(rng=20.0)))
    assert s.action is SignalAction.ENTER_LONG and s.stop_loss == pytest.approx(1335.0)
    assert s.take_profit == pytest.approx(1350.0)


@pytest.mark.parametrize("dev,n", [(7.8, 0), (8.2, 1)])
def test_threshold(dev, n):
    """ADR 20 × 0,40 = 8 điểm (VWAP dịch nhẹ vì nến lệch có KL 1)."""
    bars = with_spike(flat_day(1350.0), "10:00", 1350.0 + dev)
    assert len(entries(run(VwapReversion.from_settings(settings()), bars, history(rng=20.0)))) == n


@pytest.mark.parametrize("hhmm,n", [("09:28", 0), ("09:29", 1), ("11:28", 1), ("11:29", 0), ("13:00", 1),
                                    ("13:59", 1), ("14:00", 0)])
def test_window_and_lunch_bar_skipped(hhmm, n):
    bars = with_spike(flat_day(1350.0), hhmm, 1360.0)
    assert len(entries(run(VwapReversion.from_settings(settings()), bars, history(rng=20.0)))) == n


def test_no_history_or_no_volume_no_trade():
    strat = VwapReversion.from_settings(settings())
    bars = with_spike(flat_day(1350.0), "10:00", 1360.0)
    assert run(strat, bars, ()) == []
    zero = [bar(b.start.strftime("%H:%M"), b.close, 0) for b in bars]
    assert run(VwapReversion.from_settings(settings()), zero, history()) == []


def test_in_position_no_entry():
    bars = with_spike(flat_day(1350.0), "10:00", 1360.0)
    assert entries(run(VwapReversion.from_settings(settings()), bars, history(), pos=lambda b: -1)) == []


def test_max_two_entries_per_day():
    bars = flat_day(1350.0)
    for hhmm in ("10:00", "10:30", "11:00", "13:30"):
        bars = with_spike(bars, hhmm, 1360.0)
    sigs = entries(run(VwapReversion.from_settings(settings()), bars, history(rng=20.0)))
    assert [at for at, _ in sigs] == ["10:00", "10:30"]


def test_counter_resets_next_day():
    strat = VwapReversion.from_settings(settings())
    d2 = DAY + timedelta(days=1)
    b1 = with_spike(with_spike(flat_day(1350.0), "10:00", 1360.0), "10:30", 1360.0)
    b2 = [bar(b.start.strftime("%H:%M"), b.close, b.volume, day=d2) for b in with_spike(flat_day(1350.0), "10:00", 1360.0)]
    assert len(entries(run(strat, b1, history(rng=20.0)))) == 2
    assert len(entries(run(strat, b2, history(rng=20.0, end=d2)))) == 1


# ------------------------------------------------------------------ thoát ----

def test_exit_at_1419_once_when_holding():
    held = lambda b: -1 if b.start.time() >= time(10, 1) else 0   # noqa: E731
    sigs = run(VwapReversion.from_settings(settings()), with_spike(flat_day(), "10:00", 1360.0), history(), pos=held)
    ex = [at for at, s in sigs if s.action is SignalAction.EXIT]
    assert ex == ["14:19"]


# ------------------------------------------------------------------ tích hợp ----

def test_engine_short_spike_takes_profit_at_vwap(session_cfg, cal):
    from backtest.engine import BacktestEngine
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg, max_trades_per_day=5, last_entry_time="14:15")
    s._data["strategy"] = {"vwap_reversion": {**VR, "adr_days": 2}}
    bars = []
    for d in (date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)):
        day = flat_day(1350.0, day=d)
        day = [bar("09:05", 1350.0, 10, h=1360.0, l=1340.0, day=d) if b.start.time() == time(9, 5) else b
               for b in day]                                           # biên độ ngày 20
        if d == date(2026, 11, 4):
            day = [bar("10:00", 1360.0, 1, day=d) if b.start.time() == time(10, 0)
                   else bar("10:05", 1350.0, 10, l=1348.0, day=d) if b.start.time() == time(10, 5)   # quay về VWAP
                   else b for b in day]
        bars += day
    bars.sort(key=lambda x: x.start)
    res = BacktestEngine(s, VwapReversion.from_settings(s), calendar=cal).run_bars(bars)
    (tr,) = res.trades
    assert tr.side == "SHORT" and tr.entry_ts.date() == date(2026, 11, 4)
    assert tr.exit_reason == "take_profit"


def test_robustness_cli_runs_vwap(monkeypatch, tmp_path):
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
        R.main(["--bars", str(tmp_path), "--split", "2026-09-01",
                "--strategy", "strategy.vwap_reversion:VwapReversion"])
    assert isinstance(captured["strat"], VwapReversion) and captured["params"] == VwapReversion.ROBUSTNESS_PARAMS
