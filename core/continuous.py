"""Chuỗi giá liên tục back-adjust (cộng/trừ điểm) cho chiến lược cần lịch sử nhiều ngày.

Hàm thuần, không I/O, không đọc giờ hệ thống ⇒ dùng chung cho backtest và (sau này) live.

- Chênh lệch roll = giá đóng HĐ mới − giá đóng HĐ cũ ở PHÚT CHUNG CUỐI CÙNG của ngày trước roll.
- Lịch sử HĐ cũ được CỘNG chênh lệch (giữ nguyên khoảng cách theo điểm: biên độ, ATR, khoảng stop).
- "Tại thời điểm": `view(hđ hiện tại)` chỉ dùng các chênh lệch đã đăng ký ⇒ HĐ hiện tại là giá thật, không
  roll tương lai nào lọt vào. Thiếu chênh lệch của một roll ⇒ lịch sử dừng tại roll đó (không đoán).
- Bar đã dịch mang `symbol` = HĐ hiện tại (giá đã quy về HĐ đó); volume, cờ volume giữ nguyên.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Callable, Iterable, Optional, Sequence

from core.models import Bar


@dataclass(frozen=True)
class RollGap:
    old: str
    new: str
    day: date              # ngày đo (ngày trước roll)
    at: datetime           # phút chung dùng để đo (giờ MỞ bar)
    gap: float             # điểm: giá HĐ mới − giá HĐ cũ


def _single_symbol(bars: Sequence[Bar]) -> Optional[str]:
    syms = {b.symbol for b in bars}
    if len(syms) > 1:
        raise ValueError(f"Bar của nhiều hợp đồng lẫn nhau: {sorted(syms)}")
    return next(iter(syms), None)


def measure_gap(old_bars: Iterable[Bar], new_bars: Iterable[Bar]) -> Optional[RollGap]:
    old, new = list(old_bars), list(new_bars)
    o_sym, n_sym = _single_symbol(old), _single_symbol(new)
    if o_sym is None or n_sym is None:
        return None
    old_by = {b.start: b for b in old}
    common = sorted(t for t in {b.start for b in new} if t in old_by)
    if not common:
        return None
    t = common[-1]
    nb = next(b for b in new if b.start == t)
    return RollGap(o_sym, n_sym, t.date(), t, round(nb.close - old_by[t].close, 6))


def shift_bar(bar: Bar, offset: float, symbol: str) -> Bar:
    r = lambda x: round(x + offset, 6)   # noqa: E731
    return replace(bar, symbol=symbol, open=r(bar.open), high=r(bar.high), low=r(bar.low), close=r(bar.close))


class ContinuousHistory:
    """Giữ `max_days` phiên gần nhất (bar thô theo HĐ) + chênh lệch roll; `view` dựng lịch sử đã dịch."""

    def __init__(self, max_days: int):
        if int(max_days) < 1:
            raise ValueError("max_days phải ≥ 1")
        self.max_days = int(max_days)
        self._days: list[tuple[date, str, tuple[Bar, ...]]] = []
        self._gaps: dict[tuple[str, str], float] = {}

    def add_day(self, d: date, contract: str, bars: Iterable[Bar]) -> None:
        bs = tuple(sorted(bars, key=lambda b: b.start))
        if any(b.symbol != contract for b in bs):
            raise ValueError(f"{d}: bar không thuộc {contract}")
        if self._days and self._days[-1][0] == d:
            self._days[-1] = (d, contract, bs)
            return
        if self._days and d < self._days[-1][0]:
            raise ValueError(f"Ngày {d} thêm sau {self._days[-1][0]}")
        self._days.append((d, contract, bs))
        del self._days[:-self.max_days]

    def add_gap(self, gap: RollGap) -> None:
        self._gaps[(gap.old, gap.new)] = float(gap.gap)

    def view(self, contract: str) -> tuple[Bar, ...]:
        out: list[tuple[Bar, ...]] = []
        current, offset = contract, 0.0
        for d, c, bs in reversed(self._days):
            if c != current:
                g = self._gaps.get((c, current))
                if g is None:
                    break                                   # không đo được ⇒ cắt tại roll này
                offset += g
                current = c
            out.append(bs if c == contract else tuple(shift_bar(b, offset, contract) for b in bs))
        return tuple(b for day in reversed(out) for b in day)


@dataclass(frozen=True)
class DayClose:
    day: date
    contract: str                    # HĐ bot giao dịch ngày đó
    next_contract: str               # HĐ phiên kế tiếp
    n_bars: int                      # số bar HĐ giao dịch đã lưu
    gap: Optional[RollGap]           # chỉ có ở ngày trước roll, khi đo được

    @property
    def is_roll_eve(self) -> bool:
        return self.next_contract != self.contract


class HistoryBuilder:
    """Logic theo ngày dùng CHUNG cho backtest và live: `observe` mọi bar đã đóng (mọi HĐ), `close_day` lúc
    hết phiên ⇒ lưu bar HĐ giao dịch; ngày trước roll đo chênh lệch với HĐ kế tiếp (dùng từ phiên sau)."""

    def __init__(self, max_days: int, trading_contract: Callable[[date], str],
                 next_trading_day: Callable[[date], date]):
        self.history = ContinuousHistory(max_days)
        self._tc, self._next_day = trading_contract, next_trading_day
        self._today: dict[date, list[Bar]] = {}
        self._eve: dict[date, list[Bar]] = {}

    def next_contract(self, d: date) -> str:
        return self._tc(self._next_day(d))

    def observe(self, bar: Bar) -> None:
        d = bar.start.date()
        tc = self._tc(d)
        if bar.symbol == tc:
            self._today.setdefault(d, []).append(bar)
        elif bar.symbol == self.next_contract(d):           # ≠ tc ⇒ chỉ xảy ra ở ngày trước roll
            self._eve.setdefault(d, []).append(bar)

    def close_day(self, d: date) -> DayClose:
        tc, nxt = self._tc(d), self.next_contract(d)
        today, eve = self._today.pop(d, []), self._eve.pop(d, [])
        if today:
            self.history.add_day(d, tc, today)
        gap = measure_gap(today, eve)                       # eve chỉ có bar ở ngày trước roll
        if gap is not None:
            self.history.add_gap(gap)
        return DayClose(d, tc, nxt, len(today), gap)

    def view(self, contract: str) -> tuple[Bar, ...]:
        return self.history.view(contract)
