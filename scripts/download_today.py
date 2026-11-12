"""Tải lịch sử DNSE của hôm nay (và bù vài phiên gần nhất) rồi kiểm lịch sử đủ cho phiên kế tiếp — Task Scheduler
chạy ~15:30 mỗi ngày giao dịch (docs/runner.md "Tải lịch sử tự động").

- Phiên tải cuối = hôm nay nếu là ngày giao dịch và đã qua `history_download.not_before` (14:50); còn lại (Task
  Scheduler chạy bù sáng hôm sau vì máy tắt lúc 15:30, hoặc ngày nghỉ) = phiên giao dịch trước hôm nay.
- Tải `history_download.catchup_days` phiên gần nhất tới phiên tải cuối qua `scripts/download_history.py` (file đã có bỏ qua
  ⇒ tự bù ngày máy tắt; ngày trước roll tự thêm HĐ kế tiếp).
- Kiểm lịch sử `history_download.history_days` phiên cho phiên KẾ TIẾP sau phiên tải cuối bằng đúng hàm runner dùng
  (`live.history.load_live_history`) ⇒ sáng mai runner không bị thoát vì thiếu dữ liệu.
- Lỗi tải / hết hạn mức / lịch sử chưa đủ ⇒ Telegram `trading` + thoát 1 (hạn mức: 3). Ổn ⇒ im lặng, thoát 0.

    python scripts/download_today.py               # Task Scheduler
    python scripts/download_today.py --notify-ok   # thử: gửi cả khi ổn
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.calendar import TradingCalendar  # noqa: E402

log = logging.getLogger("download_today")


def catchup_start(today: date, cal: TradingCalendar, n: int) -> date:
    """Ngày giao dịch đầu tiên của cửa sổ `n` phiên kết thúc ở `today` (gồm `today`)."""
    d = today
    for _ in range(max(1, int(n)) - 1):
        d = cal.previous_trading_day(d)
    return d


def run(now: datetime, cal: TradingCalendar, *, downloader: Callable[[date, date], dict],
        checker: Callable[[date], Optional[str]], notify: Callable[[str], None], notify_ok: bool = False,
        catchup_days: int = 5, not_before: time = time(14, 50)) -> int:
    today = now.date()
    if cal.is_trading_day(today) and now.time() >= not_before:
        last = today
    else:                      # chạy bù buổi sáng (máy tắt lúc 15:30) / ngày nghỉ ⇒ tới phiên trước
        last = cal.previous_trading_day(today)
        log.info("Phiên hôm nay chưa xong / ngày nghỉ ⇒ tải tới phiên %s", last)
    start, nxt = catchup_start(last, cal, catchup_days), cal.next_trading_day(last)
    problems: list[str] = []
    code = 0
    try:
        s = downloader(start, last)
    except Exception as exc:                       # không văng: Task Scheduler + Telegram vẫn biết
        s = {}
        problems.append(f"tải lỗi: {type(exc).__name__}: {exc}")
    if s.get("stopped"):
        problems.append(f"dừng vì hạn mức DNSE: {s['stopped']} — chạy lại sau mốc reset")
        code = 3
    if s.get("errors"):
        problems.append(f"{s['errors']} lỗi khi tải (xem log)")
    try:
        why = checker(nxt)
    except Exception as exc:
        why = f"kiểm lịch sử lỗi: {type(exc).__name__}: {exc}"
    if why:
        problems.append(f"lịch sử cho phiên {nxt} CHƯA đủ: {why}")
    head = f"tải lịch sử {start}→{last}"
    if problems:
        notify(f"⚠️ {head}: " + "; ".join(problems))
        return code or 1
    if notify_ok:
        notify(f"✅ {head}: mới {s.get('written', 0)}, bỏ qua {s.get('skipped', 0)}; lịch sử cho phiên {nxt} đủ")
    return 0


# ---------------------------------------------------------------------- main ----

def _hhmm(v) -> time:
    hh, mm = str(v).split(":")
    return time(int(hh), int(mm))


def main(argv=None) -> int:
    from core.clock import MarketClock, to_vn
    from core.continuous import check_gap_minutes
    from core.settings import REPO_ROOT, load_settings
    from live.history import HistoryUnavailable, load_live_history
    from monitoring.telegram import notify as tg_notify
    from scripts.download_history import HistoryClient, download, plan_days

    ap = argparse.ArgumentParser(description="Tải lịch sử hôm nay + kiểm đủ cho phiên kế tiếp")
    ap.add_argument("--notify-ok", action="store_true", help="gửi Telegram cả khi ổn (thử kênh)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")              # Task Scheduler không đặt cwd = repo
    except ImportError:
        pass
    settings = load_settings()
    mclock = MarketClock.from_settings(settings)
    cal = mclock.calendar
    host = socket.gethostname()

    def notify(text: str) -> None:
        print(text)
        tg_notify(f"{text}\n({host})", "trading", parse_mode="")

    key, secret = os.environ.get("DNSE_API_KEY"), os.environ.get("DNSE_API_SECRET")
    if not key or not secret:
        notify("⚠️ tải lịch sử: thiếu DNSE_API_KEY / DNSE_API_SECRET trong .env")
        return 2
    out = settings.path("paths.data_dir") / "history"
    roll = int(settings.get("contract.roll_days_before_expiry", 1))
    client = HistoryClient(key, secret)

    def downloader(start: date, end: date) -> dict:
        return download(client, plan_days(start, end, cal, roll), out)

    dirs = [Path(p) if Path(p).is_absolute() else REPO_ROOT / p for p in settings.require("live.history_bar_dirs")]
    gap = check_gap_minutes(settings.require("contract.roll_gap_median_minutes"))
    days = int(settings.require("history_download.history_days"))

    def checker(day: date) -> Optional[str]:
        try:
            load_live_history(dirs, day, days, cal, mclock.trading_contract, gap_minutes=gap)
        except HistoryUnavailable as exc:
            return str(exc)
        return None

    return run(to_vn(mclock.now()), cal, downloader=downloader, checker=checker, notify=notify,
               notify_ok=args.notify_ok, catchup_days=int(settings.require("history_download.catchup_days")),
               not_before=_hhmm(settings.require("history_download.not_before")))


if __name__ == "__main__":
    raise SystemExit(main())
