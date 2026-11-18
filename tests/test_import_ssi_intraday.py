"""`scripts/import_ssi_intraday.py` — chuyển kết quả thô của SSI `marketdata_list_intraday_stock_infor` (JSON) thành kho
nến 1 phút Parquet. Viết TRƯỚC khi implement.

- Mỗi file thô là một phản hồi `{"status": "ok", "data": {"ticks": [...]}}`; mỗi tick có symbol, open/high/low/close,
  volume (chuỗi), trading_date "DD/MM/YYYY HH:MM:SS" (chế độ range).
- HĐ tương lai (mã KRX 41I1… hoặc VN30FYYMM) ⇒ `<out>/ohlc1m/<VN30FYYMM>/<YYYY-MM-DD>.parquet`; mã khác (chỉ số,
  vd VN30) ⇒ `<out>/index1m/<MÃ>/<YYYY-MM-DD>.parquet`. Cột: symbol, start, open, high, low, close, volume,
  interval_s=60, volume_reliable=False, source="ssi" (cùng schema kho DNSE ⇒ `load_bars_parquet` đọc được).
- Trùng (mã, phút) giữa các file ⇒ giữ một bản; khác giá ⇒ đếm `conflicts`. Nến OHLC sai ⇒ bỏ, đếm `skipped`.
- Ngày rơi vào thứ Bảy/Chủ nhật ⇒ lỗi (dấu hiệu đọc nhầm định dạng MM/DD).
- File status khác "ok" ⇒ lỗi (không âm thầm bỏ qua).
"""
import json
from datetime import datetime

import pandas as pd
import pytest

from backtest.engine import load_bars_parquet
from scripts.import_ssi_intraday import ImportError_, import_files, main


def tick(sym, ts, o, h, l, c, v=10):
    return {"symbol": sym, "open": str(o), "high": str(h), "low": str(l), "close": str(c), "volume": str(v),
            "trading_date": ts, "ref_price": "0", "avg_price": "0", "price_changed": "0"}


def raw(path, ticks, status="ok"):
    path.write_text(json.dumps({"status": status, "data": {"ticks": ticks}, "error": None}), encoding="utf-8")
    return path


def test_futures_and_index_go_to_separate_stores(tmp_path):
    f1 = raw(tmp_path / "a.json", [tick("41I1F8000", "23/07/2025 09:01:00", 1661, 1662, 1660, 1661.5),
                                   tick("41I1F8000", "23/07/2025 09:00:00", 1661, 1663, 1660.5, 1661.4, 4125)])
    f2 = raw(tmp_path / "b.json", [tick("VN30", "13/11/2026 09:15:00", 1833.32, 1835.16, 1833.32, 1834.11, 1948962)])
    out = tmp_path / "ssi"
    st = import_files([f1, f2], out)
    fut = pd.read_parquet(out / "ohlc1m" / "VN30F2508" / "2025-07-23.parquet")
    assert list(fut["start"]) == [datetime(2025, 7, 23, 9, 0), datetime(2025, 7, 23, 9, 1)]   # sắp theo giờ
    assert fut["symbol"].unique().tolist() == ["VN30F2508"]
    assert fut.iloc[0][["open", "high", "low", "close", "volume"]].tolist() == [1661.0, 1663.0, 1660.5, 1661.4, 4125]
    assert set(fut["volume_reliable"]) == {False} and set(fut["source"]) == {"ssi"} and set(fut["interval_s"]) == {60}
    idx = pd.read_parquet(out / "index1m" / "VN30" / "2026-11-13.parquet")
    assert idx["symbol"].tolist() == ["VN30"] and idx["close"].tolist() == [1834.11]
    assert st["bars"] == 3 and st["files_written"] == 2 and st["skipped"] == 0 and st["conflicts"] == 0
    bars, skipped = load_bars_parquet(out / "ohlc1m")
    assert len(bars) == 2 and skipped == 0 and bars[0].symbol == "VN30F2508"


def test_duplicates_and_conflicts(tmp_path):
    t = tick("41I1F8000", "23/07/2025 09:00:00", 1661, 1663, 1660.5, 1661.4)
    f1 = raw(tmp_path / "a.json", [t])
    f2 = raw(tmp_path / "b.json", [t, tick("41I1F8000", "23/07/2025 09:00:00", 1661, 1663, 1660.5, 1662.0)])
    st = import_files([f1, f2], tmp_path / "ssi")
    assert st["bars"] == 1 and st["duplicates"] == 2 and st["conflicts"] == 1
    df = pd.read_parquet(tmp_path / "ssi" / "ohlc1m" / "VN30F2508" / "2025-07-23.parquet")
    assert df["close"].tolist() == [1661.4]                                  # giữ bản đọc trước


def test_bad_bar_skipped(tmp_path):
    f = raw(tmp_path / "a.json", [tick("41I1F8000", "23/07/2025 09:00:00", 1661, 1660, 1662, 1661),   # high < low
                                  tick("41I1F8000", "23/07/2025 09:01:00", 1661, 1662, 1660, "x")])
    st = import_files([f], tmp_path / "ssi")
    assert st["skipped"] == 2 and st["bars"] == 0 and st["files_written"] == 0


def test_weekend_date_means_wrong_format(tmp_path):
    f = raw(tmp_path / "a.json", [tick("41I1F8000", "07/12/2025 09:00:00", 1661, 1662, 1660, 1661)])   # CN 07/12/2025
    with pytest.raises(ImportError_):
        import_files([f], tmp_path / "ssi")


def test_error_status_raises(tmp_path):
    f = raw(tmp_path / "a.json", [], status="error")
    with pytest.raises(ImportError_):
        import_files([f], tmp_path / "ssi")


def test_rewrite_is_idempotent(tmp_path):
    f = raw(tmp_path / "a.json", [tick("41I1F8000", "23/07/2025 09:00:00", 1661, 1663, 1660.5, 1661.4)])
    import_files([f], tmp_path / "ssi")
    import_files([f], tmp_path / "ssi")
    df = pd.read_parquet(tmp_path / "ssi" / "ohlc1m" / "VN30F2508" / "2025-07-23.parquet")
    assert len(df) == 1


def test_cli(tmp_path, capsys):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw(raw_dir / "a.json", [tick("41I1F8000", "23/07/2025 09:00:00", 1661, 1663, 1660.5, 1661.4)])
    assert main([str(raw_dir), "--out", str(tmp_path / "ssi")]) == 0
    assert "1 nến" in capsys.readouterr().out
    assert main([str(tmp_path / "nothing"), "--out", str(tmp_path / "ssi")]) == 2
