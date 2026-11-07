"""Lưu / nạp bộ đếm rủi ro trong ngày — khởi động lại giữa phiên KHÔNG được xoá giới hạn.

Không lưu: vị thế và lệnh chờ (luôn đối soát lại từ broker lúc khởi động — broker là sự thật).
Lưu: số lượt giao dịch, chuỗi thua, lãi/lỗ và phí trong ngày, khoá ngày, kill-switch, khoá khẩn
cấp, id lệnh đã duyệt (chống trùng id sau khi khởi động lại).
Sang ngày khác: chỉ giữ khoá do người phải xoá tay (kill-switch, khoá khẩn cấp).
File hỏng ⇒ ném ValueError: không được âm thầm chạy với bộ đếm về 0.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from core.models import AccountState
from utils.atomic_io import write_json_atomic

_DAILY = ("trades_today", "consecutive_losses", "realized_points_today", "fees_points_today",
          "locked_for_day")
_MANUAL = ("kill_reason", "emergency_reason")


def save_daily_state(state: AccountState, path: Path) -> None:
    payload = {"trading_day": state.trading_day.isoformat() if state.trading_day else None,
               **{k: getattr(state, k) for k in _DAILY + _MANUAL},
               "approved_order_ids": sorted(state.approved_order_ids)}
    write_json_atomic(payload, path)


def load_daily_state(path: Path, state: AccountState) -> bool:
    """Nạp vào `state` (đã có trading_day). True nếu file cùng ngày và đã nạp bộ đếm."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        saved_day = date.fromisoformat(data["trading_day"]) if data.get("trading_day") else None
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"File trạng thái {p} hỏng — kiểm tra tay trước khi chạy: {exc}") from exc
    for k in _MANUAL:
        if data.get(k) and not getattr(state, k):
            setattr(state, k, data[k])
    if saved_day != state.trading_day:
        return False
    for k in _DAILY:
        if k in data:
            setattr(state, k, data[k])
    state.approved_order_ids |= set(data.get("approved_order_ids", []))
    return True
