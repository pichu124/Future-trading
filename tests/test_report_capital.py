"""Báo cáo backtest theo % vốn (`backtest.initial_capital_vnd`) — viết TRƯỚC khi implement.

- Có vốn ⇒ mỗi phần (toàn bộ / IS / OOS) thêm `return_pct` = P&L sau phí / vốn và `max_drawdown_pct` = sụt
  giảm lớn nhất so với GIÁ TRỊ TÀI KHOẢN tại đỉnh (vốn + lãi luỹ kế tới đỉnh), theo thứ tự đóng lệnh.
- null ⇒ không tính %, báo cáo ghi rõ (không tự đoán vốn). Vốn ≤ 0 / không phải số ⇒ lỗi.
"""
from datetime import datetime, timedelta

import pytest

from backtest.report import TradeRecord, build_report, compute_metrics, render_html


def tr(pnl, i):
    t0 = datetime(2026, 11, 9, 10, 0) + timedelta(days=i)
    return TradeRecord(symbol="VN30F2611", side="LONG", qty=1, entry_ts=t0, entry_price=1350.0,
                       exit_ts=t0 + timedelta(minutes=5), exit_price=1350.0, exit_reason="exit",
                       gross_points=pnl / 100_000, fees_vnd=0.0, pnl_vnd=pnl, pnl_points=pnl / 100_000)


TRADES = [tr(2_000_000, 0), tr(-3_000_000, 1), tr(1_000_000, 2)]     # vốn 10tr: 12tr → 9tr → 10tr


def test_percent_metrics_with_capital():
    m = compute_metrics(TRADES, initial_capital_vnd=10_000_000)
    assert m["return_pct"] == pytest.approx(0.0)
    assert m["max_drawdown_pct"] == pytest.approx(3_000_000 / 12_000_000)
    assert m["max_drawdown_vnd"] == 3_000_000
    m2 = compute_metrics(TRADES[:1], initial_capital_vnd=10_000_000)
    assert m2["return_pct"] == pytest.approx(0.2) and m2["max_drawdown_pct"] == 0.0


def test_drawdown_from_start_counts_capital_as_peak():
    m = compute_metrics([tr(-1_000_000, 0)], initial_capital_vnd=10_000_000)
    assert m["max_drawdown_pct"] == pytest.approx(0.1)


def test_without_capital_percent_is_none():
    m = compute_metrics(TRADES)
    assert m["return_pct"] is None and m["max_drawdown_pct"] is None


@pytest.mark.parametrize("bad", [0, -5, "10tr", True])
def test_invalid_capital_rejected(bad):
    with pytest.raises(ValueError):
        compute_metrics(TRADES, initial_capital_vnd=bad)


class Result:
    def __init__(self, trades):
        self.trades, self.open_trades, self.warnings = trades, [], []
        self.data_quality, self.assumptions, self.rejections, self.events = {}, {}, [], []


def test_report_sections_and_html_show_percent():
    rep = build_report(Result(TRADES), split_date=datetime(2026, 11, 11).date(), initial_capital_vnd=10_000_000)
    assert rep["sections"]["in_sample"]["metrics"]["return_pct"] == pytest.approx(-0.1)
    assert rep["sections"]["out_of_sample"]["metrics"]["return_pct"] == pytest.approx(0.1)
    assert rep["initial_capital_vnd"] == 10_000_000
    html = render_html(rep)
    assert "Lợi nhuận / vốn" in html and "25.0%" in html and "10,000,000" in html


def test_report_without_capital_warns():
    rep = build_report(Result(TRADES))
    assert any("initial_capital_vnd" in w for w in rep["warnings"])
    assert "Lợi nhuận / vốn" in render_html(rep)                     # dòng vẫn có, giá trị "—"


def test_cli_passes_capital(monkeypatch, tmp_path, session_cfg, cal):
    import backtest.engine as eng
    from tests.test_backtest_engine import bt_settings
    from tests.test_backtest_roll import D18, NOV, day_bars, write
    s = bt_settings(session_cfg)
    s._data["backtest"]["initial_capital_vnd"] = 50_000_000
    write(tmp_path / "b" / "x.parquet", day_bars(D18, NOV, 1350.0))
    monkeypatch.setattr(eng, "load_settings", lambda: s)
    rc = eng.main(["--bars", str(tmp_path / "b"), "--strategy", "tests.test_backtest_roll:Noop",
                   "--out", str(tmp_path / "r.html")])
    assert rc == 0 and "50,000,000" in (tmp_path / "r.html").read_text(encoding="utf-8")


def test_cli_rejects_bad_capital(monkeypatch, tmp_path, session_cfg):
    import backtest.engine as eng
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg)
    s._data["backtest"]["initial_capital_vnd"] = -1
    monkeypatch.setattr(eng, "load_settings", lambda: s)
    assert eng.main(["--bars", str(tmp_path), "--strategy", "tests.test_backtest_roll:Noop"]) == 2
