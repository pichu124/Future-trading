"""Engine backtest — replay dữ liệu qua ĐÚNG pipeline live (CLAUDE.md mục 2, 6):

    nguồn sự kiện (Tick | Bar đã đóng) → strategy.on_bar → ExecutionManager → RiskManager.approve
    → PaperBroker (khớp theo tick) → RiskManager.record_fill → RiskManager.check → hành động

Chống look-ahead:
- Sự kiện phải đi theo thời gian không giảm; Bar được coi là sự kiện tại `start + interval`
  (lúc đóng). Nguồn đưa tick nằm trong một bar SAU khi đã phát bar đó ⇒ ValueError.
- Strategy chỉ thấy bar đã đóng; lệnh khớp ở tick sau (paper broker: ts > lúc đặt + độ trễ).
- `detect_lookahead` lật ngược dữ liệu sau một mốc rồi so tín hiệu trước mốc: chiến lược nhìn
  trước tương lai (qua dữ liệu được đưa vào) sẽ lộ ra.

Dữ liệu bar (không có bid/ask) ⇒ tick GIẢ, theo hướng bi quan:
- bid = giá − h, ask = giá + h với h = max(1, ⌈spread/2⌉) bước (`backtest.synthetic_spread_ticks`):
  cả mua lẫn bán đều chịu nửa spread (spread lẻ không còn cho một phía khớp đúng giá);
- đường đi O → cực trị gần O hơn → cực trị kia → C (`validation.bar_high_first`); không quyết
  được ⇒ đi về phía BẤT LỢI cho vị thế đang giữ + lệnh vào đang chờ (SL thắng, CLAUDE.md mục 6);
- mỗi cực trị phát hai tick cách nhau `độ trễ paper + 1s` (≥ 1s) ⇒ stop bị kích hoạt ở cực trị khớp
  tại chính cực trị (giả định xấu nhất) dù độ trễ dài; LO chờ chỉ khớp khi cực trị xuyên qua giá LO;
- thời điểm trong bar: O +1s, cực trị +15s và +30s (lặp sau thời gian giữ), C +59s; khối lượng sổ
  lệnh coi như vô hạn. Báo cáo luôn ghi rõ là "tick giả".
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence, Union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.report import TradeBook, TradeRecord  # noqa: E402
from backtest.validation import bar_high_first  # noqa: E402
from brokers.paper import PaperBroker, PaperConfig  # noqa: E402
from core.calendar import TradingCalendar  # noqa: E402
from core.clock import FakeClock, MarketClock  # noqa: E402
from core.execution import ExecEvent, ExecutionManager  # noqa: E402
from core.models import AccountState, Bar, Fill, Order, Signal, SignalAction, Tick  # noqa: E402
from core.settings import MissingSetting, Settings, load_settings  # noqa: E402
from risk.risk_manager import Rejected, RiskManager  # noqa: E402
from core.continuous import HistoryBuilder, check_gap_minutes  # noqa: E402
from strategy.base import Strategy, StrategyContext, history_days  # noqa: E402

Event = Union[Tick, Bar]

# vị trí tick giả trong bar 60 giây (giây kể từ start)
_T_OPEN, _T_X, _T_Y, _T_CLOSE = 1, 15, 30, 59


def event_time(ev: Event) -> datetime:
    return ev.ts if isinstance(ev, Tick) else ev.start + timedelta(seconds=ev.interval_s)


# ------------------------------------------------------------------- nguồn ----

def bars_from_ticks(ticks: Iterable[Tick], interval_s: int = 60) -> Iterator[Event]:
    """Tick (theo thời gian) → xen kẽ Tick và Bar đã đóng. Bar dựng từ tick có `last`.

    Bar phát ngay trước tick đầu tiên không còn thuộc bar đó, và ở cuối dòng dữ liệu.
    Tick chỉ có báo giá (không `last`) vẫn được chuyển tiếp cho broker nhưng không vào OHLC.
    """
    cur: dict[str, dict] = {}

    def make(sym: str, b: dict) -> Bar:
        return Bar(sym, b["start"], b["o"], b["h"], b["l"], b["c"], b["v"], interval_s)

    for t in ticks:
        for sym in sorted(cur):
            b = cur[sym]
            if t.ts >= b["start"] + timedelta(seconds=interval_s):
                yield make(sym, cur.pop(sym))
        if t.last is not None:
            epoch = datetime.combine(t.ts.date(), datetime.min.time())
            start = epoch + timedelta(seconds=((t.ts - epoch).total_seconds() // interval_s) * interval_s)
            b = cur.get(t.symbol)
            if b is None:
                cur[t.symbol] = {"start": start, "o": t.last, "h": t.last, "l": t.last,
                                 "c": t.last, "v": t.last_qty or 0}
            else:
                b["h"], b["l"], b["c"] = max(b["h"], t.last), min(b["l"], t.last), t.last
                b["v"] += t.last_qty or 0
        yield t
    for sym in sorted(cur, key=lambda s: cur[s]["start"]):
        yield make(sym, cur[sym])


def _exposure(state: AccountState, symbol: str) -> int:
    """Net hiện tại + phần chưa khớp của lệnh vào (tag "entry") đang chờ."""
    pending = sum(o.side.sign * (o.qty - o.filled_qty) for o in state.working_orders.values()
                  if o.symbol == symbol and o.tag == "entry")
    return state.net_qty(symbol) + pending


def synthetic_ticks(bar: Bar, net_qty: int, tick_size: float, spread_ticks: int,
                    hold_s: float = 1.0) -> list[Tick]:
    """Tick giả bi quan cho một bar (xem docstring module).

    `hold_s`: cực trị được giữ bao lâu (tick lặp lại sau `hold_s` giây) — phải DÀI HƠN độ trễ paper,
    nếu không lệnh stop kích hoạt ở cực trị sẽ khớp ở tick kế tiếp là cực trị KIA (lạc quan, golive #14).
    """
    high_first = bar_high_first(bar.open, bar.high, bar.low)
    if high_first is None:
        high_first = net_qty < 0          # Long/phẳng ⇒ đáy trước; Short ⇒ đỉnh trước
    x, y = (bar.high, bar.low) if high_first else (bar.low, bar.high)
    scale = bar.interval_s / 60.0
    t_x, t_y, t_close = _T_X * scale, _T_Y * scale, _T_CLOSE * scale
    hold = max(1.0, float(hold_s))
    hold_x = min(hold, t_y - t_x - 1.0)            # không chạm tới cực trị kia / giá đóng
    hold_y = min(hold, t_close - t_y - 1.0)
    path = [(_T_OPEN * scale, bar.open), (t_x, x), (t_x + hold_x, x), (t_y, y), (t_y + hold_y, y),
            (t_close, bar.close)]
    # mỗi phía cách giá ít nhất 1 bước: spread lẻ (vd 1) trước đây cho bán ở bid = giá ⇒ không mất gì
    half = 0.0 if spread_ticks <= 0 else max(1, -(-spread_ticks // 2)) * tick_size
    out = []
    for sec, px in path:
        out.append(Tick(bar.symbol, bar.start + timedelta(seconds=sec), last=px,
                        bid=round(px - half, 6), ask=round(px + half, 6), source="synthetic"))
    return out


BAR_COLUMNS = ("symbol", "start", "open", "high", "low", "close")


def _bar_files(path: Union[str, Path, Sequence[Union[str, Path]]]) -> list[Path]:
    """File/thư mục (đệ quy, vd `data/history/ohlc1m/<hợp đồng>/<ngày>.parquet`) → danh sách file."""
    items = list(path) if isinstance(path, (list, tuple)) else [path]
    files: list[Path] = []
    for it in items:
        p = Path(it)
        files += sorted(p.rglob("*.parquet")) if p.is_dir() else [p]   # file đang ghi dở có đuôi .tmp
    if not files:
        raise ValueError(f"Không có file Parquet bar nào trong {', '.join(str(x) for x in items)}")
    return files


def load_bars_parquet(path: Union[str, Path, Sequence[Union[str, Path]]],
                      stats: Optional[dict] = None) -> tuple[list[Bar], int]:
    """Đọc bar 1 phút (schema `import_dashboard_bars.py` / `download_history.py`). Trả (bars, số dòng hỏng bị bỏ).

    Nhận file, thư mục (đọc ĐỆ QUY, mỗi hợp đồng một thư mục con) hoặc danh sách các thứ đó. Bar trùng
    (symbol, start) giữa các nguồn ⇒ giữ bản có volume tin được, hoà thì bản đọc trước; `stats` (nếu
    truyền) nhận `files`, `duplicates`, `conflicts` (bản trùng khác giá OHLC).
    """
    import pandas as pd

    files = _bar_files(path)
    best: dict[tuple[str, datetime], Bar] = {}
    skipped = dups = conflicts = 0
    for f in files:
        df = pd.read_parquet(f)
        missing = [c for c in BAR_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"{f}: không phải file bar (thiếu cột {', '.join(missing)}) — trỏ --bars vào "
                             "thư mục bar (vd data/history/ohlc1m), không phải tick/trades")
        # thiếu cột ⇒ không xác nhận được nguồn ⇒ coi là KHÔNG tin được (golive #14)
        reliable = df["volume_reliable"] if "volume_reliable" in df else [False] * len(df)
        interval = df["interval_s"] if "interval_s" in df else [60] * len(df)
        volume = df["volume"] if "volume" in df else [0] * len(df)
        for row, rel, iv, vol in zip(df.itertuples(index=False), reliable, interval, volume):
            try:
                b = Bar(str(row.symbol), pd.Timestamp(row.start).to_pydatetime(),
                        float(row.open), float(row.high), float(row.low), float(row.close),
                        int(vol or 0), int(iv), bool(rel))
            except (ValueError, TypeError):
                skipped += 1
                continue
            key = (b.symbol, b.start)
            old = best.get(key)
            if old is None:
                best[key] = b
                continue
            dups += 1
            if (old.open, old.high, old.low, old.close) != (b.open, b.high, b.low, b.close):
                conflicts += 1
            if b.volume_reliable and not old.volume_reliable:
                best[key] = b
    if stats is not None:
        stats.update(files=len(files), duplicates=dups, conflicts=conflicts)
    bars = sorted(best.values(), key=lambda b: (b.start, b.symbol))
    return bars, skipped


# ------------------------------------------------------------------ kết quả ----

@dataclass
class BacktestResult:
    trades: list[TradeRecord]
    open_trades: list[TradeRecord]
    signals: list[tuple[datetime, Signal]]
    bar_log: list[tuple[datetime, Bar]]
    fills: list[Fill]
    rejections: list[Rejected]
    events: list[ExecEvent]
    warnings: list[str]
    open_orders_at_end: list[Order]
    data_quality: dict
    assumptions: dict


# ------------------------------------------------------------------- engine ----

class BacktestEngine:
    def __init__(self, settings: Settings, strategy: Strategy,
                 calendar: Optional[TradingCalendar] = None, trading_contract_only: bool = True):
        self.settings = settings
        self.strategy = strategy
        self.calendar = calendar or TradingCalendar.default()
        self.trading_contract_only = trading_contract_only
        self.history_days = history_days(strategy)
        if self.history_days and not trading_contract_only:
            raise ValueError("Chiến lược cần lịch sử nhiều ngày ⇒ phải chạy với trading_contract_only=True")
        # cùng khoá với live (live/history.py) ⇒ cùng chuỗi back-adjust; chỉ bắt buộc khi cần lịch sử
        self.gap_minutes = (check_gap_minutes(settings.require("contract.roll_gap_median_minutes"))
                            if self.history_days else None)
        # kiểm cấu hình NGAY (null ⇒ MissingSetting), không đợi tới lúc chạy
        self.paper_config = PaperConfig.from_settings(settings)
        # giữ cực trị lâu hơn độ trễ tối đa của paper (golive #14)
        self._hold_s = float(int((self.paper_config.latency_ms + self.paper_config.latency_jitter_ms) // 1000) + 1)
        self.spread_ticks = int(settings.require("backtest.synthetic_spread_ticks"))
        if self.spread_ticks < 0:
            raise ValueError("backtest.synthetic_spread_ticks phải ≥ 0")
        self.multiplier = float(settings.require("contract.multiplier_vnd_per_point"))
        probe = MarketClock.from_settings(settings, clock=FakeClock(datetime(2000, 1, 3)),
                                          calendar=self.calendar)
        RiskManager.from_settings(settings, probe, kill_file=None)

    # ---- API đồng bộ ----
    def run_bars(self, bars: Iterable[Bar]) -> BacktestResult:
        return asyncio.run(self.arun(bars, synthetic=True))

    def run_events(self, events: Iterable[Event]) -> BacktestResult:
        return asyncio.run(self.arun(events, synthetic=False))

    # ---- lõi ----
    async def arun(self, source: Iterable[Event], synthetic: bool) -> BacktestResult:
        clock = FakeClock(datetime(2000, 1, 3))
        mclock = MarketClock.from_settings(self.settings, clock=clock, calendar=self.calendar)
        risk = RiskManager.from_settings(self.settings, mclock, kill_file=None)
        state = AccountState()
        broker = PaperBroker(self.paper_config, clock, name=risk.limits.primary_broker, session=mclock)
        execu = ExecutionManager(broker, risk, state, clock, self.paper_config.tick_size)
        book = TradeBook(self.multiplier, tag_of=lambda cid: execu.orders[cid].tag
                         if cid in execu.orders else "")
        fills: list[Fill] = []
        broker.on_fill(fills.append)
        broker.on_fill(book.on_fill)

        signals: list[tuple[datetime, Signal]] = []
        bar_log: list[tuple[datetime, Bar]] = []
        warnings: list[str] = []
        bars_today: dict[str, list[Bar]] = {}
        quality = {"synthetic_ticks": synthetic, "n_bars": 0, "n_ticks": 0,
                   "unreliable_volume_bars": 0, "days": 0}
        last_ts: Optional[datetime] = None

        quality.update(rolls=[], other_contract_bars=0, days_missing_trading_contract=0, missing_trading_days=0)
        seen_days: dict[date, set[str]] = {}       # mọi hợp đồng có trong dữ liệu, theo ngày
        kept_days: dict[date, str] = {}            # hợp đồng thực sự đưa vào pipeline
        history = (HistoryBuilder(self.history_days, mclock.trading_contract, self.calendar.next_trading_day,
                                  gap_minutes=self.gap_minutes) if self.history_days else None)
        if history is not None:
            quality.update(roll_gaps=[], roll_gaps_missing=0)

        def keep(ev: Event) -> bool:
            d = (ev.ts if isinstance(ev, Tick) else ev.start).date()
            seen_days.setdefault(d, set()).add(ev.symbol)
            if history is not None and isinstance(ev, Bar):
                history.observe(ev)                # bar HĐ giao dịch + HĐ kế tiếp ngày trước roll
            if self.trading_contract_only and ev.symbol != mclock.trading_contract(d):
                if isinstance(ev, Bar):
                    quality["other_contract_bars"] += 1
                return False
            kept_days.setdefault(d, ev.symbol)
            return True

        def close_day(d: date) -> None:
            r = history.close_day(d)
            if not r.is_roll_eve:
                return
            if r.gap is None:
                quality["roll_gaps_missing"] += 1
                warnings.append(f"{d}: không đo được chênh lệch roll {r.contract} → {r.next_contract} (thiếu bar "
                                f"{r.next_contract} cùng phút với {r.contract}) — lịch sử nhiều ngày bị cắt tại roll "
                                f"này; tải thêm {r.next_contract} ngày {d}")
            else:
                quality["roll_gaps"].append(f"{d}: {r.contract} → {r.next_contract} {r.gap.gap:+.1f} điểm "
                                            f"(trung vị {r.gap.n_minutes} phút)")
        history_view: dict[str, tuple[Bar, ...]] = {}

        def expand() -> Iterator[Event]:
            for ev in source:
                if not keep(ev):
                    continue
                if synthetic:
                    if not isinstance(ev, Bar):
                        raise ValueError("run_bars chỉ nhận Bar")
                    # chiều bất lợi tính theo vị thế lúc bar bắt đầu + lệnh vào đang chờ
                    # (lệnh đặt cuối bar trước sẽ khớp ngay ở tick mở của bar này)
                    yield from synthetic_ticks(ev, _exposure(state, ev.symbol),
                                               self.paper_config.tick_size, self.spread_ticks,
                                               hold_s=self._hold_s)
                yield ev

        async def step() -> None:
            await execu.maintain()                 # như runner live: lệnh đóng kẹt, stop bị thiếu
            await execu.drain()
            actions = risk.check(state)
            if actions:
                await execu.handle_actions(actions)
                await execu.drain()

        for ev in expand():
            ts = event_time(ev)
            if last_ts is not None and ts < last_ts:
                raise ValueError(f"Sự kiện lùi thời gian ({ts} < {last_ts}) — nguồn dữ liệu sai "
                                 "thứ tự hoặc phát bar trước khi hết tick của bar (look-ahead)")
            # live kiểm mỗi giây; backtest chỉ có sự kiện ⇒ gặp khoảng dữ liệu thủng dài hơn ngưỡng
            # "mất dữ liệu khi có vị thế" thì chạy một vòng kiểm tại mốc đó (như runner sẽ làm)
            silence = risk.limits.flatten_on_feed_silence_seconds
            if state.market_data_at is not None and state.trading_day == ts.date() and \
                    (ts - state.market_data_at).total_seconds() > silence + 1:
                probe = state.market_data_at + timedelta(seconds=silence + 1)
                clock.set(probe)
                state.broker_last_seen[broker.name] = probe
                await step()
            if state.trading_day != ts.date():
                if state.trading_day is not None:
                    self._end_day(state, warnings)
                    if history is not None:
                        close_day(state.trading_day)
                risk.new_day(state, ts.date())
                bars_today.clear()
                history_view.clear()
                quality["days"] += 1
            clock.set(ts)
            state.broker_last_seen[broker.name] = ts
            if isinstance(ev, Tick):
                quality["n_ticks"] += 1
                mark = ev.last if ev.last is not None else ev.mid
                if mark is not None:
                    state.last_prices[ev.symbol] = mark
                    state.market_data_at = ts
                broker.on_tick(ev)
                await execu.drain()
            else:
                quality["n_bars"] += 1
                if not ev.volume_reliable:
                    quality["unreliable_volume_bars"] += 1
                state.last_prices[ev.symbol] = ev.close
                hist = bars_today.setdefault(ev.symbol, [])
                hist.append(ev)
                bar_log.append((ts, ev))
                await step()                       # flatten/kill trước khi strategy ra lệnh
                key = f"{broker.name}:{ev.symbol}"
                pos = state.positions.get(key)
                net = state.net_qty(ev.symbol)
                past = ()
                if history is not None:
                    if ev.symbol not in history_view:
                        history_view[ev.symbol] = history.view(ev.symbol)
                    past = history_view[ev.symbol]
                ctx = StrategyContext(net, pos.avg_price if pos and pos.qty else None, tuple(hist), past)
                for sig in self.strategy.on_bar(ev, ctx) or ():
                    if sig.symbol != ev.symbol:
                        raise ValueError(f"Tín hiệu cho {sig.symbol} từ bar {ev.symbol}")
                    signals.append((ts, sig))
                    await execu.on_signal(sig)
                    await execu.drain()
            await step()
            last_ts = ts

        if state.trading_day is not None:
            self._end_day(state, warnings)
        self._coverage(seen_days, kept_days, mclock, quality, warnings)
        marks = dict(state.last_prices)
        return BacktestResult(
            trades=list(book.closed), open_trades=book.open_trades(marks, last_ts),
            signals=signals, bar_log=bar_log, fills=fills, rejections=list(execu.rejections),
            events=list(execu.events), warnings=warnings,
            open_orders_at_end=await broker.open_orders(), data_quality=quality,
            assumptions=self._assumptions(synthetic))

    @staticmethod
    def _end_day(state: AccountState, warnings: list[str]) -> None:
        for p in state.positions.values():
            if p.qty != 0:
                warnings.append(f"{state.trading_day}: còn vị thế {p.symbol} {p.qty:+d} sau sự kiện "
                                "cuối ngày — flatten không chạy được (thiếu dữ liệu sau flatten_time?)")

    def _coverage(self, seen: dict, kept: dict, mclock: MarketClock, quality: dict, warnings: list[str]) -> None:
        """Chuỗi qua roll: ngày roll, ngày thiếu hợp đồng giao dịch, ngày giao dịch không có dữ liệu.

        Không back-adjust: bot phẳng mỗi đêm nên mỗi ngày chỉ cần đúng một hợp đồng, P&L trên giá thật.
        """
        if not seen:
            return
        if self.trading_contract_only:
            prev = None
            for d in sorted(seen):
                tc = mclock.trading_contract(d)
                if d not in kept:
                    quality["days_missing_trading_contract"] += 1
                    warnings.append(f"{d}: có dữ liệu {', '.join(sorted(seen[d]))} nhưng thiếu hợp đồng giao "
                                    f"dịch {tc} — bỏ cả ngày (tải thêm hợp đồng đó)")
                    continue
                if prev is not None and kept[d] != prev:
                    quality["rolls"].append(f"{d}: {prev} → {kept[d]}")
                prev = kept[d]
        missing = []
        d, last = min(seen), max(seen)
        while d <= last:
            if self.calendar.is_trading_day(d) and d not in seen:
                missing.append(d)
            d += timedelta(days=1)
        quality["missing_trading_days"] = len(missing)
        if missing:
            shown = ", ".join(str(x) for x in missing[:10]) + (" …" if len(missing) > 10 else "")
            warnings.append(f"{len(missing)} ngày giao dịch không có dữ liệu nào: {shown}")

    def _assumptions(self, synthetic: bool) -> dict:
        c = self.paper_config
        out = {
            "slippage_ticks": c.slippage_ticks,
            "fee_vnd_per_contract_side": c.fee_vnd_per_contract,
            "latency_ms": c.latency_ms,
            "latency_jitter_ms": c.latency_jitter_ms,
            "multiplier_vnd_per_point": self.multiplier,
        }
        if synthetic:
            out["synthetic_spread_ticks"] = self.spread_ticks
        if self.history_days:
            out["history"] = (f"{self.history_days} phiên trước, back-adjust cộng điểm tại thời điểm (chênh lệch = "
                              f"trung vị {self.gap_minutes} phút chung cuối của ngày trước roll, không dùng roll "
                              "tương lai)")
        return out


# --------------------------------------------------------- dò look-ahead ----

@dataclass
class LookAheadReport:
    detected: bool
    n_signals_compared: int
    first_difference: Optional[tuple]


def _mirror_after(bars: Sequence[Bar], cut: datetime) -> list[Bar]:
    """Lật ngược mọi bar có start ≥ cut quanh giá đóng của bar cuối trước cut."""
    before = [b for b in bars if b.start < cut]
    if not before:
        raise ValueError("Mốc cắt phải sau ít nhất một bar")
    pivots = {}
    for b in before:
        pivots[b.symbol] = b.close
    out = []
    for b in bars:
        if b.start < cut or b.symbol not in pivots:
            out.append(b)
            continue
        p = pivots[b.symbol]
        m = lambda x: round(2 * p - x, 6)   # noqa: E731
        out.append(replace(b, open=m(b.open), high=m(b.low), low=m(b.high), close=m(b.close)))
    return out


def _signal_key(ts: datetime, s: Signal) -> tuple:
    return (ts, s.symbol, s.action.value, s.qty, s.stop_loss, s.take_profit)


def detect_lookahead(settings: Settings, make_strategy: Callable[[list[Bar]], Strategy],
                     bars: Sequence[Bar], cut: datetime,
                     calendar: Optional[TradingCalendar] = None) -> LookAheadReport:
    """Chạy hai lần: dữ liệu thật và dữ liệu đã lật ngược sau `cut`. Tín hiệu phát tại thời
    điểm ≤ cut phải giống hệt nhau — khác ⇒ chiến lược đã nhìn dữ liệu tương lai.

    `make_strategy(data)` nhận đúng bộ dữ liệu sẽ chạy (chiến lược trung thực bỏ qua nó).
    Chỉ bắt được rò rỉ qua dữ liệu đưa vào; không thay được việc đọc code chiến lược.
    """
    original = list(bars)
    mirrored = _mirror_after(original, cut)

    def sigs(data: list[Bar]) -> list[tuple]:
        res = BacktestEngine(settings, make_strategy(data), calendar=calendar).run_bars(data)
        return [_signal_key(ts, s) for ts, s in res.signals
                if ts <= cut and s.action is not SignalAction.HOLD]

    a, b = sigs(original), sigs(mirrored)
    first = next(((x, y) for x, y in zip(a, b) if x != y), None)
    if first is None and len(a) != len(b):
        first = (a[len(b)] if len(a) > len(b) else None, b[len(a)] if len(b) > len(a) else None)
    return LookAheadReport(detected=first is not None, n_signals_compared=len(a),
                           first_difference=first)


# ----------------------------------------------------------------------- CLI ----

def _load_strategy(spec: str, settings: Settings) -> Strategy:
    mod_name, _, cls_name = spec.partition(":")
    cls = getattr(importlib.import_module(mod_name), cls_name)
    return cls.from_settings(settings) if hasattr(cls, "from_settings") else cls()


def main(argv: Optional[Sequence[str]] = None) -> int:
    from backtest.report import build_report, write_html

    ap = argparse.ArgumentParser(description="Backtest VN30F trên bar 1 phút (Parquet)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bars", nargs="+",
                     help="file/thư mục Parquet bar 1 phút (đọc đệ quy, nhiều nguồn được; trùng ⇒ giữ volume tin được)")
    src.add_argument("--ticks", nargs="+",
                     help="tick thật: thư mục ngày recorder (data/raw/<ngày>) và/hoặc khớp lệnh downloader "
                          "(data/history/trades) — đọc đệ quy")
    ap.add_argument("--source", help="với --ticks: chỉ dùng nguồn này (bắt buộc khi dữ liệu có nhiều nguồn)")
    ap.add_argument("--strategy", required=True, help="module:Class, vd strategy.baseline_orb:ORB")
    ap.add_argument("--split", help="ngày bắt đầu out-of-sample (YYYY-MM-DD)")
    ap.add_argument("--n-trials", type=int, default=1, help="tổng số cấu hình đã thử (research/log.md)")
    ap.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM-DD (gồm cả ngày này)")
    ap.add_argument("--out", default="reports/backtest.html")
    args = ap.parse_args(argv)

    settings = load_settings()
    try:
        engine = BacktestEngine(settings, _load_strategy(args.strategy, settings))
    except MissingSetting as exc:
        print(f"❌ {exc.args[0]}\n   Chạy `python -m core.settings` để xem mọi khoá còn thiếu.")
        return 2
    load_stats: dict = {}
    skipped = n_fake_quotes = 0

    def in_range(d: date) -> bool:
        return (not args.date_from or d >= date.fromisoformat(args.date_from)) and \
            (not args.date_to or d <= date.fromisoformat(args.date_to))
    if args.ticks:
        from backtest.ticks import fill_missing_quotes, load_tick_files
        try:
            ticks = [t for t in load_tick_files(args.ticks, source=args.source) if in_range(t.ts.date())]
        except ValueError as exc:
            print(f"❌ {exc}")
            return 2
        ticks, n_fake_quotes = fill_missing_quotes(ticks, engine.paper_config.tick_size, engine.spread_ticks)
        res = engine.run_events(bars_from_ticks(ticks))
    else:
        bars, skipped = load_bars_parquet(args.bars, stats=load_stats)
        res = engine.run_bars([b for b in bars if in_range(b.start.date())])
    rep = build_report(res, split_date=date.fromisoformat(args.split) if args.split else None,
                       n_trials=args.n_trials, title=args.strategy)
    if skipped:
        rep["warnings"].append(f"Bỏ {skipped} dòng bar hỏng khi đọc Parquet")
    if n_fake_quotes:
        rep["warnings"].append(f"{n_fake_quotes} tick khớp không có bid/ask (vd khớp lệnh tải từ DNSE) — khớp lệnh "
                               f"trên báo giá giả last ∓ nửa spread ({engine.spread_ticks} bước, "
                               "backtest.synthetic_spread_ticks)")
    if load_stats.get("duplicates"):
        rep["warnings"].append(f"{load_stats['duplicates']} bar trùng giữa các nguồn ({load_stats['conflicts']} "
                               "bản khác giá OHLC) — giữ bản có volume tin được")
    write_html(rep, Path(args.out))
    m = rep["sections"]["all"]["metrics"]
    print(f"{m['n_trades']} lệnh, P&L sau phí {m['total_pnl_vnd']:,.0f} VNĐ → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
