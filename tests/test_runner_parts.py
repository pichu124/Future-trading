"""Test các mảnh của runner (PROMPTS P5.1): BarBuilder, lưu state trong ngày, lệnh Telegram, ping broker.
Viết TRƯỚC khi implement."""
import asyncio
from datetime import date, datetime, timedelta

import pytest

from core.bars import BarBuilder
from core.models import AccountState, Tick
from live.state_store import load_daily_state, save_daily_state
from monitoring.telegram import parse_commands

SYM = "VN30F2611"
T0 = datetime(2026, 11, 9, 10, 0, 0)


def tr(sec, px, qty=1, sym=SYM):
    return Tick(sym, T0 + timedelta(seconds=sec), last=px, last_qty=qty)


# ------------------------------------------------------------------ BarBuilder ----

class TestBarBuilder:
    def test_bar_closes_on_time_even_without_new_tick(self):
        b = BarBuilder(grace_s=0.5)
        assert b.on_tick(tr(5, 1350)) == []
        b.on_tick(tr(40, 1352, 2))
        assert b.on_time(T0 + timedelta(seconds=60, milliseconds=400)) == []      # còn trong grace
        bars = b.on_time(T0 + timedelta(seconds=60, milliseconds=500))
        assert [(x.start, x.open, x.high, x.low, x.close, x.volume) for x in bars] == \
            [(T0, 1350, 1352, 1350, 1352, 3)]
        assert b.on_time(T0 + timedelta(seconds=61)) == []                       # không phát lại

    def test_tick_of_next_minute_closes_previous(self):
        b = BarBuilder(grace_s=0.5)
        b.on_tick(tr(5, 1350))
        bars = b.on_tick(tr(61, 1351))
        assert [x.start for x in bars] == [T0]

    def test_late_tick_for_closed_bar_ignored_and_counted(self):
        b = BarBuilder(grace_s=0.5)
        b.on_tick(tr(5, 1350))
        b.on_time(T0 + timedelta(seconds=61))
        assert b.on_tick(tr(59, 1360)) == []
        assert b.late_ticks == 1

    def test_quote_only_ticks_ignored(self):
        b = BarBuilder()
        b.on_tick(Tick(SYM, T0, bid=1350, ask=1350.2))
        assert b.on_time(T0 + timedelta(seconds=120)) == []

    def test_symbols_independent(self):
        b = BarBuilder(grace_s=0)
        b.on_tick(tr(5, 1350))
        b.on_tick(tr(6, 1400, sym="VN30F2612"))
        assert sorted(x.symbol for x in b.on_time(T0 + timedelta(seconds=60))) == [SYM, "VN30F2612"]


# ------------------------------------------------------------------ state trong ngày ----

class TestStateStore:
    def test_roundtrip_same_day(self, tmp_path):
        s = AccountState(trading_day=date(2026, 11, 9), trades_today=3, consecutive_losses=2,
                         realized_points_today=-12.5, fees_points_today=0.8, locked_for_day=True,
                         kill_reason="telegram", emergency_reason="dnse", approved_order_ids={"A", "B"})
        p = tmp_path / "state.json"
        save_daily_state(s, p)
        fresh = AccountState(trading_day=date(2026, 11, 9))
        assert load_daily_state(p, fresh) is True
        assert (fresh.trades_today, fresh.consecutive_losses, fresh.realized_points_today,
                fresh.fees_points_today, fresh.locked_for_day, fresh.kill_reason,
                fresh.emergency_reason, fresh.approved_order_ids) == \
            (3, 2, -12.5, 0.8, True, "telegram", "dnse", {"A", "B"})

    def test_other_day_only_keeps_manual_locks(self, tmp_path):
        s = AccountState(trading_day=date(2026, 11, 9), trades_today=3, realized_points_today=-30,
                         locked_for_day=True, kill_reason="file KILL", emergency_reason="dnse")
        p = tmp_path / "state.json"
        save_daily_state(s, p)
        fresh = AccountState(trading_day=date(2026, 11, 10))
        assert load_daily_state(p, fresh) is False
        assert fresh.trades_today == 0 and fresh.realized_points_today == 0 and not fresh.locked_for_day
        assert fresh.kill_reason == "file KILL" and fresh.emergency_reason == "dnse"   # khoá tay giữ qua ngày

    def test_missing_file(self, tmp_path):
        assert load_daily_state(tmp_path / "none.json", AccountState(trading_day=date(2026, 11, 9))) is False

    def test_corrupt_file_refuses(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("{không phải json", encoding="utf-8")
        with pytest.raises(ValueError):
            load_daily_state(p, AccountState(trading_day=date(2026, 11, 9)))


# ------------------------------------------------------------------ lệnh Telegram ----

def test_parse_commands_only_from_authorized_chat():
    updates = [
        {"update_id": 10, "message": {"chat": {"id": 111}, "text": "/kill"}},
        {"update_id": 11, "message": {"chat": {"id": 999}, "text": "/kill"}},          # người lạ
        {"update_id": 12, "message": {"chat": {"id": 111}, "text": "/status@my_bot"}},
        {"update_id": 13, "message": {"chat": {"id": 111}, "text": "chào"}},
        {"update_id": 14, "edited_message": {"chat": {"id": 111}, "text": "/pnl"}},
    ]
    cmds, next_offset = parse_commands(updates, authorized_chat_id="111")
    assert cmds == ["kill", "status"]
    assert next_offset == 15


def test_parse_commands_empty():
    assert parse_commands([], "111") == ([], None)


# ------------------------------------------------------------------ ping broker ----

def test_broker_ping_default_true():
    from brokers.paper import PaperBroker, PaperConfig
    from core.clock import FakeClock
    b = PaperBroker(PaperConfig(tick_size=0.1, slippage_ticks=1, fee_vnd_per_contract=0, latency_ms=0),
                    FakeClock(T0))
    assert asyncio.run(b.ping()) is True


def test_parse_commands_keeps_argument_only_for_otp():
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/otp 834521"}},
        {"update_id": 2, "message": {"chat": {"id": 111}, "text": "/otp@my_bot 112233"}},
        {"update_id": 3, "message": {"chat": {"id": 111}, "text": "/kill now please"}},
        {"update_id": 4, "message": {"chat": {"id": 111}, "text": "/"}},
        {"update_id": 5, "message": {"chat": {"id": 999}, "text": "/otp 000000"}},       # người lạ
    ]
    cmds, _ = parse_commands(updates, authorized_chat_id="111")
    assert cmds == ["otp 834521", "otp 112233", "kill"]
