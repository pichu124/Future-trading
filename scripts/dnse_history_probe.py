"""Thăm dò REST lịch sử của DNSE — bước "gọi thử bằng tay" BẮT BUỘC trước khi viết
`scripts/download_history.py` (PROMPTS P1.6, CLAUDE.md mục 8).

Chốt các mục ❓ trong docs/broker_notes.md §1.3:
- `from`/`to` của `/price/{symbol}/trades` và `/price/ohlc` nhận định dạng nào (epoch giây,
  mili-giây, ISO, ngày)? — thử cả bốn;
- lịch sử của hợp đồng ĐÃ đáo hạn lùi được bao xa? — thử hôm qua và 1/3/6/12 tháng trước, mỗi
  ngày dùng đúng hợp đồng đang giao dịch ngày đó (mã KRX từ core.contracts);
- chữ ký HMAC ký đường dẫn KÈM hay KHÔNG kèm query? SDK `dnse` 0.5.0 ký đường dẫn không query
  (tham số gửi riêng) trong khi ghi chú cũ nói "kèm query" — thử không-query trước, 401 thì thử
  kèm query;
- tên field, kiểu bọc response (list / {"data": [...]} / dạng cột), phân trang, hạn mức.
`/price/ohlc` KHÔNG có trong SDK ⇒ mọi thứ về nó đều ❓ cho tới khi chạy script này.

Mỗi response được ghi NGUYÊN VĂN (không kèm header/chữ ký) vào
`data/probe/dnse_history_<thời điểm>/`, cùng `summary.json` — gửi thư mục đó lại để chốt.

    python scripts/dnse_history_probe.py
    python scripts/dnse_history_probe.py --months-back 1 3 6 12 24
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time as _time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import contracts  # noqa: E402
from core.calendar import TradingCalendar  # noqa: E402
from core.clock import VN_TZ  # noqa: E402

BASE_URL = "https://openapi.dnse.com.vn"
_TIME_KEYS = ("time", "t", "timestamp", "tradingTime", "trading_time", "date", "datetime")
_LIST_KEYS = ("data", "items", "result", "results", "trades", "content", "records", "bars")


# ------------------------------------------------------------------ chữ ký ----

def build_signature_headers(method: str, target: str, api_key: str, api_secret: str, *,
                            date_value: Optional[str] = None, nonce: Optional[str] = None) -> dict:
    """Header HMAC-SHA256 như `dnse._hmac_signer.build_signature_headers` (SDK 0.5.0).

    `target` = đường dẫn được ký (có hoặc không kèm "?query" — đó là điều đang thăm dò).
    """
    date_value = date_value or datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    nonce = nonce or uuid.uuid4().hex
    sig_string = f"(request-target): {method.lower()} {target}\ndate: {date_value}\nnonce: {nonce}"
    raw = hmac.new(api_secret.encode(), sig_string.encode(), hashlib.sha256).digest()
    sig = urllib.parse.quote(base64.b64encode(raw).decode(), safe="")
    return {
        "x-api-key": api_key,
        "date": date_value,
        "X-Signature": (f'Signature keyId="{api_key}",algorithm="hmac-sha256",'
                        f'headers="(request-target) date",signature="{sig}",nonce="{nonce}"'),
        "User-Agent": "future-trading-probe",
        "Accept": "application/json",
    }


# ------------------------------------------------------------------ tham số ----

def time_candidates(start: datetime, end: datetime) -> list[tuple[str, tuple[str, str]]]:
    """Các cách biểu diễn khoảng [start, end] (giờ VN naive) cần thử cho from/to."""
    s_utc = start.replace(tzinfo=VN_TZ).astimezone(timezone.utc)
    e_utc = end.replace(tzinfo=VN_TZ).astimezone(timezone.utc)
    s, e = int(s_utc.timestamp()), int(e_utc.timestamp())
    return [
        ("epoch_s", (str(s), str(e))),
        ("epoch_ms", (str(s * 1000), str(e * 1000))),
        ("iso_vn", (start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S"))),
        ("date", (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))),
    ]


def summarize(body: Any) -> dict:
    """Tóm tắt response bất kể kiểu bọc: số dòng, field, thời điểm đầu/cuối."""
    out: dict = {"count": 0, "container": None, "keys": [], "top_keys": [],
                 "first_time": None, "last_time": None}
    rows: Optional[list] = None
    if isinstance(body, list):
        rows, out["container"] = body, "list"
    elif isinstance(body, dict):
        out["top_keys"] = sorted(body)
        for k in _LIST_KEYS:
            if isinstance(body.get(k), list):
                rows, out["container"] = body[k], k
                break
        if rows is None and isinstance(body.get("t"), list):
            n = len(body["t"])
            if all(isinstance(v, list) and len(v) == n for v in body.values() if isinstance(v, list)):
                out.update(count=n, container="columnar", keys=sorted(body),
                           first_time=body["t"][0] if n else None,
                           last_time=body["t"][-1] if n else None)
                return out
    if not rows:
        return out
    out["count"] = len(rows)
    first, last = rows[0], rows[-1]
    if isinstance(first, dict):
        out["keys"] = sorted(first)
        tk = next((k for k in _TIME_KEYS if k in first), None)
        if tk:
            out["first_time"], out["last_time"] = first.get(tk), last.get(tk) if isinstance(last, dict) else None
    return out


# ------------------------------------------------------------------ chạy ----

def _probe_days(today: date, cal: TradingCalendar, months_back: Sequence[int]) -> list[date]:
    days = [cal.previous_trading_day(today)]
    for m in months_back:
        d = cal.previous_trading_day(today - timedelta(days=30 * m), inclusive=True)
        if d not in days:
            days.append(d)
    return days


def run_probe(api_key: str, api_secret: str, *, out_dir: Path, calendar: TradingCalendar,
              today: date, session: Any = None, sleep: Callable[[float], None] = _time.sleep,
              months_back: Sequence[int] = (1, 3, 6, 12), base_url: str = BASE_URL,
              roll_days_before_expiry: int = 1) -> Path:
    if session is None:
        import requests
        session = requests.Session()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []
    n = 0

    def call(endpoint: str, path: str, params: dict, label: str, d: date, krx: str, fmt: str) -> None:
        nonlocal n
        for sign_query in (False, True):
            query = urllib.parse.urlencode(params)
            target = f"{path}?{query}" if sign_query else path
            for attempt in (1, 2):
                headers = build_signature_headers("GET", target, api_key, api_secret)
                url = base_url + (target if sign_query else path)
                try:
                    resp = session.get(url, params=None if sign_query else params,
                                       headers=headers, timeout=30)
                except Exception as exc:          # mạng: ghi lại, không làm dừng cả lượt thăm dò
                    status, body, rheaders = None, {"error": f"{type(exc).__name__}: {exc}"}, {}
                else:
                    status, rheaders = resp.status_code, dict(resp.headers or {})
                    try:
                        body = resp.json()
                    except ValueError:
                        body = {"raw_text": resp.text[:2000]}
                if status == 429 and attempt == 1:
                    sleep(float(rheaders.get("Retry-After") or rheaders.get("retry-after") or 5))
                    continue
                break
            n += 1
            name = f"{n:03d}_{endpoint}_{d.isoformat()}_{fmt}{'_signq' if sign_query else ''}.json"
            record = {"endpoint": endpoint, "day": d.isoformat(), "krx": krx, "time_format": fmt,
                      "sign_query": sign_query, "url": base_url + path, "params": params,
                      "status": status,
                      "rate_limit": {k: v for k, v in rheaders.items() if k.lower().startswith(("x-ratelimit", "retry-after"))},
                      "summary": summarize(body), "file": name}
            (out_dir / name).write_text(json.dumps({"request": {k: record[k] for k in ("url", "params", "sign_query")},
                                                    "status": status, "body": body},
                                                   ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            summary.append(record)
            s = record["summary"]
            print(f"{label:<44} {fmt:<9} ký{'+query' if sign_query else '     '} → {status}  "
                  f"{s['count']:>5} dòng  {s['first_time']} … {s['last_time']}")
            if status != 401:
                return                                   # cách ký này được server chấp nhận

    for d in _probe_days(today, calendar, months_back):
        name = contracts.trading_contract(d, calendar, roll_days_before_expiry)
        krx = contracts.krx_symbol(*contracts.contract_ym(name))
        start = datetime.combine(d, datetime.min.time()).replace(hour=10)
        for fmt, (f, t) in time_candidates(start, start + timedelta(minutes=5)):
            call("trades", f"/price/{krx}/trades", {"boardId": "G1", "from": f, "to": t, "limit": 100},
                 f"trades {name} ({krx}) {d}", d, krx, fmt)
            call("ohlc", "/price/ohlc", {"symbol": krx, "type": "DERIVATIVE", "resolution": "1",
                                         "from": f, "to": t},
                 f"ohlc   {name} ({krx}) {d}", d, krx, fmt)

    path = out_dir / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


def main(argv=None) -> int:
    from core.settings import REPO_ROOT, load_settings

    ap = argparse.ArgumentParser(description="Thăm dò REST lịch sử DNSE (P1.6)")
    ap.add_argument("--months-back", type=int, nargs="*", default=[1, 3, 6, 12])
    ap.add_argument("--today", help="YYYY-MM-DD (mặc định hôm nay giờ VN)")
    ap.add_argument("--base-url", default=BASE_URL)
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    key, secret = os.environ.get("DNSE_API_KEY"), os.environ.get("DNSE_API_SECRET")
    if not key or not secret:
        print("❌ Thiếu DNSE_API_KEY / DNSE_API_SECRET trong .env")
        return 2
    settings = load_settings()
    today = date.fromisoformat(args.today) if args.today else datetime.now(VN_TZ).date()
    out = settings.path("paths.data_dir") / "probe" / f"dnse_history_{datetime.now():%Y%m%d_%H%M%S}"
    summary = run_probe(key, secret, out_dir=out, calendar=TradingCalendar.default(), today=today,
                        months_back=args.months_back, base_url=args.base_url,
                        roll_days_before_expiry=int(settings.get("contract.roll_days_before_expiry", 1)))
    print(f"\nĐã ghi response thô + {summary.name} vào {out} — gửi lại thư mục này (không chứa khoá).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
