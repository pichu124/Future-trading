"""`backtest.robustness` trên tick thật (`--ticks`) — viết TRƯỚC khi implement.

- `run_robustness` nhận tick (thay cho bar): mỗi lần chạy = `run_events(bars_from_ticks(tick))`; chia IS/OOS,
  walk-forward, độ nhạy (chỉ IS) theo NGÀY của tick — y hệt chế độ bar.
- CLI `--ticks` (loại trừ với `--bars`, `--source` khi nhiều nguồn): khớp lệnh tải từ DNSE không có báo giá ⇒
  gắn bid/ask giả (`fill_missing_quotes`) TRƯỚC khi chạy, và ghi chú vào kết quả.
"""
from datetime import timedelta

import pytest

from backtest.engine import BacktestEngine, synthetic_ticks
from backtest.robustness import ORB_PARAMS, run_robustness
from core.models import Tick
from tests.test_robustness import dataset, make_orb, orb_settings


def ticks_from_bars(bars, spread=1):
    out = []
    for b in bars:
        out += synthetic_ticks(b, 0, 0.1, spread)
    out.sort(key=lambda t: t.ts)
    return out


@pytest.fixture(scope="module")
def tick_result():
    from datetime import date
    from core.calendar import TradingCalendar
    cal = TradingCalendar.from_dates([date(2026, 9, 2), date(2027, 1, 1)])
    cfg = {"pre_open_start": "08:30", "ato_start": "08:45", "continuous_am_start": "09:00",
           "lunch_start": "11:30", "continuous_pm_start": "13:00", "atc_start": "14:30", "close": "14:45"}
    bars = dataset(cal, 12, planted=True, seed=1)
    ticks = ticks_from_bars(bars)
    split = sorted({t.ts.date() for t in ticks})[6]
    calls = {"events": 0, "bars": 0}
    orig_e, orig_b = BacktestEngine.run_events, BacktestEngine.run_bars

    def spy_e(self, ev):
        calls["events"] += 1
        return orig_e(self, ev)

    def spy_b(self, b):
        calls["bars"] += 1
        return orig_b(self, b)
    BacktestEngine.run_events, BacktestEngine.run_bars = spy_e, spy_b
    try:
        rob = run_robustness(orb_settings(cfg), make_orb, ticks, split, ORB_PARAMS[:1], folds=3,
                             n_trials=1, calendar=cal)
    finally:
        BacktestEngine.run_events, BacktestEngine.run_bars = orig_e, orig_b
    return rob, split, calls


def test_tick_mode_uses_run_events_only(tick_result):
    rob, split, calls = tick_result
    assert calls["bars"] == 0 and calls["events"] == 1 + 1 + 2   # gốc + trượt giá + 2 biến thể độ nhạy


def test_tick_mode_splits_by_tick_date(tick_result):
    rob, split, _ = tick_result
    assert rob["base"].data_quality["synthetic_ticks"] is False   # khớp trên tick đưa vào, không dựng tick giả
    assert all(t.entry_ts.date() >= split for t in rob["sections"]["out_of_sample"]["trades"])
    assert all(t.entry_ts.date() < split for v in rob["sensitivity"] for t in v["trades"])
    assert len(rob["folds"]) == 3
    assert rob["base"].trades                                     # có lệnh thật trên tick


def test_tick_split_date_must_be_inside_data(tick_result):
    rob, split, _ = tick_result
    ticks = [Tick("VN30F2611", rob["base"].bar_log[0][0], last=1350.0, bid=1349.9, ask=1350.1)]
    with pytest.raises(ValueError, match="split_date"):
        run_robustness(orb_settings({"continuous_am_start": "09:00"}), make_orb, ticks, split, ORB_PARAMS[:1])


def test_cli_ticks_fills_quotes_and_passes_ticks(monkeypatch, tmp_path):
    import backtest.robustness as R
    from tests.test_backtest_ticks import downloader_file, trade_stream, SYM
    from tests.test_strategy_orb_filter import settings as orb_s
    s = orb_s()
    s._data.setdefault("backtest", {})["synthetic_spread_ticks"] = 1
    downloader_file(tmp_path / "trades" / SYM / "2026-11-09.parquet", trade_stream())
    monkeypatch.setattr(R, "load_settings", lambda: s)
    monkeypatch.setattr(R, "BacktestEngine", lambda *a, **k: None)
    captured = {}

    def fake_run(settings, make, data, split, params, **kw):
        captured["data"] = data
        raise SystemExit(0)
    monkeypatch.setattr(R, "run_robustness", fake_run)
    with pytest.raises(SystemExit):
        R.main(["--ticks", str(tmp_path / "trades"), "--split", "2026-11-09", "--n-trials", "2"])
    data = captured["data"]
    assert data and all(isinstance(t, Tick) for t in data)
    assert all(t.bid is not None and t.ask is not None for t in data)       # báo giá giả đã gắn


def test_cli_bars_and_ticks_exclusive(tmp_path):
    import backtest.robustness as R
    with pytest.raises(SystemExit):
        R.main(["--bars", str(tmp_path), "--ticks", str(tmp_path), "--split", "2026-11-09"])


def test_cli_source_filter_passed(monkeypatch, tmp_path):
    import backtest.robustness as R
    from tests.test_backtest_ticks import recorder_day, t
    from tests.test_strategy_orb_filter import settings as orb_s
    s = orb_s()
    s._data.setdefault("backtest", {})["synthetic_spread_ticks"] = 1
    d = recorder_day(tmp_path / "raw", [t(0, 1350.0, "dnse", bid=1349.9, ask=1350.1),
                                        t(1, 1350.1, "ssi", bid=1350.0, ask=1350.2)])
    monkeypatch.setattr(R, "load_settings", lambda: s)
    monkeypatch.setattr(R, "BacktestEngine", lambda *a, **k: None)
    captured = {}

    def fake_run(settings, make, data, split, params, **kw):
        captured["data"] = data
        raise SystemExit(0)
    monkeypatch.setattr(R, "run_robustness", fake_run)
    with pytest.raises(SystemExit):
        R.main(["--ticks", str(d), "--source", "ssi", "--split", "2026-11-09"])
    assert {x.source for x in captured["data"]} == {"ssi"}
    assert R.main(["--ticks", str(d), "--split", "2026-11-09"]) == 2         # nhiều nguồn, không chọn ⇒ thoát 2
