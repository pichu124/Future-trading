"""`scripts/download_today.py` — Task Scheduler chạy ~15:30 mỗi ngày giao dịch. Viết TRƯỚC khi implement.

Bối cảnh: runner paper nghiêm ngặt (thiếu một phiên lịch sử ⇒ thoát 2, không giao dịch); trước đây phải nhớ chạy tay
`download_history.py` mỗi tối, kể cả ngày trước roll (18/11/2026 cần thêm VN30F2612).

- Phiên tải cuối = hôm nay nếu là ngày giao dịch và đã qua 14:50; còn lại (chạy bù buổi sáng / ngày nghỉ) = phiên
  giao dịch TRƯỚC hôm nay. Kiểm lịch sử cho phiên giao dịch KẾ TIẾP sau phiên tải cuối.
- Tải khoảng `catchup_days` phiên gần nhất tới phiên tải cuối (file đã có được bỏ qua ⇒ tự bù ngày máy tắt);
  `plan_days` của downloader đã thêm HĐ kế tiếp ngày trước roll.
- Sau khi tải: kiểm lịch sử đủ cho PHIÊN KẾ TIẾP bằng đúng hàm runner dùng (`live.history.load_live_history`).
- Có lỗi tải / hết hạn mức / lịch sử chưa đủ ⇒ Telegram kênh `trading` + thoát ≠ 0 (hạn mức: 3, còn lại: 1).
  Ổn ⇒ im lặng (trừ `--notify-ok`), thoát 0.
"""
from datetime import date, datetime, time

import pytest

from core.calendar import TradingCalendar
from scripts import download_today as D

CAL = TradingCalendar.from_dates([date(2026, 11, 13)])           # giả: thứ Sáu 13/11 nghỉ


class Rig:
    def __init__(self, summary=None, problem=None):
        self.summary = summary if summary is not None else {"written": 2, "empty": 0, "skipped": 8, "errors": 0}
        self.problem = problem
        self.downloads, self.checks, self.sent = [], [], []

    def downloader(self, start, end):
        self.downloads.append((start, end))
        return dict(self.summary)

    def checker(self, day):
        self.checks.append(day)
        return self.problem

    def notify(self, text):
        self.sent.append(text)

    def run(self, now, **kw):
        return D.run(now, CAL, downloader=self.downloader, checker=self.checker, notify=self.notify, **kw)


def test_catchup_start_counts_trading_days_back():
    assert D.catchup_start(date(2026, 11, 12), CAL, 5) == date(2026, 11, 6)      # 12, 11, 10, 9, 6
    assert D.catchup_start(date(2026, 11, 16), CAL, 2) == date(2026, 11, 12)     # bỏ T7, CN, 13/11 nghỉ


def test_morning_catchup_before_close_downloads_until_previous_session():
    """Laptop tắt lúc 15:30 ⇒ task chạy bù sáng hôm sau: tải tới phiên TRƯỚC, kiểm lịch sử cho phiên HÔM NAY."""
    r = Rig()
    assert r.run(datetime(2026, 11, 12, 8, 5)) == 0
    assert r.downloads == [(date(2026, 11, 5), date(2026, 11, 11))] and r.checks == [date(2026, 11, 12)]


def test_non_trading_day_catchup_until_last_session():
    """Chạy bù vào ngày nghỉ (13/11 nghỉ, T7, CN) ⇒ tải tới 12/11, kiểm cho thứ Hai 16/11."""
    for now in (datetime(2026, 11, 13, 15, 30), datetime(2026, 11, 14, 9, 0)):
        r = Rig()
        assert r.run(now) == 0
        assert r.downloads == [(date(2026, 11, 6), date(2026, 11, 12))] and r.checks == [date(2026, 11, 16)]


def test_normal_day_downloads_window_checks_next_session_silently():
    r = Rig()
    assert r.run(datetime(2026, 11, 12, 15, 30)) == 0
    assert r.downloads == [(date(2026, 11, 6), date(2026, 11, 12))]
    assert r.checks == [date(2026, 11, 16)]                    # 13/11 nghỉ ⇒ phiên kế tiếp thứ Hai 16/11
    assert r.sent == []


def test_notify_ok_sends_summary():
    r = Rig()
    assert r.run(datetime(2026, 11, 12, 15, 30), notify_ok=True) == 0
    assert len(r.sent) == 1 and "✅" in r.sent[0] and "2026-11-16" in r.sent[0]


def test_download_errors_alert():
    r = Rig(summary={"written": 1, "empty": 0, "skipped": 0, "errors": 2})
    assert r.run(datetime(2026, 11, 12, 15, 30)) == 1
    assert len(r.sent) == 1 and "2 lỗi" in r.sent[0]


def test_quota_exhausted_alert_exit_3():
    r = Rig(summary={"written": 0, "empty": 0, "skipped": 0, "errors": 0, "stopped": "hết hạn mức, reset 20:00"})
    assert r.run(datetime(2026, 11, 12, 15, 30)) == 3
    assert "hạn mức" in r.sent[0]


def test_history_not_ready_alert_with_reason():
    r = Rig(problem="Lịch sử 10 phiên chưa đủ: 2026-11-11: thiếu bar VN30F2611")
    assert r.run(datetime(2026, 11, 12, 15, 30)) == 1
    assert "thiếu bar VN30F2611" in r.sent[0] and "2026-11-16" in r.sent[0]


def test_checker_exception_is_reported_not_raised():
    r = Rig()

    def boom(day):
        raise OSError("đĩa đầy")
    assert D.run(datetime(2026, 11, 12, 15, 30), CAL, downloader=r.downloader, checker=boom, notify=r.notify) == 1
    assert "đĩa đầy" in r.sent[0]


def test_downloader_exception_is_reported_not_raised():
    r = Rig()

    def boom(start, end):
        raise ConnectionError("mất mạng")
    assert D.run(datetime(2026, 11, 12, 15, 30), CAL, downloader=boom, checker=r.checker, notify=r.notify) == 1
    assert "mất mạng" in r.sent[0]


def test_roll_eve_plan_includes_next_contract():
    """18/11/2026 = ngày trước roll (VN30F2611 đáo hạn 19/11) ⇒ downloader tải cả VN30F2612."""
    from scripts.download_history import plan_days
    names = [n for d, n, _ in plan_days(date(2026, 11, 18), date(2026, 11, 18), TradingCalendar.default(), 1)]
    assert names == ["VN30F2611", "VN30F2612"]


def test_repo_settings():
    from core.settings import load_settings
    s = load_settings()
    assert s.require("history_download.catchup_days") == 5 and s.require("history_download.history_days") == 10
    assert s.require("history_download.not_before") == "14:50"


def test_main_wires_downloader_and_runner_history_check(monkeypatch, tmp_path):
    """main: thiếu khoá ⇒ 2 + cảnh báo; có khoá ⇒ downloader gọi download(plan_days…), checker dùng load_live_history
    (thư mục rỗng ⇒ trả lý do, không ném)."""
    import scripts.download_history as H
    from core.settings import Settings, load_settings
    base = load_settings()
    data = {**base._data, "paths": {**base._data["paths"], "data_dir": str(tmp_path)},
            "live": {**base._data["live"], "history_bar_dirs": [str(tmp_path / "nohist")]}}
    monkeypatch.setattr("core.settings.load_settings", lambda *a, **k: Settings(data))
    sent = []
    monkeypatch.setattr("monitoring.telegram.notify", lambda text, *a, **k: sent.append(text) or True)
    monkeypatch.delenv("DNSE_API_KEY", raising=False)
    monkeypatch.delenv("DNSE_API_SECRET", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None, raising=False)
    assert D.main([]) == 2 and "DNSE_API_KEY" in sent[0]

    monkeypatch.setenv("DNSE_API_KEY", "k")
    monkeypatch.setenv("DNSE_API_SECRET", "s")
    calls = {}
    monkeypatch.setattr(H, "download", lambda client, plan, out, **kw: calls.update(plan=list(plan), out=out)
                        or {"written": 0, "skipped": 0, "errors": 0})
    captured = {}
    monkeypatch.setattr(D, "run", lambda now, cal, **kw: captured.update(kw) or 0)
    assert D.main([]) == 0
    captured["downloader"](date(2026, 11, 18), date(2026, 11, 18))
    assert [n for _, n, _ in calls["plan"]] == ["VN30F2611", "VN30F2612"] and str(calls["out"]).endswith("history")
    why = captured["checker"](date(2026, 11, 19))
    assert why and "lịch sử" in why.lower()
    assert captured["catchup_days"] == 5 and captured["not_before"] == time(14, 50)
