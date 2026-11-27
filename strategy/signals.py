"""Tín hiệu cho giai đoạn đo tín hiệu (`backtest/signal_study.py`) — hàm THUẦN `(bars_today, history) -> số | None`.

Nhận đúng hai trường của `StrategyContext`: `bars_today` = nến đã đóng hôm nay của hợp đồng giao dịch, gồm nến hiện
tại; `history` = các phiên trước, back-adjust. Không I/O, không đọc giờ hệ thống. Hàm cần lịch sử khai báo
`fn.history_days`. Định nghĩa từng tín hiệu CHỐT trong research/log.md ("Đăng ký trước #10, #11, #12", 27/11/2026) —
đổi bất kỳ số nào = cấu hình MỚI (thêm dòng bảng, tăng `n_trials`), không sửa tại chỗ.
"""
from __future__ import annotations

from typing import Optional, Sequence

from core.models import Bar
from strategy.indicators import adr_before

FLOW_BARS = 30      # #10: số nến gần nhất của hôm nay
TREND_ADR_DAYS = 10  # #11: số phiên tính ADR


def volume_flow(bars_today: Sequence[Bar], history: Sequence[Bar]) -> Optional[float]:
    """#10 — dòng tiền theo khối lượng: F = Σ dấu(đóng − mở) × KL / Σ KL trên 30 nến gần nhất của hôm nay, ∈ [−1, 1].

    None khi hôm nay chưa đủ 30 nến, có nến trong cửa sổ mang khối lượng không tin được, hoặc tổng khối lượng bằng 0.
    """
    if len(bars_today) < FLOW_BARS:
        return None
    window = bars_today[-FLOW_BARS:]
    if not all(b.volume_reliable for b in window):
        return None
    total = sum(b.volume for b in window)
    if total <= 0:
        return None
    signed = sum(b.volume * ((b.close > b.open) - (b.close < b.open)) for b in window)
    return signed / total


def intraday_trend(bars_today: Sequence[Bar], history: Sequence[Bar]) -> Optional[float]:
    """#11 — xu hướng trong ngày: T = (đóng nến hiện tại − mở nến đầu tiên hôm nay) / ADR10.

    ADR10 = trung bình (đỉnh − đáy) của đúng 10 phiên trước hôm nay trong `history`; thiếu ⇒ None.
    """
    if not bars_today:
        return None
    adr = adr_before(history, bars_today[-1].start.date(), TREND_ADR_DAYS)
    if adr is None:
        return None
    return (bars_today[-1].close - bars_today[0].open) / adr


intraday_trend.history_days = TREND_ADR_DAYS
