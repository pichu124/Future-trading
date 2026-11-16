"""Kiểm tra kho lịch sử đã tải (`data/history`) trước khi backtest.

Đối chiếu với kế hoạch của downloader (`plan_days`: mọi ngày giao dịch theo lịch + HĐ kế tiếp ngày trước roll):
- mỗi mục (ngày, HĐ): ok (có file nến `ohlc1m/…`) / rỗng / lỗi / thiếu;
- ngày thiếu lệnh (`volume_gap` ≠ 0) hoặc có phút volume không tin được — chỉ BÁO (thiếu volume ≠ thiếu ngày);
- ngày đáng ngờ: quá ít nến hoặc phiên không phủ (nến đầu sau 09:05 / nến cuối trước 14:25) — chỉ kiểm HĐ giao dịch,
  HĐ kế tiếp ngày trước roll thanh khoản mỏng nên không kiểm độ phủ.

    python scripts/check_history.py --start 2026-05-11 --end 2026-11-06

Mã thoát: 0 = mọi mục ok và không ngày đáng ngờ; 1 = có vấn đề (xem báo cáo).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, time
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts  # noqa: E402
from core.calendar import TradingCalendar  # noqa: E402

FIRST_BAR_BY, LAST_BAR_FROM = time(9, 5), time(14, 25)


def check(out_dir: Path | str, start: date, end: date, calendar: TradingCalendar, roll_days_before_expiry: int = 1,
          min_bars: int = 150) -> dict:
    import pandas as pd

    from scripts.download_history import plan_days
    out = Path(out_dir)
    man_path = out / "_manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists() else {}
    plan = plan_days(start, end, calendar, roll_days_before_expiry)
    rep = {"expected": len(plan), "ok": 0, "missing": [], "errors": [], "empty": [], "gap_days": [],
           "suspicious": [], "bars_total": 0, "trading_days": len({d for d, _, _ in plan})}
    for d, name, _krx in plan:
        label = f"{d} {name}"
        bars_path = out / "ohlc1m" / name / f"{d.isoformat()}.parquet"
        trades_entry = manifest.get(f"trades/{name}/{d.isoformat()}") or {}
        if not bars_path.exists():
            if "error" in trades_entry:
                rep["errors"].append(label)
            elif trades_entry.get("rows") == 0:
                rep["empty"].append(label)
            else:
                rep["missing"].append(label)
            continue
        rep["ok"] += 1
        bars_entry = manifest.get(f"ohlc1m/{name}/{d.isoformat()}") or {}
        gap, bad = bars_entry.get("volume_gap") or 0, bars_entry.get("unreliable_minutes") or []
        if gap or bad:
            rep["gap_days"].append(f"{label}: thiếu {gap} HĐ" + (f", phút {', '.join(bad)}" if bad else ""))
        df = pd.read_parquet(bars_path, columns=["start"])
        rep["bars_total"] += len(df)
        if name != contracts.trading_contract(d, calendar, roll_days_before_expiry):
            continue                                       # HĐ kế tiếp ngày trước roll: chỉ cần có để đo chênh lệch
        problems = []
        if len(df) < min_bars:
            problems.append(f"chỉ {len(df)} nến")
        if len(df):
            first, last = pd.Timestamp(df["start"].min()).time(), pd.Timestamp(df["start"].max()).time()
            if first > FIRST_BAR_BY:
                problems.append(f"nến đầu {first:%H:%M}")
            if last < LAST_BAR_FROM:
                problems.append(f"nến cuối {last:%H:%M}")
        if problems:
            rep["suspicious"].append(f"{label}: {', '.join(problems)}")
    rep["passed"] = (rep["ok"] == rep["expected"]) and not rep["suspicious"]
    return rep


def render(rep: dict) -> str:
    lines = [f"Kế hoạch: {rep['expected']} mục ({rep['trading_days']} ngày giao dịch) · có dữ liệu: {rep['ok']} · "
             f"tổng {rep['bars_total']} nến"]
    for key, title in (("missing", "Thiếu (chưa tải)"), ("errors", "Lỗi lần tải trước"), ("empty", "Rỗng"),
                       ("suspicious", "Đáng ngờ"), ("gap_days", "Thiếu lệnh / phút volume không tin được")):
        items = rep[key]
        if items:
            lines.append(f"- {title}: {len(items)}")
            lines += [f"    {x}" for x in items[:30]] + ([f"    … và {len(items) - 30} mục nữa"] if len(items) > 30 else [])
    lines.append("✅ Đủ dữ liệu cho backtest" if rep["passed"] else
                 "⚠️  Chưa đủ — chạy lại download_history.py cho các mục thiếu/lỗi, xem các ngày đáng ngờ")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kiểm tra kho lịch sử đã tải")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--dir", help="thư mục lịch sử (mặc định <data_dir>/history)")
    ap.add_argument("--min-bars", type=int, default=150)
    args = ap.parse_args(argv)
    from core.settings import load_settings
    s = load_settings()
    roll = int(s.require("contract.roll_days_before_expiry"))
    out = Path(args.dir) if args.dir else s.path("paths.data_dir") / "history"
    rep = check(out, date.fromisoformat(args.start), date.fromisoformat(args.end), TradingCalendar.default(), roll,
                args.min_bars)
    print(render(rep))
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
