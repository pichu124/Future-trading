"""Chiến lược #4 — ngày xu hướng (đăng ký trước 10/11/2026, research/log.md).

Giả thuyết: giờ đầu phiên đi mạnh (so với biên độ ngày trung bình) ⇒ ngày xu hướng, theo hướng đó đến cuối phiên.

Đặc tả (tham số `settings.strategy.trend_day`):
1. ADR = trung bình (đỉnh − đáy) `adr_days` phiên trước (`strategy.indicators.adr_before`); thiếu ⇒ không giao dịch.
2. Mở = giá mở nến đầu tiên có giờ mở trong [continuous_am_start, +5′); thiếu ⇒ không giao dịch hôm đó.
3. Quyết định MỘT lần ở nến đầu tiên có giờ mở trong [decision_bar, +5′) khi nến đóng: m = đóng nến đó − Mở;
   |m| ≥ min_move_adr × ADR và đang phẳng ⇒ ENTER theo dấu m, stop = đóng ∓ stop_adr × ADR (làm tròn bước giá),
   không chốt lời. Giờ đầu yếu ⇒ cả ngày không vào (không xét lại sau đó).
4. Đang có vị thế và nến có giờ mở ≥ exit_bar đóng ⇒ EXIT (một lần/ngày). Risk vẫn flatten 14:25.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import ClassVar, Optional, Sequence

from core.models import Bar, Signal, SignalAction
from core.settings import Settings
from strategy.base import StrategyContext
from strategy.indicators import adr_before
from strategy.morning_momentum import _hhmm, _positive

_WINDOW = timedelta(minutes=5)
_EPS = 1e-9


@dataclass
class TrendDay:
    TITLE: ClassVar[str] = "Ngày xu hướng (#4)"
    ROBUSTNESS_PARAMS: ClassVar[tuple[str, ...]] = ("strategy.trend_day.adr_days", "strategy.trend_day.min_move_adr",
                                                    "strategy.trend_day.stop_adr")

    adr_days: int
    min_move_adr: float
    stop_adr: float
    decision_bar: time
    exit_bar: time
    qty: int
    am_start: time
    tick_size: float
    _decided: set = field(default_factory=set, repr=False)
    _exited: set = field(default_factory=set, repr=False)

    @property
    def history_days(self) -> int:
        return self.adr_days

    @classmethod
    def from_settings(cls, s: Settings) -> "TrendDay":
        p = "strategy.trend_day."
        r = lambda k: s.require(p + k)   # noqa: E731
        am_start = _hhmm("session.continuous_am_start", s.require("session.continuous_am_start"))
        decision = _hhmm(p + "decision_bar", r("decision_bar"))
        if decision <= am_start:
            raise ValueError(f"{p}decision_bar ({decision:%H:%M}) phải sau giờ mở phiên {am_start:%H:%M}")
        return cls(adr_days=_positive(p + "adr_days", r("adr_days"), True),
                   min_move_adr=_positive(p + "min_move_adr", r("min_move_adr"), False),
                   stop_adr=_positive(p + "stop_adr", r("stop_adr"), False),
                   decision_bar=decision,
                   exit_bar=_hhmm(p + "exit_bar", r("exit_bar")),
                   qty=_positive(p + "qty", r("qty"), True),
                   am_start=am_start,
                   tick_size=float(s.require("contract.tick_size_points")))

    # ------------------------------------------------------------------ API ----

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        day = bar.start.date()
        ts = bar.start + timedelta(seconds=bar.interval_s)
        if ctx.position_qty != 0 and bar.start.time() >= self.exit_bar and day not in self._exited:
            self._exited.add(day)
            return [Signal(SignalAction.EXIT, ts, bar.symbol, reason=f"Ngày xu hướng: thoát theo giờ {self.exit_bar:%H:%M}")]
        d0 = datetime.combine(day, self.decision_bar)
        if day in self._decided or not d0 <= bar.start < d0 + _WINDOW:
            return []
        self._decided.add(day)                       # quyết định đúng một lần (kể cả khi không vào)
        if ctx.position_qty != 0:
            return []
        o = self._open(bar, ctx.bars_today)
        adr = adr_before(ctx.history, day, self.adr_days)
        if o is None or adr is None:
            return []
        m = bar.close - o
        threshold = self.min_move_adr * adr
        if abs(m) + _EPS < threshold:
            return []
        long = m > 0
        stop = self._round(bar.close - self.stop_adr * adr if long else bar.close + self.stop_adr * adr)
        return [Signal(SignalAction.ENTER_LONG if long else SignalAction.ENTER_SHORT, ts, bar.symbol,
                       stop_loss=stop, take_profit=None, qty=self.qty,
                       reason=(f"Ngày xu hướng: m={m:+.1f} (mở {o:g} → {bar.close:g} lúc {bar.start:%H:%M}) ≥ "
                               f"{threshold:.1f} = {self.min_move_adr:g}×ADR{self.adr_days} {adr:.1f}; "
                               f"stop {self.stop_adr:g}×ADR"))]

    # ---------------------------------------------------------------- nội bộ ----

    def _open(self, bar: Bar, bars_today: Sequence[Bar]) -> Optional[float]:
        am0 = datetime.combine(bar.start.date(), self.am_start)
        first = sorted((b for b in bars_today if b.symbol == bar.symbol and am0 <= b.start < am0 + _WINDOW),
                       key=lambda b: b.start)
        return first[0].open if first else None      # thiếu ⇒ không đoán

    def _round(self, px: float) -> float:
        return round(round(px / self.tick_size) * self.tick_size, 6)
