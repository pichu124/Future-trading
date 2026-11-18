"""`scripts/is_gate.py` — cổng IS đăng ký trước cho #7/#9 (research/log.md, "Đăng ký trước #7, #8, #9"). Viết TRƯỚC.

KHÔNG CÓ EDGE (dừng, không mở OOS) ⇔ P&L IS sau phí ≤ 0 HOẶC < 75% biến thể độ nhạy lãi HOẶC < 50% đoạn walk-forward
lãi. Ngược lại ⇒ QUA CỔNG IS (được mở OOS). Không có biến thể / đoạn nào ⇒ coi như trượt (không đoán).
"""
from datetime import date, datetime

from backtest.report import TradeRecord
from scripts.is_gate import PASS, STOP, gate, render


def tr(d, pnl, side="LONG", reason="exit"):
    ts = datetime.combine(d, datetime.min.time()).replace(hour=9, minute=16)
    return TradeRecord("VN30F2512", side, 1, ts, 1800.0, ts.replace(hour=14, minute=20), 1801.0, reason,
                       pnl / 100000 + 0.47, 46700.0, float(pnl), pnl / 100000)


def rob(is_pnl=1e6, sens=(1, 1, 1, 1, 1, 1), folds=(1, 1, -1, 1)):
    m = lambda p, n=5: {"n_trades": n, "total_pnl_vnd": p, "win_rate": 0.5, "profit_factor": 1.2}   # noqa: E731
    return {"sections": {"in_sample": {"metrics": m(is_pnl, 20)}, "out_of_sample": {"metrics": m(0.0, 0)}},
            "slippage_sections": {"in_sample": {"metrics": m(is_pnl - 2e5, 20)}},
            "sensitivity": [{"param": f"strategy.x.p{i // 2}", "base_value": 1, "value": i, "metrics": m(v * 1e5)}
                            for i, v in enumerate(sens)],
            "folds": [{"start": date(2025, 8, 1), "end": date(2025, 9, 1), "metrics": m(v * 1e5)} for v in folds],
            "base": type("R", (), {"trades": [tr(date(2025, 8, 4), 2e5), tr(date(2025, 9, 2), -1e5, "SHORT", "stop")]})(),
            "days": (date(2025, 7, 22), date(2025, 12, 31), 113)}


def test_pass_when_all_three_hold():
    v, reasons = gate(rob())
    assert v == PASS and reasons


def test_stop_on_nonpositive_is_pnl():
    v, reasons = gate(rob(is_pnl=0.0))
    assert v == STOP and any("P&L IS" in r for r in reasons)


def test_stop_on_sensitivity_below_75pct():
    assert gate(rob(sens=(1, 1, 1, 1, -1, 1)))[0] == PASS          # 5/6 = 83%
    v, reasons = gate(rob(sens=(1, 1, 1, -1, -1, 1)))               # 4/6 = 67%
    assert v == STOP and any("độ nhạy" in r for r in reasons)


def test_stop_on_folds_below_half():
    assert gate(rob(folds=(1, 1, -1, -1)))[0] == PASS               # 2/4 = 50%
    v, reasons = gate(rob(folds=(1, -1, -1, -1)))
    assert v == STOP and any("walk-forward" in r for r in reasons)


def test_missing_variants_or_folds_is_stop():
    assert gate(rob(sens=()))[0] == STOP
    assert gate(rob(folds=()))[0] == STOP


def test_render_lists_numbers_and_verdict():
    text = render(rob(), "Thử (#7)")
    assert "Thử (#7)" in text and "1,000,000" in text and "QUA CỔNG IS" in text
    assert "p0" in text and "2025-08" in text and "LONG" in text and "stop" in text
