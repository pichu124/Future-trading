"""Đọc tick thật cho backtest (`BacktestEngine.run_events`) — recorder và downloader dùng chung schema tick.

- Thư mục ngày của recorder (`ticks.parquet` / `part-*.parquet`) ⇒ `recorder.tick_recorder.load_ticks` (gộp
  part, lọc trùng theo phiên ghi). File lẻ khác (vd downloader `data/history/trades/<HĐ>/<ngày>.parquet`) ⇒ đọc
  thẳng theo schema tick (dòng downloader không có phiên ⇒ không lọc trùng theo phiên).
- Nhiều `source` mà không chỉ định ⇒ lỗi: trộn hai nguồn cho tick đôi và bar sai.
- `fill_missing_quotes`: tick khớp không có bid lẫn ask (downloader) ⇒ bid/ask giả = last ∓ max(1, ⌈spread/2⌉)
  bước — cùng giả định bi quan với tick giả từ bar (`backtest.engine.synthetic_ticks`).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

from core.models import Tick

PathLike = Union[str, Path]


def _is_recorder_day(d: Path) -> bool:
    from recorder.tick_recorder import COMPACT_NAME
    return (d / COMPACT_NAME).exists() or any(d.glob("part-*.parquet"))


def _read_file(f: Path) -> list[Tick]:
    import pyarrow.parquet as pq

    from recorder.tick_recorder import SCHEMA, _TICK_FIELDS
    names = set(pq.read_schema(f).names)
    missing = [c for c in ("symbol", "ts", "last") if c not in names]
    if missing:
        raise ValueError(f"{f}: không phải file tick (thiếu cột {', '.join(missing)})")
    rows = pq.read_table(f, schema=SCHEMA).to_pylist()
    return [Tick(**{k: r[k] for k in _TICK_FIELDS}) for r in rows]


def _collect(p: Path, out: list[Tick]) -> None:
    from recorder.tick_recorder import load_ticks
    if p.is_dir():
        if _is_recorder_day(p):
            out.extend(load_ticks(p))
            return
        for child in sorted(p.iterdir()):
            if child.is_dir() or child.suffix == ".parquet":
                _collect(child, out)
    elif p.suffix == ".parquet":
        out.extend(_read_file(p))


def load_tick_files(paths: Sequence[PathLike], source: Optional[str] = None) -> list[Tick]:
    ticks: list[Tick] = []
    for p in paths:
        _collect(Path(p), ticks)
    if not ticks:
        raise ValueError(f"Không có file tick nào trong {', '.join(str(p) for p in paths)}")
    sources = sorted({t.source or "?" for t in ticks})
    if source is not None:
        ticks = [t for t in ticks if (t.source or "?") == source]
        if not ticks:
            raise ValueError(f"Không có tick nguồn {source!r} (có: {', '.join(sources)})")
    elif len(sources) > 1:
        raise ValueError(f"Dữ liệu có nhiều nguồn ({', '.join(sources)}) — chọn một bằng --source "
                         "(trộn nguồn ⇒ tick đôi, bar sai)")
    ticks.sort(key=lambda t: t.ts)                     # ổn định: giữ thứ tự nhận trong cùng thời điểm
    return ticks


def fill_missing_quotes(ticks: Iterable[Tick], tick_size: float, spread_ticks: int) -> tuple[list[Tick], int]:
    """(tick, số tick được gắn báo giá giả). Chỉ tick khớp thiếu CẢ bid lẫn ask."""
    if isinstance(spread_ticks, bool) or int(spread_ticks) != spread_ticks or spread_ticks < 0:
        raise ValueError("spread_ticks phải là số nguyên ≥ 0")
    half = 0.0 if spread_ticks <= 0 else max(1, -(-int(spread_ticks) // 2)) * tick_size
    out, n = [], 0
    for t in ticks:
        if t.last is not None and t.bid is None and t.ask is None:
            out.append(replace(t, bid=round(t.last - half, 6), ask=round(t.last + half, 6)))
            n += 1
        else:
            out.append(t)
    return out, n
