"""`scripts/foreign_futures_position.py` — vị thế ròng khối ngoại trên HĐTL VN30, dựng lại từ dòng lệnh. Viết TRƯỚC.

Cách tính giống `vnstock_futures_foreign_view.py` của vnstock-dashboard: vị thế ròng một HĐ = Σ (KL mua − KL bán) của
khối ngoại từ phiên niêm yết; nguồn SSI `marketdata_list_daily_stock_infor` theo mã KRX (nguồn không có OI).
- Mỗi ngày SSI trả 2 dòng giống nhau ⇒ chỉ giữ dòng có `close_raw` ≠ "0".
- Chỉ dùng phiên TRƯỚC ngày D0 (dòng ngày ≥ D0 bị bỏ — không nhìn trước).
- Dòng thiếu trường khối ngoại ⇒ tính 0 và đếm "thiếu".
- HĐ đáo hạn ĐÚNG D0 ⇒ đánh dấu (vị thế tự đóng cuối phiên); in tổng có và không có HĐ đó.
- Một mã trả đúng bội số `page_size` dòng ⇒ cảnh báo có thể còn trang (lịch sử chưa đủ).
- `--plan`: mã KRX của mọi HĐ đang lưu hành ở D0 + toDate = phiên liền trước.
"""
import json
from datetime import date

import pytest

from scripts.foreign_futures_position import compute, main, plan, render


def row(sym, d, buy, sell, raw=True, foreign=True):
    r = {"symbol": sym, "trading_date": d.strftime("%d/%m/%Y"), "close": "1800", "close_raw": "1800" if raw else "0"}
    if foreign:
        r.update(foreign_buy_volume_total=str(buy), foreign_sell_volume_total=str(sell))
    return r


def raw(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text(json.dumps({"status": "ok", "data": {"stocks": rows}}), encoding="utf-8")
    return p


D0 = date(2026, 11, 19)


def test_cumulative_dedupe_and_no_lookahead(tmp_path, cal):
    f = raw(tmp_path, "a.json", [
        row("41I1GC000", date(2026, 11, 19), 999, 0),                # D0 ⇒ bỏ
        row("41I1GC000", date(2026, 11, 18), 10, 30), row("41I1GC000", date(2026, 11, 18), 10, 30, raw=False),
        row("41I1GC000", date(2026, 11, 17), 50, 5),
        row("41I1GC000", date(2026, 11, 16), 0, 0, foreign=False),   # thiếu trường ⇒ 0, đếm thiếu
    ])
    (c,) = compute([f], D0, cal)
    assert c["contract"] == "VN30F2612" and c["position"] == 25 and c["sessions"] == 3 and c["missing"] == 1
    assert c["first"] == date(2026, 11, 16) and c["last"] == date(2026, 11, 18)
    assert c["recent"][-2:] == [(date(2026, 11, 17), 45), (date(2026, 11, 18), -20)]
    assert c["expires_today"] is False and c["maybe_truncated"] is False


def test_expiry_day_flag_and_totals(tmp_path, cal):
    f = raw(tmp_path, "a.json", [row("41I1GB000", date(2026, 11, 18), 0, 100), row("41I1GC000", date(2026, 11, 18), 40, 0)])
    cs = compute([f], D0, cal)
    by = {c["contract"]: c for c in cs}
    assert by["VN30F2611"]["expires_today"] is True and by["VN30F2612"]["expires_today"] is False
    text = render(cs, D0)
    assert "−60" in text or "-60" in text                          # tổng mọi HĐ
    assert "+40" in text and "đáo hạn hôm nay" in text


def test_truncation_warning(tmp_path, cal):
    rows = []
    d = date(2026, 1, 5)
    while len(rows) < 200:
        if cal.is_trading_day(d):
            rows += [row("41I1GC000", d, 1, 0), row("41I1GC000", d, 1, 0, raw=False)]
        d = date.fromordinal(d.toordinal() + 1)
    (c,) = compute([raw(tmp_path, "a.json", rows[:200])], D0, cal, page_size=200)
    assert c["maybe_truncated"] is True
    assert "còn trang" in render([c], D0)
    (c2,) = compute([raw(tmp_path, "p1.json", rows[:200]), raw(tmp_path, "p2.json", rows[200:] + [row("41I1GC000", date(2025, 12, 31), 1, 0)])],
                    D0, cal, page_size=200)
    assert c2["maybe_truncated"] is False                       # đã có trang 2 (tổng 201 dòng) ⇒ không cảnh báo


def test_bad_status_raises(tmp_path, cal):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"status": "error", "data": None}), encoding="utf-8")
    with pytest.raises(ValueError):
        compute([p], D0, cal)


def test_plan_lists_live_contracts(cal):
    calls = plan(date(2026, 11, 20), cal)
    assert [c[0] for c in calls] == ["41I1GC000", "41I1H1000", "41I1H3000", "41I1H6000"]
    assert all(c[1] == "1y" and c[2] == "19/11/2026" for c in calls)


def test_cli(tmp_path, capsys, cal, monkeypatch):
    from scripts import foreign_futures_position as mod
    monkeypatch.setattr(mod.TradingCalendar, "default", classmethod(lambda cls: cal))
    raw(tmp_path, "a.json", [row("41I1GC000", date(2026, 11, 18), 40, 0)])
    assert main([str(tmp_path), "--date", "2026-11-19"]) == 0
    assert "VN30F2612" in capsys.readouterr().out
    assert main(["--plan", "--date", "2026-11-20"]) == 0
    assert "41I1H1000 1y 19/11/2026" in capsys.readouterr().out
