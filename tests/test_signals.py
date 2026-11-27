"""Tín hiệu đã đăng ký trước cho giai đoạn đo tín hiệu (`strategy/signals.py`) — viết TRƯỚC khi implement.

Định nghĩa CHỐT ở research/log.md, "Đăng ký trước #10, #11, #12" (27/11/2026). Hàm thuần `(bars_today, history)`.
- #10 `volume_flow`: F = Σ dấu(đóng − mở) × KL / Σ KL trên 30 nến gần nhất của hôm nay.
- #11 `intraday_trend`: T = (đóng nến hiện tại − mở nến đầu tiên hôm nay) / ADR10.
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.models import Bar
from strategy.signals import intraday_trend, volume_flow

SYM = "VN30F2611"
DAY = date(2026, 11, 9)


def b(i, o, c, vol=10, day=DAY, reliable=True):
    ts = datetime.combine(day, time(9, 0)) + timedelta(minutes=i)
    return Bar(SYM, ts, o, max(o, c), min(o, c), c, vol, 60, reliable)


def up(i, vol=10, **kw):
    return b(i, 100.0, 101.0, vol, **kw)


def down(i, vol=10, **kw):
    return b(i, 101.0, 100.0, vol, **kw)


# ------------------------------------------------------------ #10 ----
def test_flow_needs_30_bars_today():
    assert volume_flow([up(i) for i in range(29)], ()) is None
    assert volume_flow([up(i) for i in range(30)], ()) == pytest.approx(1.0)


def test_flow_is_signed_volume_share():
    bars = [up(i) for i in range(20)] + [down(i) for i in range(20, 30)]
    assert volume_flow(bars, ()) == pytest.approx((200 - 100) / 300)
    assert volume_flow([down(i) for i in range(30)], ()) == pytest.approx(-1.0)


def test_flow_weights_by_volume_not_bar_count():
    bars = [up(i, vol=1) for i in range(29)] + [down(29, vol=100)]
    assert volume_flow(bars, ()) == pytest.approx((29 - 100) / 129)


def test_flow_doji_counts_in_volume_only():
    bars = [up(i) for i in range(15)] + [b(i, 100.0, 100.0) for i in range(15, 30)]
    assert volume_flow(bars, ()) == pytest.approx(150 / 300)


def test_flow_uses_only_last_30_bars():
    bars = [down(i, vol=1000) for i in range(10)] + [up(i) for i in range(10, 40)]
    assert volume_flow(bars, ()) == pytest.approx(1.0)
    assert volume_flow(bars[:39], ()) == pytest.approx((290 - 1000) / 1290)     # nến giảm cuối cùng còn trong cửa sổ


def test_flow_refuses_unreliable_volume_inside_window_only():
    bars = [up(i) for i in range(40)]
    bars[9] = up(9, reliable=False)                 # ngoài 30 nến cuối
    assert volume_flow(bars, ()) == pytest.approx(1.0)
    bars[10] = up(10, reliable=False)               # nến đầu của cửa sổ
    assert volume_flow(bars, ()) is None


def test_flow_none_without_volume():
    assert volume_flow([up(i, vol=0) for i in range(30)], ()) is None


def test_flow_ignores_history_and_needs_none():
    hist = tuple(down(i, vol=10_000, day=DAY - timedelta(days=3)) for i in range(50))
    assert volume_flow([up(i) for i in range(30)], hist) == pytest.approx(1.0)
    assert int(getattr(volume_flow, "history_days", 0) or 0) == 0


# ------------------------------------------------------------ #11 ----
def history(n_days=10, rng=20.0):
    out = []
    for k in range(1, n_days + 1):
        d = DAY - timedelta(days=k)
        ts = datetime.combine(d, time(9, 0))
        out.append(Bar(SYM, ts, 1000.0, 1000.0 + rng, 1000.0, 1000.0 + rng / 2, 10))
    return tuple(out)


def test_trend_declares_history_days():
    assert intraday_trend.history_days == 10


def test_trend_is_move_from_first_open_in_adr_units():
    today = [b(0, 1300.0, 1302.0), b(1, 1302.0, 1301.0), b(2, 1301.0, 1305.0)]
    assert intraday_trend(today, history()) == pytest.approx((1305.0 - 1300.0) / 20.0)
    assert intraday_trend(today[:2], history()) == pytest.approx((1301.0 - 1300.0) / 20.0)
    assert intraday_trend([b(0, 1300.0, 1296.0)], history()) == pytest.approx(-0.2)


def test_trend_none_without_full_adr_history():
    today = [b(0, 1300.0, 1302.0)]
    assert intraday_trend(today, history(9)) is None
    assert intraday_trend(today, ()) is None
    assert intraday_trend(today, history(10)) is not None


def test_trend_adr_ignores_bars_of_today_in_history():
    today = [b(0, 1300.0, 1304.0)]
    leak = Bar(SYM, datetime.combine(DAY, time(9, 0)), 1300.0, 2300.0, 1300.0, 1304.0, 10)
    assert intraday_trend(today, history() + (leak,)) == pytest.approx(4.0 / 20.0)


def test_trend_none_without_bars():
    assert intraday_trend([], history()) is None
