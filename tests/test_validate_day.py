"""Test cho recorder/validate.py + scripts/validate_day.py (PROMPTS P1.5) — viết TRƯỚC."""
from datetime import date, datetime, time, timedelta

import pytest

from backtest.engine import load_bars_parquet
from core.clock import FakeClock
from core.models import Bar, Tick
from recorder.tick_recorder import TickRecorder
from recorder.validate import (build_bars, compare_reference, divergence_stats, find_gaps,
                               validate_day, volume_check, write_bars)

SYM = "VN30F2611"
DAY = date(2026, 11, 9)


def at(hh, mm, ss=0, us=0):
    return datetime(2026, 11, 9, hh, mm) + timedelta(seconds=ss, microseconds=us)


def trade(ts, px, qty=1, src="dnse", total=None, bid=None, ask=None):
    return Tick(SYM, ts, last=px, last_qty=qty, total_volume=total, bid=bid, ask=ask, source=src)


def quote(ts, bid, ask, src="dnse"):
    return Tick(SYM, ts, bid=bid, ask=ask, source=src)


SEG = [(at(10, 0), at(10, 5))]


# ------------------------------------------------------------------ khoảng trống ----

class TestGaps:
    def test_gap_over_threshold(self):
        times = [at(10, 0, s) for s in range(0, 301, 2) if not 100 < s < 108]   # lỗ 100→108 = 8s
        gaps = find_gaps(times, SEG, gap_seconds=5)
        assert [(g.start, g.end) for g in gaps] == [(at(10, 1, 40), at(10, 1, 48))]
        assert gaps[0].seconds == 8

    def test_exactly_threshold_not_a_gap(self):
        times = [at(10, 0, s) for s in range(0, 301, 5)]
        assert find_gaps(times, SEG, gap_seconds=5) == []

    def test_segment_edges_count(self):
        times = [at(10, 0, s) for s in range(10, 280, 2)]                 # mất 10s đầu, 20s cuối
        gaps = find_gaps(times, SEG, gap_seconds=5)
        assert [(g.start, g.end) for g in gaps] == [(at(10, 0), at(10, 0, 10)), (at(10, 4, 38), at(10, 5))]

    def test_empty_segment_is_one_gap(self):
        gaps = find_gaps([], SEG, gap_seconds=5)
        assert len(gaps) == 1 and gaps[0].seconds == 300

    def test_ticks_outside_segments_ignored(self):
        times = [at(9, 59, 58)] + [at(10, 0, s) for s in range(0, 301, 2)] + [at(10, 30)]
        assert find_gaps(times, SEG, gap_seconds=5) == []


# ------------------------------------------------------------------ khối lượng ----

class TestVolume:
    def test_consistent(self):
        ticks = [trade(at(10, 0, 1), 1350, 2, total=100), trade(at(10, 0, 2), 1350, 3, total=103),
                 trade(at(10, 0, 3), 1350, 1, total=104)]
        v = volume_check(ticks)
        assert v["missing"] == 0 and v["checked"]

    def test_missing_trades_detected(self):
        ticks = [trade(at(10, 0, 1), 1350, 2, total=100), trade(at(10, 0, 2), 1350, 3, total=110)]
        assert volume_check(ticks)["missing"] == 7

    def test_no_total_volume_cannot_check(self):
        v = volume_check([trade(at(10, 0, 1), 1350, 2)])
        assert not v["checked"]


# ------------------------------------------------------------------ lệch nguồn ----

class TestDivergence:
    def test_stats_per_second(self):
        ticks = []
        for s in range(0, 60):
            ticks.append(quote(at(10, 0, s), 1350.0, 1350.2, "dnse"))
            ssi_mid = 1350.6 if 20 <= s < 25 else 1350.1                    # 5 giây lệch 5 bước
            ticks.append(quote(at(10, 0, s, 500000), ssi_mid - 0.1, ssi_mid + 0.1, "ssi"))
        d = divergence_stats(ticks, SEG, tick_size=0.1, threshold_ticks=3, stale_seconds=5)
        assert d["seconds_compared"] >= 59
        assert d["max_ticks"] == pytest.approx(5.0)
        assert d["seconds_over_threshold"] == 5

    def test_stale_source_not_compared(self):
        ticks = [quote(at(10, 0, 0), 1360, 1360.2, "ssi")] + \
                [quote(at(10, 0, s), 1350, 1350.2, "dnse") for s in range(10, 20)]
        d = divergence_stats(ticks, SEG, tick_size=0.1, threshold_ticks=3, stale_seconds=5)
        assert d["seconds_over_threshold"] == 0


    def test_stale_primary_not_compared(self):
        ticks = [quote(at(10, 0, 0), 1360, 1360.2, "dnse")] + \
                [quote(at(10, 0, s), 1350, 1350.2, "ssi") for s in range(10, 20)]
        d = divergence_stats(ticks, SEG, tick_size=0.1, threshold_ticks=3, stale_seconds=5)
        assert d["seconds_over_threshold"] == 0

    def test_exactly_threshold_not_counted(self):
        ticks = []
        for s in range(0, 30):
            ticks.append(quote(at(10, 0, s), 1350.0, 1350.2, "dnse"))
            ticks.append(quote(at(10, 0, s, 1), 1350.3, 1350.5, "ssi"))     # lệch đúng 3 bước
        d = divergence_stats(ticks, SEG, tick_size=0.1, threshold_ticks=3, stale_seconds=5)
        assert d["seconds_over_threshold"] == 0 and d["max_ticks"] == pytest.approx(3.0)


# ------------------------------------------------------------------ bar & tham chiếu ----

def test_build_bars_from_trades_only():
    ticks = [trade(at(10, 0, 5), 1350, 2), quote(at(10, 0, 6), 1349, 1351),
             trade(at(10, 0, 40), 1352, 1), trade(at(10, 0, 50), 1349.5, 3), trade(at(10, 1, 2), 1351, 1)]
    bars = build_bars(ticks)
    assert [(b.start, b.open, b.high, b.low, b.close, b.volume) for b in bars] == [
        (at(10, 0), 1350, 1352, 1349.5, 1349.5, 6), (at(10, 1), 1351, 1351, 1351, 1351, 1)]
    assert all(b.volume_reliable for b in bars)


def test_compare_reference():
    ours = [Bar(SYM, at(10, m), 1350, 1351, 1349, 1350.5, 5) for m in range(4)]
    ref = [Bar(SYM, at(10, 0), 1350, 1351, 1349, 1350.5, 0, 60, False),       # khớp
           Bar(SYM, at(10, 1), 1350.1, 1351.2, 1349, 1350.5, 0, 60, False),   # lệch ≤ 2 bước: bỏ qua
           Bar(SYM, at(10, 2), 1350, 1352, 1349, 1350.5, 0, 60, False),       # high lệch 10 bước
           Bar(SYM, at(10, 4), 1350, 1351, 1349, 1350.5, 0, 60, False)]       # ta thiếu phút 10:04
    r = compare_reference(ours, ref, tick_size=0.1, tolerance_ticks=2)
    assert r["minutes_compared"] == 3
    assert [(m["start"], m["field"]) for m in r["mismatches"]] == [(at(10, 2), "high")]
    assert r["missing_in_ours"] == [at(10, 4)] and r["missing_in_reference"] == [at(10, 3)]


def test_write_bars_readable_by_backtest(tmp_path):
    bars = [Bar(SYM, at(10, m), 1350, 1351, 1349, 1350.5, 5) for m in range(3)]
    path = write_bars(bars, tmp_path / "bars", DAY, source="dnse")
    back, skipped = load_bars_parquet(path)
    assert back == bars and skipped == 0


# ------------------------------------------------------------------ cả ngày ----

def record(tmp_path, ticks):
    clock = FakeClock(at(10, 0))
    rec = TickRecorder(tmp_path / "data", clock)
    for t in ticks:
        rec.add(t)
    rec.flush()
    return rec.day_dir(DAY)


def full_day_ticks():
    ticks, total_d, total_s = [], 1000, 1000
    for s in range(0, 301, 2):
        ts = at(10, 0) + timedelta(seconds=s)
        px = 1350.0 + ((s // 2) % 2) / 10                                # dao động 1 bước
        total_d += 1
        ticks.append(trade(ts, px, 1, "dnse", total=total_d, bid=px - 0.1, ask=px + 0.1))
        if s != 200:                                                    # SSI mất một lần khớp
            total_s += 1
        else:
            total_s += 1
            continue
        ticks.append(trade(ts + timedelta(milliseconds=300), px, 1, "ssi", total=total_s,
                           bid=px - 0.1, ask=px + 0.1))
    return ticks


class TestValidateDay:
    def test_report(self, tmp_path):
        d = record(tmp_path, full_day_ticks() + [quote(at(10, 3, 1, 1), 1351, 1350.5, "dnse")])
        rep = validate_day(d, segments=SEG, tick_size=0.1, gap_seconds=5, divergence_ticks=3,
                           stale_seconds=5, ref_price=1350.0, daily_limit_pct=7.0)
        dn, ss = rep.sources["dnse"], rep.sources["ssi"]
        assert dn.n_trades == 151 and dn.n_quotes == 1 and dn.crossed_quotes == 1
        assert ss.volume["missing"] == 1 and dn.volume["missing"] == 0
        assert dn.gaps == [] and dn.out_of_band == 0
        assert rep.divergence["seconds_over_threshold"] == 0
        assert len(rep.bars["dnse"]) == 6
        assert any("ssi" in i and "hụt" in i for i in rep.issues)
        assert any("bid > ask" in i for i in rep.issues)

    def test_out_of_band_and_jumps(self, tmp_path):
        ticks = [trade(at(10, 0, s), 1350.0) for s in range(0, 301, 2)]
        ticks[10] = trade(ticks[10].ts, 1460.0)                         # > tham chiếu +7%
        d = record(tmp_path, ticks)
        rep = validate_day(d, segments=SEG, tick_size=0.1, gap_seconds=5, divergence_ticks=3,
                           stale_seconds=5, ref_price=1350.0, daily_limit_pct=7.0, jump_pct=1.0)
        assert rep.sources["dnse"].out_of_band == 1
        assert rep.sources["dnse"].jumps == 2                           # lên rồi xuống

    def test_no_ref_price_skips_band_check(self, tmp_path):
        d = record(tmp_path, [trade(at(10, 0, s), 1350.0) for s in range(0, 301, 2)])
        rep = validate_day(d, segments=SEG, tick_size=0.1, gap_seconds=5, divergence_ticks=3,
                           stale_seconds=5, ref_price=None, daily_limit_pct=7.0)
        assert rep.sources["dnse"].out_of_band is None
        assert any("tham chiếu" in n for n in rep.notes)

    def test_empty_day(self, tmp_path):
        rep = validate_day(tmp_path / "nothing", segments=SEG, tick_size=0.1, gap_seconds=5,
                           divergence_ticks=3, stale_seconds=5)
        assert rep.issues and "không có dữ liệu" in rep.issues[0]

    def test_text_summary(self, tmp_path):
        d = record(tmp_path, full_day_ticks())
        rep = validate_day(d, segments=SEG, tick_size=0.1, gap_seconds=5, divergence_ticks=3,
                           stale_seconds=5)
        text = rep.summary()
        for needle in ("dnse", "ssi", "tick", "khoảng trống", "lệch", "bar"):
            assert needle in text


def test_script_end_to_end(tmp_path, session_cfg):
    from core.settings import Settings
    from scripts.validate_day import run
    data = tmp_path / "data"
    clock = FakeClock(at(10, 0))
    rec = TickRecorder(data, clock)
    for t in full_day_ticks():
        rec.add(t)
    rec.flush()
    ref_dir = data / "imported"
    write_bars([Bar(SYM, at(10, 0), 1350, 1350.8, 1350, 1350.8, 0, 60, False)], ref_dir, DAY, "ref",
               filename=f"{DAY.isoformat()}.parquet")
    s = Settings({"paths": {"data_dir": str(data)}, "session": session_cfg,
                  "contract": {"tick_size_points": 0.1, "daily_limit_pct": 7.0, "roll_days_before_expiry": 1},
                  "feeds": {"primary": "dnse", "feed_divergence_ticks": 3, "feed_stale_seconds": 5}})
    code, rep = run(DAY, s, ref_price=None)
    assert code == 1                                     # có vấn đề (gap cả ngày ngoài 10:00–10:05, hụt SSI)
    assert (data / "bars" / f"{DAY.isoformat()}.parquet").exists()
    assert (data / "validation" / f"{DAY.isoformat()}.json").exists()
    assert rep.reference is not None and rep.reference["minutes_compared"] == 1
