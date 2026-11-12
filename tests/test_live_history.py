"""Nối lịch sử nhiều ngày vào runner live/paper — viết TRƯỚC khi implement.

- `core.continuous.HistoryBuilder`: logic theo ngày DÙNG CHUNG cho backtest và live (bar HĐ giao dịch + ngày
  trước roll giữ bar HĐ kế tiếp ⇒ hết ngày đo chênh lệch). Backtest đã chuyển sang dùng nó.
- `live.history.load_live_history(dirs, today, days, calendar, trading_contract)`: đọc bar đã lưu
  (`data/history/ohlc1m` của downloader, `data/bars` của recorder), chỉ các phiên TRƯỚC hôm nay, dựng lịch sử
  như backtest. **Nghiêm ngặt**: phải đủ đúng N phiên giao dịch gần nhất, mọi roll trong khoảng đó phải đo được
  chênh lệch; thiếu ⇒ `HistoryUnavailable` nêu rõ ngày/HĐ cần tải ⇒ runner thoát 2, không giao dịch.
- Runner nhận `history_loader(today)`; gọi trong `startup` sau kiểm ngày giao dịch, TRƯỚC khi kết nối broker;
  cấp `ctx.history` cho chiến lược. Không có loader mà chiến lược cần lịch sử ⇒ vẫn từ chối như trước.
- `settings.live.history_bar_dirs` (danh sách, tương đối gốc repo); null ⇒ chiến lược cần lịch sử bị từ chối.
"""
import asyncio
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from core import contracts
from core.continuous import HistoryBuilder
from live.history import HistoryUnavailable, load_live_history
from tests.test_backtest_engine import bt_settings, flat
from tests.test_backtest_history import Recorder

D13, D16, D17, D18, D19, D20 = (date(2026, 11, x) for x in (13, 16, 17, 18, 19, 20))
NOV, DEC = "VN30F2611", "VN30F2612"


def tc_fn(cal):
    return lambda d: contracts.trading_contract(d, cal, 1)


def day(d, sym, px, n=4, hhmm="10:00"):
    return flat(hhmm, n, price=px, day=d, symbol=sym)


def write(path, bars):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": [b.symbol for b in bars], "start": [b.start for b in bars],
                  "open": [b.open for b in bars], "high": [b.high for b in bars], "low": [b.low for b in bars],
                  "close": [b.close for b in bars], "volume": [b.volume for b in bars], "interval_s": 60,
                  "volume_reliable": True}).to_parquet(path)


def store(root, bars):
    """Ghi như downloader: <root>/<HĐ>/<ngày>.parquet."""
    by = {}
    for b in bars:
        by.setdefault((b.symbol, b.start.date()), []).append(b)
    for (sym, d), bs in by.items():
        write(root / sym / f"{d}.parquet", bs)


def full_data():
    return (day(D13, NOV, 1340.0) + day(D16, NOV, 1350.0) + day(D17, NOV, 1350.0) + day(D18, NOV, 1350.0)
            + day(D18, DEC, 1400.0, hhmm="10:01") + day(D19, DEC, 1400.0) + day(D20, DEC, 1400.0))


# ------------------------------------------------------------------ HistoryBuilder ----

class TestBuilder:
    def test_eve_gap_and_other_contracts_ignored(self, cal):
        b = HistoryBuilder(5, tc_fn(cal), cal.next_trading_day)
        for x in day(D18, NOV, 1350.0) + day(D18, DEC, 1400.0, hhmm="10:01") + day(D18, "VN30F2703", 1500.0):
            b.observe(x)
        r = b.close_day(D18)
        assert (r.day, r.contract, r.next_contract, r.is_roll_eve) == (D18, NOV, DEC, True)
        assert r.gap is not None and r.gap.gap == 50.0 and r.n_bars == 4
        assert {x.close for x in b.view(DEC)} == {1400.0}

    def test_missing_eve_bars_reported(self, cal):
        b = HistoryBuilder(5, tc_fn(cal), cal.next_trading_day)
        for x in day(D18, NOV, 1350.0):
            b.observe(x)
        r = b.close_day(D18)
        assert r.is_roll_eve and r.gap is None
        assert b.view(DEC) == ()

    def test_normal_day_not_eve(self, cal):
        b = HistoryBuilder(5, tc_fn(cal), cal.next_trading_day)
        for x in day(D16, NOV, 1350.0):
            b.observe(x)
        r = b.close_day(D16)
        assert not r.is_roll_eve and r.gap is None and r.n_bars == 4

    def test_empty_day_closed_without_bars(self, cal):
        b = HistoryBuilder(5, tc_fn(cal), cal.next_trading_day)
        r = b.close_day(D16)
        assert r.n_bars == 0 and b.view(NOV) == ()


# ------------------------------------------------------------------ load_live_history ----

class TestLoader:
    def test_same_history_as_backtest(self, tmp_path, cal, session_cfg):
        """Cùng dữ liệu ⇒ lịch sử live ngày 20/11 GIỐNG HỆT lịch sử backtest cấp cho chiến lược ngày 20/11."""
        data = full_data()
        store(tmp_path / "ohlc1m", data)
        live = load_live_history([tmp_path / "ohlc1m"], D20, 4, cal, tc_fn(cal))
        strat = Recorder(history_days=4)
        BacktestEngine(bt_settings(session_cfg), strat, calendar=cal).run_bars(sorted(data, key=lambda b: (b.start, b.symbol)))
        assert live.view(DEC) == strat.seen[D20]
        assert [b.start.date() for b in live.view(DEC)] == [D16] * 4 + [D17] * 4 + [D18] * 4 + [D19] * 4

    def test_today_on_disk_is_excluded(self, tmp_path, cal):
        store(tmp_path, full_data())
        h = load_live_history([tmp_path], D20, 2, cal, tc_fn(cal))
        assert D20 not in {b.start.date() for b in h.view(DEC)}

    def test_roll_day_uses_old_contract_shifted(self, tmp_path, cal):
        store(tmp_path, full_data())
        h = load_live_history([tmp_path], D19, 2, cal, tc_fn(cal))       # hôm nay = ngày roll
        assert [(b.start.date(), b.symbol, b.close) for b in h.view(DEC)][::4] == [(D17, DEC, 1400.0), (D18, DEC, 1400.0)]

    def test_missing_day_refused_with_dates(self, tmp_path, cal):
        store(tmp_path, [b for b in full_data() if b.start.date() != D17])
        with pytest.raises(HistoryUnavailable) as e:
            load_live_history([tmp_path], D20, 4, cal, tc_fn(cal))
        assert f"{D17}: thiếu bar {NOV}" in str(e.value) and "download_history" in str(e.value)

    def test_missing_roll_gap_refused_naming_contract(self, tmp_path, cal):
        store(tmp_path, [b for b in full_data() if not (b.symbol == DEC and b.start.date() == D18)])
        with pytest.raises(HistoryUnavailable) as e:
            load_live_history([tmp_path], D20, 4, cal, tc_fn(cal))
        msg = str(e.value)
        assert str(D18) in msg and DEC in msg

    def test_roll_before_window_needs_no_gap(self, tmp_path, cal):
        """Cửa sổ 1 phiên (19/11) nằm sau roll: không cần chênh lệch của 18/11."""
        store(tmp_path, [b for b in full_data() if not (b.symbol == DEC and b.start.date() == D18)])
        h = load_live_history([tmp_path], D20, 1, cal, tc_fn(cal))
        assert [b.start.date() for b in h.view(DEC)] == [D19] * 4

    def test_window_skips_weekend(self, tmp_path, cal):
        store(tmp_path, full_data())
        h = load_live_history([tmp_path], D16, 1, cal, tc_fn(cal))       # thứ Hai ⇒ phiên trước = thứ Sáu 13/11
        assert {b.start.date() for b in h.view(NOV)} == {D13}

    def test_several_dirs_merged(self, tmp_path, cal):
        data = full_data()
        store(tmp_path / "history", [b for b in data if b.start.date() < D19])
        write(tmp_path / "bars" / f"{D19}.parquet", [b for b in data if b.start.date() == D19])   # kiểu recorder
        h = load_live_history([tmp_path / "history", tmp_path / "bars"], D20, 4, cal, tc_fn(cal))
        assert len(h.view(DEC)) == 16

    def test_no_data_dirs_refused(self, tmp_path, cal):
        with pytest.raises(HistoryUnavailable):
            load_live_history([tmp_path / "khong-co"], D20, 2, cal, tc_fn(cal))

    def test_existing_but_empty_or_wrong_dir_refused(self, tmp_path, cal):
        (tmp_path / "rong").mkdir()
        with pytest.raises(HistoryUnavailable):
            load_live_history([tmp_path / "rong"], D20, 2, cal, tc_fn(cal))
        bad = tmp_path / "trades" / "x.parquet"
        bad.parent.mkdir()
        pd.DataFrame({"symbol": [NOV], "ts": [datetime(2026, 11, 19, 10)]}).to_parquet(bad)
        with pytest.raises(HistoryUnavailable, match="không phải file bar"):
            load_live_history([tmp_path / "trades"], D20, 2, cal, tc_fn(cal))

    def test_days_must_be_positive(self, tmp_path, cal):
        with pytest.raises(ValueError):
            load_live_history([tmp_path], D20, 0, cal, tc_fn(cal))


# ------------------------------------------------------------------ runner ----

class HistStrategy:
    history_days = 2

    def __init__(self):
        self.seen = []

    def on_bar(self, bar, ctx):
        self.seen.append(ctx.history)
        return []


def rig_with(session_cfg, cal, tmp_path, loader):
    from tests.test_runner import Rig
    r = Rig(session_cfg, cal, tmp_path)
    r.runner.strategy = HistStrategy()
    r.runner.history_loader = loader
    connected = []
    orig = r.broker.connect

    async def spy():
        connected.append(1)
        return await orig()
    r.broker.connect = spy
    return r, connected


def test_runner_feeds_history_to_strategy(session_cfg, cal, tmp_path):
    from tests.test_runner import SYM, T0
    hist_bars = day(date(2026, 11, 5), SYM, 1300.0) + day(date(2026, 11, 6), SYM, 1310.0)
    calls = []

    def loader(today):
        calls.append(today)
        b = HistoryBuilder(2, tc_fn(cal), cal.next_trading_day)
        for d in (date(2026, 11, 5), date(2026, 11, 6)):
            for x in hist_bars:
                if x.start.date() == d:
                    b.observe(x)
            b.close_day(d)
        return b.history
    r, _ = rig_with(session_cfg, cal, tmp_path, loader)
    assert r.start() is None
    assert calls == [T0.date()]
    r.at(T0 + timedelta(seconds=5))
    r.trade(1350.0)
    r.step()
    r.at(T0 + timedelta(seconds=61))
    r.step()                                                      # bar 10:00 đóng
    assert r.runner.strategy.seen and r.runner.strategy.seen[-1] == tuple(hist_bars)


def test_runner_refuses_when_loader_fails_before_brokers(session_cfg, cal, tmp_path):
    def loader(today):
        raise HistoryUnavailable("thiếu 2026-11-06 — chạy download_history")
    r, connected = rig_with(session_cfg, cal, tmp_path, loader)
    assert r.start() == 2 and not connected
    assert any("2026-11-06" in n for n in r.notes)


def test_runner_unexpected_loader_error_also_refuses(session_cfg, cal, tmp_path):
    def loader(today):
        raise OSError("đĩa hỏng")
    r, connected = rig_with(session_cfg, cal, tmp_path, loader)
    assert r.start() == 2 and not connected


def test_runner_without_history_need_never_calls_loader(session_cfg, cal, tmp_path):
    from tests.test_runner import Rig
    r = Rig(session_cfg, cal, tmp_path)
    called = []
    r.runner.history_loader = lambda today: called.append(today)
    assert r.start() is None and called == []


def test_runner_loader_not_called_on_non_trading_day(session_cfg, cal, tmp_path):
    from tests.test_runner import Rig
    r = Rig(session_cfg, cal, tmp_path, start=datetime(2026, 11, 7, 10, 0))
    r.runner.strategy = HistStrategy()
    called = []
    r.runner.history_loader = lambda today: called.append(today)
    assert r.start() == 0 and called == []


# ------------------------------------------------------------------ main ----

def test_main_passes_history_loader_from_settings(monkeypatch, tmp_path, cal):
    from live import runner as R
    from core.settings import load_settings, Settings
    import copy
    s = load_settings()
    d = copy.deepcopy(s._data)
    d["paths"].update(data_dir=str(tmp_path / "data"), state_dir=str(tmp_path / "state"),
                      log_dir=str(tmp_path / "logs"))
    d["live"]["history_bar_dirs"] = [str(tmp_path / "hist")]
    d["live"]["allowed_machine"] = "host"
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
    loader = captured["history_loader"]
    store(tmp_path / "hist", full_data())
    h = loader(D20)                                                # nạp đúng thư mục trong settings
    assert len(h.view(DEC)) == 8


def test_main_refuses_history_strategy_when_dirs_null(monkeypatch, tmp_path):
    from live import runner as R
    from core.settings import load_settings, Settings
    import copy
    d = copy.deepcopy(load_settings()._data)
    d["paths"].update(data_dir=str(tmp_path / "data"), state_dir=str(tmp_path / "state"),
                      log_dir=str(tmp_path / "logs"))
    d["live"]["history_bar_dirs"] = None
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
    monkeypatch.setattr(R, "Runner", lambda **kw: pytest.fail("không được tạo runner"))
    assert R.main(["--assume-computed-contract", "--strategy", "tests.test_live_history:HistStrategy"]) == 2
