import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.calendar import TradingCalendar  # noqa: E402


@pytest.fixture
def cal() -> TradingCalendar:
    """Lịch cố định cho test — không phụ thuộc config/holidays.yaml đang được người dùng sửa."""
    return TradingCalendar.from_dates([
        date(2026, 1, 1),
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
        date(2026, 2, 19), date(2026, 2, 20),
        date(2026, 4, 30), date(2026, 5, 1),
        date(2026, 9, 2),
        date(2027, 1, 1),
    ])


@pytest.fixture
def session_cfg() -> dict:
    return {
        "pre_open_start": "08:30",
        "ato_start": "08:45",
        "continuous_am_start": "09:00",
        "lunch_start": "11:30",
        "continuous_pm_start": "13:00",
        "atc_start": "14:30",
        "close": "14:45",
    }


@pytest.fixture(autouse=True)
def _no_repo_logs(monkeypatch):
    """`live.runner.main()` cài log JSONL vào logs/ của repo — test không được ghi vào đó."""
    monkeypatch.setattr("live.runner.setup_logging", lambda *a, **k: None)
