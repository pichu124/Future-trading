"""Lực thống kê của kiểm định edge — chạy TRƯỚC khi đăng ký một ý tưởng (research/log.md, "Cổng lực thống kê").

    python -m backtest.power --edge 1.5 --n-trades 60 --sd 9
    python -m backtest.power --edge 1.5 --n-trades 60 --bars data/history/ohlc1m \
        --strategy strategy.late_momentum:LateMomentum --until 2026-11-06

Câu hỏi: nếu ý tưởng có lợi thế THẬT `edge` điểm/lệnh sau phí, thì với `n_trades` lệnh OOS gộp và `n_trials` cấu hình
đã thử, kiểm định quyết định của repo (`backtest.pooled`: p sign-flip × n_trials < `p_value_max`) bắt được nó với xác
suất bao nhiêu? Lực thấp ⇒ kết quả "không có edge" không nói lên điều gì, mà vẫn tốn một lần thử ⇒ KHÔNG đăng ký.

- `power_normal` / `min_detectable_edge` / `trades_needed`: công thức xấp xỉ chuẩn, một phía, α = p_value_max / n_trials.
- `simulated_power`: lấy mẫu lại từ P&L của một chiến lược tham chiếu ĐÃ TRỪ TRUNG BÌNH (chỉ mượn hình dạng và độ
  phân tán — chiến lược tham chiếu lãi hay lỗ không ảnh hưởng), cộng `edge`, rồi chạy ĐÚNG `monte_carlo_pvalue`.
- Lực tính ở đây là CẬN TRÊN của xác suất qua kiểm định OOS gộp: luật còn đòi mọi cửa sổ lãi và chịu được trượt giá căng.

Thuần numpy + thư viện chuẩn, không trạng thái. Đơn vị: điểm/lệnh SAU phí (`TradeRecord.pnl_points`).
"""
from __future__ import annotations

import argparse
import importlib
import math
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist
from typing import Optional, Sequence

import numpy as np

from backtest.validation import DEFAULT, monte_carlo_pvalue

DU_LUC = "ĐỦ LỰC — được đăng ký"
KHONG_DU = "KHÔNG ĐỦ LỰC — KHÔNG đăng ký (không tốn n_trials)"
TABLE_N = (30, 60, 100, 150, 300, 600, 1000)
_N = NormalDist()


@dataclass(frozen=True)
class PowerRules:
    alpha: float = DEFAULT.p_value_max     # p hiệu chỉnh phải < mức này (như backtest.pooled)
    mc_sims: int = DEFAULT.mc_sims         # số lần lật dấu của kiểm định ⇒ p nhỏ nhất = 1/(mc_sims+1)
    min_trades: int = DEFAULT.min_trades   # dưới mức này kiểm định gộp trả "CHƯA ĐỦ"; cũng là số lệnh tham chiếu tối thiểu
    min_power: float = 0.80                # lực tối thiểu để được đăng ký
    n_rep: int = 400                       # số lần lặp mô phỏng


RULES = PowerRules()


def alpha_per_test(n_trials: int, rules: PowerRules = RULES) -> float:
    """Mức ý nghĩa còn lại cho MỘT phép thử sau hiệu chỉnh Bonferroni (n_trials ≤ 1 ⇒ không hiệu chỉnh)."""
    return rules.alpha / max(1, n_trials)


def reachable(n_trials: int, rules: PowerRules = RULES) -> bool:
    """Kiểm định CÓ THỂ đạt không? p nhỏ nhất là 1/(mc_sims+1); cần p × n_trials < alpha."""
    return max(1, n_trials) / (rules.mc_sims + 1) < rules.alpha


def _check(sd: float, n_trades: int, alpha: float) -> None:
    if not sd > 0:
        raise ValueError(f"sd phải > 0 (nhận {sd})")
    if n_trades < 1:
        raise ValueError(f"n_trades phải ≥ 1 (nhận {n_trades})")
    if not 0 < alpha < 1:
        raise ValueError(f"alpha phải trong (0, 1) (nhận {alpha})")


def power_normal(edge: float, sd: float, n_trades: int, alpha: float) -> float:
    """Xác suất bác H0 (một phía, mức `alpha`) khi lợi thế thật là `edge`: Φ(edge/sd × √n − z₁₋α)."""
    _check(sd, n_trades, alpha)
    return _N.cdf(edge / sd * math.sqrt(n_trades) - _N.inv_cdf(1 - alpha))


def min_detectable_edge(sd: float, n_trades: int, alpha: float, power: float) -> float:
    """Lợi thế nhỏ nhất (điểm/lệnh) bắt được với xác suất `power`: (z₁₋α + z_power) × sd/√n."""
    _check(sd, n_trades, alpha)
    return (_N.inv_cdf(1 - alpha) + _N.inv_cdf(power)) * sd / math.sqrt(n_trades)


def trades_needed(edge: float, sd: float, alpha: float, power: float) -> Optional[int]:
    """Số lệnh ít nhất để đạt `power` với lợi thế `edge`. None khi edge ≤ 0 (không bao giờ đủ)."""
    _check(sd, 1, alpha)
    if edge <= 0:
        return None
    n = max(1, math.ceil(((_N.inv_cdf(1 - alpha) + _N.inv_cdf(power)) * sd / edge) ** 2))
    while n > 1 and power_normal(edge, sd, n - 1, alpha) >= power:      # chống sai số làm tròn ở ranh giới
        n -= 1
    while power_normal(edge, sd, n, alpha) < power:
        n += 1
    return n


def simulated_power(pnls: Sequence[float], edge: float, n_trades: int, n_trials: int = 1,
                    rules: PowerRules = RULES, seed: int = 42) -> Optional[float]:
    """Tỉ lệ lần mô phỏng mà kiểm định thật của repo đạt (p × n_trials < alpha).

    Mỗi lần: lấy `n_trades` lệnh có hoàn lại từ `pnls` đã trừ trung bình, cộng `edge`, chạy `monte_carlo_pvalue`.
    `edge = 0` ⇒ tỉ lệ báo động giả thực tế của kiểm định trên hình dạng P&L này. None khi < 3 lệnh hoặc không biến thiên.
    """
    arr = np.asarray(pnls, dtype=float)
    if len(arr) < 3 or arr.std() == 0:
        return None
    if n_trades < 1:
        raise ValueError(f"n_trades phải ≥ 1 (nhận {n_trades})")
    centred = arr - arr.mean()
    rng = np.random.default_rng(seed)
    k = max(1, n_trials)
    hits = 0
    for _ in range(rules.n_rep):
        sample = centred[rng.integers(0, len(centred), size=n_trades)] + edge
        p = monte_carlo_pvalue(sample, rules.mc_sims)
        if p is not None and min(1.0, p * k) < rules.alpha:
            hits += 1
    return hits / rules.n_rep


def power_gate(edge: float, n_trades: int, n_trials: int, sd: Optional[float] = None,
               pnls: Optional[Sequence[float]] = None, rules: PowerRules = RULES) -> dict:
    """Cổng đăng ký: ĐỦ LỰC ⇔ n_trades ≥ min_trades VÀ kiểm định đạt được VÀ lực ≥ min_power.

    `edge`: lợi thế giả định sau phí (điểm/lệnh) — phải ghi kèm lý do trong bản đăng ký. `sd`: độ lệch chuẩn P&L mỗi
    lệnh; hoặc đưa `pnls` (điểm/lệnh sau phí của một chiến lược tham chiếu, ≥ min_trades lệnh) để mô phỏng.
    """
    if not edge > 0:
        raise ValueError(f"edge giả định phải > 0 điểm/lệnh (nhận {edge})")
    if n_trials < 1:
        raise ValueError(f"n_trials phải ≥ 1 (nhận {n_trials})")
    if pnls is not None:
        arr = np.asarray(pnls, dtype=float)
        if len(arr) < rules.min_trades:
            raise ValueError(f"cần ≥ {rules.min_trades} lệnh tham chiếu (nhận {len(arr)})")
        if arr.std() == 0:
            raise ValueError("P&L tham chiếu không biến thiên")
        sd = float(arr.std())
    elif sd is None:
        raise ValueError("cần `sd` hoặc `pnls` tham chiếu")
    alpha = alpha_per_test(n_trials, rules)
    _check(sd, n_trades, alpha)

    out = {"edge": float(edge), "sd": float(sd), "n_trades": int(n_trades), "n_trials": int(n_trials),
           "alpha_per_test": alpha, "min_power": rules.min_power, "reachable": reachable(n_trials, rules),
           "power_normal": power_normal(edge, sd, n_trades, alpha),
           "min_detectable_edge": min_detectable_edge(sd, n_trades, alpha, rules.min_power),
           "trades_needed": trades_needed(edge, sd, alpha, rules.min_power),
           "mde_table": [{"n_trades": n, "min_detectable_edge": min_detectable_edge(sd, n, alpha, rules.min_power)}
                         for n in sorted(set(TABLE_N) | {int(n_trades)})],
           "reasons": []}
    if pnls is not None:
        out.update(method="simulation", n_reference=int(len(arr)), n_rep=rules.n_rep,
                   power=simulated_power(arr, edge, n_trades, n_trials, rules),
                   false_positive_rate=simulated_power(arr, 0.0, n_trades, n_trials, rules))
    else:
        out.update(method="normal", power=out["power_normal"])

    reasons = out["reasons"]
    if n_trades < rules.min_trades:
        reasons.append(f"chỉ {n_trades} lệnh dự kiến < {rules.min_trades} — kiểm định gộp sẽ trả CHƯA ĐỦ mẫu")
    if not out["reachable"]:
        reasons.append(f"kiểm định không thể đạt: p nhỏ nhất 1/{rules.mc_sims + 1} × {n_trials} cấu hình ≥ "
                       f"{_vn(rules.alpha, 2)} — phải tăng mc_sims trước (đăng ký trước, không sửa sau khi xem kết quả)")
    elif out["power"] < rules.min_power:
        reasons.append(f"lực {out['power']:.0%} < {rules.min_power:.0%}: với {n_trades} lệnh chỉ bắt được lợi thế ≥ "
                       f"{_vn(out['min_detectable_edge'], 2)} điểm/lệnh; lợi thế {_vn(edge, 2)} điểm cần khoảng "
                       f"{out['trades_needed']} lệnh")
    out["verdict"] = KHONG_DU if reasons else DU_LUC
    return out


def reference_pnls(trades) -> list[float]:
    """P&L điểm/lệnh sau phí của các lệnh ĐÃ ĐÓNG, xếp theo thời điểm vào lệnh."""
    closed = [t for t in trades if t.exit_ts is not None]
    return [float(t.pnl_points) for t in sorted(closed, key=lambda t: t.entry_ts)]


# ------------------------------------------------------------------ hiển thị ----
def _vn(x: float, digits: int) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def render(g: dict, title: str = "") -> str:
    head = f"{title} — " if title else ""
    lines = [f"{head}cổng lực thống kê",
             f"Giả định: lợi thế {_vn(g['edge'], 2)} điểm/lệnh sau phí · độ lệch chuẩn {_vn(g['sd'], 2)} điểm/lệnh · "
             f"{g['n_trades']} lệnh OOS gộp",
             f"Kiểm định: p sign-flip × {g['n_trials']} cấu hình ⇒ mỗi phép thử cần p < {_vn(g['alpha_per_test'], 4).rstrip('0')}"]
    if g["method"] == "simulation":
        lines.append(f"Lực (mô phỏng {g['n_rep']} lần trên {g['n_reference']} lệnh tham chiếu): {g['power']:.0%} · "
                     f"công thức chuẩn: {g['power_normal']:.0%} · báo động giả khi không có lợi thế: "
                     f"{_vn(g['false_positive_rate'] * 100, 1)}%")
    else:
        lines.append(f"Lực (công thức chuẩn): {g['power']:.0%}")
    need = "—" if g["trades_needed"] is None else str(g["trades_needed"])
    lines.append(f"Để đạt lực {g['min_power']:.0%}: cần lợi thế ≥ {_vn(g['min_detectable_edge'], 2)} điểm/lệnh với "
                 f"{g['n_trades']} lệnh, hoặc khoảng {need} lệnh với lợi thế {_vn(g['edge'], 2)} điểm")
    lines.append(f"Lợi thế nhỏ nhất bắt được (lực {g['min_power']:.0%}) theo số lệnh:")
    lines += [f"  {r['n_trades']:>5} lệnh: {_vn(r['min_detectable_edge'], 2)} điểm/lệnh" for r in g["mde_table"]]
    lines.append("(Lực là cận trên: luật OOS gộp còn đòi mọi cửa sổ lãi và chịu được trượt giá căng.)")
    lines.append(f"KẾT LUẬN: {g['verdict']}")
    lines += [f"  - {r}" for r in g["reasons"]]
    return "\n".join(lines)


# ----------------------------------------------------------------------- CLI ----
def _run_reference(bars_paths, spec: str, until: Optional[date]) -> tuple[list[float], str]:
    from backtest.engine import BacktestEngine, load_bars_parquet
    from backtest.robustness import cut_until
    from core.settings import load_settings

    mod, _, cls = spec.partition(":")
    strat_cls = getattr(importlib.import_module(mod), cls)
    bars, _skipped = load_bars_parquet(bars_paths)
    settings = load_settings()
    trades = BacktestEngine(settings, strat_cls.from_settings(settings)).run_bars(cut_until(bars, until)).trades
    return reference_pnls(trades), getattr(strat_cls, "TITLE", cls)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Cổng lực thống kê trước khi đăng ký một ý tưởng")
    ap.add_argument("--edge", type=float, required=True, help="lợi thế giả định sau phí, điểm/lệnh")
    ap.add_argument("--n-trades", type=int, required=True, help="số lệnh dự kiến ở OOS gộp")
    ap.add_argument("--sd", type=float, help="độ lệch chuẩn P&L mỗi lệnh (điểm) — hoặc dùng --bars + --strategy")
    ap.add_argument("--bars", nargs="+", help="nến 1 phút Parquet để chạy chiến lược tham chiếu")
    ap.add_argument("--strategy", help="chiến lược tham chiếu ĐÃ thử, module:Class (chỉ mượn độ phân tán P&L)")
    ap.add_argument("--until", type=date.fromisoformat, help="chỉ dùng nến tới ngày này (đừng chạm dữ liệu còn sạch)")
    ap.add_argument("--n-trials", type=int, default=None,
                    help="mặc định = số cấu hình trong research/log.md + 1 (ý tưởng đang xét)")
    ap.add_argument("--title", default="")
    args = ap.parse_args(argv)

    n_trials = args.n_trials
    if n_trials is None:
        from backtest.robustness import count_logged_trials
        n_trials = count_logged_trials() + 1
    pnls, ref = None, ""
    try:
        if args.bars or args.strategy:
            if not (args.bars and args.strategy):
                raise ValueError("--bars và --strategy phải đi cùng nhau")
            pnls, ref = _run_reference(args.bars, args.strategy, args.until)
        elif args.sd is None:
            raise ValueError("cần --sd, hoặc --bars + --strategy để lấy độ phân tán từ chiến lược tham chiếu")
        gate = power_gate(args.edge, args.n_trades, n_trials, sd=args.sd, pnls=pnls)
    except (ValueError, ImportError, AttributeError) as exc:
        print(f"❌ {exc}")
        return 2
    if ref:
        print(f"(độ phân tán lấy từ chiến lược tham chiếu: {ref})")
    print(render(gate, args.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
