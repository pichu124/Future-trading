"""Nhật ký regime shadow của nhóm AI CHỈ để nghiên cứu (CLAUDE.md mục 8: LLM không nằm trong đường ra quyết định
live). Không module nào của bot được import công cụ shadow, đọc thư mục nhận định hay khoá settings của nó.

Muốn dùng nhận định AI trong bot (dù chỉ để GIẢM rủi ro) ⇒ phải sửa CLAUDE.md mục 8 trước, rồi mới bỏ test này.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOT_PACKAGES = ("core", "feeds", "recorder", "strategy", "risk", "brokers", "backtest", "live", "monitoring", "utils")
FORBIDDEN = ("regime_call", "regime_shadow", "score_regime")


def test_bot_packages_never_touch_regime_shadow():
    hits = []
    for pkg in BOT_PACKAGES:
        for p in sorted((REPO / pkg).rglob("*.py")):
            text = p.read_text(encoding="utf-8")
            hits += [f"{p.relative_to(REPO)}: {w}" for w in FORBIDDEN if w in text]
    assert hits == []


def test_all_bot_packages_exist_so_the_scan_is_not_vacuous():
    assert all((REPO / pkg).is_dir() and list((REPO / pkg).rglob("*.py")) for pkg in BOT_PACKAGES)


def test_shadow_tools_do_not_import_execution_code():
    """Chiều ngược lại: công cụ shadow không được kéo theo broker / runner / risk (không có đường nào tới lệnh)."""
    for name in ("regime_call.py", "score_regime_calls.py"):
        text = (REPO / "scripts" / name).read_text(encoding="utf-8")
        for banned in ("brokers", "live.", "risk.", "feeds."):
            assert f"from {banned}" not in text and f"import {banned}" not in text, (name, banned)
