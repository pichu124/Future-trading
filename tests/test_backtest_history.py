"""Backtest cấp lịch sử nhiều ngày đã back-adjust cho chiến lược (`StrategyContext.history`) — viết TRƯỚC.

Chiến lược khai báo `history_days = N` (mặc định 0 ⇒ `ctx.history == ()` như cũ). Engine:
- lưu bar đã đóng của HĐ giao dịch từng ngày; ngày trước roll giữ thêm bar HĐ kế tiếp để đo chênh lệch
  (phút chung cuối cùng) lúc HẾT ngày đó ⇒ chỉ dùng từ phiên sau (không look-ahead);
- `ctx.history` = N phiên gần nhất TRƯỚC hôm nay, quy về giá HĐ hiện tại, cũ nhất trước;
- không đo được chênh lệch ⇒ lịch sử bị cắt tại roll + cảnh báo (`roll_gaps_missing`).
Live/paper: runner TỪ CHỐI chiến lược có `history_days > 0` (chưa cấp lịch sử) ⇒ không chạy lệch backtest.
"""
from datetime import date

import pytest

from backtest.engine import BacktestEngine
from tests.test_backtest_engine import bt_settings, flat

D16, D17, D18, D19, D20 = (date(2026, 11, x) for x in (16, 17, 18, 19, 20))
NOV, DEC = "VN30F2611", "VN30F2612"


class Recorder:
    """Ghi lại `ctx.history` ở bar đầu tiên mỗi ngày; không ra lệnh."""

    def __init__(self, history_days=5):
        self.history_days = history_days
        self.seen: dict[date, tuple] = {}
        self.today: dict[date, int] = {}

    def on_bar(self, bar, ctx):
        d = bar.start.date()
        self.seen.setdefault(d, ctx.history)
        self.today[d] = len(ctx.bars_today)
        return []


@pytest.fixture
def settings(session_cfg):
    return bt_settings(session_cfg)


def run(settings, cal, strat, bars, **kw):
    bars = sorted(bars, key=lambda b: (b.start, b.symbol))
    return BacktestEngine(settings, strat, calendar=cal, **kw).run_bars(bars)


def day(d, sym, px, n=4, hhmm="10:00"):
    return flat(hhmm, n, price=px, day=d, symbol=sym)


def roll_data(with_dec_on_eve=True):
    """16–18/11 giao dịch 2611 (1350), 19–20/11 giao dịch 2612 (1400). Ngày trước roll = 18/11."""
    out = day(D16, NOV, 1350.0) + day(D17, NOV, 1350.0) + day(D18, NOV, 1350.0)
    if with_dec_on_eve:
        out += day(D18, DEC, 1400.0, n=4, hhmm="10:01")            # chung các phút 10:01..10:03 với 2611
    out += day(D19, DEC, 1400.0) + day(D20, DEC, 1400.0)
    return out


def test_default_strategy_gets_no_history(settings, cal):
    strat = Recorder(history_days=0)
    del strat.history_days                                          # chiến lược cũ không khai báo
    run(settings, cal, strat, roll_data())
    assert all(h == () for h in strat.seen.values())


def test_history_back_adjusted_across_roll(settings, cal):
    strat = Recorder(history_days=5)
    res = run(settings, cal, strat, roll_data())
    h = strat.seen[D20]
    assert [b.start.date() for b in h] == [D16] * 4 + [D17] * 4 + [D18] * 4 + [D19] * 4
    assert {b.symbol for b in h} == {DEC}
    assert {b.close for b in h} == {1400.0}                         # 1350 + 50 = giá 2612
    q = res.data_quality
    assert q["roll_gaps"] == [f"{D18}: {NOV} → {DEC} {50.0:+.1f} điểm"]
    assert q["roll_gaps_missing"] == 0


def test_before_roll_history_is_raw_no_future_gap(settings, cal):
    strat = Recorder(history_days=5)
    run(settings, cal, strat, roll_data())
    assert {(b.symbol, b.close) for b in strat.seen[D18]} == {(NOV, 1350.0)}
    assert [b.start.date() for b in strat.seen[D18]] == [D16] * 4 + [D17] * 4
    # ngày roll (19/11): HĐ mới, lịch sử 16–18/11 đã dịch +50
    assert {b.close for b in strat.seen[D19]} == {1400.0} and len(strat.seen[D19]) == 12


def test_point_in_time_history_unchanged_by_later_data(settings, cal):
    full, cut = Recorder(), Recorder()
    data = roll_data()
    run(settings, cal, full, data)
    run(settings, cal, cut, [b for b in data if b.start.date() <= D19])
    assert full.seen[D19] == cut.seen[D19] and full.seen[D18] == cut.seen[D18]


def test_other_listed_contracts_on_eve_do_not_break_gap(settings, cal):
    """Dữ liệu `--all-listed` (thêm F1Q/F2Q) ngày trước roll: chỉ HĐ kế tiếp được dùng để đo."""
    data = roll_data() + day(D18, "VN30F2703", 1450.0, hhmm="10:01") + day(D18, "VN30F2706", 1480.0, hhmm="10:01")
    strat = Recorder(history_days=5)
    res = run(settings, cal, strat, data)
    assert res.data_quality["roll_gaps"] == [f"{D18}: {NOV} → {DEC} {50.0:+.1f} điểm"]
    assert {b.close for b in strat.seen[D20]} == {1400.0}


def test_history_excludes_today_and_respects_limit(settings, cal):
    strat = Recorder(history_days=2)
    run(settings, cal, strat, roll_data())
    assert [b.start.date() for b in strat.seen[D20]] == [D18] * 4 + [D19] * 4
    assert strat.today[D20] == 4


def test_missing_next_contract_on_eve_truncates_and_warns(settings, cal):
    strat = Recorder(history_days=5)
    res = run(settings, cal, strat, roll_data(with_dec_on_eve=False))
    assert [b.start.date() for b in strat.seen[D20]] == [D19] * 4
    assert strat.seen[D19] == ()
    assert res.data_quality["roll_gaps_missing"] == 1
    w = [x for x in res.warnings if str(D18) in x and DEC in x]
    assert w and "cắt" in w[0]


def test_history_mode_requires_trading_contract_only(settings, cal):
    with pytest.raises(ValueError, match="trading_contract_only"):
        BacktestEngine(settings, Recorder(), calendar=cal, trading_contract_only=False)


def test_negative_history_days_rejected(settings, cal):
    with pytest.raises(ValueError, match="history_days"):
        BacktestEngine(settings, Recorder(history_days=-1), calendar=cal)


def test_assumption_recorded(settings, cal):
    res = run(settings, cal, Recorder(), roll_data())
    assert "back-adjust" in res.assumptions["history"]
    res0 = run(settings, cal, Recorder(history_days=0), roll_data())
    assert "history" not in res0.assumptions and "roll_gaps" not in res0.data_quality


# ------------------------------------------------------------------ live/paper từ chối ----

def test_runner_refuses_strategy_needing_history(session_cfg, cal, tmp_path):
    from tests.test_runner import Rig
    r = Rig(session_cfg, cal, tmp_path)
    r.runner.strategy = Recorder(history_days=3)
    connected = []
    orig = r.broker.connect

    async def spy():
        connected.append(1)
        return await orig()
    r.broker.connect = spy
    assert r.start() == 2
    assert not connected
    assert any("lịch sử" in n for n in r.notes)


def test_downloader_plans_next_contract_on_roll_eve(cal):
    from scripts.download_history import plan_days
    plan = plan_days(D17, D19, cal, roll_days_before_expiry=1)
    assert [(d, n) for d, n, _ in plan] == [(D17, NOV), (D18, NOV), (D18, DEC), (D19, DEC)]
