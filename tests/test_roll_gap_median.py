"""Chênh lệch roll = TRUNG VỊ của K phút chung cuối (thay cho một phút) — viết TRƯỚC khi implement.

- `measure_gap(old, new, minutes=K)`: hiệu (giá đóng HĐ mới − HĐ cũ) ở từng phút CHUNG; lấy K phút chung cuối
  cùng (ít hơn K thì dùng hết số có); trả trung vị + `n_minutes`. `minutes=1` ⇒ y hệt cách cũ.
- Một phút méo (ATC, lệnh lớn ở HĐ thanh khoản mỏng) không còn làm lệch cả chuỗi lịch sử.
- K đặt ở `contract.roll_gap_median_minutes`; backtest (engine) và live (`live/history.py`) đọc CÙNG khoá ⇒ cùng
  chuỗi. Chỉ bắt buộc khi chiến lược cần lịch sử; null ⇒ từ chối.
"""
import copy
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from core import contracts
from core.continuous import HistoryBuilder, RollGap, measure_gap
from core.models import Bar
from core.settings import MissingSetting
from tests.test_backtest_engine import bt_settings, flat
from tests.test_backtest_history import Recorder

NOV, DEC = "VN30F2611", "VN30F2612"
D18, D19, D20 = date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)


def minute_bars(d, sym, closes, hhmm="14:00"):
    t0 = datetime.combine(d, time.fromisoformat(hhmm))
    return [Bar(sym, t0 + timedelta(minutes=i), c, c, c, c, 5) for i, c in enumerate(closes)]


def eve_pair(spike=True):
    """2611 phẳng 1350; 2612 = 1400 (chênh 50) nhưng phút cuối giật lên 1430 (chênh 80)."""
    old = minute_bars(D18, NOV, [1350.0] * 6)
    new = minute_bars(D18, DEC, [1400.0] * 5 + [1430.0 if spike else 1400.0])
    return old, new


class TestMeasure:
    def test_median_ignores_one_distorted_minute(self):
        old, new = eve_pair()
        g = measure_gap(old, new, minutes=5)
        assert g.gap == 50.0 and g.n_minutes == 5
        assert g.at == datetime(2026, 11, 18, 14, 5)                  # phút chung cuối
        assert measure_gap(old, new, minutes=1).gap == 80.0            # cách cũ: dính phút méo

    def test_default_is_single_minute_like_before(self):
        old, new = eve_pair()
        g = measure_gap(old, new)
        assert g.gap == 80.0 and g.n_minutes == 1

    def test_fewer_common_minutes_than_k_uses_all(self):
        old = minute_bars(D18, NOV, [1350.0] * 3)
        new = minute_bars(D18, DEC, [1398.0, 1400.0, 1460.0])
        g = measure_gap(old, new, minutes=15)
        assert g.n_minutes == 3 and g.gap == 50.0

    def test_only_common_minutes_counted(self):
        old = minute_bars(D18, NOV, [1350.0] * 6)
        new = [b for i, b in enumerate(minute_bars(D18, DEC, [1400.0, 1499.0, 1400.0, 1499.0, 1400.0, 1400.0]))
               if i not in (1, 3)]                                    # 2612 thiếu phút 14:01, 14:03
        g = measure_gap(old, new, minutes=4)
        assert g.n_minutes == 4 and g.gap == 50.0

    def test_last_k_common_minutes_not_first(self):
        old = minute_bars(D18, NOV, [1350.0] * 8)
        new = minute_bars(D18, DEC, [1300.0] * 4 + [1400.0] * 4)
        assert measure_gap(old, new, minutes=4).gap == 50.0

    def test_even_count_median_is_mean_of_middle_two(self):
        old = minute_bars(D18, NOV, [1350.0] * 4)
        new = minute_bars(D18, DEC, [1390.0, 1399.0, 1401.0, 1410.0])     # hiệu 40, 49, 51, 60 ⇒ (49+51)/2
        assert measure_gap(old, new, minutes=4).gap == 50.0

    @pytest.mark.parametrize("bad", [0, -1, 2.5, True])
    def test_invalid_minutes(self, bad):
        old, new = eve_pair()
        with pytest.raises(ValueError):
            measure_gap(old, new, minutes=bad)


def test_builder_uses_gap_minutes(cal):
    b = HistoryBuilder(3, lambda d: contracts.trading_contract(d, cal, 1), cal.next_trading_day, gap_minutes=5)
    old, new = eve_pair()
    for x in old + new:
        b.observe(x)
    r = b.close_day(D18)
    assert r.gap.gap == 50.0 and r.gap.n_minutes == 5
    assert {x.close for x in b.view(DEC)} == {1400.0}


# ------------------------------------------------------------------ backtest + live đọc cùng khoá ----

def data():
    old, new = eve_pair()
    return old + new + minute_bars(D19, DEC, [1400.0] * 3) + minute_bars(D20, DEC, [1400.0] * 3)


def engine_history(settings, cal):
    strat = Recorder(history_days=2)
    res = BacktestEngine(settings, strat, calendar=cal).run_bars(sorted(data(), key=lambda b: (b.start, b.symbol)))
    return strat.seen[D19], res


def test_engine_reads_setting(session_cfg, cal):
    s = bt_settings(session_cfg)
    s._data["contract"]["roll_gap_median_minutes"] = 5
    hist, res = engine_history(s, cal)
    assert {b.close for b in hist} == {1400.0}                         # 1350 + 50 (không phải +80)
    assert res.data_quality["roll_gaps"] == [f"{D18}: {NOV} → {DEC} +50.0 điểm (trung vị 5 phút)"]
    assert "trung vị 5 phút" in res.assumptions["history"]


def test_engine_requires_setting_only_with_history(session_cfg, cal):
    s = bt_settings(session_cfg)
    s._data["contract"]["roll_gap_median_minutes"] = None
    with pytest.raises(MissingSetting):
        BacktestEngine(s, Recorder(history_days=2), calendar=cal)
    BacktestEngine(s, Recorder(history_days=0), calendar=cal)        # không cần lịch sử ⇒ không đòi


@pytest.mark.parametrize("bad", [0, 2.5])
def test_engine_rejects_invalid_setting(session_cfg, cal, bad):
    s = bt_settings(session_cfg)
    s._data["contract"]["roll_gap_median_minutes"] = bad
    with pytest.raises(ValueError):
        BacktestEngine(s, Recorder(history_days=2), calendar=cal)


def store(root, bars):
    by = {}
    for b in bars:
        by.setdefault((b.symbol, b.start.date()), []).append(b)
    for (sym, d), bs in by.items():
        p = root / sym / f"{d}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"symbol": [x.symbol for x in bs], "start": [x.start for x in bs],
                      "open": [x.open for x in bs], "high": [x.high for x in bs], "low": [x.low for x in bs],
                      "close": [x.close for x in bs], "volume": [x.volume for x in bs], "interval_s": 60,
                      "volume_reliable": True}).to_parquet(p)


def test_live_loader_matches_engine(tmp_path, session_cfg, cal):
    from live.history import load_live_history
    store(tmp_path, data())
    s = bt_settings(session_cfg)
    s._data["contract"]["roll_gap_median_minutes"] = 5
    hist, _ = engine_history(s, cal)
    live = load_live_history([tmp_path], D19, 1, cal, lambda d: contracts.trading_contract(d, cal, 1), gap_minutes=5)
    assert live.view(DEC) == hist and {b.close for b in hist} == {1400.0}


def test_runner_main_passes_setting_to_loader(monkeypatch, tmp_path, cal):
    from core.settings import Settings, load_settings
    from live import runner as R
    d = copy.deepcopy(load_settings()._data)
    d["paths"].update(data_dir=str(tmp_path / "data"), state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    d["live"]["history_bar_dirs"] = [str(tmp_path / "hist")]
    d["contract"]["roll_gap_median_minutes"] = 5
    for k in ("max_daily_loss_points", "max_trades_per_day", "max_consecutive_losses"):
        d["risk"][k] = d["risk"].get(k) or 3
    settings = Settings(d)
    monkeypatch.setattr("core.settings.load_settings", lambda *a, **k: settings)
    monkeypatch.setattr("live.instance_lock.check_allowed_machine", lambda allowed: "host")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("DNSE_API_KEY", "k")
    monkeypatch.setenv("DNSE_API_SECRET", "s")
    monkeypatch.setattr("brokers.paper.PaperBroker.from_settings", classmethod(lambda cls, *a, **k: object()))
    monkeypatch.setattr("live.wiring.build_paper_feed", lambda *a, **k: (object(), []))
    captured = {}

    class FakeRunner:
        def __init__(self, **kw):
            captured.update(kw)

        def queue_alert(self, t):
            pass

        async def run(self):
            return 0
    monkeypatch.setattr(R, "Runner", FakeRunner)
    assert R.main(["--assume-computed-contract", "--strategy", "tests.test_live_history:HistStrategy"]) == 0
    store(tmp_path / "hist", data())
    h = captured["history_loader"](D20)                               # HistStrategy: 2 phiên = 18/11, 19/11
    v = h.view(DEC)
    assert {b.close for b in v if b.start.date() == D18} == {1400.0}  # +50 (trung vị 5′), không phải +80


def test_repo_setting_present():
    from core.settings import load_settings
    v = load_settings().get("contract.roll_gap_median_minutes")
    assert isinstance(v, int) and v >= 1
