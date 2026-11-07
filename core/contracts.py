"""Mã hợp đồng tương lai VN30: tên, mã KRX, ngày đáo hạn, hợp đồng hiệu lực, roll.

Port từ `vnstock-dashboard/vnstock_futures_foreign.py` (contract_name, krx_symbol,
symbol_contract, third_thursday, candidates), thêm:
- ngày đáo hạn thực tế lùi theo lịch nghỉ (Tết 2026: VN30F2602 đáo hạn 13/02 thay 19/02);
- chọn hợp đồng F1M/F2M theo ngày và hợp đồng ĐANG GIAO DỊCH sau khi roll;
- chuẩn hoá mọi kiểu mã (VN30FYYMM, 41I1…, VN30F1M theo ngày).

Mã KRX: 4 (CKPS) 1 (HĐTL) I1 (VN30) + chữ năm + chữ tháng (1–9, A/B/C) + 000.
Chữ năm A=2020, B=2021 … — hai nguồn độc lập khớp nhau (quy định giao dịch MBS: 41I1F6000 =
06/2025; Vietstock: 41I1G9000 = 09/2026, GDCC 17/09/2026). Năm ≥ 2028 chưa đối chiếu thực tế —
lúc khởi động bot vẫn phải so với danh sách hợp đồng broker trả về (docs/broker_notes.md §0).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from core.calendar import TradingCalendar

_KRX_MONTHS = "123456789ABC"
_KRX_RE = re.compile(r"^41I1([A-Z])([1-9ABC])000$")
_NAME_RE = re.compile(r"^VN30F(\d{2})(\d{2})$")
_ROLLING_RE = re.compile(r"^VN30F([12])([MQ])$")
KRX_VERIFIED_YEARS = range(2024, 2028)


# ── Tên & mã ─────────────────────────────────────────────────────────────────
def contract_name(y: int, m: int) -> str:
    if not 1 <= m <= 12:
        raise ValueError(f"tháng không hợp lệ: {m}")
    return f"VN30F{y % 100:02d}{m:02d}"


def contract_ym(name: str) -> tuple[int, int]:
    match = _NAME_RE.match(str(name).strip().upper())
    if not match:
        raise ValueError(f"không phải tên hợp đồng VN30FYYMM: {name!r}")
    return 2000 + int(match.group(1)), int(match.group(2))


def krx_symbol(y: int, m: int) -> str:
    if not 2020 <= y <= 2045:
        raise ValueError(f"năm ngoài phạm vi mã KRX: {y}")
    if not 1 <= m <= 12:
        raise ValueError(f"tháng không hợp lệ: {m}")
    return f"41I1{chr(ord('A') + y - 2020)}{_KRX_MONTHS[m - 1]}000"


def krx_year_verified(y: int) -> bool:
    return y in KRX_VERIFIED_YEARS


def symbol_contract(sym: object) -> Optional[str]:
    """Tên cũ hoặc mã KRX → VN30FYYMM; None nếu không phải HĐTL VN30 có kỳ hạn cố định.

    Mã cuốn chiếu (VN30F1M, VN30F2Q…) cần ngày để giải — dùng `resolve_symbol`.
    """
    s = str(sym or "").strip().upper()
    if _NAME_RE.match(s):
        return s
    match = _KRX_RE.match(s)
    if match:
        y = 2020 + ord(match.group(1)) - ord("A")
        m = _KRX_MONTHS.index(match.group(2)) + 1
        return contract_name(y, m)
    return None


def is_vn30_future(sym: object) -> bool:
    s = str(sym or "").strip().upper()
    return bool(_NAME_RE.match(s) or _KRX_RE.match(s) or _ROLLING_RE.match(s))


# ── Ngày đáo hạn ─────────────────────────────────────────────────────────────
def third_thursday(y: int, m: int) -> date:
    """Thứ Năm tuần thứ ba theo lịch, CHƯA xét ngày nghỉ."""
    d = date(y, m, 1)
    return d + timedelta(days=(3 - d.weekday()) % 7 + 14)


def expiry_date(y: int, m: int, cal: TradingCalendar) -> date:
    """Ngày giao dịch cuối cùng: thứ Năm tuần 3, trùng ngày nghỉ ⇒ phiên liền trước."""
    return cal.previous_trading_day(third_thursday(y, m), inclusive=True)


def contract_expiry(name: str, cal: TradingCalendar) -> date:
    return expiry_date(*contract_ym(name), cal)


def _add_months(y: int, m: int, k: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + k
    return i // 12, i % 12 + 1


# ── Hợp đồng hiệu lực theo ngày ──────────────────────────────────────────────
def front_month(d: date, cal: TradingCalendar) -> str:
    """VN30F1M tại ngày d: hợp đồng tháng gần nhất CHƯA quá ngày đáo hạn (ngày đáo hạn vẫn là F1M)."""
    y, m = d.year, d.month
    if d > expiry_date(y, m, cal):
        y, m = _add_months(y, m, 1)
    return contract_name(y, m)


def listed_contracts(d: date, cal: TradingCalendar) -> dict[str, str]:
    """{F1M, F2M, F1Q, F2Q} → tên hợp đồng tại ngày d.

    VN30F luôn có 2 tháng gần nhất + 2 tháng cuối quý gần nhất tiếp theo.
    """
    y1, m1 = contract_ym(front_month(d, cal))
    y2, m2 = _add_months(y1, m1, 1)
    quarters: list[tuple[int, int]] = []
    k = 2
    while len(quarters) < 2:
        y, m = _add_months(y1, m1, k)
        if m % 3 == 0:
            quarters.append((y, m))
        k += 1
    return {
        "VN30F1M": contract_name(y1, m1),
        "VN30F2M": contract_name(y2, m2),
        "VN30F1Q": contract_name(*quarters[0]),
        "VN30F2Q": contract_name(*quarters[1]),
    }


def resolve_symbol(sym: object, d: date, cal: TradingCalendar) -> Optional[str]:
    """Mọi kiểu mã (VN30FYYMM, 41I1…, VN30F1M/2M/1Q/2Q) → tên hợp đồng tại ngày d."""
    fixed = symbol_contract(sym)
    if fixed:
        return fixed
    s = str(sym or "").strip().upper()
    if _ROLLING_RE.match(s):
        return listed_contracts(d, cal)[s]
    return None


def trading_contract(d: date, cal: TradingCalendar, roll_days_before_expiry: int = 1) -> str:
    """Hợp đồng BOT giao dịch tại ngày d.

    `roll_days_before_expiry = N`: trong N phiên CUỐI của F1M (tính cả ngày đáo hạn) bot giao
    dịch F2M để tránh thanh khoản mỏng và biến động ngày đáo hạn. N=1 ⇒ chỉ ngày đáo hạn;
    N=0 ⇒ giữ F1M tới hết ngày đáo hạn.
    """
    if roll_days_before_expiry < 0:
        raise ValueError("roll_days_before_expiry phải ≥ 0")
    f1 = front_month(d, cal)
    expiry = contract_expiry(f1, cal)
    sessions_left = cal.trading_days_between(d, expiry)   # số phiên còn lại sau d tới đáo hạn
    if roll_days_before_expiry > 0 and sessions_left < roll_days_before_expiry:
        return listed_contracts(d, cal)["VN30F2M"]
    return f1


def roll_date(name: str, cal: TradingCalendar, roll_days_before_expiry: int = 1) -> date:
    """Phiên đầu tiên bot đã chuyển KHỎI hợp đồng `name`."""
    expiry = contract_expiry(name, cal)
    if roll_days_before_expiry <= 0:
        return cal.next_trading_day(expiry)
    return cal.add_trading_days(expiry, -(roll_days_before_expiry - 1))


def is_expiry_day(d: date, cal: TradingCalendar) -> bool:
    return contract_expiry(front_month(d, cal), cal) == d


def next_contract_roll_date(d: date, cal: TradingCalendar, roll_days_before_expiry: int = 1) -> date:
    """Ngày roll kế tiếp tính từ d (có thể là chính d)."""
    current = trading_contract(d, cal, roll_days_before_expiry)
    return roll_date(current, cal, roll_days_before_expiry)
