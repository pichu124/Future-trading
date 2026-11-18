"""Chấm nhận định regime "shadow" (scripts/regime_call.py) với thực tế và với baseline thống kê.

CHỈ NGHIÊN CỨU. Luật ĐĂNG KÝ TRƯỚC 16/11/2026 (research/log.md, "Nhật ký regime shadow"); số nằm ở settings
`research.regime_shadow`.

    python scripts/score_regime_calls.py --bars data/history/ohlc1m data/bars

- Thực tế mỗi ngày: nến 1 phút của HỢP ĐỒNG BOT GIAO DỊCH ngày đó trong [continuous_am_start, close] ⇒ mở, đỉnh, đáy,
  đóng. Ngày chưa có nến từ `atc_start` ⇒ chưa xong, không chấm. ADR = trung bình (đỉnh − đáy) `adr_days` phiên có dữ
  liệu liền trước.
- Nhãn: vol = (đỉnh − đáy)/ADR; day_type = |đóng − mở|/(đỉnh − đáy); direction = (đóng − mở)/ADR.
- Điểm Brier đa lớp Σ(p − o)². Baseline (chỉ dùng nhãn các ngày TRƯỚC ngày chấm, kể cả trước nhận định đầu tiên):
  `climatology` = tần suất làm trơn Laplace; `markov` = tần suất có điều kiện theo nhãn phiên liền trước.
- Kết luận: chỉ câu hỏi chính, trên ĐÚNG `min_days` phiên chấm được đầu tiên (chạy lại bao nhiêu lần cũng ra một kết
  quả ⇒ không "ngó rồi dừng lúc đẹp"). CÓ GIÁ TRỊ ⇔ Brier tranh luận < CẢ HAI baseline và p sign-flip một phía của
  (Brier baseline tốt hơn − Brier tranh luận) < `p_threshold`.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts                                   # noqa: E402
from core.calendar import TradingCalendar                    # noqa: E402
from core.clock import SessionSchedule                       # noqa: E402
from core.models import Bar                                  # noqa: E402
from scripts.regime_call import FORECASTERS, QUESTIONS, load_calls   # noqa: E402

BASELINES = ("climatology", "markov")
SIGN_FLIP_SIMS = 10_000


@dataclass(frozen=True)
class Rules:
    adr_days: int
    vol_low_below: float
    vol_high_above: float
    trend_min_efficiency: float
    flat_band_adr: float
    primary_question: str
    min_days: int
    p_threshold: float
    verdict_from: Optional[date] = None      # nhận định trước mốc này không vào cửa sổ kết luận (đổi gói dữ liệu 18/11)

    def __post_init__(self) -> None:
        if self.primary_question not in QUESTIONS:
            raise ValueError(f"primary_question phải thuộc {sorted(QUESTIONS)}")
        if self.adr_days < 1 or self.min_days < 1 or not self.vol_low_below < self.vol_high_above:
            raise ValueError("research.regime_shadow: tham số không hợp lệ")

    @classmethod
    def from_settings(cls, settings) -> "Rules":
        r = lambda k: settings.require(f"research.regime_shadow.{k}")   # noqa: E731
        return cls(int(r("adr_days")), float(r("vol_low_below")), float(r("vol_high_above")),
                   float(r("trend_min_efficiency")), float(r("flat_band_adr")), str(r("primary_question")),
                   int(r("min_days")), float(r("p_threshold")),
                   _date_or_none(settings.get("research.regime_shadow.verdict_from")))


def _date_or_none(v) -> Optional[date]:
    if v is None or v == "":
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v))


@dataclass(frozen=True)
class DayStat:
    day: date
    contract: str
    open: float
    high: float
    low: float
    close: float

    @property
    def range(self) -> float:
        return self.high - self.low


# ------------------------------------------------------------------ thực tế ----

def day_stats(bars: Iterable[Bar], schedule: SessionSchedule, calendar: TradingCalendar,
              roll_days: int = 1) -> list[DayStat]:
    """Mở/đỉnh/đáy/đóng mỗi ngày của hợp đồng bot giao dịch ngày đó, chỉ tính ngày ĐÃ XONG (có nến từ ATC)."""
    by_day: dict[date, list[Bar]] = {}
    wanted: dict[date, str] = {}
    for b in bars:
        d = b.start.date()
        if d not in wanted:
            wanted[d] = contracts.trading_contract(d, calendar, roll_days)
        if not schedule.continuous_am_start <= b.start.time() <= schedule.close:
            continue
        if contracts.resolve_symbol(b.symbol, d, calendar) != wanted[d]:
            continue
        by_day.setdefault(d, []).append(b)
    out = []
    for d in sorted(by_day):
        day_bars = sorted(by_day[d], key=lambda b: b.start)
        if day_bars[-1].start.time() < schedule.atc_start:
            continue
        out.append(DayStat(d, wanted[d], day_bars[0].open, max(b.high for b in day_bars),
                           min(b.low for b in day_bars), day_bars[-1].close))
    return out


def realized_labels(stats: Sequence[DayStat], rules: Rules) -> dict[date, dict[str, str]]:
    """{ngày: {câu hỏi: lớp thực tế}} — chỉ ngày có đủ `adr_days` phiên dữ liệu liền trước."""
    stats = sorted(stats, key=lambda s: s.day)
    out: dict[date, dict[str, str]] = {}
    for i, s in enumerate(stats):
        if i < rules.adr_days:
            continue
        adr = sum(p.range for p in stats[i - rules.adr_days:i]) / rules.adr_days
        if adr <= 0:
            continue
        ratio, move = s.range / adr, s.close - s.open
        vol = "low" if ratio < rules.vol_low_below else "high" if ratio > rules.vol_high_above else "normal"
        efficiency = abs(move) / s.range if s.range > 0 else 0.0
        day_type = "trend" if efficiency >= rules.trend_min_efficiency else "range"
        band = rules.flat_band_adr * adr
        direction = "up" if move >= band else "down" if move <= -band else "flat"
        out[s.day] = {"vol": vol, "day_type": day_type, "direction": direction}
    return out


# ------------------------------------------------------------------ điểm & baseline ----

def brier(probs: Mapping[str, float], outcome: str) -> float:
    if outcome not in probs:
        raise KeyError(outcome)
    return float(sum((p - (1.0 if c == outcome else 0.0)) ** 2 for c, p in probs.items()))


def baseline_probs(past: Sequence[str], classes: Sequence[str], kind: str) -> dict[str, float]:
    """Dự báo của baseline cho ngày kế tiếp, chỉ từ chuỗi nhãn `past` (cũ nhất trước). Làm trơn Laplace (+1 mỗi lớp)."""
    if kind == "climatology":
        sample = list(past)
    elif kind == "markov":
        sample = [nxt for prev, nxt in zip(past, past[1:]) if prev == past[-1]] if past else []
    else:
        raise ValueError(f"baseline không biết: {kind}")
    n, k = len(sample), len(classes)
    return {c: (sample.count(c) + 1) / (n + k) for c in classes}


def sign_flip_pvalue(diffs: Sequence[float], n_sim: int = SIGN_FLIP_SIMS, seed: int = 42) -> Optional[float]:
    """p một phía cho H1 "trung bình `diffs` > 0": lật dấu ngẫu nhiên từng ngày. None khi < 3 ngày."""
    import numpy as np
    arr = np.asarray(diffs, dtype=float)
    if len(arr) < 3:
        return None
    actual = arr.mean()
    signs = np.random.default_rng(seed).choice((-1.0, 1.0), size=(n_sim, len(arr)))
    sims = (signs * np.abs(arr)).mean(axis=1)
    return float((np.sum(sims >= actual - 1e-12) + 1) / (n_sim + 1))


def _summary(rows: Sequence[dict], q: str) -> dict:
    n = len(rows)
    mean = lambda who: sum(r[q]["brier"][who] for r in rows) / n   # noqa: E731
    briers = {who: mean(who) for who in FORECASTERS + BASELINES}
    best = min(BASELINES, key=lambda b: briers[b])
    return {"n": n, "brier": briers, "best_baseline": best,
            "p_vs_baseline": sign_flip_pvalue([r[q]["brier"][best] - r[q]["brier"]["debate"] for r in rows]),
            "p_vs_single": sign_flip_pvalue([r[q]["brier"]["single"] - r[q]["brier"]["debate"] for r in rows]),
            "base_rates": {c: sum(r[q]["outcome"] == c for r in rows) / n for c in QUESTIONS[q]}}


def score(calls: Sequence[Mapping], labels: Mapping[date, Mapping[str, str]], rules: Rules,
          calendar: TradingCalendar) -> dict:
    calls = sorted(calls, key=lambda c: c["trade_date"])
    label_days = sorted(labels)
    rows, unscored = [], []
    for c in calls:
        d = date.fromisoformat(c["trade_date"])
        if d not in labels:
            unscored.append(c["trade_date"])
            continue
        past_days = [x for x in label_days if x < d]
        row: dict = {"date": c["trade_date"]}
        for q, classes in QUESTIONS.items():
            outcome = labels[d][q]
            past = [labels[x][q] for x in past_days]
            probs = {**{f: dict(c[f][q]) for f in FORECASTERS},
                     **{b: baseline_probs(past, classes, b) for b in BASELINES}}
            row[q] = {"outcome": outcome, "probs": probs,
                      "brier": {who: brier(p, outcome) for who, p in probs.items()}}
        rows.append(row)

    missing: list[str] = []
    if calls:
        have = {c["trade_date"] for c in calls}
        d, last = date.fromisoformat(calls[0]["trade_date"]), date.fromisoformat(calls[-1]["trade_date"])
        while d <= last:
            if calendar.is_trading_day(d) and d.isoformat() not in have:
                missing.append(d.isoformat())
            d = calendar.next_trading_day(d)

    eligible = [r for r in rows if rules.verdict_from is None or date.fromisoformat(r["date"]) >= rules.verdict_from]
    rep = {"n_calls": len(calls), "n_scored": len(rows), "unscored": unscored, "missing_days": missing,
           "verdict_from": rules.verdict_from, "n_before_verdict_from": len(rows) - len(eligible),
           "rows": rows, "primary": rules.primary_question, "min_days": rules.min_days,
           "p_threshold": rules.p_threshold,
           "all": {q: _summary(rows, q) for q in QUESTIONS} if rows else {},
           "verdict_window": None, "verdict": "CHƯA ĐỦ", "reasons": []}
    if len(eligible) < rules.min_days:
        rep["reasons"].append(f"mới chấm được {len(eligible)}/{rules.min_days} phiên"
                              + (f" từ mốc {rules.verdict_from}" if rules.verdict_from else ""))
        return rep
    window = eligible[:rules.min_days]
    rep["verdict_window"] = {q: _summary(window, q) for q in QUESTIONS}
    w = rep["verdict_window"][rules.primary_question]
    for b in BASELINES:
        if not w["brier"]["debate"] < w["brier"][b]:
            rep["reasons"].append(f"Brier tranh luận {w['brier']['debate']:.4f} không thấp hơn {b} {w['brier'][b]:.4f}")
    p = w["p_vs_baseline"]
    if p is None or not p < rules.p_threshold:
        rep["reasons"].append(f"p so với {w['best_baseline']} = {p if p is None else round(p, 4)} "
                              f"không < {rules.p_threshold}")
    rep["verdict"] = "KHÔNG CÓ GIÁ TRỊ" if rep["reasons"] else "CÓ GIÁ TRỊ"
    return rep


# ------------------------------------------------------------------ báo cáo ----

def _fmt_p(p: Optional[float]) -> str:
    return "—" if p is None else f"{p:.3f}"


def _table(summary: Mapping[str, dict], primary: str) -> list[str]:
    lines = ["  câu hỏi      n   tranh luận  một agent  climatology  markov   p(vs baseline)  p(vs một agent)"]
    for q in QUESTIONS:
        s = summary[q]
        b = s["brier"]
        mark = "*" if q == primary else " "
        lines.append(f" {mark}{q:<10} {s['n']:>3}   {b['debate']:.4f}      {b['single']:.4f}     "
                     f"{b['climatology']:.4f}       {b['markov']:.4f}   {_fmt_p(s['p_vs_baseline']):<8} "
                     f"({s['best_baseline']})  {_fmt_p(s['p_vs_single'])}")
    return lines


def render(rep: dict) -> str:
    lines = [f"Nhật ký regime shadow — {rep['n_calls']} nhận định, chấm được {rep['n_scored']} "
             f"(Brier: thấp hơn = tốt hơn; * = câu hỏi chính `{rep['primary']}`)"]
    if rep["all"]:
        lines.append("Tất cả phiên đã chấm (chỉ để theo dõi, KHÔNG dùng kết luận):")
        lines += _table(rep["all"], rep["primary"])
    if rep["verdict_window"]:
        lines.append(f"Cửa sổ kết luận = {rep['min_days']} phiên đầu tiên"
                     + (f" từ {rep['verdict_from']}" if rep.get("verdict_from") else "") + ":")
        lines += _table(rep["verdict_window"], rep["primary"])
    if rep.get("n_before_verdict_from"):
        lines.append(f"{rep['n_before_verdict_from']} phiên chấm được nằm trước mốc {rep['verdict_from']} "
                     "(gói dữ liệu cũ) — chỉ theo dõi, không vào kết luận")
    if rep["unscored"]:
        lines.append(f"Chưa chấm được ({len(rep['unscored'])}; thiếu nến ngày đó hoặc thiếu lịch sử ADR): "
                     + ", ".join(rep["unscored"][-5:]))
    if rep["missing_days"]:
        lines.append(f"Ngày giao dịch KHÔNG có nhận định ({len(rep['missing_days'])}): "
                     + ", ".join(rep["missing_days"][-10:]))
    lines.append(f"KẾT LUẬN: {rep['verdict']}" + (" — " + "; ".join(rep["reasons"]) if rep["reasons"] else ""))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Chấm nhận định regime shadow (chỉ nghiên cứu)")
    ap.add_argument("--bars", nargs="+", required=True, help="thư mục nến 1 phút, vd data/history/ohlc1m data/bars")
    ap.add_argument("--calls", help="thư mục nhận định (mặc định settings research.regime_shadow.calls_dir)")
    args = ap.parse_args(argv)

    from core.settings import load_settings
    settings = load_settings()
    schedule = SessionSchedule.from_mapping(settings.get("session", {}))
    calendar = TradingCalendar.default()
    rules = Rules.from_settings(settings)
    calls_dir = Path(args.calls) if args.calls else settings.path("research.regime_shadow.calls_dir")
    calls = load_calls(calls_dir, schedule)
    if not calls:
        print(f"Chưa có nhận định nào trong {calls_dir}")
        return 1
    from backtest.engine import load_bars_parquet
    bars, skipped = load_bars_parquet(args.bars)
    stats = day_stats(bars, schedule, calendar, int(settings.require("contract.roll_days_before_expiry")))
    print(render(score(calls, realized_labels(stats, rules), rules, calendar)))
    if skipped:
        print(f"(bỏ {skipped} dòng nến hỏng)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
