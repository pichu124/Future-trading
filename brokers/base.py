"""Interface chung cho mọi broker (paper, DNSE, SSI, TCBS).

Hợp đồng mà MỌI adapter phải giữ:
- `place(order)` idempotent theo `client_order_id`: gọi lại với id đã gửi ⇒ trả lệnh cũ, không
  tạo lệnh thứ hai. Broker không có khoá idempotency (DNSE) ⇒ adapter KHÔNG được tự retry
  lệnh đặt; khi không chắc lệnh đã tới sàn thì tra sổ lệnh trước (docs/broker_notes.md).
- Lệnh bị broker từ chối được TRẢ VỀ với `status=REJECTED` và `reject_reason`, không ném lỗi.
  `cancel`/`modify` không thực hiện được (không tồn tại, đã kết thúc, đang ATO/ATC) ⇒ `BrokerError`.
- Mọi thay đổi trạng thái lệnh phát qua `on_order_update`; mỗi lần khớp phát qua `on_fill`
  (fill trước, cập nhật lệnh sau). Runner nối hai callback này vào `RiskManager.record_fill`
  và `RiskManager.on_order_update`.
- Adapter thật gọi `assert_live_trading_allowed()` NGAY TRƯỚC mọi request đặt/sửa/huỷ lệnh
  (CLAUDE.md mục 7) — không chỉ lúc khởi động.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Callable, Mapping, Optional

from core.models import Fill, Order, Position

FillHandler = Callable[[Fill], None]
OrderHandler = Callable[[Order], None]


class BrokerError(RuntimeError):
    """Broker không thực hiện được yêu cầu (huỷ/sửa lệnh không hợp lệ, mất kết nối…)."""


class LiveTradingNotAllowed(RuntimeError):
    """Định gửi lệnh thật khi chưa bật đủ hai chốt TRADING_MODE=live và cờ --live."""


def assert_live_trading_allowed(live_flag: bool, env: Optional[Mapping[str, str]] = None) -> None:
    """Chỉ qua khi biến môi trường TRADING_MODE=live VÀ runner được chạy với `--live`."""
    env = os.environ if env is None else env
    mode = str(env.get("TRADING_MODE", "paper")).strip().lower()
    if mode != "live":
        raise LiveTradingNotAllowed(f"TRADING_MODE={mode!r} — chỉ gửi lệnh thật khi TRADING_MODE=live")
    if not live_flag:
        raise LiveTradingNotAllowed("Thiếu cờ --live khi chạy runner — không gửi lệnh thật")


class BrokerAdapter(ABC):
    def __init__(self, name: str):
        if not name:
            raise ValueError("Broker phải có tên (khớp với Order.broker / Position.broker)")
        self.name = name
        self._fill_handlers: list[FillHandler] = []
        self._order_handlers: list[OrderHandler] = []

    # ---- vòng đời (adapter mạng ghi đè) ----
    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # ---- lệnh ----
    @abstractmethod
    async def place(self, order: Order) -> Order:
        """Gửi lệnh; trả lệnh với status PENDING (đã nhận) hoặc REJECTED (kèm lý do)."""

    @abstractmethod
    async def cancel(self, client_order_id: str) -> Order:
        """Huỷ lệnh còn sống; trả lệnh CANCELLED. Không huỷ được ⇒ BrokerError."""

    @abstractmethod
    async def modify(self, client_order_id: str, *, price: Optional[float] = None,
                     qty: Optional[int] = None) -> Order:
        """Sửa giá/khối lượng (tổng) của lệnh còn sống. Có thể đổi broker_order_id (DNSE: huỷ-đặt)."""

    @abstractmethod
    async def positions(self) -> dict[str, Position]:
        """Vị thế theo symbol trên broker này."""

    @abstractmethod
    async def open_orders(self) -> list[Order]:
        """Các lệnh chưa kết thúc."""

    # ---- callback ----
    def on_fill(self, handler: FillHandler) -> None:
        self._fill_handlers.append(handler)

    def on_order_update(self, handler: OrderHandler) -> None:
        self._order_handlers.append(handler)

    def _emit_fill(self, fill: Fill) -> None:
        for h in self._fill_handlers:
            h(fill)

    def _emit_order(self, order: Order) -> None:
        for h in self._order_handlers:
            h(order)
