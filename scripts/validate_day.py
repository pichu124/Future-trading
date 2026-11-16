"""Kiểm định dữ liệu tick một ngày + dựng bar 1 phút cho backtest.

    python scripts/validate_day.py 2026-11-09
    python scripts/validate_day.py 2026-11-09 --ref-price 1352.4 --reference data/imported/2026-11-09.parquet

Đọc `data/raw/<ngày>/` (recorder), in báo cáo, ghi:
- `data/bars/<ngày>.parquet`: bar 1 phút của nguồn chính (`feeds.primary`, không có thì nguồn kia)
  — dùng thẳng cho `python -m backtest.engine --bars data/bars`;
- `data/validation/<ngày>.json`: báo cáo dạng máy đọc.
Tham chiếu mặc định: `data/imported/<ngày>.parquet` (bar vnstock-dashboard) nếu có.
`--ref-price`: giá tham chiếu của sở để kiểm biên độ ±7% (không tự đoán).
Mã thoát: 0 = không vấn đề, 1 = có vấn đề (xem dòng "!"), 2 = không có dữ liệu.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import load_bars_parquet  # noqa: E402
from core import contracts  # noqa: E402
from core.calendar import TradingCalendar  # noqa: E402
from core.clock import SessionSchedule  # noqa: E402
from core.settings import Settings, load_settings  # noqa: E402
from recorder.validate import DayReport, validate_day, write_bars  # noqa: E402
from utils.atomic_io import write_json_atomic  # noqa: E402


def run(day: date, settings: Settings, ref_price: Optional[float] = None,
        reference: Optional[Path] = None, gap_seconds: float = 5.0,
        calendar: Optional[TradingCalendar] = None) -> tuple[int, DayReport]:
    data = settings.path("paths.data_dir")
    cal = calendar or TradingCalendar.default()
    # HĐ bot giao dịch ngày đó = HĐ được kiểm chất lượng; HĐ khác (ghi thêm ngày trước roll) chỉ dựng bar
    primary_symbol = contracts.trading_contract(day, cal, int(settings.require("contract.roll_days_before_expiry")))
    sched = SessionSchedule.from_mapping(settings.get("session", {}))
    segments = [(datetime.combine(day, sched.continuous_am_start), datetime.combine(day, sched.lunch_start)),
                (datetime.combine(day, sched.continuous_pm_start), datetime.combine(day, sched.atc_start))]
    ref_path = reference or data / "imported" / f"{day.isoformat()}.parquet"
    ref_bars = load_bars_parquet(ref_path)[0] if Path(ref_path).exists() else None
    primary = settings.get("feeds.primary", "dnse")
    rep = validate_day(data / "raw" / day.isoformat(), segments=segments,
                       tick_size=float(settings.require("contract.tick_size_points")),
                       gap_seconds=gap_seconds,
                       divergence_ticks=float(settings.require("feeds.feed_divergence_ticks")),
                       stale_seconds=float(settings.require("feeds.feed_stale_seconds")),
                       ref_price=ref_price,
                       daily_limit_pct=float(settings.get("contract.daily_limit_pct", 7.0)),
                       reference_bars=ref_bars, bars_source=primary, primary_symbol=primary_symbol)
    if ref_bars is None:
        rep.notes.append(f"Không có bar tham chiếu ({ref_path}) ⇒ bỏ qua so OHLC")
    if not rep.sources:
        return 2, rep
    src = primary if primary in rep.bars else sorted(rep.bars)[0]
    write_bars(rep.bars[src], data / "bars", day, source=src)
    write_json_atomic(rep.to_dict(), data / "validation" / f"{day.isoformat()}.json")
    return (1 if rep.issues else 0), rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Kiểm định dữ liệu tick một ngày")
    ap.add_argument("day", help="YYYY-MM-DD")
    ap.add_argument("--ref-price", type=float, help="giá tham chiếu của sở (kiểm biên độ)")
    ap.add_argument("--reference", type=Path, help="Parquet bar tham chiếu (mặc định data/imported/<ngày>)")
    ap.add_argument("--gap-seconds", type=float, default=5.0)
    args = ap.parse_args(argv)
    code, rep = run(date.fromisoformat(args.day), load_settings(), args.ref_price, args.reference,
                    args.gap_seconds)
    print(rep.summary())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
