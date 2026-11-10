"""Chiến lược #3 — đà sáng → chiều (`strategy/morning_momentum.py`). Viết TRƯỚC khi implement.

Đặc tả (đăng ký trước 10/11/2026, research/log.md mục "Đăng ký trước #3, #4, #5"):
- ADR = trung bình (đỉnh − đáy) của đúng `adr_days` phiên TRƯỚC hôm nay trong `ctx.history`
  (`strategy.indicators.adr_before`); thiếu lịch sử hoặc ADR = 0 ⇒ không giao dịch.
- Mở = giá mở nến ĐẦU TIÊN có giờ mở trong [09:00, 09:05); Đóng sáng = giá đóng nến CUỐI có giờ mở trong
  [11:25, 11:30). Thiếu một trong hai (dữ liệu hổng) ⇒ không giao dịch hôm đó (không đoán).
- m = Đóng sáng − Mở. Quyết định MỘT lần ở nến đầu tiên của phiên chiều (giờ mở trong [13:00, 13:05)) khi nến đó
  đóng: |m| ≥ min_move_adr × ADR và đang phẳng ⇒ ENTER theo dấu m; stop = đóng nến quyết định ∓ stop_adr × ADR
  (làm tròn bước giá); không chốt lời. Tối đa 1 tín hiệu vào / ngày.
- Đang có vị thế và nến có giờ mở ≥ `exit_bar` (14:19) đóng ⇒ EXIT (một lần / ngày).
- `history_days` = adr_days. Hàm thuần: không đọc giờ hệ thống, chỉ dùng bar + ctx.
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.models import Bar, SignalAction
from core.settings import MissingSetting, Settings
from strategy.base import StrategyContext, history_days
from strategy.indicators import adr_before
from strategy.morning_momentum import MorningMomentum

SYM = "VN30F2611"
DAY = date(2026, 11, 9)                     # thứ Hai
MM = {"adr_days": 3, "min_move_adr": 0.25, "stop_adr": 0.30, "exit_bar": "14:19", "qty": 1}
SESSION = {"continuous_am_start": "09:00", "lunch_start": "11:30", "continuous_pm_start": "13:00"}


def settings(**over) -> Settings:
    mm = {**MM, **over}
    return Settings({"session": dict(SESSION), "contract": {"tick_size_points": 0.1},
                     "strategy": {"morning_momentum": mm}})


def b(hhmm, o, c=None, h=None, l=None, day=DAY, sym=SYM) -> Bar:
    c = o if c is None else c
    return Bar(sym, datetime.combine(day, time.fromisoformat(hhmm)), o, max(o, c) if h is None else h,
               min(o, c) if l is None else l, c, 10)


def history(n=3, rng=20.0, base=1350.0, end=DAY):
    """n phiên trước `end`, mỗi phiên biên độ đúng `rng` ⇒ ADR = rng."""
    out, d, days = [], end, []
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    for d in sorted(days):
        out += [b("09:00", base, base, base + rng / 2, base, day=d), b("10:00", base, base, base, base - rng / 2, day=d)]
    return tuple(out)


def day_bars(open_=1350.0, am_close=1356.0, pm_close=1356.5, day=DAY, skip=()):
    """09:00 … 11:29 (đi từ open_ tới am_close), 13:00, 13:01 … 14:20."""
    out = []
    t = datetime.combine(day, time(9, 0))
    n = 150
    for i in range(n):
        hhmm = (t + timedelta(minutes=i)).strftime("%H:%M")
        if hhmm in skip:
            continue
        o = open_ + (am_close - open_) * i / n
        c = open_ + (am_close - open_) * (i + 1) / n
        out.append(b(hhmm, round(o, 1), round(c, 1), day=day))
    t = datetime.combine(day, time(13, 0))
    for i in range(81):
        hhmm = (t + timedelta(minutes=i)).strftime("%H:%M")
        if hhmm in skip:
            continue
        out.append(b(hhmm, am_close, pm_close if i == 0 else am_close, day=day))
    return out


def run(strat, bars, hist, pos=lambda bar: 0):
    today, sigs = [], []
    for bar in bars:
        today.append(bar)
        for s in strat.on_bar(bar, StrategyContext(pos(bar), None, tuple(today), hist)):
            sigs.append((bar.start.strftime("%H:%M"), s))
    return sigs


# ------------------------------------------------------------------ chỉ báo ----

def test_adr_before_uses_last_n_previous_sessions_only():
    h = history(n=5, rng=20.0)
    leak = (b("09:00", 1350.0, 1350.0, 1500.0, 1200.0),)            # hôm nay — không được tính
    assert adr_before(h + leak, DAY, 3) == pytest.approx(20.0)
    assert adr_before(h, DAY, 6) is None                              # thiếu phiên
    flat = tuple(b("09:00", 1350.0, day=d.start.date()) for d in h)   # biên độ 0
    assert adr_before(flat, DAY, 3) is None


# ------------------------------------------------------------------ cấu hình ----

def test_from_settings_and_history_days():
    s = MorningMomentum.from_settings(settings())
    assert history_days(s) == 3


@pytest.mark.parametrize("bad", [{"adr_days": 0}, {"adr_days": 2.5}, {"min_move_adr": 0}, {"stop_adr": -1},
                                 {"qty": 0}, {"exit_bar": "25:00"}])
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError):
        MorningMomentum.from_settings(settings(**bad))


def test_null_value_refused():
    with pytest.raises(MissingSetting):
        MorningMomentum.from_settings(settings(stop_adr=None))


def test_repo_settings_load():
    from core.settings import load_settings
    s = load_settings()
    strat = MorningMomentum.from_settings(s)
    mm = s.get("strategy.morning_momentum")
    assert (mm["adr_days"], mm["min_move_adr"], mm["stop_adr"], mm["exit_bar"]) == (10, 0.25, 0.30, "14:19")
    assert history_days(strat) == 10


# ------------------------------------------------------------------ vào lệnh ----

def test_up_morning_enters_long_at_first_pm_bar():
    sigs = run(MorningMomentum.from_settings(settings()), day_bars(1350.0, 1356.0, 1356.5), history(rng=20.0))
    assert len(sigs) == 1
    at, s = sigs[0]
    assert at == "13:00" and s.action is SignalAction.ENTER_LONG
    assert s.ts == datetime.combine(DAY, time(13, 1))                 # lúc nến 13:00 đóng
    assert s.stop_loss == pytest.approx(1356.5 - 0.30 * 20.0) and s.take_profit is None and s.qty == 1
    assert "m=+6.0" in s.reason and "ADR3 20.0" in s.reason


def test_down_morning_enters_short():
    sigs = run(MorningMomentum.from_settings(settings()), day_bars(1350.0, 1344.0, 1343.5), history(rng=20.0))
    (at, s), = sigs
    assert s.action is SignalAction.ENTER_SHORT and s.stop_loss == pytest.approx(1343.5 + 6.0)


@pytest.mark.parametrize("move,trades", [(4.9, 0), (5.0, 1), (-5.0, 1), (-4.9, 0)])
def test_threshold_edges(move, trades):
    """ADR 20, min_move_adr 0,25 ⇒ ngưỡng đúng 5 điểm (≥ thì vào)."""
    sigs = run(MorningMomentum.from_settings(settings()), day_bars(1350.0, 1350.0 + move, 1350.0 + move),
               history(rng=20.0))
    assert len([s for _, s in sigs if s.action is not SignalAction.EXIT]) == trades


def test_stop_rounded_to_tick():
    sigs = run(MorningMomentum.from_settings(settings(stop_adr=0.33)), day_bars(1350.0, 1356.0, 1356.5),
               history(rng=20.0))
    assert sigs[0][1].stop_loss == pytest.approx(1349.9)              # 1356,5 − 6,6


def test_no_history_or_short_history_no_trade():
    strat = MorningMomentum.from_settings(settings())
    assert run(strat, day_bars(), ()) == []
    assert run(strat, day_bars(), history(n=2)) == []


@pytest.mark.parametrize("skip", [("09:00", "09:01", "09:02", "09:03", "09:04"),
                                  ("11:25", "11:26", "11:27", "11:28", "11:29"),
                                  ("13:00", "13:01", "13:02", "13:03", "13:04")])
def test_missing_anchor_bars_no_trade(skip):
    assert run(MorningMomentum.from_settings(settings()), day_bars(skip=skip), history()) == []


def test_late_first_bar_within_window_still_used():
    """Thiếu nến 09:00 nhưng có 09:02 ⇒ dùng mở của 09:02 (trong cửa sổ 5 phút)."""
    sigs = run(MorningMomentum.from_settings(settings()), day_bars(skip=("09:00", "09:01")), history())
    assert len(sigs) == 1


def test_already_in_position_no_entry():
    sigs = run(MorningMomentum.from_settings(settings()), day_bars(), history(), pos=lambda bar: 1)
    assert not [s for _, s in sigs if s.action is not SignalAction.EXIT]


def test_only_one_entry_per_day():
    strat = MorningMomentum.from_settings(settings())
    assert len(run(strat, day_bars(), history())) == 1
    again = run(strat, day_bars(), history())                        # cùng ngày, chạy lại: không vào lần 2
    assert not [s for _, s in again if s.action is not SignalAction.EXIT]


# ------------------------------------------------------------------ thoát ----

def test_exit_at_exit_bar_when_holding_once():
    strat = MorningMomentum.from_settings(settings())
    held = lambda bar: 1 if bar.start.time() >= time(13, 1) else 0   # noqa: E731
    sigs = run(strat, day_bars(), history(), pos=held)
    exits = [(at, s) for at, s in sigs if s.action is SignalAction.EXIT]
    assert [at for at, _ in exits] == ["14:19"]
    assert exits[0][1].ts == datetime.combine(DAY, time(14, 20))


def test_no_exit_when_flat():
    sigs = run(MorningMomentum.from_settings(settings(min_move_adr=5.0)), day_bars(), history())
    assert sigs == []


# ------------------------------------------------------------------ tích hợp ----

def test_engine_trades_at_1301_and_exits_at_1420(session_cfg, cal):
    from backtest.engine import BacktestEngine
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg, max_trades_per_day=5, last_entry_time="14:15")
    s._data["strategy"] = {"morning_momentum": {**MM, "adr_days": 2}}
    days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
    bars = []
    for d in days:
        bars += [x if x.start.time() != time(10, 0) else b("10:00", x.open, x.close, 1366.0, 1346.0, day=d)
                 for x in day_bars(1350.0, 1356.0, 1356.5, day=d)]  # biên độ ngày 20
    bars.sort(key=lambda x: x.start)
    strat = MorningMomentum.from_settings(s)
    res = BacktestEngine(s, strat, calendar=cal).run_bars(bars)
    entries = [(ts, sig) for ts, sig in res.signals if sig.action is SignalAction.ENTER_LONG]
    assert [ts.date() for ts, _ in entries] == [date(2026, 11, 4)]    # 2/11, 3/11 chưa đủ 2 phiên lịch sử
    (tr,) = res.trades
    assert tr.side == "LONG" and tr.entry_ts.time() >= time(13, 1) and tr.entry_ts.time() < time(13, 2)
    assert tr.exit_reason == "exit" and time(14, 20) <= tr.exit_ts.time() < time(14, 21)


# ------------------------------------------------------------------ robustness ----

def test_robustness_cli_runs_chosen_strategy_with_its_params(monkeypatch, tmp_path):
    """`backtest.robustness --strategy strategy.morning_momentum:MorningMomentum`: chạy đúng chiến lược, độ nhạy
    trên đúng 3 tham số đăng ký trước, tiêu đề báo cáo ghi #3."""
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
        R.main(["--bars", str(tmp_path), "--split", "2026-09-01", "--n-trials", "5",
                "--strategy", "strategy.morning_momentum:MorningMomentum"])
    assert isinstance(captured["strat"], MorningMomentum)
    assert captured["params"] == ("strategy.morning_momentum.adr_days", "strategy.morning_momentum.min_move_adr",
                                  "strategy.morning_momentum.stop_adr")
    assert "#3" in MorningMomentum.TITLE


def test_robustness_cli_default_is_still_orb(monkeypatch, tmp_path):
    import backtest.robustness as R
    from strategy.baseline_orb import OpeningRangeBreakout
    from tests.test_strategy_orb_filter import settings as orb_settings
    s = orb_settings()
    monkeypatch.setattr(R, "load_settings", lambda: s)
    monkeypatch.setattr(R, "BacktestEngine", lambda *a, **k: None)
    monkeypatch.setattr(R, "load_bars_parquet", lambda *a, **k: ([], 0))
    captured = {}

    def fake_run(settings_, make, bars, split, params, **kw):
        captured.update(params=params, strat=make(settings_))
        raise SystemExit(0)
    monkeypatch.setattr(R, "run_robustness", fake_run)
    with pytest.raises(SystemExit):
        R.main(["--bars", str(tmp_path), "--split", "2026-09-01", "--n-trials", "2"])
    assert isinstance(captured["strat"], OpeningRangeBreakout) and captured["params"] == R.params_for(s)


def test_robustness_cli_bad_strategy_exits_2(tmp_path):
    import backtest.robustness as R
    assert R.main(["--bars", str(tmp_path), "--split", "2026-09-01", "--strategy", "strategy.khong_co:X"]) == 2
