"""Test cho phần nối LIVE (`live/wiring.py` + bước chờ OTP/tác vụ nền của runner) — viết TRƯỚC.

Live = DNSE chính + SSI dự phòng (đối ứng khẩn cấp) + feed tổng hợp DNSE/SSI (đối chiếu, mục 3) +
stream lệnh SSI. Mọi khoá an toàn của từng adapter vẫn giữ nguyên: thiếu một giá trị / chưa
`api_verified` ⇒ KHÔNG dựng được (runner thoát 2), không bao giờ "chạy tạm" một nửa.
"""
import asyncio
import copy
from datetime import datetime, timedelta

import pytest

from brokers.base import BrokerAdapter, LiveTradingNotAllowed
from brokers.dnse import DnseBroker
from brokers.ssi import SsiBroker
from core.clock import FakeClock, MarketClock, SessionSchedule
from core.models import AccountState
from core.settings import MissingSetting, Settings, load_settings
from feeds.composite import CompositeFeed
from live.runner import Runner, RunnerConfig
from live.wiring import LiveSetup, build_live
from risk.risk_manager import RiskManager
from tests.test_broker_ssi import PRIVATE_KEY
from tests.test_risk_manager import make_settings
from tests.test_runner import FakeFeed, Plan

T0 = datetime(2026, 11, 9, 9, 30, 0)


# --------------------------------------------------------------------------- build_live ----

@pytest.fixture
def live_settings():
    s = load_settings()
    d = copy.deepcopy(s._data)
    d["fees"] = {"broker_fee_vnd_per_contract": 2700, "exchange_fee_vnd_per_contract": 2700,
                 "clearing_fee_vnd_per_contract": 2550, "tax_vnd_per_contract": 0}
    d["brokers"]["dnse"].update(loan_package_id=1036, trading_token_ttl_seconds=28800, api_verified=True)
    d["brokers"]["ssi"].update(order_symbol_format="krx", market_order_type="MTL", api_verified=True)
    return Settings(d)


@pytest.fixture
def live_env(tmp_path):
    key = tmp_path / "ssi_key.txt"
    key.write_text(PRIVATE_KEY, encoding="utf-8")
    return {"TRADING_MODE": "live", "DNSE_API_KEY": "k", "DNSE_API_SECRET": "s", "DNSE_ACCOUNT_NO": "0001234567",
            "SSI_API_KEY": "sk", "SSI_API_SECRET": "ss", "SSI_CLIENT_ID": "cid", "SSI_ACCOUNT": "0001238",
            "SSI_PRIVATE_KEY_PATH": str(key)}


def mclock_for(settings, cal):
    return MarketClock.from_settings(settings, clock=FakeClock(T0)) if cal is None else \
        MarketClock(SessionSchedule.from_mapping(settings.get("session")), cal, FakeClock(T0))


async def _lister():
    return ["41I1GB000"]


def build(settings, env, cal, **kw):
    return build_live(settings, env, live_flag=True, market_clock=mclock_for(settings, cal),
                      contract_lister=_lister, **kw)


def test_builds_primary_backup_composite_and_stream(live_settings, live_env, cal):
    setup = build(live_settings, live_env, cal)
    assert isinstance(setup, LiveSetup)
    assert isinstance(setup.primary, DnseBroker) and setup.primary.name == "dnse" and setup.primary.live_flag
    assert isinstance(setup.backup, SsiBroker) and setup.backup.name == "ssi" and setup.backup.live_flag
    assert isinstance(setup.feed, CompositeFeed)
    assert setup.feed.primary.name == "dnse" and setup.feed.backup.name == "ssi"
    # feed SSI và broker SSI dùng CHUNG một token manager (một phiên mỗi apiKey)
    assert setup.feed.backup._tokens.__self__ is setup.backup.tokens
    assert len(setup.background) == 1
    assert setup.primary.config.api_verified and setup.backup.config.api_verified


@pytest.mark.parametrize("broker,field", [("dnse", "api_verified"), ("ssi", "api_verified")])
def test_unverified_api_refused(live_settings, live_env, cal, broker, field):
    live_settings._data["brokers"][broker][field] = False
    with pytest.raises(LiveTradingNotAllowed, match="api_verified"):
        build(live_settings, live_env, cal)


@pytest.mark.parametrize("path", [("brokers", "dnse", "loan_package_id"), ("brokers", "ssi", "market_order_type"),
                                  ("fees", "tax_vnd_per_contract")])
def test_null_values_refused(live_settings, live_env, cal, path):
    node = live_settings._data
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = None
    with pytest.raises(MissingSetting):
        build(live_settings, live_env, cal)


@pytest.mark.parametrize("var", ["DNSE_API_KEY", "DNSE_ACCOUNT_NO", "SSI_API_SECRET", "SSI_ACCOUNT",
                                 "SSI_PRIVATE_KEY_PATH"])
def test_missing_env_refused(live_settings, live_env, cal, var):
    del live_env[var]
    with pytest.raises((MissingSetting, ValueError)):
        build(live_settings, live_env, cal)


def test_unreadable_private_key_refused(live_settings, live_env, cal, tmp_path):
    live_env["SSI_PRIVATE_KEY_PATH"] = str(tmp_path / "khong_co.txt")
    with pytest.raises(OSError):
        build(live_settings, live_env, cal)


def test_only_dnse_primary_ssi_backup_supported(live_settings, live_env, cal):
    live_settings._data["brokers"]["primary"] = "ssi"
    with pytest.raises(ValueError, match="dnse"):
        build(live_settings, live_env, cal)


def test_live_flag_false_refused(live_settings, live_env, cal):
    with pytest.raises(LiveTradingNotAllowed):
        build_live(live_settings, live_env, live_flag=False, market_clock=mclock_for(live_settings, cal),
                   contract_lister=_lister)


def test_main_refuses_live_without_both_switches(monkeypatch):
    from live import runner
    monkeypatch.setenv("TRADING_MODE", "paper")
    assert runner.main(["--live", "--assume-computed-contract"]) == 2
    monkeypatch.setenv("TRADING_MODE", "live")
    assert runner.main(["--assume-computed-contract"]) == 2


def test_main_live_refused_while_settings_unverified(monkeypatch):
    """settings.yaml trong repo còn null / api_verified=false ⇒ live thoát 2 trước khi kết nối gì."""
    from live import runner
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setattr("live.instance_lock.check_allowed_machine", lambda allowed: "test-host")
    assert runner.main(["--live", "--assume-computed-contract"]) == 2


# --------------------------------------------------------------------------- runner: chờ OTP ----

class AuthBroker(BrokerAdapter):
    def __init__(self, name, code="1234"):
        super().__init__(name)
        self.code, self.trading_ready, self.awaiting_otp, self.calls = code, False, False, []

    async def connect(self):
        self.calls.append("connect")
        self.awaiting_otp = True

    async def submit_otp(self, code):
        self.calls.append("otp")
        if code == self.code:
            self.trading_ready, self.awaiting_otp = True, False
        return self.trading_ready

    async def ping(self):
        return self.trading_ready

    async def positions(self):
        self.calls.append("positions")
        return {}

    async def open_orders(self):
        self.calls.append("open_orders")
        return []

    async def place(self, order):
        raise AssertionError("không được đặt lệnh trong test này")

    async def cancel(self, client_order_id):
        raise AssertionError

    async def modify(self, client_order_id, *, price=None, qty=None):
        raise AssertionError


class AuthRig:
    def __init__(self, session_cfg, cal, tmp_path, script, otp_wait_s=600, background=()):
        self.clock = FakeClock(T0)
        self.mclock = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, self.clock)
        self.settings = make_settings(session_cfg)
        self.risk = RiskManager.from_settings(self.settings, self.mclock, kill_file=tmp_path / "KILL")
        self.primary, self.backup = AuthBroker("dnse"), AuthBroker("ssi")
        self.notes, self.script = [], list(script)

        async def fetch():
            self.clock.set(self.clock.now() + timedelta(seconds=100))
            return self.script.pop(0) if self.script else []
        self.runner = Runner(settings=self.settings, strategy=Plan({}), feed=FakeFeed(), broker=self.primary,
                             backup_broker=self.backup, market_clock=self.mclock, risk=self.risk,
                             state=AccountState(), notify=self.notes.append, fetch_commands=fetch,
                             state_path=tmp_path / "s.json", report_dir=tmp_path / "r",
                             config=RunnerConfig(tick_s=0, command_poll_s=0, otp_wait_s=otp_wait_s),
                             background=background, mode="live")


def test_startup_waits_for_otp_before_reconciling(session_cfg, cal, tmp_path):
    r = AuthRig(session_cfg, cal, tmp_path, [[], ["otp dnse 1234"], ["otp ssi 1234"]])
    assert asyncio.run(r.runner.startup()) is None
    for b in (r.primary, r.backup):
        assert b.calls.index("otp") < b.calls.index("positions")       # đối soát SAU khi có token
        assert b.calls[0] == "connect"
    assert any("OTP" in n for n in r.notes)
    assert "dnse" in r.runner.state.broker_last_seen and "ssi" in r.runner.state.broker_last_seen


def test_otp_wait_timeout_exits_2_without_trading(session_cfg, cal, tmp_path):
    r = AuthRig(session_cfg, cal, tmp_path, [["otp dnse 1234"]], otp_wait_s=250)
    assert asyncio.run(r.runner.startup()) == 2
    assert "positions" not in r.backup.calls
    assert any("ssi" in n and "OTP" in n for n in r.notes)


def test_kill_while_waiting_for_otp(session_cfg, cal, tmp_path):
    r = AuthRig(session_cfg, cal, tmp_path, [["kill"]])
    assert asyncio.run(r.runner.startup()) == 0
    assert "positions" not in r.primary.calls


def test_paper_broker_needs_no_otp(session_cfg, cal, tmp_path):
    r = AuthRig(session_cfg, cal, tmp_path, [])
    r.primary.trading_ready = r.backup.trading_ready = True
    assert asyncio.run(r.runner.startup()) is None
    assert "otp" not in r.primary.calls


# --------------------------------------------------------------------------- runner: tác vụ nền ----

def test_background_tasks_started_and_stopped(session_cfg, cal, tmp_path):
    state = {"started": 0, "cancelled": 0}

    async def stream():
        state["started"] += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] += 1
            raise

    r = AuthRig(session_cfg, cal, tmp_path, [], background=[stream])
    r.primary.trading_ready = r.backup.trading_ready = True

    async def go():
        await r.runner.startup()
        r.runner.start_background()
        await asyncio.sleep(0)
        await r.runner.stop_background()
    asyncio.run(go())
    assert state == {"started": 1, "cancelled": 1}


def test_background_crash_is_alerted_once(session_cfg, cal, tmp_path):
    async def boom():
        raise ValueError("stream hỏng")

    r = AuthRig(session_cfg, cal, tmp_path, [], background=[boom])
    r.primary.trading_ready = r.backup.trading_ready = True

    async def go():
        await r.runner.startup()
        r.runner.start_background()
        await asyncio.sleep(0)
        await r.runner.step()
        await r.runner.step()
        await r.runner.stop_background()
    asyncio.run(go())
    hits = [n for n in r.notes if "stream hỏng" in n]
    assert len(hits) == 1 and hits[0].startswith("🚨")


def test_queued_alerts_are_sent(session_cfg, cal, tmp_path):
    r = AuthRig(session_cfg, cal, tmp_path, [])
    r.primary.trading_ready = r.backup.trading_ready = True

    async def go():
        await r.runner.startup()
        r.runner.queue_alert("⚠️ feed lệch giá")
        await r.runner.step()
    asyncio.run(go())
    assert "⚠️ feed lệch giá" in r.notes


def test_run_stops_background_before_closing_brokers(session_cfg, cal, tmp_path):
    events = []

    async def stream():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("stream_cancelled")
            raise

    r = AuthRig(session_cfg, cal, tmp_path, [["kill"]], background=[stream])
    r.primary.trading_ready = r.backup.trading_ready = True

    async def close():
        events.append("broker_closed")
    r.primary.close = close
    assert asyncio.run(r.runner.run()) == 0
    assert events[:2] == ["stream_cancelled", "broker_closed"]
