"""Chuyển kết quả thô của SSI `marketdata_list_intraday_stock_infor` (kết nối SSI của Claude, chỉ nghiên cứu) thành kho
nến 1 phút Parquet, cùng schema kho DNSE.

    python scripts/import_ssi_intraday.py data/ssi/raw --out data/ssi

- HĐ tương lai (41I1… / VN30FYYMM) ⇒ `<out>/ohlc1m/<VN30FYYMM>/<ngày>.parquet`; mã khác (chỉ số VN30…) ⇒
  `<out>/index1m/<MÃ>/<ngày>.parquet`. `volume_reliable=False` (nguồn SSI, chưa đối chiếu volume từng phút).
- `trading_date` chế độ range là DD/MM/YYYY HH:MM:SS (mô tả tool ghi MM/dd — sai với chế độ này). Ngày rơi vào cuối
  tuần ⇒ dừng: gần như chắc chắn đọc nhầm định dạng.
- Ghi lại toàn bộ từ các file thô mỗi lần chạy (idempotent), tmp-rồi-tráo.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts                        # noqa: E402
from core.models import Bar                       # noqa: E402

COLUMNS = ["symbol", "start", "open", "high", "low", "close", "volume", "interval_s", "volume_reliable", "source"]


class ImportError_(ValueError):
    """Dữ liệu thô không dùng được — dừng, không ghi gì sai."""


def _ticks(path: Path) -> list[dict]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ImportError_(f"{path}: không đọc được JSON ({e})") from e
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ImportError_(f"{path}: status {payload.get('status') if isinstance(payload, dict) else '?'} ≠ ok")
    ticks = (payload.get("data") or {}).get("ticks")
    if not isinstance(ticks, list):
        raise ImportError_(f"{path}: thiếu data.ticks")
    return ticks


def _parse_ts(text: str, path: Path) -> datetime:
    try:
        ts = datetime.strptime(str(text).strip(), "%d/%m/%Y %H:%M:%S")
    except ValueError as e:
        raise ImportError_(f"{path}: trading_date {text!r} không đúng DD/MM/YYYY HH:MM:SS") from e
    if ts.weekday() >= 5:
        raise ImportError_(f"{path}: {text!r} rơi vào cuối tuần — có thể là định dạng MM/DD, dừng")
    return ts


def import_files(files: Iterable[Path], out: Path | str) -> dict:
    import pandas as pd

    from utils.atomic_io import write_dataframe_atomic

    out = Path(out)
    best: dict[tuple[str, str, datetime], tuple] = {}
    stats = {"bars": 0, "skipped": 0, "duplicates": 0, "conflicts": 0, "files_written": 0, "raw_files": 0}
    for path in files:
        stats["raw_files"] += 1
        for t in _ticks(Path(path)):
            ts = _parse_ts(t.get("trading_date"), Path(path))
            raw_sym = str(t.get("symbol") or "").strip().upper()
            name = contracts.symbol_contract(raw_sym)
            kind, sym = ("ohlc1m", name) if name else ("index1m", raw_sym)
            try:
                o, h, l, c = (float(t[k]) for k in ("open", "high", "low", "close"))
                v = int(float(t.get("volume") or 0))
                Bar(sym, ts, o, h, l, c, v)
            except (KeyError, TypeError, ValueError):
                stats["skipped"] += 1
                continue
            key = (kind, sym, ts)
            row = (o, h, l, c, v)
            if key in best:
                stats["duplicates"] += 1
                if best[key][:4] != row[:4]:
                    stats["conflicts"] += 1
                continue
            best[key] = row
    groups: dict[tuple[str, str, date], list] = defaultdict(list)
    for (kind, sym, ts), (o, h, l, c, v) in best.items():
        groups[(kind, sym, ts.date())].append((sym, ts, o, h, l, c, v, 60, False, "ssi"))
    for (kind, sym, d), rows in sorted(groups.items()):
        df = pd.DataFrame(sorted(rows, key=lambda r: r[1]), columns=COLUMNS)
        dest = out / kind / sym / f"{d.isoformat()}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_dataframe_atomic(df, dest)
        stats["files_written"] += 1
    stats["bars"] = len(best)
    return stats


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kết quả thô SSI intraday → kho nến 1 phút Parquet")
    ap.add_argument("raw", nargs="+", help="file hoặc thư mục chứa file thô *.json")
    ap.add_argument("--out", required=True, help="thư mục kho, vd data/ssi")
    args = ap.parse_args(argv)
    files: list[Path] = []
    for item in args.raw:
        p = Path(item)
        files += sorted(p.glob("*.json")) if p.is_dir() else ([p] if p.exists() else [])
    if not files:
        print("❌ không có file thô nào")
        return 2
    try:
        st = import_files(files, args.out)
    except ImportError_ as e:
        print(f"❌ {e}")
        return 2
    print(f"{st['raw_files']} file thô → {st['bars']} nến, {st['files_written']} file (mã, ngày); "
          f"bỏ {st['skipped']} nến hỏng; trùng {st['duplicates']} (khác giá {st['conflicts']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
