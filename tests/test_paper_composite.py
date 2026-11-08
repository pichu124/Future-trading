"""Test cho feed tổng hợp ở chế độ PAPER — viết TRƯỚC.

Paper giữ PaperBroker (khớp mô phỏng) nhưng feed = DNSE + SSI qua `CompositeFeed` như live, để luật
"lệch giá / một feed im lặng ⇒ ngừng mở vị thế" (CLAUDE.md mục 3) được chạy thử trước khi live.
Feed SSI cần token ⇒ OTP: runner chờ `/otp ssi <mã>` cho đối tượng xác thực (không phải broker).
Bật bằng `paper.composite_feed: true` (mặc định false ⇒ chỉ feed DNSE như cũ).
"""
import asyncio
import copy
from datetime import datetime, timedelta

import pytest

from brokers.ssi import SsiHttp, SsiTokenManager
from core.clock import FakeClock, MarketClock, SessionSchedule
from core.models import AccountState
from core.settings import MissingSetting, Settings, load_settings
from feeds.composite import CompositeFeed
from feeds.data_feed_dnse import DnseFeed
from live.runner import Runner, RunnerConfig
from live.wiring import build_paper_feed
from risk.risk_manager import RiskManager
from tests.test_broker_ssi import API_KEY, API_SECRET, OTP, FakeSsi, Wall
from tests.test_live_wiring import AuthBroker
from tests.test_risk_manager import make_settings
from tests.test_runner import FakeFeed, Plan

T0 = datetime(2026, 11, 9, 9, 30)
ENV = {"DNSE_API_KEY": "k", "DNSE_API_SECRET": "s", "SSI_API_KEY": "sk", "SSI_API_SECRET": "ss"}


async def _lister():
    return ["41I1GB000"]


@pytest.fixture
def settings():
    d = copy.deepcopy(load_settings()._data)
    d["paper"]["composite_feed"] = True
    return Settings(d)


def mclock_for(settings, cal):
    return MarketClock(SessionSchedule.from_mapping(settings.get("session")), cal, FakeClock(T0))


# --------------------------------------------------------------------------- dựng feed ----

def test_composite_when_enabled(settings, cal):
    feed, auth = build_paper_feed(settings, ENV, market_clock=mclock_for(settings, cal), contract_lister=_lister)
    assert isinstance(feed, CompositeFeed)
    assert feed.primary.name == "dnse" and feed.backup.name == "ssi"
    assert len(auth) == 1 and isinstance(auth[0], SsiTokenManager) and auth[0].name == "ssi"
    assert feed.backup._tokens.__self__ is auth[0]                  # feed SSI dùng đúng token manager này


def test_dnse_only_when_disabled(settings, cal):
    settings._data["paper"]["composite_feed"] = False
    feed, auth = build_paper_feed(settings, ENV, market_clock=mclock_for(settings, cal), contract_lister=_lister)
    assert isinstance(feed, DnseFeed) and auth == []
    settings._data["paper"].pop("composite_feed")                   # thiếu khoá ⇒ như false
    feed, auth = build_paper_feed(settings, ENV, market_clock=mclock_for(settings, cal), contract_lister=_lister)
    assert isinstance(feed, DnseFeed) and auth == []


@pytest.mark.parametrize("var", ["SSI_API_KEY", "SSI_API_SECRET", "DNSE_API_KEY"])
def test_missing_keys_refused(settings, cal, var):
    env = dict(ENV)
    del env[var]
    with pytest.raises(MissingSetting, match=var):
        build_paper_feed(settings, env, market_clock=mclock_for(settings, cal), contract_lister=_lister)


def test_feed_alerts_forwarded(settings, cal):
    got = []
    feed, _ = build_paper_feed(settings, ENV, market_clock=mclock_for(settings, cal), contract_lister=_lister,
                               on_feed_alert=got.append)
    feed._alert(datetime(2026, 11, 9, 10, 0), feed_alert_kind(), "lệch giá", "ssi")
    assert got and got[0].message == "lệch giá"


def feed_alert_kind():
    from feeds.composite import AlertKind
    return next(iter(AlertKind))


# --------------------------------------------------------------------------- token manager làm đối tượng xác thực ----

def test_token_manager_as_auth_target():
    srv, wall, notes = FakeSsi(), Wall(), []
    srv.now = wall
    m = SsiTokenManager(SsiHttp(transport=srv), API_KEY, API_SECRET, now=wall, notify=notes.append)
    assert m.name == "ssi" and m.trading_ready is False
    asyncio.run(m.connect())
    asyncio.run(m.connect())                                         # không xin OTP lần hai khi đang chờ
    assert [c["path"] for c in srv.calls].count("/api/v3/auth/requestOtp") == 1 and m.awaiting_otp
    assert asyncio.run(m.submit_otp(OTP)) and m.trading_ready


# --------------------------------------------------------------------------- runner chờ OTP của feed ----

class AuthTarget:
    def __init__(self, name, code="1234"):
        self.name, self.code, self.trading_ready, self.awaiting_otp, self.calls = name, code, False, False, []

    async def connect(self):
        self.calls.append("connect")
        self.awaiting_otp = True

    async def submit_otp(self, code):
        self.calls.append(("otp", code))
        if code == self.code:
            self.trading_ready, self.awaiting_otp = True, False
        return self.trading_ready


def rig(session_cfg, cal, tmp_path, script, auth, broker=None):
    clock = FakeClock(T0)
    mclock = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, clock)
    s = make_settings(session_cfg)
    notes, q = [], list(script)

    async def fetch():
        clock.set(clock.now() + timedelta(seconds=100))
        return q.pop(0) if q else []
    if broker is None:
        broker = AuthBroker("dnse")
        broker.trading_ready = True
    r = Runner(settings=s, strategy=Plan({}), feed=FakeFeed(), broker=broker, market_clock=mclock,
               risk=RiskManager.from_settings(s, mclock, kill_file=tmp_path / "KILL"), state=AccountState(),
               notify=notes.append, fetch_commands=fetch, state_path=tmp_path / "s.json",
               report_dir=tmp_path / "r", config=RunnerConfig(tick_s=0, command_poll_s=0, otp_wait_s=250),
               auth=auth, mode="paper")
    return r, notes


def test_runner_waits_for_feed_otp(session_cfg, cal, tmp_path):
    a = AuthTarget("ssi")
    r, notes = rig(session_cfg, cal, tmp_path, [[], ["otp ssi 1234"]], [a])
    assert asyncio.run(r.startup()) is None
    assert a.calls[0] == "connect" and ("otp", "1234") in a.calls
    assert any("ssi" in n and "OTP" in n for n in notes)


def test_runner_feed_otp_without_name_goes_to_only_waiting(session_cfg, cal, tmp_path):
    a = AuthTarget("ssi")
    r, _ = rig(session_cfg, cal, tmp_path, [["otp 1234"]], [a])
    assert asyncio.run(r.startup()) is None and a.trading_ready


def test_runner_feed_otp_timeout(session_cfg, cal, tmp_path):
    a = AuthTarget("ssi")
    r, notes = rig(session_cfg, cal, tmp_path, [], [a])
    assert asyncio.run(r.startup()) == 2


def test_auth_target_with_same_name_as_broker_not_duplicated(session_cfg, cal, tmp_path):
    """Live: broker 'ssi' đã nhận OTP (uỷ cho token manager) ⇒ không gửi mã hai lần."""
    a = AuthTarget("ssi")
    b = AuthBroker("dnse")
    b.trading_ready = True
    r, _ = rig(session_cfg, cal, tmp_path, [], [a], broker=b)
    ssi_broker = AuthBroker("ssi")
    r.brokers["ssi"] = ssi_broker
    asyncio.run(r._otp("ssi 1234"))
    assert ssi_broker.calls.count("otp") == 1 and ("otp", "1234") not in a.calls
