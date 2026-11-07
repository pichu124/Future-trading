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
- bid = giá − ⌊spread/2⌋ bước, ask = bid + spread bước (`backtest.synthetic_spread_ticks`);
- đường đi O → cực trị gần O hơn → cực trị kia → C (`validation.bar_high_first`); không quyết
  được ⇒ đi về phía BẤT LỢI cho vị thế đang giữ + lệnh vào đang chờ (SL thắng, CLAUDE.md mục 6);
- mỗi cực trị phát hai tick liền nhau ⇒ stop bị kích hoạt ở cực trị khớp tại chính cực trị
  (giả định xấu nhất), LO chờ chỉ khớp khi cực trị xuyên qua giá LO;
- thời điểm trong bar: O +1s, cực trị +15s/+16s và +30s/+31s, C +59s; khối lượng sổ lệnh coi
  như vô hạn. Báo cáo luôn ghi rõ là "tick giả".
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
from strategy.base import Strategy, StrategyContext  # noqa: E402

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


def synthetic_ticks(bar: Bar, net_qty: int, tick_size: float, spread_ticks: int) -> list[Tick]:
    """Tick giả bi quan cho một bar (xem docstring module)."""
    high_first = bar_high_first(bar.open, bar.high, bar.low)
    if high_first is None:
        high_first = net_qty < 0          # Long/phẳng ⇒ đáy trước; Short ⇒ đỉnh trước
    x, y = (bar.high, bar.low) if high_first else (bar.low, bar.high)
    scale = bar.interval_s / 60.0
    path = [(_T_OPEN, bar.open), (_T_X, x), (_T_X + 1, x), (_T_Y, y), (_T_Y + 1, y),
            (_T_CLOSE, bar.close)]
    down = (spread_ticks // 2) * tick_size
    out = []
    for sec, px in path:
        bid = round(px - down, 6)
        ask = round(bid + spread_ticks * tick_size, 6)
        out.append(Tick(bar.symbol, bar.start + timedelta(seconds=sec * scale), last=px,
                        bid=bid, ask=ask, source="synthetic"))
    return out


def load_bars_parquet(path: Union[str, Path, Sequence[Path]]) -> tuple[list[Bar], int]:
    """Đọc bar 1 phút (schema của scripts/import_dashboard_bars.py). Trả (bars, số dòng hỏng bị bỏ)."""
    import pandas as pd

    p = path if isinstance(path, (list, tuple)) else Path(path)
    files = list(p) if isinstance(p, (list, tuple)) else (sorted(p.glob("*.parquet")) if p.is_dir() else [p])
    bars: list[Bar] = []
    skipped = 0
    for f in files:
        df = pd.read_parquet(f)
        # thiếu cột ⇒ không xác nhận được nguồn ⇒ coi là KHÔNG tin được (golive #14)
        reliable = df["volume_reliable"] if "volume_reliable" in df else [False] * len(df)
        interval = df["interval_s"] if "interval_s" in df else [60] * len(df)
        for row, rel, iv in zip(df.itertuples(index=False), reliable, interval):
            try:
                bars.append(Bar(str(row.symbol), pd.Timestamp(row.start).to_pydatetime(),
                                float(row.open), float(row.high), float(row.low), float(row.close),
                                int(row.volume or 0), int(iv), bool(rel)))
            except (ValueError, TypeError):
                skipped += 1
    bars.sort(key=lambda b: (b.start, b.symbol))
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
        # kiểm cấu hình NGAY (null ⇒ MissingSetting), không đợi tới lúc chạy
        self.paper_config = PaperConfig.from_settings(settings)
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

        def keep(ev: Event) -> bool:
            if not self.trading_contract_only:
                return True
            d = (ev.ts if isinstance(ev, Tick) else ev.start).date()
            return ev.symbol == mclock.trading_contract(d)

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
                                               self.paper_config.tick_size, self.spread_ticks)
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
                risk.new_day(state, ts.date())
                bars_today.clear()
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
                ctx = StrategyContext(net, pos.avg_price if pos and pos.qty else None, tuple(hist))
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
    ap.add_argument("--bars", required=True, help="thư mục hoặc file Parquet bar 1 phút")
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
    bars, skipped = load_bars_parquet(args.bars)
    if args.date_from:
        bars = [b for b in bars if b.start.date() >= date.fromisoformat(args.date_from)]
    if args.date_to:
        bars = [b for b in bars if b.start.date() <= date.fromisoformat(args.date_to)]
    res = engine.run_bars(bars)
    rep = build_report(res, split_date=date.fromisoformat(args.split) if args.split else None,
                       n_trials=args.n_trials, title=args.strategy)
    if skipped:
        rep["warnings"].append(f"Bỏ {skipped} dòng bar hỏng khi đọc Parquet")
    write_html(rep, Path(args.out))
    m = rep["sections"]["all"]["metrics"]
    print(f"{m['n_trades']} lệnh, P&L sau phí {m['total_pnl_vnd']:,.0f} VNĐ → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
