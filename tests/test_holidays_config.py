"""config/holidays.yaml — 2026 đã đối chiếu với dữ liệu ngày VN30 (DNSE, 10/11/2025–06/11/2026).

Dữ liệu thật: đúng 13 ngày thường dưới đây KHÔNG có phiên, mọi ngày thường khác trong khoảng đó đều có phiên.
Sửa danh sách 2026 mà test này đỏ ⇒ đang làm lệch lịch đã được kiểm.
"""
from datetime import date, timedelta

from core import contracts
from core.calendar import TradingCalendar

NO_SESSION_2026 = {date(2026, 1, 1), date(2026, 1, 2), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
                   date(2026, 2, 19), date(2026, 2, 20), date(2026, 4, 27), date(2026, 4, 30), date(2026, 5, 1),
                   date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)}


def test_2026_matches_observed_sessions():
    cal = TradingCalendar.default()
    d, end = date(2025, 11, 10), date(2026, 11, 6)
    closed = set()
    while d <= end:
        if d.weekday() < 5 and not cal.is_trading_day(d):
            closed.add(d)
        d += timedelta(days=1)
    assert closed == NO_SESSION_2026


def test_feb_2026_expiry_moves_before_tet():
    cal = TradingCalendar.default()
    assert contracts.contract_expiry("VN30F2602", cal) == date(2026, 2, 13)
