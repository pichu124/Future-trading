"""Test cho live/runner.py (PROMPTS P5.1) — viết TRƯỚC khi implement.

Runner chạy bằng `step()` (một vòng) để test tất định với đồng hồ giả; `run()` = startup + lặp step.
"""
import asyncio
from datetime import date, datetime, timedelta

import pytest

from brokers.paper import PaperBroker, PaperConfig
from core.clock import FakeClock, MarketClock, SessionSchedule
from core.models import AccountState, Order, OrderType, Side, Signal, SignalAction, Tick
from feeds.base import DataFeed
from live.instance_lock import LockHeld
from live.runner import Runner, RunnerConfig
from risk.risk_manager import RejectCode, RiskManager
from tests.test_risk_manager import make_settings

SYM = "VN30F2611"
T0 = datetime(2026, 11, 9, 10, 0, 0)


class FakeFeed(DataFeed):
    name = "fake"

    def __init__(self):
        super().__init__()
        self.stopped = False

    def emit(self, tick):
        for h in self._handlers:
            h(tick)

    async def run(self):
        while not self.stopped:
            await asyncio.sleep(0.001)

    async def stop(self):
        self.stopped = True

    def is_stale(self, now=None):
        return False

    def stats(self):
        return {}


class Plan:
    """Strategy kịch bản theo giờ mở bar."""

    def __init__(self, plan):
        self.plan = plan

    def on_bar(self, bar, ctx):
        p = self.plan.get(bar.start.strftime("%H:%M"))
        if not p or ctx.position_qty != 0:
            return []
        return [Signal(SignalAction.ENTER_LONG, bar.start + timedelta(minutes=1), bar.symbol,
                       stop_loss=p[0], take_profit=p[1] if len(p) > 1 else None, reason="plan")]


class FakeLock:
    def __init__(self):
        self.beats = 0
        self.lost = False

    def heartbeat(self):
        if self.lost:
            raise LockHeld("bị chiếm")
        self.beats += 1


class Rig:
    def __init__(self, session_cfg, cal, tmp_path, start=T0, plan=None, commands=None, **risk_over):
        self.clock = FakeClock(start)
        self.mclock = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, self.clock)
        self.settings = make_settings(session_cfg, **risk_over)
        self.risk = RiskManager.from_settings(self.settings, self.mclock, kill_file=tmp_path / "KILL")
        self.kill_file = tmp_path / "KILL"
        self.state = AccountState()
        cfg = PaperConfig(tick_size=0.1, slippage_ticks=0, fee_vnd_per_contract=10_000, latency_ms=0)
        self.broker = PaperBroker(cfg, self.clock, name="dnse", session=self.mclock)
        self.feed = FakeFeed()
        self.notes: list[str] = []
        self.lock = FakeLock()
        self.cmd_queue = list(commands or [])

        async def fetch_commands():
            out, self.cmd_queue = self.cmd_queue, []
            return out

        self.runner = Runner(settings=self.settings, strategy=Plan(plan or {}), feed=self.feed,
                             broker=self.broker, market_clock=self.mclock, risk=self.risk,
                             state=self.state, lock=self.lock, notify=self.notes.append,
                             fetch_commands=fetch_commands, state_path=tmp_path / "state.json",
                             report_dir=tmp_path / "reports",
                             config=RunnerConfig(heartbeat_s=30, command_poll_s=0, kill_flat_timeout_s=60))

    def run(self, coro):
        return asyncio.run(coro)

    def start(self):
        return self.run(self.runner.startup())

    def step(self):
        return self.run(self.runner.step())

    def at(self, ts):
        self.clock.set(ts)

    def trade(self, px=1350.0, bid=None, ask=None, qty=1):
        self.feed.emit(Tick(SYM, self.clock.now(), last=px, last_qty=qty,
                            bid=px - 0.1 if bid is None else bid, ask=px + 0.1 if ask is None else ask))

    def open_position(self):
        """Theo kịch bản: bar 10:00 đóng ⇒ vào Long, khớp ở tick kế tiếp."""
        self.at(T0 + timedelta(seconds=5))
        self.trade(1350.0)
        self.step()
        self.at(T0 + timedelta(seconds=60, milliseconds=600))
        self.step()                                          # bar 10:00 đóng ⇒ tín hiệu ⇒ lệnh
        self.at(T0 + timedelta(seconds=62))
        self.trade(1350.4)
        self.step()                                          # khớp ⇒ đặt stop
        assert self.state.net_qty(SYM) == 1


@pytest.fixture
def rig(session_cfg, cal, tmp_path):
    return Rig(session_cfg, cal, tmp_path, plan={"10:00": (1340.0,)})


# ------------------------------------------------------------------ khởi động ----

def test_not_trading_day_skips(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path, start=datetime(2026, 11, 7, 10, 0))     # thứ Bảy
    assert r.start() == 0


def test_startup_reconciles_broker_positions(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path)
    o = Order("PRE1", SYM, Side.BUY, 1, OrderType.MARKET, T0, broker="dnse")
    r.run(r.broker.place(o))                                 # vị thế có sẵn trên broker (vd sau crash)
    r.at(T0 + timedelta(seconds=1))
    r.broker.on_tick(Tick(SYM, r.clock.now(), last=1350, bid=1349.9, ask=1350.1))
    r.state.positions.clear()                                # tiến trình mới: state trống, broker có vị thế
    r.state.working_orders.clear()
    assert r.start() is None
    assert r.state.net_qty(SYM) == 1
    assert any("vị thế" in n for n in r.notes)


# ------------------------------------------------------------------ đường ống ----

def test_tick_to_bar_to_order_to_fill_to_stop(rig):
    assert rig.start() is None
    rig.open_position()
    stops = [o for o in rig.state.working_orders.values() if o.tag == "stop"]
    assert len(stops) == 1 and stops[0].price == 1340.0
    assert rig.state.broker_last_seen["dnse"] == rig.clock.now()


def test_unhealthy_feed_blocks_entries(rig):
    rig.feed.healthy = False
    rig.start()
    rig.at(T0 + timedelta(seconds=5))
    rig.trade()
    rig.step()
    rig.at(T0 + timedelta(seconds=61))
    rig.step()
    assert rig.state.feed_healthy is False
    assert [r.code for r in rig.runner.execution.rejections] == [RejectCode.FEED_UNHEALTHY]


def test_failed_ping_does_not_refresh_last_seen(rig):
    rig.start()
    rig.step()
    seen = rig.state.broker_last_seen["dnse"]

    async def down():
        return False
    rig.broker.ping = down
    rig.at(T0 + timedelta(seconds=5))
    rig.step()
    assert rig.state.broker_last_seen["dnse"] == seen


# ------------------------------------------------------------------ kill-switch ----

def test_kill_file_flattens_then_exits(rig):
    rig.start()
    rig.open_position()
    rig.kill_file.write_text("")
    rig.at(T0 + timedelta(seconds=63))
    assert rig.step() is None                                # lệnh đóng đã gửi, chờ khớp
    rig.at(T0 + timedelta(seconds=64))
    rig.trade(1351.0)
    assert rig.step() == 0
    assert rig.state.net_qty(SYM) == 0
    assert any("Báo cáo" in n for n in rig.notes)


def test_telegram_kill(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path, plan={"10:00": (1340.0,)})
    r.start()
    r.open_position()
    r.cmd_queue = ["kill"]
    r.at(T0 + timedelta(seconds=63))
    r.step()
    assert r.state.kill_reason and "telegram" in r.state.kill_reason
    r.at(T0 + timedelta(seconds=64))
    r.trade(1351.0)
    assert r.step() == 0


def test_kill_not_flat_after_timeout_exits_with_error(rig):
    rig.start()
    rig.open_position()
    rig.kill_file.write_text("")
    rig.at(T0 + timedelta(seconds=63))
    rig.step()
    rig.at(T0 + timedelta(seconds=63 + 61))                  # không tick nào ⇒ lệnh đóng không khớp
    assert rig.step() == 1
    assert any("CÒN VỊ THẾ" in n for n in rig.notes)


def test_status_and_pnl_commands(rig):
    rig.start()
    rig.open_position()
    rig.cmd_queue = ["status", "pnl", "help", "lạ"]
    rig.at(T0 + timedelta(seconds=63))
    rig.step()
    text = "\n".join(rig.notes)
    assert "Trạng thái" in text and "VN30F2611 +1" in text
    assert "P&L" in text and "/kill" in text


# ------------------------------------------------------------------ khoá phiên ----

def test_lock_lost_halts_immediately(rig):
    rig.start()
    rig.open_position()
    rig.lock.lost = True
    rig.at(T0 + timedelta(seconds=95))                       # quá heartbeat_s ⇒ heartbeat
    placed_before = len(rig.runner.execution.orders)
    assert rig.step() == 3
    assert any("khoá" in n.lower() for n in rig.notes)
    assert len(rig.runner.execution.orders) == placed_before  # không gửi thêm lệnh nào
    rig.lock.lost = False                                    # kể cả khi khoá "có vẻ" ổn trở lại
    rig.kill_file.write_text("")                             # và có lý do để gửi lệnh đóng
    rig.at(T0 + timedelta(seconds=200))
    rig.trade(1351.0)
    assert rig.step() == 3
    assert len(rig.runner.execution.orders) == placed_before


def test_heartbeat_saves_state_and_restart_restores(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path, plan={"10:00": (1340.0,)})
    r.start()
    r.open_position()
    r.at(T0 + timedelta(seconds=95))
    r.step()
    assert r.lock.beats >= 1 and (tmp_path / "state.json").exists()
    r2 = Rig(session_cfg, cal, tmp_path, start=T0 + timedelta(seconds=100))
    r2.start()
    assert r2.state.trades_today == 1


# ------------------------------------------------------------------ cuối ngày ----

def test_end_of_day_flat_reports_and_exits(rig, tmp_path):
    rig.start()
    rig.at(datetime(2026, 11, 9, 14, 45))
    assert rig.step() == 0
    report = (tmp_path / "reports" / "daily_2026-11-09.txt").read_text(encoding="utf-8")
    assert "P&L sau phí" in report and "Risk từ chối" in report
    assert any("Báo cáo" in n for n in rig.notes)


def test_end_of_day_with_position_exits_error(rig):
    rig.start()
    rig.open_position()
    rig.at(datetime(2026, 11, 9, 14, 45))
    assert rig.step() == 1
    assert any("CÒN VỊ THẾ" in n for n in rig.notes)


def test_report_lists_trades_after_fees(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path, plan={"10:00": (1340.0, 1351.0)})
    r.start()
    r.open_position()                                        # mua 1350.5 (ask)
    r.at(T0 + timedelta(seconds=80))
    r.trade(1352.0)                                          # bid 1351.9 > TP 1351 ⇒ chốt lời 1351
    r.step()
    assert r.state.net_qty(SYM) == 0
    r.at(datetime(2026, 11, 9, 14, 45))
    assert r.step() == 0
    rep = (tmp_path / "reports" / "daily_2026-11-09.txt").read_text(encoding="utf-8")
    assert "take_profit" in rep and "30,000" in rep         # (1351 − 1350.5) × 100.000 − 2 × 10.000


def test_run_loop_end_to_end(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path, start=datetime(2026, 11, 9, 14, 46))
    r.runner.config = RunnerConfig(tick_s=0.001, heartbeat_s=30, command_poll_s=0)
    assert r.run(r.runner.run()) == 0
    assert r.feed.stopped


# ------------------------------------------------------------------ main() ----

def test_main_refuses_live_without_adapter(monkeypatch):
    from live.runner import main
    monkeypatch.setenv("TRADING_MODE", "live")
    assert main(["--live", "--assume-computed-contract"]) == 2


def test_main_refuses_live_flag_in_paper_mode(monkeypatch):
    from live.runner import main
    monkeypatch.setenv("TRADING_MODE", "paper")
    assert main(["--live", "--assume-computed-contract"]) == 2


def test_main_paper_refuses_without_allowed_machine(monkeypatch):
    from live.runner import main
    monkeypatch.setenv("TRADING_MODE", "paper")
    assert main(["--assume-computed-contract"]) == 2        # máy chạy test ≠ live.allowed_machine


def test_feed_silence_with_position_flattens(rig):
    """golive #9: đang Long mà feed im > 30s trong phiên liên tục ⇒ đóng bằng MTL."""
    rig.start()
    rig.open_position()                                      # tick cuối 10:01:02
    rig.at(T0 + timedelta(seconds=62 + 30))
    rig.step()
    assert not [o for o in rig.state.working_orders.values() if o.tag == "flatten"]
    rig.at(T0 + timedelta(seconds=62 + 31))
    rig.step()
    assert [o.tag for o in rig.state.working_orders.values()] == ["flatten"]
    rig.at(T0 + timedelta(seconds=62 + 40))
    rig.trade(1349.0)                                        # dữ liệu trở lại ⇒ lệnh đóng khớp
    rig.step()
    assert rig.state.net_qty(SYM) == 0


# ------------------------------------------------------------------ /otp + on_tick bất đồng bộ ----

def test_otp_command_goes_to_broker_and_code_is_never_echoed(session_cfg, cal, tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    r = Rig(session_cfg, cal, tmp_path, commands=["otp 834521"])
    got = []

    async def submit_otp(code):
        got.append(code)
        return False
    r.broker.submit_otp = submit_otp
    r.start()
    r.step()
    assert got == ["834521"]
    assert any("OTP không được chấp nhận" in n for n in r.notes)
    assert not any("834521" in n for n in r.notes) and "834521" not in caplog.text


def test_otp_command_without_supporting_broker(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path, commands=["otp 834521", "otp"])
    r.start()
    r.step()
    assert any("Không broker nào cần OTP" in n for n in r.notes)
    assert not any("834521" in n for n in r.notes)


def test_async_on_tick_of_real_broker_is_awaited(session_cfg, cal, tmp_path):
    """Adapter DNSE giữ stop ở bot ⇒ runner phải chuyển tick cho MỌI broker có on_tick (kể cả async)."""
    r = Rig(session_cfg, cal, tmp_path)
    seen = []
    orig = r.broker.on_tick

    async def on_tick(t):
        seen.append(t.last)
        orig(t)
    r.broker.on_tick = on_tick
    r.start()
    r.at(T0 + timedelta(seconds=5))
    r.trade(1350.0)
    r.step()
    assert seen == [1350.0]


class _OtpBroker:
    def __init__(self, name, awaiting):
        self.name, self.awaiting_otp, self.got = name, awaiting, []

    async def submit_otp(self, code):
        self.got.append(code)
        return True


def _two_otp_brokers(session_cfg, cal, tmp_path, dnse_wait, ssi_wait):
    r = Rig(session_cfg, cal, tmp_path)
    d, s = _OtpBroker("dnse", dnse_wait), _OtpBroker("ssi", ssi_wait)
    r.runner.brokers = {"dnse": d, "ssi": s}
    return r, d, s


def test_otp_routed_by_broker_name(session_cfg, cal, tmp_path):
    r, d, s = _two_otp_brokers(session_cfg, cal, tmp_path, True, True)
    r.run(r.runner._otp("ssi 445566"))
    assert s.got == ["445566"] and d.got == []


def test_otp_without_name_goes_to_the_only_waiting_broker(session_cfg, cal, tmp_path):
    r, d, s = _two_otp_brokers(session_cfg, cal, tmp_path, False, True)
    r.run(r.runner._otp("445566"))
    assert s.got == ["445566"] and d.got == []


def test_otp_without_name_ambiguous_is_refused(session_cfg, cal, tmp_path):
    """Hai broker cùng chờ ⇒ mỗi bên một mã: không đoán, hỏi lại."""
    r, d, s = _two_otp_brokers(session_cfg, cal, tmp_path, True, True)
    r.run(r.runner._otp("445566"))
    assert d.got == [] and s.got == []
    assert any("Ghi rõ broker" in n for n in r.notes) and not any("445566" in n for n in r.notes)
    r2, d2, s2 = _two_otp_brokers(session_cfg, cal, tmp_path, False, False)
    r2.run(r2.runner._otp("445566"))
    assert d2.got == [] and s2.got == []


def test_kill_at_lunch_waits_for_pm_open_instead_of_timing_out(rig):
    """/kill lúc nghỉ trưa: không đóng được tới 13:00. Thoát sau 60s (kill_flat_timeout) ⇒ mất stop do BOT GIỮ
    (DNSE không có lệnh stop) ⇒ vị thế trần. Bộ đếm chỉ chạy trong phiên liên tục; báo Telegram giờ sẽ đóng."""
    rig.start()
    rig.open_position()
    lunch = datetime(2026, 11, 9, 11, 45)
    rig.at(lunch)
    rig.kill_file.write_text("")
    for m in (0, 5, 30, 74):
        rig.at(lunch + timedelta(minutes=m))
        assert rig.step() is None                                    # không thoát giữa trưa
    assert rig.state.net_qty(SYM) == 1
    assert [o for o in rig.state.working_orders.values() if o.tag == "stop"]
    assert any("13:00" in n for n in rig.notes)
    rig.at(datetime(2026, 11, 9, 13, 0, 1))
    assert rig.step() is None                                        # vào phiên chiều: gửi MTL đóng
    rig.at(datetime(2026, 11, 9, 13, 0, 2))
    rig.trade(1351.0)
    assert rig.step() == 0 and rig.state.net_qty(SYM) == 0
