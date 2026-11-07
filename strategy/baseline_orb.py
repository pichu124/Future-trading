"""Chiến lược baseline: phá biên độ đầu phiên (Opening Range Breakout) — PROMPTS P3.2.

Đặc tả (tham số trong `settings.strategy.orb`):
1. Biên độ = đỉnh cao nhất / đáy thấp nhất của các bar có giờ mở trong
   [continuous_am_start, continuous_am_start + range_minutes). Phải đủ đúng số bar
   (range_minutes × 60 / interval) — thiếu bar (dữ liệu hổng) ⇒ bỏ cả ngày, không đoán.
2. Sau khi biên hoàn tất, bar ĐÓNG cửa trên đỉnh biên ⇒ ENTER_LONG; dưới đáy biên ⇒
   ENTER_SHORT. Chỉ khi đang phẳng. Chạm/xuyên bằng râu nhưng đóng trong biên ⇒ không vào.
3. Stop = cạnh đối diện của biên. R = |giá đóng bar tín hiệu − stop|.
   Chốt lời = giá đóng ± reward_risk × R, làm tròn về bước giá.
4. Tối đa `max_trades_per_day` tín hiệu vào lệnh mỗi ngày (tính cả tín hiệu bị risk từ chối —
   thận trọng).
5. Thoát: stop / chốt lời (ExecutionManager đặt lệnh), hoặc risk flatten trước ATC.
   Giờ vào lệnh sớm nhất / muộn nhất, ngày đáo hạn… do risk_manager quyết, không lặp ở đây.

Hàm thuần theo nghĩa CLAUDE.md: chỉ dùng bar + ctx; trạng thái nội bộ duy nhất là số tín hiệu
đã phát trong ngày (xác định hoàn toàn bởi chuỗi bar đã thấy).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional, Sequence

from core.models import Bar, Signal, SignalAction
from core.settings import Settings
from strategy.base import StrategyContext


def _parse_hhmm(value: str) -> time:
    hh, mm = str(value).split(":")
    return time(int(hh), int(mm))


def _positive(name: str, value, integer: bool):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"strategy.orb.{name} phải là số > 0, nhận {value!r}")
    if integer and int(value) != value:
        raise ValueError(f"strategy.orb.{name} phải là số nguyên, nhận {value!r}")
    return int(value) if integer else float(value)


@dataclass
class OpeningRangeBreakout:
    range_minutes: int
    reward_risk: float
    max_trades_per_day: int
    qty: int
    session_start: time
    tick_size: float
    _entries: dict[date, int] = field(default_factory=dict, repr=False)

    @classmethod
    def from_settings(cls, s: Settings) -> "OpeningRangeBreakout":
        r = lambda k: s.require(f"strategy.orb.{k}")   # noqa: E731
        return cls(
            range_minutes=_positive("range_minutes", r("range_minutes"), True),
            reward_risk=_positive("reward_risk", r("reward_risk"), False),
            max_trades_per_day=_positive("max_trades_per_day", r("max_trades_per_day"), True),
            qty=_positive("qty", r("qty"), True),
            session_start=_parse_hhmm(s.require("session.continuous_am_start")),
            tick_size=float(s.require("contract.tick_size_points")),
        )

    # ------------------------------------------------------------------ API ----

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        rng = self._range(bar, ctx.bars_today)
        if rng is None or ctx.position_qty != 0:
            return []
        day = bar.start.date()
        if self._entries.get(day, 0) >= self.max_trades_per_day:
            return []
        low, high = rng
        if bar.close > high:
            action, stop = SignalAction.ENTER_LONG, low
        elif bar.close < low:
            action, stop = SignalAction.ENTER_SHORT, high
        else:
            return []
        risk = abs(bar.close - stop)
        direction = 1 if action is SignalAction.ENTER_LONG else -1
        take_profit = self._round(bar.close + direction * self.reward_risk * risk)
        self._entries[day] = self._entries.get(day, 0) + 1
        return [Signal(action, bar.start + timedelta(seconds=bar.interval_s), bar.symbol,
                       stop_loss=stop, take_profit=take_profit, qty=self.qty,
                       reason=(f"ORB{self.range_minutes}: đóng {bar.close:g} ngoài biên "
                               f"[{low:g}, {high:g}], R={risk:.1f}, TP {self.reward_risk:g}R"))]

    # ---------------------------------------------------------------- nội bộ ----

    def _range(self, bar: Bar, bars_today: Sequence[Bar]) -> Optional[tuple[float, float]]:
        """(đáy, đỉnh) của biên nếu đã hoàn tất TRƯỚC bar hiện tại và đủ bar; ngược lại None."""
        start = datetime.combine(bar.start.date(), self.session_start)
        end = start + timedelta(minutes=self.range_minutes)
        if bar.start < end:
            return None                                   # biên chưa xong
        in_range = [b for b in bars_today if start <= b.start < end and b.symbol == bar.symbol]
        expected = self.range_minutes * 60 // bar.interval_s
        if len(in_range) != expected or len({b.start for b in in_range}) != expected:
            return None                                   # dữ liệu hổng ⇒ không giao dịch hôm nay
        return min(b.low for b in in_range), max(b.high for b in in_range)

    def _round(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 6)
