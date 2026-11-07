"""Đồng hồ & trạng thái phiên giao dịch phái sinh.

- Mọi thời gian trong bot là `datetime` NAIVE theo giờ Asia/Ho_Chi_Minh. `to_vn()` chuyển
  datetime có múi giờ (hoặc UTC) về chuẩn đó.
- `SystemClock` dùng cho live; `FakeClock` cho backtest/test (đặt và tua giờ được).
- Lịch phiên đọc từ `settings.session` (không hardcode giờ trong code).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Mapping, Optional, Protocol
from zoneinfo import ZoneInfo

from core import contracts
from core.calendar import TradingCalendar

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


class SessionState(str, Enum):
    CLOSED = "CLOSED"
    PRE_OPEN = "PRE_OPEN"
    ATO = "ATO"
    CONTINUOUS = "CONTINUOUS"
    LUNCH = "LUNCH"
    ATC = "ATC"


def to_vn(ts: datetime) -> datetime:
    """datetime có tz → naive giờ VN. Naive giữ nguyên (đã được coi là giờ VN)."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(VN_TZ).replace(tzinfo=None)


def _parse_hhmm(value: str) -> time:
    hh, mm = str(value).split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class SessionSchedule:
    pre_open_start: time
    ato_start: time
    continuous_am_start: time
    lunch_start: time
    continuous_pm_start: time
    atc_start: time
    close: time

    def __post_init__(self) -> None:
        order = [self.pre_open_start, self.ato_start, self.continuous_am_start,
                 self.lunch_start, self.continuous_pm_start, self.atc_start, self.close]
        if order != sorted(order) or len(set(order)) != len(order):
            raise ValueError(f"Lịch phiên phải tăng dần nghiêm ngặt: {order}")

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, str]) -> "SessionSchedule":
        fields = ("pre_open_start", "ato_start", "continuous_am_start", "lunch_start",
                  "continuous_pm_start", "atc_start", "close")
        missing = [f for f in fields if not cfg.get(f)]
        if missing:
            raise ValueError(f"settings.session thiếu: {missing}")
        return cls(**{f: _parse_hhmm(cfg[f]) for f in fields})

    def state_at(self, t: time) -> SessionState:
        """Trạng thái theo GIỜ (chưa xét ngày nghỉ). Mỗi mốc là đầu đoạn [mốc, mốc kế)."""
        if t < self.pre_open_start or t >= self.close:
            return SessionState.CLOSED
        if t < self.ato_start:
            return SessionState.PRE_OPEN
        if t < self.continuous_am_start:
            return SessionState.ATO
        if t < self.lunch_start:
            return SessionState.CONTINUOUS
        if t < self.continuous_pm_start:
            return SessionState.LUNCH
        if t < self.atc_start:
            return SessionState.CONTINUOUS
        return SessionState.ATC


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(VN_TZ).replace(tzinfo=None)


class FakeClock:
    """Đồng hồ giả cho backtest/test."""

    def __init__(self, start: datetime):
        self._now = to_vn(start)

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        self._now = to_vn(ts)

    def advance(self, **delta: float) -> datetime:
        self._now = self._now + timedelta(**delta)
        return self._now


class MarketClock:
    """Gộp đồng hồ + lịch phiên + lịch ngày nghỉ + quy tắc roll. Strategy KHÔNG dùng lớp này
    trực tiếp (strategy nhận thời gian qua Bar/Tick); runner, risk và recorder dùng."""

    def __init__(self, schedule: SessionSchedule, calendar: TradingCalendar,
                 clock: Optional[Clock] = None, roll_days_before_expiry: int = 1):
        self.schedule = schedule
        self.calendar = calendar
        self.clock: Clock = clock or SystemClock()
        self.roll_days_before_expiry = roll_days_before_expiry

    @classmethod
    def from_settings(cls, settings, clock: Optional[Clock] = None,
                      calendar: Optional[TradingCalendar] = None) -> "MarketClock":
        return cls(
            schedule=SessionSchedule.from_mapping(settings.get("session", {})),
            calendar=calendar or TradingCalendar.default(),
            clock=clock,
            roll_days_before_expiry=int(settings.get("contract.roll_days_before_expiry", 1)),
        )

    def now(self) -> datetime:
        return self.clock.now()

    def session_state(self, ts: Optional[datetime] = None) -> SessionState:
        ts = to_vn(ts) if ts else self.now()
        if not self.calendar.is_trading_day(ts.date()):
            return SessionState.CLOSED
        return self.schedule.state_at(ts.time())

    def is_trading_day(self, d: Optional[date] = None) -> bool:
        return self.calendar.is_trading_day(d or self.now().date())

    def is_expiry_day(self, d: Optional[date] = None) -> bool:
        return contracts.is_expiry_day(d or self.now().date(), self.calendar)

    def trading_contract(self, d: Optional[date] = None) -> str:
        return contracts.trading_contract(d or self.now().date(), self.calendar,
                                          self.roll_days_before_expiry)

    def next_contract_roll_date(self, d: Optional[date] = None) -> date:
        return contracts.next_contract_roll_date(d or self.now().date(), self.calendar,
                                                 self.roll_days_before_expiry)

    def minutes_since_continuous_open(self, ts: Optional[datetime] = None) -> Optional[float]:
        """Số phút từ đầu phiên liên tục sáng; None ngoài ngày giao dịch hoặc trước giờ mở."""
        ts = to_vn(ts) if ts else self.now()
        if not self.calendar.is_trading_day(ts.date()):
            return None
        start = datetime.combine(ts.date(), self.schedule.continuous_am_start)
        if ts < start:
            return None
        return (ts - start).total_seconds() / 60.0
