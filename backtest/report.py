"""Sổ lệnh khứ hồi + số liệu + báo cáo HTML cho backtest (CLAUDE.md mục 6).

Báo cáo tối thiểu: số lệnh, win rate, avg win/loss, profit factor, max drawdown, P&L SAU PHÍ,
P&L theo giờ trong ngày và theo ngày trong tuần, `validate_trades()` (p-value sign-flip, CI
Sharpe bootstrap, walk-forward) đã qua `apply_selection_penalty(n_trials)` — cho toàn bộ,
in-sample và out-of-sample.

Một "lệnh" (TradeRecord) = một lượt từ lúc vị thế rời 0 tới lúc về 0. P&L tính bằng VNĐ sau phí
(`pnl_vnd`) và quy ra điểm (`pnl_points`); `gross_points` là trước phí.
"""
from __future__ import annotations

import html
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from backtest.validation import DEFAULT, Thresholds, apply_selection_penalty, validate_trades
from core.models import Fill, Position, Side
from utils.atomic_io import write_text_atomic

WEEKDAYS = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "CN"]


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    side: str                       # "LONG" | "SHORT"
    qty: int                        # tổng khối lượng vào lệnh
    entry_ts: datetime
    entry_price: float              # giá vào bình quân
    exit_ts: Optional[datetime]     # None với lệnh còn mở
    exit_price: float               # giá ra bình quân (lệnh mở: giá đánh dấu)
    exit_reason: str                # tag lệnh đóng: stop | take_profit | exit | flatten | open
    gross_points: float             # điểm × số HĐ, trước phí
    fees_vnd: float
    pnl_vnd: float                  # SAU phí
    pnl_points: float               # SAU phí, quy ra điểm


@dataclass
class _Open:
    side: Side
    entry_ts: datetime
    pos: Position
    entry_qty: int = 0
    entry_value: float = 0.0
    exit_qty: int = 0
    exit_value: float = 0.0
    fees: float = 0.0


class TradeBook:
    """Ghép fill thành lệnh khứ hồi (theo net của từng hợp đồng)."""

    def __init__(self, multiplier_vnd_per_point: float, tag_of: Callable[[str], str] = lambda _: ""):
        self.multiplier = multiplier_vnd_per_point
        self.tag_of = tag_of
        self.closed: list[TradeRecord] = []
        self._open: dict[str, _Open] = {}

    def on_fill(self, f: Fill) -> None:
        o = self._open.get(f.symbol)
        if o is None:
            self._start(f)
            return
        if (o.pos.qty > 0) == (f.side is Side.BUY):          # cộng thêm cùng chiều
            o.entry_qty += f.qty
            o.entry_value += f.qty * f.price
            o.fees += f.fee_vnd
            o.pos = o.pos.apply(f)
            return
        closing = min(abs(o.pos.qty), f.qty)
        fee_close = f.fee_vnd * closing / f.qty
        o.exit_qty += closing
        o.exit_value += closing * f.price
        o.fees += fee_close
        o.pos = o.pos.apply(replace(f, qty=closing, fee_vnd=fee_close))
        if o.pos.qty == 0:
            self.closed.append(self._record(o, f.ts, o.exit_value / o.exit_qty,
                                            self.tag_of(f.client_order_id) or "exit",
                                            o.pos.realized_points))
            del self._open[f.symbol]
        if f.qty > closing:                                   # đảo chiều trong một fill
            self._start(replace(f, qty=f.qty - closing, fee_vnd=f.fee_vnd - fee_close))

    def _start(self, f: Fill) -> None:
        self._open[f.symbol] = _Open(side=f.side, entry_ts=f.ts, pos=Position(f.symbol).apply(f),
                                     entry_qty=f.qty, entry_value=f.qty * f.price, fees=f.fee_vnd)

    def _record(self, o: _Open, exit_ts: Optional[datetime], exit_price: float, reason: str,
                gross_points: float) -> TradeRecord:
        pnl_vnd = gross_points * self.multiplier - o.fees
        return TradeRecord(symbol=o.pos.symbol, side="LONG" if o.side is Side.BUY else "SHORT",
                           qty=o.entry_qty, entry_ts=o.entry_ts,
                           entry_price=o.entry_value / o.entry_qty, exit_ts=exit_ts,
                           exit_price=exit_price, exit_reason=reason, gross_points=gross_points,
                           fees_vnd=o.fees, pnl_vnd=pnl_vnd, pnl_points=pnl_vnd / self.multiplier)

    def open_trades(self, marks: dict[str, float], ts: Optional[datetime]) -> list[TradeRecord]:
        """Lệnh chưa đóng, đánh dấu theo giá cuối (chưa trừ phí đóng)."""
        out = []
        for sym, o in self._open.items():
            mark = marks.get(sym, o.pos.avg_price)
            gross = o.pos.realized_points + o.pos.unrealized_points(mark)
            out.append(self._record(o, None, mark, "open", gross))
        return out


# ------------------------------------------------------------------ số liệu ----

def _mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def compute_metrics(trades: Sequence[TradeRecord]) -> dict:
    pnls = [t.pnl_vnd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    ordered = sorted(trades, key=lambda t: (t.exit_ts or t.entry_ts, t.entry_ts))
    equity = peak = max_dd = 0.0
    for t in ordered:
        equity += t.pnl_vnd
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    by_hour: dict[int, dict] = {}
    by_weekday: dict[str, dict] = {}
    for t in sorted(trades, key=lambda t: t.entry_ts):
        for bucket, key in ((by_hour, t.entry_ts.hour), (by_weekday, WEEKDAYS[t.entry_ts.weekday()])):
            b = bucket.setdefault(key, {"n": 0, "pnl_vnd": 0.0})
            b["n"] += 1
            b["pnl_vnd"] += t.pnl_vnd
    return {
        "n_trades": len(trades),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else None,
        "avg_win_vnd": _mean(wins),
        "avg_loss_vnd": _mean(losses),
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "total_pnl_vnd": sum(pnls),
        "total_pnl_points": sum(t.pnl_points for t in trades),
        "gross_points": sum(t.gross_points for t in trades),
        "fees_vnd": sum(t.fees_vnd for t in trades),
        "max_drawdown_vnd": max_dd,
        "by_hour": dict(sorted(by_hour.items())),
        "by_weekday": {k: by_weekday[k] for k in WEEKDAYS if k in by_weekday},
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
    }


def _section(trades: list[TradeRecord], n_trials: int, th: Thresholds) -> dict:
    v = validate_trades([{"pnl": t.pnl_vnd, "entry_ts": t.entry_ts} for t in trades], th)
    return {"trades": trades, "metrics": compute_metrics(trades),
            "validation": apply_selection_penalty(v, n_trials, th)}


def build_report(result, split_date: Optional[date] = None, n_trials: int = 1, title: str = "",
                 thresholds: Thresholds = DEFAULT) -> dict:
    """Báo cáo dạng dict. `split_date` = ngày đầu out-of-sample; `n_trials` = số cấu hình đã thử."""
    trades = sorted(result.trades, key=lambda t: t.entry_ts)
    sections = {"all": _section(trades, n_trials, thresholds)}
    if split_date is not None:
        sections["in_sample"] = _section([t for t in trades if t.entry_ts.date() < split_date],
                                         n_trials, thresholds)
        sections["out_of_sample"] = _section([t for t in trades if t.entry_ts.date() >= split_date],
                                             n_trials, thresholds)
    q = result.data_quality
    warnings = list(result.warnings)
    if q.get("unreliable_volume_bars"):
        warnings.insert(0, f"{q['unreliable_volume_bars']} bar có volume KHÔNG tin được (dữ liệu snapshot, "
                           "vd import từ vnstock-dashboard) — chỉ để thử nhanh, không dùng để kết luận "
                           "có edge (CLAUDE.md mục 6).")
    if q.get("synthetic_ticks"):
        warnings.append("Khớp lệnh trên tick giả dựng từ bar (bid/ask và thứ tự đỉnh/đáy là giả định "
                        "bi quan) — kết quả thật có thể khác.")
    return {
        "title": title or "Backtest",
        "split_date": split_date,
        "n_trials": n_trials,
        "sections": sections,
        "data_quality": dict(q),
        "assumptions": dict(result.assumptions),
        "warnings": warnings,
        "rejection_counts": dict(Counter(r.code.value for r in result.rejections)),
        "rejection_events": [e for e in result.events if e.kind == "rejected"],
        "open_trades": list(result.open_trades),
    }


# --------------------------------------------------------------------- HTML ----

def _f(x, digits=0, pct=False) -> str:
    if x is None:
        return "—"
    if pct:
        return f"{x * 100:.1f}%"
    return f"{x:,.{digits}f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence]) -> str:
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
                   for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"


_SECTION_TITLES = {"all": "Toàn bộ", "in_sample": "In-sample", "out_of_sample": "Out-of-sample"}


def _render_section(key: str, sec: dict) -> str:
    m, v = sec["metrics"], sec["validation"]
    summary = _table(["Chỉ số", "Giá trị"], [
        ["Số lệnh", m["n_trades"]],
        ["Win rate", _f(m["win_rate"], pct=True)],
        ["Avg win (VNĐ, sau phí)", _f(m["avg_win_vnd"])],
        ["Avg loss (VNĐ, sau phí)", _f(m["avg_loss_vnd"])],
        ["Profit factor", _f(m["profit_factor"], 2)],
        ["Max drawdown (VNĐ)", _f(m["max_drawdown_vnd"])],
        ["P&L sau phí (VNĐ)", _f(m["total_pnl_vnd"])],
        ["P&L sau phí (điểm)", _f(m["total_pnl_points"], 1)],
        ["Trước phí (điểm)", _f(m["gross_points"], 1)],
        ["Phí (VNĐ)", _f(m["fees_vnd"])],
        ["Lý do thoát", ", ".join(f"{k}: {n}" for k, n in m["exit_reasons"].items()) or "—"],
    ])
    hours = _table(["Giờ vào", "Số lệnh", "P&L sau phí (VNĐ)"],
                   [[f"{h:02d}h", b["n"], _f(b["pnl_vnd"])] for h, b in m["by_hour"].items()])
    days = _table(["Ngày", "Số lệnh", "P&L sau phí (VNĐ)"],
                  [[d, b["n"], _f(b["pnl_vnd"])] for d, b in m["by_weekday"].items()])
    wf = v.get("walk_forward")
    ci = v.get("sharpe_ci")
    val = _table(["Kiểm định", "Kết quả"], [
        ["Sharpe / lệnh", _f(v.get("sharpe_per_trade"), 3)],
        ["p-value (sign-flip)", _f(v.get("p_value_sharpe"), 3)],
        ["p-value hiệu chỉnh (× n_trials)", _f(v.get("p_value_adj"), 3)],
        ["CI Sharpe bootstrap", f"[{ci[0]:.3f}, {ci[1]:.3f}]" if ci else "—"],
        ["Walk-forward: tỷ lệ đoạn dương", _f(wf["positive_frac"], pct=True) if wf else "—"],
        ["Walk-forward: P&L từng đoạn", ", ".join(_f(x) for x in wf["fold_pnls"]) if wf else "—"],
        ["Đạt", "CÓ" if v.get("passed") else "KHÔNG"],
        ["Lý do", "; ".join(v.get("reasons", [])) or "—"],
    ])
    return (f"<section><h2>{_SECTION_TITLES[key]}</h2>{summary}"
            f"<h3>Kiểm định thống kê (p-value, CI, Walk-forward)</h3>{val}"
            f"<h3>Theo giờ trong ngày</h3>{hours}<h3>Theo ngày trong tuần</h3>{days}</section>")


def render_html(report: dict) -> str:
    warn = "".join(f"<li>{html.escape(w)}</li>" for w in report["warnings"])
    assumptions = _table(["Giả định", "Giá trị"], [[k, v] for k, v in report["assumptions"].items()])
    quality = _table(["Dữ liệu", "Giá trị"], [[k, v] for k, v in report["data_quality"].items()])
    sections = "".join(_render_section(k, report["sections"][k])
                       for k in ("all", "in_sample", "out_of_sample") if k in report["sections"])
    rej_counts = _table(["Mã", "Số lần"], sorted(report["rejection_counts"].items()))
    rej_list = _table(["Thời điểm", "Chi tiết"],
                      [[e.ts, e.message] for e in report["rejection_events"][:200]])
    trades = report["sections"]["all"]["trades"]
    trade_rows = [[t.entry_ts, t.side, t.qty, _f(t.entry_price, 1), t.exit_ts, _f(t.exit_price, 1),
                   t.exit_reason, _f(t.pnl_points, 1), _f(t.pnl_vnd)] for t in trades]
    open_rows = [[t.entry_ts, t.side, t.qty, _f(t.entry_price, 1), _f(t.exit_price, 1),
                  _f(t.pnl_vnd)] for t in report["open_trades"]]
    split = report["split_date"]
    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(report['title'])}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;max-width:1100px;color:#222;background:#fff}}
table{{border-collapse:collapse;margin:8px 0 16px;font-size:14px;display:block;overflow-x:auto;max-width:100%}}
th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left}} th{{background:#f2f2f2}}
.warn{{background:#fff4e5;border:1px solid #f0b37e;padding:8px 16px}}
section{{border-top:2px solid #444;margin-top:24px}}
</style></head><body>
<h1>{html.escape(report['title'])}</h1>
<p>P&amp;L sau phí. Out-of-sample từ: {html.escape(str(split)) if split else "— (không chia)"} ·
n_trials = {report['n_trials']} (ghi mỗi cấu hình đã thử vào research/log.md)</p>
{f'<ul class="warn">{warn}</ul>' if warn else ''}
<h3>Giả định khớp lệnh</h3>{assumptions}<h3>Dữ liệu</h3>{quality}
{sections}
<section><h2>Lệnh bị risk từ chối</h2>{rej_counts}{rej_list}</section>
<section><h2>Danh sách lệnh</h2>{_table(["Vào", "Chiều", "SL", "Giá vào", "Ra", "Giá ra", "Lý do", "Điểm sau phí", "VNĐ sau phí"], trade_rows)}
<h3>Lệnh còn mở cuối dữ liệu</h3>{_table(["Vào", "Chiều", "SL", "Giá vào", "Giá đánh dấu", "VNĐ (chưa phí đóng)"], open_rows)}</section>
</body></html>
"""


def write_html(report: dict, path: Path) -> None:
    write_text_atomic(render_html(report), path)
