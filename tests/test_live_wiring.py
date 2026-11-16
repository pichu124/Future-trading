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


@pytest.mark.parametrize("primary,backup", [("ssi", "ssi"), ("dnse", "dnse"), ("tcbs", "tcbs")])
def test_primary_and_backup_must_differ(live_settings, live_env, cal, primary, backup):
    live_settings._data["brokers"].update(primary=primary, backup=backup, tcbs_enabled=True)
    with pytest.raises(ValueError, match="khác nhau"):
        build(live_settings, live_env, cal)


def test_unknown_broker_refused(live_settings, live_env, cal):
    live_settings._data["brokers"]["backup"] = "vps"
    with pytest.raises(ValueError, match="dnse, ssi, tcbs"):
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
    monkeypatch.setattr("live.runner.setup_logging", lambda *a, **k: None)   # không ghi vào logs/ của repo
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


@pytest.mark.parametrize("key", ["data_dir", "state_dir", "log_dir"])
def test_main_refuses_paths_inside_sync_folder(monkeypatch, key):
    """CLAUDE.md mục 3b: data/ state/ (và logs/) của bot KHÔNG được nằm trong thư mục Dropbox."""
    from live import runner
    s = load_settings()
    d = copy.deepcopy(s._data)
    d["paths"][key] = "C:/Users/me/Dropbox/bot/" + key
    monkeypatch.setattr("core.settings.load_settings", lambda *a, **k: Settings(d))
    monkeypatch.setenv("TRADING_MODE", "paper")
    called = []
    monkeypatch.setattr("live.instance_lock.check_allowed_machine", lambda allowed: called.append(1) or "h")
    assert runner.main(["--assume-computed-contract"]) == 2
    assert called == []                                   # từ chối trước cả bước kiểm máy / khoá


# --------------------------------------------------------------------------- đối chiếu hợp đồng (§4) ----

def test_live_contract_list_comes_from_dnse_and_is_preflighted(live_settings, live_env, cal):
    calls = []

    async def dnse_transport(method, url, params, json_body, headers, timeout):
        calls.append(url)
        import json as _j
        return 200, {}, _j.dumps({"data": [{"symbol": "41I1GB000", "symbolType": "VN30F1M"},
                                            {"symbol": "41I1GC000", "symbolType": "VN30F2M"}]})
    setup = build_live(live_settings, live_env, live_flag=True, market_clock=mclock_for(live_settings, cal),
                       contract_lister=None, dnse_transport=dnse_transport)
    assert setup.preflight
    asyncio.run(setup.preflight[0]())                                  # 09/11/2026: F1M = 41I1GB000 ✔
    assert any(u.endswith("/instruments") for u in calls)
    assert asyncio.run(setup.contract_lister()) == ["41I1GB000", "41I1GC000"]


def test_live_preflight_fails_on_mismatch(live_settings, live_env, cal):
    from brokers.dnse import ContractMismatch

    async def dnse_transport(method, url, params, json_body, headers, timeout):
        import json as _j
        return 200, {}, _j.dumps({"data": [{"symbol": "41I1GC000", "symbolType": "VN30F1M"}]})
    setup = build_live(live_settings, live_env, live_flag=True, market_clock=mclock_for(live_settings, cal),
                       contract_lister=None, dnse_transport=dnse_transport)
    with pytest.raises(ContractMismatch):
        asyncio.run(setup.preflight[0]())


def test_runner_preflight_failure_exits_2_before_reconcile(session_cfg, cal, tmp_path):
    from brokers.dnse import ContractMismatch

    async def check():
        raise ContractMismatch("DNSE báo VN30F1M = 41I1GC000, bot tính 41I1GB000")
    r = AuthRig(session_cfg, cal, tmp_path, [])
    r.primary.trading_ready = r.backup.trading_ready = True
    r.runner.preflight = [check]
    assert asyncio.run(r.runner.startup()) == 2
    assert "positions" not in r.primary.calls
    assert any("VN30F1M" in n and n.startswith("🚨") for n in r.notes)


def test_runner_preflight_ok_continues(session_cfg, cal, tmp_path):
    ran = []

    async def check():
        ran.append(1)
    r = AuthRig(session_cfg, cal, tmp_path, [])
    r.primary.trading_ready = r.backup.trading_ready = True
    r.runner.preflight = [check]
    assert asyncio.run(r.runner.startup()) is None and ran == [1] and "positions" in r.primary.calls


def test_live_preflight_checks_traded_contract_after_roll(live_settings, live_env, cal):
    """Ngày đáo hạn 19/11/2026 (roll_days_before_expiry=1 ⇒ bot đã chuyển sang F2M = 41I1GC000), còn DNSE
    vẫn báo F1M = 41I1GB000 và không trả F2M ⇒ đối chiếu nhãn qua, nhưng hợp đồng ĐANG giao dịch không có ⇒ dừng."""
    from core import contracts
    from feeds.data_feed_dnse import ContractNotListed
    day = datetime(2026, 11, 19, 10, 0)
    assert contracts.trading_contract(day.date(), cal, 1) == "VN30F2612"

    async def dnse_transport(method, url, params, json_body, headers, timeout):
        import json as _j
        return 200, {}, _j.dumps({"data": [{"symbol": "41I1GB000", "symbolType": "VN30F1M"}]})
    mclock = MarketClock(SessionSchedule.from_mapping(live_settings.get("session")), cal, FakeClock(day))
    setup = build_live(live_settings, live_env, live_flag=True, market_clock=mclock, contract_lister=None,
                       dnse_transport=dnse_transport)
    with pytest.raises(ContractNotListed):
        asyncio.run(setup.preflight[0]())



# --------------------------------------------------------------------------- TCBS làm chính / dự phòng ----

@pytest.fixture
def tcbs_env(live_env):
    e = dict(live_env)
    e.update(TCBS_API_KEY="tcbs-key-x", TCBS_CUSTODY_CODE="105C334455", TCBS_DERIVATIVE_ACCOUNT="105C334455A")
    return e


def enable_tcbs(settings):
    settings._data["brokers"]["tcbs_enabled"] = True
    settings._data["brokers"].setdefault("tcbs", {})["api_verified"] = True


def test_dnse_primary_tcbs_backup(live_settings, tcbs_env, cal):
    from brokers.tcbs import TcbsBroker
    enable_tcbs(live_settings)
    live_settings._data["brokers"]["backup"] = "tcbs"
    for k in ("SSI_ACCOUNT", "SSI_PRIVATE_KEY_PATH", "SSI_CLIENT_ID"):     # SSI chỉ còn làm feed: không cần
        tcbs_env.pop(k)
    setup = build(live_settings, tcbs_env, cal)
    assert isinstance(setup.primary, DnseBroker) and isinstance(setup.backup, TcbsBroker)
    assert setup.backup.name == "tcbs" and setup.backup.live_flag
    assert setup.feed.primary.name == "dnse" and setup.feed.backup.name == "ssi"     # vẫn đối chiếu DNSE/SSI
    assert [a.name for a in setup.auth] == ["ssi"]                          # token SSI cho feed: chờ /otp ssi
    assert setup.feed.backup._tokens.__self__ is setup.auth[0]
    assert len(setup.background) == 1 and setup.background[0].__self__.__class__.__name__ == "TcbsOrderStream"


def test_tcbs_primary_ssi_backup(live_settings, tcbs_env, cal):
    from brokers.tcbs import TcbsBroker
    enable_tcbs(live_settings)
    live_settings._data["brokers"]["primary"] = "tcbs"
    tcbs_env.pop("DNSE_ACCOUNT_NO")                                          # DNSE chỉ làm feed: không cần tiểu khoản
    live_settings._data["brokers"]["dnse"].update(loan_package_id=None, trading_token_ttl_seconds=None,
                                                  api_verified=False)
    setup = build(live_settings, tcbs_env, cal)
    assert isinstance(setup.primary, TcbsBroker) and isinstance(setup.backup, SsiBroker)
    assert setup.auth == []                                                  # token SSI do broker SSI giữ
    assert setup.feed.backup._tokens.__self__ is setup.backup.tokens
    names = sorted(f.__self__.__class__.__name__ for f in setup.background)
    assert names == ["SsiOrderStream", "TcbsOrderStream"]


def test_tcbs_refused_unless_enabled_and_verified(live_settings, tcbs_env, cal):
    live_settings._data["brokers"]["backup"] = "tcbs"
    live_settings._data["brokers"]["tcbs_enabled"] = False
    with pytest.raises(LiveTradingNotAllowed, match="tcbs_enabled"):
        build(live_settings, tcbs_env, cal)
    live_settings._data["brokers"]["tcbs_enabled"] = True
    live_settings._data["brokers"].setdefault("tcbs", {})["api_verified"] = False
    with pytest.raises(LiveTradingNotAllowed, match="api_verified"):
        build(live_settings, tcbs_env, cal)


def test_tcbs_missing_env_refused(live_settings, tcbs_env, cal):
    enable_tcbs(live_settings)
    live_settings._data["brokers"]["backup"] = "tcbs"
    del tcbs_env["TCBS_DERIVATIVE_ACCOUNT"]
    with pytest.raises(MissingSetting, match="TCBS_DERIVATIVE_ACCOUNT"):
        build(live_settings, tcbs_env, cal)


def test_feed_still_needs_dnse_and_ssi_keys_when_tcbs(live_settings, tcbs_env, cal):
    enable_tcbs(live_settings)
    live_settings._data["brokers"]["backup"] = "tcbs"
    del tcbs_env["SSI_API_SECRET"]
    with pytest.raises(MissingSetting, match="SSI_API_SECRET"):
        build(live_settings, tcbs_env, cal)


def test_runner_waits_otp_for_tcbs_and_feed_token(session_cfg, cal, tmp_path):
    """dnse chính + tcbs dự phòng: chờ /otp dnse, /otp tcbs VÀ /otp ssi (feed) trước khi giao dịch."""
    from tests.test_paper_composite import AuthTarget
    r = AuthRig(session_cfg, cal, tmp_path, [["otp dnse 1234"], ["otp tcbs 1234"], [], ["otp ssi 1234"]])
    r.backup.name = "tcbs"
    r.runner.brokers = {"dnse": r.primary, "tcbs": r.backup}
    feed_tok = AuthTarget("ssi")
    r.runner.auth = {"ssi": feed_tok}
    assert asyncio.run(r.runner.startup()) is None
    assert feed_tok.trading_ready and r.primary.trading_ready and r.backup.trading_ready


def test_main_live_passes_auth_preflight_background_to_runner(monkeypatch, live_settings, tmp_path):
    from live import runner as R
    d = live_settings._data
    d["risk"].update(max_daily_loss_points=20, max_trades_per_day=3, max_consecutive_losses=2)
    d["paths"].update(data_dir=str(tmp_path / "data"), state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    monkeypatch.setattr("core.settings.load_settings", lambda *a, **k: live_settings)
    monkeypatch.setattr("live.instance_lock.check_allowed_machine", lambda allowed: "host")
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("TELEGRAM_TRADING_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_TRADING_CHAT_ID", "1")
    sentinel_auth, sentinel_pre, sentinel_bg = [object()], [lambda: None], [lambda: None]

    class Setup:
        primary, backup, feed = AuthBroker("dnse"), AuthBroker("tcbs"), FakeFeed()
        background, preflight, auth = sentinel_bg, sentinel_pre, sentinel_auth
    monkeypatch.setattr("live.wiring.build_live", lambda *a, **k: Setup())
    captured = {}

    class FakeRunner:
        def __init__(self, **kw):
            captured.update(kw)

        def queue_alert(self, t):
            pass

        async def run(self):
            return 0
    monkeypatch.setattr(R, "Runner", FakeRunner)
    assert R.main(["--live", "--assume-computed-contract"]) == 0
    assert captured["auth"] is sentinel_auth and captured["preflight"] is sentinel_pre
    assert captured["background"] is sentinel_bg and captured["backup_broker"].name == "tcbs"
