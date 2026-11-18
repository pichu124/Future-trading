"""Chiến lược #9 — đà cuối phiên (`strategy/late_momentum.py`). Viết TRƯỚC khi implement.

Đặc tả (đăng ký trước 16/11/2026, research/log.md mục "Đăng ký trước #7, #8, #9"):
- C₋₁ = đóng nến CUỐI của đúng `prev_trading_day(hôm nay)` trong `ctx.history`; không có ⇒ không giao dịch.
- ADR = `adr_before(history, hôm nay, adr_days)`; thiếu ⇒ không giao dịch.
- Quyết định MỘT lần ở nến có giờ mở ĐÚNG `decision_bar` (13:49) khi nến đóng; thiếu nến đó ⇒ cả ngày không vào.
  r = đóng − C₋₁; |r| ≥ min_move_adr × ADR và đang phẳng ⇒ vào THEO dấu r; stop = đóng ∓ stop_adr × ADR, làm tròn
  bước giá; KHÔNG chốt lời. Tối đa 1 tín hiệu vào / ngày.
- Đang có vị thế và nến có giờ mở ≥ `exit_bar` (14:19) đóng ⇒ EXIT (một lần / ngày).
- `history_days` = adr_days; `ROBUSTNESS_PARAMS` = adr_days, min_move_adr, stop_adr; `TITLE` ghi #9.
"""
from datetime import date, datetime, time

import pytest

from core.models import SignalAction
from core.settings import MissingSetting, Settings
from strategy.base import history_days
from strategy.late_momentum import LateMomentum
from tests.test_strategy_gap_fade import FRI, weekdays
from tests.test_strategy_morning_momentum import DAY, b, history
from tests.test_strategy_prev_day_reversal import day_bars, entries, run

LM = {"adr_days": 3, "min_move_adr": 0.30, "stop_adr": 0.20, "decision_bar": "13:49", "exit_bar": "14:19", "qty": 1}


def settings(**over) -> Settings:
    return Settings({"session": {"continuous_am_start": "09:00"}, "contract": {"tick_size_points": 0.1},
                     "strategy": {"late_momentum": {**LM, **over}}})


def lm(prev=weekdays, **over):
    return LateMomentum.from_settings(settings(**over), prev_trading_day=prev)


def test_from_settings_history_days_title_params():
    assert history_days(lm()) == 3 and "#9" in LateMomentum.TITLE
    assert LateMomentum.ROBUSTNESS_PARAMS == ("strategy.late_momentum.adr_days", "strategy.late_momentum.min_move_adr",
                                              "strategy.late_momentum.stop_adr")


@pytest.mark.parametrize("bad", [{"adr_days": 0}, {"min_move_adr": 0}, {"stop_adr": -0.1}, {"qty": 0},
                                 {"decision_bar": "13h49"}, {"decision_bar": "08:59"}, {"exit_bar": "13:49"}])
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError):
        lm(**bad)


def test_null_value_refused():
    with pytest.raises(MissingSetting):
        lm(stop_adr=None)


def test_repo_settings_load():
    from core.settings import load_settings
    s = load_settings()
    g = s.get("strategy.late_momentum")
    assert (g["adr_days"], g["min_move_adr"], g["stop_adr"], g["decision_bar"], g["exit_bar"], g["qty"]) == \
        (10, 0.30, 0.20, "13:49", "14:19", 1)
    assert history_days(LateMomentum.from_settings(s)) == 10


def test_up_from_previous_close_goes_long_at_1350():
    """C₋₁ = 1350, ADR 20 ⇒ ngưỡng 6; 13:49 ở 1357 ⇒ r = +7 ⇒ Long lúc 13:50; stop = 1357 − 4."""
    (at, s), = run(lm(), day_bars(1357.0), history(rng=20.0))
    assert at == "13:49" and s.action is SignalAction.ENTER_LONG
    assert s.ts == datetime.combine(DAY, time(13, 50))
    assert s.stop_loss == pytest.approx(1353.0) and s.take_profit is None and s.qty == 1
    assert "r=+7.0" in s.reason and "ADR3 20.0" in s.reason


def test_down_goes_short():
    (_, s), = run(lm(), day_bars(1343.0), history(rng=20.0))
    assert s.action is SignalAction.ENTER_SHORT and s.stop_loss == pytest.approx(1347.0)


@pytest.mark.parametrize("move,n", [(5.9, 0), (6.0, 1), (-6.0, 1), (-5.9, 0), (0.0, 0)])
def test_threshold_edges(move, n):
    assert len(entries(run(lm(), day_bars(1350.0 + move), history(rng=20.0)))) == n


def test_previous_close_is_last_bar_of_previous_session():
    h = history(rng=20.0) + (b("14:45", 1352.0, day=FRI),)
    (_, s), = run(lm(), day_bars(1358.0), h)
    assert "r=+6.0" in s.reason
    assert run(lm(), day_bars(1357.0), h) == []


def test_decision_uses_close_of_1349_not_other_bars():
    bars = [x if x.start.time() != time(13, 49) else b("13:49", 1350.0, 1357.0) for x in day_bars(1350.0)]
    (_, s), = run(lm(), bars, history(rng=20.0))
    assert s.action is SignalAction.ENTER_LONG and "r=+7.0" in s.reason


def test_missing_previous_session_or_history_no_trade():
    h = tuple(x for x in history(n=4, rng=20.0) if x.start.date() != FRI)
    assert run(lm(), day_bars(1357.0), h) == []
    assert run(lm(), day_bars(1357.0), tuple(x for x in history(rng=20.0) if x.start.date() == FRI)) == []


def test_previous_session_follows_calendar_holiday():
    thu = date(2026, 11, 5)
    prev = lambda d: thu if d == DAY else weekdays(d)                 # noqa: E731
    h = tuple(x for x in history(n=4, rng=20.0) if x.start.date() != FRI)
    (_, s), = run(lm(prev=prev), day_bars(1357.0), h)
    assert s.action is SignalAction.ENTER_LONG


def test_once_per_day_missing_bar_and_position():
    assert run(lm(), day_bars(1357.0, skip=("13:49",)), history(rng=20.0)) == []
    assert entries(run(lm(), day_bars(1357.0), history(rng=20.0), pos=lambda bar: 1)) == []


def test_exit_at_exit_bar_once():
    sigs = run(lm(), day_bars(1357.0), history(rng=20.0),
               pos=lambda bar: 1 if bar.start.time() > time(13, 49) else 0)
    exits = [(at, s) for at, s in sigs if s.action is SignalAction.EXIT]
    assert [at for at, _ in exits] == ["14:19"] and exits[0][1].ts == datetime.combine(DAY, time(14, 20))
