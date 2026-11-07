"""Cứu bar 1 phút VN30F từ vnstock-dashboard trước khi dashboard tự xoá (30 ngày).

Nguồn: `<data_warehouse>/_live/archive/bars_YYYY-MM-DD.parquet` và `_live/bars_*.parquet`
(schema: ticker, time, open, high, low, close, volume) — do Live Mode ghi, poll ~2 giây.
Đích:  `data/imported/YYYY-MM-DD.parquet` của repo này (một file mỗi ngày).

Xử lý:
- chỉ giữ dòng HĐTL VN30 (VN30F1M/2M/1Q/2Q, VN30FYYMM, mã KRX 41I1…);
- đổi mọi kiểu mã về tên hợp đồng thật THEO NGÀY của bar (VN30F1M ngày 07/11/2026 = VN30F2611);
- một hợp đồng-phút có nhiều nguồn mã ⇒ ưu tiên mã cố định (KRX/VN30FYYMM) hơn mã cuốn chiếu;
- giá nghìn-lần (vd 1350200) ⇒ chia 1000; giá ngoài khoảng hợp lý bị loại và báo cáo;
- chỉ giữ phút trong giờ giao dịch (ATO → đóng cửa);
- `volume_reliable = False`: dashboard KHÔNG cộng volume cho phái sinh (không có delta tin cậy).

Chạy lại bao nhiêu lần cũng được: mỗi ngày ghi đè trọn file đích (ghi nguyên tử), không sửa nguồn.

Dùng:
    python scripts/import_dashboard_bars.py --warehouse C:/vnstock/data_warehouse
    python scripts/import_dashboard_bars.py            # đọc paths.dashboard_warehouse trong settings
    python scripts/import_dashboard_bars.py --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts  # noqa: E402
from core.calendar import TradingCalendar  # noqa: E402
from core.settings import load_settings  # noqa: E402
from utils.atomic_io import write_dataframe_atomic  # noqa: E402

_FILE_RE = re.compile(r"^bars_(\d{4}-\d{2}-\d{2})\.parquet$")
PRICE_MIN, PRICE_MAX = 300.0, 5000.0          # điểm VN30F hợp lý (bảo thủ, rộng)
SESSION_START, SESSION_END = time(8, 45), time(14, 46)
OUTPUT_COLUMNS = ["symbol", "start", "open", "high", "low", "close", "volume",
                  "volume_reliable", "source", "source_symbol", "price_rescaled"]


@dataclass
class DayReport:
    day: date
    rows_in: int = 0
    rows_out: int = 0
    dropped_price: int = 0
    dropped_time: int = 0
    rescaled: int = 0
    symbols: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        syms = ", ".join(f"{k}:{v}" for k, v in sorted(self.symbols.items())) or "—"
        extra = []
        if self.rescaled:
            extra.append(f"chia 1000: {self.rescaled}")
        if self.dropped_price:
            extra.append(f"loại giá lạ: {self.dropped_price}")
        if self.dropped_time:
            extra.append(f"ngoài giờ: {self.dropped_time}")
        tail = f" ({'; '.join(extra)})" if extra else ""
        return f"{self.day}  {self.rows_out:>4} bar  [{syms}]{tail}"


def find_source_files(warehouse: Path) -> dict[date, list[Path]]:
    """{ngày: [file…]} từ _live/archive và _live (một ngày có thể có ở cả hai nơi)."""
    out: dict[date, list[Path]] = {}
    for folder in (warehouse / "_live" / "archive", warehouse / "_live"):
        if not folder.is_dir():
            continue
        for f in sorted(folder.iterdir()):
            m = _FILE_RE.match(f.name)
            if m:
                out.setdefault(date.fromisoformat(m.group(1)), []).append(f)
    return out


def _fixed_code_first(sym: str) -> int:
    return 0 if contracts.symbol_contract(sym) else 1


def extract_futures_bars(raw: pd.DataFrame, cal: TradingCalendar,
                         report: Optional[DayReport] = None) -> pd.DataFrame:
    """Hàm thuần: DataFrame bar thô của dashboard → bar HĐTL VN30 đã chuẩn hoá."""
    rep = report or DayReport(day=date.min)
    if raw.empty or "ticker" not in raw.columns:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    df = raw[raw["ticker"].map(contracts.is_vn30_future)].copy()
    rep.rows_in += len(df)
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df["start"] = pd.to_datetime(df["time"])
    if df["start"].dt.tz is not None:
        df["start"] = df["start"].dt.tz_convert("Asia/Ho_Chi_Minh").dt.tz_localize(None)
    df["start"] = df["start"].dt.floor("1min")

    in_session = df["start"].dt.time.between(SESSION_START, SESSION_END, inclusive="left")
    rep.dropped_time += int((~in_session).sum())
    df = df[in_session]

    price_cols = ["open", "high", "low", "close"]
    df[price_cols] = df[price_cols].astype(float)
    big = df["close"] > PRICE_MAX * 10
    df.loc[big, price_cols] = df.loc[big, price_cols] / 1000.0
    df["price_rescaled"] = big
    rep.rescaled += int(big.sum())
    ok_price = df[price_cols].apply(lambda s: s.between(PRICE_MIN, PRICE_MAX)).all(axis=1)
    rep.dropped_price += int((~ok_price).sum())
    df = df[ok_price]
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df["symbol"] = [contracts.resolve_symbol(t, ts.date(), cal) for t, ts in zip(df["ticker"], df["start"])]
    df = df[df["symbol"].notna()]
    df["source_symbol"] = df["ticker"].astype(str)
    df["_rank"] = df["source_symbol"].map(_fixed_code_first)
    df = (df.sort_values(["symbol", "start", "_rank"])
            .drop_duplicates(subset=["symbol", "start"], keep="first"))

    # Bar tự mâu thuẫn từ snapshot: nới high/low theo open/close đã có (không bịa số)
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0).clip(lower=0).astype("int64")
    df["volume_reliable"] = False
    df["source"] = "dashboard"
    out = df[OUTPUT_COLUMNS].reset_index(drop=True)
    rep.rows_out += len(out)
    for sym, n in out["symbol"].value_counts().items():
        rep.symbols[sym] = rep.symbols.get(sym, 0) + int(n)
    return out


def import_day(day: date, files: Iterable[Path], out_dir: Path, cal: TradingCalendar,
               dry_run: bool = False) -> DayReport:
    rep = DayReport(day=day)
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception as exc:              # một file hỏng không chặn các ngày khác
            print(f"  ⚠️  không đọc được {f}: {type(exc).__name__}: {exc}")
    raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    bars = extract_futures_bars(raw, cal, rep)
    if not bars.empty and not dry_run:
        write_dataframe_atomic(bars, out_dir / f"{day.isoformat()}.parquet")
    return rep


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", help="thư mục data_warehouse của vnstock-dashboard")
    ap.add_argument("--out", help="thư mục đích (mặc định <data_dir>/imported)")
    ap.add_argument("--dry-run", action="store_true", help="chỉ báo cáo, không ghi file")
    args = ap.parse_args(argv)

    settings = load_settings()
    warehouse = Path(args.warehouse) if args.warehouse else (
        settings.path("paths.dashboard_warehouse") if settings.get("paths.dashboard_warehouse") else None)
    if warehouse is None:
        print("Thiếu --warehouse (hoặc paths.dashboard_warehouse trong config/settings.yaml)")
        return 2
    if not warehouse.is_dir():
        print(f"Không thấy thư mục {warehouse}")
        return 2
    out_dir = Path(args.out) if args.out else settings.path("paths.data_dir") / "imported"
    cal = TradingCalendar.default()

    files = find_source_files(warehouse)
    if not files:
        print(f"Không có file bars_*.parquet trong {warehouse / '_live'} — Live Mode chưa từng chạy?")
        return 1
    print(f"{'(dry-run) ' if args.dry_run else ''}{len(files)} ngày nguồn → {out_dir}")
    total = 0
    for day in sorted(files):
        rep = import_day(day, files[day], out_dir, cal, args.dry_run)
        total += rep.rows_out
        print("  " + rep.line())
    print(f"Xong: {total} bar phái sinh. volume_reliable=False — chỉ dùng thử nhanh, không kết luận edge.")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
