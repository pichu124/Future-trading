"""Feed ghép: DNSE (chính) + SSI (dự phòng) chạy song song, đối chiếu liên tục (CLAUDE.md mục 3).

- `on_tick`: chỉ tick của nguồn ĐANG DÙNG (`active`) — strategy/risk nhận một dòng duy nhất.
- `on_any_tick`: tick của MỌI nguồn (recorder ghi cả hai để đối chiếu sau).
- `check()` (gọi sau mỗi tick và định kỳ bởi `run()`), chỉ trong phiên liên tục:
  * một feed không có tick quá `stale_seconds` (hoặc `run()` của nó đã chết) ⇒ STALE / FEED_DOWN;
    thời gian im tính từ mốc muộn nhất: tick cuối, lúc bắt đầu theo dõi, đầu đoạn phiên (09:00 /
    13:00) ⇒ không báo động giả lúc khởi động hay ngay sau nghỉ trưa;
  * giá hai nguồn (mid nếu có bid/ask còn mới, không thì giá khớp còn mới) lệch quá
    `divergence_ticks` bước ⇒ DIVERGENCE;
  * `healthy` = CẢ HAI nguồn có tick mới (≤ `stale_seconds`), không feed nào chết, không lệch giá
    (chưa đủ dữ liệu ⇒ False) ⇒ runner gán vào
    `AccountState.feed_healthy` ⇒ risk_manager ngừng mở vị thế mới khi False;
  * nguồn chính im mà nguồn dự phòng còn sống ⇒ chuyển `active` sang dự phòng (SWITCHED); nguồn
    chính sống lại liên tục `switch_back_after_s` giây ⇒ chuyển về (SWITCHED_BACK); nguồn dự
    phòng đang dùng mà im trong khi nguồn chính sống ⇒ chuyển về ngay.
- Cảnh báo chỉ phát khi trạng thái ĐỔI (không lặp mỗi tick); `on_alert` để nối Telegram.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

from core.clock import MarketClock, SessionState
from core.models import Tick
from feeds.base import DataFeed, TickHandler

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class AlertKind(str, Enum):
    STALE = "STALE"
    RECOVERED = "RECOVERED"
    DIVERGENCE = "DIVERGENCE"
    DIVERGENCE_CLEARED = "DIVERGENCE_CLEARED"
    SWITCHED = "SWITCHED"
    SWITCHED_BACK = "SWITCHED_BACK"
    FEED_DOWN = "FEED_DOWN"


@dataclass(frozen=True)
class FeedAlert:
    ts: datetime
    kind: AlertKind
    message: str
    source: str = ""


@dataclass
class _SourceState:
    last_tick_at: Optional[datetime] = None
    mid: Optional[float] = None
    mid_at: Optional[datetime] = None
    last: Optional[float] = None
    last_at: Optional[datetime] = None
    stale: bool = False
    down: bool = False
    fresh_since: Optional[datetime] = None


class CompositeFeed(DataFeed):
    name = "composite"

    def __init__(self, primary: DataFeed, backup: DataFeed, market_clock: MarketClock, *,
                 tick_size: float, divergence_ticks: float, stale_seconds: float,
                 switch_back_after_s: float = 30.0, monitor_interval_s: float = 1.0,
                 on_alert: Optional[Callable[[FeedAlert], None]] = None):
        super().__init__()
        if primary.name == backup.name:
            raise ValueError("Hai feed phải khác tên")
        self.primary, self.backup = primary, backup
        self.clock = market_clock
        self.tick_size = tick_size
        self.divergence_ticks = divergence_ticks
        self.stale_seconds = stale_seconds
        self.switch_back_after_s = switch_back_after_s
        self.monitor_interval_s = monitor_interval_s
        self._on_alert = on_alert
        self.active = primary.name
        self.alerts: list[FeedAlert] = []
        self._any_handlers: list[TickHandler] = []
        self._src = {primary.name: _SourceState(), backup.name: _SourceState()}
        self._diverged = False
        self._healthy = False
        self._watch_start: Optional[datetime] = None
        self._stopping = False
        self._counts: Counter = Counter()
        primary.on_tick(lambda t: self._on_source_tick(primary.name, t))
        backup.on_tick(lambda t: self._on_source_tick(backup.name, t))

    @classmethod
    def from_settings(cls, settings, primary: DataFeed, backup: DataFeed,
                      market_clock: MarketClock, **kw) -> "CompositeFeed":
        return cls(primary, backup, market_clock,
                   tick_size=float(settings.require("contract.tick_size_points")),
                   divergence_ticks=float(settings.require("feeds.feed_divergence_ticks")),
                   stale_seconds=float(settings.require("feeds.feed_stale_seconds")),
                   switch_back_after_s=float(settings.require("feeds.switch_back_after_seconds")),
                   **kw)

    def on_any_tick(self, handler: TickHandler) -> None:
        self._any_handlers.append(handler)

    @property
    def healthy(self) -> bool:
        return self._healthy

    # ------------------------------------------------------------------ tick ----

    def _on_source_tick(self, source: str, tick: Tick) -> None:
        now = self.clock.now()
        st = self._src[source]
        st.last_tick_at = now
        if tick.bid is not None and tick.ask is not None:
            st.mid, st.mid_at = (tick.bid + tick.ask) / 2.0, now
        if tick.last is not None:
            st.last, st.last_at = tick.last, now
        self._counts[source] += 1
        for h in self._any_handlers:
            self._safe(h, tick)
        self.check(now)
        if source == self.active:
            self._last_message_at = now
            for h in self._handlers:
                self._safe(h, tick)

    def _safe(self, handler, tick) -> None:
        try:
            handler(tick)
        except Exception:
            self._counts["callback_errors"] += 1
            log.exception("composite: lỗi ở bộ nhận tick")

    # ------------------------------------------------------------------ giám sát ----

    def _fresh(self, at: Optional[datetime], now: datetime) -> bool:
        return at is not None and (now - at).total_seconds() <= self.stale_seconds

    def _ref_price(self, st: _SourceState, now: datetime) -> Optional[float]:
        if st.mid is not None and self._fresh(st.mid_at, now):
            return st.mid
        if st.last is not None and self._fresh(st.last_at, now):
            return st.last
        return None

    def _alert(self, now: datetime, kind: AlertKind, message: str, source: str = "") -> FeedAlert:
        a = FeedAlert(now, kind, message, source)
        self.alerts.append(a)
        level = logging.INFO if kind in (AlertKind.RECOVERED, AlertKind.DIVERGENCE_CLEARED,
                                         AlertKind.SWITCHED_BACK) else logging.WARNING
        log.log(level, "composite %s: %s", kind.value, message)
        if self._on_alert is not None:
            try:
                self._on_alert(a)
            except Exception:
                log.exception("composite: lỗi ở on_alert")
        return a

    def check(self, now: Optional[datetime] = None) -> list[FeedAlert]:
        """Đánh giá lại sức khoẻ; trả cảnh báo MỚI phát sinh trong lần gọi này."""
        now = now or self.clock.now()
        new: list[FeedAlert] = []
        if self.clock.session_state(now) is not SessionState.CONTINUOUS:
            # ngoài phiên liên tục: im lặng là bình thường, không so giá
            self._healthy = not any(s.down for s in self._src.values())
            return new

        if self._watch_start is None:
            self._watch_start = now
        sched = self.clock.schedule
        seg = sched.continuous_pm_start if now.time() >= sched.continuous_pm_start else sched.continuous_am_start
        floor = max(self._watch_start, datetime.combine(now.date(), seg))
        for name, st in self._src.items():
            # im lặng tính từ mốc muộn nhất: tick cuối / lúc bắt đầu theo dõi / đầu đoạn phiên
            ref = max(st.last_tick_at, floor) if st.last_tick_at is not None else floor
            stale = st.down or (now - ref).total_seconds() > self.stale_seconds
            if stale and not st.stale:
                st.fresh_since = None
                if not st.down:
                    ago = "chưa có tick nào" if st.last_tick_at is None else \
                        f"im {(now - st.last_tick_at).total_seconds():.1f}s"
                    new.append(self._alert(now, AlertKind.STALE,
                                           f"feed {name} {ago} (> {self.stale_seconds:g}s)", name))
            elif not stale and st.stale:
                st.fresh_since = now
                new.append(self._alert(now, AlertKind.RECOVERED, f"feed {name} có tick trở lại", name))
            elif not stale and st.fresh_since is None:
                st.fresh_since = now
            st.stale = stale

        p = self._ref_price(self._src[self.primary.name], now)
        b = self._ref_price(self._src[self.backup.name], now)
        if p is not None and b is not None:
            gap = abs(p - b) / self.tick_size
            diverged = gap > self.divergence_ticks + 1e-9
            if diverged and not self._diverged:
                new.append(self._alert(now, AlertKind.DIVERGENCE,
                                       f"{self.primary.name} {p:g} vs {self.backup.name} {b:g}: lệch "
                                       f"{gap:.1f} bước > {self.divergence_ticks:g}"))
            elif not diverged and self._diverged:
                new.append(self._alert(now, AlertKind.DIVERGENCE_CLEARED,
                                       f"hai feed khớp lại (lệch {gap:.1f} bước)"))
            self._diverged = diverged

        ps, bs = self._src[self.primary.name], self._src[self.backup.name]
        if self.active == self.primary.name:
            if ps.stale and not bs.stale:
                self.active = self.backup.name
                new.append(self._alert(now, AlertKind.SWITCHED,
                                       f"chuyển nguồn chính sang {self.backup.name}", self.backup.name))
        else:
            stable = (not ps.stale and ps.fresh_since is not None
                      and (now - ps.fresh_since).total_seconds() >= self.switch_back_after_s)
            if not ps.stale and (stable or bs.stale):
                self.active = self.primary.name
                new.append(self._alert(now, AlertKind.SWITCHED_BACK,
                                       f"trở lại nguồn chính {self.primary.name}", self.primary.name))

        # mở vị thế chỉ khi CẢ HAI nguồn có dữ liệu mới và khớp nhau
        self._healthy = (not self._diverged and not ps.down and not bs.down
                         and self._fresh(ps.last_tick_at, now) and self._fresh(bs.last_tick_at, now))
        return new

    # ------------------------------------------------------------------ vòng đời ----

    async def _run_source(self, feed: DataFeed) -> None:
        try:
            await feed.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            st = self._src[feed.name]
            st.down = True
            self._alert(self.clock.now(), AlertKind.FEED_DOWN,
                        f"feed {feed.name} dừng: {type(exc).__name__}: {exc}", feed.name)
            self.check()
            raise

    async def _monitor(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.monitor_interval_s)
            self.check()

    async def run(self) -> None:
        self._stopping = False
        self._watch_start = self.clock.now()
        tasks = [asyncio.create_task(self._run_source(self.primary)),
                 asyncio.create_task(self._run_source(self.backup))]
        monitor = asyncio.create_task(self._monitor())
        errors: list[BaseException] = []
        try:
            for t in asyncio.as_completed(tasks):
                try:
                    await t
                except asyncio.CancelledError:
                    if self._stopping:
                        continue
                    raise
                except Exception as exc:
                    errors.append(exc)
            if len(errors) == len(tasks) and not self._stopping:
                raise RuntimeError("Cả hai feed đều dừng: " + "; ".join(map(str, errors))) from errors[-1]
        finally:
            monitor.cancel()
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(monitor, *tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stopping = True
        await asyncio.gather(self.primary.stop(), self.backup.stop(), return_exceptions=True)

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        """True khi KHÔNG còn nguồn nào có dữ liệu (trong phiên liên tục)."""
        now = now or self.clock.now()
        if self.clock.session_state(now) is not SessionState.CONTINUOUS:
            return False
        return all(s.down or not self._fresh(s.last_tick_at, now) for s in self._src.values())

    def stats(self) -> dict:
        return {"active": self.active, "healthy": self._healthy, "diverged": self._diverged,
                "ticks_by_source": {k: self._counts[k] for k in self._src},
                "callback_errors": self._counts["callback_errors"],
                "alerts": len(self.alerts),
                "sources": {self.primary.name: self.primary.stats(),
                            self.backup.name: self.backup.stats()}}
