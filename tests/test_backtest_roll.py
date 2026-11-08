"""Backtest nối chuỗi qua roll — viết TRƯỚC khi implement.

Bot intraday, không giữ vị thế qua đêm ⇒ "chuỗi liên tục" = mỗi ngày dùng ĐÚNG hợp đồng bot giao dịch
(`core.contracts.trading_contract`, cùng hàm live dùng), KHÔNG back-adjust giá (P&L tính trên giá thật
của từng hợp đồng). Việc cần làm:
- `load_bars_parquet` đọc đệ quy `data/history/ohlc1m/<hợp đồng>/<ngày>.parquet` (+ nhiều đường dẫn),
  bar trùng (symbol, start) giữa các nguồn ⇒ giữ bản volume tin được; file không phải bar ⇒ lỗi rõ ràng;
- engine báo ngày roll, số bar hợp đồng khác bị bỏ, ngày có dữ liệu nhưng thiếu hợp đồng giao dịch, và
  ngày giao dịch không có dữ liệu nào.

Hợp đồng tháng 11/2026 đáo hạn thứ Năm 19/11; `roll_days_before_expiry = 1` ⇒ 19/11 bot đã giao dịch 2612.
"""
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from backtest.engine import BacktestEngine, load_bars_parquet
from core.models import Bar
from tests.test_backtest_engine import Script, bt_settings, flat

D18, D19, D20, D23 = date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23)
NOV, DEC = "VN30F2611", "VN30F2612"


def write(path, bars, reliable=True, source="dnse_history"):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": [b.symbol for b in bars], "start": [b.start for b in bars],
                  "open": [b.open for b in bars], "high": [b.high for b in bars], "low": [b.low for b in bars],
                  "close": [b.close for b in bars], "volume": [b.volume for b in bars], "interval_s": 60,
                  "volume_reliable": reliable, "source": source}).to_parquet(path)


def day_bars(d, sym, price, n=6):
    return flat("10:00", n, price=price, day=d, symbol=sym)


def run(settings, cal, strat, bars):
    bars = sorted(bars, key=lambda b: (b.start, b.symbol))
    return BacktestEngine(settings, strat, calendar=cal).run_bars(bars)


@pytest.fixture
def settings(session_cfg):
    return bt_settings(session_cfg, max_trades_per_day=10)


# ------------------------------------------------------------------ đọc dữ liệu ----

class TestLoader:
    def test_reads_contract_subfolders_recursively(self, tmp_path):
        root = tmp_path / "ohlc1m"
        write(root / NOV / "2026-11-18.parquet", day_bars(D18, NOV, 1350.0, 2))
        write(root / DEC / "2026-11-19.parquet", day_bars(D19, DEC, 1400.0, 2))
        bars, skipped = load_bars_parquet(root)
        assert [(b.symbol, b.start.date()) for b in bars] == [(NOV, D18)] * 2 + [(DEC, D19)] * 2
        assert skipped == 0

    def test_several_paths_and_dedupe_prefers_reliable_volume(self, tmp_path):
        imported = tmp_path / "imported" / "bars.parquet"
        hist = tmp_path / "history" / NOV / "2026-11-18.parquet"
        a = day_bars(D18, NOV, 1350.0, 3)
        b = [Bar(x.symbol, x.start, x.open + 0.5, x.high + 0.5, x.low + 0.5, x.close + 0.5, 99) for x in a[:2]]
        write(imported, a, reliable=False, source="dashboard")
        write(hist, b, reliable=True)
        stats = {}
        for order in ([imported, hist], [hist, imported]):          # thứ tự đường dẫn không đổi kết quả
            bars, _ = load_bars_parquet([tmp_path / "imported", tmp_path / "history"] if order[0] == imported
                                        else [tmp_path / "history", tmp_path / "imported"], stats=stats)
            assert len(bars) == 3
            assert [x.volume_reliable for x in bars] == [True, True, False]
            assert bars[0].open == 1350.5 and bars[0].volume == 99
        assert stats["duplicates"] == 2 and stats["conflicts"] == 2 and stats["files"] == 2

    def test_identical_duplicates_are_not_conflicts(self, tmp_path):
        a = day_bars(D18, NOV, 1350.0, 2)
        write(tmp_path / "x" / "a.parquet", a)
        write(tmp_path / "y" / "a.parquet", a)
        stats = {}
        bars, _ = load_bars_parquet(tmp_path, stats=stats)
        assert len(bars) == 2 and stats["duplicates"] == 2 and stats["conflicts"] == 0

    def test_two_reliable_sources_first_path_wins(self, tmp_path):
        a = day_bars(D18, NOV, 1350.0, 1)
        b = [Bar(NOV, a[0].start, 1351.0, 1351.0, 1351.0, 1351.0, 7)]
        write(tmp_path / "a" / "x.parquet", a)
        write(tmp_path / "b" / "x.parquet", b)
        assert load_bars_parquet([tmp_path / "a", tmp_path / "b"])[0][0].open == 1350.0
        assert load_bars_parquet([tmp_path / "b", tmp_path / "a"])[0][0].open == 1351.0

    def test_non_bar_parquet_rejected_with_file_name(self, tmp_path):
        write(tmp_path / "ohlc1m" / NOV / "2026-11-18.parquet", day_bars(D18, NOV, 1350.0, 2))
        bad = tmp_path / "trades" / NOV / "2026-11-18.parquet"
        bad.parent.mkdir(parents=True)
        pd.DataFrame({"symbol": [NOV], "ts": [datetime(2026, 11, 18, 10)], "last": [1350.0]}).to_parquet(bad)
        with pytest.raises(ValueError, match="trades.*2026-11-18.parquet|không phải bar"):
            load_bars_parquet(tmp_path)

    def test_temp_files_ignored(self, tmp_path):
        write(tmp_path / NOV / "2026-11-18.parquet", day_bars(D18, NOV, 1350.0, 2))
        (tmp_path / NOV / "2026-11-19.parquet.123.140.tmp").write_bytes(b"dang ghi")
        bars, _ = load_bars_parquet(tmp_path)
        assert len(bars) == 2

    def test_empty_folder_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Không có file"):
            load_bars_parquet(tmp_path)


# ------------------------------------------------------------------ engine qua roll ----

class TestRoll:
    def test_strategy_follows_trading_contract_across_roll(self, settings, cal):
        strat = Script({})
        bars = []
        for d in (D18, D19, D20):
            bars += day_bars(d, NOV, 1350.0) + day_bars(d, DEC, 1400.0)
        res = run(settings, cal, strat, bars)
        seen = {(b.start.date(), b.symbol) for b, _ in strat.seen}
        assert seen == {(D18, NOV), (D19, DEC), (D20, DEC)}
        q = res.data_quality
        assert q["rolls"] == [f"{D19}: {NOV} → {DEC}"]
        assert q["other_contract_bars"] == 18
        assert q["days_missing_trading_contract"] == 0 and q["missing_trading_days"] == 0

    def test_trades_on_both_sides_of_roll_use_real_prices(self, settings, cal):
        plan = {"10:00": ("long", 1300.0), "10:03": ("exit",)}
        # 19/11 là ngày đáo hạn ⇒ risk chặn mở vị thế (trade_on_expiry_day=False) ⇒ lệnh 2612 ở 20/11
        bars = day_bars(D18, NOV, 1350.0) + day_bars(D19, DEC, 1400.0) + day_bars(D20, DEC, 1400.0)
        res = run(settings, cal, Script(plan), bars)
        assert [(t.symbol, t.entry_ts.date()) for t in res.trades] == [(NOV, D18), (DEC, D20)]
        # giá đi ngang ⇒ mỗi lệnh chỉ lỗ spread + trượt + phí, không dính khoảng chênh 50 điểm giữa hai HĐ
        assert all(-2.0 < t.pnl_points < 0 for t in res.trades)
        assert res.trades[1].entry_price > 1399 and res.trades[0].entry_price < 1351

    def test_day_with_only_old_contract_is_flagged_not_traded(self, settings, cal):
        strat = Script({"10:00": ("long", 1300.0), "10:03": ("exit",)})
        bars = day_bars(D18, NOV, 1350.0) + day_bars(D19, NOV, 1350.0) + day_bars(D20, DEC, 1400.0)
        res = run(settings, cal, strat, bars)
        assert {b.start.date() for b, _ in strat.seen} == {D18, D20}
        assert res.data_quality["days_missing_trading_contract"] == 1
        w = [x for x in res.warnings if str(D19) in x]
        assert w and DEC in w[0] and NOV in w[0]

    def test_trading_day_without_any_data_counted(self, settings, cal):
        bars = day_bars(D18, NOV, 1350.0) + day_bars(D20, DEC, 1400.0)
        res = run(settings, cal, Script({}), bars)
        assert res.data_quality["missing_trading_days"] == 1
        assert any(str(D19) in x and "không có dữ liệu" in x for x in res.warnings)

    def test_weekend_and_holiday_gaps_not_counted(self, settings, cal):
        bars = day_bars(D20, DEC, 1400.0) + day_bars(D23, DEC, 1400.0)      # thứ Sáu → thứ Hai
        res = run(settings, cal, Script({}), bars)
        assert res.data_quality["missing_trading_days"] == 0
        h = day_bars(date(2026, 4, 29), "VN30F2605", 1300.0) + day_bars(date(2026, 5, 4), "VN30F2605", 1300.0)
        res = run(settings, cal, Script({}), h)                               # 30/4, 1/5 nghỉ
        assert res.data_quality["missing_trading_days"] == 0

    def test_tick_mode_counts_other_contract_bars_not_ticks(self, settings, cal):
        from backtest.engine import bars_from_ticks
        from core.models import Tick
        ticks = []
        for sym, px in ((NOV, 1350.0), (DEC, 1400.0)):
            for sec in (5, 40, 70, 100):                                   # 4 tick ⇒ 2 bar mỗi HĐ
                ticks.append(Tick(sym, datetime.combine(D20, time(10, 0)) + timedelta(seconds=sec),
                                  last=px, last_qty=1, bid=px - 0.1, ask=px + 0.1))
        ticks.sort(key=lambda t: (t.ts, t.symbol))
        res = BacktestEngine(settings, Script({}), calendar=cal).run_events(bars_from_ticks(ticks))
        assert res.data_quality["other_contract_bars"] == 2

    def test_all_contracts_mode_reports_no_rolls_across_days(self, settings, cal):
        bars = day_bars(D18, NOV, 1350.0) + day_bars(D19, DEC, 1400.0)
        res = BacktestEngine(settings, Script({}), calendar=cal, trading_contract_only=False).run_bars(bars)
        assert res.data_quality["rolls"] == []

    def test_all_contracts_mode_skips_contract_checks(self, settings, cal):
        # tick giả của hai HĐ cùng phút không xen kẽ được ⇒ đặt hai HĐ ở hai khung giờ khác nhau
        bars = day_bars(D19, NOV, 1350.0) + flat("11:00", 6, price=1400.0, day=D19, symbol=DEC)
        strat = Script({})
        res = BacktestEngine(settings, strat, calendar=cal, trading_contract_only=False).run_bars(bars)
        assert {b.symbol for b, _ in strat.seen} == {NOV, DEC}
        assert res.data_quality["rolls"] == [] and res.data_quality["other_contract_bars"] == 0


# ------------------------------------------------------------------ CLI ----

class Noop:
    def on_bar(self, bar, ctx):
        return []


def test_cli_accepts_several_bar_paths(tmp_path, monkeypatch, settings, cal, capsys):
    import backtest.engine as eng
    write(tmp_path / "h" / NOV / "2026-11-18.parquet", day_bars(D18, NOV, 1350.0))
    write(tmp_path / "h" / DEC / "2026-11-19.parquet", day_bars(D19, DEC, 1400.0))
    write(tmp_path / "imp" / "x.parquet", day_bars(D18, NOV, 1350.0), reliable=False)
    monkeypatch.setattr(eng, "load_settings", lambda: settings)
    orig = eng.BacktestEngine.__init__

    def init(self, s, strategy, calendar=None, trading_contract_only=True):
        orig(self, s, strategy, calendar=cal, trading_contract_only=trading_contract_only)
    monkeypatch.setattr(eng.BacktestEngine, "__init__", init)
    out = tmp_path / "r.html"
    rc = eng.main(["--bars", str(tmp_path / "h"), str(tmp_path / "imp"), "--strategy",
                   "tests.test_backtest_roll:Noop", "--out", str(out)])
    assert rc == 0 and out.exists()
    html = out.read_text(encoding="utf-8")
    assert f"{D19}: {NOV} → {DEC}" in html
    assert "trùng" in html                                   # cảnh báo bar trùng giữa các nguồn
