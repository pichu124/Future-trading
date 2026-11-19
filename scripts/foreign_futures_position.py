"""Vị thế ròng khối ngoại trên HĐTL VN30, dựng lại từ dòng lệnh (research/regime_shadow.md, Bước 1 mục 5b).

Cách tính giống `vnstock_futures_foreign_view.py` (vnstock-dashboard): vị thế ròng một HĐ = Σ (KL mua − KL bán) của khối
ngoại từ phiên niêm yết. Nguồn: SSI `marketdata_list_daily_stock_infor` theo mã KRX — nguồn KHÔNG có OI, nên đây là ước
tính, đúng khi không thiếu phiên và khối ngoại bắt đầu từ 0 lúc niêm yết. Đối chiếu 19/11/2026: VN30F2612 từ SSI riêng
(147 phiên) = +1.642, khớp seed dashboard + SSI.

    python scripts/foreign_futures_position.py --plan --date 2026-11-20      # mã cần tải (mỗi mã: range 1y, pageSize 200)
    python scripts/foreign_futures_position.py <thư mục thô> --date 2026-11-20
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts                        # noqa: E402
from core.calendar import TradingCalendar         # noqa: E402

PAGE_SIZE = 200


def plan(d0: date, calendar: TradingCalendar) -> list[tuple[str, str, str]]:
    """(mã KRX, range, toDate) cho mọi HĐ đang lưu hành ở D0; toDate = phiên giao dịch liền trước D0."""
    to = calendar.previous_trading_day(d0).strftime("%d/%m/%Y")
    names = sorted(set(contracts.listed_contracts(d0, calendar).values()), key=contracts.contract_ym)
    return [(contracts.krx_symbol(*contracts.contract_ym(n)), "1y", to) for n in names]


def compute(files: Iterable[Path], d0: date, calendar: TradingCalendar, page_size: int = PAGE_SIZE) -> list[dict]:
    per: dict[str, dict] = {}
    all_rows: list[dict] = []
    for path in files:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ValueError(f"{path}: status ≠ ok")
        all_rows += (payload.get("data") or {}).get("stocks") or []
    counts: dict[str, int] = {}                 # tổng dòng thô mỗi mã trên MỌI file (trang 1 + trang 2…)
    for r in all_rows:
        counts[r.get("symbol")] = counts.get(r.get("symbol"), 0) + 1
    for r in all_rows:
        name = contracts.symbol_contract(r.get("symbol"))
        if not name:
            continue
        c = per.setdefault(name, {"contract": name, "days": {}, "missing_days": set(), "truncated": False})
        if counts[r.get("symbol")] % page_size == 0:
            c["truncated"] = True
        if str(r.get("close_raw")) == "0":
            continue
        d = datetime.strptime(str(r["trading_date"]), "%d/%m/%Y").date()
        if d >= d0:
            continue
        if "foreign_buy_volume_total" not in r:
            c["days"][d] = 0
            c["missing_days"].add(d)
            continue
        c["days"][d] = int(float(r["foreign_buy_volume_total"])) - int(float(r.get("foreign_sell_volume_total") or 0))
    out = []
    for name in sorted(per, key=contracts.contract_ym):
        c = per[name]
        days = sorted(c["days"])
        if not days:
            continue
        out.append({"contract": name, "position": sum(c["days"].values()), "sessions": len(days),
                    "missing": len(c["missing_days"]), "first": days[0], "last": days[-1],
                    "recent": [(d, c["days"][d]) for d in days[-5:]],
                    "expires_today": contracts.contract_expiry(name, calendar) == d0,
                    "maybe_truncated": c["truncated"]})
    return out


def _s(x: int) -> str:
    return f"{x:+,}".replace(",", ".")


def render(cs: Sequence[dict], d0: date) -> str:
    lines = [f"VỊ THẾ RÒNG KHỐI NGOẠI HĐTL VN30 (ước tính = Σ mua − bán từ phiên niêm yết; nguồn không có OI) — trước "
             f"phiên {d0:%d/%m/%Y}"]
    for c in cs:
        flags = []
        if c["expires_today"]:
            flags.append("đáo hạn hôm nay — vị thế tự đóng cuối phiên")
        if c["missing"]:
            flags.append(f"{c['missing']} phiên thiếu số khối ngoại")
        if c["maybe_truncated"]:
            flags.append("CẢNH BÁO: số dòng = bội số pageSize, có thể còn trang ⇒ lịch sử chưa đủ, tải thêm pageIndex")
        recent = ", ".join(f"{d:%d/%m} {_s(v)}" for d, v in c["recent"])
        lines.append(f"- {c['contract']}: vị thế {_s(c['position'])} HĐ ({c['sessions']} phiên {c['first']:%d/%m/%Y}→"
                     f"{c['last']:%d/%m/%Y}); ròng 5 phiên gần nhất: {recent}" + (f" [{'; '.join(flags)}]" if flags else ""))
    total = sum(c["position"] for c in cs)
    after = sum(c["position"] for c in cs if not c["expires_today"])
    lines.append(f"TỔNG mọi HĐ: {_s(total)} HĐ" + (f"; không tính HĐ đáo hạn hôm nay: {_s(after)} HĐ"
                                                      if any(c["expires_today"] for c in cs) else ""))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Vị thế ròng khối ngoại HĐTL VN30 (chỉ nghiên cứu)")
    ap.add_argument("raw", nargs="*", help="file hoặc thư mục kết quả thô *.json của SSI daily_stock_infor")
    ap.add_argument("--date", required=True, help="D0, YYYY-MM-DD (chỉ dùng phiên TRƯỚC ngày này)")
    ap.add_argument("--plan", action="store_true", help="chỉ in danh sách mã cần tải")
    args = ap.parse_args(argv)
    d0 = date.fromisoformat(args.date)
    cal = TradingCalendar.default()
    if args.plan:
        for sym, rng, to in plan(d0, cal):
            print(f"{sym} {rng} {to}")
        return 0
    files: list[Path] = []
    for item in args.raw:
        p = Path(item)
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    if not files:
        print("❌ không có file thô nào")
        return 2
    print(render(compute(files, d0, cal), d0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
