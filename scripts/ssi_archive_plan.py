"""Danh sách lệnh tải SSI (`marketdata_list_intraday_stock_infor`) cho kho lưu trữ nến 1 phút hằng tuần
(docs/ssi_archive.md). Mỗi dòng: `<mã> <range> <toDate>`.

    python scripts/ssi_archive_plan.py --to 2026-11-20

Khung = 7 ngày lịch kết thúc ở `--to` (= `range 1w`). Mã: chỉ số VN30 + mọi HĐ là F1M hoặc F2M ở một ngày giao dịch
trong khung (phủ ngày đáo hạn, ngày trước roll và HĐ mới).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts                        # noqa: E402
from core.calendar import TradingCalendar         # noqa: E402

INDEX = "VN30"


def plan(to: date, calendar: TradingCalendar) -> list[tuple[str, str, str]]:
    syms: list[str] = []
    for i in range(6, -1, -1):
        d = to - timedelta(days=i)
        if not calendar.is_trading_day(d):
            continue
        listed = contracts.listed_contracts(d, calendar)
        for key in ("VN30F1M", "VN30F2M"):
            krx = contracts.krx_symbol(*contracts.contract_ym(listed[key]))
            if krx not in syms:
                syms.append(krx)
    to_s = to.strftime("%d/%m/%Y")
    return [(s, "1w", to_s) for s in [INDEX] + sorted(syms)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Lệnh tải SSI cho kho lưu trữ tuần")
    ap.add_argument("--to", required=True, help="YYYY-MM-DD (ngày cuối khung, thường là thứ Sáu)")
    args = ap.parse_args(argv)
    for sym, rng, to in plan(date.fromisoformat(args.to), TradingCalendar.default()):
        print(f"{sym} {rng} {to}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
