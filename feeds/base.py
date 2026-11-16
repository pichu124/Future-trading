"""Interface chung cho mọi nguồn dữ liệu realtime (DNSE, SSI, composite).

Mọi feed chuẩn hoá về `core.models.Tick` (symbol = tên hợp đồng VN30FYYMM, giá theo điểm,
thời gian naive giờ VN) để strategy/risk/recorder không phụ thuộc broker.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Optional

from core.models import Tick

TickHandler = Callable[[Tick], None]


class DataFeed(ABC):
    name: str = ""

    def __init__(self) -> None:
        self._handlers: list[TickHandler] = []
        self._last_message_at: Optional[datetime] = None

    def on_tick(self, handler: TickHandler) -> None:
        self._handlers.append(handler)

    @property
    def last_message_at(self) -> Optional[datetime]:
        """Lúc nhận tick gần nhất (giờ VN, theo đồng hồ của bot)."""
        return self._last_message_at

    @abstractmethod
    async def run(self) -> None:
        """Chạy tới khi `stop()`; tự kết nối lại. Lỗi không thể tự khắc phục (sai khoá…) ⇒ ném."""

    @abstractmethod
    async def stop(self) -> None:
        """Dừng êm, đóng kết nối."""

    @abstractmethod
    def is_stale(self, now: Optional[datetime] = None) -> bool:
        """True khi feed im lặng quá ngưỡng TRONG phiên giao dịch liên tục."""

    @abstractmethod
    def stats(self) -> dict:
        """Số liệu vận hành (không chứa secret)."""
