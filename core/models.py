"""Mô hình dữ liệu dùng chung cho feed, strategy, risk, broker, backtest.

Quy ước:
- Giá tính bằng ĐIỂM chỉ số (ví dụ 1352.4), không phải VNĐ.
- Thời gian là `datetime` naive theo giờ Asia/Ho_Chi_Minh (core.clock lo chuyển đổi).
- Số lượng là số hợp đồng, luôn dương; chiều nằm ở `Side`.
- Dataclass `frozen=True`: tạo bản mới thay vì sửa tại chỗ.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"     # mở/đóng vị thế Long
    SELL = "SELL"   # mở/đóng vị thế Short

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    LIMIT = "LO"
    MARKET = "MTL"   # tên loại lệnh thị trường khác nhau theo broker — adapter tự map
    ATO = "ATO"
    ATC = "ATC"


class OrderStatus(str, Enum):
    NEW = "NEW"                  # đã tạo, chưa gửi
    PENDING = "PENDING"          # đã gửi, chờ khớp
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


class SignalAction(str, Enum):
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"        # đóng toàn bộ vị thế hiện có
    HOLD = "HOLD"


@dataclass(frozen=True)
class Tick:
    """Một sự kiện thị trường đã chuẩn hoá từ bất kỳ feed nào."""
    symbol: str                      # tên hợp đồng chuẩn VN30FYYMM
    ts: datetime
    last: Optional[float] = None     # giá khớp gần nhất
    last_qty: Optional[int] = None   # KL của lần khớp này (delta, KHÔNG phải luỹ kế)
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_qty: Optional[int] = None
    ask_qty: Optional[int] = None
    total_volume: Optional[int] = None   # KL luỹ kế trong phiên (nếu feed có)
    source: str = ""                 # "dnse" | "ssi" | "imported" | …
    seq: Optional[int] = None        # số thứ tự message của feed (phát hiện gap)

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class Bar:
    symbol: str
    start: datetime                  # thời điểm MỞ bar (bar 09:01 chứa tick [09:01, 09:02))
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    interval_s: int = 60
    volume_reliable: bool = True     # False với dữ liệu snapshot (vd import từ dashboard)

    def __post_init__(self) -> None:
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"Bar OHLC không hợp lệ: {self}")
        if self.volume < 0:
            raise ValueError("volume âm")


@dataclass(frozen=True)
class Signal:
    """Đầu ra của strategy (hàm thuần). Risk manager quyết định có thành lệnh hay không."""
    action: SignalAction
    ts: datetime
    symbol: str
    stop_loss: Optional[float] = None     # bắt buộc với ENTER_* (risk manager kiểm)
    take_profit: Optional[float] = None
    reason: str = ""
    qty: int = 1


@dataclass(frozen=True)
class Order:
    client_order_id: str             # duy nhất, dùng để idempotent khi retry
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    created_at: datetime
    price: Optional[float] = None    # bắt buộc với LIMIT
    status: OrderStatus = OrderStatus.NEW
    broker: str = ""
    broker_order_id: Optional[str] = None
    filled_qty: int = 0
    tag: str = ""                    # "entry" | "stop" | "take_profit" | "flatten" | "emergency"

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError("qty phải > 0")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("Lệnh LO phải có giá")
        if not 0 <= self.filled_qty <= self.qty:
            raise ValueError("filled_qty ngoài [0, qty]")

    def with_status(self, status: OrderStatus, **changes) -> "Order":
        return replace(self, status=status, **changes)


@dataclass(frozen=True)
class Fill:
    client_order_id: str
    symbol: str
    side: Side
    qty: int
    price: float
    ts: datetime
    fee_vnd: float = 0.0
    broker: str = ""


@dataclass(frozen=True)
class Position:
    """Vị thế ròng trên MỘT broker. qty dương = Long, âm = Short, 0 = phẳng."""
    symbol: str
    qty: int = 0
    avg_price: float = 0.0
    realized_points: float = 0.0     # lãi/lỗ đã chốt, theo điểm × số HĐ
    broker: str = ""

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    def apply(self, fill: Fill) -> "Position":
        """Vị thế mới sau một lần khớp (không sửa đối tượng cũ)."""
        if fill.symbol != self.symbol:
            raise ValueError(f"Fill {fill.symbol} không khớp vị thế {self.symbol}")
        signed = fill.side.sign * fill.qty
        new_qty = self.qty + signed
        realized = self.realized_points
        if self.qty == 0 or (self.qty > 0) == (signed > 0):
            # mở mới hoặc cộng thêm cùng chiều → giá vốn bình quân
            total = abs(self.qty) + fill.qty
            avg = (abs(self.qty) * self.avg_price + fill.qty * fill.price) / total
            return replace(self, qty=new_qty, avg_price=avg)
        # giảm / đóng / đảo chiều
        closed = min(abs(self.qty), fill.qty)
        direction = 1 if self.qty > 0 else -1
        realized += closed * (fill.price - self.avg_price) * direction
        if new_qty == 0:
            avg = 0.0
        elif (new_qty > 0) != (self.qty > 0):
            avg = fill.price          # đảo chiều: phần dư mở mới tại giá khớp
        else:
            avg = self.avg_price
        return replace(self, qty=new_qty, avg_price=avg, realized_points=realized)

    def unrealized_points(self, mark: float) -> float:
        return self.qty * (mark - self.avg_price)


@dataclass
class AccountState:
    """Ảnh chụp trạng thái cho risk manager. Có thể thay đổi (runner cập nhật)."""
    positions: dict[str, Position] = field(default_factory=dict)   # key = f"{broker}:{symbol}"
    trades_today: int = 0
    consecutive_losses: int = 0
    realized_points_today: float = 0.0
    locked_for_day: bool = False

    def net_qty(self, symbol: str) -> int:
        """Vị thế ròng của một hợp đồng cộng trên MỌI broker (CLAUDE.md mục 3)."""
        return sum(p.qty for p in self.positions.values() if p.symbol == symbol)
