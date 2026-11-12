"""Recorder ghi thêm hợp đồng kế tiếp ngày trước roll — viết TRƯỚC khi implement.

Mục đích: `data/bars/<ngày trước roll>.parquet` có cả HĐ kế tiếp ⇒ đo được chênh lệch back-adjust
(`live/history.py`) mà không cần chạy downloader bằng tay.

- `DnseFeed(extra_contracts=[...])`: đăng ký thêm trên CÙNG kết nối (một message `symbols: [chính, phụ]`) —
  không mở kết nối thứ hai (giới hạn số kết nối/khoá ❓; kết nối thừa có thể làm rớt kết nối của runner).
  Độ "im lặng" (`last_message_at`, `is_stale`) chỉ tính tick của HĐ CHÍNH. HĐ phụ không có trong danh sách
  broker ⇒ bỏ HĐ phụ + cảnh báo, vẫn ghi HĐ chính. Không truyền ⇒ y hệt trước (runner không đổi).
- `scripts/record_today.py`: `roll_eve_extras` (ngày trước roll ⇒ [HĐ kế tiếp]); bật bằng
  `recorder.record_next_contract_on_roll_eve` (mặc định false tới khi `dnse_feed_probe.py --extra-contract`
  xác nhận server nhận đăng ký nhiều mã — CLAUDE.md mục 8: ❓ phải gọi thử trước khi dựa vào).
- Kiểm định cuối ngày tách theo HĐ: mọi kiểm chất lượng (khoảng trống, giá nhảy, KL, lệch hai nguồn, so tham
  chiếu) chỉ trên HĐ chính; HĐ phụ chỉ đếm + dựng bar (ghi vào cùng file bar).
"""
import asyncio
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from core import contracts
from core.clock import FakeClock
from core.models import Bar, Tick
from recorder.tick_recorder import TickRecorder
from recorder.validate import validate_day
from tests.test_feed_dnse import (KRX, NOW, SYM, Connector, FakeTransport, m, make_feed, run_until, stream)

DEC, DEC_KRX = "VN30F2612", "41I1GC000"
D18, D19 = date(2026, 11, 18), date(2026, 11, 19)


# ------------------------------------------------------------------ feed ----

def test_extra_contract_subscribed_on_same_connection(cal, session_cfg):
    tr = FakeTransport(stream("trade", m("trade", symbol=DEC_KRX, price=1402.0)))
    conn = Connector(tr)
    feed, _, _ = make_feed(cal, session_cfg, conn, listed=(KRX, DEC_KRX), extra_contracts=[DEC])
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 2))
    assert tr.sent[1] == {"action": "subscribe", "channels": [{"name": "tick.G1.json", "symbols": [KRX, DEC_KRX]}]}
    assert len(conn.urls) == 1                                       # một kết nối
    assert [(t.symbol, t.last) for t in ticks] == [(SYM, 1352.5), (DEC, 1402.0)]
    assert feed.stats()["extra_contracts"] == [DEC]


def test_without_extras_subscription_unchanged(cal, session_cfg):
    tr = FakeTransport(stream("trade"))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr), listed=(KRX, DEC_KRX))
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert tr.sent[1] == {"action": "subscribe", "channels": [{"name": "tick.G1.json", "symbols": [KRX]}]}
    assert feed.stats()["extra_contracts"] == []


def test_only_primary_ticks_keep_feed_fresh(cal, session_cfg):
    tr = FakeTransport(stream(m("trade", symbol=DEC_KRX, price=1402.0)))
    feed, clock, _ = make_feed(cal, session_cfg, Connector(tr), listed=(KRX, DEC_KRX), extra_contracts=[DEC])
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert ticks and ticks[0].symbol == DEC
    assert feed.last_message_at is None and feed.is_stale(NOW)      # HĐ chính chưa có gì ⇒ vẫn "im lặng"


def test_unlisted_extra_skipped_primary_still_recorded(cal, session_cfg, caplog):
    tr = FakeTransport(stream("trade"))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr), listed=(KRX,), extra_contracts=[DEC])
    ticks = []
    feed.on_tick(ticks.append)
    with caplog.at_level("WARNING"):
        asyncio.run(run_until(feed, ticks, 1))
    assert tr.sent[1] == {"action": "subscribe", "channels": [{"name": "tick.G1.json", "symbols": [KRX]}]}
    assert [t.symbol for t in ticks] == [SYM]
    assert any(DEC in r.getMessage() for r in caplog.records)


def test_extra_equal_to_primary_ignored(cal, session_cfg):
    tr = FakeTransport(stream("trade"))
    feed, _, _ = make_feed(cal, session_cfg, Connector(tr), extra_contracts=[SYM])
    ticks = []
    feed.on_tick(ticks.append)
    asyncio.run(run_until(feed, ticks, 1))
    assert tr.sent[1] == {"action": "subscribe", "channels": [{"name": "tick.G1.json", "symbols": [KRX]}]}


# ------------------------------------------------------------------ record_today ----

def test_roll_eve_extras(cal):
    from scripts.record_today import roll_eve_extras
    assert roll_eve_extras(D18, cal, 1) == [DEC]
    assert roll_eve_extras(date(2026, 11, 17), cal, 1) == []
    assert roll_eve_extras(D19, cal, 1) == []
    assert roll_eve_extras(D19, cal, 0) == [DEC]                   # roll sau ngày đáo hạn ⇒ ngày trước = 19/11


def test_feed_kwargs_follow_setting(cal):
    from core.settings import Settings
    from scripts.record_today import feed_kwargs
    on = Settings({"recorder": {"record_next_contract_on_roll_eve": True}})
    off = Settings({"recorder": {"record_next_contract_on_roll_eve": False}})
    assert feed_kwargs(on, D18, cal, 1) == {"extra_contracts": [DEC]}
    assert feed_kwargs(off, D18, cal, 1) == {"extra_contracts": []}
    assert feed_kwargs(Settings({}), D18, cal, 1) == {"extra_contracts": []}
    assert feed_kwargs(on, date(2026, 11, 17), cal, 1) == {"extra_contracts": []}


def test_computed_listing_includes_extras(cal):
    from scripts.record_today import computed_listing
    assert computed_listing(D18, cal, 1, [DEC]) == [KRX, DEC_KRX]
    assert computed_listing(D18, cal, 1, []) == [KRX]


def test_repo_setting_defaults_off():
    from core.settings import load_settings
    assert load_settings().get("recorder.record_next_contract_on_roll_eve") is False


# ------------------------------------------------------------------ kiểm định theo HĐ ----

def at(hh, mm, ss=0, ms=0, d=D18):
    return datetime.combine(d, time(hh, mm)) + timedelta(seconds=ss, milliseconds=ms)


SEG = [(at(10, 0), at(10, 5))]


def mixed_ticks():
    """HĐ chính dày (dnse + ssi, KL luỹ kế đủ); HĐ phụ thưa (dnse, khoảng trống 60s, giá 1400)."""
    out, tot_d, tot_s, tot_x = [], 1000, 1000, 50
    for s in range(0, 301, 2):
        tot_d += 1
        tot_s += 1
        out.append(Tick(SYM, at(10, 0, s), last=1350.0, last_qty=1, total_volume=tot_d, bid=1349.9, ask=1350.1,
                        source="dnse"))
        out.append(Tick(SYM, at(10, 0, s, 300), last=1350.0, last_qty=1, total_volume=tot_s, bid=1349.9,
                        ask=1350.1, source="ssi"))
        if s % 60 == 0:
            tot_x += 2
            out.append(Tick(DEC, at(10, 0, s, 500), last=1400.0, last_qty=2, total_volume=tot_x, bid=1399.9,
                            ask=1400.1, source="dnse"))
    return out


def record(tmp_path, ticks, d=D18):
    rec = TickRecorder(tmp_path, FakeClock(at(10, 0, d=d)))
    for t in ticks:
        rec.add(t)
    rec.flush()
    return rec.day_dir(d)


def validate(path, **kw):
    return validate_day(path, segments=SEG, tick_size=0.1, gap_seconds=5, divergence_ticks=3, stale_seconds=5,
                        ref_price=1350.0, daily_limit_pct=7.0, **kw)


def test_quality_checks_on_primary_only(tmp_path):
    rep = validate(record(tmp_path, mixed_ticks()), primary_symbol=SYM)
    dn = rep.sources["dnse"]
    assert dn.n_trades == 151 and dn.gaps == [] and dn.jumps == 0 and dn.volume["missing"] == 0
    assert rep.divergence["seconds_over_threshold"] == 0
    assert rep.issues == []
    assert rep.extras[DEC]["trades"] == 6
    assert any(DEC in n for n in rep.notes)


def test_bars_include_extra_contract(tmp_path):
    rep = validate(record(tmp_path, mixed_ticks()), primary_symbol=SYM)
    syms = {b.symbol for b in rep.bars["dnse"]}
    assert syms == {SYM, DEC}
    assert {b.close for b in rep.bars["dnse"] if b.symbol == DEC} == {1400.0}


def test_reference_compared_on_primary_only(tmp_path):
    ref = [Bar(SYM, at(10, m), 1350.0, 1350.0, 1350.0, 1350.0, 0, 60, False) for m in range(5)]
    rep = validate(record(tmp_path, mixed_ticks()), primary_symbol=SYM, reference_bars=ref, bars_source="dnse")
    assert rep.reference["missing_in_reference"] == [at(10, 5)]      # chỉ phút của HĐ chính
    assert rep.reference["mismatches"] == []


def test_several_contracts_without_primary_is_an_issue(tmp_path):
    rep = validate(record(tmp_path, mixed_ticks()))
    assert any("nhiều hợp đồng" in i for i in rep.issues)


def test_single_contract_without_primary_unchanged(tmp_path):
    ticks = [t for t in mixed_ticks() if t.symbol == SYM]
    rep = validate(record(tmp_path, ticks))
    assert rep.issues == [] and rep.extras == {}


def test_primary_absent_is_an_issue(tmp_path):
    ticks = [t for t in mixed_ticks() if t.symbol == DEC]
    rep = validate(record(tmp_path, ticks), primary_symbol=SYM)
    assert any(SYM in i for i in rep.issues)


# ------------------------------------------------------------------ đầu-cuối ----

def test_recorded_eve_lets_live_history_measure_gap(tmp_path, session_cfg, cal):
    """Ghi 18/11 có cả 2612 ⇒ validate_day.run ghi data/bars ⇒ runner ngày roll 19/11 nạp được lịch sử."""
    from core.settings import Settings
    from live.history import load_live_history
    from scripts.validate_day import run
    data = tmp_path / "data"
    record(data, mixed_ticks())
    s = Settings({"paths": {"data_dir": str(data)}, "session": session_cfg,
                  "contract": {"tick_size_points": 0.1, "daily_limit_pct": 7.0, "roll_days_before_expiry": 1},
                  "feeds": {"primary": "dnse", "feed_divergence_ticks": 3, "feed_stale_seconds": 5}})
    code, rep = run(D18, s, ref_price=None, calendar=cal)
    assert rep.extras[DEC]["trades"] == 6
    h = load_live_history([data / "bars"], D19, 1, cal, lambda d: contracts.trading_contract(d, cal, 1))
    v = h.view(DEC)
    assert {b.start.date() for b in v} == {D18} and {b.symbol for b in v} == {DEC}
    assert {b.close for b in v} == {1400.0}                          # 1350 + chênh lệch 50


# ------------------------------------------------------------------ main ----

@pytest.mark.parametrize("flag,expected", [(True, [DEC]), (False, [])])
def test_main_passes_extras_to_feed_on_roll_eve(monkeypatch, tmp_path, cal, session_cfg, flag, expected):
    import copy
    import scripts.record_today as RT
    from core.clock import MarketClock, SessionSchedule
    from core.settings import Settings, load_settings
    d = copy.deepcopy(load_settings()._data)
    d["paths"].update(data_dir=str(tmp_path / "data"), state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    d["recorder"]["record_next_contract_on_roll_eve"] = flag
    settings = Settings(d)
    monkeypatch.setattr("core.settings.load_settings", lambda *a, **k: settings)
    monkeypatch.setattr("monitoring.logger.setup_logging", lambda *a, **k: None)
    monkeypatch.setenv("DNSE_API_KEY", "k")
    monkeypatch.setenv("DNSE_API_SECRET", "s")

    class MC:
        @staticmethod
        def from_settings(_s):
            return MarketClock(SessionSchedule.from_mapping(session_cfg), cal, FakeClock(datetime(2026, 11, 18, 10, 0)))
    monkeypatch.setattr(RT, "MarketClock", MC)
    captured = {}

    def fake_from_settings(s, key, secret, lister, mclock, **kw):
        captured.update(kw, lister=lister)
        return object()
    monkeypatch.setattr("feeds.data_feed_dnse.DnseFeed.from_settings", staticmethod(fake_from_settings))

    async def fake_session(*a, **k):
        return 0
    monkeypatch.setattr(RT, "record_session", fake_session)

    class Rep:
        def summary(self):
            return ""
    monkeypatch.setattr("scripts.validate_day.run", lambda *a, **k: (0, Rep()))
    assert RT.main(["--assume-computed-contract"]) == 0
    assert captured["extra_contracts"] == expected
    assert asyncio.run(captured["lister"]()) == [KRX] + ([DEC_KRX] if flag else [])
