"""Risk manager — cổng DUY NHẤT giữa Signal và Order (CLAUDE.md mục 2, 5).

Hai việc:

1. `approve(order, state) -> Approved | Rejected(code, reason)` — gọi TRƯỚC khi gửi bất kỳ lệnh
   nào tới broker. Lệnh được duyệt được ghi ngay vào `state.working_orders` (chặn hai tín hiệu
   liền nhau cùng lọt trước khi broker kịp báo).
2. `check(state) -> list[RiskAction]` — runner gọi định kỳ (mỗi tick / mỗi giây). Trả về việc
   PHẢI làm: KILL, FLATTEN_ALL, FLATTEN_SYMBOL, EMERGENCY_HEDGE. Risk manager không tự gửi lệnh.

Phân loại lệnh theo TÁC DỤNG lên net position (cộng mọi broker), không theo `tag`:
- **Lệnh giảm** (ngược chiều net, qty ≤ |net|): luôn được duyệt — kể cả khi đã khoá ngày,
  kill-switch, ngoài giờ — vì chặn lệnh thoát còn nguy hiểm hơn. Chỉ kiểm id trùng và không cho
  hai lệnh đóng (exit/flatten/emergency/unwind) hoặc hai lệnh bảo vệ cùng loại cùng chiều trên cùng
  broker. Đảo chiều ngoài ý muốn (stop và TP cùng khớp) do ExecutionManager phát hiện và đóng.
- **Lệnh mở** (mọi lệnh còn lại, gồm cả đảo chiều): qua toàn bộ luật mục 5.

Đơn vị lãi/lỗ: ĐIỂM × số hợp đồng (như `Position.realized_points`); phí VNĐ quy ra điểm bằng
hệ số nhân. Ngưỡng lỗ ngày tính SAU phí và gồm cả lỗ chưa chốt.

Bảo vệ stop-loss: vị thế net ≠ 0 phải được phủ bởi lệnh `tag="stop"` ngược chiều mà broker
ĐÃ XÁC NHẬN (PENDING / PARTIALLY_FILLED; NEW chưa tính). Thiếu quá `stop_grace_seconds` ⇒
FLATTEN_SYMBOL ("không có stop → huỷ vị thế").
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import ClassVar, Optional, Union

from core.clock import MarketClock, SessionSchedule, SessionState
from core.models import (MAX_CLIENT_ORDER_ID_LEN, AccountState, Fill, Order, OrderStatus,
                         OrderType, Position, Side)
from core.settings import REPO_ROOT, Settings

STOP_TAG = "stop"
CLOSE_TAGS = ("exit", "flatten", "emergency", "unwind")
PROTECT_TAGS = ("stop", "take_profit")
DEFAULT_KILL_FILE = REPO_ROOT / "KILL"
_ACKED = (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
_EPS = 1e-9


# ------------------------------------------------------------------ kết quả ----

class RejectCode(str, Enum):
    UNKNOWN_BROKER = "UNKNOWN_BROKER"
    DUPLICATE_ORDER_ID = "DUPLICATE_ORDER_ID"
    DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
    MAX_CONTRACTS = "MAX_CONTRACTS"
    KILLED = "KILLED"
    STALE_STATE = "STALE_STATE"
    LOCKED_FOR_DAY = "LOCKED_FOR_DAY"
    DAILY_LOSS = "DAILY_LOSS"
    NO_MARK_PRICE = "NO_MARK_PRICE"
    FEED_UNHEALTHY = "FEED_UNHEALTHY"
    NOT_PRIMARY_BROKER = "NOT_PRIMARY_BROKER"
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    SESSION_CLOSED = "SESSION_CLOSED"
    OPENING_WINDOW = "OPENING_WINDOW"
    AFTER_LAST_ENTRY = "AFTER_LAST_ENTRY"
    EXPIRY_DAY = "EXPIRY_DAY"
    MAX_TRADES = "MAX_TRADES"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    NO_STOP_LOSS = "NO_STOP_LOSS"
    INVALID_STOP_LOSS = "INVALID_STOP_LOSS"
    EMERGENCY = "EMERGENCY"
    UNWIND_INVALID = "UNWIND_INVALID"
    DUPLICATE_CLOSE = "DUPLICATE_CLOSE"


@dataclass(frozen=True)
class Approved:
    order: Order
    ok: ClassVar[bool] = True


@dataclass(frozen=True)
class Rejected:
    order: Order
    code: RejectCode
    reason: str
    ok: ClassVar[bool] = False

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.order.client_order_id}: {self.reason}"


Decision = Union[Approved, Rejected]


class ActionKind(str, Enum):
    KILL = "KILL"                        # huỷ mọi lệnh chờ, đóng mọi vị thế, dừng bot
    FLATTEN_ALL = "FLATTEN_ALL"          # huỷ lệnh chờ + đóng mọi vị thế
    FLATTEN_SYMBOL = "FLATTEN_SYMBOL"    # đóng vị thế một hợp đồng (thiếu stop)
    EMERGENCY_HEDGE = "EMERGENCY_HEDGE"  # broker mất kết nối khi có vị thế ⇒ live/emergency.py


@dataclass(frozen=True)
class RiskAction:
    kind: ActionKind
    reason: str
    symbol: Optional[str] = None
    broker: Optional[str] = None


# ------------------------------------------------------------------ cấu hình ----

def _parse_hhmm(value) -> time:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Giờ phải dạng HH:MM, nhận {value!r}") from exc


def _number(name: str, value, minimum: float, integer: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"risk.{name} phải là số, nhận {value!r}")
    if integer and int(value) != value:
        raise ValueError(f"risk.{name} phải là số nguyên, nhận {value!r}")
    if value < minimum:
        raise ValueError(f"risk.{name} phải ≥ {minimum}, nhận {value!r}")
    return int(value) if integer else float(value)


@dataclass(frozen=True)
class RiskLimits:
    max_contracts: int
    max_daily_loss_points: float
    max_trades_per_day: int
    max_consecutive_losses: int
    no_entry_open_minutes: float
    last_entry_time: time
    flatten_time: time
    trade_on_expiry_day: bool
    broker_timeout_seconds: float
    stop_grace_seconds: float
    flatten_on_feed_silence_seconds: float
    primary_broker: str
    multiplier_vnd_per_point: float

    @classmethod
    def from_settings(cls, s: Settings) -> "RiskLimits":
        """Mọi khoá bắt buộc; còn null ⇒ MissingSetting (không tự đoán)."""
        r = lambda k: s.require(f"risk.{k}")   # noqa: E731
        on_expiry = r("trade_on_expiry_day")
        if not isinstance(on_expiry, bool):
            raise ValueError(f"risk.trade_on_expiry_day phải true/false, nhận {on_expiry!r}")
        limits = cls(
            max_contracts=_number("max_contracts", r("max_contracts"), 1, True),
            max_daily_loss_points=_number("max_daily_loss_points", r("max_daily_loss_points"),
                                          _EPS, False),
            max_trades_per_day=_number("max_trades_per_day", r("max_trades_per_day"), 1, True),
            max_consecutive_losses=_number("max_consecutive_losses",
                                           r("max_consecutive_losses"), 1, True),
            no_entry_open_minutes=_number("no_entry_open_minutes", r("no_entry_open_minutes"),
                                          0, False),
            last_entry_time=_parse_hhmm(r("last_entry_time")),
            flatten_time=_parse_hhmm(r("flatten_time")),
            trade_on_expiry_day=on_expiry,
            broker_timeout_seconds=_number("broker_timeout_seconds",
                                           r("broker_timeout_seconds"), _EPS, False),
            stop_grace_seconds=_number("stop_grace_seconds", r("stop_grace_seconds"), 0, False),
            flatten_on_feed_silence_seconds=_number("flatten_on_feed_silence_seconds",
                                                    r("flatten_on_feed_silence_seconds"), _EPS, False),
            primary_broker=str(s.require("brokers.primary")),
            multiplier_vnd_per_point=_number("multiplier",
                                             s.require("contract.multiplier_vnd_per_point"),
                                             _EPS, False),
        )
        schedule = SessionSchedule.from_mapping(s.get("session", {}))
        if not limits.last_entry_time < limits.flatten_time:
            raise ValueError("risk.last_entry_time phải TRƯỚC risk.flatten_time")
        if not limits.flatten_time < schedule.atc_start:
            raise ValueError("risk.flatten_time phải TRƯỚC giờ ATC (không huỷ/sửa được lệnh trong ATC)")
        return limits


# ------------------------------------------------------------ id lệnh duy nhất ----

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = _B36[r] + out
        if n == 0:
            return out


class OrderIdFactory:
    """Sinh `client_order_id` duy nhất ≤ 20 ký tự: prefix + yymmddHHMMSS + số thứ tự base36.

    Dùng LẠI cùng id khi retry một lệnh (idempotent ở SSI); lệnh mới luôn lấy id mới.
    Hai tiến trình khởi động trong cùng một giây có thể trùng id ⇒ chặn bằng InstanceLock và
    `state.approved_order_ids`.
    """

    def __init__(self, prefix: str = "B"):
        if not prefix or len(prefix) > 3 or not prefix.isalnum():
            raise ValueError("prefix phải 1–3 ký tự chữ/số")
        self.prefix = prefix
        self._seq = 0

    def next(self, ts: datetime) -> str:
        self._seq += 1
        cid = f"{self.prefix}{ts:%y%m%d%H%M%S}{_base36(self._seq)}"
        if len(cid) > MAX_CLIENT_ORDER_ID_LEN:
            raise ValueError("Hết dải số thứ tự id lệnh trong phiên")
        return cid


# --------------------------------------------------------------- risk manager ----

def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _remaining(o: Order) -> int:
    return o.qty - o.filled_qty


class RiskManager:
    def __init__(self, limits: RiskLimits, clock: MarketClock,
                 kill_file: Optional[Path] = DEFAULT_KILL_FILE):
        self.limits = limits
        self.clock = clock
        self.kill_file = Path(kill_file) if kill_file else None

    @classmethod
    def from_settings(cls, settings: Settings, clock: MarketClock,
                      kill_file: Optional[Path] = DEFAULT_KILL_FILE) -> "RiskManager":
        return cls(RiskLimits.from_settings(settings), clock, kill_file)

    # ---------------------------------------------------------------- duyệt lệnh --

    def approve(self, order: Order, state: AccountState) -> Decision:
        decision = self._decide(order, state)
        if isinstance(decision, Approved):
            state.approved_order_ids.add(order.client_order_id)
            state.working_orders[order.client_order_id] = order
        return decision

    def _decide(self, order: Order, state: AccountState) -> Decision:
        L = self.limits
        rej = lambda code, msg: Rejected(order, code, msg)   # noqa: E731
        now = self.clock.now()
        self._refresh_kill(state)

        if not order.broker:
            return rej(RejectCode.UNKNOWN_BROKER, "Lệnh chưa gắn broker")
        if order.client_order_id in state.approved_order_ids:
            return rej(RejectCode.DUPLICATE_ORDER_ID,
                       f"client_order_id {order.client_order_id} đã được duyệt hôm nay — retry phải "
                       "dùng kết quả cũ, lệnh mới phải lấy id mới")

        net = state.net_qty(order.symbol)
        signed = order.side.sign * order.qty
        reducing = net != 0 and _sign(signed) != _sign(net) and order.qty <= abs(net)

        if reducing:
            # KHÔNG áp trần "kịch bản xấu nhất" cho lệnh giảm: stop + TP + lệnh đóng/đối ứng cộng lại
            # luôn vượt trần, và chặn chúng đúng lúc cần thoát còn nguy hiểm hơn (golive #1, #7).
            # Thay bằng: không hai lệnh đóng (hoặc hai lệnh bảo vệ cùng loại) cùng chiều trên cùng broker.
            group = CLOSE_TAGS if order.tag in CLOSE_TAGS else (
                (order.tag,) if order.tag in PROTECT_TAGS else None)
            if group:
                dup = next((o for o in state.working_orders.values()
                            if o.symbol == order.symbol and o.side is order.side
                            and o.broker == order.broker and o.tag in group), None)
                if dup is not None:
                    return rej(RejectCode.DUPLICATE_CLOSE,
                               f"Đã có lệnh {dup.tag} {dup.client_order_id} cùng chiều trên {dup.broker}")
            return Approved(order)

        exposure = self._exposure_breach(order, state, net)

        # ---- lệnh MỞ vị thế: toàn bộ luật mục 5 ----
        if state.kill_reason:
            return rej(RejectCode.KILLED, f"Kill-switch đang bật ({state.kill_reason})")
        if state.emergency_reason:
            return rej(RejectCode.EMERGENCY,
                       f"Đang trong quy trình khẩn cấp ({state.emergency_reason}) — chờ người kiểm tra")
        if state.trading_day != now.date():
            return rej(RejectCode.STALE_STATE,
                       f"Sổ sách của ngày {state.trading_day}, hôm nay {now.date()} — runner phải "
                       "gọi new_day() trước")
        if state.locked_for_day:
            return rej(RejectCode.LOCKED_FOR_DAY, "Bot đã khoá đến hết ngày (chạm ngưỡng lỗ ngày)")
        pnl = self.daily_pnl_points(state)
        if pnl is None:
            return rej(RejectCode.NO_MARK_PRICE,
                       "Thiếu giá đánh dấu cho vị thế đang mở — không tính được lãi/lỗ ngày")
        if pnl <= -L.max_daily_loss_points + _EPS:
            state.locked_for_day = True
            return rej(RejectCode.DAILY_LOSS,
                       f"Lãi/lỗ ngày {pnl:.1f} điểm ≤ -{L.max_daily_loss_points:g} — khoá đến hết ngày")
        if not state.feed_healthy:
            return rej(RejectCode.FEED_UNHEALTHY, "Feed lệch giá hoặc im lặng — ngừng mở vị thế mới")
        if order.broker != L.primary_broker:
            return rej(RejectCode.NOT_PRIMARY_BROKER,
                       f"Chỉ mở vị thế ở broker chính '{L.primary_broker}', lệnh gửi '{order.broker}'")
        if not self._broker_alive(state, order.broker, now):
            return rej(RejectCode.BROKER_DISCONNECTED,
                       f"Broker '{order.broker}' im lặng > {L.broker_timeout_seconds:g}s")

        session = self.clock.session_state(now)
        if session is not SessionState.CONTINUOUS:
            return rej(RejectCode.SESSION_CLOSED, f"Chỉ mở vị thế trong phiên liên tục (đang {session.value})")
        since_open = self.clock.minutes_since_continuous_open(now)
        if since_open is not None and since_open < L.no_entry_open_minutes:
            return rej(RejectCode.OPENING_WINDOW,
                       f"Mới {since_open:.1f} phút từ đầu phiên, cần ≥ {L.no_entry_open_minutes:g}")
        if now.time() >= L.last_entry_time:
            return rej(RejectCode.AFTER_LAST_ENTRY,
                       f"Đã qua giờ vào lệnh cuối {L.last_entry_time:%H:%M}")
        if not L.trade_on_expiry_day and self.clock.is_expiry_day(now.date()):
            return rej(RejectCode.EXPIRY_DAY, "Ngày đáo hạn — không mở vị thế (trade_on_expiry_day=false)")
        if state.trades_today >= L.max_trades_per_day:
            return rej(RejectCode.MAX_TRADES,
                       f"Đã {state.trades_today}/{L.max_trades_per_day} lượt giao dịch hôm nay")
        if state.consecutive_losses >= L.max_consecutive_losses:
            return rej(RejectCode.CONSECUTIVE_LOSSES,
                       f"Đã thua {state.consecutive_losses} lượt liên tiếp (tối đa {L.max_consecutive_losses})")

        if order.stop_loss is None:
            return rej(RejectCode.NO_STOP_LOSS, "Lệnh mở vị thế phải kèm stop_loss")
        ref = order.price if order.order_type is OrderType.LIMIT else state.last_prices.get(order.symbol)
        if ref is None:
            return rej(RejectCode.NO_MARK_PRICE, "Không có giá tham chiếu để kiểm stop_loss")
        if (order.stop_loss - ref) * order.side.sign >= 0:
            where = "dưới" if order.side is Side.BUY else "trên"
            return rej(RejectCode.INVALID_STOP_LOSS,
                       f"stop_loss {order.stop_loss} phải nằm {where} giá tham chiếu {ref}")

        if exposure:
            return rej(RejectCode.MAX_CONTRACTS, exposure)
        dup = next((o for o in state.working_orders.values()
                    if o.symbol == order.symbol and o.side is order.side), None)
        if dup is not None:
            return rej(RejectCode.DUPLICATE_ENTRY,
                       f"Đang có lệnh {dup.client_order_id} cùng chiều {order.side.value} chờ khớp")
        return Approved(order)

    def approve_unwind(self, orders: list[Order], state: AccountState) -> list[Decision]:
        """Duyệt CẢ BỘ lệnh gỡ vị thế chéo broker (sau đối ứng khẩn cấp: Long ở broker A, Short ở B).

        Đạt khi: mỗi lệnh giảm vị thế trên CHÍNH broker của nó (ngược chiều, không vượt), tổng các
        lệnh theo từng hợp đồng = 0 (net không đổi), id chưa dùng. Không áp luật giờ/khoá/kill vì
        bộ lệnh chỉ làm giảm tổng vị thế. Một lệnh hỏng ⇒ từ chối tất cả (không gửi lẻ một chân).
        """
        def reject_all(msg: str) -> list[Decision]:
            return [Rejected(o, RejectCode.UNWIND_INVALID, msg) for o in orders]
        if not orders:
            return []
        ids = [o.client_order_id for o in orders]
        if len(set(ids)) != len(ids) or any(i in state.approved_order_ids for i in ids):
            return reject_all("client_order_id trùng")
        per_symbol: dict[str, int] = {}
        for o in orders:
            if not o.broker:
                return reject_all("lệnh gỡ thiếu broker")
            pos = state.positions.get(f"{o.broker}:{o.symbol}")
            q = pos.qty if pos else 0
            if q == 0 or _sign(q) == o.side.sign or o.qty > abs(q):
                return reject_all(f"chân {o.broker} {o.side.value} {o.qty} không giảm vị thế {q:+d} "
                                  f"của chính broker đó")
            per_symbol[o.symbol] = per_symbol.get(o.symbol, 0) + o.side.sign * o.qty
        if any(v != 0 for v in per_symbol.values()):
            return reject_all(f"tổng các chân phải giữ nguyên net, đang lệch {per_symbol}")
        for o in orders:
            state.approved_order_ids.add(o.client_order_id)
            state.working_orders[o.client_order_id] = o
        return [Approved(o) for o in orders]

    def _exposure_breach(self, order: Order, state: AccountState, net: int) -> Optional[str]:
        """Trần hợp đồng theo kịch bản xấu nhất: mọi lệnh chờ cùng chiều đều khớp."""
        pending = sum(_remaining(o) for o in state.working_orders.values()
                      if o.symbol == order.symbol and o.side is order.side)
        worst = net + order.side.sign * (pending + order.qty)
        if abs(worst) > self.limits.max_contracts:
            return (f"Net {net:+d}, lệnh chờ cùng chiều {pending}, lệnh này {order.qty} ⇒ "
                    f"tối đa {worst:+d} HĐ > max_contracts {self.limits.max_contracts}")
        return None

    # ---------------------------------------------------------------- giám sát --

    def check(self, state: AccountState) -> list[RiskAction]:
        L = self.limits
        now = self.clock.now()
        self._refresh_kill(state)
        actions: list[RiskAction] = []

        if state.kill_reason:
            actions.append(RiskAction(ActionKind.KILL, f"Kill-switch: {state.kill_reason}"))
            return actions + self._emergencies(state, now)

        exposed = self._has_exposure(state)
        flatten_reasons = []
        pnl = self.daily_pnl_points(state)
        if pnl is not None and pnl <= -L.max_daily_loss_points + _EPS and not state.locked_for_day:
            state.locked_for_day = True
            flatten_reasons.append(f"Lỗ ngày {pnl:.1f} điểm chạm ngưỡng -{L.max_daily_loss_points:g}"
                                   " — khoá đến hết ngày")
        elif state.locked_for_day and exposed:
            flatten_reasons.append("Bot đã khoá trong ngày nhưng còn vị thế/lệnh chờ")
        if now.time() >= L.flatten_time and exposed:
            flatten_reasons.append(f"Đến giờ flatten {L.flatten_time:%H:%M} (trước ATC)")
        silent = self._data_silence(state, now)
        if silent is not None and any(p.qty != 0 for p in state.positions.values()):
            flatten_reasons.append(f"Mất dữ liệu thị trường {silent:.0f}s (> "
                                   f"{L.flatten_on_feed_silence_seconds:g}s) khi đang có vị thế — stop đang mù")
        if flatten_reasons:
            actions.append(RiskAction(ActionKind.FLATTEN_ALL, "; ".join(flatten_reasons)))

        for symbol in self._symbols(state):
            self._refresh_protection(state, symbol, now)
        if not flatten_reasons:
            for symbol, since in sorted(state.unprotected_since.items()):
                if (now - since).total_seconds() >= L.stop_grace_seconds:
                    actions.append(RiskAction(
                        ActionKind.FLATTEN_SYMBOL,
                        f"Net {state.net_qty(symbol):+d} không có stop đã xác nhận suốt "
                        f"{(now - since).total_seconds():.1f}s (≥ {L.stop_grace_seconds:g}s)",
                        symbol=symbol))
        return actions + self._emergencies(state, now)

    def _data_silence(self, state: AccountState, now: datetime) -> Optional[float]:
        """Số giây không có dữ liệu thị trường nếu VƯỢT ngưỡng (chỉ trong phiên liên tục), không thì None.

        Mốc tính = muộn nhất của: lần cuối có dữ liệu, đầu đoạn phiên liên tục hiện tại (09:00 /
        13:00) ⇒ nghỉ trưa không tính, vào phiên chiều có thời gian chờ tick đầu tiên.
        """
        if self.clock.session_state(now) is not SessionState.CONTINUOUS:
            return None
        sched = self.clock.schedule
        seg = sched.continuous_pm_start if now.time() >= sched.continuous_pm_start else sched.continuous_am_start
        ref = datetime.combine(now.date(), seg)
        if state.market_data_at is not None and state.market_data_at > ref:
            ref = state.market_data_at
        gap = (now - ref).total_seconds()
        return gap if gap > self.limits.flatten_on_feed_silence_seconds else None

    def _emergencies(self, state: AccountState, now: datetime) -> list[RiskAction]:
        brokers = sorted({p.broker for p in state.positions.values() if p.qty != 0})
        out = []
        for b in brokers:
            if not self._broker_alive(state, b, now):
                last = state.broker_last_seen.get(b)
                ago = f"{(now - last).total_seconds():.1f}s" if last else "chưa từng"
                out.append(RiskAction(
                    ActionKind.EMERGENCY_HEDGE,
                    f"Broker '{b}' mất kết nối ({ago} > {self.limits.broker_timeout_seconds:g}s) "
                    "khi đang có vị thế", broker=b))
        return out

    def broker_alive(self, state: AccountState, broker: str, now: Optional[datetime] = None) -> bool:
        return self._broker_alive(state, broker, now or self.clock.now())

    def _broker_alive(self, state: AccountState, broker: str, now: datetime) -> bool:
        last = state.broker_last_seen.get(broker)
        return last is not None and (now - last).total_seconds() <= self.limits.broker_timeout_seconds

    # ------------------------------------------------------------------ sổ sách --

    def record_fill(self, state: AccountState, fill: Fill) -> Position:
        """Cập nhật vị thế, lãi/lỗ, phí, lượt giao dịch, chuỗi thua và lệnh chờ sau một lần khớp.

        Một "lượt" bắt đầu khi net (cộng mọi broker) rời 0 và kết thúc khi net về 0 hoặc đổi
        chiều. Lượt lỗ (< 0 điểm SAU PHÍ) tăng chuỗi thua; lãi đặt lại 0; hoà giữ nguyên.
        """
        if not fill.broker:
            raise ValueError("Fill thiếu broker — không biết cộng vào vị thế nào")
        key = f"{fill.broker}:{fill.symbol}"
        old = state.positions.get(key, Position(fill.symbol, broker=fill.broker))
        net_before = state.net_qty(fill.symbol)
        new = old.apply(fill)
        state.positions[key] = new
        net_after = state.net_qty(fill.symbol)

        delta = new.realized_points - old.realized_points
        state.realized_points_today += delta
        state.fees_points_today += fill.fee_vnd / self.limits.multiplier_vnd_per_point

        fee_pts = fill.fee_vnd / self.limits.multiplier_vnd_per_point
        if net_before == 0:
            if net_after != 0:
                self._start_round_trip(state, fill.symbol)
                state.round_trip_points[fill.symbol] -= fee_pts
        else:
            rt = state.round_trip_points.get(fill.symbol, 0.0) + delta - fee_pts
            if net_after == 0 or _sign(net_after) != _sign(net_before):
                if rt < -_EPS:
                    state.consecutive_losses += 1
                elif rt > _EPS:
                    state.consecutive_losses = 0
                state.round_trip_points.pop(fill.symbol, None)
                if net_after != 0:
                    self._start_round_trip(state, fill.symbol)
            else:
                state.round_trip_points[fill.symbol] = rt

        o = state.working_orders.get(fill.client_order_id)
        if o is not None:
            filled = min(o.qty, o.filled_qty + fill.qty)
            if filled >= o.qty:
                del state.working_orders[fill.client_order_id]
            else:
                state.working_orders[fill.client_order_id] = o.with_status(
                    OrderStatus.PARTIALLY_FILLED, filled_qty=filled)

        self._refresh_protection(state, fill.symbol, fill.ts)
        return new

    @staticmethod
    def _start_round_trip(state: AccountState, symbol: str) -> None:
        state.trades_today += 1
        state.round_trip_points[symbol] = 0.0

    def on_order_update(self, state: AccountState, order: Order) -> None:
        """Broker báo trạng thái lệnh (xác nhận, khớp một phần, huỷ, từ chối…)."""
        if order.status.is_terminal:
            state.working_orders.pop(order.client_order_id, None)
        else:
            state.working_orders[order.client_order_id] = order
        self._refresh_protection(state, order.symbol, self.clock.now())

    def new_day(self, state: AccountState, d: date) -> None:
        """Đặt lại bộ đếm trong ngày. Vị thế thật, kill-switch và khoá khẩn cấp KHÔNG bị xoá."""
        state.trading_day = d
        state.trades_today = 0
        state.consecutive_losses = 0
        state.realized_points_today = 0.0
        state.fees_points_today = 0.0
        state.locked_for_day = False
        state.approved_order_ids.clear()
        open_symbols = {p.symbol for p in state.positions.values() if p.qty != 0}
        state.round_trip_points = {s: v for s, v in state.round_trip_points.items()
                                   if s in open_symbols}

    def daily_pnl_points(self, state: AccountState) -> Optional[float]:
        """Lãi/lỗ trong ngày SAU phí, gồm phần chưa chốt. None nếu thiếu giá đánh dấu."""
        total = state.realized_points_today - state.fees_points_today
        for p in state.positions.values():
            if p.qty == 0:
                continue
            mark = state.last_prices.get(p.symbol)
            if mark is None:
                return None
            total += p.unrealized_points(mark)
        return total

    # ------------------------------------------------------------------ kill-switch --

    def kill(self, state: AccountState, reason: str) -> None:
        """Kill-switch thủ công (vd lệnh Telegram /kill)."""
        if not state.kill_reason:
            state.kill_reason = reason or "kill"

    def clear_kill(self, state: AccountState) -> None:
        """Chỉ người dùng gọi, sau khi đã kiểm tra. Ném lỗi nếu file KILL vẫn còn."""
        if self.kill_file is not None and self.kill_file.exists():
            raise RuntimeError(f"Xoá file {self.kill_file} trước khi tắt kill-switch")
        state.kill_reason = None

    def clear_emergency(self, state: AccountState) -> None:
        """Chỉ người dùng gọi sau khi đã đối soát vị thế trên mọi broker."""
        state.emergency_reason = None

    def _refresh_kill(self, state: AccountState) -> None:
        if not state.kill_reason and self.kill_file is not None and self.kill_file.exists():
            state.kill_reason = f"file {self.kill_file.name}"

    # ------------------------------------------------------------------ nội bộ --

    @staticmethod
    def _has_exposure(state: AccountState) -> bool:
        return bool(state.working_orders) or any(p.qty != 0 for p in state.positions.values())

    @staticmethod
    def _symbols(state: AccountState) -> set[str]:
        return ({p.symbol for p in state.positions.values() if p.qty != 0}
                | set(state.unprotected_since))

    @staticmethod
    def _refresh_protection(state: AccountState, symbol: str, ts: datetime) -> None:
        net = state.net_qty(symbol)
        if net == 0:
            state.unprotected_since.pop(symbol, None)
            return
        cover = sum(_remaining(o) for o in state.working_orders.values()
                    if o.symbol == symbol and o.tag == STOP_TAG and o.status in _ACKED
                    and o.side.sign == -_sign(net))
        if cover >= abs(net):
            state.unprotected_since.pop(symbol, None)
        else:
            state.unprotected_since.setdefault(symbol, ts)
