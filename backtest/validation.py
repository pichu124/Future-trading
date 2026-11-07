"""Kiểm định thống kê kết quả backtest — "chống tự lừa mình" (CLAUDE.md mục 6).

Port từ `vnstock-dashboard/vnstock_strategy.py` (monte_carlo_pvalue,
bootstrap_sharpe_ci, walk_forward_consistency, validate_backtest_quality,
apply_selection_penalty, _bar_high_first). Khác bản gốc:
- thuần, không trạng thái, KHÔNG sửa dict đầu vào (trả bản mới);
- ngưỡng gom vào `Thresholds` để chỉnh có chủ đích, không rải hằng số;
- walk-forward xếp lệnh theo thời điểm vào lệnh (`entry_ts`), không theo chỉ số cửa sổ;
- bỏ cổng "tầm với chốt lời" của chiến lược cổ phiếu nhiều phiên (không áp cho intraday).

Đơn vị P&L: điểm hoặc VNĐ sau phí — miễn nhất quán trong một lần gọi.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class Thresholds:
    mc_sims: int = 1000
    p_value_max: float = 0.10          # p ≥ ngưỡng ⇒ không hơn ăn may
    boot_sims: int = 1000
    boot_ci: float = 0.90
    wf_folds: int = 4
    wf_min_positive_frac: float = 0.5  # ≥ nửa số đoạn thời gian phải dương
    min_trades: int = 30               # dưới mức này kết luận "chưa đủ mẫu", không bao giờ passed


DEFAULT = Thresholds()


def _sharpe(pnls: np.ndarray) -> Optional[float]:
    if len(pnls) < 2:
        return None
    sd = pnls.std()
    return None if sd == 0 else float(pnls.mean() / sd)


def monte_carlo_pvalue(pnls: Sequence[float], n_sim: int = DEFAULT.mc_sims,
                       seed: int = 42) -> Optional[float]:
    """p-value sign-flip cho Sharpe per-trade > 0.

    H0: không có edge — P&L mỗi lệnh đối xứng quanh 0. Lật dấu ngẫu nhiên từng lệnh
    `n_sim` lần; p = tỷ lệ mô phỏng có Sharpe ≥ thực (+1 hiệu chỉnh để p không bằng 0).
    None khi < 3 lệnh hoặc độ lệch chuẩn bằng 0.
    """
    arr = np.asarray(pnls, dtype=float)
    if len(arr) < 3 or arr.std() == 0:
        return None
    actual = arr.mean() / arr.std()
    rng = np.random.default_rng(seed)
    signs = rng.choice((-1.0, 1.0), size=(n_sim, len(arr)))
    sims = signs * np.abs(arr)
    stds = sims.std(axis=1)
    stds[stds == 0] = np.inf
    sim_sharpes = sims.mean(axis=1) / stds
    return float((np.sum(sim_sharpes >= actual) + 1) / (n_sim + 1))


def bootstrap_sharpe_ci(pnls: Sequence[float], n_boot: int = DEFAULT.boot_sims,
                        seed: int = 42, ci: float = DEFAULT.boot_ci) -> Optional[tuple[float, float]]:
    """CI percentile cho Sharpe per-trade (lấy mẫu có hoàn lại). lo > 0 ⇒ edge không dựa vào vài lệnh may."""
    arr = np.asarray(pnls, dtype=float)
    if len(arr) < 3:
        return None
    rng = np.random.default_rng(seed)
    samples = arr[rng.integers(0, len(arr), size=(n_boot, len(arr)))]
    stds = samples.std(axis=1)
    stds[stds == 0] = np.inf
    sharpes = samples.mean(axis=1) / stds
    alpha = (1 - ci) / 2
    return float(np.quantile(sharpes, alpha)), float(np.quantile(sharpes, 1 - alpha))


def walk_forward_consistency(trades: Sequence[Mapping], n_folds: int = DEFAULT.wf_folds) -> Optional[dict]:
    """Chia lệnh THEO THỜI GIAN thành n đoạn: hiệu năng đều hay dồn vào một giai đoạn?

    Mỗi trade cần `pnl` và `entry_ts` (so sánh được). None khi ít lệnh hơn số đoạn.
    """
    if len(trades) < n_folds:
        return None
    ordered = sorted(trades, key=lambda t: t["entry_ts"])
    pnls = np.asarray([t["pnl"] for t in ordered], dtype=float)
    fold_pnls = [float(f.sum()) for f in np.array_split(pnls, n_folds)]
    positive = sum(1 for f in fold_pnls if f > 0)
    return {"n_folds": n_folds,
            "positive_frac": round(positive / n_folds, 2),
            "fold_pnls": [round(f, 4) for f in fold_pnls]}


def validate_trades(trades: Sequence[Mapping], th: Thresholds = DEFAULT) -> dict:
    """Chạy cả ba kiểm định → dict báo cáo.

    `passed` = đủ `min_trades` VÀ p-value < ngưỡng VÀ ≥ `wf_min_positive_frac` đoạn dương.
    CI chỉ để tham khảo (mẫu nhỏ cho CI rộng). Luôn trả dict, kể cả khi 0 lệnh.
    """
    pnls = np.asarray([t["pnl"] for t in trades], dtype=float)
    p = monte_carlo_pvalue(pnls, th.mc_sims)
    ci = bootstrap_sharpe_ci(pnls, th.boot_sims, ci=th.boot_ci)
    wf = walk_forward_consistency(trades, th.wf_folds)
    reasons: list[str] = []
    if len(pnls) < th.min_trades:
        reasons.append(f"chỉ {len(pnls)} lệnh < {th.min_trades} — chưa đủ mẫu để kết luận")
    if p is None:
        if len(pnls) >= 3:
            reasons.append("không tính được p-value (P&L không biến thiên)")
    elif p >= th.p_value_max:
        reasons.append(f"p-value {p:.3f} ≥ {th.p_value_max} — Sharpe không hơn ăn may")
    if wf is not None and wf["positive_frac"] < th.wf_min_positive_frac:
        reasons.append(f"chỉ {wf['positive_frac']:.0%} đoạn thời gian dương — hiệu năng dồn cục, không bền")
    return {
        "n_trades": int(len(pnls)),
        "sharpe_per_trade": _sharpe(pnls),
        "p_value_sharpe": p,
        "sharpe_ci": list(ci) if ci else None,
        "walk_forward": wf,
        "passed": not reasons,
        "reasons": reasons,
    }


def apply_selection_penalty(report: Mapping, n_trials: int, th: Thresholds = DEFAULT) -> dict:
    """Hiệu chỉnh đa-thử-nghiệm kiểu Bonferroni cho "chọn cấu hình tốt nhất trong N".

    p_adj = min(1, p × n_trials). Chỉ SIẾT, không nới: p_adj thấp không lật `passed` thành True.
    Trả dict MỚI; `n_trials ≤ 1` hoặc thiếu p ⇒ bản sao không đổi.
    """
    out = dict(report)
    out["reasons"] = list(report.get("reasons", []))
    p = report.get("p_value_sharpe")
    if n_trials <= 1 or p is None:
        return out
    p_adj = min(1.0, p * n_trials)
    out["n_trials"] = n_trials
    out["p_value_adj"] = round(p_adj, 6)
    if p_adj >= th.p_value_max and out.get("passed"):
        out["passed"] = False
        out["reasons"].append(
            f"p hiệu chỉnh {p_adj:.3f} (p {p:.3f} × {n_trials} cấu hình) ≥ {th.p_value_max} "
            "— thắng cuộc thi nội bộ chưa đủ bằng chứng edge")
    return out


def bar_high_first(open_: float, high: float, low: float) -> Optional[bool]:
    """Bar chứa cả TP lẫn SL: cực trị GẦN giá mở hơn coi như chạm trước.

    True = high trước, False = low trước, None = không quyết được (open hỏng hoặc cách đều)
    ⇒ caller phải chọn kịch bản bi quan (SL thắng).
    """
    try:
        o, hi, lo = float(open_), float(high), float(low)
    except (TypeError, ValueError):
        return None
    if any(math.isnan(x) for x in (o, hi, lo)) or o <= 0:
        return None
    d_hi, d_lo = abs(hi - o), abs(lo - o)
    if d_hi == d_lo:
        return None
    return d_hi < d_lo
