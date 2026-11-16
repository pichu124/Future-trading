"""Ghi Parquet/JSON an toàn: ghi ra file tạm rồi tráo (`os.replace`).

Port từ `vnstock-dashboard/vnstock_parquet_io.py`. Lý do: `pq.write_table` ghi
thẳng sẽ cắt file về 0 byte trước khi chép nội dung mới — process chết giữa
chừng (mất điện, Task Scheduler kill, Ctrl+C) để lại file cụt. Tráo nguyên tử
nghĩa là file trên đĩa luôn là bản cũ đầy đủ hoặc bản mới đầy đủ.

Windows khoá file tạm thời (antivirus, Explorer, Dropbox) ⇒ `os.replace` có thể
ném PermissionError ngay; ta thử lại với backoff, hết lượt thì ném lỗi cuối —
caller phải biết lần ghi đó đã trượt.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

REPLACE_RETRIES = 6
REPLACE_BACKOFF_S = 0.01          # 10ms, 20ms, 40ms, 80ms, 160ms, 320ms


def _tmp_path(out_path: Path) -> Path:
    # pid + thread id: nhiều tiến trình/luồng không giẫm lên nhau. Đuôi .tmp
    # (không phải .parquet/.json) nên glob dữ liệu không bao giờ nhặt nhầm.
    return out_path.with_name(f"{out_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def replace_with_retry(tmp: Path, dest: Path, retries: int = REPLACE_RETRIES,
                       backoff_s: float = REPLACE_BACKOFF_S) -> None:
    last: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            os.replace(str(tmp), str(dest))
            return
        except OSError as exc:        # PermissionError là lớp con của OSError
            last = exc
            if attempt < retries - 1:
                time.sleep(backoff_s * (2 ** attempt))
    raise last if last is not None else OSError(f"không tráo được {tmp} → {dest}")


def _write_via_tmp(out_path: Path, writer) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(out_path)
    try:
        writer(tmp)
        replace_with_retry(tmp, out_path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_table_atomic(table, out_path: Path | str, compression: str = "snappy") -> None:
    """Ghi một `pyarrow.Table`."""
    import pyarrow.parquet as pq

    _write_via_tmp(Path(out_path), lambda tmp: pq.write_table(table, str(tmp), compression=compression))


def write_dataframe_atomic(df, out_path: Path | str, compression: str = "snappy") -> None:
    """Ghi một `pandas.DataFrame` (không kèm index)."""
    import pyarrow as pa

    write_table_atomic(pa.Table.from_pandas(df, preserve_index=False), out_path, compression)


def write_text_atomic(text: str, out_path: Path | str, encoding: str = "utf-8") -> None:
    """Ghi văn bản (vd báo cáo HTML) theo kiểu tmp-rồi-tráo."""
    _write_via_tmp(Path(out_path), lambda tmp: Path(tmp).write_text(text, encoding=encoding))


def write_json_atomic(payload: Any, out_path: Path | str, *, indent: int = 2) -> None:
    """Serialize TRƯỚC khi đụng đĩa: payload lỗi không bao giờ làm cụt file cũ."""
    blob = json.dumps(payload, ensure_ascii=False, indent=indent, default=str)
    _write_via_tmp(Path(out_path), lambda tmp: tmp.write_text(blob, encoding="utf-8"))


def read_json(path: Path | str, default: Any = None) -> Any:
    """Đọc JSON; file không tồn tại ⇒ `default`. File HỎNG thì ném lỗi (không nuốt)."""
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))
