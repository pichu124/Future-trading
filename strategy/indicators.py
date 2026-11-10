"""Chỉ báo dùng chung cho chiến lược — hàm thuần, chỉ dùng bar đưa vào."""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from core.models import Bar


def adr_before(history: Sequence[Bar], day: date, n: int) -> Optional[float]:
    """Biên độ ngày trung bình (đỉnh − đáy) của đúng `n` phiên GẦN NHẤT TRƯỚC `day` trong `history`.

    Lịch sử back-adjust cộng điểm ⇒ đỉnh − đáy không đổi qua roll. Bar của `day` trở đi bị bỏ qua (phòng lẫn
    dữ liệu hôm nay). Thiếu phiên hoặc ADR = 0 ⇒ None (không đoán).
    """
    by_day: dict[date, tuple[float, float]] = {}
    for b in history:
        d = b.start.date()
        if d >= day:
            continue
        lo, hi = by_day.get(d, (b.low, b.high))
        by_day[d] = (min(lo, b.low), max(hi, b.high))
    last = sorted(by_day)[-n:] if n > 0 else []
    if len(last) != n or n <= 0:
        return None
    adr = sum(by_day[d][1] - by_day[d][0] for d in last) / n
    return adr if adr > 0 else None
