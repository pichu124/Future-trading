"""Kiểm định độ bền + KẾT LUẬN có edge hay không (PROMPTS P3.3, CLAUDE.md mục 6).

Một lần chạy gồm:
1. Cấu hình gốc trên toàn bộ dữ liệu ⇒ tách in-sample / out-of-sample theo `split_date`
   (OOS chỉ được nhìn MỘT lần với cấu hình đã chốt — không tune trên OOS).
2. Walk-forward: chia các ngày thành `folds` đoạn liên tiếp, xem từng đoạn có lãi sau phí không.
3. Độ nhạy ±20% từng tham số (số nguyên làm tròn, luôn lệch ít nhất 1 bước) — chạy trên
   IN-SAMPLE để không chạm OOS nhiều lần. Đỉnh nhọn (chỉ cấu hình gốc lãi) ⇒ dấu hiệu overfit.
4. Cấu hình gốc với trượt giá `slippage_stress_ticks` (mặc định 2).
5. `verdict()` áp luật CỐ ĐỊNH (`VerdictRules`) ⇒ CO_EDGE / KHONG_EDGE / CHUA_DU.

Luật kết luận được chốt trong code trước khi có dữ liệu thật — đổi luật phải sửa code + test,
không được chỉnh sau khi xem kết quả.
"""
from __future__ import annotations

import argparse
import copy
import html
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import BacktestEngine, bars_from_ticks, load_bars_parquet  # noqa: E402
from backtest.report import (_table, compute_metrics, make_section,  # noqa: E402
                             render_section)
from backtest.validation import DEFAULT, Thresholds  # noqa: E402
from core.calendar import TradingCalendar  # noqa: E402
from core.models import Bar, Tick  # noqa: E402
from core.settings import REPO_ROOT, MissingSetting, Settings, load_settings  # noqa: E402
from strategy.base import Strategy  # noqa: E402
from utils.atomic_io import write_text_atomic  # noqa: E402

CO_EDGE = "CÓ BẰNG CHỨNG EDGE"
KHONG_EDGE = "KHÔNG CÓ EDGE"
CHUA_DU = "CHƯA ĐỦ BẰNG CHỨNG"

ORB_PARAMS = ("strategy.orb.range_minutes", "strategy.orb.reward_risk",
              "strategy.orb.max_trades_per_day")


FILTER_PARAMS = ("strategy.orb.range_filter.adr_days", "strategy.orb.range_filter.min_ratio",
                 "strategy.orb.range_filter.max_ratio")


def params_for(settings: Settings) -> tuple[str, ...]:
    """Tham số kiểm độ nhạy: baseline + bộ lọc độ rộng biên (cấu hình #2) khi đang bật."""
    rf = settings.get("strategy.orb.range_filter")
    on = isinstance(rf, dict) and rf.get("enabled") is True
    return ORB_PARAMS + (FILTER_PARAMS if on else ())


@dataclass(frozen=True)
class VerdictRules:
    min_oos_trades: int = DEFAULT.min_trades          # 30 — dưới mức này không kết luận gì
    sensitivity_min_positive_frac: float = 0.75       # ≥ 75% biến thể ±20% phải lãi sau phí (IS)
    folds_min_positive_frac: float = 0.5              # ≥ nửa số đoạn walk-forward lãi sau phí
    # Dữ liệu "tin được" ⇔ tỉ lệ nến volume không tin được ≤ 1% (người dùng chọn 09/11/2026, TRƯỚC khi xem kết quả):
    # khớp lệnh DNSE thật có vài phút ATO/ATC thiếu lệnh (~0,4% nến); snapshot dashboard (100%) vẫn bị chặn.
    max_unreliable_volume_frac: float = 0.01


RULES = VerdictRules()


def data_reliable(quality: dict, rules: VerdictRules = RULES) -> bool:
    """Tỉ lệ nến có volume không tin được ≤ ngưỡng; không có nến nào ⇒ không tin được."""
    n = int(quality.get("n_bars") or 0)
    bad = int(quality.get("unreliable_volume_bars") or 0)
    return n > 0 and bad / n <= rules.max_unreliable_volume_frac + 1e-12


# ---------------------------------------------------------------- tiện ích ----

def perturbations(value: float, pct: float = 0.2, integer: bool = False, minimum: int = 1) -> list:
    """[giá trị × (1−pct), × (1+pct)]; số nguyên làm tròn, lệch ≥ 1 bước, không dưới `minimum`."""
    if not integer:
        return [round(value * (1 - pct), 10), round(value * (1 + pct), 10)]
    lo = min(int(round(value * (1 - pct))), int(value) - 1)
    hi = max(int(round(value * (1 + pct))), int(value) + 1)
    return [v for v in (lo, hi) if v >= minimum]


def with_overrides(settings: Settings, overrides: dict) -> Settings:
    """Bản sao settings với các khoá chấm được thay. Khoá không tồn tại ⇒ KeyError (chống gõ sai)."""
    data = copy.deepcopy(settings.raw)
    for dotted, value in overrides.items():
        node = data
        parts = dotted.split(".")
        for p in parts[:-1]:
            if not isinstance(node, dict) or p not in node:
                raise KeyError(dotted)
            node = node[p]
        if parts[-1] not in node:
            raise KeyError(dotted)
        node[parts[-1]] = value
    return Settings(data)


def parse_sets(items) -> dict:
    """["khoá.chấm=giá_trị", …] ⇒ {khoá: giá trị YAML} (true/false/số/chuỗi). Sai dạng ⇒ ValueError."""
    import yaml
    out = {}
    for it in items or ():
        key, eq, val = str(it).partition("=")
        if not eq or not key.strip() or not val.strip():
            raise ValueError(f"--set phải có dạng khoá.chấm=giá_trị, nhận {it!r}")
        out[key.strip()] = yaml.safe_load(val.strip())
    return out


def apply_sets(settings: Settings, items) -> Settings:
    """Bản sao settings với các `--set` (khoá phải tồn tại — KeyError khi gõ sai)."""
    sets = parse_sets(items)
    return with_overrides(settings, sets) if sets else settings


def count_logged_trials(path: Path = REPO_ROOT / "research" / "log.md") -> int:
    """Số cấu hình đã ghi trong research/log.md (mỗi dòng bảng bắt đầu bằng ngày). Tối thiểu 1."""
    p = Path(path)
    if not p.exists():
        return 1
    rows = [ln for ln in p.read_text(encoding="utf-8").splitlines()
            if re.match(r"^\|\s*\d{4}-\d{2}-\d{2}", ln)]
    return max(1, len(rows))


# ------------------------------------------------------------------- chạy ----

def run_robustness(settings: Settings, make_strategy: Callable[[Settings], Strategy],
                   bars: Sequence[Bar], split_date: date, params: Sequence[str] = ORB_PARAMS,
                   folds: int = 4, n_trials: int = 1, calendar: Optional[TradingCalendar] = None,
                   pct: float = 0.2, slippage_stress_ticks: int = 2,
                   rules: VerdictRules = RULES, thresholds: Thresholds = DEFAULT) -> dict:
    # `bars` có thể là bar 1 phút, list tick thật, HOẶC `TickStore` (--ticks: đọc dần từng ngày mỗi lượt — cả kho
    # 6 tháng ~8 GB nếu giữ trong RAM): tick ⇒ run_events(bars_from_ticks), chia theo ngày tick
    from backtest.ticks import TickStore
    if isinstance(bars, TickStore):
        store = bars
        ticks_mode = True
        days = store.days()

        all_src = store.iter
        is_src = lambda: store.iter(date_to=split_date - timedelta(days=1))  # noqa: E731
    else:
        data = list(bars)
        ticks_mode = bool(data) and isinstance(data[0], Tick)
        day_of = (lambda x: x.ts.date()) if ticks_mode else (lambda x: x.start.date())
        data.sort(key=(lambda t: t.ts) if ticks_mode else (lambda b: (b.start, b.symbol)))
        days = sorted({day_of(x) for x in data})
        is_data = [x for x in data if day_of(x) < split_date]
        all_src, is_src = (lambda: data), (lambda: is_data)
    if not days or not days[0] < split_date <= days[-1]:
        raise ValueError("split_date phải nằm trong khoảng dữ liệu (có cả IS lẫn OOS)")

    def run(s: Settings, src: Callable[[], Iterable]):
        engine = BacktestEngine(s, make_strategy(s), calendar=calendar)
        return engine.run_events(bars_from_ticks(src())) if ticks_mode else engine.run_bars(src())

    def split(trades):
        return {"in_sample": make_section([t for t in trades if t.entry_ts.date() < split_date],
                                          n_trials, thresholds),
                "out_of_sample": make_section([t for t in trades if t.entry_ts.date() >= split_date],
                                              n_trials, thresholds)}

    base = run(settings, all_src)
    sections = split(base.trades)

    fold_list = []
    for chunk in np.array_split(np.array(days, dtype=object), folds):
        if len(chunk) == 0:
            continue
        lo, hi = chunk[0], chunk[-1]
        tr = [t for t in base.trades if lo <= t.entry_ts.date() <= hi]
        fold_list.append({"start": lo, "end": hi, "metrics": compute_metrics(tr)})
    folds_pos = (sum(1 for f in fold_list if f["metrics"]["total_pnl_vnd"] > 0) / len(fold_list)
                 if fold_list else None)

    stressed_settings = with_overrides(settings, {"backtest.slippage_ticks": slippage_stress_ticks})
    stressed = run(stressed_settings, all_src)
    slip_sections = split(stressed.trades)

    sensitivity = []
    for param in params:
        base_value = settings.require(param)
        integer = isinstance(base_value, int) and not isinstance(base_value, bool)
        for v in perturbations(base_value, pct, integer):
            res = run(with_overrides(settings, {param: v}), is_src)
            sensitivity.append({"param": param, "base_value": base_value, "value": v,
                                "trades": res.trades, "metrics": compute_metrics(res.trades)})
    sens_pos = (sum(1 for v in sensitivity if v["metrics"]["total_pnl_vnd"] > 0) / len(sensitivity)
                if sensitivity else None)

    oos = sections["out_of_sample"]
    summ = {
        "data_reliable": data_reliable(base.data_quality, rules),
        "oos": {"n_trades": oos["metrics"]["n_trades"], "pnl_vnd": oos["metrics"]["total_pnl_vnd"],
                "passed": bool(oos["validation"]["passed"]),
                "reasons": list(oos["validation"]["reasons"])},
        "oos_slippage_stress_pnl_vnd": slip_sections["out_of_sample"]["metrics"]["total_pnl_vnd"],
        "sensitivity_positive_frac": sens_pos,
        "folds_positive_frac": folds_pos,
    }
    label, reasons = verdict(summ, rules)
    return {
        "split_date": split_date, "n_trials": n_trials, "pct": pct,
        "slippage_stress_ticks": slippage_stress_ticks,
        "base": base, "sections": sections, "folds": fold_list,
        "slippage_sections": slip_sections, "sensitivity": sensitivity,
        "summary": summ, "verdict": label, "verdict_reasons": reasons, "rules": rules,
        "days": (days[0], days[-1], len(days)),
    }


# ------------------------------------------------------------------ kết luận ----

def verdict(s: dict, rules: VerdictRules = RULES) -> tuple[str, list[str]]:
    oos = s["oos"]
    lean = " Hiện đang lỗ sau phí ⇒ nghiêng về KHÔNG có edge." if oos["pnl_vnd"] <= 0 else ""
    if not s["data_reliable"]:
        return CHUA_DU, [f"Quá {rules.max_unreliable_volume_frac:.0%} số nến có volume không tin được (vd dữ liệu "
                         "snapshot import từ vnstock-dashboard) — CLAUDE.md mục 6 cấm dùng để kết luận có edge." + lean]
    if oos["n_trades"] < rules.min_oos_trades:
        return CHUA_DU, [f"Chỉ {oos['n_trades']} lệnh out-of-sample < {rules.min_oos_trades} — "
                         "chưa đủ mẫu." + lean]
    fails = []
    if oos["pnl_vnd"] <= 0:
        fails.append(f"P&L out-of-sample sau phí = {oos['pnl_vnd']:,.0f} VNĐ ≤ 0.")
    if not oos["passed"]:
        fails.append("Kiểm định thống kê out-of-sample không đạt: " + "; ".join(oos["reasons"]))
    if s["oos_slippage_stress_pnl_vnd"] <= 0:
        fails.append(f"Trượt giá 2 tick: P&L out-of-sample = {s['oos_slippage_stress_pnl_vnd']:,.0f} "
                     "VNĐ ≤ 0 — lợi thế không đủ trả chi phí thực thi xấu hơn một chút.")
    sp = s["sensitivity_positive_frac"]
    if sp is None or sp < rules.sensitivity_min_positive_frac:
        fails.append(f"Độ nhạy ±20%: chỉ {0 if sp is None else sp:.0%} biến thể lãi "
                     f"(cần ≥ {rules.sensitivity_min_positive_frac:.0%}) — tham số là đỉnh nhọn, dễ overfit.")
    fp = s["folds_positive_frac"]
    if fp is None or fp < rules.folds_min_positive_frac:
        fails.append(f"Walk-forward: chỉ {0 if fp is None else fp:.0%} đoạn lãi "
                     f"(cần ≥ {rules.folds_min_positive_frac:.0%}) — lãi dồn vào một giai đoạn.")
    if fails:
        return KHONG_EDGE, fails
    return CO_EDGE, [
        f"Out-of-sample: {oos['n_trades']} lệnh, P&L sau phí {oos['pnl_vnd']:,.0f} VNĐ, kiểm định đạt.",
        f"Trượt 2 tick vẫn lãi: {s['oos_slippage_stress_pnl_vnd']:,.0f} VNĐ.",
        f"Độ nhạy ±20%: {sp:.0%} biến thể lãi; walk-forward: {fp:.0%} đoạn lãi.",
        "Vẫn phải paper trade (P4/P5) trước khi bật live — backtest trên bar dùng tick giả.",
    ]


# --------------------------------------------------------------------- HTML ----

def render_html(rob: dict, title: str = "Kiểm định độ bền") -> str:
    r: VerdictRules = rob["rules"]
    color = {CO_EDGE: "#e6f4ea", KHONG_EDGE: "#fde8e8", CHUA_DU: "#fff4e5"}[rob["verdict"]]
    reasons = "".join(f"<li>{html.escape(x)}</li>" for x in rob["verdict_reasons"])
    d0, d1, nd = rob["days"]
    folds = _table(["Đoạn", "Từ", "Đến", "Số lệnh", "P&L sau phí (VNĐ)", "Profit factor"],
                   [[i + 1, f["start"], f["end"], f["metrics"]["n_trades"],
                     f"{f['metrics']['total_pnl_vnd']:,.0f}",
                     "—" if f["metrics"]["profit_factor"] is None else f"{f['metrics']['profit_factor']:.2f}"]
                    for i, f in enumerate(rob["folds"])])
    base_is = rob["sections"]["in_sample"]["metrics"]
    sens_rows = [["(gốc)", "", base_is["n_trades"], f"{base_is['total_pnl_vnd']:,.0f}",
                  "—" if base_is["win_rate"] is None else f"{base_is['win_rate']:.0%}"]]
    for v in rob["sensitivity"]:
        m = v["metrics"]
        sens_rows.append([v["param"].split(".")[-1], f"{v['base_value']} → {v['value']}",
                          m["n_trades"], f"{m['total_pnl_vnd']:,.0f}",
                          "—" if m["win_rate"] is None else f"{m['win_rate']:.0%}"])
    sens = _table(["Tham số", "Giá trị", "Số lệnh (IS)", "P&L sau phí IS (VNĐ)", "Win rate"], sens_rows)
    slip = rob["slippage_sections"]
    slip_tbl = _table(["Phần", "Số lệnh", "P&L sau phí (VNĐ)"],
                      [[k, slip[k]["metrics"]["n_trades"], f"{slip[k]['metrics']['total_pnl_vnd']:,.0f}"]
                       for k in ("in_sample", "out_of_sample")])
    rules = _table(["Luật kết luận (chốt trước khi có dữ liệu)", "Ngưỡng"], [
        ["Tỉ lệ nến volume không tin được (snapshot / thiếu lệnh)", f"≤ {r.max_unreliable_volume_frac:.0%}"],
        ["Số lệnh out-of-sample tối thiểu", r.min_oos_trades],
        ["P&L out-of-sample sau phí", "> 0"],
        ["Kiểm định OOS (p-value sign-flip × n_trials, walk-forward theo lệnh)", "đạt"],
        [f"P&L OOS với trượt giá {rob['slippage_stress_ticks']} tick", "> 0"],
        ["Tỷ lệ biến thể ±20% lãi (in-sample)", f"≥ {r.sensitivity_min_positive_frac:.0%}"],
        ["Tỷ lệ đoạn walk-forward lãi", f"≥ {r.folds_min_positive_frac:.0%}"],
    ])
    q = rob["base"].data_quality
    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;max-width:1100px;color:#222;background:#fff}}
table{{border-collapse:collapse;margin:8px 0 16px;font-size:14px;display:block;overflow-x:auto;max-width:100%}}
th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left}} th{{background:#f2f2f2}}
.verdict{{background:{color};border:1px solid #999;padding:8px 16px}}
section{{border-top:2px solid #444;margin-top:24px}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<p>Dữ liệu {d0} → {d1} ({nd} ngày) · out-of-sample từ {rob['split_date']} · n_trials = {rob['n_trials']}
· tick giả từ bar: {q.get('synthetic_ticks')} · bar volume không tin được: {q.get('unreliable_volume_bars')} /
{q.get('n_bars')}</p>
<div class="verdict"><h2>Kết luận: {html.escape(rob['verdict'])}</h2><ul>{reasons}</ul></div>
<h3>Áp theo luật kết luận cố định (chốt trong code, không chỉnh sau khi xem kết quả)</h3>
{rules}
{render_section("in_sample", rob["sections"]["in_sample"])}
{render_section("out_of_sample", rob["sections"]["out_of_sample"])}
<section><h2>Walk-forward theo đoạn ngày</h2>{folds}</section>
<section><h2>Độ nhạy ±{rob['pct']:.0%} (chỉ in-sample)</h2>{sens}</section>
<section><h2>Trượt giá {rob['slippage_stress_ticks']} tick</h2>{slip_tbl}</section>
</body></html>
"""


# ----------------------------------------------------------------------- CLI ----

def main(argv: Optional[Sequence[str]] = None) -> int:
    import importlib

    ap = argparse.ArgumentParser(description="Kiểm định độ bền chiến lược + kết luận edge")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bars", nargs="+", help="file/thư mục Parquet bar 1 phút (đọc đệ quy)")
    src.add_argument("--ticks", nargs="+", help="tick thật: data/raw/<ngày> (recorder) và/hoặc data/history/trades")
    ap.add_argument("--source", help="với --ticks: chỉ dùng nguồn này (bắt buộc khi nhiều nguồn)")
    ap.add_argument("--strategy", default="strategy.baseline_orb:OpeningRangeBreakout",
                    help="module:Class (mặc định ORB). Class có ROBUSTNESS_PARAMS ⇒ kiểm độ nhạy trên các khoá đó")
    ap.add_argument("--split", required=True, help="ngày đầu out-of-sample (YYYY-MM-DD), chốt TRƯỚC khi chạy")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--n-trials", type=int, default=None,
                    help="mặc định = số dòng cấu hình trong research/log.md")
    ap.add_argument("--out", default="reports/orb_robustness.html")
    ap.add_argument("--set", action="append", default=[], metavar="KHOÁ=GIÁ_TRỊ",
                    help="đổi cấu hình CHỈ cho lần chạy này, vd strategy.orb.range_filter.enabled=true")
    args = ap.parse_args(argv)

    try:
        settings = apply_sets(load_settings(), args.set)
    except (ValueError, KeyError) as exc:
        print(f"❌ --set: {exc}")
        return 2
    mod_name, _, cls_name = args.strategy.partition(":")
    try:
        strategy_cls = getattr(importlib.import_module(mod_name), cls_name)
    except (ImportError, AttributeError, ValueError) as exc:
        print(f"❌ --strategy {args.strategy}: {exc}")
        return 2
    try:
        BacktestEngine(settings, strategy_cls.from_settings(settings))
    except MissingSetting as exc:
        print(f"❌ {exc.args[0]}\n   Chạy `python -m core.settings` để xem mọi khoá còn thiếu.")
        return 2
    skipped = n_fake = 0
    if args.ticks:
        from backtest.ticks import TickStore
        try:                                               # đọc dần từng ngày mỗi lượt — không nạp cả kho vào RAM
            data = TickStore(args.ticks, source=args.source,
                             tick_size=float(settings.require("contract.tick_size_points")),
                             spread_ticks=int(settings.require("backtest.synthetic_spread_ticks")))
        except ValueError as exc:
            print(f"❌ {exc}")
            return 2
    else:
        data, skipped = load_bars_parquet(args.bars)
    bars = data
    n_trials = args.n_trials or count_logged_trials()
    params = tuple(getattr(strategy_cls, "ROBUSTNESS_PARAMS", ()) or params_for(settings))
    rob = run_robustness(settings, strategy_cls.from_settings, bars,
                         date.fromisoformat(args.split), params, folds=args.folds,
                         n_trials=n_trials)
    if args.ticks:
        n_fake = data.fake_quotes()                        # sau lượt cuối: số tick bị gắn báo giá giả
    rf = settings.get("strategy.orb.range_filter")
    name = getattr(strategy_cls, "TITLE", None) or (
        "ORB #2 (lọc độ rộng biên / ADR)" if isinstance(rf, dict) and rf.get("enabled") is True
        else "ORB baseline (#1)")
    write_text_atomic(render_html(rob, title=f"{name} — kiểm định độ bền"), Path(args.out))
    print(f"Kết luận: {rob['verdict']}")
    for r in rob["verdict_reasons"]:
        print(f"  - {r}")
    if skipped:
        print(f"  (bỏ {skipped} dòng bar hỏng khi đọc)")
    if n_fake:
        print(f"  ({n_fake} tick khớp không có bid/ask — khớp lệnh trên báo giá giả ∓ nửa spread)")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
