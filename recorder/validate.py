"""Kiểm định dữ liệu tick một ngày (PROMPTS P1.5) — hàm thuần, dùng bởi scripts/validate_day.py.

Mỗi nguồn (dnse / ssi): số tick (khớp / báo giá), khoảng trống > `gap_seconds` trong phiên
liên tục (tính cả đầu/cuối đoạn phiên), báo giá chéo (bid > ask), tick ngoài biên độ ±% so với
giá tham chiếu (cần `ref_price` — không tự đoán), giá nhảy bất thường giữa hai lần khớp, và
**đối chiếu khối lượng**: KL luỹ kế (`total_volume`) phải tăng đúng bằng tổng KL từng lần khớp —
thiếu ⇒ feed đã rơi message (giao thức không có số thứ tự nên đây là cách duy nhất phát hiện).

Hai nguồn: mỗi giây trong phiên liên tục, so giá (mid nếu có báo giá còn mới, không thì giá
khớp còn mới) — đếm số giây lệch > ngưỡng. Dựng bar 1 phút từ giá khớp; so OHLC với bar tham
chiếu (vd `data/imported/` = vnstock-dashboard, snapshot ~2 giây ⇒ dung sai mặc định 2 bước).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence

from core.models import Bar, Tick
from recorder.tick_recorder import load_ticks

Segment = tuple[datetime, datetime]


@dataclass(frozen=True)
class Gap:
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class SourceReport:
    n_ticks: int = 0
    n_trades: int = 0
    n_quotes: int = 0
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    gaps: list[Gap] = field(default_factory=list)
    crossed_quotes: int = 0
    out_of_band: Optional[int] = None
    jumps: int = 0
    volume: dict = field(default_factory=dict)


@dataclass
class DayReport:
    day: Optional[date]
    sources: dict[str, SourceReport] = field(default_factory=dict)
    divergence: Optional[dict] = None
    reference: Optional[dict] = None
    bars: dict[str, list[Bar]] = field(default_factory=dict)
    extras: dict[str, dict] = field(default_factory=dict)     # HĐ ghi thêm (ngày trước roll): chỉ đếm
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "sources": {k: {**asdict(v), "gaps": [(g.start, g.end, g.seconds) for g in v.gaps]}
                        for k, v in self.sources.items()},
            "divergence": self.divergence, "reference": self.reference,
            "bars": {k: len(v) for k, v in self.bars.items()},
            "extras": self.extras,
            "issues": self.issues, "notes": self.notes,
        }

    def summary(self) -> str:
        lines = [f"Kiểm định dữ liệu {self.day}"]
        for name, s in sorted(self.sources.items()):
            gap_total = sum(g.seconds for g in s.gaps)
            vol = s.volume
            vol_txt = (f"hụt {vol['missing']} HĐ" if vol.get("checked") else "không kiểm được KL")
            lines.append(
                f"- {name}: {s.n_ticks} tick ({s.n_trades} khớp, {s.n_quotes} báo giá), "
                f"{s.first_ts:%H:%M:%S}→{s.last_ts:%H:%M:%S}; {len(s.gaps)} khoảng trống "
                f"(tổng {gap_total:.0f}s); bid > ask {s.crossed_quotes}; ngoài biên độ "
                f"{'—' if s.out_of_band is None else s.out_of_band}; giá nhảy {s.jumps}; {vol_txt}; "
                f"{len(self.bars.get(name, []))} bar")
        if self.divergence:
            d = self.divergence
            lines.append(f"- lệch hai nguồn: so {d['seconds_compared']} giây, tối đa {d['max_ticks']:.1f} "
                         f"bước, {d['seconds_over_threshold']} giây vượt ngưỡng")
        if self.reference:
            r = self.reference
            lines.append(f"- so tham chiếu: {r['minutes_compared']} phút, {len(r['mismatches'])} sai lệch, "
                         f"ta thiếu {len(r['missing_in_ours'])} phút, tham chiếu thiếu "
                         f"{len(r['missing_in_reference'])} phút")
        lines += [f"! {i}" for i in self.issues] + [f"· {n}" for n in self.notes]
        return "\n".join(lines)


# ------------------------------------------------------------------ từng phần ----

def find_gaps(times: Iterable[datetime], segments: Sequence[Segment], gap_seconds: float) -> list[Gap]:
    """Khoảng trống > gap_seconds trong từng đoạn phiên (gồm đầu/cuối đoạn)."""
    ts = sorted(times)
    out: list[Gap] = []
    for seg_start, seg_end in segments:
        points = [seg_start] + [t for t in ts if seg_start <= t <= seg_end] + [seg_end]
        for a, b in zip(points, points[1:]):
            if (b - a).total_seconds() > gap_seconds:
                out.append(Gap(a, b))
    return out


def volume_check(trades: Sequence[Tick]) -> dict:
    """KL luỹ kế tăng có khớp tổng KL từng lần khớp không (từ lần đầu có total_volume)."""
    with_total = [t for t in trades if t.total_volume is not None]
    if len(with_total) < 2:
        return {"checked": False, "missing": None}
    first, last = with_total[0], with_total[-1]
    idx = trades.index(first)
    after = trades[idx + 1: trades.index(last) + 1]
    summed = sum(t.last_qty or 0 for t in after)
    implied = last.total_volume - first.total_volume
    return {"checked": True, "implied": implied, "summed": summed, "missing": implied - summed}


def _ref_price(t: Tick) -> Optional[float]:
    if t.bid is not None and t.ask is not None:
        return (t.bid + t.ask) / 2.0
    return t.last


def divergence_stats(ticks: Sequence[Tick], segments: Sequence[Segment], tick_size: float,
                     threshold_ticks: float, stale_seconds: float) -> Optional[dict]:
    """Mỗi giây trong phiên liên tục: lệch giá giữa hai nguồn (chỉ khi cả hai còn mới)."""
    sources = sorted({t.source for t in ticks})
    if len(sources) < 2:
        return None
    a, b = sources[0], sources[1]
    events = sorted((t for t in ticks if t.source in (a, b) and _ref_price(t) is not None),
                    key=lambda t: t.ts)
    latest: dict[str, tuple[datetime, float]] = {}
    i, n = 0, len(events)
    compared, over, max_gap, total = 0, 0, 0.0, 0.0
    for seg_start, seg_end in segments:
        t = seg_start
        while t <= seg_end:
            while i < n and events[i].ts <= t:
                e = events[i]
                latest[e.source] = (e.ts, _ref_price(e))
                i += 1
            pa, pb = latest.get(a), latest.get(b)
            if pa and pb and (t - pa[0]).total_seconds() <= stale_seconds \
                    and (t - pb[0]).total_seconds() <= stale_seconds:
                gap = abs(pa[1] - pb[1]) / tick_size
                compared += 1
                total += gap
                max_gap = max(max_gap, gap)
                if gap > threshold_ticks + 1e-9:
                    over += 1
            t += timedelta(seconds=1)
    return {"sources": [a, b], "seconds_compared": compared, "max_ticks": max_gap,
            "mean_ticks": total / compared if compared else None,
            "seconds_over_threshold": over, "threshold_ticks": threshold_ticks}


def build_bars(ticks: Sequence[Tick], interval_s: int = 60) -> list[Bar]:
    """Bar từ giá khớp (bỏ tick chỉ có báo giá); volume = tổng KL từng lần khớp."""
    groups: dict[tuple[str, datetime], list[Tick]] = {}
    for t in sorted((t for t in ticks if t.last is not None), key=lambda t: t.ts):
        epoch = datetime.combine(t.ts.date(), datetime.min.time())
        start = epoch + timedelta(seconds=((t.ts - epoch).total_seconds() // interval_s) * interval_s)
        groups.setdefault((t.symbol, start), []).append(t)
    out = []
    for (sym, start), ts in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        prices = [t.last for t in ts]
        out.append(Bar(sym, start, prices[0], max(prices), min(prices), prices[-1],
                       sum(t.last_qty or 0 for t in ts), interval_s, True))
    return out


def compare_reference(ours: Sequence[Bar], reference: Sequence[Bar], tick_size: float,
                      tolerance_ticks: float = 2.0) -> dict:
    mine = {(b.symbol, b.start): b for b in ours}
    ref = {(b.symbol, b.start): b for b in reference}
    symbols = {s for s, _ in mine}
    ref = {k: v for k, v in ref.items() if k[0] in symbols}
    mismatches = []
    common = sorted(set(mine) & set(ref), key=lambda k: k[1])
    for key in common:
        for fld in ("open", "high", "low", "close"):
            diff = abs(getattr(mine[key], fld) - getattr(ref[key], fld)) / tick_size
            if diff > tolerance_ticks + 1e-9:
                mismatches.append({"start": key[1], "field": fld, "ours": getattr(mine[key], fld),
                                   "reference": getattr(ref[key], fld), "ticks": round(diff, 1)})
    return {"minutes_compared": len(common), "tolerance_ticks": tolerance_ticks,
            "mismatches": mismatches,
            "missing_in_ours": sorted(k[1] for k in set(ref) - set(mine)),
            "missing_in_reference": sorted(k[1] for k in set(mine) - set(ref))}


def write_bars(bars: Sequence[Bar], out_dir: Path, day: date, source: str,
               filename: Optional[str] = None) -> Path:
    """Ghi bar theo schema `backtest.engine.load_bars_parquet` (ghi nguyên tử)."""
    import pandas as pd

    from utils.atomic_io import write_dataframe_atomic
    df = pd.DataFrame([{"symbol": b.symbol, "start": b.start, "open": b.open, "high": b.high,
                        "low": b.low, "close": b.close, "volume": b.volume,
                        "volume_reliable": b.volume_reliable, "interval_s": b.interval_s,
                        "source": source} for b in bars])
    path = Path(out_dir) / (filename or f"{day.isoformat()}.parquet")
    write_dataframe_atomic(df, path)
    return path


# -------------------------------------------------------------------- cả ngày ----

def validate_day(day_dir: Path, *, segments: Sequence[Segment], tick_size: float,
                 gap_seconds: float, divergence_ticks: float, stale_seconds: float,
                 ref_price: Optional[float] = None, daily_limit_pct: float = 7.0,
                 jump_pct: float = 1.0, reference_bars: Optional[Sequence[Bar]] = None,
                 bars_source: Optional[str] = None, tolerance_ticks: float = 2.0,
                 primary_symbol: Optional[str] = None) -> DayReport:
    """`primary_symbol` = HĐ bot giao dịch ngày đó: mọi kiểm chất lượng chỉ trên HĐ này; HĐ khác (ghi thêm ngày
    trước roll) chỉ được đếm và dựng bar. Không truyền mà dữ liệu có nhiều HĐ ⇒ báo lỗi (kiểm lẫn HĐ là sai)."""
    all_ticks = load_ticks(Path(day_dir)) if Path(day_dir).exists() else []
    day = all_ticks[0].ts.date() if all_ticks else None
    rep = DayReport(day)
    if not all_ticks:
        rep.issues.append(f"không có dữ liệu trong {day_dir}")
        return rep
    symbols = {t.symbol for t in all_ticks}
    if primary_symbol is None:
        ticks = all_ticks
        if len(symbols) > 1:
            rep.issues.append(f"dữ liệu có nhiều hợp đồng ({', '.join(sorted(symbols))}) mà không chỉ định HĐ chính "
                              "— kiểm chất lượng bị lẫn HĐ")
    else:
        ticks = [t for t in all_ticks if t.symbol == primary_symbol]
        if not ticks:
            rep.issues.append(f"không có tick nào của {primary_symbol} (HĐ giao dịch ngày này)")
        for sym in sorted(symbols - {primary_symbol}):
            mine = [t for t in all_ticks if t.symbol == sym]
            n_tr = sum(1 for t in mine if t.last is not None)
            rep.extras[sym] = {"ticks": len(mine), "trades": n_tr, "sources": sorted({t.source or "?" for t in mine})}
            rep.notes.append(f"{sym}: {n_tr} lần khớp — HĐ ghi thêm (đo chênh lệch roll), không kiểm chất lượng")
    if ref_price is None:
        rep.notes.append("Chưa có giá tham chiếu (--ref-price) ⇒ bỏ qua kiểm tra biên độ ±%")
    lo = hi = None
    if ref_price is not None:
        lo, hi = ref_price * (1 - daily_limit_pct / 100), ref_price * (1 + daily_limit_pct / 100)

    for src in sorted({t.source or "?" for t in all_ticks}):
        # bar: mọi HĐ của nguồn này (HĐ ghi thêm cần bar để đo chênh lệch); kiểm chất lượng: chỉ HĐ chính
        rep.bars[src] = build_bars([t for t in all_ticks if (t.source or "?") == src and t.last is not None])
        mine = [t for t in ticks if (t.source or "?") == src]
        if not mine:
            continue
        trades = [t for t in mine if t.last is not None]
        s = SourceReport(n_ticks=len(mine), n_trades=len(trades), n_quotes=len(mine) - len(trades),
                         first_ts=mine[0].ts, last_ts=mine[-1].ts)
        s.gaps = find_gaps([t.ts for t in mine], segments, gap_seconds)
        s.crossed_quotes = sum(1 for t in mine if t.bid is not None and t.ask is not None and t.bid > t.ask)
        if lo is not None:
            s.out_of_band = sum(1 for t in mine
                                if any(p is not None and not lo <= p <= hi for p in (t.last, t.bid, t.ask)))
        s.jumps = sum(1 for a, b in zip(trades, trades[1:])
                      if a.last and abs(b.last - a.last) / a.last * 100 > jump_pct)
        s.volume = volume_check(trades)
        rep.sources[src] = s

        if s.gaps:
            rep.issues.append(f"{src}: {len(s.gaps)} khoảng trống > {gap_seconds:g}s trong phiên "
                              f"(dài nhất {max(g.seconds for g in s.gaps):.0f}s)")
        if s.crossed_quotes:
            rep.issues.append(f"{src}: {s.crossed_quotes} báo giá bid > ask")
        if s.out_of_band:
            rep.issues.append(f"{src}: {s.out_of_band} tick ngoài biên độ ±{daily_limit_pct:g}%")
        if s.jumps:
            rep.notes.append(f"{src}: {s.jumps} lần giá nhảy > {jump_pct:g}% giữa hai lần khớp")
        if s.volume.get("checked") and s.volume["missing"]:
            rep.issues.append(f"{src}: hụt {s.volume['missing']} HĐ khớp lệnh so với KL luỹ kế "
                              "(feed rơi message)")

    rep.divergence = divergence_stats(ticks, segments, tick_size, divergence_ticks, stale_seconds)
    if rep.divergence and rep.divergence["seconds_over_threshold"]:
        rep.issues.append(f"hai nguồn lệch > {divergence_ticks:g} bước trong "
                          f"{rep.divergence['seconds_over_threshold']} giây")
    if reference_bars is not None:
        src = bars_source if bars_source in rep.bars else next(iter(sorted(rep.bars)))
        ours = [b for b in rep.bars[src] if primary_symbol is None or b.symbol == primary_symbol]
        rep.reference = compare_reference(ours, reference_bars, tick_size, tolerance_ticks)
        if rep.reference["mismatches"]:
            rep.issues.append(f"{len(rep.reference['mismatches'])} sai lệch OHLC > {tolerance_ticks:g} "
                              f"bước so với tham chiếu (nguồn {src})")
    return rep
