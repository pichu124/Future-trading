"""`scripts/compare_bars.py` — đối chiếu hai kho nến 1 phút (vd SSI với DNSE) trên các (hợp đồng, ngày) trùng nhau.
Viết TRƯỚC khi implement.

- Chỉ so (hợp đồng, ngày) có ở CẢ hai kho; phút so = phút có ở cả hai.
- Mỗi ngày: số nến mỗi bên, số phút chung, lệch giá đóng trung bình / lớn nhất (điểm), tỉ lệ phút OHLC khớp hệt,
  tỉ lệ phút volume khớp hệt, tổng volume hai bên.
- Tổng hợp: số ngày so, tỉ lệ phút chung khớp OHLC, lệch đóng lớn nhất, tỉ lệ volume khớp, ngày lệch nhiều nhất.
"""
from datetime import date, datetime, time, timedelta

import pandas as pd

from scripts.compare_bars import compare, main, render


def write(root, sym, d, minutes, close=1350.0, vol=10, bump=None):
    rows = []
    for i in range(minutes):
        t = datetime.combine(d, time(9, 0)) + timedelta(minutes=i)
        c = close + (bump.get(i, 0.0) if bump else 0.0)
        rows.append({"symbol": sym, "start": t, "open": close, "high": max(close, c) + 0.5, "low": min(close, c) - 0.5,
                     "close": c, "volume": vol, "interval_s": 60, "volume_reliable": True, "source": "x"})
    p = root / "ohlc1m" / sym / f"{d}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(p)


D1, D2, D3 = date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)


def test_identical_days_match_fully(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        write(root, "VN30F2606", D1, 30)
    rep = compare(a, b)
    (day,) = rep["days"]
    assert day["common"] == 30 and day["ohlc_match"] == 1.0 and day["volume_match"] == 1.0
    assert day["close_mae"] == 0 and rep["summary"]["n_days"] == 1 and rep["summary"]["ohlc_match"] == 1.0


def test_only_overlap_compared(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, "VN30F2606", D1, 30)
    write(a, "VN30F2606", D2, 30)          # chỉ có ở a
    write(b, "VN30F2606", D1, 20)          # b thiếu 10 phút cuối
    write(b, "VN30F2606", D3, 30)          # chỉ có ở b
    rep = compare(a, b)
    (day,) = rep["days"]
    assert (day["n_a"], day["n_b"], day["common"]) == (30, 20, 20)
    assert rep["summary"]["only_a"] == 1 and rep["summary"]["only_b"] == 1


def test_price_and_volume_differences_reported(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, "VN30F2606", D1, 10, vol=10)
    write(b, "VN30F2606", D1, 10, vol=12, bump={3: 0.4, 7: -1.0})
    (day,) = compare(a, b)["days"]
    assert day["close_max"] == 1.0 and abs(day["close_mae"] - 0.14) < 1e-9
    assert day["ohlc_match"] == 0.8 and day["volume_match"] == 0.0
    assert (day["volume_a"], day["volume_b"]) == (100, 120)


def test_date_range_filter_and_cli(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (D1, D2):
        write(a, "VN30F2606", d, 10)
        write(b, "VN30F2606", d, 10)
    assert compare(a, b, start=D2)["summary"]["n_days"] == 1
    assert main([str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "2 ngày" in out and "khớp OHLC" in out


def test_render_lists_worst_days(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a, "VN30F2606", D1, 10)
    write(b, "VN30F2606", D1, 10, bump={1: 5.0})
    text = render(compare(a, b))
    assert "2026-06-01" in text and "5.0" in text
