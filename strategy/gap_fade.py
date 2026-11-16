"""Chiến lược #6 — lấp gap mở cửa (đăng ký trước 12/11/2026, research/log.md).

Giả thuyết: VN30F nhiều nhà đầu tư cá nhân ⇒ đầu phiên phản ứng quá đà với tin qua đêm; khoảng chênh so với giá
đóng phiên trước (gap) hay bị lấp lại một phần trong buổi sáng.

Đặc tả (tham số `settings.strategy.gap_fade`):
1. C₋₁ = đóng nến CUỐI của đúng phiên giao dịch liền trước (`prev_trading_day`, theo lịch nghỉ) trong lịch sử
   back-adjust. Phiên đó không có trong lịch sử (mất dữ liệu) ⇒ không giao dịch (không lấy phiên cũ hơn).
2. ADR = trung bình (đỉnh − đáy) `adr_days` phiên trước (`strategy.indicators.adr_before`); thiếu ⇒ không giao dịch.
3. Quyết định MỘT lần ở nến có giờ mở ĐÚNG `decision_bar` khi nến đóng (thiếu nến đó ⇒ cả ngày không vào):
   g = đóng − C₋₁; |g| ≥ min_gap_adr × ADR và đang phẳng ⇒ vào NGƯỢC dấu g; chốt lời = C₋₁;
   stop = đóng ± stop_adr × ADR (xa thêm về phía gap), làm tròn bước giá.
4. Đang có vị thế và nến có giờ mở ≥ exit_bar đóng ⇒ EXIT (một lần/ngày) — trước nghỉ trưa.

Lịch nghỉ chỉ được đọc lúc dựng (`from_settings`, lịch mặc định `config/holidays.yaml`); `on_bar` thuần.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, ClassVar, Optional, Sequence

from core.models import Bar, Signal, SignalAction
from core.settings import Settings
from strategy.base import StrategyContext
from strategy.indicators import adr_before
from strategy.morning_momentum import _hhmm, _positive

_EPS = 1e-9


@dataclass
class GapFade:
    TITLE: ClassVar[str] = "Lấp gap mở cửa (#6)"
    ROBUSTNESS_PARAMS: ClassVar[tuple[str, ...]] = ("strategy.gap_fade.adr_days", "strategy.gap_fade.min_gap_adr",
                                                    "strategy.gap_fade.stop_adr")

    adr_days: int
    min_gap_adr: float
    stop_adr: float
    decision_bar: time
    exit_bar: time
    qty: int
    tick_size: float
    prev_trading_day: Callable[[date], date]
    _decided: set = field(default_factory=set, repr=False)
    _exited: set = field(default_factory=set, repr=False)

    @property
    def history_days(self) -> int:
        return self.adr_days

    @classmethod
    def from_settings(cls, s: Settings, prev_trading_day: Optional[Callable[[date], date]] = None) -> "GapFade":
        p = "strategy.gap_fade."
        r = lambda k: s.require(p + k)   # noqa: E731
        am_start = _hhmm("session.continuous_am_start", s.require("session.continuous_am_start"))
        decision = _hhmm(p + "decision_bar", r("decision_bar"))
        exit_bar = _hhmm(p + "exit_bar", r("exit_bar"))
        if decision <= am_start:
            raise ValueError(f"{p}decision_bar ({decision:%H:%M}) phải sau giờ mở phiên {am_start:%H:%M}")
        if exit_bar <= decision:
            raise ValueError(f"{p}exit_bar ({exit_bar:%H:%M}) phải sau decision_bar ({decision:%H:%M})")
        if prev_trading_day is None:
            from core.calendar import TradingCalendar
            prev_trading_day = TradingCalendar.default().previous_trading_day
        return cls(adr_days=_positive(p + "adr_days", r("adr_days"), True),
                   min_gap_adr=_positive(p + "min_gap_adr", r("min_gap_adr"), False),
                   stop_adr=_positive(p + "stop_adr", r("stop_adr"), False),
                   decision_bar=decision, exit_bar=exit_bar,
                   qty=_positive(p + "qty", r("qty"), True),
                   tick_size=float(s.require("contract.tick_size_points")),
                   prev_trading_day=prev_trading_day)

    # ------------------------------------------------------------------ API ----

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        day = bar.start.date()
        ts = bar.start + timedelta(seconds=bar.interval_s)
        if ctx.position_qty != 0 and bar.start.time() >= self.exit_bar and day not in self._exited:
            self._exited.add(day)
            return [Signal(SignalAction.EXIT, ts, bar.symbol, reason=f"Lấp gap: thoát theo giờ {self.exit_bar:%H:%M}")]
        d0 = datetime.combine(day, self.decision_bar)
        if day in self._decided or bar.start < d0:
            return []
        self._decided.add(day)                        # quyết định đúng một lần (kể cả khi không vào / thiếu nến)
        if bar.start != d0 or ctx.position_qty != 0:
            return []
        prev = self._prev_close(ctx.history, day)
        adr = adr_before(ctx.history, day, self.adr_days)
        if prev is None or adr is None:
            return []
        g = bar.close - prev
        threshold = self.min_gap_adr * adr
        if abs(g) + _EPS < threshold:
            return []
        short = g > 0
        stop = self._round(bar.close + self.stop_adr * adr if short else bar.close - self.stop_adr * adr)
        return [Signal(SignalAction.ENTER_SHORT if short else SignalAction.ENTER_LONG, ts, bar.symbol,
                       stop_loss=stop, take_profit=self._round(prev), qty=self.qty,
                       reason=(f"Lấp gap: g={g:+.1f} (đóng trước {prev:g} → {bar.close:g} lúc {bar.start:%H:%M}) ≥ "
                               f"{threshold:.1f} = {self.min_gap_adr:g}×ADR{self.adr_days} {adr:.1f}; "
                               f"TP {prev:g}, stop {self.stop_adr:g}×ADR"))]

    # ---------------------------------------------------------------- nội bộ ----

    def _prev_close(self, history: Sequence[Bar], day: date) -> Optional[float]:
        prev_day = self.prev_trading_day(day)
        last = max((b for b in history if b.start.date() == prev_day), key=lambda b: b.start, default=None)
        return last.close if last is not None else None   # thiếu phiên liền trước ⇒ không đoán

    def _round(self, px: float) -> float:
        return round(round(px / self.tick_size) * self.tick_size, 6)
