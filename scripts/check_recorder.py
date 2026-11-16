"""Canh recorder từ BÊN NGOÀI — Task Scheduler gọi định kỳ trong giờ giao dịch (docs/recorder_windows.md §2b).

Recorder không tự báo được khi nó không chạy (12/11/2026: task bị Windows chặn vì laptop chạy pin, không ai biết).
Script này chỉ đọc thư mục `data/raw/<hôm nay>`:

- không phải ngày giao dịch / ngoài phiên LIÊN TỤC ⇒ OK, không kiểm;
- không có file parquet nào hôm nay ⇒ CẢNH BÁO "chưa ghi";
- file mới nhất cũ hơn `recorder.watch_max_age_seconds` (tính từ giờ mở phiên nếu file cũ hơn — sát 09:00/13:00
  tick có thể chưa về) ⇒ CẢNH BÁO "ngừng ghi";
- cảnh báo ⇒ Telegram kênh `trading`, thoát 1. OK ⇒ im lặng (`--notify-ok` để thử kênh), thoát 0.

    python scripts/check_recorder.py              # Task Scheduler: T2–T6 mỗi 30′ từ 09:10 tới 14:10
    python scripts/check_recorder.py --notify-ok  # thử: gửi cả khi OK
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.calendar import TradingCalendar  # noqa: E402
from core.clock import SessionSchedule, SessionState  # noqa: E402
from monitoring.telegram import send_message  # noqa: E402

log = logging.getLogger("check_recorder")
VN = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class Status:
    ok: bool
    checked: bool
    text: str


def vn_epoch(t: datetime) -> float:
    """datetime naive giờ VN → epoch giây."""
    return t.replace(tzinfo=VN).timestamp()


def from_vn_epoch(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, VN).replace(tzinfo=None)


def _session_open(now: datetime, sched: SessionSchedule) -> datetime:
    """Giờ mở của đoạn phiên liên tục đang diễn ra (sáng hoặc chiều)."""
    pm = datetime.combine(now.date(), sched.continuous_pm_start)
    return pm if now >= pm else datetime.combine(now.date(), sched.continuous_am_start)


def check(now: datetime, cal: TradingCalendar, sched: SessionSchedule, data_dir: Path, *,
          max_age_s: float) -> Status:
    if not cal.is_trading_day(now.date()):
        return Status(True, False, f"{now:%Y-%m-%d} không phải ngày giao dịch — không kiểm")
    if sched.state_at(now.time()) is not SessionState.CONTINUOUS:
        return Status(True, False, f"{now:%H:%M} ngoài phiên liên tục — không kiểm")
    day_dir = Path(data_dir) / "raw" / now.date().isoformat()
    files = sorted(day_dir.glob("*.parquet")) if day_dir.is_dir() else []
    if not files:
        return Status(False, True, f"⚠️ Recorder CHƯA ghi hôm nay ({now:%Y-%m-%d}) — {now:%H:%M} không có file trong "
                                   f"{day_dir}. Kiểm Task Scheduler (VN30F Recorder) / chạy tay.")
    last = max(from_vn_epoch(p.stat().st_mtime) for p in files)
    age = (now - max(last, _session_open(now, sched))).total_seconds()
    if age > max_age_s:
        return Status(False, True, f"⚠️ Recorder ngừng ghi: file mới nhất lúc {last:%H:%M:%S} "
                                   f"({(now - last).total_seconds() / 60:.0f} phút trước, ngưỡng {max_age_s:.0f} giây). "
                                   "Xem logs/record_YYYY-MM-DD.jsonl, Task Scheduler.")
    return Status(True, True, f"✅ Recorder đang ghi: {len(files)} file, mới nhất {(now - last).total_seconds():.0f} "
                              "giây trước")


# ---------------------------------------------------------------------- main ----

def _load():
    from core.clock import MarketClock
    from core.settings import load_settings
    s = load_settings()
    mc = MarketClock.from_settings(s)
    return mc.calendar, mc.schedule, s.path("paths.data_dir"), float(s.require("recorder.watch_max_age_seconds"))


def _now() -> datetime:
    from core.clock import to_vn
    return to_vn(datetime.now(VN))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Canh recorder: báo Telegram khi không ghi trong giờ giao dịch")
    ap.add_argument("--notify-ok", action="store_true", help="gửi Telegram cả khi OK (thử kênh)")
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        from core.settings import REPO_ROOT
        load_dotenv(REPO_ROOT / ".env")          # Task Scheduler không đặt cwd = repo
    except ImportError:
        pass
    cal, sched, data_dir, max_age = _load()
    st = check(_now(), cal, sched, data_dir, max_age_s=max_age)
    print(st.text)
    if not st.ok or args.notify_ok:
        try:
            send_message(f"{st.text}\n({socket.gethostname()})", channel="trading")
        except Exception as exc:                  # không văng traceback; Task Scheduler vẫn thấy mã thoát
            log.error("Gửi Telegram thất bại: %s", exc)
    return 0 if st.ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
