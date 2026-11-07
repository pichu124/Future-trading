"""Dựng bar live từ tick — đóng bar THEO ĐỒNG HỒ (kể cả khi không có tick mới).

- `on_tick(tick)`: cập nhật bar đang mở của hợp đồng (chỉ tick có giá khớp); tick thuộc phút sau
  ⇒ phát bar trước. Tick đến muộn cho một bar ĐÃ phát ⇒ bỏ và đếm (`late_ticks`) — không sửa bar
  đã đưa cho strategy (tránh look-ahead ngược).
- `on_time(now)`: phát mọi bar có `start + interval + grace_s ≤ now`. `grace_s` chờ tick trễ mạng.
Backtest dùng `backtest.engine.bars_from_ticks` (cùng quy ước bar = [start, start+interval)).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core.models import Bar, Tick


class BarBuilder:
    def __init__(self, interval_s: int = 60, grace_s: float = 0.5):
        self.interval_s = interval_s
        self.grace = timedelta(seconds=grace_s)
        self._cur: dict[str, dict] = {}
        self._closed_until: dict[str, datetime] = {}
        self.late_ticks = 0

    def _start_of(self, ts: datetime) -> datetime:
        epoch = datetime.combine(ts.date(), datetime.min.time())
        return epoch + timedelta(seconds=((ts - epoch).total_seconds() // self.interval_s) * self.interval_s)

    def _emit(self, sym: str) -> Bar:
        b = self._cur.pop(sym)
        self._closed_until[sym] = b["start"] + timedelta(seconds=self.interval_s)
        return Bar(sym, b["start"], b["o"], b["h"], b["l"], b["c"], b["v"], self.interval_s)

    def on_tick(self, tick: Tick) -> list[Bar]:
        if tick.last is None:
            return []
        start = self._start_of(tick.ts)
        if start < self._closed_until.get(tick.symbol, datetime.min):
            self.late_ticks += 1
            return []
        out = []
        cur = self._cur.get(tick.symbol)
        if cur is not None and cur["start"] != start:
            out.append(self._emit(tick.symbol))
            cur = None
        if cur is None:
            self._cur[tick.symbol] = {"start": start, "o": tick.last, "h": tick.last, "l": tick.last,
                                      "c": tick.last, "v": tick.last_qty or 0}
        else:
            cur["h"], cur["l"], cur["c"] = max(cur["h"], tick.last), min(cur["l"], tick.last), tick.last
            cur["v"] += tick.last_qty or 0
        return out

    def on_time(self, now: datetime) -> list[Bar]:
        due = [s for s, b in self._cur.items()
               if b["start"] + timedelta(seconds=self.interval_s) + self.grace <= now]
        return [self._emit(s) for s in sorted(due)]
