"""Đối chiếu hai kho nến 1 phút (vd SSI `data/ssi` với DNSE `data/history`) trên các (hợp đồng, ngày) trùng nhau.

Mỗi kho có dạng `<thư mục>/ohlc1m/<HĐ>/<YYYY-MM-DD>.parquet` (hoặc trỏ thẳng vào `ohlc1m`). Chỉ so phút có ở CẢ hai.
Báo: số nến, phút chung, lệch giá đóng (điểm), tỉ lệ phút OHLC khớp hệt, tỉ lệ volume khớp hệt, tổng volume.

    python scripts/compare_bars.py data/ssi data/history --start 2026-03-25
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _index(root: Path) -> dict[tuple[str, date], Path]:
    base = root / "ohlc1m" if (root / "ohlc1m").is_dir() else root
    out = {}
    for p in base.glob("*/*.parquet"):
        try:
            out[(p.parent.name, date.fromisoformat(p.stem))] = p
        except ValueError:
            continue
    return out


def compare(a: Path | str, b: Path | str, start: Optional[date] = None, end: Optional[date] = None) -> dict:
    import pandas as pd
    ia, ib = _index(Path(a)), _index(Path(b))
    keep = lambda k: (start is None or k[1] >= start) and (end is None or k[1] <= end)   # noqa: E731
    ka, kb = {k for k in ia if keep(k)}, {k for k in ib if keep(k)}
    days = []
    cols = ["start", "open", "high", "low", "close", "volume"]
    for k in sorted(ka & kb, key=lambda x: (x[1], x[0])):
        da = pd.read_parquet(ia[k], columns=cols).drop_duplicates("start").set_index("start")
        db = pd.read_parquet(ib[k], columns=cols).drop_duplicates("start").set_index("start")
        j = da.merge(db, how="inner", left_index=True, right_index=True, suffixes=("_a", "_b"))
        n = len(j)
        diff = (j["close_a"] - j["close_b"]).abs() if n else pd.Series(dtype=float)
        ohlc = ((j["open_a"] == j["open_b"]) & (j["high_a"] == j["high_b"]) & (j["low_a"] == j["low_b"])
                & (j["close_a"] == j["close_b"])) if n else pd.Series(dtype=bool)
        days.append({"symbol": k[0], "date": k[1], "n_a": len(da), "n_b": len(db), "common": n,
                     "close_mae": float(diff.mean()) if n else None, "close_max": float(diff.max()) if n else None,
                     "ohlc_match": float(ohlc.mean()) if n else None,
                     "volume_match": float((j["volume_a"] == j["volume_b"]).mean()) if n else None,
                     "volume_a": int(j["volume_a"].sum()) if n else 0, "volume_b": int(j["volume_b"].sum()) if n else 0})
    common = sum(d["common"] for d in days)
    wavg = lambda key: (sum((d[key] or 0) * d["common"] for d in days) / common) if common else None   # noqa: E731
    summary = {"n_days": len(days), "only_a": len(ka - kb), "only_b": len(kb - ka), "common_minutes": common,
               "ohlc_match": wavg("ohlc_match"), "volume_match": wavg("volume_match"),
               "close_mae": wavg("close_mae"),
               "close_max": max((d["close_max"] or 0) for d in days) if days else None,
               "volume_a": sum(d["volume_a"] for d in days), "volume_b": sum(d["volume_b"] for d in days)}
    return {"days": days, "summary": summary}


def render(rep: dict, worst: int = 10) -> str:
    s = rep["summary"]
    pct = lambda x: "—" if x is None else f"{x:.1%}"   # noqa: E731
    lines = [f"So {s['n_days']} ngày trùng ({s['common_minutes']} phút chung) · chỉ ở kho 1: {s['only_a']} · "
             f"chỉ ở kho 2: {s['only_b']}",
             f"- khớp OHLC hệt: {pct(s['ohlc_match'])} · lệch giá đóng TB {s['close_mae'] or 0:.3f} điểm, lớn nhất "
             f"{s['close_max'] or 0:.1f} điểm",
             f"- volume khớp hệt: {pct(s['volume_match'])} · tổng volume kho 1 {s['volume_a']:,} / kho 2 {s['volume_b']:,}"]
    bad = sorted((d for d in rep["days"] if d["common"]), key=lambda d: (d["ohlc_match"], -(d["close_max"] or 0)))
    if bad:
        lines.append(f"- {min(worst, len(bad))} ngày lệch nhiều nhất:")
        for d in bad[:worst]:
            lines.append(f"    {d['date']} {d['symbol']}: nến {d['n_a']}/{d['n_b']}, chung {d['common']}, "
                         f"khớp OHLC {pct(d['ohlc_match'])}, lệch đóng max {d['close_max']:.1f}, "
                         f"volume {d['volume_a']:,}/{d['volume_b']:,}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Đối chiếu hai kho nến 1 phút")
    ap.add_argument("store_a", help="vd data/ssi")
    ap.add_argument("store_b", help="vd data/history")
    ap.add_argument("--start", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD")
    args = ap.parse_args(argv)
    rep = compare(args.store_a, args.store_b, date.fromisoformat(args.start) if args.start else None,
                  date.fromisoformat(args.end) if args.end else None)
    print(render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
