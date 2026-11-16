"""`scripts/check_recorder.py` — canh recorder (chạy định kỳ bằng Task Scheduler). Viết TRƯỚC khi implement.

Bối cảnh: 12/11/2026 task recorder bị Windows chặn (laptop chạy pin) — không ai biết tới 09:10. Recorder không thể
tự báo khi nó không chạy ⇒ cần một tiến trình RIÊNG kiểm từ bên ngoài.

- Không phải ngày giao dịch, hoặc ngoài phiên LIÊN TỤC (trước 09:00, nghỉ trưa, ATC, sau giờ) ⇒ OK, không kiểm
  (ngoài phiên liên tục có thể không có tick).
- Trong phiên liên tục: thư mục `data/raw/<hôm nay>` không có file parquet ⇒ CẢNH BÁO "CHƯA ghi";
  file mới nhất cũ hơn `recorder.watch_max_age_seconds` ⇒ CẢNH BÁO "ngừng ghi" (kèm giờ file cuối);
  còn lại ⇒ OK (số file, tuổi file mới nhất).
- `main`: cảnh báo ⇒ gửi Telegram kênh `trading` và thoát 1; OK ⇒ không gửi (trừ `--notify-ok`), thoát 0.
  Gửi Telegram lỗi ⇒ vẫn thoát 1 (Task Scheduler ghi lại), không văng traceback.
"""
import os
from datetime import date, datetime, time, timedelta

import pytest

from core.calendar import TradingCalendar
from core.clock import SessionSchedule
from scripts import check_recorder as C

DAY = date(2026, 11, 12)                  # thứ Năm
SCHED = SessionSchedule.from_mapping({"pre_open_start": "08:30", "ato_start": "08:45", "continuous_am_start": "09:00",
                                      "lunch_start": "11:30", "continuous_pm_start": "13:00", "atc_start": "14:30",
                                      "close": "14:45"})
CAL = TradingCalendar.from_dates([date(2026, 11, 13)])          # giả: thứ Sáu 13/11 nghỉ


def at(hh, mm, ss=0, d=DAY):
    return datetime.combine(d, time(hh, mm, ss))


def part(root, written: datetime, name="part-x-000001.parquet", d=DAY):
    p = root / "raw" / d.isoformat() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    ts = C.vn_epoch(written)
    os.utime(p, (ts, ts))
    return p


def check(tmp_path, now, max_age=180):
    return C.check(now, CAL, SCHED, tmp_path, max_age_s=max_age)


@pytest.mark.parametrize("now", [at(8, 50), at(11, 45), at(14, 35), at(15, 10), at(10, 0, d=date(2026, 11, 13)),
                                 at(10, 0, d=date(2026, 11, 14))])
def test_outside_continuous_or_holiday_is_ok_without_checking(tmp_path, now):
    st = check(tmp_path, now)                                   # không có thư mục nào mà vẫn OK
    assert st.ok and not st.checked


def test_no_data_today_alerts(tmp_path):
    part(tmp_path, at(14, 59), d=date(2026, 11, 11))            # có hôm qua, không có hôm nay
    st = check(tmp_path, at(9, 10))
    assert not st.ok and st.checked and "CHƯA ghi" in st.text and "2026-11-12" in st.text


def test_empty_day_dir_alerts(tmp_path):
    (tmp_path / "raw" / DAY.isoformat()).mkdir(parents=True)
    assert not check(tmp_path, at(9, 10)).ok


def test_fresh_file_ok(tmp_path):
    part(tmp_path, at(9, 9, 50), "part-a-000001.parquet")
    part(tmp_path, at(9, 9, 55), "part-a-000002.parquet")
    st = check(tmp_path, at(9, 10))
    assert st.ok and st.checked and "2 file" in st.text and "5 giây" in st.text


def test_stale_file_alerts_with_last_write_time(tmp_path):
    part(tmp_path, at(9, 30, 0))
    st = check(tmp_path, at(9, 33, 1), max_age=180)             # 181 giây
    assert not st.ok and "ngừng ghi" in st.text and "09:30:00" in st.text
    assert check(tmp_path, at(9, 33, 0), max_age=180).ok          # đúng 180 giây vẫn OK


def test_after_lunch_morning_file_is_stale(tmp_path):
    """Recorder chết lúc nghỉ trưa ⇒ 13:10 thấy file cuối 11:30 ⇒ cảnh báo."""
    part(tmp_path, at(11, 30))
    assert not check(tmp_path, at(13, 10)).ok


def test_grace_right_after_session_opens(tmp_path):
    """Sát giờ mở phiên (sáng/chiều) tick có thể chưa về ⇒ tuổi tính từ giờ mở nếu file cũ hơn (không báo nhầm)."""
    part(tmp_path, at(8, 46))
    assert check(tmp_path, at(9, 2)).ok and not check(tmp_path, at(9, 3, 1)).ok
    part(tmp_path, at(11, 29, 58))
    assert check(tmp_path, at(13, 2)).ok and not check(tmp_path, at(13, 3, 1)).ok


def test_ignores_non_parquet_and_tmp(tmp_path):
    part(tmp_path, at(9, 9, 55), "ghi-chu.txt")
    part(tmp_path, at(9, 9, 55), "part-a-000003.parquet.tmp")
    assert not check(tmp_path, at(9, 10)).ok


def test_vn_epoch_roundtrip():
    assert C.from_vn_epoch(C.vn_epoch(at(9, 10, 5))) == at(9, 10, 5)


# ---------------------------------------------------------------------- main ----

def _main(monkeypatch, tmp_path, now, argv=(), send_ok=True):
    sent = []
    monkeypatch.setattr(C, "_now", lambda: now)
    monkeypatch.setattr(C, "_load", lambda: (CAL, SCHED, tmp_path, 180.0))

    def fake_send(text, channel="trading", **kw):
        sent.append((channel, text))
        if not send_ok:
            raise RuntimeError("mất mạng")
    monkeypatch.setattr(C, "send_message", fake_send)
    return C.main(list(argv)), sent


def test_main_alert_sends_telegram_and_exits_1(monkeypatch, tmp_path):
    code, sent = _main(monkeypatch, tmp_path, at(9, 10))
    assert code == 1 and len(sent) == 1 and sent[0][0] == "trading" and "Recorder" in sent[0][1]


def test_main_ok_is_silent_unless_notify_ok(monkeypatch, tmp_path):
    part(tmp_path, at(9, 9, 55))
    code, sent = _main(monkeypatch, tmp_path, at(9, 10))
    assert code == 0 and sent == []
    code, sent = _main(monkeypatch, tmp_path, at(9, 10), ["--notify-ok"])
    assert code == 0 and len(sent) == 1 and "đang ghi" in sent[0][1]


def test_main_telegram_failure_still_exits_1(monkeypatch, tmp_path, caplog):
    code, _ = _main(monkeypatch, tmp_path, at(9, 10), send_ok=False)
    assert code == 1 and "Telegram" in caplog.text


def test_repo_setting_present():
    from core.settings import load_settings
    assert load_settings().require("recorder.watch_max_age_seconds") == 180
