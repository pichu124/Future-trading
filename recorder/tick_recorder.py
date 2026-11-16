"""Ghi tick (kèm giá tốt nhất bid/ask — "snapshot sổ lệnh" mức 1) ra Parquet theo ngày.

Bố cục: `<data_dir>/raw/YYYY-MM-DD/`
- `part-<session>-<n>.parquet`: mỗi lần flush (mỗi `flush_seconds` hoặc khi đệm đầy) ghi MỘT
  file MỚI bằng tmp-rồi-tráo (`utils.atomic_io`). Không bao giờ ghi đè/ghi nối file đã có ⇒
  crash chỉ mất phần còn trong RAM, không có file hỏng. Ghi lỗi ⇒ giữ nguyên đệm, thử lại sau.
- `ticks.parquet`: bản gộp cuối ngày (`compact_day`). Gộp = đọc bản gộp cũ + mọi part, khử
  trùng theo id ghi (`session`, `rx_seq`), ghi nguyên tử, RỒI mới xoá part ⇒ crash giữa chừng
  chỉ để lại part thừa, lần gộp/đọc sau tự loại trùng.

Khử trùng theo id ghi chứ không theo nội dung: hai lần khớp giống hệt nhau là hai sự kiện thật.
Mỗi dòng có `recv_ts` (giờ bot nhận) bên cạnh `ts` (giờ sàn) để đo độ trễ feed.

Thư mục dữ liệu KHÔNG được nằm trong thư mục đồng bộ (Dropbox/OneDrive/…) — CLAUDE.md mục 3b.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

from core.clock import Clock
from core.models import Tick
from utils.atomic_io import write_table_atomic

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

COMPACT_NAME = "ticks.parquet"
SYNC_FOLDER_MARKERS = ("dropbox", "onedrive", "google drive", "googledrive", "icloud")

SCHEMA = pa.schema([
    ("symbol", pa.string()),
    ("ts", pa.timestamp("us")),
    ("last", pa.float64()),
    ("last_qty", pa.int64()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
    ("bid_qty", pa.int64()),
    ("ask_qty", pa.int64()),
    ("total_volume", pa.int64()),
    ("source", pa.string()),
    ("seq", pa.int64()),
    ("recv_ts", pa.timestamp("us")),
    ("session", pa.string()),
    ("rx_seq", pa.int64()),
])
_TICK_FIELDS = ("symbol", "ts", "last", "last_qty", "bid", "ask", "bid_qty", "ask_qty",
                "total_volume", "source", "seq")


def assert_not_in_sync_folder(path: Path) -> None:
    low = str(path).replace("\\", "/").lower()
    for marker in SYNC_FOLDER_MARKERS:
        if marker in low:
            raise ValueError(f"Thư mục dữ liệu {path} nằm trong thư mục đồng bộ ({marker}) — "
                             "đặt paths.data_dir ra ngoài (CLAUDE.md mục 3b)")


def part_files(day_dir: Path) -> list[Path]:
    return sorted(Path(day_dir).glob("part-*.parquet"))


def _read_rows(day_dir: Path) -> tuple[list[dict], list[Path]]:
    d = Path(day_dir)
    parts = part_files(d)
    files = ([d / COMPACT_NAME] if (d / COMPACT_NAME).exists() else []) + parts
    seen: set[tuple] = set()
    rows: list[dict] = []
    for f in files:
        for r in pq.read_table(f, schema=SCHEMA).to_pylist():
            key = (r["session"], r["rx_seq"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
    rows.sort(key=lambda r: (r["ts"], r["recv_ts"], r["session"], r["rx_seq"]))
    return rows, parts


def load_ticks(day_dir: Path) -> list[Tick]:
    """Mọi tick đã lưu của một ngày (bản gộp + part chưa gộp), theo thời gian, không trùng."""
    rows, _ = _read_rows(day_dir)
    return [Tick(**{k: r[k] for k in _TICK_FIELDS}) for r in rows]


def _remove_parts(parts: list[Path]) -> None:
    for p in parts:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def compact_day(day_dir: Path) -> int:
    """Gộp part vào ticks.parquet (idempotent). Trả số dòng sau gộp."""
    rows, parts = _read_rows(day_dir)
    if not rows:
        return 0
    write_table_atomic(pa.Table.from_pylist(rows, schema=SCHEMA), Path(day_dir) / COMPACT_NAME)
    _remove_parts(parts)
    return len(rows)


class TickRecorder:
    def __init__(self, data_dir: Path, clock: Clock, *, session_id: Optional[str] = None,
                 flush_seconds: float = 5.0, max_buffer_rows: int = 5000):
        self.data_dir = Path(data_dir)
        assert_not_in_sync_folder(self.data_dir)
        if flush_seconds <= 0 or max_buffer_rows <= 0:
            raise ValueError("flush_seconds và max_buffer_rows phải > 0")
        self.clock = clock
        self.session_id = session_id or (
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}").replace("-", "_")
        self.flush_seconds = flush_seconds
        self.max_buffer_rows = max_buffer_rows
        self._buffer: list[dict] = []
        self._rx_seq = 0
        self._part_seq = 0
        self._closed = False
        self._wake: Optional[asyncio.Event] = None
        self._stats: Counter = Counter()
        self._by_source: Counter = Counter()
        self._last_flush_at: Optional[datetime] = None
        self._days: set = set()

    @classmethod
    def from_settings(cls, settings, clock: Clock, **kw) -> "TickRecorder":
        return cls(settings.path("paths.data_dir"), clock,
                   flush_seconds=float(settings.require("recorder.flush_seconds")),
                   max_buffer_rows=int(settings.require("recorder.max_buffer_rows")), **kw)

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def day_dir(self, d=None) -> Path:
        d = d or self.clock.now().date()
        return self.data_dir / "raw" / d.isoformat()

    # ------------------------------------------------------------------ ghi ----

    def add(self, tick: Tick) -> None:
        """Nhận một tick (gọi được từ callback đồng bộ của feed)."""
        self._rx_seq += 1
        row = {k: getattr(tick, k) for k in _TICK_FIELDS}
        row.update(recv_ts=self.clock.now(), session=self.session_id, rx_seq=self._rx_seq)
        self._buffer.append(row)
        self._by_source[tick.source or "?"] += 1
        if len(self._buffer) >= self.max_buffer_rows:
            self.flush()

    def flush(self) -> int:
        """Ghi đệm ra một part mới. Lỗi ⇒ ném lại, đệm GIỮ NGUYÊN."""
        if not self._buffer:
            return 0
        rows = self._buffer
        day = self.day_dir()
        path = day / f"part-{self.session_id}-{self._part_seq + 1:06d}.parquet"
        try:
            write_table_atomic(pa.Table.from_pylist(rows, schema=SCHEMA), path)
        except Exception:
            self._stats["write_errors"] += 1
            log.exception("recorder: ghi %s thất bại — giữ %d dòng trong RAM", path.name, len(rows))
            raise
        self._part_seq += 1
        self._buffer = []
        self._stats["rows_written"] += len(rows)
        self._stats["parts_written"] += 1
        self._last_flush_at = self.clock.now()
        self._days.add(day)
        return len(rows)

    async def run_flusher(self) -> None:
        """Flush mỗi `flush_seconds` tới khi `close()`. Lỗi ghi chỉ log, lần sau thử lại."""
        self._wake = asyncio.Event()
        while not self._closed:
            try:
                await asyncio.wait_for(self._wake.wait(), self.flush_seconds)
            except asyncio.TimeoutError:
                pass
            if self._closed:
                break
            try:
                self.flush()
            except Exception:
                pass

    async def close(self, compact: bool = True) -> None:
        """Flush lần cuối và (mặc định) gộp các ngày đã ghi."""
        self._closed = True
        if self._wake is not None:
            self._wake.set()
        self.flush()
        if compact:
            for d in sorted(self._days | {self.day_dir()}):
                if d.exists():
                    n = compact_day(d)
                    log.info("recorder: gộp %s → %d dòng", d.name, n)

    def stats(self) -> dict:
        return {"session": self.session_id, "buffered": len(self._buffer),
                "rows_written": self._stats["rows_written"],
                "parts_written": self._stats["parts_written"],
                "write_errors": self._stats["write_errors"],
                "by_source": dict(self._by_source), "last_flush_at": self._last_flush_at}
