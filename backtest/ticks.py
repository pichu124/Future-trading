"""Đọc tick thật cho backtest (`BacktestEngine.run_events`) — recorder và downloader dùng chung schema tick.

- Thư mục ngày của recorder (`ticks.parquet` / `part-*.parquet`) ⇒ `recorder.tick_recorder.load_ticks` (gộp
  part, lọc trùng theo phiên ghi). File lẻ khác (vd downloader `data/history/trades/<HĐ>/<ngày>.parquet`) ⇒ đọc
  thẳng theo schema tick (dòng downloader không có phiên ⇒ không lọc trùng theo phiên).
- `date_from`/`date_to`: file/thư mục tên `YYYY-MM-DD` ngoài khoảng ⇒ bỏ qua KHÔNG đọc (6 tháng khớp lệnh ~14 triệu
  dòng — đọc hết rồi mới lọc thì cạn RAM); mọi dòng còn lại vẫn lọc theo ngày của `ts`.
- Nhiều `source` mà không chỉ định ⇒ lỗi: trộn hai nguồn cho tick đôi và bar sai.
- `TickStore`: đọc DẦN theo ngày mỗi lượt duyệt (CLI `backtest.engine/robustness --ticks` dùng cái này) — cả kho 6
  tháng ~14,5 triệu tick ≈ 8 GB nếu dựng hết thành `Tick`; robustness chạy lại ~14 lượt.
- `fill_missing_quotes`: tick khớp không có bid lẫn ask (downloader) ⇒ bid/ask giả = last ∓ max(1, ⌈spread/2⌉)
  bước — cùng giả định bi quan với tick giả từ bar (`backtest.engine.synthetic_ticks`).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence, Union

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


def _name_date(p: Path) -> Optional[date]:
    try:
        return date.fromisoformat(p.stem if p.suffix == ".parquet" else p.name)
    except ValueError:
        return None


def _collect(p: Path, out: list[Tick], keep, seen: list) -> None:
    from recorder.tick_recorder import load_ticks
    d = _name_date(p)
    if p.is_dir():
        if _is_recorder_day(p):
            seen.append(p)
            if d is None or keep(d):
                out.extend(load_ticks(p))
            return
        for child in sorted(p.iterdir()):
            if child.is_dir() or child.suffix == ".parquet":
                _collect(child, out, keep, seen)
    elif p.suffix == ".parquet":
        seen.append(p)
        if d is None or keep(d):
            out.extend(_read_file(p))


def load_tick_files(paths: Sequence[PathLike], source: Optional[str] = None, date_from: Optional[date] = None,
                    date_to: Optional[date] = None) -> list[Tick]:
    def keep(d: date) -> bool:
        return (date_from is None or d >= date_from) and (date_to is None or d <= date_to)
    ticks: list[Tick] = []
    seen: list = []
    for p in paths:
        _collect(Path(p), ticks, keep, seen)
    if not seen:
        raise ValueError(f"Không có file tick nào trong {', '.join(str(p) for p in paths)}")
    if date_from is not None or date_to is not None:
        ticks = [t for t in ticks if keep(t.ts.date())]
    if not ticks:
        if date_from is not None or date_to is not None:
            return []
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
    out = list(ticks)
    return out, fill_missing_quotes_inplace(out, tick_size, spread_ticks)


def _half_spread(tick_size: float, spread_ticks: int) -> float:
    if isinstance(spread_ticks, bool) or int(spread_ticks) != spread_ticks or spread_ticks < 0:
        raise ValueError("spread_ticks phải là số nguyên ≥ 0")
    return 0.0 if spread_ticks <= 0 else max(1, -(-int(spread_ticks) // 2)) * tick_size


def fill_missing_quotes_inplace(ticks: list[Tick], tick_size: float, spread_ticks: int) -> int:
    """Như `fill_missing_quotes` nhưng thay ngay trong `ticks` (không giữ hai bản cùng lúc ⇒ đỉnh RAM ~½
    với kho 6 tháng ~14 triệu tick). Trả số tick được gắn báo giá giả."""
    half = _half_spread(tick_size, spread_ticks)
    n = 0
    for i, t in enumerate(ticks):
        if t.last is not None and t.bid is None and t.ask is None:
            ticks[i] = replace(t, bid=round(t.last - half, 6), ask=round(t.last + half, 6))
            n += 1
    return n


# ------------------------------------------------------------------ đọc dần theo ngày ----

def _sources_of(entry: Path) -> set[str]:
    """Tập `source` của một file/thư mục ngày — chỉ đọc MỘT cột (rẻ), không dựng Tick."""
    import pyarrow.parquet as pq

    from recorder.tick_recorder import COMPACT_NAME
    files = ([entry / COMPACT_NAME] if (entry / COMPACT_NAME).exists() else []) + sorted(entry.glob("part-*.parquet")) \
        if entry.is_dir() else [entry]
    out: set[str] = set()
    for f in files:
        if "source" in pq.read_schema(f).names:
            out |= {s or "?" for s in pq.read_table(f, columns=["source"]).column(0).to_pylist()}
        else:
            out.add("?")
    return out


class TickStore:
    """Kho tick đọc DẦN theo ngày — thay cho `load_tick_files` khi dữ liệu lớn.

    6 tháng khớp lệnh ~14,5 triệu tick ≈ 8 GB nếu dựng hết thành `Tick` (đo thực tế ~570 B/tick), mà robustness
    chạy lại ~14 lượt. Ở đây mỗi lượt duyệt đọc lại từ đĩa từng ngày, gắn báo giá giả (`fill_missing_quotes`),
    phát theo thời gian ⇒ RAM ≈ một ngày. Đổi lại mỗi lượt tốn thêm thời gian đọc file.

    - File/thư mục ngày phải tên `YYYY-MM-DD` (downloader, recorder) — không thì lỗi (không biết thuộc ngày nào);
      dòng nằm ngoài ngày của tên file ⇒ lỗi khi duyệt.
    - Kiểm nguồn NGAY khi tạo (chỉ đọc cột `source`): nhiều nguồn mà không chọn ⇒ lỗi, như `load_tick_files`.
    - Duyệt được nhiều lần (`iter()`, `for t in store`); `fake_quotes()` = số tick bị gắn báo giá giả (theo ngày,
      lượt sau ghi đè lượt trước).
    """

    def __init__(self, paths: Sequence[PathLike], source: Optional[str] = None, *, tick_size: float,
                 spread_ticks: int):
        _half_spread(tick_size, spread_ticks)                      # kiểm tham số sớm
        self.tick_size, self.spread_ticks, self.source = tick_size, spread_ticks, source
        entries: list[Path] = []
        for p in paths:
            self._scan(Path(p), entries)
        if not entries:
            raise ValueError(f"Không có file tick nào trong {', '.join(str(p) for p in paths)}")
        self._by_day: dict[date, list[Path]] = {}
        for e in entries:
            d = _name_date(e)
            if d is None:
                raise ValueError(f"{e}: tên phải là YYYY-MM-DD để đọc dần theo ngày")
            self._by_day.setdefault(d, []).append(e)
        sources = sorted(set().union(*(_sources_of(e) for e in entries)))
        if source is not None:
            if source not in sources:
                raise ValueError(f"Không có tick nguồn {source!r} (có: {', '.join(sources)})")
        elif len(sources) > 1:
            raise ValueError(f"Dữ liệu có nhiều nguồn ({', '.join(sources)}) — chọn một bằng --source "
                             "(trộn nguồn ⇒ tick đôi, bar sai)")
        self._fake: dict[date, int] = {}

    @staticmethod
    def _scan(p: Path, out: list[Path]) -> None:
        if p.is_dir():
            if _is_recorder_day(p):
                out.append(p)
                return
            for child in sorted(p.iterdir()):
                if child.is_dir() or child.suffix == ".parquet":
                    TickStore._scan(child, out)
        elif p.suffix == ".parquet":
            out.append(p)

    def days(self) -> list[date]:
        return sorted(self._by_day)

    def __bool__(self) -> bool:
        return bool(self._by_day)

    def __iter__(self) -> Iterator[Tick]:
        return self.iter()

    def fake_quotes(self) -> int:
        return sum(self._fake.values())

    def load_day(self, d: date) -> list[Tick]:
        from recorder.tick_recorder import load_ticks
        ticks: list[Tick] = []
        for e in self._by_day.get(d, ()):
            ticks.extend(load_ticks(e) if e.is_dir() else _read_file(e))
        if self.source is not None:
            ticks = [t for t in ticks if (t.source or "?") == self.source]
        bad = next((t for t in ticks if t.ts.date() != d), None)
        if bad is not None:
            raise ValueError(f"Dữ liệu ngày {d} chứa tick ngày {bad.ts.date()} ({bad.symbol} {bad.ts}) — tên file "
                             "không khớp nội dung")
        ticks.sort(key=lambda t: t.ts)
        self._fake[d] = fill_missing_quotes_inplace(ticks, self.tick_size, self.spread_ticks)
        return ticks

    def iter(self, date_from: Optional[date] = None, date_to: Optional[date] = None) -> Iterator[Tick]:
        for d in self.days():
            if (date_from is None or d >= date_from) and (date_to is None or d <= date_to):
                yield from self.load_day(d)
