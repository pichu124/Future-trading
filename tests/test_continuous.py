"""Chuỗi back-adjust cho chiến lược nhiều ngày (`core/continuous.py`) — viết TRƯỚC khi implement.

Quyết định của người dùng: làm module + backtest trước (live/paper TỪ CHỐI chiến lược cần lịch sử tới khi
nối riêng); dịch giá bằng CỘNG/TRỪ điểm: lịch sử HĐ cũ + (giá HĐ mới − giá HĐ cũ).

- Chênh lệch đo bằng giá đóng của hai HĐ ở PHÚT CHUNG CUỐI CÙNG của ngày trước roll; không có phút chung
  ⇒ không đo được ⇒ lịch sử bị CẮT tại roll đó (không đoán).
- Điều chỉnh "tại thời điểm": lịch sử thấy ở ngày X chỉ dùng các roll đã xảy ra tới X ⇒ HĐ đang giao dịch
  luôn là giá thật, không có roll tương lai nào lọt vào (không look-ahead).
- Bar đã dịch mang `symbol` = HĐ hiện tại (giá đã quy về HĐ đó), volume giữ nguyên.
"""
from datetime import date, datetime, time, timedelta

import pytest

from core.continuous import ContinuousHistory, RollGap, measure_gap, shift_bar
from core.models import Bar

A, B, C = "VN30F2610", "VN30F2611", "VN30F2612"


def bars(d, sym, price, n=3, hhmm="10:00", step=0.0):
    t0 = datetime.combine(d, time.fromisoformat(hhmm))
    return [Bar(sym, t0 + timedelta(minutes=i), price + i * step, price + i * step + 1, price + i * step - 1,
                price + i * step, 5) for i in range(n)]


D1, D2, D3, D4, D5 = (date(2026, 11, x) for x in (16, 17, 18, 19, 20))


class TestMeasureGap:
    def test_last_common_minute_close_difference(self):
        old = bars(D3, B, 1350.0, n=5, step=1.0)                     # 10:00..10:04, close 1350..1354
        new = bars(D3, C, 1400.0, n=4, hhmm="10:01", step=2.0)       # 10:01..10:04, close 1400..1406
        g = measure_gap(list(reversed(old)), new)                    # thứ tự đầu vào không quan trọng
        assert g == RollGap(old=B, new=C, day=D3, at=datetime(2026, 11, 18, 10, 4), gap=1406.0 - 1354.0)

    def test_no_common_minute_returns_none(self):
        assert measure_gap(bars(D3, B, 1350.0, hhmm="10:00"), bars(D3, C, 1400.0, hhmm="11:00")) is None
        assert measure_gap([], bars(D3, C, 1400.0)) is None
        assert measure_gap(bars(D3, B, 1350.0), []) is None

    def test_mixed_symbols_rejected(self):
        with pytest.raises(ValueError):
            measure_gap(bars(D3, B, 1350.0) + bars(D3, C, 1400.0), bars(D3, C, 1400.0))


def test_shift_bar_moves_prices_keeps_volume_and_validity():
    b = Bar(B, datetime(2026, 11, 18, 10, 0), 1350.0, 1352.0, 1349.0, 1351.0, 7, 60, False)
    s = shift_bar(b, -12.5, C)
    assert (s.symbol, s.open, s.high, s.low, s.close, s.volume, s.interval_s, s.volume_reliable) == \
        (C, 1337.5, 1339.5, 1336.5, 1338.5, 7, 60, False)
    assert s.start == b.start


class TestView:
    def test_same_contract_unchanged_oldest_first(self):
        h = ContinuousHistory(max_days=10)
        h.add_day(D1, B, bars(D1, B, 1350.0))
        h.add_day(D2, B, bars(D2, B, 1360.0))
        v = h.view(B)
        assert [x.start.date() for x in v] == [D1] * 3 + [D2] * 3
        assert v[0].close == 1350.0 and v[-1].close == 1360.0 and {x.symbol for x in v} == {B}

    def test_roll_shifts_old_contract_by_gap(self):
        h = ContinuousHistory(max_days=10)
        h.add_day(D2, B, bars(D2, B, 1350.0))
        h.add_gap(RollGap(B, C, D2, datetime(2026, 11, 17, 14, 29), 48.5))
        h.add_day(D3, C, bars(D3, C, 1400.0))
        v = h.view(C)
        assert [(x.symbol, x.close) for x in v] == [(C, 1398.5)] * 3 + [(C, 1400.0)] * 3
        assert v[0].high == 1399.5 and v[0].low == 1397.5

    def test_view_for_new_contract_on_roll_day(self):
        """Ngày roll: chưa có ngày nào của HĐ mới, lịch sử toàn HĐ cũ đã dịch."""
        h = ContinuousHistory(max_days=10)
        h.add_day(D2, B, bars(D2, B, 1350.0))
        h.add_gap(RollGap(B, C, D2, datetime(2026, 11, 17, 14, 29), 50.0))
        assert [x.close for x in h.view(C)] == [1400.0] * 3

    def test_chained_rolls_sum_gaps(self):
        h = ContinuousHistory(max_days=10)
        h.add_day(D1, A, bars(D1, A, 1300.0))
        h.add_gap(RollGap(A, B, D1, datetime(2026, 11, 16, 14, 29), 10.0))
        h.add_day(D2, B, bars(D2, B, 1320.0))
        h.add_gap(RollGap(B, C, D2, datetime(2026, 11, 17, 14, 29), -5.0))
        h.add_day(D3, C, bars(D3, C, 1330.0))
        v = h.view(C)
        assert [x.close for x in v[::3]] == [1305.0, 1315.0, 1330.0]

    def test_missing_gap_truncates_at_that_roll(self):
        h = ContinuousHistory(max_days=10)
        h.add_day(D1, A, bars(D1, A, 1300.0))
        h.add_gap(RollGap(A, B, D1, datetime(2026, 11, 16, 14, 29), 10.0))
        h.add_day(D2, B, bars(D2, B, 1320.0))
        h.add_day(D3, C, bars(D3, C, 1330.0))                        # không có gap B → C
        assert [x.start.date() for x in h.view(C)] == [D3] * 3

    def test_history_stops_at_first_missing_gap_even_if_older_days_match(self):
        h = ContinuousHistory(max_days=10)
        h.add_day(D1, C, bars(D1, C, 1290.0))                        # dữ liệu lạ: cùng HĐ, trước roll thiếu
        h.add_day(D2, B, bars(D2, B, 1320.0))
        h.add_day(D3, C, bars(D3, C, 1330.0))
        assert [x.start.date() for x in h.view(C)] == [D3] * 3

    def test_max_days_keeps_most_recent(self):
        h = ContinuousHistory(max_days=2)
        for d, px in ((D1, 1300.0), (D2, 1310.0), (D3, 1320.0)):
            h.add_day(d, B, bars(d, B, px))
        assert [x.start.date() for x in h.view(B)] == [D2] * 3 + [D3] * 3

    def test_re_adding_a_day_replaces_it(self):
        h = ContinuousHistory(max_days=5)
        h.add_day(D1, B, bars(D1, B, 1300.0))
        h.add_day(D1, B, bars(D1, B, 1305.0, n=2))
        assert [x.close for x in h.view(B)] == [1305.0, 1305.0]

    def test_days_must_be_added_in_order(self):
        h = ContinuousHistory(max_days=5)
        h.add_day(D2, B, bars(D2, B, 1300.0))
        with pytest.raises(ValueError):
            h.add_day(D1, B, bars(D1, B, 1300.0))

    def test_bars_of_other_contract_in_day_rejected(self):
        h = ContinuousHistory(max_days=5)
        with pytest.raises(ValueError):
            h.add_day(D1, B, bars(D1, C, 1300.0))

    def test_empty_and_invalid(self):
        assert ContinuousHistory(max_days=3).view(B) == ()
        with pytest.raises(ValueError):
            ContinuousHistory(max_days=0)
