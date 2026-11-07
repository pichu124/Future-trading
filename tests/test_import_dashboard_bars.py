import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import import_dashboard_bars as imp  # noqa: E402
from check_calendar import check_year  # noqa: E402


def raw_rows(rows):
    return pd.DataFrame(rows, columns=["ticker", "time", "open", "high", "low", "close", "volume"])


def test_filters_futures_and_resolves_contract_by_date(cal):
    raw = raw_rows([
        ("VN30F1M", "2026-11-09 09:15:00", 1350.0, 1351.0, 1349.5, 1350.5, 0),
        ("FPT", "2026-11-09 09:15:00", 77.7, 78.0, 77.5, 77.9, 1000),
        ("VN30", "2026-11-09 09:15:00", 1340.0, 1341.0, 1339.0, 1340.5, 0),
        ("VN30F2M", "2026-11-09 09:15:00", 1352.0, 1352.0, 1351.0, 1351.5, 0),
    ])
    out = imp.extract_futures_bars(raw, cal)
    assert sorted(out["symbol"]) == ["VN30F2611", "VN30F2612"]
    assert not out["volume_reliable"].any()
    assert set(out["source"]) == {"dashboard"}


def test_after_expiry_f1m_maps_to_next_contract(cal):
    raw = raw_rows([("VN30F1M", "2026-11-20 10:00:00", 1350, 1351, 1349, 1350, 0)])
    assert imp.extract_futures_bars(raw, cal)["symbol"].tolist() == ["VN30F2612"]


def test_fixed_code_preferred_over_rolling_alias(cal):
    raw = raw_rows([
        ("VN30F1M", "2026-11-09 09:15:20", 1350.0, 1350.0, 1350.0, 1350.0, 0),
        ("41I1GB000", "2026-11-09 09:15:40", 1350.3, 1350.3, 1350.3, 1350.3, 0),
    ])
    out = imp.extract_futures_bars(raw, cal)
    assert len(out) == 1
    assert out.loc[0, "source_symbol"] == "41I1GB000" and out.loc[0, "close"] == 1350.3


def test_thousand_scaled_prices_rescaled_and_garbage_dropped(cal):
    rep = imp.DayReport(day=date(2026, 11, 9))
    raw = raw_rows([
        ("VN30F1M", "2026-11-09 09:16:00", 1350200, 1351000, 1349800, 1350500, 0),
        ("VN30F1M", "2026-11-09 09:17:00", 0.0, 0.0, 0.0, 0.0, 0),
    ])
    out = imp.extract_futures_bars(raw, cal, rep)
    assert out["close"].tolist() == [1350.5]
    assert bool(out.loc[0, "price_rescaled"])
    assert rep.rescaled == 1 and rep.dropped_price == 1


def test_out_of_session_minutes_dropped(cal):
    rep = imp.DayReport(day=date(2026, 11, 9))
    raw = raw_rows([
        ("VN30F1M", "2026-11-09 08:40:00", 1350, 1350, 1350, 1350, 0),
        ("VN30F1M", "2026-11-09 14:45:30", 1350, 1350, 1350, 1350, 0),
        ("VN30F1M", "2026-11-09 15:10:00", 1350, 1350, 1350, 1350, 0),
    ])
    out = imp.extract_futures_bars(raw, cal, rep)
    assert len(out) == 1 and rep.dropped_time == 2


def test_inconsistent_snapshot_bar_widened(cal):
    raw = raw_rows([("VN30F1M", "2026-11-09 09:20:00", 1350.0, 1349.0, 1349.5, 1348.0, 0)])
    row = imp.extract_futures_bars(raw, cal).iloc[0]
    assert row["high"] == 1350.0 and row["low"] == 1348.0


def test_empty_and_no_futures_inputs(cal):
    assert imp.extract_futures_bars(pd.DataFrame(), cal).empty
    raw = raw_rows([("FPT", "2026-11-09 09:15:00", 77.7, 78.0, 77.5, 77.9, 1000)])
    assert imp.extract_futures_bars(raw, cal).empty


def test_end_to_end_idempotent(tmp_path, cal, monkeypatch):
    wh = tmp_path / "data_warehouse"
    (wh / "_live" / "archive").mkdir(parents=True)
    raw_rows([
        ("VN30F1M", datetime(2026, 11, 9, 9, 15), 1350.0, 1351.0, 1349.5, 1350.5, 0),
        ("VN30F1M", datetime(2026, 11, 9, 9, 16), 1350.5, 1352.0, 1350.0, 1351.5, 0),
    ]).to_parquet(wh / "_live" / "archive" / "bars_2026-11-09.parquet")
    raw_rows([("VN30F1M", datetime(2026, 11, 10, 9, 15), 1355.0, 1356.0, 1354.0, 1355.5, 0)]
             ).to_parquet(wh / "_live" / "bars_2026-11-10.parquet")
    (wh / "_live" / "quote_VN30F1M.json").write_text("{}")
    out = tmp_path / "imported"
    monkeypatch.setattr(imp.TradingCalendar, "default", classmethod(lambda cls: cal))

    files = imp.find_source_files(wh)
    assert sorted(files) == [date(2026, 11, 9), date(2026, 11, 10)]
    assert imp.main(["--warehouse", str(wh), "--out", str(out)]) == 0
    first = pd.read_parquet(out / "2026-11-09.parquet")
    assert imp.main(["--warehouse", str(wh), "--out", str(out)]) == 0
    pd.testing.assert_frame_equal(first, pd.read_parquet(out / "2026-11-09.parquet"))
    assert sorted(p.name for p in out.iterdir()) == ["2026-11-09.parquet", "2026-11-10.parquet"]


def test_dry_run_writes_nothing(tmp_path, cal, monkeypatch):
    wh = tmp_path / "wh"
    (wh / "_live").mkdir(parents=True)
    raw_rows([("VN30F1M", datetime(2026, 11, 9, 9, 15), 1350, 1351, 1349, 1350, 0)]
             ).to_parquet(wh / "_live" / "bars_2026-11-09.parquet")
    monkeypatch.setattr(imp.TradingCalendar, "default", classmethod(lambda cls: cal))
    out = tmp_path / "imported"
    assert imp.main(["--warehouse", str(wh), "--out", str(out), "--dry-run"]) == 0
    assert not out.exists()


def test_missing_warehouse_returns_error(tmp_path):
    assert imp.main(["--warehouse", str(tmp_path / "nope")]) == 2


def test_check_calendar_flags_sparse_year_and_weekend():
    issues = check_year(2027, {date(2027, 1, 1): "Tết DL", date(2027, 5, 1): "thứ Bảy"})
    assert any("chỉ có 1" in i for i in issues)
    assert any("cuối tuần" in i for i in issues)
