"""Nạp lịch sử nhiều ngày cho runner live/paper — cùng logic với backtest (`core.continuous.HistoryBuilder`).

Nguồn: bar 1 phút đã lưu trên đĩa (`settings.live.history_bar_dirs`, mặc định `data/history/ohlc1m` của
downloader rồi `data/bars` của recorder; trùng (HĐ, phút) ⇒ bản đọc trước thắng). Chỉ dùng các phiên TRƯỚC hôm
nay.

NGHIÊM NGẶT (ưu tiên 1 của CLAUDE.md — không giao dịch trên dữ liệu thiếu): phải có đủ đúng N phiên giao dịch gần
nhất của HĐ giao dịch từng ngày, và mọi roll trong khoảng đó phải đo được chênh lệch (ngày trước roll cần cả HĐ
kế tiếp). Thiếu ⇒ `HistoryUnavailable` nêu ngày/HĐ cần tải ⇒ runner thoát, không giao dịch. Backtest thì chỉ cảnh
báo và cấp lịch sử ngắn hơn (khác biệt có chủ ý: live chặt hơn).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable, Sequence, Union

from core.calendar import TradingCalendar
from core.continuous import ContinuousHistory, HistoryBuilder


class HistoryUnavailable(RuntimeError):
    pass


def required_days(today: date, days: int, calendar: TradingCalendar) -> list[date]:
    out, d = [], today
    for _ in range(days):
        d = calendar.previous_trading_day(d)
        out.append(d)
    return sorted(out)


def load_live_history(dirs: Sequence[Union[str, Path]], today: date, days: int, calendar: TradingCalendar,
                      trading_contract: Callable[[date], str]) -> ContinuousHistory:
    from backtest.engine import load_bars_parquet

    if int(days) < 1:
        raise ValueError("days phải ≥ 1")
    need = required_days(today, int(days), calendar)
    hint = f"chạy `python scripts/download_history.py --start {need[0]} --end {need[-1]}`"
    existing = [Path(p) for p in dirs if Path(p).exists()]
    if not existing:
        raise HistoryUnavailable(f"Không có thư mục lịch sử nào ({', '.join(str(p) for p in dirs)}) — {hint}")
    try:
        bars, _ = load_bars_parquet(existing)
    except ValueError as exc:
        raise HistoryUnavailable(f"Đọc lịch sử lỗi: {exc} — {hint}") from exc
    wanted = set(need)
    builder = HistoryBuilder(int(days), trading_contract, calendar.next_trading_day)
    for b in bars:
        if b.start.date() in wanted:
            builder.observe(b)
    problems = []
    for d in need:
        r = builder.close_day(d)
        if r.n_bars == 0:
            problems.append(f"{d}: thiếu bar {r.contract}")
        if r.is_roll_eve and r.gap is None:
            problems.append(f"{d}: không đo được chênh lệch roll {r.contract} → {r.next_contract} "
                            f"(thiếu bar {r.next_contract} cùng phút)")
    history = builder.history
    got = {b.start.date() for b in history.view(trading_contract(today))}
    if not problems and got != wanted:                        # phòng xa: lịch sử bị cắt vì lý do khác
        problems.append(f"thiếu phiên {', '.join(str(d) for d in sorted(wanted - got))} sau khi nối chuỗi")
    if problems:
        raise HistoryUnavailable(f"Lịch sử {days} phiên chưa đủ: " + "; ".join(problems) + f" — {hint}")
    return history
