"""Kiểm config/holidays.yaml cho năm hiện tại và năm tới.

Cảnh báo khi:
- một năm có ít hơn `MIN_HOLIDAYS` ngày nghỉ ngày thường (thường thiếu Tết, Giỗ Tổ, nghỉ bù);
- có ngày nghỉ rơi vào thứ 7/CN (thừa, có thể gõ nhầm năm).
In luôn lịch đáo hạn VN30F trong hai năm đó để đối chiếu với thông báo của HNX.

    python scripts/check_calendar.py [--year 2027]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts  # noqa: E402
from core.calendar import TradingCalendar  # noqa: E402
from core.settings import load_holidays  # noqa: E402

MIN_HOLIDAYS = 9   # Tết DL + ~5 ngày Tết + Giỗ Tổ + 30/4 + 1/5 + 2/9 (+ bù) — dưới mức này gần như chắc thiếu


def check_year(year: int, holidays: dict[date, str]) -> list[str]:
    issues = []
    in_year = {d: v for d, v in holidays.items() if d.year == year}
    weekday = [d for d in in_year if d.weekday() < 5]
    weekend = [d for d in in_year if d.weekday() >= 5]
    if len(weekday) < MIN_HOLIDAYS:
        issues.append(f"{year}: chỉ có {len(weekday)} ngày nghỉ ngày thường (< {MIN_HOLIDAYS}) — "
                      "bổ sung Tết, Giỗ Tổ, ngày nghỉ bù theo thông báo của sở")
    for d in sorted(weekend):
        issues.append(f"{year}: {d} là cuối tuần — không cần khai")
    return issues


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=date.today().year)
    args = ap.parse_args(argv)
    holidays = load_holidays()
    cal = TradingCalendar.from_dates(holidays)
    issues: list[str] = []
    for year in (args.year, args.year + 1):
        issues += check_year(year, holidays)
        print(f"Đáo hạn VN30F {year}:")
        for m in range(1, 13):
            exp = contracts.expiry_date(year, m, cal)
            moved = "" if exp == contracts.third_thursday(year, m) else "  ← lùi do ngày nghỉ"
            print(f"  {contracts.contract_name(year, m)}  {exp}{moved}")
    if issues:
        print("\n⚠️  Lịch nghỉ cần xem lại:")
        for msg in issues:
            print(f"   - {msg}")
        return 1
    print("\n✅ Lịch nghỉ trông đầy đủ.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
