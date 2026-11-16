"""Chiến lược #7 — đảo chiều qua ngày (`strategy/prev_day_reversal.py`). Viết TRƯỚC khi implement.

Đặc tả (đăng ký trước 16/11/2026, research/log.md mục "Đăng ký trước #7, #8, #9"):
- Phiên trước = đúng `prev_trading_day(hôm nay)` (theo lịch nghỉ) trong `ctx.history` (back-adjust); không có ⇒ không
  giao dịch (không lấy phiên cũ hơn). m = đóng nến CUỐI − mở nến ĐẦU của phiên đó.
- ADR = `adr_before(history, hôm nay, adr_days)`; thiếu ⇒ không giao dịch.
- Quyết định MỘT lần ở nến có giờ mở ĐÚNG `decision_bar` (09:15) khi nến đóng; thiếu nến đó ⇒ cả ngày không vào.
  |m| ≥ min_move_adr × ADR và đang phẳng ⇒ vào NGƯỢC dấu m; stop = đóng ± stop_adr × ADR (về phía ngược lệnh),
  làm tròn bước giá; KHÔNG chốt lời. Tối đa 1 tín hiệu vào / ngày.
- Đang có vị thế và nến có giờ mở ≥ `exit_bar` (14:19) đóng ⇒ EXIT (một lần / ngày).
- `history_days` = adr_days; `ROBUSTNESS_PARAMS` = adr_days, min_move_adr, stop_adr; `TITLE` ghi #7.
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.models import SignalAction
from core.settings import MissingSetting, Settings
from strategy.base import StrategyContext, history_days
from strategy.prev_day_reversal import PrevDayReversal
from tests.test_strategy_gap_fade import FRI, weekdays
from tests.test_strategy_morning_momentum import DAY, b, history

PR = {"adr_days": 3, "min_move_adr": 0.50, "stop_adr": 0.40, "decision_bar": "09:15", "exit_bar": "14:19", "qty": 1}


def settings(**over) -> Settings:
    return Settings({"session": {"continuous_am_start": "09:00"}, "contract": {"tick_size_points": 0.1},
                     "strategy": {"prev_day_reversal": {**PR, **over}}})


def pr(prev=weekdays, **over):
    return PrevDayReversal.from_settings(settings(**over), prev_trading_day=prev)


def hist(o, c, rng=20.0, prev_day=FRI):
    """history(rng) nhưng phiên `prev_day` mở `o`, đóng `c` (nến 14:45), biên độ vẫn đúng `rng` ⇒ ADR = rng."""
    base = tuple(x for x in history(rng=rng) if x.start.date() != prev_day)
    pad = (rng - abs(c - o)) / 2
    lo, hi = min(o, c) - pad, max(o, c) + pad
    return base + (b("09:00", o, o, day=prev_day), b("10:00", o, o, h=hi, l=lo, day=prev_day),
                   b("14:45", c, c, day=prev_day))


def day_bars(price=1350.0, day=DAY, skip=()):
    out = []
    for start, n in ((time(9, 0), 150), (time(13, 0), 81)):
        t = datetime.combine(day, start)
        for i in range(n):
            hhmm = (t + timedelta(minutes=i)).strftime("%H:%M")
            if hhmm not in skip:
                out.append(b(hhmm, price, day=day))
    return out


def run(strat, bars, hist_, pos=lambda bar: 0):
    today, sigs = [], []
    for bar in bars:
        today.append(bar)
        for s in strat.on_bar(bar, StrategyContext(pos(bar), None, tuple(today), hist_)):
            sigs.append((bar.start.strftime("%H:%M"), s))
    return sigs


def entries(sigs):
    return [(at, s) for at, s in sigs if s.action is not SignalAction.EXIT]


# ------------------------------------------------------------------ cấu hình ----

def test_from_settings_history_days_title_params():
    assert history_days(pr()) == 3 and "#7" in PrevDayReversal.TITLE
    assert PrevDayReversal.ROBUSTNESS_PARAMS == ("strategy.prev_day_reversal.adr_days",
                                                 "strategy.prev_day_reversal.min_move_adr",
                                                 "strategy.prev_day_reversal.stop_adr")


@pytest.mark.parametrize("bad", [{"adr_days": 0}, {"adr_days": 1.5}, {"min_move_adr": 0}, {"stop_adr": -0.1},
                                 {"qty": 0}, {"decision_bar": "9h15"}, {"exit_bar": "24:01"},
                                 {"decision_bar": "09:00"}, {"exit_bar": "09:15"}])
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError):
        pr(**bad)


def test_null_value_refused():
    with pytest.raises(MissingSetting):
        pr(min_move_adr=None)


def test_repo_settings_load_and_default_calendar():
    from core.settings import load_settings
    s = load_settings()
    g = s.get("strategy.prev_day_reversal")
    assert (g["adr_days"], g["min_move_adr"], g["stop_adr"], g["decision_bar"], g["exit_bar"], g["qty"]) == \
        (10, 0.50, 0.40, "09:15", "14:19", 1)
    strat = PrevDayReversal.from_settings(s)
    assert history_days(strat) == 10 and strat.prev_trading_day(date(2026, 11, 9)) == FRI


# ------------------------------------------------------------------ vào lệnh ----

def test_up_day_yesterday_shorts_at_0916_without_take_profit():
    """Phiên trước 1340 → 1352 (m = +12), ADR 20 ⇒ ngưỡng 10 ⇒ Short lúc 09:16; stop = 1350 + 8."""
    (at, s), = run(pr(), day_bars(1350.0), hist(1340.0, 1352.0))
    assert at == "09:15" and s.action is SignalAction.ENTER_SHORT
    assert s.ts == datetime.combine(DAY, time(9, 16))
    assert s.stop_loss == pytest.approx(1358.0) and s.take_profit is None and s.qty == 1
    assert "m=+12.0" in s.reason and "ADR3 20.0" in s.reason


def test_down_day_yesterday_goes_long():
    (_, s), = run(pr(), day_bars(1350.0), hist(1352.0, 1340.0))
    assert s.action is SignalAction.ENTER_LONG and s.stop_loss == pytest.approx(1342.0) and s.take_profit is None


@pytest.mark.parametrize("move,n", [(9.9, 0), (10.0, 1), (-10.0, 1), (-9.9, 0), (0.0, 0)])
def test_threshold_edges(move, n):
    assert len(entries(run(pr(), day_bars(), hist(1340.0, 1340.0 + move)))) == n


def test_move_uses_first_open_and_last_close_of_previous_session():
    """Nến giữa phiên (đỉnh/đáy) không phải mốc; mở = nến đầu, đóng = nến cuối."""
    h = hist(1340.0, 1352.0) + (b("14:30", 1338.0, 1338.0, day=FRI),)    # nến 14:30 (trong biên) trước nến cuối 14:45
    (_, s), = run(pr(), day_bars(), h)
    assert "m=+12.0" in s.reason


def test_missing_previous_session_no_trade():
    h = tuple(x for x in history(n=4, rng=20.0) if x.start.date() != FRI)
    assert run(pr(), day_bars(), h) == []


def test_previous_session_follows_calendar_holiday():
    thu = date(2026, 11, 5)
    prev = lambda d: thu if d == DAY else weekdays(d)                 # noqa: E731
    h = hist(1340.0, 1352.0, prev_day=thu)
    (_, s), = run(pr(prev=prev), day_bars(), h)
    assert s.action is SignalAction.ENTER_SHORT


def test_missing_adr_history_no_trade():
    h = tuple(x for x in hist(1340.0, 1352.0) if x.start.date() == FRI)    # chỉ 1 phiên < adr_days 3
    assert run(pr(), day_bars(), h) == []


# ------------------------------------------------------------------ một lần / ngày ----

def test_decides_once_and_missing_decision_bar_means_no_trade():
    assert run(pr(), day_bars(skip=("09:15",)), hist(1340.0, 1352.0)) == []
    assert len(entries(run(pr(), day_bars(), hist(1340.0, 1352.0)))) == 1


def test_no_entry_while_in_position():
    assert entries(run(pr(), day_bars(), hist(1340.0, 1352.0), pos=lambda bar: -1)) == []


def test_exit_at_exit_bar_once():
    sigs = run(pr(), day_bars(), hist(1340.0, 1352.0),
               pos=lambda bar: -1 if bar.start.time() > time(9, 15) else 0)
    exits = [(at, s) for at, s in sigs if s.action is SignalAction.EXIT]
    assert [at for at, _ in exits] == ["14:19"]
    assert exits[0][1].ts == datetime.combine(DAY, time(14, 20))


def test_stop_rounded_to_tick():
    (_, s), = run(pr(), day_bars(1350.03), hist(1340.0, 1352.0, rng=20.33))
    assert round(s.stop_loss * 10) == pytest.approx(s.stop_loss * 10)
