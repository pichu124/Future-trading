"""Kiểm định GỘP nhiều giai đoạn out-of-sample (`backtest/pooled.py`) — viết TRƯỚC khi implement.

Luật đăng ký trước (research/log.md, 11/11/2026 — "giai đoạn kiểm tra THỨ HAI"), cho từng chiến lược:
- lệnh OOS = lệnh có ngày VÀO thuộc một trong các cửa sổ (đóng hai đầu; cửa sổ mở cuối = tới hết dữ liệu);
- CÓ EDGE ⇔ (a) ≥ 30 lệnh gộp; (b) P&L sau phí > 0 ở MỌI cửa sổ (cửa sổ 0 lệnh = 0 ⇒ trượt);
  (c) p sign-flip của chuỗi gộp × n_trials < 0,1; (d) P&L gộp với trượt 2 tick > 0;
- thiếu (a) ⇒ CHƯA ĐỦ (vẫn báo các con số); trượt (b)/(c)/(d) ⇒ KHÔNG CÓ EDGE.
"""
from datetime import date, datetime

import pytest

from backtest.pooled import CHUA_DU, CO_EDGE, KHONG_EDGE, parse_window, pooled_verdict
from backtest.report import TradeRecord

W1 = (date(2026, 3, 16), date(2026, 5, 10))
W2 = (date(2026, 9, 1), None)


def tr(d, pnl):
    ts = datetime.combine(d, datetime.min.time()).replace(hour=13, minute=1)
    return TradeRecord("VN30F2611", "LONG", 1, ts, 1350.0, ts.replace(hour=14, minute=20), 1351.0, "exit",
                       pnl / 100000 + 0.47, 46700.0, float(pnl), pnl / 100000)


def series(start, n, pnls):
    from datetime import timedelta
    return [tr(start + timedelta(days=i), pnls[i % len(pnls)]) for i in range(n)]


def test_parse_window():
    assert parse_window("2026-03-16:2026-05-10") == W1
    assert parse_window("2026-09-01:") == W2
    with pytest.raises(ValueError):
        parse_window("2026-05-10:2026-03-16")


def test_only_trades_inside_windows_counted():
    base = series(date(2026, 3, 12), 10, [100]) + series(date(2026, 6, 1), 10, [100]) + series(date(2026, 9, 1), 5, [100])
    r = pooled_verdict(base, base, [W1, W2], n_trials=5)
    assert [w["n_trades"] for w in r["windows"]] == [6, 5]           # 16/03…21/03 trong W1; tháng 6 bị bỏ
    assert r["n_trades"] == 11


def test_too_few_trades_is_not_enough_even_if_profitable():
    base = series(date(2026, 4, 1), 10, [500_000]) + series(date(2026, 9, 2), 10, [500_000])
    r = pooled_verdict(base, base, [W1, W2], n_trials=5)
    assert r["verdict"] == CHUA_DU and r["n_trades"] == 20


def test_strong_consistent_edge_passes():
    pnls = [900_000, 800_000, -300_000, 700_000, 600_000, -200_000, 1_000_000]
    base = series(date(2026, 4, 1), 20, pnls) + series(date(2026, 9, 2), 20, pnls)
    slip = [tr(t.entry_ts.date(), t.pnl_vnd - 20_000) for t in base]
    r = pooled_verdict(base, slip, [W1, W2], n_trials=5)
    assert r["verdict"] == CO_EDGE and r["p_adj"] < 0.1 and all(w["pnl_vnd"] > 0 for w in r["windows"])


def test_one_losing_window_fails():
    good = [900_000, 800_000, -300_000, 700_000]
    base = series(date(2026, 4, 1), 20, [-400_000, 100_000]) + series(date(2026, 9, 2), 30, good)
    r = pooled_verdict(base, base, [W1, W2], n_trials=5)
    assert r["verdict"] == KHONG_EDGE and any("cửa sổ" in x for x in r["reasons"])


def test_empty_window_fails():
    good = [900_000, 800_000, -300_000, 700_000]
    base = series(date(2026, 9, 2), 40, good)
    r = pooled_verdict(base, base, [W1, W2], n_trials=5)
    assert r["verdict"] == KHONG_EDGE and r["windows"][0]["n_trades"] == 0


def test_p_value_penalised_by_trials():
    pnls = [300_000, -250_000, 200_000, -150_000, 260_000]           # dương nhẹ, p không đủ nhỏ sau ×5
    base = series(date(2026, 4, 1), 20, pnls) + series(date(2026, 9, 2), 20, pnls)
    r = pooled_verdict(base, base, [W1, W2], n_trials=5)
    assert r["p_adj"] == pytest.approx(min(1.0, r["p"] * 5))
    assert r["verdict"] == KHONG_EDGE and any("p" in x for x in r["reasons"])


def test_slippage_stress_negative_fails():
    pnls = [900_000, 800_000, -300_000, 700_000, 600_000, -200_000, 1_000_000]
    base = series(date(2026, 4, 1), 20, pnls) + series(date(2026, 9, 2), 20, pnls)
    slip = [tr(t.entry_ts.date(), -100_000) for t in base]
    r = pooled_verdict(base, slip, [W1, W2], n_trials=5)
    assert r["verdict"] == KHONG_EDGE and r["slip_pnl_vnd"] < 0


# ------------------------------------------------------------ CLI --bars (16/11/2026, cho #7/#9 trên nến SSI) ----

class _NeverTrades:
    TITLE = "không giao dịch"

    @classmethod
    def from_settings(cls, s):
        return cls()

    def on_bar(self, bar, ctx):
        return []


def test_cli_accepts_bars_instead_of_ticks(tmp_path, capsys):
    import pandas as pd

    from backtest.pooled import main
    for d in (date(2026, 1, 5), date(2026, 1, 6)):
        rows = [{"symbol": "VN30F2601", "start": datetime(d.year, d.month, d.day, 9, m), "open": 1800.0, "high": 1801.0,
                 "low": 1799.0, "close": 1800.5, "volume": 10, "interval_s": 60, "volume_reliable": False}
                for m in range(0, 30)]
        p = tmp_path / "ohlc1m" / "VN30F2601" / f"{d}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(p)
    rc = main(["--bars", str(tmp_path / "ohlc1m"), "--strategy", "tests.test_pooled_oos:_NeverTrades",
               "--window", "2026-01-01:2026-01-05", "--window", "2026-01-06:", "--n-trials", "9"])
    out = capsys.readouterr().out
    assert rc == 0 and "n_trials = 9" in out and CHUA_DU in out and "0 lệnh" in out


def test_cli_requires_exactly_one_source(capsys):
    from backtest.pooled import main
    with pytest.raises(SystemExit):
        main(["--strategy", "tests.test_pooled_oos:_NeverTrades", "--window", "2026-01-01:"])
