"""`scripts/check_bar_store.py` — bước 1 đã đăng ký trước cho #7/#8/#9: đếm ngày giao dịch thiếu trong từng cửa sổ của
một kho nến (`<kho>/<HĐ>/<ngày>.parquet`). Viết TRƯỚC khi implement.

- Mỗi ngày giao dịch (theo lịch) trong cửa sổ: thiếu file của HĐ BOT GIAO DỊCH ngày đó ⇒ "thiếu"; ngày trước roll mà
  thiếu file HĐ kế tiếp ⇒ "thiếu ngày trước roll" (không đo được chênh roll ⇒ ~adr_days phiên sau không giao dịch).
- Một cửa sổ thiếu > `max_missing` (10) ngày ⇒ mã thoát 1 (dừng tìm lỗi đường ống, không kết luận).
"""
from datetime import date

import pandas as pd

from core import contracts
from scripts.check_bar_store import check, main


def put(root, sym, d):
    p = root / sym / f"{d.isoformat()}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"x": [1]}).to_parquet(p)


def fill(root, cal, lo, hi, skip=()):
    d = cal.next_trading_day(lo, inclusive=True)
    while d <= hi:
        c = contracts.trading_contract(d, cal, 1)
        if d not in skip:
            put(root, c, d)
        nxt = contracts.trading_contract(cal.next_trading_day(d), cal, 1)
        if nxt != c:
            put(root, nxt, d)
        d = cal.next_trading_day(d)


def test_complete_store(tmp_path, cal):
    fill(tmp_path, cal, date(2026, 3, 2), date(2026, 3, 31))
    (w,) = check(tmp_path, [("A", date(2026, 3, 2), date(2026, 3, 31))], cal)
    assert w["missing"] == [] and w["missing_roll_eve"] == [] and w["sessions"] == 22


def test_missing_days_and_roll_eve(tmp_path, cal):
    fill(tmp_path, cal, date(2026, 3, 2), date(2026, 3, 31), skip=(date(2026, 3, 5),))
    roll_eve = date(2026, 3, 18)                              # VN30F2603 đáo hạn 19/03 ⇒ 18/03 cần cả VN30F2604
    (tmp_path / "VN30F2604" / f"{roll_eve}.parquet").unlink()
    (w,) = check(tmp_path, [("A", date(2026, 3, 2), date(2026, 3, 31))], cal)
    assert w["missing"] == [date(2026, 3, 5)] and w["missing_roll_eve"] == [roll_eve]


def test_exit_code_by_threshold(tmp_path, cal, monkeypatch, capsys):
    from scripts import check_bar_store as mod
    monkeypatch.setattr(mod.TradingCalendar, "default", classmethod(lambda cls: cal))
    days = []
    d = date(2026, 3, 2)
    while len(days) < 11:
        if cal.is_trading_day(d):
            days.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    fill(tmp_path, cal, date(2026, 3, 2), date(2026, 3, 31), skip=tuple(days[:10]))
    assert main([str(tmp_path), "--window", "A=2026-03-02:2026-03-31"]) == 0
    fill(tmp_path, cal, date(2026, 4, 1), date(2026, 4, 30), skip=())
    for p in tmp_path.glob(f"*/{days[10]}.parquet"):
        p.unlink()
    assert main([str(tmp_path), "--window", "A=2026-03-02:2026-03-31"]) == 1
    assert "11" in capsys.readouterr().out
