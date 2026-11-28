"""Kiểm toán chi phí khớp lệnh từ dữ liệu recorder — đo phần "trượt giá + spread" mà backtest đang GIẢ ĐỊNH.

    python -m backtest.cost_audit --from 2026-11-10 --to 2026-11-27
    python -m backtest.cost_audit --raw D:\\Future-trading\\data\\raw --from 2026-11-10 --to 2026-11-27 --timeouts 10 30 60

Backtest tính mỗi chiều: trượt `backtest.slippage_ticks` + nửa spread giả (làm tròn lên 1 bước) = 0,2 điểm với settings
hiện tại; cộng phí + thuế hai chiều ⇒ 0,87 điểm mỗi lượt. Phí là cố định; phần 0,4 điểm khớp lệnh chưa từng được đo.
Ở đây đo trên tick + giá tốt nhất (bid/ask và khối lượng chờ) recorder đã ghi (`data/raw/YYYY-MM-DD/`), chỉ trong phiên
liên tục, chỉ HĐ bot giao dịch ngày đó, một nguồn feed:

- Spread theo thời gian: tỉ lệ thời gian spread = 1, 2, ≥ 3 bước; nửa spread trung bình; độ sâu ở giá tốt nhất; theo khung giờ.
- Tại mỗi đầu phút trong cửa sổ được vào lệnh, CẢ HAI chiều, lệnh 1 HĐ ra quyết định lúc t, tới sàn lúc t + độ trễ:
  * lệnh thị trường: thiếu hụt = (giá khớp − mid lúc t) × chiều — mua ở ask, bán ở bid của báo giá lúc tới sàn;
  * lệnh giới hạn ở giá tốt nhất CÙNG phía lúc tới sàn, xếp SAU toàn bộ khối lượng đang chờ ở mức đó (không tính huỷ phía
    trước ⇒ bi quan). Khớp khi có giao dịch XUYÊN giá, hoặc tổng khối lượng giao dịch ĐÚNG giá vượt quá hàng chờ phía
    trước. Hết T giây chưa khớp ⇒ đuổi bằng lệnh thị trường (tới sàn sau thêm một độ trễ). Thiếu hụt so với mid lúc t.
- Giao dịch thật: nửa spread hiệu dụng |giá khớp − mid|, tỉ lệ khớp ở giá bán / giá mua.

Giới hạn: quyết định lấy ở đầu phút, KHÔNG gắn với tín hiệu nào. Tín hiệu theo đà (giá hay chạy tiếp theo chiều lệnh) làm
lệnh giới hạn ít khớp hơn và phải đuổi đắt hơn ⇒ con số ở đây là CẬN DƯỚI chi phí của lệnh giới hạn cho loại tín hiệu đó.
Đây là phép đo, không phải lần thử chiến lược: không tính `n_trials`. Đổi giả định chi phí của backtest dựa trên kết quả
phải ghi research/log.md trước, và không được dùng để "cứu" các lần thử đã trượt.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

Segment = tuple[datetime, datetime]
NS = 1_000_000_000
DEFAULT_BUCKETS = ((time(9, 0), time(9, 15)), (time(9, 15), time(10, 0)), (time(10, 0), time(11, 0)),
                   (time(11, 0), time(11, 30)), (time(13, 0), time(14, 0)), (time(14, 0), time(14, 30)))


@dataclass(frozen=True)
class AuditConfig:
    tick_size: float
    latency_s: float
    timeouts: tuple[int, ...]
    entry_from: time
    entry_until: time
    max_quote_age_s: float = 30.0    # báo giá cũ hơn mức này lúc ra quyết định ⇒ nghi feed hụt ⇒ bỏ thời điểm đó


def _ns(dt: datetime) -> int:
    return pd.Timestamp(dt).value


# ------------------------------------------------------------------ dữ liệu ----
def load_day(day_dir: Path, symbol: str, source: str) -> pd.DataFrame:
    """Mọi tick đã ghi của một ngày (bản gộp + part), không trùng (`session`, `rx_seq`), một HĐ, một nguồn, theo giờ."""
    d = Path(day_dir)
    files = ([d / "ticks.parquet"] if (d / "ticks.parquet").exists() else []) + sorted(d.glob("part-*.parquet"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df.drop_duplicates(subset=["session", "rx_seq"])
    df = df[(df["symbol"] == symbol) & (df["source"] == source)]
    return df.sort_values(["ts", "recv_ts"], kind="stable").reset_index(drop=True)


class DayBook:
    """Chuỗi báo giá hợp lệ (bid < ask) và chuỗi giao dịch của một ngày, dạng mảng numpy (giờ = ns)."""

    def __init__(self, quote_ns, bid, ask, bq, aq, trade_ns, trade_last, trade_px, trade_qty, trade_bid, trade_ask,
                 tick_size: float):
        self.quote_ns, self.bid, self.ask, self.bq, self.aq = quote_ns, bid, ask, bq, aq
        self.trade_ns, self.trade_last, self.trade_px, self.trade_qty = trade_ns, trade_last, trade_px, trade_qty
        self.trade_bid, self.trade_ask = trade_bid, trade_ask
        self.tick_size = tick_size

    @classmethod
    def from_frame(cls, df: pd.DataFrame, tick_size: float) -> "DayBook":
        if df is None or df.empty:
            e = np.array([], dtype=np.int64)
            f = np.array([], dtype=float)
            return cls(e, f, f, f, f, e, f, e, e, f, f, tick_size)
        ts = pd.to_datetime(df["ts"]).to_numpy(dtype="datetime64[ns]").astype(np.int64)   # đơn vị gốc có thể là µs
        bid = pd.to_numeric(df["bid"], errors="coerce").to_numpy(dtype=float)
        ask = pd.to_numeric(df["ask"], errors="coerce").to_numpy(dtype=float)
        bq = pd.to_numeric(df["bid_qty"], errors="coerce").to_numpy(dtype=float)
        aq = pd.to_numeric(df["ask_qty"], errors="coerce").to_numpy(dtype=float)
        last = pd.to_numeric(df["last"], errors="coerce").to_numpy(dtype=float)
        qty = pd.to_numeric(df["last_qty"], errors="coerce").fillna(0).to_numpy(dtype=float)
        valid_q = ~np.isnan(bid) & ~np.isnan(ask) & (ask - bid > tick_size / 2)
        is_tr = ~np.isnan(last)
        return cls(ts[valid_q], bid[valid_q], ask[valid_q], bq[valid_q], aq[valid_q],
                   ts[is_tr], last[is_tr], np.rint(last[is_tr] / tick_size).astype(np.int64),
                   qty[is_tr].astype(np.int64), bid[is_tr], ask[is_tr], tick_size)

    def _qidx(self, t_ns: int) -> int:
        return int(np.searchsorted(self.quote_ns, t_ns, side="right")) - 1

    def quote_at(self, t_ns: int) -> Optional[tuple]:
        """(bid, ask, bid_qty, ask_qty) của báo giá hợp lệ cuối cùng tại hoặc trước `t_ns`; khối lượng không rõ ⇒ None."""
        i = self._qidx(t_ns)
        if i < 0:
            return None
        bq, aq = self.bq[i], self.aq[i]
        return (float(self.bid[i]), float(self.ask[i]), None if np.isnan(bq) else int(bq),
                None if np.isnan(aq) else int(aq))

    def quote_age_s(self, t_ns: int) -> Optional[float]:
        i = self._qidx(t_ns)
        return None if i < 0 else (t_ns - int(self.quote_ns[i])) / NS


# -------------------------------------------------------------------- spread ----
def _states(b: DayBook, s_ns: int, e_ns: int) -> tuple[np.ndarray, np.ndarray]:
    """(chỉ số báo giá, thời lượng ns) của các trạng thái báo giá phủ đoạn [s, e)."""
    lo = int(np.searchsorted(b.quote_ns, s_ns, side="right"))
    hi = int(np.searchsorted(b.quote_ns, e_ns, side="left"))
    idx = np.concatenate(([lo - 1], np.arange(lo, hi)))
    starts = np.concatenate(([s_ns], b.quote_ns[lo:hi]))
    ends = np.concatenate((starts[1:], [e_ns]))
    keep = idx >= 0
    return idx[keep].astype(np.int64), (ends - starts)[keep]


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> Optional[float]:
    m = ~np.isnan(values) & (weights > 0)
    if not m.any():
        return None
    v, w = values[m], weights[m]
    o = np.argsort(v)
    cw = np.cumsum(w[o])
    return float(v[o][np.searchsorted(cw, cw[-1] / 2.0)])


def _profile(b: DayBook, pieces: Sequence[tuple[int, int]], tick: float) -> dict:
    idx_all, dur_all = [], []
    for s, e in pieces:
        if e > s:
            i, d = _states(b, s, e)
            idx_all.append(i)
            dur_all.append(d)
    idx = np.concatenate(idx_all) if idx_all else np.array([], dtype=np.int64)
    dur = np.concatenate(dur_all).astype(float) if dur_all else np.array([], dtype=float)
    total = float(dur.sum())
    if total <= 0:
        return {"seconds": 0.0, "frac": {}, "mean_half_spread": None, "min_depth_median": None}
    spread = b.ask[idx] - b.bid[idx]
    ticks = np.minimum(np.rint(spread / tick).astype(int), 3)
    frac = {int(k): float(dur[ticks == k].sum() / total) for k in sorted(set(ticks.tolist()))}
    depth = np.fmin(b.bq[idx], b.aq[idx])
    depth[np.isnan(b.bq[idx]) | np.isnan(b.aq[idx])] = np.nan
    md = _weighted_median(depth, dur)
    return {"seconds": total / NS, "frac": frac, "mean_half_spread": float((spread / 2 * dur).sum() / total),
            "min_depth_median": None if md is None else int(md)}


def spread_profile(b: DayBook, segments: Sequence[Segment], tick: float,
                   buckets: Sequence[tuple[time, time]] = DEFAULT_BUCKETS) -> dict:
    """Spread và độ sâu theo THỜI GIAN trong các đoạn phiên (trạng thái trước đầu đoạn được mang vào)."""
    segs = [(_ns(s), _ns(e)) for s, e in segments]
    out = _profile(b, segs, tick)
    rows = []
    for t0, t1 in buckets:
        pieces = []
        for s, e in segments:
            lo = max(s, datetime.combine(s.date(), t0))
            hi = min(e, datetime.combine(s.date(), t1))
            if hi > lo:
                pieces.append((_ns(lo), _ns(hi)))
        p = _profile(b, pieces, tick)
        rows.append({"from": t0, "until": t1, **p})
    out["buckets"] = rows
    return out


# ---------------------------------------------------------- thời điểm quyết định ----
def decision_times(day: date, segments: Sequence[Segment], cfg: AuditConfig) -> list[datetime]:
    """Đầu mỗi phút trong [entry_from, entry_until) và trong một đoạn phiên, còn đủ chỗ cho T lớn nhất + hai độ trễ."""
    room = timedelta(seconds=2 * cfg.latency_s + max(cfg.timeouts))
    out = []
    for s, e in segments:
        t = s.replace(second=0, microsecond=0)
        if t < s:
            t += timedelta(minutes=1)
        while t + room < e:
            if cfg.entry_from <= t.time() < cfg.entry_until:
                out.append(t)
            t += timedelta(minutes=1)
    return sorted(set(out))


def _mid(qt: tuple) -> float:
    return (qt[0] + qt[1]) / 2.0


def market_now(b: DayBook, t_ns: int, side: int, cfg: AuditConfig) -> Optional[float]:
    """Thiếu hụt (điểm) của lệnh thị trường ra quyết định lúc t: (giá khớp lúc tới sàn − mid lúc t) × chiều."""
    q0 = b.quote_at(t_ns)
    qa = b.quote_at(t_ns + int(cfg.latency_s * NS))
    if q0 is None or qa is None:
        return None
    exec_px = qa[1] if side > 0 else qa[0]
    return side * (exec_px - _mid(q0))


def limit_then_market(b: DayBook, t_ns: int, side: int, cfg: AuditConfig) -> Optional[dict]:
    """{T: {filled, fill_s, shortfall}} cho lệnh giới hạn ở giá tốt nhất cùng phía, đuổi bằng lệnh thị trường sau T giây."""
    lat = int(cfg.latency_s * NS)
    q0, qa = b.quote_at(t_ns), b.quote_at(t_ns + lat)
    if q0 is None or qa is None:
        return None
    mid0 = _mid(q0)
    arrive = t_ns + lat
    price = qa[0] if side > 0 else qa[1]
    p_ticks = int(round(price / cfg.tick_size))
    queue = qa[2] if side > 0 else qa[3]
    queue = math.inf if queue is None else queue
    horizon = arrive + int(max(cfg.timeouts) * NS)
    lo = int(np.searchsorted(b.trade_ns, arrive, side="right"))
    hi = int(np.searchsorted(b.trade_ns, horizon, side="right"))
    px, qty, tns = b.trade_px[lo:hi], b.trade_qty[lo:hi], b.trade_ns[lo:hi]
    through = (px < p_ticks) if side > 0 else (px > p_ticks)
    cum = np.cumsum(np.where(px == p_ticks, qty, 0))
    hit = through | (cum > queue)
    fill_ns = int(tns[int(np.argmax(hit))]) if hit.any() else None
    out = {}
    for T in cfg.timeouts:
        end = arrive + int(T * NS)
        if fill_ns is not None and fill_ns <= end:
            out[T] = {"filled": True, "fill_s": (fill_ns - t_ns) / NS, "shortfall": side * (price - mid0)}
        else:
            qc = b.quote_at(end + lat)
            exec_px = None if qc is None else (qc[1] if side > 0 else qc[0])
            out[T] = {"filled": False, "fill_s": None,
                      "shortfall": None if exec_px is None else side * (exec_px - mid0)}
    return out


# ----------------------------------------------------------------- giao dịch ----
def trade_stats(b: DayBook, segments: Sequence[Segment], tick: float) -> dict:
    """Giao dịch trong phiên liên tục có báo giá hợp lệ đi kèm: nửa spread hiệu dụng, tỉ lệ khớp ở ask / bid."""
    inside = np.zeros(len(b.trade_ns), dtype=bool)
    for s, e in segments:
        inside |= (b.trade_ns >= _ns(s)) & (b.trade_ns < _ns(e))
    ok = inside & ~np.isnan(b.trade_bid) & ~np.isnan(b.trade_ask) & (b.trade_ask - b.trade_bid > tick / 2)
    n = int(ok.sum())
    if n == 0:
        return {"n": 0, "mean_eff_half_spread": None, "qty_weighted_eff_half_spread": None,
                "frac_at_ask": None, "frac_at_bid": None}
    px, bid, ask, qty = b.trade_last[ok], b.trade_bid[ok], b.trade_ask[ok], b.trade_qty[ok].astype(float)
    eff = np.abs(px - (bid + ask) / 2)
    eps = tick / 1000
    return {"n": n, "mean_eff_half_spread": float(eff.mean()),
            "qty_weighted_eff_half_spread": float((eff * qty).sum() / qty.sum()) if qty.sum() > 0 else None,
            "frac_at_ask": float((px >= ask - eps).mean()), "frac_at_bid": float((px <= bid + eps).mean())}


# ------------------------------------------------------------------ một ngày ----
def audit_day(b: DayBook, day: date, segments: Sequence[Segment], cfg: AuditConfig) -> dict:
    mkt: list[float] = []
    lim = {T: {"values": [], "fills": []} for T in cfg.timeouts}
    for t in decision_times(day, segments, cfg):
        t_ns = _ns(t)
        age = b.quote_age_s(t_ns)
        if age is None or age > cfg.max_quote_age_s:
            continue
        for side in (1, -1):
            m = market_now(b, t_ns, side, cfg)
            r = limit_then_market(b, t_ns, side, cfg)
            if m is None or r is None or any(r[T]["shortfall"] is None for T in cfg.timeouts):
                continue
            mkt.append(m)
            for T in cfg.timeouts:
                lim[T]["values"].append(r[T]["shortfall"])
                lim[T]["fills"].append(r[T]["filled"])
    for T, d in lim.items():
        d["mean"] = float(np.mean(d["values"])) if d["values"] else None
        d["fill_rate"] = float(np.mean(d["fills"])) if d["fills"] else None
    return {"day": day, "n_decisions": len(mkt),
            "market": {"values": mkt, "mean": float(np.mean(mkt)) if mkt else None},
            "limit": lim, "spread": spread_profile(b, segments, cfg.tick_size),
            "trades": trade_stats(b, segments, cfg.tick_size)}


# -------------------------------------------------------------------- gộp ----
def _pool(values_by_day: list[list[float]]) -> dict:
    allv = [v for vs in values_by_day for v in vs]
    means = [float(np.mean(vs)) for vs in values_by_day if vs]
    se = float(np.std(means, ddof=1) / math.sqrt(len(means))) if len(means) >= 2 else None
    return {"mean": float(np.mean(allv)) if allv else None, "se": se, "n": len(allv)}


def summarize(days: Sequence[dict], fees_round_trip: float, assumed_side: float) -> dict:
    """Gộp nhiều ngày: trung bình trên mọi quyết định; sai số chuẩn tính trên trung bình NGÀY (các phút trong ngày tương quan)."""
    days = [d for d in days if d["n_decisions"] > 0 or d["spread"]["seconds"] > 0]
    timeouts = sorted({T for d in days for T in d["limit"]})
    out = {"n_days": len(days), "days": [d["day"] for d in days], "fees_round_trip": fees_round_trip,
           "assumed_side": assumed_side, "n_decisions": sum(d["n_decisions"] for d in days),
           "market": _pool([d["market"]["values"] for d in days]), "limit": {}}
    for T in timeouts:
        pooled = _pool([d["limit"][T]["values"] for d in days if T in d["limit"]])
        fills = [f for d in days if T in d["limit"] for f in d["limit"][T]["fills"]]
        pooled["fill_rate"] = float(np.mean(fills)) if fills else None
        out["limit"][T] = pooled
    secs = sum(d["spread"]["seconds"] for d in days)
    frac: dict[int, float] = {}
    for d in days:
        for k, v in d["spread"]["frac"].items():
            frac[k] = frac.get(k, 0.0) + v * d["spread"]["seconds"]
    out["spread"] = {"seconds": secs, "frac": {k: v / secs for k, v in sorted(frac.items())} if secs else {},
                     "mean_half_spread": (sum(d["spread"]["mean_half_spread"] * d["spread"]["seconds"]
                                              for d in days if d["spread"]["mean_half_spread"] is not None) / secs)
                     if secs else None,
                     "min_depth_median": (float(np.median([d["spread"]["min_depth_median"] for d in days
                                                           if d["spread"]["min_depth_median"] is not None]))
                                          if any(d["spread"]["min_depth_median"] is not None for d in days) else None)}
    if any(d["spread"].get("buckets") for d in days):
        rows = []
        for i, bk in enumerate(days[0]["spread"]["buckets"]):
            s = sum(d["spread"]["buckets"][i]["seconds"] for d in days)
            mh = (sum(d["spread"]["buckets"][i]["mean_half_spread"] * d["spread"]["buckets"][i]["seconds"]
                      for d in days if d["spread"]["buckets"][i]["mean_half_spread"] is not None) / s) if s else None
            one = (sum(d["spread"]["buckets"][i]["frac"].get(1, 0.0) * d["spread"]["buckets"][i]["seconds"]
                       for d in days) / s) if s else None
            rows.append({"from": bk["from"], "until": bk["until"], "mean_half_spread": mh, "frac_one_tick": one})
        out["spread"]["buckets"] = rows
    n_tr = sum(d["trades"]["n"] for d in days)
    def tw(key):
        vals = [(d["trades"][key], d["trades"]["n"]) for d in days if d["trades"][key] is not None]
        w = sum(n for _, n in vals)
        return sum(v * n for v, n in vals) / w if w else None
    out["trades"] = {"n": n_tr, "mean_eff_half_spread": tw("mean_eff_half_spread"),
                     "qty_weighted_eff_half_spread": tw("qty_weighted_eff_half_spread"),
                     "frac_at_ask": tw("frac_at_ask"), "frac_at_bid": tw("frac_at_bid")}
    policies = {"market": out["market"]["mean"]}
    policies.update({f"limit_{T}": out["limit"][T]["mean"] for T in timeouts})
    known = {k: v for k, v in policies.items() if v is not None}
    out["best_policy"] = min(known, key=known.get) if known else None
    out["round_trip"] = {"assumed": fees_round_trip + 2 * assumed_side,
                         **{k: fees_round_trip + 2 * v for k, v in known.items()}}
    return out


# ------------------------------------------------------------------ hiển thị ----
def _vn(x: Optional[float], digits: int = 2) -> str:
    return "—" if x is None else f"{x:.{digits}f}".replace(".", ",")


def _pm(p: dict) -> str:
    return f"{_vn(p['mean'], 3)}" + ("" if p.get("se") is None else f" ± {_vn(2 * p['se'], 3)}")


def render(s: dict) -> str:
    d0 = s["days"][0] if s["days"] else None
    d1 = s["days"][-1] if s["days"] else None
    lines = [f"KIỂM TOÁN CHI PHÍ KHỚP LỆNH — {s['n_days']} ngày ({d0} → {d1}), {s['n_decisions']} quyết định (đầu phút × 2 chiều)",
             f"Giả định backtest mỗi chiều: {_vn(s['assumed_side'])} điểm ⇒ mỗi lượt (gồm phí + thuế "
             f"{_vn(s['fees_round_trip'], 3)}): {_vn(s['round_trip']['assumed'])} điểm"]
    sp = s["spread"]
    if sp["seconds"]:
        fr = " · ".join(f"{'≥3' if k >= 3 else k} bước {v:.1%}".replace(".", ",") for k, v in sp["frac"].items())
        lines.append(f"Spread theo thời gian ({_vn(sp['seconds'] / 3600, 1)} giờ phiên liên tục): {fr}; nửa spread TB "
                     f"{_vn(sp['mean_half_spread'], 3)} điểm; độ sâu nhỏ hơn hai phía (trung vị) {_vn(sp['min_depth_median'], 0)} HĐ")
        for bk in sp.get("buckets", []):
            if bk["mean_half_spread"] is not None:
                lines.append(f"  {bk['from']:%H:%M}–{bk['until']:%H:%M}: nửa spread {_vn(bk['mean_half_spread'], 3)}; "
                             f"spread 1 bước {_vn(100 * bk['frac_one_tick'], 1)}% thời gian")
    tr = s["trades"]
    if tr["n"]:
        lines.append(f"Giao dịch thật: {tr['n']} lần khớp; nửa spread hiệu dụng {_vn(tr['mean_eff_half_spread'], 3)} "
                     f"(theo KL {_vn(tr['qty_weighted_eff_half_spread'], 3)}); khớp ở giá bán "
                     f"{_vn(100 * tr['frac_at_ask'], 1)}%, ở giá mua {_vn(100 * tr['frac_at_bid'], 1)}%")
    lines.append("Thiếu hụt mỗi chiều so với mid lúc ra quyết định (điểm; ± = 2 sai số chuẩn theo ngày):")
    lines.append(f"  lệnh thị trường: {_pm(s['market'])} ⇒ mỗi lượt {_vn(s['round_trip'].get('market'))}")
    for T, p in s["limit"].items():
        lines.append(f"  giới hạn ở giá tốt nhất, chờ {T} s rồi đuổi: {_pm(p)}; khớp {_vn(100 * p['fill_rate'], 1)}% ⇒ "
                     f"mỗi lượt {_vn(s['round_trip'].get(f'limit_{T}'))}")
    best = s["best_policy"]
    if best is not None:
        name = "lệnh thị trường" if best == "market" else f"giới hạn chờ {best.split('_')[1]} s rồi đuổi"
        lines.append(f"Rẻ nhất: {name} — "
                     f"mỗi lượt {_vn(s['round_trip'][best])} điểm so với giả định {_vn(s['round_trip']['assumed'])}")
    lines.append("(Quyết định ở đầu phút, không gắn tín hiệu: với tín hiệu theo đà, lệnh giới hạn khớp ít hơn và đuổi đắt hơn "
                 "⇒ con số của lệnh giới hạn là cận dưới. Hàng chờ không tính huỷ phía trước ⇒ bi quan.)")
    return "\n".join(lines)


# ----------------------------------------------------------------------- CLI ----
def main(argv: Optional[Sequence[str]] = None) -> int:
    from brokers.paper import PaperConfig
    from core.calendar import TradingCalendar
    from core.clock import FakeClock, MarketClock, SessionSchedule
    from core.settings import MissingSetting, load_settings
    from backtest.signal_study import tradable_window

    ap = argparse.ArgumentParser(description="Kiểm toán chi phí khớp lệnh từ dữ liệu recorder")
    ap.add_argument("--raw", help="thư mục data/raw (mặc định: paths.data_dir/raw)")
    ap.add_argument("--from", dest="date_from", type=date.fromisoformat, required=True)
    ap.add_argument("--to", dest="date_to", type=date.fromisoformat, required=True)
    ap.add_argument("--source", help="nguồn feed (mặc định feeds.primary hoặc dnse)")
    ap.add_argument("--timeouts", type=int, nargs="+", default=[10, 30, 60], help="giây chờ lệnh giới hạn")
    ap.add_argument("--latency-ms", type=float, default=None, help="mặc định paper.latency_ms")
    args = ap.parse_args(argv)
    try:
        s = load_settings()
        raw = Path(args.raw) if args.raw else s.path("paths.data_dir") / "raw"
        source = args.source or s.get("feeds.primary", "dnse") or "dnse"
        paper = PaperConfig.from_settings(s)
        tick = float(s.require("contract.tick_size_points"))
        mult = float(s.require("contract.multiplier_vnd_per_point"))
        spread_ticks = int(s.require("backtest.synthetic_spread_ticks"))
        half = 0 if spread_ticks <= 0 else max(1, -(-spread_ticks // 2))
        assumed_side = (paper.slippage_ticks + half) * tick
        fees_rt = 2 * paper.fee_vnd_per_contract / mult
        entry_from, entry_until, _ = tradable_window(s)
        latency = (args.latency_ms if args.latency_ms is not None else paper.latency_ms) / 1000.0
        cfg = AuditConfig(tick_size=tick, latency_s=latency, timeouts=tuple(sorted(set(args.timeouts))),
                          entry_from=entry_from, entry_until=entry_until)
        sched = SessionSchedule.from_mapping(s.get("session", {}))
        cal = TradingCalendar.default()
        mclock = MarketClock.from_settings(s, clock=FakeClock(datetime(2000, 1, 3)), calendar=cal)
    except (ValueError, MissingSetting) as exc:
        print(f"❌ {exc}")
        return 2
    results = []
    d = args.date_from
    while d <= args.date_to:
        day_dir = raw / d.isoformat()
        if cal.is_trading_day(d) and day_dir.is_dir():
            sym = mclock.trading_contract(d)
            df = load_day(day_dir, sym, source)
            if not df.empty:
                segs = [(datetime.combine(d, sched.continuous_am_start), datetime.combine(d, sched.lunch_start)),
                        (datetime.combine(d, sched.continuous_pm_start), datetime.combine(d, sched.atc_start))]
                r = audit_day(DayBook.from_frame(df, tick), d, segs, cfg)
                results.append(r)
                lim = " ".join(f"{T}s {_vn(r['limit'][T]['mean'], 3)} ({_vn(100 * (r['limit'][T]['fill_rate'] or 0), 0)}%)"
                               for T in cfg.timeouts)
                print(f"{d} {sym}: {len(df)} tick, {r['n_decisions']} quyết định; thị trường {_vn(r['market']['mean'], 3)}; "
                      f"giới hạn {lim}")
        d += timedelta(days=1)
    if not results:
        print(f"❌ không có dữ liệu recorder nào ({source}) trong {raw} từ {args.date_from} tới {args.date_to}")
        return 2
    print(render(summarize(results, fees_rt, assumed_side)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
