"""Chiến lược #7 — đảo chiều qua ngày (đăng ký trước 16/11/2026, research/log.md).

Giả thuyết: VN30F chủ yếu là nhà đầu tư cá nhân ⇒ phiên đi mạnh một chiều là phản ứng quá đà, phiên sau quay lại một
phần (đảo chiều chuỗi thời gian qua ngày).

Đặc tả (tham số `settings.strategy.prev_day_reversal`):
1. Phiên trước = đúng `prev_trading_day(hôm nay)` trong lịch sử back-adjust; không có ⇒ không giao dịch.
   m = đóng nến CUỐI − mở nến ĐẦU của phiên đó.
2. ADR = trung bình (đỉnh − đáy) `adr_days` phiên trước (`strategy.indicators.adr_before`); thiếu ⇒ không giao dịch.
3. Quyết định MỘT lần ở nến có giờ mở ĐÚNG `decision_bar` khi nến đóng (thiếu nến đó ⇒ cả ngày không vào):
   |m| ≥ min_move_adr × ADR và đang phẳng ⇒ vào NGƯỢC dấu m; stop = đóng ± stop_adr × ADR; không chốt lời.
4. Đang có vị thế và nến có giờ mở ≥ exit_bar đóng ⇒ EXIT (một lần/ngày).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, ClassVar, Optional, Sequence

from core.models import Bar, Signal, SignalAction
from core.settings import Settings
from strategy.base import StrategyContext
from strategy.indicators import adr_before, session_bars
from strategy.morning_momentum import _hhmm, _positive

_EPS = 1e-9


@dataclass
class PrevDayReversal:
    TITLE: ClassVar[str] = "Đảo chiều qua ngày (#7)"
    ROBUSTNESS_PARAMS: ClassVar[tuple[str, ...]] = ("strategy.prev_day_reversal.adr_days",
                                                    "strategy.prev_day_reversal.min_move_adr",
                                                    "strategy.prev_day_reversal.stop_adr")

    adr_days: int
    min_move_adr: float
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
    def from_settings(cls, s: Settings,
                      prev_trading_day: Optional[Callable[[date], date]] = None) -> "PrevDayReversal":
        p = "strategy.prev_day_reversal."
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
                   min_move_adr=_positive(p + "min_move_adr", r("min_move_adr"), False),
                   stop_adr=_positive(p + "stop_adr", r("stop_adr"), False),
                   decision_bar=decision, exit_bar=exit_bar,
                   qty=_positive(p + "qty", r("qty"), True),
                   tick_size=float(s.require("contract.tick_size_points")),
                   prev_trading_day=prev_trading_day)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        day = bar.start.date()
        ts = bar.start + timedelta(seconds=bar.interval_s)
        if ctx.position_qty != 0 and bar.start.time() >= self.exit_bar and day not in self._exited:
            self._exited.add(day)
            return [Signal(SignalAction.EXIT, ts, bar.symbol,
                           reason=f"Đảo chiều qua ngày: thoát theo giờ {self.exit_bar:%H:%M}")]
        d0 = datetime.combine(day, self.decision_bar)
        if day in self._decided or bar.start < d0:
            return []
        self._decided.add(day)
        if bar.start != d0 or ctx.position_qty != 0:
            return []
        prev = session_bars(ctx.history, self.prev_trading_day(day))
        adr = adr_before(ctx.history, day, self.adr_days)
        if not prev or adr is None:
            return []
        m = prev[-1].close - prev[0].open
        threshold = self.min_move_adr * adr
        if abs(m) + _EPS < threshold:
            return []
        short = m > 0
        stop = self._round(bar.close + self.stop_adr * adr if short else bar.close - self.stop_adr * adr)
        return [Signal(SignalAction.ENTER_SHORT if short else SignalAction.ENTER_LONG, ts, bar.symbol,
                       stop_loss=stop, qty=self.qty,
                       reason=(f"Đảo chiều qua ngày: m={m:+.1f} (phiên {prev[0].start:%d/%m} {prev[0].open:g} → "
                               f"{prev[-1].close:g}) ≥ {threshold:.1f} = {self.min_move_adr:g}×ADR{self.adr_days} "
                               f"{adr:.1f}; stop {self.stop_adr:g}×ADR"))]

    def _round(self, px: float) -> float:
        return round(round(px / self.tick_size) * self.tick_size, 6)
