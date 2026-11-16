"""Kiểm định GỘP nhiều giai đoạn out-of-sample — luật đăng ký trước 11/11/2026 (research/log.md, "giai đoạn kiểm tra
THỨ HAI"): dữ liệu trước 11/05/2026 (DNSE lưu ~7 tháng) + 01/09/2026 → nay, tham số chiến lược KHÔNG đổi.

Cho từng chiến lược: lệnh OOS = ngày VÀO thuộc một cửa sổ (đóng hai đầu; cửa sổ mở cuối = tới hết dữ liệu).
CÓ EDGE ⇔ (a) ≥ 30 lệnh gộp; (b) P&L sau phí > 0 ở MỌI cửa sổ (0 lệnh ⇒ trượt); (c) p sign-flip chuỗi gộp × n_trials
< 0,1; (d) P&L gộp với trượt `slippage_stress_ticks` tick > 0. Thiếu (a) ⇒ CHƯA ĐỦ; trượt (b)/(c)/(d) ⇒ KHÔNG CÓ EDGE.

    python -m backtest.pooled --ticks data/history/trades --strategy strategy.morning_momentum:MorningMomentum \\
        --window 2026-03-16:2026-05-10 --window 2026-09-01:
"""
from __future__ import annotations

import argparse
import importlib
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.validation import DEFAULT, Thresholds, monte_carlo_pvalue  # noqa: E402

CO_EDGE = "CÓ BẰNG CHỨNG EDGE"
KHONG_EDGE = "KHÔNG CÓ EDGE"
CHUA_DU = "CHƯA ĐỦ BẰNG CHỨNG"

Window = tuple[date, Optional[date]]


def parse_window(text: str) -> Window:
    a, _, b = text.partition(":")
    lo = date.fromisoformat(a)
    hi = date.fromisoformat(b) if b.strip() else None
    if hi is not None and hi < lo:
        raise ValueError(f"cửa sổ {text}: ngày cuối trước ngày đầu")
    return lo, hi


def _in(d: date, w: Window) -> bool:
    return d >= w[0] and (w[1] is None or d <= w[1])


def _label(w: Window) -> str:
    return f"{w[0]}→{w[1] or 'nay'}"


def pooled_verdict(trades, slip_trades, windows: Sequence[Window], n_trials: int,
                   th: Thresholds = DEFAULT) -> dict:
    """`trades` / `slip_trades`: TradeRecord của cùng chiến lược chạy với trượt thường / trượt căng."""
    pick = lambda ts: [t for t in ts if any(_in(t.entry_ts.date(), w) for w in windows)]   # noqa: E731
    pooled, slip = pick(trades), pick(slip_trades)
    wins = []
    for w in windows:
        sel = [t for t in trades if _in(t.entry_ts.date(), w)]
        wins.append({"window": _label(w), "n_trades": len(sel), "pnl_vnd": float(sum(t.pnl_vnd for t in sel))})
    pnls = [t.pnl_vnd for t in sorted(pooled, key=lambda t: t.entry_ts)]
    p = monte_carlo_pvalue(pnls, th.mc_sims)
    p_adj = None if p is None else min(1.0, p * max(1, n_trials))
    slip_pnl = float(sum(t.pnl_vnd for t in slip))
    out = {"n_trades": len(pooled), "pnl_vnd": float(sum(pnls)), "windows": wins, "p": p, "p_adj": p_adj,
           "n_trials": n_trials, "slip_pnl_vnd": slip_pnl, "reasons": []}
    reasons = out["reasons"]
    if len(pooled) < th.min_trades:
        reasons.append(f"Chỉ {len(pooled)} lệnh OOS gộp < {th.min_trades} — chưa đủ mẫu.")
        out["verdict"] = CHUA_DU
        return out
    for w in wins:
        if w["pnl_vnd"] <= 0:
            reasons.append(f"cửa sổ {w['window']}: {w['n_trades']} lệnh, P&L sau phí {w['pnl_vnd']:,.0f} VNĐ ≤ 0.")
    if p_adj is None or p_adj >= th.p_value_max:
        reasons.append(f"p sign-flip gộp {p if p is None else round(p, 3)} × {n_trials} cấu hình = "
                       f"{p_adj if p_adj is None else round(p_adj, 3)} ≥ {th.p_value_max} — không hơn ăn may.")
    if slip_pnl <= 0:
        reasons.append(f"Trượt giá căng: P&L OOS gộp {slip_pnl:,.0f} VNĐ ≤ 0.")
    out["verdict"] = KHONG_EDGE if reasons else CO_EDGE
    if not reasons:
        reasons.append("Đạt cả 4 điều kiện đăng ký trước — vẫn phải paper ổn định trước khi tính đến live.")
    return out


def render(r: dict, title: str) -> str:
    lines = [f"{title} — kiểm định OOS gộp (n_trials = {r['n_trials']})"]
    for w in r["windows"]:
        lines.append(f"  cửa sổ {w['window']}: {w['n_trades']} lệnh, P&L sau phí {w['pnl_vnd']:,.0f} VNĐ")
    p = "—" if r["p"] is None else f"{r['p']:.3f}"
    pa = "—" if r["p_adj"] is None else f"{r['p_adj']:.3f}"
    lines.append(f"  gộp: {r['n_trades']} lệnh, P&L {r['pnl_vnd']:,.0f} VNĐ · p {p} (×{r['n_trials']} = {pa}) · "
                 f"trượt căng {r['slip_pnl_vnd']:,.0f} VNĐ")
    lines.append(f"Kết luận: {r['verdict']}")
    lines += [f"  - {x}" for x in r["reasons"]]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from backtest.engine import BacktestEngine, bars_from_ticks
    from backtest.robustness import count_logged_trials, with_overrides
    from backtest.ticks import TickStore
    from core.settings import MissingSetting, load_settings

    ap = argparse.ArgumentParser(description="Kiểm định OOS gộp nhiều cửa sổ (luật đăng ký trước)")
    ap.add_argument("--ticks", nargs="+", required=True, help="vd data/history/trades (đọc dần từng ngày)")
    ap.add_argument("--source", help="chỉ dùng nguồn này khi dữ liệu có nhiều nguồn")
    ap.add_argument("--strategy", required=True, help="module:Class")
    ap.add_argument("--window", action="append", required=True, help="YYYY-MM-DD:YYYY-MM-DD hoặc YYYY-MM-DD: (tới nay)")
    ap.add_argument("--n-trials", type=int, default=None, help="mặc định = số dòng cấu hình trong research/log.md")
    ap.add_argument("--slippage-stress-ticks", type=int, default=2)
    args = ap.parse_args(argv)
    try:
        windows = [parse_window(w) for w in args.window]
        mod, _, cls = args.strategy.partition(":")
        strat_cls = getattr(importlib.import_module(mod), cls)
    except (ValueError, ImportError, AttributeError) as exc:
        print(f"❌ {exc}")
        return 2
    settings = load_settings()
    try:
        store = TickStore(args.ticks, source=args.source,
                          tick_size=float(settings.require("contract.tick_size_points")),
                          spread_ticks=int(settings.require("backtest.synthetic_spread_ticks")))
    except (ValueError, MissingSetting) as exc:
        print(f"❌ {exc}")
        return 2

    def run(s):
        return BacktestEngine(s, strat_cls.from_settings(s)).run_events(bars_from_ticks(store.iter())).trades

    base = run(settings)
    slip = run(with_overrides(settings, {"backtest.slippage_ticks": args.slippage_stress_ticks}))
    n_trials = args.n_trials or count_logged_trials()
    title = getattr(strat_cls, "TITLE", cls)
    print(render(pooled_verdict(base, slip, windows, n_trials), title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
