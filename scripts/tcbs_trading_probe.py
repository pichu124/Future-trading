"""Thăm dò API TCBS iFlash — CHỈ ĐỌC (không đặt lệnh: TCBS không có môi trường thử), để chốt các mục ❓ ở
docs/broker_notes.md §2b trước khi đặt `brokers.tcbs.api_verified: true`.

    python scripts/tcbs_trading_probe.py --payload-only     # không mạng: in payload adapter sẽ gửi
    python scripts/tcbs_trading_probe.py                    # nhập iOTP (app TCInvest) → chỉ đọc

Chế độ đọc: hạn API Key, tiểu khoản (tìm tiểu khoản DERIVATIVE), giá trần/sàn + mã hợp đồng TCBS trả về
(KRX hay VN30FYYMM?), vị thế mở, sổ lệnh trang 1 và 2 (pageNo bắt đầu từ 0 hay 1?). Response ghi NGUYÊN VĂN vào
`data/probe/tcbs_trading_<thời điểm>/` (bỏ header; xoá apiKey, otp, token) cùng `summary.json`.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brokers.tcbs import (EP_BOOK, EP_DERIVATIVES, EP_POSITIONS, TcbsConfig, TcbsHttp, TcbsTokenManager,  # noqa: E402
                          _requests_transport, build_place_payload)
from core.models import Order, OrderType, Side  # noqa: E402

REDACT_KEYS = ("apiKey", "otp", "token", "jwt", "Authorization")


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if k in REDACT_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class Recorder:
    def __init__(self, inner, out_dir: Path):
        self.inner, self.out_dir, self.records = inner, Path(out_dir), []
        self.out_dir.mkdir(parents=True, exist_ok=True)

    async def __call__(self, method, url, params, body, headers, timeout):
        rec: dict = {"n": len(self.records) + 1, "method": method, "path": urllib.parse.urlsplit(url).path,
                     "params": params, "body": redact(body)}
        try:
            status, rh, text = await self.inner(method, url, params, body, headers, timeout)
        except Exception as exc:
            rec.update(status=None, error=f"{type(exc).__name__}: {exc}")
            self._write(rec)
            raise
        try:
            resp: Any = json.loads(text) if text and text.strip() else None
        except ValueError:
            resp = {"raw_text": text[:2000]}
        rec.update(status=status, response=redact(resp),
                   rate_limit={k: v for k, v in (rh or {}).items() if k.lower().startswith(("x-ratelimit", "retry-after"))})
        self._write(rec)
        return status, rh, text

    def _write(self, rec: dict) -> None:
        self.records.append(rec)
        name = f"{rec['n']:03d}_{rec['method']}_{rec['path'].strip('/').replace('/', '_')}.json"
        (self.out_dir / name).write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def summarize(records: list[dict]) -> dict:
    symbols, statuses, codes, keys = set(), set(), set(), set()
    pages: dict = {}
    for r in records:
        resp = r.get("response")
        rows = resp.get("data") if isinstance(resp, dict) else None
        if r["path"] == EP_BOOK:
            pages[str((r.get("params") or {}).get("pageNo"))] = len(rows) if isinstance(rows, list) else None
        if isinstance(rows, list):
            for x in rows:
                if not isinstance(x, dict):
                    continue
                if x.get("symbol"):
                    symbols.add(str(x["symbol"]))
                if r["path"] == EP_BOOK:
                    keys.update(x)
                    statuses.add(f"{x.get('orderStatus')}/{x.get('status')}")
        if isinstance(resp, dict) and resp.get("code") is not None:
            codes.add(str(resp["code"]))
    return {"symbols_seen": sorted(symbols), "order_status_seen": sorted(statuses), "order_item_keys": sorted(keys),
            "book_rows_by_pageNo": pages, "error_codes": sorted(codes),
            "endpoints": {f"{r['method']} {r['path']}": r.get("status") for r in records}}


def sample_payloads(cfg: TcbsConfig, symbol: str) -> list[dict]:
    now = datetime.now()
    lo = Order("PROBE00001", symbol, Side.BUY, 1, OrderType.LIMIT, now, price=1300.0)
    mkt = Order("PROBE00002", symbol, Side.SELL, 1, OrderType.MARKET, now)
    return [{"what": "LO mua", "body": build_place_payload(lo, cfg, band=None)},
            {"what": "lệnh thị trường bán = LO giá SÀN (ví dụ trần/sàn 1444.5/1255.5)",
             "body": build_place_payload(mkt, cfg, band=(1444.5, 1255.5))}]


async def read_only(tokens: TcbsTokenManager, http: TcbsHttp, cfg: TcbsConfig, read_otp: Callable[[], str]) -> None:
    await tokens.connect()
    if not await tokens.submit_otp(read_otp()):
        raise SystemExit("❌ iOTP không được chấp nhận")
    days = await tokens.check_api_key()
    print(f"API Key còn {days if days is None else round(days, 1)} ngày")
    token = await tokens.get()
    calls: list[tuple[str, dict]] = [(EP_DERIVATIVES, {})]
    if cfg.custody_code:
        calls.insert(0, (f"/eros/v2/get-profile/by-username/{cfg.custody_code}", {"fields": "basicInfo,bankSubAccounts"}))
        for page in (0, 1, 2):
            calls.append((EP_BOOK, {"accountId": cfg.custody_code, "pageNo": page, "pageSize": 20, "symbol": "ALL,ALL",
                                    "orderType": "", "status": 0}))
        if cfg.sub_account:
            calls.append((EP_POSITIONS, {"accountId": cfg.custody_code, "subAccountId": cfg.sub_account}))
    else:
        print("⚠️ Chưa điền TCBS_CUSTODY_CODE — chỉ đọc bảng giá")
    for path, params in calls:
        r = await http.request("GET", path, params=params, token=token)
        print(f"GET {path} {params.get('pageNo', '')} → {r.status}{'' if r.error is None else f' ({r.error})'}")


def main(argv=None) -> int:
    from core import contracts
    from core.calendar import TradingCalendar
    from core.clock import VN_TZ
    from core.settings import REPO_ROOT, load_settings

    ap = argparse.ArgumentParser(description="Thăm dò API TCBS (chỉ đọc)")
    ap.add_argument("--payload-only", action="store_true")
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    settings = load_settings()
    cfg = TcbsConfig.from_settings(settings, live=False)
    symbol = contracts.trading_contract(datetime.now(VN_TZ).date(), TradingCalendar.default(),
                                        int(settings.get("contract.roll_days_before_expiry", 1)))
    if args.payload_only:
        print(json.dumps(sample_payloads(cfg, symbol), ensure_ascii=False, indent=1))
        return 0
    if not cfg.api_key:
        print("❌ Thiếu TCBS_API_KEY trong .env")
        return 2
    out = settings.path("paths.data_dir") / "probe" / f"tcbs_trading_{datetime.now():%Y%m%d_%H%M%S}"
    rec = Recorder(_requests_transport(), out)
    http = TcbsHttp(cfg.base_url, cfg.timeout_s, transport=rec)
    tokens = TcbsTokenManager(http, cfg.api_key, notify=print)
    try:
        asyncio.run(read_only(tokens, http, cfg, lambda: getpass.getpass("iOTP (app TCInvest): ")))
    finally:
        summary = summarize(rec.records)
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nMã hợp đồng TCBS trả về: {summary['symbols_seen'][:6]}")
        print(f"Đã ghi vào {out} — gửi lại thư mục này (không chứa khoá/token/OTP).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
