"""Nạp và kiểm tra cấu hình (config/settings.yaml, config/holidays.yaml).

Quy ước: giá trị `null` trong YAML nghĩa là CHƯA ĐIỀN. Code cần giá trị đó gọi
`require("fees.broker_fee_vnd_per_contract")` — thiếu thì ném `MissingSetting`
thay vì tự đoán (CLAUDE.md mục 2).

Chạy `python -m core.settings` để in danh sách khoá còn thiếu.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
DEFAULT_HOLIDAYS_PATH = REPO_ROOT / "config" / "holidays.yaml"


class MissingSetting(KeyError):
    """Khoá cấu hình chưa điền (null) hoặc không tồn tại."""


class Settings:
    """Bọc dict cấu hình, truy cập bằng đường dẫn chấm: `s.get("risk.max_contracts")`."""

    def __init__(self, data: Mapping[str, Any]):
        self._data = dict(data or {})

    @property
    def raw(self) -> dict:
        return self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value is None:
            raise MissingSetting(
                f"Chưa điền '{dotted}' trong config/settings.yaml — "
                "điền giá trị thật trước khi chạy module này."
            )
        return value

    def missing(self) -> list[str]:
        """Mọi khoá lá đang là null (chưa điền)."""
        return [k for k, v in _walk(self._data) if v is None]

    def path(self, dotted: str) -> Path:
        """Đường dẫn trong settings; tương đối thì tính từ gốc repo."""
        p = Path(self.require(dotted))
        return p if p.is_absolute() else REPO_ROOT / p


def _walk(node: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            yield from _walk(value, dotted + ".")
        else:
            yield dotted, value


def load_settings(path: Path | str | None = None) -> Settings:
    p = Path(path) if path else DEFAULT_SETTINGS_PATH
    with open(p, encoding="utf-8") as fh:
        return Settings(yaml.safe_load(fh) or {})


def load_holidays(path: Path | str | None = None) -> dict[date, str]:
    """{ngày: mô tả}. Khoá phải là chuỗi YYYY-MM-DD (YAML có thể tự đọc thành date)."""
    p = Path(path) if path else DEFAULT_HOLIDAYS_PATH
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    out: dict[date, str] = {}
    for key, desc in raw.items():
        d = key if isinstance(key, date) else date.fromisoformat(str(key))
        out[d] = str(desc or "")
    return out


def main() -> int:
    s = load_settings()
    missing = s.missing()
    if not missing:
        print("✅ settings.yaml đã điền đủ.")
        return 0
    print(f"⚠️  Còn {len(missing)} khoá chưa điền trong config/settings.yaml:")
    for key in missing:
        print(f"   - {key}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
