"""`scripts/ssi_archive_plan.py` — danh sách lệnh tải SSI cho kho lưu trữ hằng tuần. Viết TRƯỚC khi implement.

- Khung = 7 ngày lịch kết thúc ở `to` (đúng phạm vi `range=1w` của tool SSI).
- Mã cần tải: chỉ số VN30 + mã KRX của mọi HĐ là F1M hoặc F2M ở BẤT KỲ ngày giao dịch nào trong khung (phủ ngày đáo
  hạn, ngày trước roll và HĐ mới sau roll). Mỗi mã một lệnh `range=1w`, `toDate` = `to` dạng DD/MM/YYYY.
"""
from datetime import date

from scripts.ssi_archive_plan import main, plan


def test_normal_week(cal):
    calls = plan(date(2026, 11, 13), cal)                 # tuần 09–13/11: F1M = 2611, F2M = 2612
    assert calls == [("VN30", "1w", "13/11/2026"), ("41I1GB000", "1w", "13/11/2026"), ("41I1GC000", "1w", "13/11/2026")]


def test_expiry_week_adds_new_second_month(cal):
    syms = [c[0] for c in plan(date(2026, 11, 20), cal)]   # đáo hạn 19/11 ⇒ từ 20/11 F1M = 2612, F2M = 2701
    assert syms == ["VN30", "41I1GB000", "41I1GC000", "41I1H1000"]


def test_week_without_trading_days_only_index(cal):
    assert plan(date(2026, 2, 20), cal)[0][0] == "VN30"


def test_cli_prints_one_call_per_line(capsys, cal, monkeypatch):
    from scripts import ssi_archive_plan as mod
    monkeypatch.setattr(mod.TradingCalendar, "default", classmethod(lambda cls: cal))
    assert main(["--to", "2026-11-13"]) == 0
    assert capsys.readouterr().out.splitlines() == ["VN30 1w 13/11/2026", "41I1GB000 1w 13/11/2026",
                                                    "41I1GC000 1w 13/11/2026"]
