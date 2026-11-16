"""Chiến lược #3 — đà sáng → chiều (đăng ký trước 10/11/2026, research/log.md).

Giả thuyết (intraday momentum, Gao et al. 2018): biến động phiên sáng dự báo hướng phiên chiều.

Đặc tả (tham số `settings.strategy.morning_momentum`):
1. ADR = trung bình (đỉnh − đáy) của `adr_days` phiên trước (`strategy.indicators.adr_before`); thiếu ⇒ không
   giao dịch.
2. Mở = giá mở nến đầu tiên có giờ mở trong [continuous_am_start, +5′); Đóng sáng = giá đóng nến cuối có giờ mở
   trong [lunch_start − 5′, lunch_start). Thiếu ⇒ không giao dịch hôm đó.
3. Quyết định MỘT lần ở nến đầu tiên của phiên chiều (giờ mở trong [continuous_pm_start, +5′)) khi nến đóng —
   11:30 đã nghỉ trưa nên risk sẽ chặn mở vị thế. m = Đóng sáng − Mở; |m| ≥ min_move_adr × ADR và đang phẳng ⇒
   ENTER theo dấu m, stop = đóng nến quyết định ∓ stop_adr × ADR (làm tròn bước giá), không chốt lời.
4. Đang có vị thế và nến có giờ mở ≥ exit_bar đóng ⇒ EXIT (một lần/ngày). Risk vẫn flatten 14:25.

Trạng thái nội bộ (ngày đã quyết định / đã vào / đã thoát) xác định hoàn toàn bởi chuỗi bar đã thấy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import ClassVar, Optional, Sequence

from core.models import Bar, Signal, SignalAction
from core.settings import Settings
from strategy.base import StrategyContext
from strategy.indicators import adr_before

_WINDOW = timedelta(minutes=5)
_EPS = 1e-9


def _hhmm(name: str, value) -> time:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} phải dạng HH:MM, nhận {value!r}") from exc


def _positive(name: str, value, integer: bool):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} phải là số > 0, nhận {value!r}")
    if integer and int(value) != value:
        raise ValueError(f"{name} phải là số nguyên, nhận {value!r}")
    return int(value) if integer else float(value)


@dataclass
class MorningMomentum:
    TITLE: ClassVar[str] = "Đà sáng → chiều (#3)"
    # tham số kiểm độ nhạy ±20% trong backtest.robustness (đăng ký trước cùng giá trị gốc)
    ROBUSTNESS_PARAMS: ClassVar[tuple[str, ...]] = ("strategy.morning_momentum.adr_days",
                                                    "strategy.morning_momentum.min_move_adr",
                                                    "strategy.morning_momentum.stop_adr")

    adr_days: int
    min_move_adr: float
    stop_adr: float
    exit_bar: time
    qty: int
    am_start: time
    lunch_start: time
    pm_start: time
    tick_size: float
    _decided: set = field(default_factory=set, repr=False)
    _exited: set = field(default_factory=set, repr=False)

    @property
    def history_days(self) -> int:
        return self.adr_days

    @classmethod
    def from_settings(cls, s: Settings) -> "MorningMomentum":
        p = "strategy.morning_momentum."
        r = lambda k: s.require(p + k)   # noqa: E731
        return cls(adr_days=_positive(p + "adr_days", r("adr_days"), True),
                   min_move_adr=_positive(p + "min_move_adr", r("min_move_adr"), False),
                   stop_adr=_positive(p + "stop_adr", r("stop_adr"), False),
                   exit_bar=_hhmm(p + "exit_bar", r("exit_bar")),
                   qty=_positive(p + "qty", r("qty"), True),
                   am_start=_hhmm("session.continuous_am_start", s.require("session.continuous_am_start")),
                   lunch_start=_hhmm("session.lunch_start", s.require("session.lunch_start")),
                   pm_start=_hhmm("session.continuous_pm_start", s.require("session.continuous_pm_start")),
                   tick_size=float(s.require("contract.tick_size_points")))

    # ------------------------------------------------------------------ API ----

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        day = bar.start.date()
        ts = bar.start + timedelta(seconds=bar.interval_s)
        if ctx.position_qty != 0 and bar.start.time() >= self.exit_bar and day not in self._exited:
            self._exited.add(day)
            return [Signal(SignalAction.EXIT, ts, bar.symbol, reason=f"Đà sáng: thoát theo giờ {self.exit_bar:%H:%M}")]
        pm0 = datetime.combine(day, self.pm_start)
        if day in self._decided or not pm0 <= bar.start < pm0 + _WINDOW:
            return []
        self._decided.add(day)                       # quyết định đúng một lần (kể cả khi không vào)
        if ctx.position_qty != 0:
            return []
        anchors = self._anchors(bar, ctx.bars_today)
        adr = adr_before(ctx.history, day, self.adr_days)
        if anchors is None or adr is None:
            return []
        o, c = anchors
        m = c - o
        threshold = self.min_move_adr * adr
        if abs(m) + _EPS < threshold:
            return []
        long = m > 0
        stop = self._round(bar.close - self.stop_adr * adr if long else bar.close + self.stop_adr * adr)
        return [Signal(SignalAction.ENTER_LONG if long else SignalAction.ENTER_SHORT, ts, bar.symbol,
                       stop_loss=stop, take_profit=None, qty=self.qty,
                       reason=(f"Đà sáng: m={m:+.1f} (mở {o:g} → đóng sáng {c:g}) ≥ {threshold:.1f} = "
                               f"{self.min_move_adr:g}×ADR{self.adr_days} {adr:.1f}; stop {self.stop_adr:g}×ADR"))]

    # ---------------------------------------------------------------- nội bộ ----

    def _anchors(self, bar: Bar, bars_today: Sequence[Bar]) -> Optional[tuple[float, float]]:
        day = bar.start.date()
        am0, lunch = datetime.combine(day, self.am_start), datetime.combine(day, self.lunch_start)
        mine = sorted((b for b in bars_today if b.symbol == bar.symbol), key=lambda b: b.start)
        first = [b for b in mine if am0 <= b.start < am0 + _WINDOW]
        last = [b for b in mine if lunch - _WINDOW <= b.start < lunch]
        if not first or not last:
            return None                              # dữ liệu hổng ⇒ không đoán
        return first[0].open, last[-1].close

    def _round(self, px: float) -> float:
        return round(round(px / self.tick_size) * self.tick_size, 6)
