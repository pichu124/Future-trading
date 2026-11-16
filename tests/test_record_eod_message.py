"""Recorder gửi báo cáo cuối ngày lên Telegram (`scripts/record_today.py`). Viết TRƯỚC khi implement.

- `eod_message(day, code, vcode, rep, error)`: một tin ngắn — ✅ khi feed êm (code 0) và kiểm định không có vấn đề
  (vcode 0); ⚠️ khi feed dừng bất thường / flush lỗi (code 1), kiểm định có vấn đề (vcode 1), không có dữ liệu
  (vcode 2) hoặc kiểm định ném lỗi. Mỗi nguồn: số tick, giờ đầu→cuối, số khoảng trống (tổng giây), hụt KL, số bar;
  tối đa 5 dòng vấn đề (còn lại ghi "+N").
- `finish(day, code, settings, validate, notify)`: chạy kiểm định (lỗi ⇒ không văng), log bản đầy đủ, gửi tin (nếu
  `recorder.notify_end_of_day`), trả mã thoát của recorder (không đổi vì kiểm định).
"""
from datetime import date, datetime

from core.settings import Settings
from recorder.validate import DayReport, Gap, SourceReport
from scripts.record_today import eod_message, finish

DAY = date(2026, 11, 12)


def rep(issues=(), missing=0, gaps=1):
    s = SourceReport(n_ticks=865_246, n_trades=120_000, n_quotes=745_246,
                     first_ts=datetime(2026, 11, 12, 8, 45, 1), last_ts=datetime(2026, 11, 12, 14, 45, 3),
                     gaps=[Gap(datetime(2026, 11, 12, 10, 0), datetime(2026, 11, 12, 10, 0, 12))] * gaps,
                     volume={"checked": True, "missing": missing})
    r = DayReport(day=DAY, sources={"dnse": s}, bars={"dnse": [object()] * 271})
    r.issues = list(issues)
    return r


def test_ok_message():
    m = eod_message(DAY, 0, 0, rep(), None)
    assert m.startswith("✅ Recorder 2026-11-12")
    assert "865,246 tick" in m and "08:45:01→14:45:03" in m and "1 khoảng trống (12s)" in m
    assert "hụt KL 0" in m and "271 bar" in m


def test_feed_died_is_warning():
    m = eod_message(DAY, 1, 0, rep(), None)
    assert m.startswith("⚠️") and "feed dừng bất thường" in m


def test_validation_issues_listed_and_capped():
    issues = [f"vấn đề {i}" for i in range(8)]
    m = eod_message(DAY, 0, 1, rep(issues=issues, missing=429), None)
    assert m.startswith("⚠️") and "hụt KL 429" in m
    assert "vấn đề 4" in m and "vấn đề 5" not in m and "+3" in m


def test_no_data_and_validation_error():
    assert "không có dữ liệu" in eod_message(DAY, 0, 2, DayReport(day=DAY), None)
    m = eod_message(DAY, 0, None, None, "OSError: đĩa đầy")
    assert m.startswith("⚠️") and "đĩa đầy" in m


def settings(notify=True):
    return Settings({"recorder": {"notify_end_of_day": notify}})


def test_finish_sends_and_keeps_recorder_exit_code():
    sent = []
    assert finish(DAY, 0, settings(), validate=lambda d, s: (1, rep(issues=["x"])), notify=sent.append) == 0
    assert len(sent) == 1 and sent[0].startswith("⚠️")


def test_finish_validation_exception_does_not_raise():
    sent = []

    def boom(d, s):
        raise OSError("đĩa đầy")
    assert finish(DAY, 1, settings(), validate=boom, notify=sent.append) == 1
    assert "đĩa đầy" in sent[0]


def test_finish_respects_setting_off():
    sent = []
    assert finish(DAY, 0, settings(False), validate=lambda d, s: (0, rep()), notify=sent.append) == 0
    assert sent == []


def test_finish_notify_failure_does_not_raise():
    def bad(text):
        raise RuntimeError("mất mạng")
    assert finish(DAY, 0, settings(), validate=lambda d, s: (0, rep()), notify=bad) == 0


def test_repo_setting():
    from core.settings import load_settings
    assert load_settings().require("recorder.notify_end_of_day") is True
