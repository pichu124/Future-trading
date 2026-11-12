"""Test cho recorder/tick_recorder.py và scripts/record_today.py (PROMPTS P1.4) — viết TRƯỚC.

Thiết kế chống mất dữ liệu: mỗi lần flush ghi một file part MỚI (tmp-rồi-tráo), không bao giờ
ghi đè/ghi nối file cũ ⇒ crash chỉ mất phần còn trong RAM (≤ flush_seconds), không làm hỏng
file nào. Cuối ngày gộp part → ticks.parquet, gộp lại nhiều lần không sinh bản trùng.
"""
import asyncio
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from core.clock import FakeClock, MarketClock, SessionSchedule
from core.models import Tick
from feeds.base import DataFeed
from recorder.tick_recorder import (TickRecorder, assert_not_in_sync_folder, compact_day,
                                    load_ticks, part_files)

SYM = "VN30F2611"
T0 = datetime(2026, 11, 9, 9, 0, 0)


def tk(sec, last=1350.0, source="dnse", **kw):
    return Tick(SYM, T0 + timedelta(seconds=sec), last=last, last_qty=kw.pop("last_qty", 1),
                bid=kw.pop("bid", last - 0.1), ask=kw.pop("ask", last + 0.1), source=source, **kw)


@pytest.fixture
def clock():
    return FakeClock(T0)


def make(tmp_path, clock, **kw):
    return TickRecorder(tmp_path / "data", clock, session_id=kw.pop("session_id", "S1"), **kw)


def day_dir(tmp_path, d=date(2026, 11, 9)) -> Path:
    return tmp_path / "data" / "raw" / d.isoformat()


# ------------------------------------------------------------------ ghi part ----

class TestFlush:
    def test_flush_writes_one_part_and_roundtrips_all_fields(self, tmp_path, clock):
        r = make(tmp_path, clock)
        ticks = [tk(1), tk(2, bid=None, ask=None, last_qty=None),
                 Tick(SYM, T0 + timedelta(seconds=3), bid=1349.9, ask=1350.1, bid_qty=5,
                      ask_qty=7, total_volume=1234, source="ssi", seq=42)]
        for t in ticks:
            r.add(t)
        assert r.flush() == 3
        parts = part_files(day_dir(tmp_path))
        assert len(parts) == 1 and not list(day_dir(tmp_path).glob("*.tmp"))
        back = load_ticks(day_dir(tmp_path))
        assert back == ticks                                   # đủ mọi trường, None giữ nguyên

    def test_empty_flush_writes_nothing(self, tmp_path, clock):
        r = make(tmp_path, clock)
        assert r.flush() == 0
        assert not day_dir(tmp_path).exists() or part_files(day_dir(tmp_path)) == []

    def test_each_flush_is_a_new_file(self, tmp_path, clock):
        r = make(tmp_path, clock)
        r.add(tk(1)); r.flush()
        clock.advance(seconds=5)
        r.add(tk(6)); r.flush()
        assert len(part_files(day_dir(tmp_path))) == 2
        assert [t.ts.second for t in load_ticks(day_dir(tmp_path))] == [1, 6]

    def test_buffer_limit_triggers_flush(self, tmp_path, clock):
        r = make(tmp_path, clock, max_buffer_rows=3)
        for i in range(7):
            r.add(tk(i))
        assert len(part_files(day_dir(tmp_path))) == 2           # 3 + 3, còn 1 trong RAM
        assert r.buffered == 1

    def test_failed_write_keeps_buffer(self, tmp_path, clock, monkeypatch):
        import recorder.tick_recorder as mod
        r = make(tmp_path, clock)
        r.add(tk(1)); r.add(tk(2))

        def boom(*a, **k):
            raise OSError("ổ đĩa đầy")
        monkeypatch.setattr(mod, "write_table_atomic", boom)
        with pytest.raises(OSError):
            r.flush()
        assert r.buffered == 2 and r.stats()["write_errors"] == 1
        monkeypatch.undo()
        r.add(tk(3))
        assert r.flush() == 3
        assert len(load_ticks(day_dir(tmp_path))) == 3

    def test_crash_loses_only_unflushed(self, tmp_path, clock):
        r = make(tmp_path, clock)
        r.add(tk(1)); r.flush()
        r.add(tk(2))                                              # chưa flush thì "crash"
        del r
        assert [t.ts.second for t in load_ticks(day_dir(tmp_path))] == [1]

    def test_day_folder_follows_recorder_clock(self, tmp_path, clock):
        clock.set(datetime(2026, 11, 10, 9, 0))
        r = make(tmp_path, clock)
        r.add(tk(1)); r.flush()
        assert part_files(day_dir(tmp_path, date(2026, 11, 10)))

    def test_stats(self, tmp_path, clock):
        r = make(tmp_path, clock)
        r.add(tk(1)); r.add(tk(2, source="ssi")); r.flush()
        s = r.stats()
        assert s["rows_written"] == 2 and s["parts_written"] == 1 and s["by_source"] == {"dnse": 1, "ssi": 1}
        assert s["last_flush_at"] == T0


# --------------------------------------------------------------- flusher định kỳ ----

def test_periodic_flusher(tmp_path, clock):
    r = make(tmp_path, clock, flush_seconds=0.01)

    async def go():
        task = asyncio.create_task(r.run_flusher())
        r.add(tk(1))
        await asyncio.sleep(0.05)
        r.add(tk(2))
        await asyncio.sleep(0.05)
        await r.close()                                           # dừng + flush cuối + gộp
        await asyncio.wait_for(task, 1)
    asyncio.run(go())
    d = day_dir(tmp_path)
    assert (d / "ticks.parquet").exists() and part_files(d) == []
    assert [t.ts.second for t in load_ticks(d)] == [1, 2]


# ------------------------------------------------------------------ gộp cuối ngày ----

class TestCompaction:
    def _two_sessions(self, tmp_path, clock):
        r1 = make(tmp_path, clock, session_id="S1")
        r1.add(tk(1)); r1.add(tk(2)); r1.flush()
        clock.advance(seconds=30)                                 # khởi động lại cùng ngày
        r2 = make(tmp_path, clock, session_id="S2")
        r2.add(tk(40)); r2.flush()
        return day_dir(tmp_path)

    def test_restart_same_day_keeps_both_sessions(self, tmp_path, clock):
        d = self._two_sessions(tmp_path, clock)
        assert [t.ts.second for t in load_ticks(d)] == [1, 2, 40]

    def test_compact_merges_and_removes_parts(self, tmp_path, clock):
        d = self._two_sessions(tmp_path, clock)
        assert compact_day(d) == 3
        assert part_files(d) == [] and (d / "ticks.parquet").exists()
        assert [t.ts.second for t in load_ticks(d)] == [1, 2, 40]

    def test_compact_is_idempotent(self, tmp_path, clock):
        d = self._two_sessions(tmp_path, clock)
        compact_day(d)
        assert compact_day(d) == 3
        assert len(load_ticks(d)) == 3

    def test_interrupted_compaction_does_not_duplicate(self, tmp_path, clock, monkeypatch):
        """Crash sau khi ghi ticks.parquet nhưng trước khi xoá part ⇒ gộp lại không nhân đôi."""
        import recorder.tick_recorder as mod
        d = self._two_sessions(tmp_path, clock)
        monkeypatch.setattr(mod, "_remove_parts", lambda parts: None)
        compact_day(d)
        assert len(part_files(d)) == 2                            # part còn sót
        assert len(load_ticks(d)) == 3                            # đọc vẫn không trùng
        monkeypatch.undo()
        compact_day(d)
        assert len(load_ticks(d)) == 3 and part_files(d) == []

    def test_failed_compaction_write_keeps_parts(self, tmp_path, clock, monkeypatch):
        """Ghi bản gộp lỗi (đĩa đầy, mất điện) ⇒ part KHÔNG được xoá, dữ liệu còn nguyên."""
        import recorder.tick_recorder as mod
        d = self._two_sessions(tmp_path, clock)

        def boom(*a, **k):
            raise OSError("ổ đĩa đầy")
        monkeypatch.setattr(mod, "write_table_atomic", boom)
        with pytest.raises(OSError):
            compact_day(d)
        monkeypatch.undo()
        assert len(part_files(d)) == 2 and len(load_ticks(d)) == 3

    def test_identical_ticks_from_feed_are_kept(self, tmp_path, clock):
        """Hai lần khớp giống hệt nhau là hai sự kiện thật — chỉ khử trùng theo id ghi, không theo nội dung."""
        r = make(tmp_path, clock)
        r.add(tk(1)); r.add(tk(1)); r.flush()
        compact_day(day_dir(tmp_path))
        assert len(load_ticks(day_dir(tmp_path))) == 2

    def test_new_parts_after_compaction(self, tmp_path, clock):
        d = self._two_sessions(tmp_path, clock)
        compact_day(d)
        r3 = make(tmp_path, clock, session_id="S3")
        r3.add(tk(50)); r3.flush()
        assert [t.ts.second for t in load_ticks(d)] == [1, 2, 40, 50]


def test_loaded_ticks_feed_backtest_engine(tmp_path, clock):
    from backtest.engine import bars_from_ticks
    r = make(tmp_path, clock)
    for i, px in enumerate([1350.0, 1351.0, 1349.5]):
        r.add(tk(i * 30, last=px))
    r.flush()
    events = list(bars_from_ticks(load_ticks(day_dir(tmp_path))))
    bars = [e for e in events if not isinstance(e, Tick)]
    assert (bars[0].open, bars[0].high, bars[0].close) == (1350.0, 1351.0, 1351.0)


# -------------------------------------------------------------- thư mục đồng bộ ----

@pytest.mark.parametrize("p", ["C:/Users/binh/Dropbox/bot/data", "/home/x/dropbox/data",
                               "D:/OneDrive/data", "/Users/x/Google Drive/data"])
def test_sync_folder_refused(p):
    with pytest.raises(ValueError):
        assert_not_in_sync_folder(Path(p))


def test_normal_folder_ok(tmp_path):
    assert_not_in_sync_folder(tmp_path / "data")


def test_recorder_refuses_sync_folder(tmp_path, clock):
    with pytest.raises(ValueError):
        TickRecorder(tmp_path / "Dropbox" / "data", clock)


# ------------------------------------------------------------- record_today ----

from scripts.record_today import plan, record_session  # noqa: E402


@pytest.fixture
def mclock(cal, session_cfg):
    return MarketClock(SessionSchedule.from_mapping(session_cfg), cal, FakeClock(T0))


class TestPlan:
    @pytest.mark.parametrize("now,expected", [
        (datetime(2026, 11, 9, 8, 0), ("wait", 1800.0)),
        (datetime(2026, 11, 9, 8, 30), ("run", 0.0)),
        (datetime(2026, 11, 9, 12, 0), ("run", 0.0)),
        (datetime(2026, 11, 9, 15, 0), ("done", 0.0)),
        (datetime(2026, 11, 7, 10, 0), ("skip", 0.0)),          # thứ Bảy
        (datetime(2026, 9, 2, 10, 0), ("skip", 0.0)),           # Quốc khánh (cal fixture)
    ])
    def test_plan(self, cal, now, expected):
        assert plan(now, cal, time(8, 30), time(15, 0)) == expected


class FakeFeed(DataFeed):
    name = "fake"

    def __init__(self, clock, ticks, fail_after=None):
        super().__init__()
        self.clock, self.ticks, self.fail_after = clock, list(ticks), fail_after
        self.stopped = False

    async def run(self):
        for i, (dt, t) in enumerate(self.ticks):
            if self.fail_after is not None and i >= self.fail_after:
                raise RuntimeError("feed chết")
            self.clock.set(dt)
            for h in self._handlers:
                h(t)
            await asyncio.sleep(0)
        while not self.stopped:
            await asyncio.sleep(0.001)

    async def stop(self):
        self.stopped = True

    def is_stale(self, now=None):
        return False

    def stats(self):
        return {}


def test_record_session_runs_until_stop_time_then_compacts(tmp_path, mclock):
    clock = mclock.clock
    rec = make(tmp_path, clock, flush_seconds=0.01)
    ticks = [(T0 + timedelta(seconds=i), tk(i)) for i in range(5)]
    feed = FakeFeed(clock, ticks)

    async def go():
        async def fake_sleep(s):
            clock.set(datetime(2026, 11, 9, 15, 0))                # nhảy tới giờ dừng
            await asyncio.sleep(0)
        return await record_session(feed, rec, mclock, stop_at=time(15, 0), poll=fake_sleep)
    code = asyncio.run(go())
    d = day_dir(tmp_path)
    assert code == 0 and feed.stopped
    assert (d / "ticks.parquet").exists() and len(load_ticks(d)) == 5


def test_record_session_feed_crash_still_saves(tmp_path, mclock):
    clock = mclock.clock
    rec = make(tmp_path, clock, flush_seconds=60)
    feed = FakeFeed(clock, [(T0 + timedelta(seconds=i), tk(i)) for i in range(5)], fail_after=3)

    async def go():
        return await record_session(feed, rec, mclock, stop_at=time(15, 0),
                                    poll=lambda s: asyncio.sleep(0))
    code = asyncio.run(go())
    assert code != 0                                               # báo lỗi để Task Scheduler thấy
    assert len(load_ticks(day_dir(tmp_path))) == 3                 # 3 tick trước khi chết vẫn được lưu
