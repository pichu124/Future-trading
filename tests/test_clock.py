from datetime import date, datetime, timezone

import pytest

from core.calendar import TradingCalendar
from core.clock import FakeClock, MarketClock, SessionSchedule, SessionState, to_vn
from core.settings import Settings


@pytest.fixture
def mclock(cal, session_cfg):
    return MarketClock(SessionSchedule.from_mapping(session_cfg), cal,
                       FakeClock(datetime(2026, 11, 9, 9, 0)))


@pytest.mark.parametrize("hhmmss,state", [
    ("08:29:59", SessionState.CLOSED),
    ("08:30:00", SessionState.PRE_OPEN),
    ("08:44:59", SessionState.PRE_OPEN),
    ("08:45:00", SessionState.ATO),
    ("08:59:59", SessionState.ATO),
    ("09:00:00", SessionState.CONTINUOUS),
    ("11:29:59", SessionState.CONTINUOUS),
    ("11:30:00", SessionState.LUNCH),
    ("12:59:59", SessionState.LUNCH),
    ("13:00:00", SessionState.CONTINUOUS),
    ("14:29:59", SessionState.CONTINUOUS),
    ("14:30:00", SessionState.ATC),
    ("14:44:59", SessionState.ATC),
    ("14:45:00", SessionState.CLOSED),
    ("23:00:00", SessionState.CLOSED),
])
def test_every_session_boundary(mclock, hhmmss, state):
    ts = datetime.fromisoformat(f"2026-11-09T{hhmmss}")   # thứ Hai
    assert mclock.session_state(ts) is state


def test_weekend_and_holiday_are_closed(mclock):
    assert mclock.session_state(datetime(2026, 11, 7, 10, 0)) is SessionState.CLOSED   # thứ Bảy
    assert mclock.session_state(datetime(2026, 9, 2, 10, 0)) is SessionState.CLOSED    # Quốc khánh
    assert mclock.session_state(datetime(2026, 9, 3, 10, 0)) is SessionState.CONTINUOUS


def test_fake_clock_drives_state(mclock):
    assert mclock.session_state() is SessionState.CONTINUOUS
    mclock.clock.advance(hours=2, minutes=30)                 # 11:30
    assert mclock.session_state() is SessionState.LUNCH
    mclock.clock.set(datetime(2026, 11, 9, 14, 31))
    assert mclock.session_state() is SessionState.ATC


def test_timezone_aware_input_converted_to_vn():
    utc = datetime(2026, 11, 9, 2, 0, tzinfo=timezone.utc)
    assert to_vn(utc) == datetime(2026, 11, 9, 9, 0)
    naive = datetime(2026, 11, 9, 9, 0)
    assert to_vn(naive) is naive


def test_schedule_rejects_unordered(session_cfg):
    bad = dict(session_cfg, lunch_start="08:50")
    with pytest.raises(ValueError):
        SessionSchedule.from_mapping(bad)


def test_schedule_rejects_missing_key(session_cfg):
    bad = dict(session_cfg)
    bad.pop("atc_start")
    with pytest.raises(ValueError):
        SessionSchedule.from_mapping(bad)


def test_expiry_and_contract_helpers(mclock):
    mclock.clock.set(datetime(2026, 11, 19, 10, 0))
    assert mclock.is_expiry_day()
    assert mclock.trading_contract() == "VN30F2612"           # roll 1 ⇒ ngày đáo hạn đã sang F2M
    assert mclock.next_contract_roll_date() == date(2026, 12, 17)


def test_minutes_since_open(mclock):
    assert mclock.minutes_since_continuous_open(datetime(2026, 11, 9, 9, 15)) == 15
    assert mclock.minutes_since_continuous_open(datetime(2026, 11, 9, 8, 50)) is None
    assert mclock.minutes_since_continuous_open(datetime(2026, 11, 8, 10, 0)) is None


def test_from_settings_reads_session_and_roll(cal, session_cfg):
    s = Settings({"session": session_cfg, "contract": {"roll_days_before_expiry": 2}})
    mc = MarketClock.from_settings(s, clock=FakeClock(datetime(2026, 11, 18, 10)), calendar=cal)
    assert mc.trading_contract() == "VN30F2612"


def test_calendar_helpers(cal):
    assert cal.previous_trading_day(date(2026, 2, 23)) == date(2026, 2, 13)
    assert cal.next_trading_day(date(2026, 2, 13)) == date(2026, 2, 23)
    assert cal.add_trading_days(date(2026, 11, 19), -2) == date(2026, 11, 17)
    assert cal.add_trading_days(date(2026, 11, 7), 0) == date(2026, 11, 9)
    assert cal.trading_days_between(date(2026, 11, 13), date(2026, 11, 19)) == 4
    assert cal.trading_days_between(date(2026, 11, 19), date(2026, 11, 19)) == 0


def test_default_calendar_loads_repo_file():
    cal = TradingCalendar.default()
    assert date(2026, 9, 2) in cal.holidays
