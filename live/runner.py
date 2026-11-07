"""Runner live/paper (PROMPTS P5.1) — vòng lặp sự kiện một ngày giao dịch.

Khởi động (`startup`):
  ngày nghỉ ⇒ thoát 0; `risk.new_day`; nạp bộ đếm trong ngày đã lưu (`live/state_store.py` —
  khởi động lại giữa phiên không xoá giới hạn lỗ ngày/lượt giao dịch); ĐỐI SOÁT vị thế + lệnh chờ
  từ broker (broker là sự thật); thấy vị thế có sẵn ⇒ cảnh báo (risk sẽ đòi stop hoặc đóng).
Mỗi vòng (`step`, mặc định mỗi giây):
  1. heartbeat khoá phiên (mỗi `heartbeat_s`) — mất khoá ⇒ DỪNG NGAY, không gửi thêm lệnh (thoát 3);
     lưu bộ đếm trong ngày;
  2. ping broker ⇒ `broker_last_seen` (TRƯỚC mọi quyết định của vòng này);
  3. xử lý tick đã nhận: giá đánh dấu, paper broker khớp lệnh, dựng bar; sức khoẻ feed ⇒
     `state.feed_healthy`;
  4. bar đóng theo đồng hồ ⇒ strategy ⇒ ExecutionManager (qua risk);
  5. lệnh Telegram: /kill /status /pnl /help;
  6. `risk.check` ⇒ hành động (flatten, kill, khẩn cấp); gỡ hedge khi broker chính sống lại;
  7. kết thúc: kill + đã phẳng ⇒ báo cáo, thoát 0 (quá `kill_flat_timeout_s` chưa phẳng ⇒ thoát 1);
     sau giờ đóng cửa ⇒ báo cáo, thoát 0 nếu phẳng, 1 nếu còn vị thế.
Báo cáo cuối ngày: lệnh, P&L sau phí, lệnh bị risk từ chối kèm lý do, cảnh báo — gửi Telegram và
ghi `<report_dir>/daily_YYYY-MM-DD.txt`.

Chạy: `python -m live.runner --assume-computed-contract` (paper). Live cần TRADING_MODE=live VÀ
`--live`, và adapter broker thật (P4.1/P4.2 — chưa có ⇒ từ chối).
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import sys
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.report import TradeBook, compute_metrics  # noqa: E402
from brokers.base import BrokerAdapter  # noqa: E402
from brokers.paper import PaperBroker  # noqa: E402
from core.bars import BarBuilder  # noqa: E402
from core.clock import MarketClock  # noqa: E402
from core.execution import ExecutionManager  # noqa: E402
from core.models import AccountState, Bar, Tick  # noqa: E402
from core.settings import Settings  # noqa: E402
from feeds.base import DataFeed  # noqa: E402
from live.emergency import EmergencyHandler  # noqa: E402
from live.instance_lock import LockHeld  # noqa: E402
from live.state_store import load_daily_state, save_daily_state  # noqa: E402
from risk.risk_manager import RiskManager  # noqa: E402
from strategy.base import Strategy, StrategyContext  # noqa: E402
from utils.atomic_io import write_text_atomic  # noqa: E402

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

HELP = "/kill — huỷ lệnh chờ, đóng vị thế, dừng bot · /status — trạng thái · /pnl — lãi/lỗ hôm nay"


@dataclass
class RunnerConfig:
    tick_s: float = 1.0
    heartbeat_s: float = 30.0
    command_poll_s: float = 3.0
    kill_flat_timeout_s: float = 120.0
    bar_grace_s: float = 0.5
    max_consecutive_errors: int = 5


class Runner:
    def __init__(self, *, settings: Settings, strategy: Strategy, feed: DataFeed,
                 broker: BrokerAdapter, market_clock: MarketClock, risk: RiskManager,
                 state: AccountState, backup_broker: Optional[BrokerAdapter] = None, lock=None,
                 notify: Optional[Callable[[str], Any]] = None,
                 fetch_commands: Optional[Callable[[], Awaitable[list[str]]]] = None,
                 state_path: Optional[Path] = None, report_dir: Optional[Path] = None,
                 config: RunnerConfig = RunnerConfig(), mode: str = "paper"):
        self.settings, self.strategy, self.feed = settings, strategy, feed
        self.broker, self.backup_broker = broker, backup_broker
        self.clock, self.risk, self.state = market_clock, risk, state
        self.lock, self.notify, self.fetch_commands = lock, notify, fetch_commands
        self.state_path, self.report_dir = state_path, report_dir
        self.config, self.mode = config, mode
        self.multiplier = float(settings.require("contract.multiplier_vnd_per_point"))
        self.brokers = {broker.name: broker}
        self.emergency = None
        if backup_broker is not None:
            self.brokers[backup_broker.name] = backup_broker
            self.emergency = EmergencyHandler(risk, state, self.brokers, primary=broker.name,
                                              backup=backup_broker.name, clock=market_clock,
                                              notify=notify)
        self.execution = ExecutionManager(broker, risk, state, market_clock.clock,
                                          float(settings.require("contract.tick_size_points")),
                                          emergency=self.emergency)
        self.book = TradeBook(self.multiplier, tag_of=lambda cid: self.execution.orders[cid].tag
                              if cid in self.execution.orders else "")
        for b in self.brokers.values():
            b.on_fill(self.book.on_fill)
        self.bars = BarBuilder(grace_s=config.bar_grace_s)
        self._bars_today: dict[str, list[Bar]] = {}
        self._ticks: deque[Tick] = deque()
        feed.on_tick(self._ticks.append)
        self._last_heartbeat: Optional[datetime] = None
        self._last_commands: Optional[datetime] = None
        self._kill_seen_at: Optional[datetime] = None
        self._alerts: list[str] = []
        self._halted = False
        self._reported = False
        self._saved_key: Optional[tuple] = None

    # ------------------------------------------------------------------ tiện ích ----

    async def _say(self, text: str) -> None:
        self._alerts.append(text) if text.startswith(("⚠️", "🚨")) else None
        log.info("runner: %s", text)
        if self.notify is None:
            return
        try:
            res = self.notify(text)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("runner: gửi thông báo lỗi")

    def _flat(self) -> bool:
        return (not any(p.qty != 0 for p in self.state.positions.values())
                and not self.state.working_orders)

    def _counters(self) -> tuple:
        s = self.state
        return (s.trades_today, s.consecutive_losses, s.realized_points_today, s.fees_points_today,
                s.locked_for_day, s.kill_reason, s.emergency_reason, len(s.approved_order_ids))

    def _save_if_changed(self) -> None:
        """Lưu ngay khi bộ đếm rủi ro đổi — mất điện ngay sau khi chạm ngưỡng không được làm mất khoá (golive #12)."""
        key = self._counters()
        if key != self._saved_key:
            self._save()
            self._saved_key = key

    def _save(self) -> None:
        if self.state_path is not None:
            try:
                save_daily_state(self.state, self.state_path)
            except Exception:
                log.exception("runner: lưu trạng thái lỗi")

    # ------------------------------------------------------------------ khởi động ----

    async def startup(self) -> Optional[int]:
        now = self.clock.now()
        if not self.clock.is_trading_day(now.date()):
            log.info("runner: %s không phải ngày giao dịch", now.date())
            return 0
        self.risk.new_day(self.state, now.date())
        if self.state_path is not None and load_daily_state(self.state_path, self.state):
            await self._say(f"Khôi phục bộ đếm hôm nay: {self.state.trades_today} lượt, "
                            f"lãi/lỗ {self.state.realized_points_today:+.1f} điểm")
        for name, b in self.brokers.items():
            await b.connect()
            for sym, pos in (await b.positions()).items():
                if pos.qty != 0:
                    self.state.positions[f"{name}:{sym}"] = replace(pos, broker=name)
            for o in await b.open_orders():
                self.state.working_orders[o.client_order_id] = o
            if await self._ping(b):
                self.state.broker_last_seen[name] = now
        held = [f"{p.broker} {p.symbol} {p.qty:+d}" for p in self.state.positions.values() if p.qty]
        if held:
            await self._say(f"⚠️ Khởi động thấy vị thế có sẵn trên broker: {', '.join(held)} — bot nhận "
                            "vị thế này; thiếu stop sẽ bị đóng sau stop_grace_seconds")
        self._last_heartbeat = now
        self.state.market_data_at = now                  # mốc chờ dữ liệu đầu tiên sau khởi động
        self._save()
        await self._say(f"🤖 Runner {self.mode} khởi động {now:%Y-%m-%d %H:%M:%S}")
        return None

    async def _ping(self, b: BrokerAdapter) -> bool:
        try:
            return bool(await b.ping())
        except Exception:
            return False

    # ------------------------------------------------------------------ một vòng ----

    async def step(self) -> Optional[int]:
        if self._halted:
            return 3
        now = self.clock.now()
        if self.lock is not None and self._last_heartbeat is not None and \
                (now - self._last_heartbeat).total_seconds() >= self.config.heartbeat_s:
            try:
                self.lock.heartbeat()
            except (LockHeld, RuntimeError) as exc:
                self._halted = True
                self.execution.halted = True
                await self._say(f"🚨 Mất khoá phiên ({exc}) — DỪNG NGAY, không gửi thêm lệnh. "
                                "Kiểm tra máy còn lại và vị thế trên app.")
                return 3
            self._last_heartbeat = now
            self._save()

        # trạng thái kết nối/feed cập nhật TRƯỚC khi ra quyết định trong vòng này
        for name, b in self.brokers.items():
            if await self._ping(b):
                self.state.broker_last_seen[name] = now

        new_bars: list[Bar] = []
        while self._ticks:
            t = self._ticks.popleft()
            mark = t.last if t.last is not None else t.mid
            if mark is not None:
                self.state.last_prices[t.symbol] = mark
                self.state.market_data_at = now          # luật mất dữ liệu khi có vị thế (golive #9)
            for b in self.brokers.values():
                if isinstance(b, PaperBroker):
                    b.on_tick(t)
            await self.execution.drain()      # từng tick: huỷ TP/stop còn lại TRƯỚC tick sau (golive #4)
            new_bars += self.bars.on_tick(t)
        healthy = getattr(self.feed, "healthy", None)
        self.state.feed_healthy = (not self.feed.is_stale(now)) if healthy is None else bool(healthy)
        new_bars += self.bars.on_time(now)
        for bar in new_bars:
            await self._on_bar(bar)

        await self._commands(now)

        await self.execution.maintain()
        await self.execution.drain()
        actions = self.risk.check(self.state)
        if actions:
            await self.execution.handle_actions(actions)
            await self.execution.drain()
        self._save_if_changed()
        if self.emergency is not None and self.emergency.needs_unwind() and \
                self.risk.broker_alive(self.state, self.broker.name):
            await self.emergency.unwind()

        if self.state.kill_reason and self._kill_seen_at is None:
            self._kill_seen_at = now
        if self._kill_seen_at is not None:
            if self._flat():
                await self._finish(now, "kill-switch")
                return 0
            if (now - self._kill_seen_at).total_seconds() >= self.config.kill_flat_timeout_s:
                await self._finish(now, "kill-switch (quá hạn chờ đóng vị thế)")
                return 1
        if now.time() >= self.clock.schedule.close:
            flat = self._flat()
            await self._finish(now, "hết phiên")
            return 0 if flat else 1
        return None

    async def _on_bar(self, bar: Bar) -> None:
        hist = self._bars_today.setdefault(bar.symbol, [])
        hist.append(bar)
        net = self.state.net_qty(bar.symbol)
        pos = next((p for p in self.state.positions.values()
                    if p.symbol == bar.symbol and p.broker == self.broker.name and p.qty), None)
        ctx = StrategyContext(net, pos.avg_price if pos else None, tuple(hist))
        try:
            signals = list(self.strategy.on_bar(bar, ctx) or ())
        except Exception:
            log.exception("runner: strategy lỗi ở bar %s", bar.start)
            await self._say(f"⚠️ Strategy lỗi ở bar {bar.start:%H:%M} — bỏ qua bar này")
            return
        for sig in signals:
            await self.execution.on_signal(sig)
            await self.execution.drain()

    # ------------------------------------------------------------------ Telegram ----

    async def _commands(self, now: datetime) -> None:
        if self.fetch_commands is None:
            return
        if self._last_commands is not None and \
                (now - self._last_commands).total_seconds() < self.config.command_poll_s:
            return
        self._last_commands = now
        try:
            cmds = await self.fetch_commands()
        except Exception as exc:
            log.warning("runner: đọc lệnh Telegram lỗi: %s", exc)
            return
        for c in cmds:
            if c == "kill":
                self.risk.kill(self.state, "telegram /kill")
                await self._say("🛑 Nhận /kill — huỷ lệnh chờ, đóng vị thế, dừng bot")
            elif c == "status":
                await self._say(self.status_text())
            elif c == "pnl":
                await self._say(self.pnl_text())
            elif c in ("help", "start"):
                await self._say(HELP)
            else:
                await self._say(f"Lệnh /{c} không hỗ trợ. {HELP}")

    def status_text(self) -> str:
        now = self.clock.now()
        pos = ", ".join(f"{p.symbol} {p.qty:+d} @{p.avg_price:.1f} ({p.broker})"
                        for p in self.state.positions.values() if p.qty) or "phẳng"
        seen = {b: f"{(now - t).total_seconds():.0f}s" for b, t in self.state.broker_last_seen.items()}
        locks = [x for x in (self.state.kill_reason and f"kill: {self.state.kill_reason}",
                             self.state.emergency_reason and f"khẩn cấp: {self.state.emergency_reason}",
                             self.state.locked_for_day and "khoá ngày (lỗ)") if x]
        return (f"📟 Trạng thái {now:%H:%M:%S} ({self.mode})\n"
                f"Vị thế: {pos}\nLệnh chờ: {len(self.state.working_orders)}\n"
                f"Feed khoẻ: {self.state.feed_healthy} · broker ping: {seen}\n"
                f"Lượt hôm nay: {self.state.trades_today} · thua liên tiếp: {self.state.consecutive_losses}\n"
                f"Khoá: {', '.join(locks) or 'không'}")

    def pnl_text(self) -> str:
        pnl = self.risk.daily_pnl_points(self.state)
        pnl_txt = "—" if pnl is None else f"{pnl:+.1f} điểm ({pnl * self.multiplier:+,.0f} VNĐ)"
        return (f"💰 P&L hôm nay (sau phí, gồm chưa chốt): {pnl_txt}\n"
                f"Đã chốt {self.state.realized_points_today:+.1f} điểm, phí "
                f"{self.state.fees_points_today:.2f} điểm")

    # ------------------------------------------------------------------ cuối ngày ----

    def daily_report(self, reason: str) -> str:
        now = self.clock.now()
        trades = list(self.book.closed)
        m = compute_metrics(trades)
        lines = [f"📊 Báo cáo {now:%Y-%m-%d} ({self.mode}) — kết thúc: {reason}",
                 f"Lệnh: {m['n_trades']} (thắng {m['n_wins']} / thua {m['n_losses']}) · "
                 f"P&L sau phí: {m['total_pnl_vnd']:,.0f} VNĐ ({m['total_pnl_points']:+.1f} điểm) · "
                 f"phí {m['fees_vnd']:,.0f} VNĐ"]
        for t in trades:
            lines.append(f"  {t.entry_ts:%H:%M:%S} {t.side} {t.qty} @{t.entry_price:.1f} → "
                         f"{t.exit_price:.1f} {t.exit_ts:%H:%M:%S} ({t.exit_reason}) {t.pnl_vnd:+,.0f}")
        rej = self.execution.rejections
        lines.append(f"Risk từ chối: {len(rej)}" + (
            " — " + ", ".join(f"{k}: {n}" for k, n in Counter(r.code.value for r in rej).items())
            if rej else ""))
        for r in rej[-10:]:
            lines.append(f"  {r}")
        held = [f"{p.broker} {p.symbol} {p.qty:+d}" for p in self.state.positions.values() if p.qty]
        lines.append("Vị thế cuối: " + ("phẳng" if not held else "🚨 CÒN VỊ THẾ " + ", ".join(held)))
        if self.state.working_orders:
            lines.append(f"🚨 Còn {len(self.state.working_orders)} lệnh chờ")
        if self._alerts:
            lines.append(f"Cảnh báo trong ngày: {len(self._alerts)}")
            lines += [f"  {a}" for a in self._alerts[-10:]]
        return "\n".join(lines)

    async def _finish(self, now: datetime, reason: str) -> None:
        if self._reported:
            return
        self._reported = True
        text = self.daily_report(reason)
        if self.report_dir is not None:
            try:
                write_text_atomic(text, Path(self.report_dir) / f"daily_{now:%Y-%m-%d}.txt")
            except Exception:
                log.exception("runner: ghi báo cáo lỗi")
        self._save()
        await self._say(text)

    # ------------------------------------------------------------------ vòng lặp ----

    async def _on_loop_errors(self, errors: int) -> Optional[int]:
        """Lỗi lặp lại ⇒ kill + cố đóng vị thế bằng đường tối thiểu; quá gấp đôi ngưỡng ⇒ bỏ cuộc."""
        n = self.config.max_consecutive_errors
        if errors < n:
            return None
        self.risk.kill(self.state, f"lỗi vòng lặp lặp lại {errors} lần")
        try:
            await self.execution.handle_actions(self.risk.check(self.state))
            await self.execution.drain()
        except Exception:
            log.exception("runner: đóng vị thế khi lỗi vòng lặp cũng lỗi")
        now = self.clock.now()
        if self._flat():
            await self._finish(now, "dừng vì lỗi vòng lặp")
            return 1
        if errors >= 2 * n:
            await self._finish(now, "dừng vì lỗi vòng lặp — CHƯA đóng được vị thế")
            await self._say("🚨🚨 Runner dừng vì lỗi lặp lại, VỊ THẾ CÓ THỂ CÒN MỞ — XỬ LÝ TAY NGAY")
            return 1
        return None

    async def run(self) -> int:
        code = await self.startup()
        if code is not None:
            return code
        feed_task = asyncio.create_task(self.feed.run())
        errors = 0
        try:
            while True:
                if feed_task.done() and feed_task.exception() is not None:
                    await self._say(f"🚨 Feed dừng: {feed_task.exception()} — risk sẽ chặn mở vị thế")
                    feed_task = asyncio.create_task(asyncio.sleep(10 ** 9))
                try:
                    code = await self.step()
                    errors = 0
                except Exception as exc:
                    # lỗi lạ KHÔNG được làm chết tiến trình (stop do bot giữ sẽ mất) — golive #3
                    errors += 1
                    log.exception("runner: lỗi vòng lặp")
                    await self._say(f"🚨 Lỗi vòng lặp ({errors}/{self.config.max_consecutive_errors}): "
                                    f"{type(exc).__name__}: {exc}")
                    code = await self._on_loop_errors(errors)
                if code is not None:
                    return code
                await asyncio.sleep(self.config.tick_s)
        finally:
            await self.feed.stop()
            if not feed_task.done():
                feed_task.cancel()
            await asyncio.gather(feed_task, return_exceptions=True)
            for b in self.brokers.values():
                try:
                    await b.close()
                except Exception:
                    pass
            self._save()


# ----------------------------------------------------------------------- CLI ----

def main(argv=None) -> int:
    import importlib

    from brokers.base import LiveTradingNotAllowed, assert_live_trading_allowed
    from core.settings import MissingSetting, REPO_ROOT, load_settings
    from live.instance_lock import InstanceLock, MachineNotAllowed, check_allowed_machine

    ap = argparse.ArgumentParser(description="Bot VN30F — runner paper/live")
    ap.add_argument("--live", action="store_true", help="giao dịch thật (cần thêm TRADING_MODE=live)")
    ap.add_argument("--strategy", default="strategy.baseline_orb:OpeningRangeBreakout")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--krx", nargs="+", help="mã KRX đang niêm yết (đã kiểm tay)")
    g.add_argument("--assume-computed-contract", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
    if args.live or mode == "live":
        try:
            assert_live_trading_allowed(live_flag=args.live)
        except LiveTradingNotAllowed as exc:
            log.error("Từ chối: %s", exc)
            return 2
        log.error("Chưa có adapter broker thật (P4.1 DNSE / P4.2 SSI) — không chạy live.")
        return 2

    settings = load_settings()
    try:
        machine = check_allowed_machine(settings.get("live.allowed_machine"))
    except MachineNotAllowed as exc:
        log.error("%s", exc)
        return 2
    key, secret = os.environ.get("DNSE_API_KEY"), os.environ.get("DNSE_API_SECRET")
    if not key or not secret:
        log.error("Thiếu DNSE_API_KEY / DNSE_API_SECRET trong .env (feed dữ liệu)")
        return 2
    try:
        from core import contracts
        from feeds.data_feed_dnse import DnseFeed
        from monitoring import telegram

        mclock = MarketClock.from_settings(settings)
        risk = RiskManager.from_settings(settings, mclock)
        mod, _, cls = args.strategy.partition(":")
        strat_cls = getattr(importlib.import_module(mod), cls)
        strategy = strat_cls.from_settings(settings) if hasattr(strat_cls, "from_settings") else strat_cls()
        broker = PaperBroker.from_settings(settings, mclock.clock, name=risk.limits.primary_broker,
                                           session=mclock)
    except MissingSetting as exc:
        log.error("%s — chạy `python -m core.settings`", exc)
        return 2

    async def lister():
        if args.krx:
            return [s.strip().upper() for s in args.krx]
        name = contracts.trading_contract(mclock.now().date(), mclock.calendar, mclock.roll_days_before_expiry)
        return [contracts.krx_symbol(*contracts.contract_ym(name))]

    feed = DnseFeed.from_settings(settings, key, secret, lister, mclock)
    tg_token, tg_chat = telegram.credentials("trading")
    offset: dict = {"v": None}

    async def notify(text: str):
        if tg_token and tg_chat:
            await asyncio.to_thread(telegram.notify, text, "trading", parse_mode="")

    async def fetch_commands() -> list[str]:
        if not (tg_token and tg_chat):
            return []
        updates = await asyncio.to_thread(telegram.fetch_updates, offset["v"], "trading")
        cmds, nxt = telegram.parse_commands(updates, tg_chat)
        if nxt is not None:
            offset["v"] = nxt
        return cmds

    lock = InstanceLock(settings.path("paths.state_dir") / "bot.lock", mode=mode,
                        stale_after_s=float(settings.get("live.lock_stale_after_seconds", 120)))
    try:
        lock.acquire()
    except LockHeld as exc:
        log.error("Đã có phiên khác: %s", exc)
        return 3
    try:
        state_dir = settings.path("paths.state_dir")
        runner = Runner(settings=settings, strategy=strategy, feed=feed, broker=broker,
                        market_clock=mclock, risk=risk, state=AccountState(), lock=lock,
                        notify=notify, fetch_commands=fetch_commands,
                        state_path=state_dir / "daily_state.json",
                        report_dir=settings.path("paths.data_dir") / "reports",
                        config=RunnerConfig(heartbeat_s=float(settings.get("monitoring.heartbeat_seconds", 30))),
                        mode=f"{mode}@{machine}")
        return asyncio.run(runner.run())
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
