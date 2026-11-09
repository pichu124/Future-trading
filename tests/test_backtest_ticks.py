"""Backtest trên tick thật (`backtest/ticks.py` + CLI `--ticks`) — viết TRƯỚC khi implement.

Nguồn tick (cùng schema `recorder.tick_recorder.SCHEMA`):
- recorder: `data/raw/<ngày>/` (`ticks.parquet` + `part-*.parquet`, lọc trùng theo phiên ghi) — có bid/ask;
- downloader: `data/history/trades/<HĐ>/<ngày>.parquet` (`source = dnse_history`) — CHỈ giá khớp, không bid/ask.

Luật:
- đọc đệ quy; thư mục ngày của recorder đọc bằng `load_ticks` (gộp part, lọc trùng); file lẻ đọc thẳng (không
  lọc trùng theo phiên — dòng downloader không có phiên);
- có nhiều `source` mà không chỉ định ⇒ lỗi (trộn hai nguồn ⇒ tick đôi, bar sai); `source=` lọc một nguồn;
- tick khớp KHÔNG có bid lẫn ask ⇒ bid/ask GIẢ = last ∓ max(1, ⌈spread/2⌉) bước (bi quan, như tick giả từ
  bar; spread 0 ⇒ bid = ask = last); tick đã có báo giá giữ nguyên; báo cáo ghi số tick bị giả báo giá.
"""
from datetime import date, datetime, timedelta

import pyarrow as pa
import pytest

from backtest.ticks import fill_missing_quotes, load_tick_files
from core.clock import FakeClock
from core.models import Tick
from recorder.tick_recorder import SCHEMA, TickRecorder

SYM = "VN30F2611"
DAY = date(2026, 11, 9)


def t(sec, px, src="dnse", bid=None, ask=None, qty=1, day=DAY):
    return Tick(SYM, datetime(day.year, day.month, day.day, 10, 0) + timedelta(seconds=sec), last=px, last_qty=qty,
                bid=bid, ask=ask, source=src)


def recorder_day(root, ticks, day=DAY, compact=False):
    rec = TickRecorder(root, FakeClock(datetime(day.year, day.month, day.day, 10, 0)))
    for x in ticks:
        rec.add(x)
    rec.flush()
    if compact:
        from recorder.tick_recorder import compact_day
        compact_day(rec.day_dir(day))
    return rec.day_dir(day)


def downloader_file(path, ticks):
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"symbol": x.symbol, "ts": x.ts, "last": x.last, "last_qty": x.last_qty, "bid": None, "ask": None,
             "bid_qty": None, "ask_qty": None, "total_volume": None, "source": "dnse_history", "seq": None,
             "recv_ts": None, "session": None, "rx_seq": None} for x in ticks]
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), path)


# ------------------------------------------------------------------ đọc ----

class TestLoad:
    def test_recorder_day_dir_with_parts_and_compact(self, tmp_path):
        d = recorder_day(tmp_path, [t(i, 1350.0, bid=1349.9, ask=1350.1) for i in range(5)], compact=True)
        rec = TickRecorder(tmp_path, FakeClock(datetime(2026, 11, 9, 10, 5)))
        rec.add(t(10, 1351.0, bid=1350.9, ask=1351.1))
        rec.flush()                                                     # thêm part sau khi đã gộp
        ticks = load_tick_files([d])
        assert [x.last for x in ticks] == [1350.0] * 5 + [1351.0]

    def test_interrupted_compaction_not_double_counted(self, tmp_path):
        """Gộp bị ngắt (ticks.parquet đã có nhưng part chưa xoá) ⇒ đọc qua load_ticks, không đếm đôi."""
        import shutil
        d = recorder_day(tmp_path, [t(i, 1350.0, bid=1349.9, ask=1350.1) for i in range(5)])
        parts = sorted(d.glob("part-*.parquet"))
        keep = tmp_path / "keep"
        keep.mkdir()
        for p in parts:
            shutil.copy(p, keep / p.name)
        from recorder.tick_recorder import compact_day
        compact_day(d)
        for p in keep.iterdir():
            shutil.copy(p, d / p.name)                                  # part còn sót lại
        assert len(load_tick_files([d])) == 5

    def test_downloader_rows_without_session_all_kept(self, tmp_path):
        downloader_file(tmp_path / "trades" / SYM / "2026-11-09.parquet", [t(i, 1350.0 + i / 10) for i in range(4)])
        ticks = load_tick_files([tmp_path / "trades"])
        assert len(ticks) == 4 and [x.last for x in ticks] == [1350.0, 1350.1, 1350.2, 1350.3]
        assert {x.source for x in ticks} == {"dnse_history"}

    def test_recursive_root_sorted_by_time(self, tmp_path):
        downloader_file(tmp_path / SYM / "2026-11-10.parquet", [t(0, 1360.0, day=date(2026, 11, 10))])
        downloader_file(tmp_path / SYM / "2026-11-09.parquet", [t(5, 1350.0), t(1, 1349.0)])
        assert [x.last for x in load_tick_files([tmp_path])] == [1349.0, 1350.0, 1360.0]

    def test_several_sources_need_choice(self, tmp_path):
        d = recorder_day(tmp_path / "raw", [t(0, 1350.0, "dnse"), t(1, 1350.1, "ssi")])
        with pytest.raises(ValueError, match="dnse.*ssi|nguồn"):
            load_tick_files([d])
        assert [x.source for x in load_tick_files([d], source="ssi")] == ["ssi"]

    def test_source_filter_leaving_nothing_raises(self, tmp_path):
        d = recorder_day(tmp_path, [t(0, 1350.0, "dnse")])
        with pytest.raises(ValueError, match="Không có tick"):
            load_tick_files([d], source="ssi")

    def test_bar_parquet_rejected(self, tmp_path):
        import pandas as pd
        p = tmp_path / "bars" / "x.parquet"
        p.parent.mkdir()
        pd.DataFrame({"symbol": [SYM], "start": [datetime(2026, 11, 9, 10)], "open": [1.0], "high": [1.0],
                      "low": [1.0], "close": [1.0]}).to_parquet(p)
        with pytest.raises(ValueError, match="không phải file tick"):
            load_tick_files([tmp_path / "bars"])

    def test_date_range_skips_files_by_name_without_reading(self, tmp_path, monkeypatch):
        """File/thư mục tên YYYY-MM-DD ngoài khoảng ⇒ không đọc (6 tháng khớp lệnh ~14 triệu dòng, không nạp hết RAM)."""
        import backtest.ticks as bt
        downloader_file(tmp_path / SYM / "2026-11-09.parquet", [t(0, 1350.0)])
        downloader_file(tmp_path / SYM / "2026-11-10.parquet", [t(0, 1360.0, day=date(2026, 11, 10))])
        downloader_file(tmp_path / SYM / "2026-11-11.parquet", [t(0, 1370.0, day=date(2026, 11, 11))])
        recorder_day(tmp_path / "raw", [t(0, 1380.0, day=date(2026, 11, 12))], day=date(2026, 11, 12))
        read = []
        orig = bt._read_file
        monkeypatch.setattr(bt, "_read_file", lambda f: read.append(f.name) or orig(f))
        ticks = load_tick_files([tmp_path], date_from=date(2026, 11, 10), date_to=date(2026, 11, 10))
        assert [x.last for x in ticks] == [1360.0] and read == ["2026-11-10.parquet"]

    def test_date_range_keeps_undated_files_and_filters_rows(self, tmp_path):
        """Tên không phải ngày ⇒ vẫn đọc, rồi lọc từng dòng theo ngày."""
        downloader_file(tmp_path / "gop.parquet", [t(0, 1350.0), t(0, 1360.0, day=date(2026, 11, 10))])
        assert [x.last for x in load_tick_files([tmp_path], date_from=date(2026, 11, 10))] == [1360.0]

    def test_date_range_leaving_nothing_returns_empty(self, tmp_path):
        downloader_file(tmp_path / SYM / "2026-11-09.parquet", [t(0, 1350.0)])
        assert load_tick_files([tmp_path], date_from=date(2026, 12, 1)) == []

    def test_nothing_found_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Không có"):
            load_tick_files([tmp_path])


# ------------------------------------------------------------------ báo giá giả ----

class TestQuotes:
    @pytest.mark.parametrize("spread,half", [(1, 0.1), (2, 0.1), (3, 0.2), (0, 0.0)])
    def test_trades_without_quotes_get_pessimistic_quotes(self, spread, half):
        out, n = fill_missing_quotes([t(0, 1350.0)], tick_size=0.1, spread_ticks=spread)
        assert n == 1 and out[0].bid == pytest.approx(1350.0 - half) and out[0].ask == pytest.approx(1350.0 + half)
        assert out[0].last == 1350.0 and out[0].last_qty == 1

    def test_existing_quotes_untouched(self):
        x = t(0, 1350.0, bid=1349.8, ask=1350.3)
        out, n = fill_missing_quotes([x], tick_size=0.1, spread_ticks=1)
        assert n == 0 and out[0] is x

    def test_one_sided_quote_kept_and_not_counted(self):
        x = t(0, 1350.0, bid=1349.9)
        out, n = fill_missing_quotes([x], tick_size=0.1, spread_ticks=1)
        assert n == 0 and out[0].ask is None

    def test_quote_only_tick_untouched(self):
        x = Tick(SYM, datetime(2026, 11, 9, 10), bid=1349.9, ask=1350.1, source="dnse")
        out, n = fill_missing_quotes([x], tick_size=0.1, spread_ticks=1)
        assert n == 0 and out[0] is x

    def test_negative_spread_rejected(self):
        with pytest.raises(ValueError):
            fill_missing_quotes([t(0, 1350.0)], tick_size=0.1, spread_ticks=-1)


# ------------------------------------------------------------------ engine + CLI ----

class LongAt1000:
    def on_bar(self, bar, ctx):
        from core.models import Signal, SignalAction
        if bar.start.strftime("%H:%M") == "10:00" and ctx.position_qty == 0:
            return [Signal(SignalAction.ENTER_LONG, bar.start + timedelta(seconds=60), bar.symbol,
                           stop_loss=bar.close - 10, reason="test")]
        return []


def trade_stream():
    out = []
    for s in range(0, 600, 5):
        out.append(t(s, 1350.0 + (s // 60) * 0.5))
    return out


def test_trades_only_ticks_fill_at_synthetic_ask(session_cfg, cal):
    from backtest.engine import BacktestEngine, bars_from_ticks
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg)
    ticks, n = fill_missing_quotes(trade_stream(), tick_size=0.1, spread_ticks=1)
    res = BacktestEngine(s, LongAt1000(), calendar=cal).run_events(bars_from_ticks(ticks))
    entry = (res.trades or res.open_trades)[0]
    # bar 10:00 đóng lúc 10:01:00; tick sau độ trễ 300ms là 10:01:05 (last 1350.5) ⇒ ask 1350.6 + trượt 1 bước
    assert entry.entry_price == pytest.approx(1350.7)
    assert n == len(trade_stream())


def test_cli_runs_on_ticks(monkeypatch, tmp_path, session_cfg, cal):
    import backtest.engine as eng
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg)
    downloader_file(tmp_path / "trades" / SYM / "2026-11-09.parquet", trade_stream())
    monkeypatch.setattr(eng, "load_settings", lambda: s)
    orig = eng.BacktestEngine.__init__

    def init(self, st, strategy, calendar=None, trading_contract_only=True):
        orig(self, st, strategy, calendar=cal, trading_contract_only=trading_contract_only)
    monkeypatch.setattr(eng.BacktestEngine, "__init__", init)
    out = tmp_path / "r.html"
    rc = eng.main(["--ticks", str(tmp_path / "trades"), "--strategy", "tests.test_backtest_ticks:LongAt1000",
                   "--out", str(out)])
    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "báo giá giả" in html and str(len(trade_stream())) in html


def test_cli_date_range_applies_to_ticks(monkeypatch, tmp_path, session_cfg, cal):
    import backtest.engine as eng
    from tests.test_backtest_engine import bt_settings
    s = bt_settings(session_cfg)
    downloader_file(tmp_path / "trades" / SYM / "2026-11-09.parquet", trade_stream())
    monkeypatch.setattr(eng, "load_settings", lambda: s)
    seen = {}
    orig = eng.BacktestEngine.run_events

    def spy(self, events):
        evs = list(events)
        seen["n"] = len(evs)
        return orig(self, evs)
    monkeypatch.setattr(eng.BacktestEngine, "run_events", spy)
    rc = eng.main(["--ticks", str(tmp_path / "trades"), "--strategy", "tests.test_backtest_ticks:LongAt1000",
                   "--from", "2026-11-10", "--out", str(tmp_path / "r.html")])
    assert rc == 0 and seen["n"] == 0                                 # 09/11 nằm ngoài khoảng


def test_cli_bars_and_ticks_mutually_exclusive(tmp_path):
    import backtest.engine as eng
    with pytest.raises(SystemExit):
        eng.main(["--bars", str(tmp_path), "--ticks", str(tmp_path), "--strategy", "x:Y"])
