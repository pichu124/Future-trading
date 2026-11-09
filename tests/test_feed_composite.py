"""Test cho feeds/composite.py (PROMPTS P1.3) — viết TRƯỚC khi implement.

Luật (CLAUDE.md mục 3): đối chiếu DNSE (chính) và SSI (dự phòng) liên tục; lệch giá
> feed_divergence_ticks hoặc MỘT feed im lặng > feed_stale_seconds ⇒ `healthy=False` (risk
ngừng mở vị thế mới) + cảnh báo; DNSE chết ⇒ tự chuyển nguồn chính sang SSI.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from core.clock import FakeClock, MarketClock, SessionSchedule
from core.models import Tick
from core.settings import Settings
from feeds.base import DataFeed
from feeds.composite import AlertKind, CompositeFeed

SYM = "VN30F2611"
T0 = datetime(2026, 11, 9, 10, 0, 0)


class FakeFeed(DataFeed):
    def __init__(self, name, fail=None):
        super().__init__()
        self.name = name
        self.fail = fail
        self._stop = False

    def emit(self, tick):
        for h in self._handlers:
            h(tick)

    async def run(self):
        if self.fail:
            await asyncio.sleep(0.01)
            raise self.fail
        while not self._stop:
            await asyncio.sleep(0.005)

    async def stop(self):
        self._stop = True

    def is_stale(self, now=None):
        return False

    def stats(self):
        return {"name": self.name}


class Rig:
    def __init__(self, session_cfg, cal, **kw):
        self.clock = FakeClock(kw.pop("start", T0))
        self.mclock = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, self.clock)
        self.dnse, self.ssi = FakeFeed("dnse", kw.pop("fail_dnse", None)), FakeFeed("ssi", kw.pop("fail_ssi", None))
        self.alerts = []
        self.comp = CompositeFeed(self.dnse, self.ssi, self.mclock, tick_size=0.1,
                                  divergence_ticks=3, stale_seconds=5, switch_back_after_s=30,
                                  on_alert=self.alerts.append, **kw)
        self.out, self.all = [], []
        self.comp.on_tick(self.out.append)
        self.comp.on_any_tick(self.all.append)

    def at(self, seconds):
        self.clock.set(T0 + timedelta(seconds=seconds))

    def quote(self, feed, bid, ask):
        f = self.dnse if feed == "dnse" else self.ssi
        f.emit(Tick(SYM, self.clock.now(), bid=bid, ask=ask, source=feed))

    def trade(self, feed, px):
        f = self.dnse if feed == "dnse" else self.ssi
        f.emit(Tick(SYM, self.clock.now(), last=px, source=feed))

    def both(self, bid=1352.3, ask=1352.5):
        self.quote("dnse", bid, ask)
        self.quote("ssi", bid, ask)

    def kinds(self):
        return [a.kind for a in self.alerts]


@pytest.fixture
def rig(session_cfg, cal):
    return Rig(session_cfg, cal)


def test_is_data_feed(rig):
    assert isinstance(rig.comp, DataFeed) and rig.comp.name == "composite"


def test_forwards_only_active_source(rig):
    rig.both()
    assert [t.source for t in rig.out] == ["dnse"]
    assert [t.source for t in rig.all] == ["dnse", "ssi"]
    assert rig.comp.active == "dnse" and rig.comp.healthy


class TestDivergence:
    def test_above_threshold_alerts_and_unhealthy(self, rig):
        rig.quote("dnse", 1352.3, 1352.5)                 # mid 1352.4
        rig.quote("ssi", 1352.8, 1353.0)                  # mid 1352.9 ⇒ lệch 5 bước
        assert not rig.comp.healthy
        assert rig.kinds() == [AlertKind.DIVERGENCE]
        assert "5.0" in rig.alerts[0].message

    def test_exactly_threshold_is_ok(self, rig):
        rig.quote("dnse", 1352.3, 1352.5)
        rig.quote("ssi", 1352.6, 1352.8)                  # mid 1352.7 ⇒ đúng 3 bước
        assert rig.comp.healthy and rig.alerts == []

    def test_clears_when_back_in_line(self, rig):
        rig.quote("dnse", 1352.3, 1352.5)
        rig.quote("ssi", 1352.8, 1353.0)
        rig.quote("ssi", 1352.3, 1352.5)
        assert rig.comp.healthy
        assert rig.kinds() == [AlertKind.DIVERGENCE, AlertKind.DIVERGENCE_CLEARED]

    def test_uses_last_trade_when_no_quote(self, rig):
        rig.trade("dnse", 1352.4)
        rig.trade("ssi", 1353.0)
        assert AlertKind.DIVERGENCE in rig.kinds()

    def test_old_price_not_compared(self, rig):
        rig.quote("ssi", 1360.0, 1360.2)                  # rất lệch nhưng đã cũ
        rig.at(10)
        rig.quote("dnse", 1352.3, 1352.5)
        assert AlertKind.DIVERGENCE not in rig.kinds()

    def test_alerts_are_edge_triggered(self, rig):
        rig.quote("dnse", 1352.3, 1352.5)
        for _ in range(5):
            rig.quote("ssi", 1352.8, 1353.0)
            rig.comp.check()
        assert rig.kinds() == [AlertKind.DIVERGENCE]


class TestStaleAndSwitch:
    def test_primary_silent_switches_to_backup(self, rig):
        rig.both()
        rig.at(5)
        rig.quote("ssi", 1352.3, 1352.5)
        rig.comp.check()
        assert rig.comp.active == "dnse"                  # đúng 5 giây: chưa "> 5"
        rig.at(6)
        rig.quote("ssi", 1352.3, 1352.5)
        assert rig.comp.active == "ssi"
        assert not rig.comp.healthy                       # vẫn chặn mở vị thế khi một feed im
        assert AlertKind.STALE in rig.kinds() and AlertKind.SWITCHED in rig.kinds()
        assert rig.out[-1].source == "ssi"

    def test_switch_back_after_primary_stable(self, rig):
        rig.both()
        rig.at(6)
        rig.quote("ssi", 1352.3, 1352.5)
        assert rig.comp.active == "ssi"
        for s in range(7, 37):                            # DNSE sống lại từ giây 7
            rig.at(s)
            rig.both()
            if s < 37 - 1:
                assert rig.comp.active == "ssi", s
        rig.at(37)
        rig.both()
        assert rig.comp.active == "dnse"
        assert rig.kinds().count(AlertKind.SWITCHED_BACK) == 1
        assert rig.comp.healthy

    def test_backup_silent_blocks_entries_but_keeps_primary(self, rig):
        rig.both()
        rig.at(6)
        rig.quote("dnse", 1352.3, 1352.5)
        assert rig.comp.active == "dnse" and not rig.comp.healthy
        assert [a.source for a in rig.alerts if a.kind is AlertKind.STALE] == ["ssi"]

    def test_backup_dies_while_active_switches_back_immediately(self, rig):
        rig.both()
        rig.at(6)
        rig.quote("ssi", 1352.3, 1352.5)                  # chuyển sang ssi
        rig.at(8)
        rig.quote("dnse", 1352.3, 1352.5)                 # dnse sống lại
        rig.at(12)
        rig.quote("dnse", 1352.3, 1352.5)                 # ssi im > 5 giây
        assert rig.comp.active == "dnse"

    def test_lunch_silence_is_normal(self, session_cfg, cal):
        r = Rig(session_cfg, cal, start=datetime(2026, 11, 9, 11, 29, 0))
        r.both()
        r.clock.set(datetime(2026, 11, 9, 12, 30))
        r.comp.check()
        assert r.comp.healthy and r.alerts == [] and r.comp.active == "dnse"

    def test_never_received_is_stale_in_session(self, rig):
        rig.comp.check()
        assert not rig.comp.healthy and rig.comp.is_stale()

    def test_recovered_alert(self, rig):
        rig.both()
        rig.at(6)
        rig.quote("dnse", 1352.3, 1352.5)
        rig.quote("ssi", 1352.3, 1352.5)
        assert AlertKind.RECOVERED in rig.kinds()


class TestRun:
    def test_one_feed_down_composite_keeps_running(self, session_cfg, cal):
        r = Rig(session_cfg, cal, fail_ssi=RuntimeError("sai token"), monitor_interval_s=0.005)

        async def go():
            task = asyncio.create_task(r.comp.run())
            await asyncio.sleep(0.05)
            r.quote("dnse", 1352.3, 1352.5)
            assert not task.done()
            await r.comp.stop()
            await asyncio.wait_for(task, 1)
        asyncio.run(go())
        assert AlertKind.FEED_DOWN in r.kinds()
        assert not r.comp.healthy and r.out[-1].source == "dnse"

    def test_both_down_raises(self, session_cfg, cal):
        r = Rig(session_cfg, cal, fail_dnse=RuntimeError("a"), fail_ssi=RuntimeError("b"),
                monitor_interval_s=0.005)
        with pytest.raises(RuntimeError):
            asyncio.run(r.comp.run())

    def test_monitor_detects_silence_without_ticks(self, session_cfg, cal):
        r = Rig(session_cfg, cal, monitor_interval_s=0.005)
        r.both()

        async def go():
            task = asyncio.create_task(r.comp.run())
            await asyncio.sleep(0.02)
            r.at(10)                                       # không tick nào tới nữa
            await asyncio.sleep(0.03)
            await r.comp.stop()
            await asyncio.wait_for(task, 1)
        asyncio.run(go())
        assert AlertKind.STALE in r.kinds() and not r.comp.healthy


def test_from_settings(session_cfg, cal):
    s = Settings({"contract": {"tick_size_points": 0.1},
                  "feeds": {"feed_divergence_ticks": 3, "feed_stale_seconds": 5,
                            "switch_back_after_seconds": 30}})
    mclock = MarketClock(SessionSchedule.from_mapping(session_cfg), cal, FakeClock(T0))
    c = CompositeFeed.from_settings(s, FakeFeed("dnse"), FakeFeed("ssi"), mclock)
    assert (c.divergence_ticks, c.stale_seconds, c.switch_back_after_s) == (3, 5, 30)


def test_stats_and_recorder_hook(rig, tmp_path):
    from recorder.tick_recorder import TickRecorder, load_ticks
    rec = TickRecorder(tmp_path / "data", rig.clock)
    rig.comp.on_any_tick(rec.add)
    rig.both()
    rec.flush()
    assert {t.source for t in load_ticks(rec.day_dir())} == {"dnse", "ssi"}
    s = rig.comp.stats()
    assert s["active"] == "dnse" and s["ticks_by_source"] == {"dnse": 1, "ssi": 1}


def test_no_false_alarm_at_startup_but_blocks_entries(rig):
    rig.quote("dnse", 1352.3, 1352.5)                     # SSI chưa kịp gửi gì
    assert AlertKind.STALE not in rig.kinds()
    assert not rig.comp.healthy                           # chưa đủ bằng chứng ⇒ chưa cho mở vị thế
    rig.at(6)
    rig.quote("dnse", 1352.3, 1352.5)
    assert [a.source for a in rig.alerts if a.kind is AlertKind.STALE] == ["ssi"]


def test_no_false_alarm_right_after_lunch(session_cfg, cal):
    r = Rig(session_cfg, cal, start=datetime(2026, 11, 9, 11, 29, 50))
    r.both()
    r.clock.set(datetime(2026, 11, 9, 13, 0, 3))          # vừa vào phiên chiều, chưa có tick
    r.comp.check()
    assert AlertKind.STALE not in r.kinds()
    r.clock.set(datetime(2026, 11, 9, 13, 0, 6))          # im quá 5 giây kể từ 13:00
    r.comp.check()
    assert AlertKind.STALE in r.kinds()


def test_both_silent_does_not_switch_to_dead_backup(rig):
    rig.both()
    rig.at(10)
    rig.comp.check()
    assert rig.comp.active == "dnse" and AlertKind.SWITCHED not in rig.kinds()
    assert rig.comp.is_stale()


def test_feed_down_is_unhealthy_immediately(session_cfg, cal):
    """Feed chết ngay sau tick cuối (tick còn mới) ⇒ không khoẻ NGAY, không đợi hết stale_seconds."""
    r = Rig(session_cfg, cal, fail_ssi=RuntimeError("sai token"), monitor_interval_s=0.005)
    r.both()
    assert r.comp.healthy

    async def go():
        task = asyncio.create_task(r.comp.run())
        # đồng hồ giả vẫn ở T0 (tick còn mới) ⇒ chỉ feed chết mới làm mất khoẻ; chờ tối đa 2s thay vì
        # ngủ cứng 50ms (máy CI bận có thể chưa xử lý kịp ⇒ test chập chờn)
        for _ in range(400):
            if not r.comp.healthy:
                break
            await asyncio.sleep(0.005)
        healthy = r.comp.healthy
        await r.comp.stop()
        await asyncio.wait_for(task, 1)
        return healthy
    assert asyncio.run(go()) is False
