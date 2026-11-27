"""Đo khả năng dự báo của MỘT tín hiệu trên toàn bộ nến — giai đoạn TRƯỚC khi dựng chiến lược.

    python -m backtest.signal_study --bars data/history/ohlc1m --signal backtest.signal_study:placebo_signal \
        --horizon 5 --expected-sign 1 --until 2026-08-31

Vì sao: một chiến lược gộp tín hiệu + stop + chốt lời + giới hạn lệnh, kiểm trên vài chục lệnh ⇒ lực thống kê thấp
(`backtest/power.py`) và trượt mà không biết trượt ở đâu. Ở đây chỉ hỏi một câu, trên mọi nến: giá trị tín hiệu lúc
nến đóng có xếp hạng được lợi suất `horizon` nến kế tiếp không, và lợi thế thô đó có lớn hơn chi phí một lượt không.

- Tín hiệu: hàm thuần `(bars_today, history) -> số | None` — đúng hai trường của `StrategyContext`. `SignalProbe` là một
  "chiến lược" không bao giờ ra lệnh, chạy trong `BacktestEngine` ⇒ tín hiệu nhận ĐÚNG dữ liệu chiến lược thật sẽ nhận
  (hợp đồng giao dịch, chuỗi back-adjust qua roll, chỉ nến đã đóng). Hàm cần lịch sử thì gắn `fn.history_days = N`.
- Quan sát: tín hiệu ở nến i ⇒ vào ở giá MỞ nến i+1, ra ở giá ĐÓNG nến i+h cùng ngày (h tính theo số nến; phút thiếu và
  giờ nghỉ trưa không được bù). Không bao giờ dùng giá đóng của chính nến tín hiệu (CLAUDE.md mục 6).
- Thống kê theo NGÀY: các quan sát trong một ngày chồng lấn nhau, không độc lập ⇒ mỗi ngày một "điểm ngày" = trung
  bình (tín hiệu × lợi suất kỳ sau) chia hằng số chuẩn hoá của cả mẫu, rồi kiểm định sign-flip (`monte_carlo_pvalue`)
  trên chuỗi điểm ngày, một phía theo CHIỀU đã đăng ký, nhân `n_trials`. KHÔNG xếp hạng hay trừ trung bình trong ngày:
  làm vậy dùng thông tin tương lai của chính ngày đó và tạo "đảo chiều" giả với tín hiệu chậm (đã gặp 27/11/2026 khi
  thử trên bước ngẫu nhiên — xem research/log.md).
- Độ lớn: trung bình trên mọi nến |tín hiệu| ≥ ngưỡng đăng ký của lợi suất kỳ sau đã lấy dấu theo tín hiệu,
  sau khi trừ trôi giá trung bình của CÁC NGÀY KHÁC, so với chi phí một lượt của engine. Đây là ước lượng SÀNG LỌC (bỏ qua luật một vị thế, stop, lệnh/ngày) — tín hiệu
  qua được vẫn phải qua cổng lực thống kê và kiểm định chiến lược như cũ.

Mỗi (tín hiệu, kỳ, ngưỡng) đo trên dữ liệu thật = MỘT lần thử: đăng ký trước, thêm dòng bảng, tăng `n_trials`.
"""
from __future__ import annotations

import argparse
import importlib
import math
import zlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable, Optional, Sequence

import numpy as np

from backtest.power import PowerRules, reachable
from backtest.validation import DEFAULT, monte_carlo_pvalue, walk_forward_consistency
from core.models import Bar

CHUA_DU = "CHƯA ĐỦ DỮ LIỆU — chưa kết luận được"
KHONG = "KHÔNG CÓ TÍN HIỆU — dừng, không dựng chiến lược"
NHO = "CÓ TÍN HIỆU nhưng KHÔNG ĐỦ TRẢ PHÍ — không dựng chiến lược"
DU_LON = "CÓ TÍN HIỆU, ĐỦ LỚN — sang cổng lực thống kê rồi đăng ký chiến lược"

SignalFn = Callable[[Sequence[Bar], Sequence[Bar]], Optional[float]]


@dataclass(frozen=True)
class StudyRules:
    min_days: int = DEFAULT.min_trades                  # số ngày tối thiểu (có IC / có tín hiệu vượt ngưỡng)
    min_obs_per_day: int = 20                           # ngày ít quan sát hơn ⇒ không tính IC
    p_value_max: float = DEFAULT.p_value_max            # p × n_trials phải < mức này
    mc_sims: int = DEFAULT.mc_sims
    wf_folds: int = DEFAULT.wf_folds
    wf_min_positive_frac: float = DEFAULT.wf_min_positive_frac
    quantiles: int = 5


RULES = StudyRules()


@dataclass(frozen=True)
class Observation:
    day: date
    ts: datetime          # giờ mở của nến SINH tín hiệu
    signal: float
    fwd: float            # điểm, trước phí: đóng nến i+h − mở nến i+1


class SignalProbe:
    """Chiến lược không ra lệnh: ghi (nến, giá trị tín hiệu) tại mỗi nến đã đóng mà engine đưa tới."""

    def __init__(self, fn: SignalFn):
        self.fn = fn
        self.history_days = int(getattr(fn, "history_days", 0) or 0)
        self.rows: list[tuple[Bar, Optional[float]]] = []

    def on_bar(self, bar: Bar, ctx) -> tuple:
        self.rows.append((bar, self.fn(ctx.bars_today, ctx.history)))
        return ()


def placebo_signal(bars_today: Sequence[Bar], history: Sequence[Bar]) -> float:
    """Giả dược: số giả ngẫu nhiên trong [−1, 1] chỉ phụ thuộc giờ mở nến. Dùng để thử đường ống — phải ra KHÔNG CÓ TÍN HIỆU."""
    return zlib.crc32(bars_today[-1].start.isoformat().encode()) / 0xFFFFFFFF * 2.0 - 1.0


# ------------------------------------------------------------ quan sát ----
def forward_observations(rows: Iterable[tuple[Bar, Optional[float]]], horizon: int,
                         entry_from: Optional[time] = None, entry_until: Optional[time] = None,
                         exit_by: Optional[time] = None, skip_days: Iterable[date] = ()) -> list[Observation]:
    """(nến, tín hiệu) theo thứ tự thời gian → quan sát (tín hiệu, lợi suất kỳ sau), từng ngày riêng.

    Cửa sổ giao dịch theo luật rủi ro: nến VÀO mở trong [entry_from, entry_until); nến RA đóng không muộn hơn `exit_by`.
    """
    if horizon < 1:
        raise ValueError(f"horizon phải ≥ 1 nến (nhận {horizon})")
    skip = set(skip_days)
    by_day: dict[date, list[tuple[Bar, Optional[float]]]] = {}
    for b, v in rows:
        by_day.setdefault(b.start.date(), []).append((b, v))
    out: list[Observation] = []
    for day in sorted(by_day):
        seq = by_day[day]
        if len({b.symbol for b, _ in seq}) > 1:
            raise ValueError(f"{day}: nhiều hợp đồng trong cùng một ngày — chỉ đưa nến của hợp đồng giao dịch")
        if day in skip:
            continue
        for i in range(len(seq) - horizon):
            v = seq[i][1]
            if v is None or not math.isfinite(v):
                continue
            entry, exit_ = seq[i + 1][0], seq[i + horizon][0]
            t_in = entry.start.time()
            if entry_from is not None and t_in < entry_from:
                continue
            if entry_until is not None and t_in >= entry_until:
                continue
            if exit_by is not None and (exit_.start + timedelta(seconds=exit_.interval_s)).time() > exit_by:
                continue
            out.append(Observation(day, seq[i][0].start, float(v), exit_.close - entry.open))
    return out


def _by_day(obs: Sequence[Observation]) -> dict[date, list[Observation]]:
    out: dict[date, list[Observation]] = {}
    for o in obs:
        out.setdefault(o.day, []).append(o)
    return out


def drift_adjusted(obs: Sequence[Observation]) -> tuple[list[Observation], Optional[float]]:
    """Trừ khỏi mỗi quan sát lợi suất kỳ sau trung bình của CÁC NGÀY KHÁC ⇒ (quan sát mới, trôi giá trung bình cả mẫu).

    Tín hiệu lệch hẳn một chiều sẽ "ăn" theo trôi giá chung của thị trường; phần đó không phải khả năng dự báo. KHÔNG
    trừ trung bình của chính ngày đó: trung bình ngày chứa lợi suất TƯƠNG LAI của từng quan sát, trừ nó đi tạo tương
    quan âm giả giữa tín hiệu chậm (mức giá, độ lệch…) và lợi suất kỳ sau — trên bước ngẫu nhiên cũng ra "đảo chiều".
    """
    by = _by_day(obs)
    if not by:
        return [], None
    total, n = sum(o.fwd for o in obs), len(obs)
    out: list[Observation] = []
    for _day, items in sorted(by.items()):
        k = len(items)
        other = (total - sum(o.fwd for o in items)) / (n - k) if n > k else 0.0
        out += [Observation(o.day, o.ts, o.signal, o.fwd - other) for o in items]
    return out, total / n


def daily_scores(obs: Sequence[Observation], min_obs: int = RULES.min_obs_per_day) -> list[tuple[date, float]]:
    """Điểm ngày = trung bình trong ngày của (tín hiệu × lợi suất kỳ sau) / (RMS tín hiệu × RMS lợi suất của CẢ mẫu).

    Trung bình các điểm ngày ≈ tương quan gộp không định tâm giữa tín hiệu và lợi suất kỳ sau. Cố ý KHÔNG xếp hạng hay
    trừ trung bình TRONG ngày (cả hai đều dùng thông tin tương lai của ngày đó ⇒ lệch âm với tín hiệu chậm). Với tín
    hiệu chỉ phụ thuộc quá khứ và giá là bước ngẫu nhiên, kỳ vọng của điểm ngày đúng bằng 0. Ngày < `min_obs` quan sát ⇒ bỏ.
    """
    if not obs:
        return []
    scale = math.sqrt(float(np.mean([o.signal ** 2 for o in obs])) * float(np.mean([o.fwd ** 2 for o in obs])))
    if scale == 0:
        return []
    out = []
    for day, items in sorted(_by_day(obs).items()):
        if len(items) >= min_obs:
            out.append((day, float(np.mean([o.signal * o.fwd for o in items])) / scale))
    return out


def triggered_edge(obs: Sequence[Observation], threshold: float, expected_sign: int) -> list[tuple[date, float, int]]:
    """Mỗi ngày: trung bình `expected_sign × dấu(tín hiệu) × lợi suất kỳ sau` tại các nến |tín hiệu| ≥ ngưỡng, và số nến đó.

    Tín hiệu đúng bằng 0 không có chiều ⇒ không tính. Ngày không có nến nào vượt ngưỡng ⇒ không có dòng.
    """
    out = []
    for day, items in sorted(_by_day(obs).items()):
        vals = [expected_sign * math.copysign(1.0, o.signal) * o.fwd
                for o in items if o.signal != 0 and abs(o.signal) >= threshold]
        if vals:
            out.append((day, float(np.mean(vals)), len(vals)))
    return out


def signal_quantile(obs: Sequence[Observation], q: float) -> Optional[float]:
    """Phân vị `q` của |tín hiệu| trên các quan sát — KHÔNG dùng lợi suất kỳ sau. Dùng để chốt ngưỡng trên IS."""
    if not 0 < q < 1:
        raise ValueError(f"phân vị phải trong (0, 1) (nhận {q})")
    if not obs:
        return None
    return float(np.quantile(np.abs([o.signal for o in obs]), q))


def quantile_table(obs: Sequence[Observation], q: int = RULES.quantiles) -> list[dict]:
    """Mô tả: chia quan sát thành `q` nhóm theo tín hiệu (ranh giới tính trên CẢ mẫu — chỉ để xem hình dạng)."""
    if not obs:
        return []
    ordered = sorted(obs, key=lambda o: o.signal)
    out = []
    for part in np.array_split(np.arange(len(ordered)), q):
        if len(part) == 0:
            continue
        chunk = [ordered[i] for i in part]
        out.append({"n": len(chunk), "mean_signal": float(np.mean([o.signal for o in chunk])),
                    "mean_fwd": float(np.mean([o.fwd for o in chunk]))})
    return out


# ------------------------------------------------------------ kết luận ----
def study(obs: Sequence[Observation], expected_sign: int, n_trials: int, cost_points: float,
          threshold: float = 0.0, rules: StudyRules = RULES) -> dict:
    """Báo cáo + kết luận cho một tín hiệu.

    `expected_sign`: +1 = tín hiệu cao ⇒ giá lên (thuận), −1 = tín hiệu cao ⇒ giá xuống (nghịch) — đăng ký TRƯỚC.
    CÓ TÍN HIỆU ⇔ đủ ngày VÀ điểm ngày trung bình (`daily_scores`) đúng chiều VÀ p sign-flip × n_trials < ngưỡng VÀ
    đủ số đoạn walk-forward đúng chiều. ĐỦ LỚN ⇔ thêm: đủ ngày có tín hiệu vượt ngưỡng VÀ lợi thế thô − chi phí > 0.
    """
    if expected_sign not in (1, -1):
        raise ValueError(f"expected_sign phải là +1 hoặc −1 (nhận {expected_sign})")
    if n_trials < 1:
        raise ValueError(f"n_trials phải ≥ 1 (nhận {n_trials})")
    if cost_points < 0 or threshold < 0:
        raise ValueError("cost_points và threshold phải ≥ 0")

    adj, drift = drift_adjusted(obs)
    ics = daily_scores(adj, rules.min_obs_per_day)
    signed = np.asarray([expected_sign * v for _, v in ics], dtype=float)
    obs_days = len(_by_day(obs))
    p = monte_carlo_pvalue(signed, rules.mc_sims) if len(signed) else None
    p_adj = None if p is None else min(1.0, p * n_trials)
    wf = walk_forward_consistency([{"pnl": float(v), "entry_ts": d} for (d, _), v in zip(ics, signed)],
                                  rules.wf_folds)
    trig = triggered_edge(adj, threshold, expected_sign)
    # trung bình trên MỌI nến vượt ngưỡng (ngày nhiều nến nặng hơn). Trung bình của trung bình ngày bị lệch âm: số nến
    # vượt ngưỡng trong ngày phụ thuộc diễn biến SAU đó (giá đi tiếp ⇒ nhiều nến, kết quả tốt bị pha loãng).
    n_trig = sum(n for _, _, n in trig)
    gross = (sum(m * n for _, m, n in trig) / n_trig) if n_trig else None
    sd = float(signed.std()) if len(signed) else None
    can_pass = reachable(n_trials, PowerRules(alpha=rules.p_value_max, mc_sims=rules.mc_sims))
    out = {
        "expected_sign": expected_sign, "n_trials": n_trials, "threshold": float(threshold),
        "cost_points": float(cost_points), "n_obs": len(obs), "obs_days": obs_days, "n_days": len(ics),
        "days_skipped": obs_days - len(ics),
        "mean_ic": float(np.mean([v for _, v in ics])) if ics else None,
        "mean_ic_signed": float(signed.mean()) if len(signed) else None,
        "ic_sd": sd,
        "ic_tstat": float(signed.mean() / sd * math.sqrt(len(signed))) if sd else None,
        "p": p, "p_adj": p_adj, "reachable": can_pass, "walk_forward": wf,
        "trigger_days": len(trig),
        "triggers_per_day": (n_trig / obs_days) if obs_days else 0.0,
        "drift": drift, "gross_edge": gross, "net_edge": None if gross is None else gross - cost_points,
        "quantiles": quantile_table(obs, rules.quantiles), "reasons": [],
    }
    reasons = out["reasons"]
    if len(ics) < rules.min_days:
        reasons.append(f"chỉ {len(ics)} ngày đủ quan sát < {rules.min_days}")
        out["verdict"] = CHUA_DU
        return out
    if not can_pass:
        reasons.append(f"kiểm định không thể đạt: p nhỏ nhất 1/{rules.mc_sims + 1} × {n_trials} cấu hình ≥ "
                       f"{_vn(rules.p_value_max, 2)} — phải tăng mc_sims trước")
    if out["mean_ic_signed"] <= 0:
        reasons.append(f"tương quan trung bình {_vn(out['mean_ic'], 4)} ngược chiều đã đăng ký")
    if p_adj is None or p_adj >= rules.p_value_max:
        reasons.append(f"p sign-flip {'—' if p is None else _vn(p, 3)} × {n_trials} cấu hình = "
                       f"{'—' if p_adj is None else _vn(p_adj, 3)} ≥ {_vn(rules.p_value_max, 2)} — không hơn ăn may")
    if wf is None or wf["positive_frac"] < rules.wf_min_positive_frac:
        frac = 0.0 if wf is None else wf["positive_frac"]
        reasons.append(f"chỉ {frac:.0%} đoạn thời gian đúng chiều (cần ≥ {rules.wf_min_positive_frac:.0%}) "
                       "— tín hiệu dồn vào một giai đoạn")
    if reasons:
        out["verdict"] = KHONG
        return out
    if len(trig) < rules.min_days:
        reasons.append(f"chỉ {len(trig)} ngày có tín hiệu vượt ngưỡng {_vn(threshold, 2)} < {rules.min_days} "
                       "— chưa đo được độ lớn")
        out["verdict"] = CHUA_DU
        return out
    if out["net_edge"] <= 0:
        reasons.append(f"lợi thế thô {_vn(gross, 2)} điểm ≤ chi phí {_vn(cost_points, 2)} điểm mỗi lượt")
        out["verdict"] = NHO
        return out
    out["verdict"] = DU_LON
    return out


# ------------------------------------------------- cấu hình, chạy engine ----
def round_trip_cost_points(settings) -> float:
    """Chi phí một lượt (điểm) theo đúng quy ước của engine: phí + thuế hai chiều, và mỗi chiều trả trượt giá +
    nửa spread giả (làm tròn lên, ít nhất 1 bước khi spread > 0 — như `backtest.engine.synthetic_ticks`)."""
    from brokers.paper import PaperConfig

    cfg = PaperConfig.from_settings(settings)
    spread = int(settings.require("backtest.synthetic_spread_ticks"))
    half = 0 if spread <= 0 else max(1, -(-spread // 2))
    multiplier = float(settings.require("contract.multiplier_vnd_per_point"))
    return 2 * cfg.fee_vnd_per_contract / multiplier + 2 * (cfg.slippage_ticks + half) * cfg.tick_size


def tradable_window(settings) -> tuple[time, time, time]:
    """(vào sớm nhất, vào muộn nhất — không gồm, ra muộn nhất) theo `session` và `risk` trong settings."""
    start = time.fromisoformat(str(settings.require("session.continuous_am_start")))
    wait = int(settings.require("risk.no_entry_open_minutes"))
    entry_from = (datetime.combine(date(2000, 1, 3), start) + timedelta(minutes=wait)).time()
    return (entry_from, time.fromisoformat(str(settings.require("risk.last_entry_time"))),
            time.fromisoformat(str(settings.require("risk.flatten_time"))))


def collect(settings, fn: SignalFn, bars: Sequence[Bar], horizon: int, calendar=None,
            date_from: Optional[date] = None) -> tuple[list[Observation], dict]:
    """Chạy tín hiệu qua engine (không lệnh nào được đặt) → quan sát trong cửa sổ giao dịch được phép.

    Bỏ ngày đáo hạn khi `risk.trade_on_expiry_day` = false. `date_from`: chỉ lấy quan sát từ ngày này (nến trước đó vẫn
    được nạp làm lịch sử).
    """
    from backtest.engine import BacktestEngine
    from core.clock import FakeClock, MarketClock

    probe = SignalProbe(fn)
    engine = BacktestEngine(settings, probe, calendar=calendar)
    res = engine.run_bars(bars)
    days = sorted({b.start.date() for b, _ in probe.rows})
    skip: set[date] = set()
    if not settings.require("risk.trade_on_expiry_day"):
        mclock = MarketClock.from_settings(settings, clock=FakeClock(datetime(2000, 1, 3)), calendar=engine.calendar)
        skip = {d for d in days if mclock.is_expiry_day(d)}
    entry_from, entry_until, exit_by = tradable_window(settings)
    obs = forward_observations(probe.rows, horizon, entry_from, entry_until, exit_by, skip)
    if date_from is not None:
        obs = [o for o in obs if o.day >= date_from]
    info = {"days": len(days), "n_bars": int(res.data_quality.get("n_bars", len(probe.rows))),
            "skipped_expiry_days": len(skip), "warnings": list(res.warnings),
            "first_day": days[0] if days else None, "last_day": days[-1] if days else None}
    return obs, info


# ------------------------------------------------------------------ hiển thị ----
def _vn(x: Optional[float], digits: int) -> str:
    return "—" if x is None else f"{x:.{digits}f}".replace(".", ",")


def render(r: dict, title: str = "", horizon: Optional[int] = None) -> str:
    head = f"{title} — " if title else ""
    kỳ = "" if horizon is None else f", kỳ {horizon} nến"
    chiều = "thuận (tín hiệu cao ⇒ giá lên)" if r["expected_sign"] > 0 else "nghịch (tín hiệu cao ⇒ giá xuống)"
    lines = [f"{head}đo tín hiệu{kỳ} · chiều đăng ký: {chiều}",
             f"Quan sát: {r['n_obs']} trong {r['obs_days']} ngày; dùng {r['n_days']} ngày "
             f"(bỏ {r['days_skipped']} ngày thiếu quan sát)",
             f"Tương quan tín hiệu–lợi suất kỳ sau (điểm ngày, không định tâm trong ngày): trung bình "
             f"{_vn(r['mean_ic'], 4)}, độ lệch chuẩn theo ngày {_vn(r['ic_sd'], 4)}, t = {_vn(r['ic_tstat'], 2)}",
             f"Kiểm định sign-flip trên điểm ngày (một phía): p {_vn(r['p'], 3)} × {r['n_trials']} cấu hình = "
             f"{_vn(r['p_adj'], 3)}"]
    wf = r["walk_forward"]
    if wf:
        lines.append(f"Walk-forward: {wf['positive_frac']:.0%} trong {wf['n_folds']} đoạn đúng chiều")
    lines.append(f"Độ lớn khi |tín hiệu| ≥ {_vn(r['threshold'], 4).rstrip('0').rstrip(',') or '0'}: "
                 f"{r['trigger_days']} ngày, {_vn(r['triggers_per_day'], 1)} nến/ngày; lợi thế thô "
                 f"{_vn(r['gross_edge'], 2)} điểm − chi phí {_vn(r['cost_points'], 2)} = {_vn(r['net_edge'], 2)} điểm mỗi lượt")
    lines.append(f"(Đã trừ trôi giá trung bình của các ngày khác; trôi giá cả mẫu {_vn(r['drift'], 2)} điểm mỗi kỳ.)")
    if r["quantiles"]:
        lines.append("Theo nhóm tín hiệu (mô tả; ranh giới tính trên cả mẫu): "
                     + " | ".join(f"{_vn(q['mean_signal'], 2)} ⇒ {_vn(q['mean_fwd'], 2)}" for q in r["quantiles"]))
    lines.append("(Ước lượng sàng lọc: bỏ qua luật một vị thế, stop và số lệnh/ngày.)")
    lines.append(f"KẾT LUẬN: {r['verdict']}")
    lines += [f"  - {x}" for x in r["reasons"]]
    return "\n".join(lines)


# ----------------------------------------------------------------------- CLI ----
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Đo khả năng dự báo của một tín hiệu trên toàn bộ nến")
    ap.add_argument("--bars", nargs="+", required=True, help="nến 1 phút Parquet, vd data/history/ohlc1m")
    ap.add_argument("--signal", required=True, help="module:hàm, chữ ký (bars_today, history) -> số | None")
    ap.add_argument("--horizon", type=int, required=True, help="số nến giữ lệnh (vào mở nến kế, ra đóng nến thứ h)")
    ap.add_argument("--expected-sign", type=int, choices=(1, -1), required=True,
                    help="+1: tín hiệu cao ⇒ giá lên; -1: tín hiệu cao ⇒ giá xuống (đăng ký trước)")
    ap.add_argument("--threshold", type=float, default=None, help="chỉ tính độ lớn khi |tín hiệu| ≥ ngưỡng này (mặc định 0)")
    ap.add_argument("--threshold-quantile", type=float, default=None,
                    help="đặt ngưỡng = phân vị này của |tín hiệu| trên các quan sát của lần chạy (chỉ dùng cho IS; "
                         "OOS phải truyền --threshold bằng con số IS đã in)")
    ap.add_argument("--from", dest="date_from", type=date.fromisoformat, help="chỉ lấy quan sát từ ngày này")
    ap.add_argument("--until", type=date.fromisoformat, help="bỏ MỌI nến sau ngày này (đừng chạm dữ liệu còn sạch)")
    ap.add_argument("--n-trials", type=int, default=None,
                    help="mặc định = số dòng cấu hình trong research/log.md (phép đo phải được đăng ký TRƯỚC khi chạy)")
    ap.add_argument("--title", default="")
    args = ap.parse_args(argv)

    from backtest.engine import load_bars_parquet
    from backtest.robustness import count_logged_trials, cut_until
    from core.settings import MissingSetting, load_settings

    try:
        mod, _, name = args.signal.partition(":")
        fn = getattr(importlib.import_module(mod), name)
        settings = load_settings()
        bars, skipped = load_bars_parquet(args.bars)
        bars = cut_until(bars, args.until)
        if args.threshold is not None and args.threshold_quantile is not None:
            raise ValueError("chỉ dùng một trong --threshold và --threshold-quantile")
        n_trials = args.n_trials if args.n_trials is not None else count_logged_trials()
        obs, info = collect(settings, fn, bars, args.horizon, date_from=args.date_from)
        threshold, note = args.threshold or 0.0, ""
        if args.threshold_quantile is not None:
            q = signal_quantile(obs, args.threshold_quantile)
            threshold = 0.0 if q is None else q
            note = (f"Ngưỡng = phân vị {args.threshold_quantile:.0%} của |tín hiệu| trên {len(obs)} quan sát "
                    f"(không dùng lợi suất). Đo OOS với: --threshold {threshold:.6f}")
        report = study(obs, args.expected_sign, n_trials, round_trip_cost_points(settings), threshold)
    except (ValueError, ImportError, AttributeError, MissingSetting) as exc:
        print(f"❌ {exc}")
        return 2
    print(f"(dữ liệu: {info['days']} ngày {info['first_day']} → {info['last_day']}, {info['n_bars']} nến; bỏ "
          f"{info['skipped_expiry_days']} ngày đáo hạn; {skipped} dòng nến hỏng; {len(info['warnings'])} cảnh báo engine)")
    if note:
        print(note)
    print(render(report, args.title or name, args.horizon))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
