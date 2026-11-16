"""Runner paper chạy chiến lược chọn bằng `--strategy` (vd #3 đà sáng → chiều). Viết TRƯỚC khi sửa.

- Cả ngày qua runner thật (tick → nến → chiến lược → ExecutionManager → PaperBroker): #3 vào Long lúc 13:01 với
  stop đúng, thoát lúc 14:20 (`exit`), lịch sử ADR lấy từ `history_loader` như live.
- Tin khởi động ghi tên chiến lược (`TITLE`, không có thì tên class) — nhìn Telegram biết bot đang chạy gì.
- `main --strategy` sai module/class ⇒ thoát 2 với thông báo rõ, không văng traceback.
"""
from datetime import datetime, time, timedelta

from strategy.morning_momentum import MorningMomentum
from tests.test_runner import SYM, Rig
from tests.test_strategy_morning_momentum import history


class FakeHistory:
    def __init__(self, bars):
        self.bars = tuple(bars)

    def view(self, symbol):
        return tuple(b for b in self.bars if b.symbol == symbol)


def mm():
    return MorningMomentum(adr_days=3, min_move_adr=0.25, stop_adr=0.30, exit_bar=time(14, 19), qty=1,
                           am_start=time(9, 0), lunch_start=time(11, 30), pm_start=time(13, 0), tick_size=0.1)


def day_rig(session_cfg, cal, tmp_path):
    r = Rig(session_cfg, cal, tmp_path, start=datetime(2026, 11, 9, 8, 59, 30))
    r.runner.strategy = mm()
    r.runner.history_loader = lambda d: FakeHistory(history(n=3, rng=20.0, end=d))
    return r


def drive(r, start, minutes, price_at):
    """Mỗi phút: một khớp lệnh ở giây 5 rồi một vòng step (nến phút trước đóng ở vòng này)."""
    for i in range(minutes):
        t = start + timedelta(minutes=i)
        r.at(t + timedelta(seconds=5))
        r.trade(price_at(i))
        r.step()


def test_runner_paper_runs_morning_momentum_full_day(session_cfg, cal, tmp_path):
    r = day_rig(session_cfg, cal, tmp_path)
    assert r.start() is None
    am = datetime(2026, 11, 9, 9, 0)
    drive(r, am, 150, lambda i: round(1350.0 + 6.0 * i / 149, 1))           # 1350 → 1356 cả sáng
    pm = datetime(2026, 11, 9, 13, 0)
    drive(r, pm, 3, lambda i: 1356.5)                                         # 13:00 … 13:02
    assert r.state.net_qty(SYM) == 1                                           # vào ở tick 13:02:05
    stops = [o for o in r.state.working_orders.values() if o.tag == "stop"]
    assert len(stops) == 1 and abs(stops[0].price - (1356.5 - 6.0)) < 1e-6
    drive(r, pm + timedelta(minutes=3), 79, lambda i: 1356.5)                # 13:03 … 14:21
    assert r.state.net_qty(SYM) == 0
    (tr,) = r.runner.book.closed
    # mỗi phút chỉ MỘT khớp lệnh (giây 5) ⇒ nến 13:00 đóng ở vòng 13:01:05, lệnh khớp ở tick kế tiếp 13:02:05
    # (feed thật ~7 khớp/giây ⇒ gần như tức thì); tương tự EXIT ở nến 14:19 ⇒ khớp 14:21:05
    assert tr.side == "LONG" and time(13, 1) <= tr.entry_ts.time() <= time(13, 2, 5)
    assert tr.exit_reason == "exit" and time(14, 20) <= tr.exit_ts.time() <= time(14, 21, 5)


def test_startup_message_names_strategy(session_cfg, cal, tmp_path):
    r = day_rig(session_cfg, cal, tmp_path)
    r.start()
    assert any("Đà sáng → chiều (#3)" in n for n in r.notes)
    r2 = Rig(session_cfg, cal, tmp_path)                                       # Plan (không có TITLE)
    r2.start()
    assert any("chiến lược Plan" in n for n in r2.notes)


def test_main_bad_strategy_exits_2(monkeypatch, caplog):
    import live.instance_lock as il
    from live.runner import main
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setattr(il, "check_allowed_machine", lambda allowed, machine=None: "test-host")
    assert main(["--assume-computed-contract", "--strategy", "strategy.khong_co:X"]) == 2
    assert "--strategy" in caplog.text
