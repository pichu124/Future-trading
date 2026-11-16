"""`scripts/check_history.py` — kiểm tra kho lịch sử đã tải (data/history) trước khi backtest. Viết TRƯỚC.

Đối chiếu với kế hoạch `plan_days` (ngày giao dịch theo lịch + HĐ kế tiếp ngày trước roll):
- mỗi mục (ngày, HĐ): ok (có file nến) / rỗng / lỗi / thiếu;
- ngày có `volume_gap` ≠ 0 hoặc phút bị đánh dấu volume không tin được;
- ngày đáng ngờ: quá ít nến (< `min_bars`) hoặc phiên không phủ (nến đầu sau 09:05 / nến cuối trước 14:25);
- mã thoát 0 khi mọi mục ok và không có ngày đáng ngờ, 1 nếu có vấn đề.
"""
import json
from datetime import date, datetime, timedelta

import pandas as pd

from scripts.check_history import check, main

NOV = "VN30F2611"
D1, D2, D3 = date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)       # thứ Tư–thứ Sáu


def bars(d, first="09:00", last="14:29", sym=NOV, reliable=True):
    t0 = datetime.combine(d, datetime.strptime(first, "%H:%M").time())
    t1 = datetime.combine(d, datetime.strptime(last, "%H:%M").time())
    out, t = [], t0
    while t <= t1:
        if not (datetime.combine(d, datetime.strptime("11:30", "%H:%M").time()) <= t
                < datetime.combine(d, datetime.strptime("13:00", "%H:%M").time())):
            out.append(t)
        t += timedelta(minutes=1)
    return pd.DataFrame({"symbol": sym, "start": out, "open": 1350.0, "high": 1351.0, "low": 1349.0, "close": 1350.0,
                         "volume": 10, "interval_s": 60, "volume_reliable": reliable, "source": "dnse_trades"})


def store(root, d, df, sym=NOV, manifest=None, **entry):
    p = root / "ohlc1m" / sym / f"{d}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p)
    (root / "trades" / sym).mkdir(parents=True, exist_ok=True)
    (root / "trades" / sym / f"{d}.parquet").write_bytes(b"x")
    if manifest is not None:
        manifest[f"ohlc1m/{sym}/{d}"] = {"rows": 1000, "from": "trades", "volume_gap": entry.get("gap", 0),
                                         "unreliable_minutes": entry.get("bad", [])}
        manifest[f"trades/{sym}/{d}"] = {"rows": 1000, "volume_gap": entry.get("gap", 0)}


def write_manifest(root, m):
    (root / "_manifest.json").write_text(json.dumps(m), encoding="utf-8")


def test_all_good(tmp_path, cal):
    m = {}
    for d in (D1, D2, D3):
        store(tmp_path, d, bars(d), manifest=m)
    write_manifest(tmp_path, m)
    rep = check(tmp_path, D1, D3, cal)
    assert rep["expected"] == 3 and rep["ok"] == 3
    assert rep["missing"] == rep["errors"] == rep["empty"] == rep["suspicious"] == rep["gap_days"] == []
    assert rep["passed"] is True


def test_missing_error_empty_classified(tmp_path, cal):
    m = {}
    store(tmp_path, D1, bars(D1), manifest=m)
    m[f"trades/{NOV}/{D2}"] = {"error": "HTTP 500"}
    m[f"trades/{NOV}/{D3}"] = {"rows": 0}
    write_manifest(tmp_path, m)
    rep = check(tmp_path, D1, D3, cal)
    assert rep["ok"] == 1 and rep["errors"] == [f"{D2} {NOV}"] and rep["empty"] == [f"{D3} {NOV}"]
    assert rep["passed"] is False


def test_entirely_missing_day(tmp_path, cal):
    m = {}
    store(tmp_path, D1, bars(D1), manifest=m)
    store(tmp_path, D3, bars(D3), manifest=m)
    write_manifest(tmp_path, m)
    rep = check(tmp_path, D1, D3, cal)
    assert rep["missing"] == [f"{D2} {NOV}"]


def test_gap_and_unreliable_minutes_reported(tmp_path, cal):
    m = {}
    store(tmp_path, D1, bars(D1), manifest=m, gap=12, bad=["09:00", "14:45"])
    write_manifest(tmp_path, m)
    rep = check(tmp_path, D1, D1, cal)
    assert rep["gap_days"] == [f"{D1} {NOV}: thiếu 12 HĐ, phút 09:00, 14:45"]
    assert rep["passed"] is True                                   # thiếu volume ≠ thiếu ngày; chỉ báo


def test_suspicious_short_or_partial_session(tmp_path, cal):
    m = {}
    store(tmp_path, D1, bars(D1, first="10:30"), manifest=m)           # mất đầu phiên
    store(tmp_path, D2, bars(D2, last="13:30"), manifest=m)            # mất cuối phiên
    store(tmp_path, D3, bars(D3).head(50), manifest=m)                 # quá ít nến
    write_manifest(tmp_path, m)
    rep = check(tmp_path, D1, D3, cal)
    assert [s.split(":")[0].split()[0] for s in rep["suspicious"]] == [str(D1), str(D2), str(D3)]
    assert rep["passed"] is False


def test_roll_eve_next_contract_expected(tmp_path, cal):
    """18/11 là ngày trước roll ⇒ cần cả VN30F2612."""
    d = date(2026, 11, 18)
    m = {}
    store(tmp_path, d, bars(d), manifest=m)
    write_manifest(tmp_path, m)
    rep = check(tmp_path, d, d, cal)
    assert rep["expected"] == 2 and rep["missing"] == [f"{d} VN30F2612"]


def test_cli_exit_codes(tmp_path, cal, monkeypatch, capsys):
    monkeypatch.setattr("scripts.check_history.TradingCalendar.default", classmethod(lambda c: cal))
    m = {}
    store(tmp_path, D1, bars(D1), manifest=m)
    write_manifest(tmp_path, m)
    assert main(["--start", str(D1), "--end", str(D1), "--dir", str(tmp_path)]) == 0
    assert main(["--start", str(D1), "--end", str(D2), "--dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "thiếu" in out.lower()


def test_few_bars_but_full_session_span_is_suspicious(tmp_path, cal):
    df = bars(D1).iloc[::5]                                          # 1 nến / 5 phút, vẫn phủ 09:00–14:25
    m = {}
    store(tmp_path, D1, df, manifest=m)
    write_manifest(tmp_path, m)
    rep = check(tmp_path, D1, D1, cal)
    assert len(rep["suspicious"]) == 1 and "nến" in rep["suspicious"][0]


def test_thin_next_contract_on_roll_eve_not_suspicious(tmp_path, cal):
    d = date(2026, 11, 18)
    m = {}
    store(tmp_path, d, bars(d), manifest=m)
    store(tmp_path, d, bars(d, first="10:00", last="10:20", sym="VN30F2612"), sym="VN30F2612", manifest=m)
    write_manifest(tmp_path, m)
    rep = check(tmp_path, d, d, cal)
    assert rep["ok"] == 2 and rep["suspicious"] == [] and rep["passed"] is True
