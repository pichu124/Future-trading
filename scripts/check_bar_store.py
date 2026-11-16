"""Đếm ngày giao dịch thiếu trong từng cửa sổ của một kho nến — bước 1 đã đăng ký trước cho #7/#8/#9 (research/log.md).

    python scripts/check_bar_store.py data/ssi/ohlc1m --window IS=2025-07-22:2025-12-31 \\
        --window OOS-A=2026-01-01:2026-03-24 --window OOS-B=2026-03-25:2026-10-30

- Thiếu file của HĐ bot giao dịch ngày đó ⇒ "thiếu". Ngày trước roll thiếu file HĐ kế tiếp ⇒ không đo được chênh roll
  ⇒ khoảng `adr_days` phiên sau đó chiến lược cần lịch sử không giao dịch (chỉ báo, không đổi mã thoát).
- Có cửa sổ thiếu > `--max-missing` (mặc định 10) ngày ⇒ mã thoát 1: dừng tìm lỗi đường ống, không kết luận.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts                        # noqa: E402
from core.calendar import TradingCalendar         # noqa: E402


def check(store: Path | str, windows: Sequence[tuple[str, date, date]], calendar: TradingCalendar,
          roll_days: int = 1) -> list[dict]:
    root = Path(store)
    have = set()
    for p in root.glob("*/*.parquet"):
        try:
            have.add((p.parent.name, date.fromisoformat(p.stem)))
        except ValueError:
            continue
    out = []
    for name, lo, hi in windows:
        d = calendar.next_trading_day(lo, inclusive=True)
        n, missing, eve = 0, [], []
        while d <= hi:
            n += 1
            c = contracts.trading_contract(d, calendar, roll_days)
            if (c, d) not in have:
                missing.append(d)
            nxt = contracts.trading_contract(calendar.next_trading_day(d), calendar, roll_days)
            if nxt != c and (nxt, d) not in have:
                eve.append(d)
            d = calendar.next_trading_day(d)
        out.append({"window": name, "start": lo, "end": hi, "sessions": n, "missing": missing, "missing_roll_eve": eve})
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Đếm ngày thiếu theo cửa sổ trong kho nến")
    ap.add_argument("store", help="thư mục <HĐ>/<ngày>.parquet, vd data/ssi/ohlc1m")
    ap.add_argument("--window", action="append", required=True, help="TÊN=YYYY-MM-DD:YYYY-MM-DD")
    ap.add_argument("--max-missing", type=int, default=10)
    args = ap.parse_args(argv)
    wins = []
    for w in args.window:
        name, _, span = w.partition("=")
        a, _, b = span.partition(":")
        wins.append((name, date.fromisoformat(a), date.fromisoformat(b)))
    from core.settings import load_settings
    roll = int(load_settings().require("contract.roll_days_before_expiry"))
    bad = False
    for r in check(args.store, wins, TradingCalendar.default(), roll):
        flag = len(r["missing"]) > args.max_missing
        bad |= flag
        print(f"{'❌' if flag else '✅'} {r['window']} {r['start']}→{r['end']}: {r['sessions']} phiên, thiếu "
              f"{len(r['missing'])}" + (f" ({', '.join(d.strftime('%d/%m') for d in r['missing'][:15])})"
                                        if r["missing"] else "")
              + f"; thiếu HĐ kế tiếp ngày trước roll {len(r['missing_roll_eve'])}"
              + (f" ({', '.join(d.strftime('%d/%m') for d in r['missing_roll_eve'])})" if r["missing_roll_eve"] else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
