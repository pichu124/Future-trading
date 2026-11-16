"""Cổng IS đăng ký trước cho #7/#9 (research/log.md, "Đăng ký trước #7, #8, #9"): in các con số IS và áp luật.

    python scripts/is_gate.py --bars data/ssi/ohlc1m --strategy strategy.prev_day_reversal:PrevDayReversal

Chạy `backtest.robustness` với ĐÚNG cấu hình đăng ký (nạp nến tới hết 31/12/2025, `--split 2025-12-31` như #6), rồi:
KHÔNG CÓ EDGE — DỪNG (không mở OOS) ⇔ P&L IS sau phí ≤ 0 HOẶC < 75% biến thể độ nhạy ±20% lãi HOẶC < 50% đoạn
walk-forward lãi. Ngược lại ⇒ QUA CỔNG IS (được chạy OOS gộp). Lệnh vào đúng 31/12/2025 công cụ tính là "OOS": in riêng
để minh bạch, không đổi kết luận (như #6).
"""
from __future__ import annotations

import argparse
import importlib
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = "QUA CỔNG IS — được chạy OOS gộp"
STOP = "KHÔNG CÓ EDGE — DỪNG, KHÔNG mở OOS"
SENS_MIN, FOLDS_MIN = 0.75, 0.50
UNTIL, SPLIT = date(2025, 12, 31), date(2025, 12, 31)


def _frac(items) -> Optional[float]:
    items = list(items)
    return sum(1 for x in items if x["metrics"]["total_pnl_vnd"] > 0) / len(items) if items else None


def gate(rob: dict) -> tuple[str, list[str]]:
    is_pnl = rob["sections"]["in_sample"]["metrics"]["total_pnl_vnd"]
    sens, folds = _frac(rob["sensitivity"]), _frac(rob["folds"])
    fails = []
    if is_pnl <= 0:
        fails.append(f"P&L IS sau phí {is_pnl:,.0f} VNĐ ≤ 0")
    if sens is None or sens < SENS_MIN:
        fails.append(f"độ nhạy ±20%: {'—' if sens is None else f'{sens:.0%}'} biến thể lãi < {SENS_MIN:.0%}")
    if folds is None or folds < FOLDS_MIN:
        fails.append(f"walk-forward: {'—' if folds is None else f'{folds:.0%}'} đoạn lãi < {FOLDS_MIN:.0%}")
    if fails:
        return STOP, fails
    return PASS, [f"P&L IS {is_pnl:,.0f} VNĐ > 0; độ nhạy {sens:.0%}; walk-forward {folds:.0%}"]


def render(rob: dict, title: str) -> str:
    m = rob["sections"]["in_sample"]["metrics"]
    oos = rob["sections"]["out_of_sample"]["metrics"]
    slip = rob["slippage_sections"]["in_sample"]["metrics"]
    d0, d1, nd = rob["days"]
    pf = "—" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}"
    wr = "—" if m["win_rate"] is None else f"{m['win_rate']:.0%}"
    lines = [f"{title} — cổng IS (dữ liệu {d0} → {d1}, {nd} ngày)",
             f"IS: {m['n_trades']} lệnh, P&L sau phí {m['total_pnl_vnd']:,.0f} VNĐ, thắng {wr}, PF {pf}; "
             f"trượt 2 tick {slip['total_pnl_vnd']:,.0f} VNĐ",
             f"(lệnh vào đúng {SPLIT}: {oos['n_trades']} lệnh, {oos['total_pnl_vnd']:,.0f} VNĐ — công cụ tính là OOS)",
             "Độ nhạy ±20% (chạy trên IS):"]
    for v in rob["sensitivity"]:
        vm = v["metrics"]
        lines.append(f"  {v['param'].split('.')[-1]} {v['base_value']} → {v['value']}: {vm['n_trades']} lệnh, "
                     f"{vm['total_pnl_vnd']:,.0f} VNĐ")
    lines.append("Walk-forward:")
    for i, f in enumerate(rob["folds"], 1):
        fm = f["metrics"]
        lines.append(f"  đoạn {i} {f['start']} → {f['end']}: {fm['n_trades']} lệnh, {fm['total_pnl_vnd']:,.0f} VNĐ")
    trades = [t for t in rob["base"].trades if t.entry_ts.date() < SPLIT]
    by_month, by_side, by_exit = defaultdict(lambda: [0, 0.0]), defaultdict(lambda: [0, 0.0]), defaultdict(int)
    for t in trades:
        for bucket, key in ((by_month, t.entry_ts.strftime("%Y-%m")), (by_side, t.side)):
            bucket[key][0] += 1
            bucket[key][1] += t.pnl_vnd
        by_exit[t.exit_reason] += 1
    lines.append("Theo tháng: " + "; ".join(f"{k} {n} lệnh {p:,.0f}" for k, (n, p) in sorted(by_month.items())))
    lines.append("Theo chiều: " + "; ".join(f"{k} {n} lệnh {p:,.0f}" for k, (n, p) in sorted(by_side.items())))
    lines.append("Cách thoát: " + "; ".join(f"{k} {n}" for k, n in sorted(by_exit.items())))
    verdict, reasons = gate(rob)
    lines.append(f"KẾT LUẬN CỔNG IS: {verdict}")
    lines += [f"  - {r}" for r in reasons]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from backtest.engine import load_bars_parquet
    from backtest.robustness import count_logged_trials, cut_until, run_robustness
    from core.settings import load_settings

    ap = argparse.ArgumentParser(description="Cổng IS đăng ký trước (#7/#9)")
    ap.add_argument("--bars", nargs="+", required=True)
    ap.add_argument("--strategy", required=True, help="module:Class")
    args = ap.parse_args(argv)
    mod, _, cls = args.strategy.partition(":")
    strat = getattr(importlib.import_module(mod), cls)
    bars, skipped = load_bars_parquet(args.bars)
    bars = cut_until(bars, UNTIL)
    rob = run_robustness(load_settings(), strat.from_settings, bars, SPLIT, tuple(strat.ROBUSTNESS_PARAMS),
                         folds=4, n_trials=count_logged_trials())
    print(render(rob, getattr(strat, "TITLE", cls)))
    if skipped:
        print(f"(bỏ {skipped} dòng nến hỏng)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
