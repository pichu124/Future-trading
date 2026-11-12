"""Thăm dò API giao dịch DNSE — bước "gọi thử" BẮT BUỘC trước khi đặt `brokers.dnse.api_verified: true`
(CLAUDE.md mục 8, docs/broker_notes.md §1.5).

Ba mức, từ an toàn nhất:

1. `--payload-only` — không cần khoá, không gọi mạng: in payload mà adapter SẼ gửi (dry-run).
2. mặc định — CHỈ ĐỌC: /accounts, balances, loan-packages, sổ lệnh, lịch sử lệnh, vị thế, secdef.
   Chốt: số tiểu khoản phái sinh, `loanPackageId`, khuôn response sổ lệnh/vị thế, chữ ký có được
   chấp nhận không (path không kèm query).
3. `--uat --order-test` — trên môi trường UAT (tiền ảo): xin OTP (nhập tay), đặt 1 lệnh LO giá sàn
   (khó khớp), [sửa giá nếu `--with-modify`], huỷ, ghi mọi trạng thái thấy được. Chốt: chuỗi
   `orderStatus` thật, sửa lệnh trả orderId mới và `quantity` là tổng hay phần còn lại, huỷ trả gì.
   Đi qua CHÍNH adapter `brokers/dnse.py` nên vẫn cần TRADING_MODE=live + `--live` (production bị từ chối).

Response được ghi NGUYÊN VĂN vào `data/probe/dnse_trading_<thời điểm>/` (bỏ header/chữ ký; xoá
`passcode` và `tradingToken`) cùng `summary.json` — gửi thư mục đó lại để chốt các ❓.

    python scripts/dnse_trading_probe.py --payload-only
    python scripts/dnse_trading_probe.py
    set TRADING_MODE=live && python scripts/dnse_trading_probe.py --uat --order-test --live --with-modify
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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brokers.dnse import (BASE_URL_SANDBOX, BASE_URL_UAT, MARKET_TYPE, ORDER_PARAMS, TEST_BASE_URLS,  # noqa: E402
                          DnseBroker,
                          DnseConfig, DnseHttp, DnseTransportError, _requests_transport, build_place_payload, to_krx)
from core.models import Order, OrderType, Side  # noqa: E402

REDACT_KEYS = ("passcode", "tradingToken", "trading-token", "trading_token", "token", "accessToken", "refreshToken")
SANDBOX_OTP = "666666"          # developers.dnse.com.vn/docs/guide/sandbox: OTP mô phỏng, không gửi mã thật


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if k in REDACT_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class RecordingTransport:
    """Bọc transport thật: ghi (method, path, params, body đã xoá bí mật, status, response) ra file."""

    def __init__(self, inner, out_dir: Path):
        self.inner, self.out_dir = inner, Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []

    async def __call__(self, method, url, params, json_body, headers, timeout):
        path = urllib.parse.urlsplit(url).path
        rec: dict = {"n": len(self.records) + 1, "at": datetime.now().isoformat(timespec="seconds"),
                     "method": method, "path": path, "params": params, "json": redact(json_body)}
        try:
            status, rheaders, text = await self.inner(method, url, params, json_body, headers, timeout)
        except Exception as exc:
            rec.update(status=None, error=f"{type(exc).__name__}: {exc}")
            self._write(rec)
            raise
        try:
            body: Any = json.loads(text) if text and text.strip() else None
        except ValueError:
            body = {"raw_text": text[:2000]}
        rec.update(status=status, body=redact(body),
                   rate_limit={k: v for k, v in (rheaders or {}).items()
                               if k.lower().startswith(("x-ratelimit", "retry-after"))})
        self._write(rec)
        return status, rheaders, text

    def _write(self, rec: dict) -> None:
        self.records.append(rec)
        slug = rec["path"].strip("/").replace("/", "_") or "root"
        name = f"{rec['n']:03d}_{rec['method']}_{slug}.json"
        (self.out_dir / name).write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")


def summarize(records: list[dict]) -> dict:
    """Tóm tắt các điểm ❓: chuỗi orderStatus thấy được, khoá field, status HTTP từng endpoint."""
    statuses: set[str] = set()
    order_keys: set[str] = set()
    deal_keys: set[str] = set()
    endpoints: dict[str, list] = {}

    def items(body):
        if isinstance(body, dict):
            for k in ("orders", "data"):
                if isinstance(body.get(k), list):
                    return body[k]
            if "orderStatus" in body:
                return [body]
        return body if isinstance(body, list) else []

    for r in records:
        endpoints.setdefault(f"{r['method']} {r['path']}", []).append(r.get("status"))
        body = r.get("body")
        if "/orders" in r["path"]:
            for it in items(body):
                if isinstance(it, dict):
                    order_keys.update(it)
                    if it.get("orderStatus") is not None:
                        statuses.add(str(it["orderStatus"]))
        if r["path"].endswith("/positions") and isinstance(body, dict):
            for d in body.get("positions") or body.get("deals") or []:      # server thật: "positions"
                if isinstance(d, dict):
                    deal_keys.update(d)
    return {"order_status_values": sorted(statuses), "order_item_keys": sorted(order_keys),
            "deal_keys": sorted(deal_keys), "endpoints": endpoints}


# ------------------------------------------------------------------ các bước ----

def sample_payloads(cfg: DnseConfig, symbol: str) -> list[dict]:
    now = datetime.now()
    lo = Order("PROBE1", symbol, Side.BUY, 1, OrderType.LIMIT, now, price=1000.0)
    mtl = Order("PROBE2", symbol, Side.SELL, 1, OrderType.MARKET, now)
    acc = cfg.account_no or "<DNSE_ACCOUNT_NO>"
    return [
        {"what": "đặt LO mua", "method": "POST", "path": "/accounts/orders", "params": ORDER_PARAMS,
         "json": build_place_payload(lo, cfg)},
        {"what": "đặt MTL bán (stop chạm / đóng vị thế)", "method": "POST", "path": "/accounts/orders",
         "params": ORDER_PARAMS, "json": build_place_payload(mtl, cfg)},
        {"what": "sửa (huỷ-đặt, orderId mới)", "method": "PUT", "path": f"/accounts/{acc}/orders/<orderId>",
         "params": ORDER_PARAMS, "json": {"price": 1000.1}},
        {"what": "huỷ", "method": "DELETE", "path": f"/accounts/{acc}/orders/<orderId>",
         "params": ORDER_PARAMS, "json": None},
    ]


async def read_only(http: DnseHttp, account: Optional[str], krx: str) -> Optional[str]:
    """Các GET chỉ đọc. Trả số tiểu khoản phái sinh dùng tiếp (đã điền hoặc tìm thấy)."""
    r = await http.request("GET", "/accounts")
    accs = r.body.get("accounts", []) if isinstance(r.body, dict) else []
    deriv = [str(a.get("id")) for a in accs if isinstance(a, dict) and a.get("derivativeAccount")]
    print(f"/accounts → {r.status}; tiểu khoản phái sinh: {deriv or 'không thấy'}")
    acc = account or (deriv[0] if len(deriv) == 1 else None)
    if acc is None:
        print("⚠️ Không chọn được tiểu khoản — điền DNSE_ACCOUNT_NO rồi chạy lại")
        return None
    calls = [
        ("GET", f"/accounts/{acc}/balances", None),
        ("GET", f"/accounts/{acc}/loan-packages", {"marketType": MARKET_TYPE, "symbol": krx}),
        ("GET", f"/accounts/{acc}/orders", ORDER_PARAMS),
        ("GET", f"/accounts/{acc}/orders/history", {"marketType": MARKET_TYPE,
                                                     "from": date.today().isoformat(), "to": date.today().isoformat()}),
        ("GET", f"/accounts/{acc}/positions", {"marketType": MARKET_TYPE, "pageSize": 100}),
        ("GET", f"/price/{krx}/secdef", {"boardId": "G1"}),
    ]
    for method, path, params in calls:
        r = await http.request(method, path, params=params)
        print(f"{method} {path} → {r.status}")
        if path.endswith("loan-packages") and isinstance(r.body, dict):
            for p in r.body.get("loanPackages") or []:
                print(f"   gói vay id={p.get('id')} {p.get('name')} (initialRate={p.get('initialRate')})")
    return acc


async def order_test(broker: DnseBroker, symbol: str, price: float, *, with_modify: bool,
                     read_otp: Callable[[], str], sleep=asyncio.sleep, polls: int = 5, flatten: bool = False) -> None:
    """Môi trường thử (Sandbox/UAT): đặt 1 LO → [sửa] → huỷ; đọc sổ lệnh sau mỗi bước để thấy chuỗi trạng thái.
    `flatten`: Sandbox tự khớp lệnh (PendingNew → New → PartiallyFilled → Filled) ⇒ còn vị thế thì đặt lệnh đối ứng."""
    await broker.connect()
    if not broker.token_valid():
        if not await broker.submit_otp(read_otp()):
            raise SystemExit("❌ OTP không được chấp nhận")
    o = Order("PROBE" + datetime.now().strftime("%H%M%S"), symbol, Side.BUY, 1, OrderType.LIMIT,
              datetime.now(), price=price, tag="probe")

    async def watch(label, cid):
        for _ in range(polls):
            await sleep(1)
            await broker.sync()
        cur = broker.order(cid)
        print(f"{label}: {cur.status.value} orderId={cur.broker_order_id} khớp={cur.filled_qty}")
        return cur

    placed = await broker.place(o)
    print(f"đặt: {placed.status.value} orderId={placed.broker_order_id} {placed.reject_reason}")
    if placed.status.is_terminal:
        return
    cur = await watch("sau đặt", o.client_order_id)
    if with_modify and not cur.status.is_terminal:
        m = await broker.modify(o.client_order_id, price=round(price + broker.config.tick_size, 1))
        print(f"sửa: orderId {placed.broker_order_id} → {m.broker_order_id}")
        cur = await watch("sau sửa", o.client_order_id)
    if not cur.status.is_terminal:
        await broker.cancel(o.client_order_id)
        await watch("sau huỷ", o.client_order_id)
    if flatten:
        pos = (await broker.positions()).get(symbol)
        print(f"vị thế sau test: {pos.qty if pos else 0}")
        if pos and pos.qty:
            side = Side.SELL if pos.qty > 0 else Side.BUY
            back = Order("PROBEX" + datetime.now().strftime("%H%M%S"), symbol, side, abs(pos.qty), OrderType.LIMIT,
                         datetime.now(), price=price, tag="probe")
            b = await broker.place(back)
            print(f"đối ứng {side.value} {abs(pos.qty)}: {b.status.value} orderId={b.broker_order_id}")
            if not b.status.is_terminal:
                await watch("sau đối ứng", back.client_order_id)


def _first_loan_package(records: list[dict]) -> Optional[int]:
    """loanPackageId đầu tiên từ GET …/loan-packages đã ghi (Sandbox có gói vay riêng, khác production)."""
    for r in reversed(records):
        if r["path"].endswith("/loan-packages") and isinstance(r.get("body"), dict):
            for p in r["body"].get("loanPackages") or []:
                if isinstance(p, dict) and p.get("id") is not None:
                    return int(p["id"])
    return None


def _floor_price(body: Any) -> Optional[float]:
    if isinstance(body, dict):
        for k in ("floor", "floorPrice", "floor_price"):
            if body.get(k) is not None:
                return float(body[k])
    return None


def main(argv=None) -> int:
    from core import contracts
    from core.calendar import TradingCalendar
    from core.clock import SystemClock, VN_TZ
    from core.settings import REPO_ROOT, load_settings

    ap = argparse.ArgumentParser(description="Thăm dò API giao dịch DNSE (P4.1)")
    ap.add_argument("--payload-only", action="store_true", help="chỉ in payload, không gọi mạng")
    ap.add_argument("--uat", action="store_true", help=f"dùng {BASE_URL_UAT} (DNSE không cấp cho khách hàng)")
    ap.add_argument("--sandbox", action="store_true",
                    help=f"dùng Sandbox {BASE_URL_SANDBOX} với DNSE_SANDBOX_API_KEY/SECRET (tiền ảo, OTP {SANDBOX_OTP})")
    ap.add_argument("--order-test", action="store_true", help="Sandbox/UAT: đặt/sửa/huỷ thử 1 lệnh LO")
    ap.add_argument("--with-modify", action="store_true")
    ap.add_argument("--live", action="store_true", help="cờ --live (cùng TRADING_MODE=live) cho lệnh UAT")
    ap.add_argument("--price", type=float, help="giá LO thử (mặc định: giá sàn từ secdef)")
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    settings = load_settings()
    cfg = DnseConfig.from_settings(settings, live=False)
    if args.uat and args.sandbox:
        print("❌ Chọn MỘT trong --uat / --sandbox")
        return 2
    if args.uat:
        cfg = replace(cfg, base_url=BASE_URL_UAT)
    if args.sandbox:      # tài khoản Sandbox khác production — không dùng DNSE_ACCOUNT_NO
        cfg = replace(cfg, base_url=BASE_URL_SANDBOX, account_no=os.environ.get("DNSE_SANDBOX_ACCOUNT_NO") or None,
                      loan_package_id=None)
    today = datetime.now(VN_TZ).date()
    symbol = contracts.trading_contract(today, TradingCalendar.default(),
                                        int(settings.get("contract.roll_days_before_expiry", 1)))
    krx = to_krx(symbol)
    if args.payload_only:
        print(json.dumps(sample_payloads(cfg, symbol), ensure_ascii=False, indent=1))
        return 0
    if args.order_test and cfg.base_url not in TEST_BASE_URLS:
        print("❌ --order-test chỉ chạy trên Sandbox (thêm --sandbox). Production không bao giờ đặt lệnh thử.")
        return 2
    kv = ("DNSE_SANDBOX_API_KEY", "DNSE_SANDBOX_API_SECRET") if args.sandbox else ("DNSE_API_KEY", "DNSE_API_SECRET")
    key, secret = os.environ.get(kv[0]), os.environ.get(kv[1])
    if not key or not secret:     # Sandbox KHÔNG BAO GIỜ lấy key production (và ngược lại)
        print(f"❌ Thiếu {kv[0]} / {kv[1]} trong .env")
        return 2
    out = settings.path("paths.data_dir") / "probe" / f"dnse_trading_{datetime.now():%Y%m%d_%H%M%S}"
    rec = RecordingTransport(_requests_transport(cfg.timeout_s), out)
    http = DnseHttp(key, secret, base_url=cfg.base_url, timeout=cfg.timeout_s, transport=rec,
                    api_version=cfg.api_version)
    print(f"Base URL: {cfg.base_url} · hợp đồng {symbol} ({krx})")
    code = 0
    try:
        acc = asyncio.run(read_only(http, cfg.account_no, krx))
        if args.order_test and acc:
            secdef = next((r.get("body") for r in reversed(rec.records) if r["path"].endswith("/secdef")), None)
            if cfg.loan_package_id is None:
                cfg = replace(cfg, loan_package_id=_first_loan_package(rec.records))
            price = args.price or _floor_price(secdef)
            if price is None:
                print("❌ Không đọc được giá sàn từ secdef — truyền --price")
                code = 2
            else:
                broker = DnseBroker(http, replace(cfg, account_no=acc), clock=SystemClock(),
                                    live_flag=args.live, notify=print)
                read_otp = (lambda: SANDBOX_OTP) if args.sandbox else (lambda: getpass.getpass("OTP trong email: "))
                asyncio.run(order_test(broker, symbol, price, with_modify=args.with_modify,
                                       read_otp=read_otp, flatten=args.sandbox))
    except DnseTransportError as exc:
        host = urllib.parse.urlsplit(cfg.base_url).hostname
        print(f"❌ Không kết nối được {host}: {exc}\n"
              "   Máy chủ không trả lời (timeout / bị chặn) — không phải lỗi khoá. Kiểm: "
              f"Test-NetConnection {host} -Port 443 ; Resolve-DnsName {host}")
        code = 2
    finally:
        summary = summarize(rec.records)
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\norderStatus thấy được: {summary['order_status_values']}")
        print(f"Đã ghi response thô + summary.json vào {out} — gửi lại thư mục này (không chứa khoá/token/OTP).")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
