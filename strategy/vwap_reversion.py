"""Chiến lược #5 — đảo chiều về VWAP (đăng ký trước 10/11/2026, research/log.md).

Giả thuyết: giá lệch xa VWAP trong ngày (so với biên độ ngày trung bình) có xu hướng quay về VWAP.

Đặc tả (tham số `settings.strategy.vwap_reversion`):
1. VWAP = Σ((H+L+C)/3 × KL) / ΣKL các nến hôm nay giờ mở ≥ continuous_am_start (gồm nến hiện tại), bỏ KL = 0;
   ΣKL = 0 ⇒ không giao dịch. ADR = `adr_before` (`adr_days` phiên trước); thiếu ⇒ không giao dịch.
2. Xét ở nến có giờ mở trong [window_start, window_end], BỎ nến đóng đúng lúc nghỉ trưa (giờ đóng = lunch_start —
   risk chặn mở vị thế, tín hiệu chỉ phí một lượt; làm rõ thực thi, không đổi giả thuyết).
3. d = đóng − VWAP; |d| ≥ entry_dev_adr × ADR và đang phẳng ⇒ vào NGƯỢC dấu d; chốt lời = VWAP lúc ra tín hiệu;
   stop = đóng ± stop_adr × ADR (xa thêm về phía lệch); làm tròn bước giá.
4. Tối đa `max_trades_per_day` tín hiệu vào/ngày (tính cả tín hiệu bị risk từ chối — thận trọng).
5. Đang có vị thế và nến giờ mở ≥ exit_bar đóng ⇒ EXIT (một lần/ngày). Risk vẫn flatten 14:25.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import ClassVar, Optional, Sequence

from core.models import Bar, Signal, SignalAction
from core.settings import Settings
from strategy.base import StrategyContext
from strategy.indicators import adr_before
from strategy.morning_momentum import _hhmm, _positive

_EPS = 1e-9


def session_vwap(bars_today: Sequence[Bar], am_start: time) -> Optional[float]:
    num = vol = 0.0
    for b in bars_today:
        if b.start.time() < am_start or not b.volume:
            continue
        num += (b.high + b.low + b.close) / 3.0 * b.volume
        vol += b.volume
    return num / vol if vol > 0 else None


@dataclass
class VwapReversion:
    TITLE: ClassVar[str] = "Đảo chiều về VWAP (#5)"
    ROBUSTNESS_PARAMS: ClassVar[tuple[str, ...]] = ("strategy.vwap_reversion.adr_days",
                                                    "strategy.vwap_reversion.entry_dev_adr",
                                                    "strategy.vwap_reversion.stop_adr")

    adr_days: int
    entry_dev_adr: float
    stop_adr: float
    window_start: time
    window_end: time
    max_trades_per_day: int
    exit_bar: time
    qty: int
    am_start: time
    lunch_start: time
    tick_size: float
    _entries: dict = field(default_factory=dict, repr=False)
    _exited: set = field(default_factory=set, repr=False)

    @property
    def history_days(self) -> int:
        return self.adr_days

    @classmethod
    def from_settings(cls, s: Settings) -> "VwapReversion":
        p = "strategy.vwap_reversion."
        r = lambda k: s.require(p + k)   # noqa: E731
        am_start = _hhmm("session.continuous_am_start", s.require("session.continuous_am_start"))
        w0, w1 = _hhmm(p + "window_start", r("window_start")), _hhmm(p + "window_end", r("window_end"))
        if not am_start <= w0 <= w1:
            raise ValueError(f"{p}window_start/window_end phải thoả {am_start:%H:%M} ≤ start ≤ end, "
                             f"nhận {w0:%H:%M}–{w1:%H:%M}")
        return cls(adr_days=_positive(p + "adr_days", r("adr_days"), True),
                   entry_dev_adr=_positive(p + "entry_dev_adr", r("entry_dev_adr"), False),
                   stop_adr=_positive(p + "stop_adr", r("stop_adr"), False),
                   window_start=w0, window_end=w1,
                   max_trades_per_day=_positive(p + "max_trades_per_day", r("max_trades_per_day"), True),
                   exit_bar=_hhmm(p + "exit_bar", r("exit_bar")),
                   qty=_positive(p + "qty", r("qty"), True),
                   am_start=am_start,
                   lunch_start=_hhmm("session.lunch_start", s.require("session.lunch_start")),
                   tick_size=float(s.require("contract.tick_size_points")))

    # ------------------------------------------------------------------ API ----

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]:
        day = bar.start.date()
        end = bar.start + timedelta(seconds=bar.interval_s)
        if ctx.position_qty != 0:
            if bar.start.time() >= self.exit_bar and day not in self._exited:
                self._exited.add(day)
                return [Signal(SignalAction.EXIT, end, bar.symbol, reason=f"VWAP: thoát theo giờ {self.exit_bar:%H:%M}")]
            return []
        if not self.window_start <= bar.start.time() <= self.window_end:
            return []
        if end == datetime.combine(day, self.lunch_start):
            return []                                     # đóng lúc nghỉ trưa: risk chặn mở
        if self._entries.get(day, 0) >= self.max_trades_per_day:
            return []
        mine = [b for b in ctx.bars_today if b.symbol == bar.symbol]
        vwap = session_vwap(mine, self.am_start)
        adr = adr_before(ctx.history, day, self.adr_days)
        if vwap is None or adr is None:
            return []
        d = bar.close - vwap
        threshold = self.entry_dev_adr * adr
        if abs(d) + _EPS < threshold:
            return []
        short = d > 0
        stop = self._round(bar.close + self.stop_adr * adr if short else bar.close - self.stop_adr * adr)
        self._entries[day] = self._entries.get(day, 0) + 1
        return [Signal(SignalAction.ENTER_SHORT if short else SignalAction.ENTER_LONG, end, bar.symbol,
                       stop_loss=stop, take_profit=self._round(vwap), qty=self.qty,
                       reason=(f"VWAP: đóng {bar.close:g} lệch {d:+.1f} so với VWAP {vwap:.1f} ≥ {threshold:.1f} = "
                               f"{self.entry_dev_adr:g}×ADR{self.adr_days} {adr:.1f}; TP = VWAP, "
                               f"stop {self.stop_adr:g}×ADR"))]

    def _round(self, px: float) -> float:
        return round(round(px / self.tick_size) * self.tick_size, 6)
