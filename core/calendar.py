"""Lịch ngày giao dịch: bỏ thứ 7, CN và ngày trong config/holidays.yaml.

Mọi hàm nhận `holidays` (tập ngày nghỉ) làm tham số để thuần và dễ test;
`TradingCalendar.default()` nạp từ file cấu hình.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable


@dataclass(frozen=True)
class TradingCalendar:
    holidays: frozenset[date] = field(default_factory=frozenset)

    @classmethod
    def from_dates(cls, dates: Iterable[date]) -> "TradingCalendar":
        return cls(frozenset(dates))

    @classmethod
    def default(cls) -> "TradingCalendar":
        from core.settings import load_holidays
        return cls(frozenset(load_holidays()))

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def previous_trading_day(self, d: date, inclusive: bool = False) -> date:
        cur = d if inclusive else d - timedelta(days=1)
        for _ in range(60):
            if self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        raise ValueError(f"Không tìm thấy ngày giao dịch trong 60 ngày trước {d}")

    def next_trading_day(self, d: date, inclusive: bool = False) -> date:
        cur = d if inclusive else d + timedelta(days=1)
        for _ in range(60):
            if self.is_trading_day(cur):
                return cur
            cur += timedelta(days=1)
        raise ValueError(f"Không tìm thấy ngày giao dịch trong 60 ngày sau {d}")

    def add_trading_days(self, d: date, n: int) -> date:
        """Dịch n phiên (n âm = lùi). n=0 trả chính d nếu là ngày giao dịch, không thì phiên kế tiếp."""
        if n == 0:
            return self.next_trading_day(d, inclusive=True)
        cur = d
        step = self.next_trading_day if n > 0 else self.previous_trading_day
        for _ in range(abs(n)):
            cur = step(cur)
        return cur

    def trading_days_between(self, start: date, end: date) -> int:
        """Số phiên trong (start, end] — dùng để đếm 'còn bao nhiêu phiên tới đáo hạn'."""
        if end <= start:
            return 0
        count, cur = 0, start
        while cur < end:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                count += 1
        return count
