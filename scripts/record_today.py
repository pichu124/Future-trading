"""Ghi tick cả ngày giao dịch rồi tự thoát — chạy bằng Windows Task Scheduler mỗi sáng.

- Không phải ngày giao dịch (cuối tuần / config/holidays.yaml) ⇒ thoát 0 ngay.
- Khởi động trước giờ `recorder.start` ⇒ chờ; sau `recorder.stop` ⇒ thoát 0.
- Chạy feed + recorder tới `recorder.stop` (mặc định 15:00), flush lần cuối, gộp file ngày, rồi
  chạy `scripts/validate_day.py` (báo cáo chất lượng + `data/bars/<ngày>.parquet` cho backtest).
- Feed chết bất thường ⇒ vẫn lưu phần đã nhận, thoát mã 1 (Task Scheduler ghi "Last Run Result").
- Ngày trước roll + bật `recorder.record_next_contract_on_roll_eve`: ghi thêm HĐ kế tiếp trên CÙNG kết nối
  (đo chênh lệch back-adjust cho chiến lược nhiều ngày); kiểm định chất lượng chỉ trên HĐ giao dịch.
- Chặn hai tiến trình ghi cùng lúc trên một máy bằng `live.instance_lock.InstanceLock`
  (file `state/recorder.lock`, tách khỏi khoá của bot giao dịch).

Hợp đồng: SDK DNSE chưa có REST danh sách hợp đồng (P4.1) ⇒ phải chọn MỘT trong hai:
  --krx 41I1GB000              mã KRX bạn đã kiểm trên bảng giá (đổi mỗi tháng)
  --assume-computed-contract   dùng mã do core.contracts tính (chỉ ghi dữ liệu, không giao dịch);
                               nếu sai mã thì feed im lặng ⇒ log cảnh báo "feed im lặng"

    python scripts/record_today.py --assume-computed-contract
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts  # noqa: E402
from core.calendar import TradingCalendar  # noqa: E402
from core.clock import MarketClock, SessionState  # noqa: E402
from feeds.base import DataFeed  # noqa: E402
from recorder.tick_recorder import TickRecorder  # noqa: E402

log = logging.getLogger("record_today")


def _hhmm(value: str) -> time:
    hh, mm = str(value).split(":")
    return time(int(hh), int(mm))


def plan(now: datetime, cal: TradingCalendar, start: time, stop: time) -> tuple[str, float]:
    """("skip"|"wait"|"run"|"done", số giây phải chờ)."""
    if not cal.is_trading_day(now.date()):
        return "skip", 0.0
    start_dt = datetime.combine(now.date(), start)
    if now < start_dt:
        return "wait", (start_dt - now).total_seconds()
    if now.time() >= stop:
        return "done", 0.0
    return "run", 0.0


def roll_eve_extras(d, cal: TradingCalendar, roll_days_before_expiry: int) -> list[str]:
    """Ngày trước roll ⇒ [HĐ kế tiếp] (để đo chênh lệch back-adjust, core/continuous.py); ngày khác ⇒ []."""
    tc = contracts.trading_contract(d, cal, roll_days_before_expiry)
    nxt = contracts.trading_contract(cal.next_trading_day(d), cal, roll_days_before_expiry)
    return [nxt] if nxt != tc else []


def feed_kwargs(settings, d, cal: TradingCalendar, roll_days_before_expiry: int) -> dict:
    """Tham số thêm cho DnseFeed. Ghi HĐ kế tiếp chỉ khi bật `recorder.record_next_contract_on_roll_eve`
    (mặc định TẮT tới khi `dnse_feed_probe.py --extra-contract` xác nhận đăng ký nhiều mã — broker_notes §1.4)."""
    on = settings.get("recorder.record_next_contract_on_roll_eve") is True
    return {"extra_contracts": roll_eve_extras(d, cal, roll_days_before_expiry) if on else []}


def computed_listing(d, cal: TradingCalendar, roll_days_before_expiry: int, extras) -> list[str]:
    """Mã KRX TÍNH TOÁN (--assume-computed-contract) của HĐ giao dịch + HĐ ghi thêm."""
    names = [contracts.trading_contract(d, cal, roll_days_before_expiry), *extras]
    return [contracts.krx_symbol(*contracts.contract_ym(n)) for n in names]


async def record_session(feed: DataFeed, recorder: TickRecorder, mclock: MarketClock, *,
                         stop_at: time,
                         poll: Callable[[float], Awaitable[None]] = asyncio.sleep,
                         heartbeat: Optional[Callable[[], None]] = None,
                         stale_warn_every_s: float = 60.0) -> int:
    """Chạy feed → recorder tới `stop_at`. Trả 0 nếu êm, 1 nếu feed chết bất thường."""
    feed.on_tick(recorder.add)
    flusher = asyncio.create_task(recorder.run_flusher())
    feed_task = asyncio.create_task(feed.run())
    code = 0
    last_warn: Optional[datetime] = None
    try:
        while mclock.now().time() < stop_at and not feed_task.done():
            await poll(1.0)
            if heartbeat is not None:
                heartbeat()
            now = mclock.now()
            if feed.is_stale(now) and (last_warn is None
                                       or (now - last_warn).total_seconds() >= stale_warn_every_s):
                last_warn = now
                log.warning("feed %s im lặng trong phiên liên tục (lần cuối: %s) — kiểm tra kết nối/mã HĐ",
                            feed.name, feed.last_message_at)
        if feed_task.done() and feed_task.exception() is not None:
            exc = feed_task.exception()
            log.error("feed %s dừng bất thường: %s: %s", feed.name, type(exc).__name__, exc)
            code = 1
    finally:
        await feed.stop()
        if not feed_task.done():
            feed_task.cancel()
        await asyncio.gather(feed_task, return_exceptions=True)
        try:
            await recorder.close()
        except Exception:
            log.exception("recorder: flush/gộp cuối ngày thất bại — part đã ghi vẫn còn trên đĩa")
            code = code or 1
        await asyncio.gather(flusher, return_exceptions=True)
        log.info("recorder: %s", recorder.stats())
    return code


def main(argv=None) -> int:
    from core.settings import load_settings
    from feeds.data_feed_dnse import DnseFeed
    from live.instance_lock import InstanceLock, LockHeld

    ap = argparse.ArgumentParser(description="Ghi tick VN30F cả ngày rồi thoát")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--krx", nargs="+", help="mã KRX đang niêm yết (đã kiểm tay)")
    g.add_argument("--assume-computed-contract", action="store_true",
                   help="tin mã do core.contracts tính (chỉ để ghi dữ liệu)")
    args = ap.parse_args(argv)

    settings = load_settings()
    try:
        from dotenv import load_dotenv
        from core.settings import REPO_ROOT
        load_dotenv(REPO_ROOT / ".env")          # Task Scheduler không đặt cwd = repo
    except ImportError:
        pass
    from monitoring.logger import setup_logging
    setup_logging(settings.path("paths.log_dir"), prefix="record")   # logs/record_YYYY-MM-DD.jsonl

    mclock = MarketClock.from_settings(settings)
    start, stop = _hhmm(settings.require("recorder.start")), _hhmm(settings.require("recorder.stop"))
    action, wait_s = plan(mclock.now(), mclock.calendar, start, stop)
    if action in ("skip", "done"):
        log.info("Không ghi: %s", "không phải ngày giao dịch" if action == "skip" else "đã quá giờ dừng")
        return 0
    key, secret = os.environ.get("DNSE_API_KEY"), os.environ.get("DNSE_API_SECRET")
    if not key or not secret:
        log.error("Thiếu DNSE_API_KEY / DNSE_API_SECRET trong .env")
        return 2

    today = mclock.now().date()
    extra_kw = feed_kwargs(settings, today, mclock.calendar, mclock.roll_days_before_expiry)
    if extra_kw["extra_contracts"]:
        log.info("Ngày trước roll: ghi thêm %s (đo chênh lệch back-adjust)", ", ".join(extra_kw["extra_contracts"]))

    async def lister():
        if args.krx:
            return [s.strip().upper() for s in args.krx]
        krx = computed_listing(today, mclock.calendar, mclock.roll_days_before_expiry, extra_kw["extra_contracts"])
        log.warning("Dùng mã hợp đồng TÍNH TOÁN %s — chưa đối chiếu danh sách broker", ", ".join(krx))
        return krx

    lock = InstanceLock(settings.path("paths.state_dir") / "recorder.lock", mode="recorder",
                        stale_after_s=float(settings.get("live.lock_stale_after_seconds", 120)))
    try:
        lock.acquire()
    except LockHeld as exc:
        log.error("Đã có recorder khác đang chạy: %s", exc)
        return 3
    try:
        async def go() -> int:
            if action == "wait":
                log.info("Chờ %.0f giây tới %s", wait_s, start.strftime("%H:%M"))
                await asyncio.sleep(wait_s)
            feed = DnseFeed.from_settings(settings, key, secret, lister, mclock, **extra_kw)
            rec = TickRecorder.from_settings(settings, mclock.clock)
            log.info("Ghi vào %s, dừng lúc %s", rec.day_dir(), stop.strftime("%H:%M"))
            return await record_session(feed, rec, mclock, stop_at=stop, heartbeat=lock.heartbeat)
        code = asyncio.run(go())
    finally:
        lock.release()
    try:                                    # kiểm định + dựng bar 1 phút cho backtest (P1.5)
        from scripts.validate_day import run as validate
        vcode, rep = validate(mclock.now().date(), settings)
        log.info("Kiểm định dữ liệu:\n%s", rep.summary())
        if vcode == 1:
            log.warning("Dữ liệu hôm nay có vấn đề — xem data/validation/%s.json", mclock.now().date())
    except Exception:
        log.exception("Kiểm định dữ liệu cuối ngày lỗi (dữ liệu tick đã lưu không bị ảnh hưởng)")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
