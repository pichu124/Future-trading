"""Thăm dò API giao dịch SSI FastConnect V3 — bước "gọi thử" BẮT BUỘC trước khi điền
`brokers.ssi.order_symbol_format`, `brokers.ssi.market_order_type` và đặt `api_verified: true`
(CLAUDE.md mục 8, docs/broker_notes.md §2.6). SSI KHÔNG có môi trường UAT trong SDK ⇒ script
này KHÔNG đặt lệnh.

    python scripts/ssi_trading_probe.py --payload-only     # không mạng, không khoá
    python scripts/ssi_trading_probe.py                    # xin OTP → nhập mã → chỉ ĐỌC

Chế độ đọc: masterdata (mã hợp đồng phái sinh SSI trả về: KRX hay VN30FYYMM?), sổ lệnh hôm nay
(định dạng, có bọc `data` không, mã trạng thái thật), vị thế phái sinh, header hạn mức.
Response ghi NGUYÊN VĂN vào `data/probe/ssi_trading_<thời điểm>/` (bỏ header; xoá apiSecret,
otp, accessToken, refreshToken) cùng `summary.json` — gửi thư mục đó lại để chốt các ❓.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import urllib.parse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brokers.ssi import (EP_ORDER_BOOK, EP_POSITIONS, SsiConfig, SsiHttp, SsiTokenManager,  # noqa: E402
                         _requests_transport, build_place_payload)
from core.models import Order, OrderType, Side  # noqa: E402

REDACT_KEYS = ("apiSecret", "otp", "accessToken", "refreshToken", "transactionId", "passcode")
EP_MASTER = "/api/v3/data/masterdata"


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if k in REDACT_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class Recorder:
    """Bọc transport SSI: ghi (method, path, params, body đã xoá bí mật, status, response)."""

    def __init__(self, inner, out_dir: Path):
        self.inner, self.out_dir = inner, Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []

    async def __call__(self, method, url, params, content, headers, timeout):
        path = urllib.parse.urlsplit(url).path
        try:
            body = json.loads(content) if content else None
        except ValueError:
            body = None
        rec: dict = {"n": len(self.records) + 1, "method": method, "path": path, "params": params,
                     "body": redact(body)}
        try:
            status, rheaders, text = await self.inner(method, url, params, content, headers, timeout)
        except Exception as exc:
            rec.update(status=None, error=f"{type(exc).__name__}: {exc}")
            self._write(rec)
            raise
        try:
            resp: Any = json.loads(text) if text and text.strip() else None
        except ValueError:
            resp = {"raw_text": text[:2000]}
        rec.update(status=status, response=redact(resp),
                   rate_limit={k: v for k, v in (rheaders or {}).items()
                               if k.lower().startswith(("x-ratelimit", "retry-after"))})
        self._write(rec)
        return status, rheaders, text

    def _write(self, rec: dict) -> None:
        self.records.append(rec)
        name = f"{rec['n']:03d}_{rec['method']}_{rec['path'].strip('/').replace('/', '_')}.json"
        (self.out_dir / name).write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")


def _rows(resp: Any, key: str) -> list:
    d = resp.get("data", resp) if isinstance(resp, dict) else resp
    if isinstance(d, dict):
        d = d.get(key, d.get("data"))
    return d if isinstance(d, list) else []


def summarize(records: list[dict]) -> dict:
    statuses, order_keys, der_symbols = set(), set(), set()
    wrapped: dict[str, bool] = {}
    for r in records:
        resp = r.get("response")
        if isinstance(resp, dict):
            wrapped[r["path"]] = "data" in resp
        if r["path"] == EP_ORDER_BOOK:
            for o in _rows(resp, "orderList"):
                if isinstance(o, dict):
                    order_keys.update(o)
                    if o.get("orderStatus"):
                        statuses.add(str(o["orderStatus"]))
        if r["path"] == EP_MASTER:
            for m in _rows(resp, "data"):
                if isinstance(m, dict):
                    sym = str(m.get("symbol") or m.get("Symbol") or "")
                    if sym.startswith(("41I", "VN30F")):
                        der_symbols.add(sym)
    return {"order_status_values": sorted(statuses), "order_item_keys": sorted(order_keys),
            "derivative_symbols_seen": sorted(der_symbols), "wrapped_in_data": wrapped,
            "endpoints": {f"{r['method']} {r['path']}": r.get("status") for r in records}}


def sample_payloads(cfg: SsiConfig, symbol: str) -> list[dict]:
    now = datetime.now()
    out = []
    for fmt in ("krx", "name"):
        c = replace(cfg, order_symbol_format=fmt, market_order_type=cfg.market_order_type or "MTL",
                    account_no=cfg.account_no or "<SSI_ACCOUNT>", device_id=cfg.device_id or "<deviceId>")
        out.append({"what": f"LO mua, mã dạng {fmt}", "method": "POST", "path": "/api/v3/trading/order",
                    "body": build_place_payload(Order("PROBE1", symbol, Side.BUY, 1, OrderType.LIMIT, now,
                                                      price=1000.0), c)})
    c = replace(cfg, order_symbol_format="krx", market_order_type=cfg.market_order_type or "MTL",
                account_no=cfg.account_no or "<SSI_ACCOUNT>", device_id=cfg.device_id or "<deviceId>")
    out.append({"what": "lệnh thị trường bán (stop chạm / đối ứng khẩn cấp)", "method": "POST",
                "path": "/api/v3/trading/order",
                "body": build_place_payload(Order("PROBE2", symbol, Side.SELL, 1, OrderType.MARKET, now), c)})
    return out


async def read_only(tokens: SsiTokenManager, http: SsiHttp, cfg: SsiConfig, read_otp: Callable[[], str]) -> None:
    if not await tokens.request_otp():
        raise SystemExit("❌ Không xin được OTP")
    if not await tokens.submit_otp(read_otp()):
        raise SystemExit("❌ OTP không được chấp nhận")
    token = await tokens.get()
    day = datetime.now().strftime("%Y/%m/%d")
    calls = [(EP_MASTER, {"from": day, "to": day, "pageIndex": 1, "pageSize": 1000})]
    if cfg.account_no:
        calls += [(EP_ORDER_BOOK, {"accountNo": cfg.account_no, "from": day, "to": day,
                                   "pageIndex": 1, "pageSize": 1000}),
                  (EP_POSITIONS, {"clientId": cfg.client_id or "", "querySummary": "true",
                                  "accountNo": cfg.account_no})]
    else:
        print("⚠️ Chưa điền SSI_ACCOUNT — bỏ qua sổ lệnh / vị thế")
    for path, params in calls:
        r = await http.request("GET", path, params=params, token=token)
        print(f"GET {path} → {r.status}{'' if r.error is None else f' ({r.error})'}")


def main(argv=None) -> int:
    from core import contracts
    from core.calendar import TradingCalendar
    from core.clock import VN_TZ
    from core.settings import REPO_ROOT, load_settings

    ap = argparse.ArgumentParser(description="Thăm dò API giao dịch SSI (P4.2) — không đặt lệnh")
    ap.add_argument("--payload-only", action="store_true", help="chỉ in payload, không gọi mạng")
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    settings = load_settings()
    cfg = SsiConfig.from_settings(settings, live=False)
    symbol = contracts.trading_contract(datetime.now(VN_TZ).date(), TradingCalendar.default(),
                                        int(settings.get("contract.roll_days_before_expiry", 1)))
    if args.payload_only:
        print(json.dumps(sample_payloads(cfg, symbol), ensure_ascii=False, indent=1))
        return 0
    key, secret = os.environ.get("SSI_API_KEY"), os.environ.get("SSI_API_SECRET")
    if not key or not secret:
        print("❌ Thiếu SSI_API_KEY / SSI_API_SECRET trong .env")
        return 2
    out = settings.path("paths.data_dir") / "probe" / f"ssi_trading_{datetime.now():%Y%m%d_%H%M%S}"
    rec = Recorder(_requests_transport(), out)
    http = SsiHttp(cfg.base_url, cfg.timeout_s, transport=rec)
    tokens = SsiTokenManager(http, key, secret, notify=print)
    try:
        asyncio.run(read_only(tokens, http, cfg, lambda: getpass.getpass("OTP SSI: ")))
    finally:
        summary = summarize(rec.records)
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nMã phái sinh SSI trả về: {summary['derivative_symbols_seen'][:6]}")
        print(f"orderStatus thấy được: {summary['order_status_values']}")
        print(f"Đã ghi vào {out} — gửi lại thư mục này (không chứa khoá/token/OTP).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
