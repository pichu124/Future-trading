"""Interface chiến lược — chạy y hệt cho backtest, paper và live (CLAUDE.md mục 2).

Luật:
- Chiến lược là hàm THUẦN theo nghĩa: chỉ dùng `bar` và `ctx`; KHÔNG I/O, KHÔNG gọi API,
  KHÔNG đọc giờ hệ thống, không biết đang chạy live hay backtest.
- `on_bar` chỉ được gọi với bar ĐÃ ĐÓNG, tại thời điểm đóng bar. Lệnh sinh ra khớp ở tick
  sau đó (không bao giờ ở giá đóng của chính bar tín hiệu).
- Lệnh vào (`ENTER_*`) phải kèm `stop_loss`; risk manager từ chối nếu thiếu.
- `ENTER_*` ngược chiều vị thế đang giữ chỉ ĐÓNG vị thế; muốn đảo chiều thì phát lệnh vào ở
  bar sau (tránh hai lệnh ngược nhau cùng chờ khớp).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from core.models import Bar, Signal


@dataclass(frozen=True)
class StrategyContext:
    position_qty: int                    # net cộng mọi broker; dương = Long
    avg_price: Optional[float]           # giá vốn vị thế đang giữ (None khi phẳng)
    bars_today: tuple[Bar, ...]          # bar đã đóng hôm nay của hợp đồng này, gồm bar hiện tại
    # `history_days` phiên TRƯỚC hôm nay, back-adjust (cộng điểm) về giá HĐ hiện tại, cũ nhất trước
    # (`core/continuous.py`). Chỉ có khi chiến lược khai báo `history_days > 0`; có thể ngắn hơn N
    # (thiếu dữ liệu / không đo được chênh lệch roll). Live/paper CHƯA cấp ⇒ runner từ chối chiến lược đó.
    history: tuple[Bar, ...] = ()


def history_days(strategy) -> int:
    """Số phiên lịch sử chiến lược cần (thuộc tính `history_days`, mặc định 0)."""
    n = int(getattr(strategy, "history_days", 0) or 0)
    if n < 0:
        raise ValueError("history_days phải ≥ 0")
    return n


class Strategy(Protocol):
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Signal]: ...
