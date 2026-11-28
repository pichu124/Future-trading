"""Kiểm toán chi phí khớp lệnh từ dữ liệu recorder (`backtest/cost_audit.py`) — viết TRƯỚC khi implement.

Đo, trên tick + giá tốt nhất mà recorder ghi (`data/raw/YYYY-MM-DD/`), chi phí THỰC của một chiều giao dịch 1 HĐ so với
giả định của backtest (trượt 1 bước + nửa spread làm tròn 1 bước = 0,2 điểm mỗi chiều):
- spread theo thời gian (tỉ lệ thời gian spread = 1, 2, ≥ 3 bước), độ sâu ở giá tốt nhất, theo khung giờ;
- lệnh thị trường ra quyết định lúc t, tới sàn lúc t + độ trễ: thiếu hụt = giá khớp − mid lúc t (lấy dấu theo chiều);
- lệnh giới hạn đặt ở giá tốt nhất CÙNG phía lúc tới sàn, xếp sau toàn bộ khối lượng đang chờ ở mức đó (không tính huỷ
  phía trước ⇒ bi quan); khớp khi có giao dịch XUYÊN giá, hoặc tổng khối lượng giao dịch ĐÚNG giá vượt quá hàng chờ
  phía trước; hết T giây chưa khớp ⇒ đuổi bằng lệnh thị trường. Thiếu hụt tính so với mid lúc ra quyết định.
- giao dịch thật: nửa spread hiệu dụng |giá khớp − mid|, tỉ lệ khớp ở giá bán / giá mua / giữa.
"""
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from backtest.cost_audit import (AuditConfig, DayBook, audit_day, decision_times, limit_then_market, load_day,
                                 main, market_now, render, spread_profile, summarize, trade_stats)

DAY = date(2026, 11, 17)
SYM = "VN30F2611"
TICK = 0.1


def T(hhmmss, day=DAY):
    return datetime.combine(day, time.fromisoformat(hhmmss))


def ts_ns(dt):
    return pd.Timestamp(dt).value


def q(t, bid, ask, bq=5, aq=5):
    return dict(ts=T(t), last=None, last_qty=None, bid=bid, ask=ask, bid_qty=bq, ask_qty=aq)


def tr(t, price, qty, bid=None, ask=None):
    return dict(ts=T(t), last=price, last_qty=qty, bid=bid, ask=ask, bid_qty=None, ask_qty=None)


def frame(rows, symbol=SYM, source="dnse", session="s1"):
    cols = ["ts", "last", "last_qty", "bid", "ask", "bid_qty", "ask_qty"]
    df = pd.DataFrame(rows, columns=cols)
    df["symbol"], df["source"], df["session"] = symbol, source, session
    df["rx_seq"] = np.arange(len(df))
    df["recv_ts"] = df["ts"]
    df["total_volume"], df["seq"] = None, None
    return df


def book(rows):
    return DayBook.from_frame(frame(rows), TICK)


SEG = [(T("09:00:00"), T("11:30:00")), (T("13:00:00"), T("14:30:00"))]


# ---------------------------------------------------------------- sổ lệnh ----
def test_quote_at_is_last_valid_quote_at_or_before():
    b = book([q("09:00:00", 1300.0, 1300.1), q("09:00:10", 1300.1, 1300.2), q("09:00:20", 1300.2, 1300.2),
              q("09:00:30", 1300.3, 1300.2)])
    assert b.quote_at(ts_ns(T("08:59:59"))) is None
    assert b.quote_at(ts_ns(T("09:00:05")))[:2] == (1300.0, 1300.1)
    assert b.quote_at(ts_ns(T("09:00:10")))[:2] == (1300.1, 1300.2)          # đúng thời điểm ⇒ báo giá đó
    assert b.quote_at(ts_ns(T("09:00:40")))[:2] == (1300.1, 1300.2)          # khoá (bid = ask) và chéo bị bỏ


def test_trades_and_quotes_are_split():
    b = book([q("09:00:00", 1300.0, 1300.1), tr("09:00:01", 1300.1, 2, 1300.0, 1300.1)])
    assert len(b.trade_ns) == 1 and b.trade_px[0] == 13001 and b.trade_qty[0] == 2   # giá theo số bước
    assert len(b.quote_ns) == 2                                                     # tick khớp cũng mang báo giá


# ---------------------------------------------------------------- spread ----
def test_spread_profile_is_time_weighted_and_clipped_to_segments():
    rows = [q("08:59:00", 1300.0, 1300.2),                                     # trước phiên: mang vào đầu đoạn
            q("09:01:00", 1300.0, 1300.1),
            q("09:02:00", 1300.0, 1300.3)]
    seg = [(T("09:00:00"), T("09:03:00"))]
    p = spread_profile(book(rows), seg, TICK)
    assert p["seconds"] == pytest.approx(180)
    assert p["frac"] == {1: pytest.approx(1 / 3), 2: pytest.approx(1 / 3), 3: pytest.approx(1 / 3)}
    assert p["mean_half_spread"] == pytest.approx((0.1 + 0.05 + 0.15) / 3)
    assert p["min_depth_median"] == 5


def test_spread_profile_buckets_by_time_of_day():
    rows = [q("09:00:00", 1300.0, 1300.1), q("09:15:00", 1300.0, 1300.2), q("09:30:00", 1300.0, 1300.1)]
    p = spread_profile(book(rows), [(T("09:00:00"), T("09:45:00"))], TICK,
                       buckets=[(time(9, 0), time(9, 15)), (time(9, 15), time(9, 30)), (time(9, 30), time(9, 45))])
    assert [b["mean_half_spread"] for b in p["buckets"]] == pytest.approx([0.05, 0.1, 0.05])


def test_spread_profile_empty_without_quotes():
    p = spread_profile(book([tr("09:00:01", 1300.0, 1)]), SEG, TICK)
    assert p["seconds"] == 0 and p["mean_half_spread"] is None


# ------------------------------------------------------- thời điểm quyết định ----
def test_decision_times_are_minute_starts_inside_window_with_room():
    cfg = AuditConfig(tick_size=TICK, latency_s=0.3, timeouts=(10, 30, 60), entry_from=time(9, 15),
                      entry_until=time(14, 15))
    ts = decision_times(DAY, SEG, cfg)
    assert ts[0] == T("09:15:00") and T("09:14:00") not in ts
    assert T("11:28:00") in ts and T("11:29:00") not in ts                   # 11:29 + 0,3 s + 60 s ≥ 11:30
    assert T("13:00:00") in ts and T("12:00:00") not in ts                    # nghỉ trưa
    assert ts[-1] == T("14:14:00")                                            # vào trước 14:15
    assert len(ts) == len(set(ts)) and ts == sorted(ts)


# ------------------------------------------------------------ lệnh thị trường ----
CFG = AuditConfig(tick_size=TICK, latency_s=1.0, timeouts=(10,), entry_from=time(9, 0), entry_until=time(14, 15))


def test_market_now_uses_quote_on_arrival_and_mid_at_decision():
    b = book([q("09:00:00", 1300.0, 1300.1), q("09:00:00.500000", 1300.1, 1300.2)])
    t = ts_ns(T("09:00:00"))
    assert market_now(b, t, +1, CFG) == pytest.approx(1300.2 - 1300.05)       # mua ở ask lúc tới sàn
    assert market_now(b, t, -1, CFG) == pytest.approx(-(1300.1 - 1300.05))    # bán ở bid (giá đi có lợi ⇒ âm)
    assert market_now(book([]), t, +1, CFG) is None


# ------------------------------------------------------------ lệnh giới hạn ----
def lim(rows, side, cfg=CFG):
    return limit_then_market(book(rows), ts_ns(T("09:00:00")), side, cfg)


def test_limit_fills_on_trade_through():
    rows = [q("09:00:00", 1300.0, 1300.1, bq=50), tr("09:00:05", 1299.9, 1)]
    r = lim(rows, +1)[10]
    assert r["filled"] and r["shortfall"] == pytest.approx(1300.0 - 1300.05)  # khớp ở bid: được nửa spread
    assert r["fill_s"] == pytest.approx(5.0)


def test_limit_queue_must_be_exceeded_by_trades_at_price():
    base = [q("09:00:00", 1300.0, 1300.1, bq=5)]
    assert lim(base + [tr("09:00:03", 1300.0, 3), tr("09:00:04", 1300.0, 2)], +1)[10]["filled"] is False  # đúng 5
    r = lim(base + [tr("09:00:03", 1300.0, 3), tr("09:00:04", 1300.0, 3)], +1)[10]
    assert r["filled"] and r["fill_s"] == pytest.approx(4.0)


def test_limit_ignores_trades_before_arrival_and_after_timeout():
    rows = [q("09:00:00", 1300.0, 1300.1, bq=1), tr("09:00:00.500000", 1299.9, 5),   # trước khi tới sàn (độ trễ 1 s)
            tr("09:00:11.500000", 1299.9, 5)]                                         # sau 1 + 10 s
    assert lim(rows, +1)[10]["filled"] is False


def test_limit_unfilled_chases_at_market_after_timeout():
    rows = [q("09:00:00", 1300.0, 1300.1), tr("09:00:03", 1300.1, 9),
            q("09:00:08", 1300.2, 1300.3)]                                    # giá chạy lên, không quay lại
    r = lim(rows, +1)[10]
    assert r["filled"] is False and r["fill_s"] is None
    assert r["shortfall"] == pytest.approx(1300.3 - 1300.05)                   # đuổi ở ask lúc t + 1 + 10 + 1 s


def test_limit_sell_side_is_symmetric():
    rows = [q("09:00:00", 1300.0, 1300.1, aq=2), tr("09:00:04", 1300.1, 3)]
    r = lim(rows, -1)[10]
    assert r["filled"] and r["shortfall"] == pytest.approx(-(1300.1 - 1300.05))
    rows = [q("09:00:00", 1300.0, 1300.1, aq=2), tr("09:00:04", 1300.2, 1)]   # xuyên giá phía trên
    assert lim(rows, -1)[10]["filled"]
    rows = [q("09:00:00", 1300.0, 1300.1, aq=2), tr("09:00:04", 1300.0, 9)]   # khớp ở bid không chạm lệnh bán ở ask
    assert lim(rows, -1)[10]["filled"] is False


def test_limit_unknown_queue_fills_only_on_trade_through():
    rows = [q("09:00:00", 1300.0, 1300.1, bq=None), tr("09:00:03", 1300.0, 999)]
    assert lim(rows, +1)[10]["filled"] is False
    rows = [q("09:00:00", 1300.0, 1300.1, bq=None), tr("09:00:03", 1299.9, 1)]
    assert lim(rows, +1)[10]["filled"] is True


def test_limit_several_timeouts_share_one_order():
    cfg = AuditConfig(tick_size=TICK, latency_s=1.0, timeouts=(10, 30), entry_from=time(9, 0),
                      entry_until=time(14, 15))
    rows = [q("09:00:00", 1300.0, 1300.1), tr("09:00:20", 1299.9, 1)]
    r = limit_then_market(book(rows), ts_ns(T("09:00:00")), +1, cfg)
    assert r[10]["filled"] is False and r[30]["filled"] is True and r[30]["fill_s"] == pytest.approx(20.0)


def test_limit_none_without_quote():
    assert limit_then_market(book([]), ts_ns(T("09:00:00")), +1, CFG) is None


# ------------------------------------------------------------- giao dịch ----
def test_trade_stats_effective_half_spread_and_side():
    rows = [tr("09:00:01", 1300.1, 1, 1300.0, 1300.1), tr("09:00:02", 1300.0, 3, 1300.0, 1300.1),
            tr("09:00:03", 1300.05, 1, 1300.0, 1300.1), tr("09:00:04", 1300.0, 1)]     # dòng cuối không có báo giá
    s = trade_stats(book(rows), SEG, TICK)
    assert s["n"] == 3 and s["frac_at_ask"] == pytest.approx(1 / 3) and s["frac_at_bid"] == pytest.approx(1 / 3)
    assert s["mean_eff_half_spread"] == pytest.approx((0.05 + 0.05 + 0.0) / 3)
    assert s["qty_weighted_eff_half_spread"] == pytest.approx((0.05 + 0.15 + 0.0) / 5)


# ------------------------------------------------------------- một ngày ----
def day_rows():
    rng = np.random.default_rng(1)
    rows, mid = [], 13000                        # đơn vị bước
    t = T("09:00:00")
    while t < T("10:30:00"):
        if rng.random() < 0.5:
            mid += int(rng.choice([-1, 1]))
        bid = mid / 10.0
        rows.append(dict(ts=t, last=None, last_qty=None, bid=bid, ask=round(bid + 0.1, 1), bid_qty=3, ask_qty=3))
        px = bid if rng.random() < 0.5 else round(bid + 0.1, 1)
        rows.append(dict(ts=t + timedelta(seconds=1), last=px, last_qty=int(rng.integers(1, 4)), bid=bid,
                         ask=round(bid + 0.1, 1), bid_qty=None, ask_qty=None))
        t += timedelta(seconds=2)
    return rows


def test_audit_day_collects_both_sides_at_each_decision():
    cfg = AuditConfig(tick_size=TICK, latency_s=0.3, timeouts=(10, 30), entry_from=time(9, 15),
                      entry_until=time(10, 0))
    r = audit_day(book(day_rows()), DAY, [(T("09:00:00"), T("10:30:00"))], cfg)
    assert r["n_decisions"] == 45 * 2
    assert r["market"]["mean"] == pytest.approx(0.05, abs=1e-9)               # spread luôn 1 bước ⇒ nửa spread
    for T_ in (10, 30):
        assert 0 <= r["limit"][T_]["fill_rate"] <= 1
    assert r["spread"]["frac"][1] == pytest.approx(1.0)


def test_summarize_pools_days_and_uses_day_level_standard_error():
    d1 = {"day": DAY, "n_decisions": 2, "market": {"values": [0.05, 0.05], "mean": 0.05},
          "limit": {10: {"values": [-0.05, 0.15], "fills": [True, False], "mean": 0.05, "fill_rate": 0.5}},
          "spread": {"seconds": 100.0, "frac": {1: 1.0}, "mean_half_spread": 0.05, "min_depth_median": 3},
          "trades": {"n": 10, "mean_eff_half_spread": 0.05, "qty_weighted_eff_half_spread": 0.05,
                     "frac_at_ask": 0.5, "frac_at_bid": 0.5}}
    d2 = {"day": DAY + timedelta(days=1), "n_decisions": 2, "market": {"values": [0.15, 0.15], "mean": 0.15},
          "limit": {10: {"values": [0.25, 0.25], "fills": [False, False], "mean": 0.25, "fill_rate": 0.0}},
          "spread": {"seconds": 300.0, "frac": {1: 0.5, 2: 0.5}, "mean_half_spread": 0.075, "min_depth_median": 1},
          "trades": {"n": 30, "mean_eff_half_spread": 0.1, "qty_weighted_eff_half_spread": 0.1,
                     "frac_at_ask": 0.4, "frac_at_bid": 0.6}}
    s = summarize([d1, d2], fees_round_trip=0.467, assumed_side=0.2)
    assert s["n_days"] == 2
    assert s["market"]["mean"] == pytest.approx(0.10)
    assert s["market"]["se"] == pytest.approx(0.05)                           # sd(0,05; 0,15)/√2 (ddof=1)
    assert s["limit"][10]["mean"] == pytest.approx(0.15) and s["limit"][10]["fill_rate"] == pytest.approx(0.25)
    assert s["spread"]["frac"][1] == pytest.approx((100 + 150) / 400)         # gộp theo thời gian
    assert s["round_trip"]["market"] == pytest.approx(0.467 + 2 * 0.10)
    assert s["round_trip"]["assumed"] == pytest.approx(0.467 + 2 * 0.2)
    assert s["best_policy"] == "market"


def test_render_has_the_comparison_a_person_needs():
    cfg = AuditConfig(tick_size=TICK, latency_s=0.3, timeouts=(10, 30), entry_from=time(9, 15),
                      entry_until=time(10, 0))
    s = summarize([audit_day(book(day_rows()), DAY, [(T("09:00:00"), T("10:30:00"))], cfg)],
                  fees_round_trip=0.467, assumed_side=0.2)
    out = render(s)
    for needle in ("0,20", "0,87", "lệnh thị trường", "giới hạn", "10 s", "30 s", "1 ngày"):
        assert needle in out, needle
    assert "Rẻ nhất: lệnh thị trường" in out or "Rẻ nhất: giới hạn chờ" in out


# ------------------------------------------------------------- đọc file ----
def write_day(root, day, df, name="ticks.parquet"):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from recorder.tick_recorder import SCHEMA
    d = root / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    cols = [f.name for f in SCHEMA]
    pq.write_table(pa.Table.from_pandas(df[cols], schema=SCHEMA, preserve_index=False), d / name)
    return d


def test_load_day_dedupes_parts_and_filters_symbol_and_source(tmp_path):
    rows = [q("09:00:00", 1300.0, 1300.1), q("09:00:10", 1300.1, 1300.2)]
    df = frame(rows)
    write_day(tmp_path, DAY, df)
    write_day(tmp_path, DAY, df, name="part-s1-1.parquet")                    # trùng (session, rx_seq)
    other = frame([q("09:00:05", 1400.0, 1400.1)], symbol="VN30F2612")
    other["rx_seq"] += 100
    write_day(tmp_path, DAY, other, name="part-s1-2.parquet")
    ssi = frame([q("09:00:06", 1300.0, 1300.1)], source="ssi", session="s2")
    write_day(tmp_path, DAY, ssi, name="part-s2-1.parquet")
    got = load_day(tmp_path / DAY.isoformat(), SYM, "dnse")
    assert len(got) == 2 and set(got["symbol"]) == {SYM} and set(got["source"]) == {"dnse"}
    assert list(got["ts"]) == sorted(got["ts"])
    assert load_day(tmp_path / "2026-01-01", SYM, "dnse").empty


def test_cli_runs_over_recorded_days(tmp_path, capsys):
    from core.clock import FakeClock, MarketClock
    from core.settings import load_settings
    mc = MarketClock.from_settings(load_settings(), clock=FakeClock(datetime(2000, 1, 3)))
    days = [date(2026, 6, 1), date(2026, 6, 2)]
    for d in days:
        rows = [dict(r, ts=datetime.combine(d, r["ts"].time())) for r in day_rows()]
        write_day(tmp_path, d, frame(rows, symbol=mc.trading_contract(d)))
    code = main(["--raw", str(tmp_path), "--from", "2026-06-01", "--to", "2026-06-05", "--timeouts", "10", "30"])
    out = capsys.readouterr().out
    assert code == 0 and "2 ngày" in out and "lệnh thị trường" in out


def test_cli_reports_no_data(tmp_path, capsys):
    assert main(["--raw", str(tmp_path), "--from", "2026-06-01", "--to", "2026-06-05"]) == 2
    assert "❌" in capsys.readouterr().out


# ------------------------------------------------------- ca biên bổ sung ----
def test_chase_uses_quote_after_one_more_latency():
    rows = [q("09:00:00", 1300.0, 1300.1), q("09:00:11.500000", 1300.4, 1300.5)]   # đổi giữa t+11 s và t+12 s
    r = lim(rows, +1)[10]
    assert r["filled"] is False and r["shortfall"] == pytest.approx(1300.5 - 1300.05)


def test_spread_three_or_more_ticks_share_one_bucket():
    rows = [q("09:00:00", 1300.0, 1300.3), q("09:01:00", 1300.0, 1300.5)]
    p = spread_profile(book(rows), [(T("09:00:00"), T("09:02:00"))], TICK)
    assert p["frac"] == {3: pytest.approx(1.0)}


def test_quote_exactly_at_segment_start_replaces_the_old_one():
    rows = [q("08:59:00", 1300.0, 1300.2), q("09:00:00", 1300.0, 1300.1)]
    p = spread_profile(book(rows), [(T("09:00:00"), T("09:01:00"))], TICK)
    assert p["frac"] == {1: pytest.approx(1.0)}


def test_decision_room_includes_both_latencies():
    cfg = AuditConfig(tick_size=TICK, latency_s=1.0, timeouts=(59,), entry_from=time(9, 15),
                      entry_until=time(14, 15))
    ts = decision_times(DAY, SEG, cfg)
    assert T("11:28:00") in ts and T("11:29:00") not in ts                   # 11:29 + 2 × 1 s + 59 s > 11:30


def test_audit_day_skips_decisions_on_stale_quotes():
    rows = [r for r in day_rows() if r["ts"] < T("09:20:00")]
    cfg = AuditConfig(tick_size=TICK, latency_s=0.3, timeouts=(10,), entry_from=time(9, 15),
                      entry_until=time(10, 0), max_quote_age_s=30)
    r = audit_day(book(rows), DAY, [(T("09:00:00"), T("10:30:00"))], cfg)
    assert r["n_decisions"] == 6 * 2              # 09:15 … 09:20 (báo giá cuối 09:19:59, mới 1 s); từ 09:21 báo giá cũ > 30 s
