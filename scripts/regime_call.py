"""Ghi nhận định regime "shadow" của nhóm AI — MỘT file JSON mỗi ngày giao dịch, TRƯỚC giờ mở cửa.

CHỈ NGHIÊN CỨU (CLAUDE.md mục 8: LLM không nằm trong đường ra quyết định live). Không module nào của bot đọc các
file này; chúng chỉ để `scripts/score_regime_calls.py` chấm điểm về sau. Quy trình: research/regime_shadow.md.

    python scripts/regime_call.py --check                    # 0 = ghi được · 3 = ngày nghỉ · 4 = đã ATO · 5 = đã ghi
    python scripts/regime_call.py --payload nhan_dinh.json   # kiểm + đóng dấu giờ + ghi <calls_dir>/YYYY-MM-DD.json
    python scripts/regime_call.py --payload x.json --dry-run # thử đường ống: bỏ chốt giờ, ghi vào <calls_dir>/dryrun/

Chống tự lừa mình:
- Giờ (`created_at`), ngày (`trade_date`), hợp đồng do SCRIPT đóng dấu từ đồng hồ hệ thống; payload không được tự
  khai, dòng lệnh không có cờ đặt giờ.
- Từ `session.ato_start` trở đi ⇒ từ chối (giá đã lộ thì không còn là dự báo). Ngày nghỉ ⇒ từ chối.
- Mỗi ngày một file, KHÔNG ghi đè. Thời điểm commit/push lên GitHub là bằng chứng độc lập thứ hai.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts                                   # noqa: E402
from core.calendar import TradingCalendar                    # noqa: E402
from core.clock import SessionSchedule, SystemClock          # noqa: E402
from utils.atomic_io import read_json, write_json_atomic     # noqa: E402

SCHEMA_VERSION = 1
# Ba câu hỏi và các lớp — ĐĂNG KÝ TRƯỚC 16/11/2026 (research/log.md). Định nghĩa "thực tế" của từng lớp nằm ở
# scripts/score_regime_calls.py + settings `research.regime_shadow`.
QUESTIONS: dict[str, tuple[str, ...]] = {
    "vol": ("low", "normal", "high"),
    "day_type": ("trend", "range"),
    "direction": ("up", "flat", "down"),
}
FORECASTERS = ("debate", "single")          # sau tranh luận · một agent làm TRƯỚC tranh luận
TRANSCRIPT_ROLES = ("single", "trend_advocate", "reversion_advocate", "skeptic", "judge")
PAYLOAD_KEYS = ("model", "inputs", "single", "debate", "transcript")
PROB_TOLERANCE = 0.01
DRYRUN_DIR = "dryrun"

EXIT_OK, EXIT_BAD_PAYLOAD, EXIT_NOT_TRADING_DAY, EXIT_WINDOW_CLOSED, EXIT_ALREADY = 0, 2, 3, 4, 5


class CallError(ValueError):
    """Nhận định không hợp lệ / không được phép ghi."""


class NotTradingDay(CallError):
    pass


class WindowClosed(CallError):
    pass


class AlreadyRecorded(CallError):
    pass


# ------------------------------------------------------------------ kiểm ----

def validate_forecast(obj: Any) -> dict[str, dict[str, float]]:
    """{câu hỏi: {lớp: xác suất}} đúng các câu hỏi/lớp đã đăng ký, mỗi phân phối tổng = 1 (± 0,01)."""
    if not isinstance(obj, Mapping):
        raise CallError("dự báo phải là object {câu hỏi: {lớp: xác suất}}")
    if set(obj) != set(QUESTIONS):
        raise CallError(f"dự báo phải có đúng các câu hỏi {sorted(QUESTIONS)}, nhận {sorted(obj)}")
    out: dict[str, dict[str, float]] = {}
    for q, classes in QUESTIONS.items():
        dist = obj[q]
        if not isinstance(dist, Mapping) or set(dist) != set(classes):
            raise CallError(f"{q}: cần xác suất cho đúng các lớp {list(classes)}")
        probs: dict[str, float] = {}
        for c in classes:
            v = dist[c]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1:
                raise CallError(f"{q}.{c}: xác suất phải là số trong [0, 1], nhận {v!r}")
            probs[c] = float(v)
        total = sum(probs.values())
        if abs(total - 1.0) > PROB_TOLERANCE:
            raise CallError(f"{q}: tổng xác suất = {total:.3f}, phải = 1")
        out[q] = probs
    return out


def _validate_payload(payload: Any) -> dict:
    if not isinstance(payload, Mapping):
        raise CallError("payload phải là object JSON")
    extra = sorted(set(payload) - set(PAYLOAD_KEYS))
    if extra:
        raise CallError(f"payload có trường không cho phép {extra} (giờ/ngày/hợp đồng do script đóng dấu)")
    missing = [k for k in PAYLOAD_KEYS if k not in payload]
    if missing:
        raise CallError(f"payload thiếu {missing}")
    model = payload["model"]
    if not isinstance(model, str) or not model.strip():
        raise CallError("model: cần tên mô hình đã chạy")
    inputs = payload["inputs"]
    if not isinstance(inputs, Mapping) or not inputs:
        raise CallError("inputs: cần object không rỗng ghi dữ liệu đã dùng (để kiểm lại về sau)")
    transcript = payload["transcript"]
    if not isinstance(transcript, Mapping) or set(transcript) != set(TRANSCRIPT_ROLES):
        raise CallError(f"transcript: cần đúng các vai {list(TRANSCRIPT_ROLES)}")
    for role in TRANSCRIPT_ROLES:
        if not isinstance(transcript[role], str) or not transcript[role].strip():
            raise CallError(f"transcript.{role}: rỗng")
    return {"model": model.strip(), "inputs": dict(inputs),
            "single": validate_forecast(payload["single"]), "debate": validate_forecast(payload["debate"]),
            "transcript": {r: transcript[r] for r in TRANSCRIPT_ROLES}}


def call_window(now: datetime, schedule: SessionSchedule, calendar: TradingCalendar) -> date:
    """Ngày giao dịch mà nhận định ghi lúc `now` (giờ VN, naive) được tính cho; ném lỗi nếu không được ghi."""
    d = now.date()
    if not calendar.is_trading_day(d):
        raise NotTradingDay(f"{d} không phải ngày giao dịch")
    if now.time() >= schedule.ato_start:
        raise WindowClosed(f"{now:%H:%M} đã từ {schedule.ato_start:%H:%M} (ATO) trở đi — không ghi nhận định cho {d}")
    return d


# ------------------------------------------------------------------ ghi / đọc ----

def write_call(payload: Any, out_dir: Path | str, *, now: datetime, schedule: SessionSchedule,
               calendar: TradingCalendar, roll_days: int = 1, dry_run: bool = False) -> Path:
    body = _validate_payload(payload)
    out_dir = Path(out_dir)
    if dry_run:
        d = now.date()
        path = out_dir / DRYRUN_DIR / f"{now:%Y-%m-%dT%H%M%S}.json"
    else:
        d = call_window(now, schedule, calendar)
        path = out_dir / f"{d.isoformat()}.json"
    if path.exists():
        raise AlreadyRecorded(f"{path} đã có — không ghi đè nhận định")
    record = {"schema_version": SCHEMA_VERSION, "trade_date": d.isoformat(),
              "created_at": now.replace(microsecond=0).isoformat() + "+07:00",
              "contract": contracts.trading_contract(calendar.next_trading_day(d, inclusive=True), calendar, roll_days),
              "dry_run": bool(dry_run), **body}
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(record, path)
    return path


def _check_record(rec: Any, path: Path, schedule: Optional[SessionSchedule]) -> dict:
    if not isinstance(rec, Mapping):
        raise CallError(f"{path}: không phải object JSON")
    if rec.get("schema_version") != SCHEMA_VERSION:
        raise CallError(f"{path}: schema_version {rec.get('schema_version')!r} ≠ {SCHEMA_VERSION}")
    if rec.get("dry_run") is not False:
        raise CallError(f"{path}: bản dry-run nằm lẫn trong thư mục nhận định thật")
    if rec.get("trade_date") != path.stem:
        raise CallError(f"{path}: trade_date {rec.get('trade_date')!r} không khớp tên file")
    try:
        created = datetime.fromisoformat(str(rec.get("created_at")))
    except ValueError as e:
        raise CallError(f"{path}: created_at hỏng") from e
    if created.tzinfo is None or created.utcoffset().total_seconds() != 7 * 3600:
        raise CallError(f"{path}: created_at phải ghi theo +07:00")
    if created.date().isoformat() != rec["trade_date"]:
        raise CallError(f"{path}: created_at {created} không thuộc ngày {rec['trade_date']}")
    if schedule is not None and created.time() >= schedule.ato_start:
        raise CallError(f"{path}: created_at {created:%H:%M} không trước giờ ATO — nhận định không hợp lệ")
    out = dict(rec)
    for f in FORECASTERS:
        try:
            out[f] = validate_forecast(rec.get(f))
        except CallError as e:
            raise CallError(f"{path}: {f}: {e}") from e
    return out


def load_calls(calls_dir: Path | str, schedule: Optional[SessionSchedule] = None) -> list[dict]:
    """Mọi nhận định THẬT (bỏ thư mục dryrun/), cũ nhất trước. File hỏng/bị sửa ⇒ ném lỗi, không âm thầm bỏ qua."""
    base = Path(calls_dir)
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.glob("*.json")):
        try:
            date.fromisoformat(p.stem)
        except ValueError as e:
            raise CallError(f"{p}: tên file phải là YYYY-MM-DD.json") from e
        out.append(_check_record(read_json(p), p, schedule))
    return out


# ------------------------------------------------------------------ CLI ----

def main(argv: Optional[Sequence[str]] = None, now: Optional[datetime] = None) -> int:
    ap = argparse.ArgumentParser(description="Ghi nhận định regime shadow (chỉ nghiên cứu)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="chỉ kiểm còn ghi được nhận định hôm nay không")
    g.add_argument("--payload", help="file JSON: model, inputs, single, debate, transcript")
    ap.add_argument("--dry-run", action="store_true", help="thử đường ống: bỏ chốt giờ, ghi vào dryrun/")
    ap.add_argument("--out", help="thư mục nhận định (mặc định settings research.regime_shadow.calls_dir)")
    args = ap.parse_args(argv)

    from core.settings import load_settings
    settings = load_settings()
    schedule = SessionSchedule.from_mapping(settings.get("session", {}))
    calendar = TradingCalendar.default()
    roll_days = int(settings.require("contract.roll_days_before_expiry"))
    out_dir = Path(args.out) if args.out else settings.path("research.regime_shadow.calls_dir")
    now = now or SystemClock().now()

    try:
        if args.check:
            d = call_window(now, schedule, calendar)
            if (out_dir / f"{d.isoformat()}.json").exists():
                raise AlreadyRecorded(f"đã có nhận định cho {d}")
            left = (datetime.combine(d, schedule.ato_start) - now).total_seconds() / 60
            print(f"MỞ: ghi được nhận định cho {d} (HĐ {contracts.trading_contract(d, calendar, roll_days)}), "
                  f"còn {left:.0f} phút tới ATO {schedule.ato_start:%H:%M}")
            return EXIT_OK
        try:
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise CallError(f"không đọc được payload {args.payload}: {e}") from e
        path = write_call(payload, out_dir, now=now, schedule=schedule, calendar=calendar, roll_days=roll_days,
                          dry_run=args.dry_run)
        print(f"ĐÃ GHI {path}" + (" (dry-run — không được chấm điểm)" if args.dry_run else ""))
        return EXIT_OK
    except NotTradingDay as e:
        print(f"KHÔNG GHI: {e}")
        return EXIT_NOT_TRADING_DAY
    except WindowClosed as e:
        print(f"KHÔNG GHI: {e}")
        return EXIT_WINDOW_CLOSED
    except AlreadyRecorded as e:
        print(f"KHÔNG GHI: {e}")
        return EXIT_ALREADY
    except CallError as e:
        print(f"PAYLOAD LỖI: {e}")
        return EXIT_BAD_PAYLOAD


if __name__ == "__main__":
    raise SystemExit(main())
