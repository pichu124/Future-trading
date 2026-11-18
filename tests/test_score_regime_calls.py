"""`scripts/score_regime_calls.py` — chấm nhận định regime shadow với thực tế và với baseline thống kê. Viết TRƯỚC khi
implement. Luật đăng ký trước 16/11/2026 (research/log.md, "Nhật ký regime shadow").

- Thực tế mỗi ngày tính từ nến 1 phút của HỢP ĐỒNG BOT GIAO DỊCH ngày đó, trong [continuous_am_start, close]:
  mở = giá mở nến đầu, đỉnh/đáy = cực trị, đóng = giá đóng nến cuối. Ngày chưa có nến từ `atc_start` ⇒ chưa đủ, bỏ.
  ADR = trung bình (đỉnh − đáy) của `adr_days` phiên có dữ liệu liền trước.
- Nhãn: vol theo (đỉnh − đáy)/ADR; day_type theo |đóng − mở|/(đỉnh − đáy); direction theo (đóng − mở)/ADR.
- Điểm: Brier đa lớp Σ(p − o)² (0 = hoàn hảo, 2 = tệ nhất). Baseline chỉ dùng nhãn các ngày TRƯỚC ngày chấm:
  `climatology` = tần suất làm trơn Laplace; `markov` = tần suất có điều kiện theo nhãn phiên liền trước.
- Kết luận chỉ trên câu hỏi chính, trên ĐÚNG `min_days` phiên chấm được đầu tiên: CÓ GIÁ TRỊ ⇔ Brier tranh luận thấp
  hơn CẢ HAI baseline và p sign-flip so với baseline tốt hơn < ngưỡng. Chưa đủ phiên ⇒ CHƯA ĐỦ.
"""
import random
from datetime import date, datetime, time

import pandas as pd
import pytest

from core import contracts
from core.clock import SessionSchedule
from core.models import Bar
from scripts.regime_call import QUESTIONS
from scripts.score_regime_calls import (Rules, baseline_probs, brier, day_stats, main, realized_labels, render, score)

RULES = Rules(adr_days=2, vol_low_below=0.8, vol_high_above=1.2, trend_min_efficiency=0.5, flat_band_adr=0.2,
              primary_question="day_type", min_days=20, p_threshold=0.05)
START = date(2026, 6, 1)


@pytest.fixture
def schedule(session_cfg) -> SessionSchedule:
    return SessionSchedule.from_mapping(session_cfg)


def days(cal, n: int, start: date = START) -> list[date]:
    out, d = [], cal.next_trading_day(start, inclusive=True)
    while len(out) < n:
        out.append(d)
        d = cal.next_trading_day(d)
    return out


def bar(sym, d, hhmm, o, h, l, c) -> Bar:
    hh, mm = map(int, hhmm.split(":"))
    return Bar(sym, datetime.combine(d, time(hh, mm)), o, h, l, c, 10)


def mk_day(cal, d, o, h, l, c, sym=None) -> list[Bar]:
    """Một ngày đủ: mở `o` lúc 09:00, chạm `h` và `l` trong phiên, đóng `c` ở nến 14:45."""
    sym = sym or contracts.trading_contract(d, cal, 1)
    return [bar(sym, d, "09:00", o, max(o, c), min(o, c), o), bar(sym, d, "10:00", o, h, o, o),
            bar(sym, d, "13:30", o, o, l, o), bar(sym, d, "14:45", c, max(o, c), min(o, c), c)]


def trend_day(cal, d):          # biên 20, hiệu suất 0,75, lên 15 điểm
    return mk_day(cal, d, 1000.0, 1017.0, 997.0, 1015.0)


def range_day(cal, d):          # biên 20, hiệu suất 0,1, gần như đứng yên
    return mk_day(cal, d, 1000.0, 1010.0, 990.0, 1002.0)


def call(d: date, day_type_trend: float, single_trend: float = 0.5) -> dict:
    def f(p):
        return {"vol": {"low": 0.2, "normal": 0.6, "high": 0.2}, "day_type": {"trend": p, "range": 1 - p},
                "direction": {"up": 0.3, "flat": 0.4, "down": 0.3}}
    return {"trade_date": d.isoformat(), "debate": f(day_type_trend), "single": f(single_trend)}


# ------------------------------------------------------------------ thực tế ----

def test_day_stats_uses_session_window_and_trading_contract(cal, schedule):
    d = days(cal, 1)[0]
    sym = contracts.trading_contract(d, cal, 1)
    bars = mk_day(cal, d, 1000.0, 1017.0, 997.0, 1015.0)
    bars.append(bar(sym, d, "08:45", 1500.0, 1500.0, 900.0, 1500.0))          # trước 09:00 ⇒ không tính
    bars += mk_day(cal, d, 2000.0, 2050.0, 1950.0, 2040.0, sym="VN30F2612")   # hợp đồng khác ⇒ không tính
    (st,) = day_stats(bars, schedule, cal, 1)
    assert (st.day, st.contract) == (d, sym)
    assert (st.open, st.high, st.low, st.close) == (1000.0, 1017.0, 997.0, 1015.0)


def test_day_stats_skips_incomplete_day(cal, schedule):
    d1, d2 = days(cal, 2)
    partial = [b for b in trend_day(cal, d2) if b.start.time() < time(14, 30)]   # chưa có nến ATC ⇒ ngày chưa xong
    assert [s.day for s in day_stats(trend_day(cal, d1) + partial, schedule, cal, 1)] == [d1]


def test_day_stats_resolves_krx_symbol(cal, schedule):
    d = days(cal, 1)[0]
    name = contracts.trading_contract(d, cal, 1)
    krx = contracts.krx_symbol(*contracts.contract_ym(name))
    (st,) = day_stats(mk_day(cal, d, 1000.0, 1017.0, 997.0, 1015.0, sym=krx), schedule, cal, 1)
    assert st.contract == name


def test_realized_labels_thresholds(cal, schedule):
    ds = days(cal, 6)
    bars = (mk_day(cal, ds[0], 1000.0, 1010.0, 990.0, 1000.0)          # biên 20
            + mk_day(cal, ds[1], 1000.0, 1010.0, 990.0, 1000.0)        # biên 20
            + mk_day(cal, ds[2], 1000.0, 1008.0, 998.0, 1007.0)        # biên 10 / ADR 20 = 0,5 ⇒ low; hiệu suất 0,7; +7 ≥ 4
            + mk_day(cal, ds[3], 1000.0, 1015.0, 985.0, 997.5)         # biên 30 / ADR 15 = 2,0 ⇒ high; 2,5 < 0,2×15
            + mk_day(cal, ds[4], 1000.0, 1004.0, 984.0, 986.0)         # biên 20 / ADR 20 = 1,0 ⇒ normal; −14 ⇒ down
            + mk_day(cal, ds[5], 1000.0, 1020.0, 990.0, 1015.0))       # biên 30 / ADR 25 = 1,2 ⇒ normal (không > 1,2)
    labels = realized_labels(day_stats(bars, schedule, cal, 1), RULES)
    assert set(labels) == set(ds[2:])                                   # 2 ngày đầu chưa đủ lịch sử ADR
    assert labels[ds[2]] == {"vol": "low", "day_type": "trend", "direction": "up"}
    assert labels[ds[3]] == {"vol": "high", "day_type": "range", "direction": "flat"}
    assert labels[ds[4]] == {"vol": "normal", "day_type": "trend", "direction": "down"}
    assert labels[ds[5]] == {"vol": "normal", "day_type": "trend", "direction": "up"}
    assert all(set(v) == set(QUESTIONS) for v in labels.values())


def test_realized_labels_zero_range_day_is_range_flat(cal, schedule):
    ds = days(cal, 3)
    bars = trend_day(cal, ds[0]) + trend_day(cal, ds[1]) + mk_day(cal, ds[2], 1000.0, 1000.0, 1000.0, 1000.0)
    assert realized_labels(day_stats(bars, schedule, cal, 1), RULES)[ds[2]] == {
        "vol": "low", "day_type": "range", "direction": "flat"}


# ------------------------------------------------------------------ điểm & baseline ----

def test_brier():
    assert brier({"trend": 1.0, "range": 0.0}, "trend") == 0.0
    assert brier({"trend": 0.0, "range": 1.0}, "trend") == 2.0
    assert brier({"low": 1 / 3, "normal": 1 / 3, "high": 1 / 3}, "high") == pytest.approx(2 / 3)
    with pytest.raises(KeyError):
        brier({"trend": 0.5, "range": 0.5}, "sideways")


def test_baseline_climatology_is_laplace_smoothed():
    classes = ("trend", "range")
    assert baseline_probs([], classes, "climatology") == {"trend": 0.5, "range": 0.5}
    assert baseline_probs(["trend", "trend", "range"], classes, "climatology") == pytest.approx(
        {"trend": 3 / 5, "range": 2 / 5})


def test_baseline_markov_conditions_on_previous_label():
    classes = ("trend", "range")
    past = ["trend", "range", "trend", "range", "trend"]        # sau trend luôn là range (2 lần); phiên trước = trend
    assert baseline_probs(past, classes, "markov") == pytest.approx({"trend": 1 / 4, "range": 3 / 4})
    assert baseline_probs([], classes, "markov") == {"trend": 0.5, "range": 0.5}
    assert baseline_probs(["trend"], classes, "markov") == {"trend": 0.5, "range": 0.5}   # chưa thấy chuyển tiếp nào
    with pytest.raises(ValueError):
        baseline_probs(past, classes, "tarot")


# ------------------------------------------------------------------ chấm ----

def _world(cal, schedule, n_days: int, seed: int = 7, warmup: int = RULES.adr_days):
    rng = random.Random(seed)
    ds = days(cal, n_days + warmup)
    kinds = [rng.random() < 0.5 for _ in ds]
    bars = [b for d, k in zip(ds, kinds) for b in (trend_day(cal, d) if k else range_day(cal, d))]
    labels = realized_labels(day_stats(bars, schedule, cal, 1), RULES)
    return ds[warmup:], dict(zip(ds, kinds)), labels, bars


def test_score_not_enough_days(cal, schedule):
    ds, truth, labels, _ = _world(cal, schedule, 10)
    rep = score([call(d, 0.9 if truth[d] else 0.1) for d in ds], labels, RULES, cal)
    assert rep["n_calls"] == 10 and rep["n_scored"] == 10
    assert rep["verdict"] == "CHƯA ĐỦ" and rep["verdict_window"] is None
    assert rep["all"]["day_type"]["brier"]["debate"] == pytest.approx(0.02)
    assert "CHƯA ĐỦ" in render(rep)


def test_score_skilled_debate_has_value(cal, schedule):
    ds, truth, labels, _ = _world(cal, schedule, 30)
    rep = score([call(d, 0.9 if truth[d] else 0.1) for d in ds], labels, RULES, cal)
    w = rep["verdict_window"]["day_type"]
    assert w["n"] == RULES.min_days
    assert w["brier"]["debate"] == pytest.approx(0.02)
    assert w["brier"]["debate"] < min(w["brier"]["climatology"], w["brier"]["markov"])
    assert w["best_baseline"] in ("climatology", "markov") and w["p_vs_baseline"] < 0.05
    assert w["brier"]["single"] == pytest.approx(0.5) and w["p_vs_single"] < 0.05
    assert rep["verdict"] == "CÓ GIÁ TRỊ"
    assert rep["all"]["day_type"]["n"] == 30
    assert set(rep["all"]) == set(QUESTIONS)                      # câu hỏi phụ vẫn được báo cáo
    text = render(rep)
    assert "CÓ GIÁ TRỊ" in text and "day_type" in text and "markov" in text


def test_score_confidently_wrong_debate_has_no_value(cal, schedule):
    ds, truth, labels, _ = _world(cal, schedule, 30)
    rep = score([call(d, 0.1 if truth[d] else 0.9) for d in ds], labels, RULES, cal)
    assert rep["verdict"] == "KHÔNG CÓ GIÁ TRỊ" and rep["reasons"]


def test_verdict_uses_only_first_min_days(cal, schedule):
    ds, truth, labels, _ = _world(cal, schedule, 40)
    good_then_bad = [call(d, (0.9 if truth[d] else 0.1) if i < RULES.min_days else (0.0 if truth[d] else 1.0))
                     for i, d in enumerate(ds)]
    rep = score(good_then_bad, labels, RULES, cal)
    assert rep["verdict"] == "CÓ GIÁ TRỊ"                        # chốt ở 20 phiên đầu, không trượt theo dữ liệu về sau
    assert rep["all"]["day_type"]["brier"]["debate"] > rep["verdict_window"]["day_type"]["brier"]["debate"]


def test_score_reports_unscored_and_missing_days(cal, schedule):
    ds, truth, labels, _ = _world(cal, schedule, 10)
    later = cal.next_trading_day(ds[-1])                          # có nhận định nhưng chưa có nến để chấm
    calls = [call(d, 0.6) for d in ds if d != ds[3]] + [call(later, 0.6)]
    rep = score(calls, labels, RULES, cal)
    assert rep["n_calls"] == 10 and rep["n_scored"] == 9
    assert rep["unscored"] == [later.isoformat()]
    assert rep["missing_days"] == [ds[3].isoformat()]            # ngày giao dịch không có nhận định (tác vụ lỡ)


def test_baselines_never_see_the_scored_day(cal, schedule):
    """Đổi nhãn của CHÍNH ngày chấm và các ngày sau không được làm đổi xác suất baseline của ngày đó."""
    ds, truth, labels, _ = _world(cal, schedule, 12)
    calls = [call(d, 0.5) for d in ds]
    target = ds[6]
    flipped = {d: (dict(v, day_type="range" if v["day_type"] == "trend" else "trend") if d >= target else v)
               for d, v in labels.items()}
    row_a = next(r for r in score(calls, labels, RULES, cal)["rows"] if r["date"] == target.isoformat())
    row_b = next(r for r in score(calls, flipped, RULES, cal)["rows"] if r["date"] == target.isoformat())
    assert row_a["day_type"]["probs"]["markov"] == row_b["day_type"]["probs"]["markov"]
    assert row_a["day_type"]["probs"]["climatology"] == row_b["day_type"]["probs"]["climatology"]


# ------------------------------------------------------------------ CLI ----

def test_cli_reads_calls_and_bars(tmp_path, cal, schedule, capsys, monkeypatch):
    import json

    from scripts import score_regime_calls as mod
    ds, truth, labels, bars = _world(cal, schedule, 6, warmup=10)      # settings thật: adr_days = 10
    monkeypatch.setattr(mod.TradingCalendar, "default", classmethod(lambda cls: cal))
    for b_day in {b.start.date() for b in bars}:
        rows = [{"symbol": b.symbol, "start": b.start, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                 "volume": b.volume, "interval_s": 60, "volume_reliable": True} for b in bars if b.start.date() == b_day]
        p = tmp_path / "bars" / rows[0]["symbol"] / f"{b_day}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(p)
    calls_dir = tmp_path / "calls"
    calls_dir.mkdir()
    for d in ds:
        rec = dict(call(d, 0.9 if truth[d] else 0.1), schema_version=1, dry_run=False, contract="x", model="m",
                   created_at=f"{d.isoformat()}T08:00:00+07:00")
        (calls_dir / f"{d.isoformat()}.json").write_text(json.dumps(rec), encoding="utf-8")
    assert main(["--bars", str(tmp_path / "bars"), "--calls", str(calls_dir)]) == 0
    out = capsys.readouterr().out
    assert "6 nhận định" in out and "CHƯA ĐỦ" in out and "day_type" in out


def test_cli_no_calls(tmp_path, capsys):
    assert main(["--bars", str(tmp_path), "--calls", str(tmp_path / "none")]) == 1
    assert "Chưa có nhận định" in capsys.readouterr().out


# ------------------------------------------------------------------ verdict_from (sửa 18/11/2026) ----

def test_verdict_window_starts_at_verdict_from(cal, schedule):
    """Nhận định trước `verdict_from` (gói dữ liệu cũ) vẫn được chấm để theo dõi nhưng KHÔNG vào cửa sổ kết luận."""
    import dataclasses
    ds, truth, labels, _ = _world(cal, schedule, 30)
    start = ds[5]
    rules = dataclasses.replace(RULES, verdict_from=start)
    bad_then_good = [call(d, (0.0 if truth[d] else 1.0) if d < start else (0.9 if truth[d] else 0.1)) for d in ds]
    rep = score(bad_then_good, labels, rules, cal)
    assert rep["n_scored"] == 30 and rep["n_before_verdict_from"] == 5
    assert rep["verdict"] == "CÓ GIÁ TRỊ"                                  # 25 phiên từ mốc ≥ 20: chấm 20 phiên đầu từ mốc
    assert rep["verdict_window"]["day_type"]["n"] == 20
    assert rep["verdict_window"]["day_type"]["brier"]["debate"] == pytest.approx(0.02)
    assert "trước mốc" in render(rep)
    short = score(bad_then_good[:20], labels, rules, cal)                  # 15 phiên từ mốc < 20 ⇒ CHƯA ĐỦ
    assert short["verdict"] == "CHƯA ĐỦ"


def test_rules_from_settings_reads_verdict_from():
    from core.settings import Settings
    base = {"adr_days": 10, "vol_low_below": 0.8, "vol_high_above": 1.2, "trend_min_efficiency": 0.5,
            "flat_band_adr": 0.2, "primary_question": "day_type", "min_days": 60, "p_threshold": 0.05}
    s = Settings({"research": {"regime_shadow": dict(base, verdict_from="2026-11-19")}})
    assert Rules.from_settings(s).verdict_from == date(2026, 11, 19)
    assert Rules.from_settings(Settings({"research": {"regime_shadow": base}})).verdict_from is None
