"""Thăm dò feed DNSE bằng khoá thật — chốt các mục ❓ ở docs/broker_notes.md §1.4.

Chạy N giây trong giờ giao dịch, in tick đã chuẩn hoá và GHI NGUYÊN VĂN các message đầu tiên
(dữ liệu thị trường, không chứa khoá) ra `data/probe/dnse_raw_<thời điểm>.jsonl` để:
- xem tên field thật (camelCase/snake_case), đơn vị `timestamp`, đơn vị giá, ý nghĩa `side`;
- thay message mẫu trong tests/fixtures/dnse_stream.json bằng message thật.

SDK chưa có endpoint danh sách hợp đồng ⇒ script bắt bạn khẳng định mã KRX bằng `--krx`
(xem trên app/bảng giá DNSE) thay cho bước đối chiếu tự động; runner thật sẽ lấy từ REST (P4.1).

    python scripts/dnse_feed_probe.py --krx 41I1GB000 --seconds 120

Kiểm đăng ký NHIỀU mã trên một kết nối (cần trước khi bật `recorder.record_next_contract_on_roll_eve`):

    python scripts/dnse_feed_probe.py --krx 41I1GB000 41I1GC000 --extra-contract VN30F2612 --seconds 120

⇒ phải thấy tick của CẢ HAI hợp đồng (dòng in ra có tên HĐ) và `ticks_by_symbol` có hai mã.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.clock import MarketClock  # noqa: E402
from core.settings import load_settings  # noqa: E402
from feeds.data_feed_dnse import DnseFeed, websockets_connector  # noqa: E402


class _Recording:
    """Bọc transport: ghi các frame NHẬN được (không ghi frame gửi đi — có chữ ký)."""

    def __init__(self, inner, out, limit):
        self.inner, self.out, self.left = inner, out, limit

    async def send(self, data):
        await self.inner.send(data)

    async def recv(self):
        frame = await self.inner.recv()
        if self.left > 0:
            self.out.write((frame if isinstance(frame, str) else frame.decode()) + "\n")
            self.out.flush()
            self.left -= 1
        return frame

    async def close(self):
        await self.inner.close()


async def main_async(args) -> int:
    try:
        from dotenv import load_dotenv
        from core.settings import REPO_ROOT
        load_dotenv(REPO_ROOT / ".env")          # Task Scheduler không đặt cwd = repo
    except ImportError:
        pass
    key, secret = os.environ.get("DNSE_API_KEY"), os.environ.get("DNSE_API_SECRET")
    if not key or not secret:
        print("❌ Thiếu DNSE_API_KEY / DNSE_API_SECRET trong .env")
        return 2
    settings = load_settings()
    mclock = MarketClock.from_settings(settings)
    out_dir = settings.path("paths.data_dir") / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"dnse_raw_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    with open(out_path, "w", encoding="utf-8") as out:
        async def connector(url):
            return _Recording(await websockets_connector(url), out, args.raw)

        async def lister():
            return [s.strip().upper() for s in args.krx]

        feed = DnseFeed.from_settings(settings, key, secret, lister, mclock, connector=connector,
                                      extra_contracts=args.extra_contract or ())
        by_symbol: dict = {}
        feed.on_tick(lambda t: by_symbol.__setitem__(t.symbol, by_symbol.get(t.symbol, 0) + 1))
        feed.on_tick(lambda t: print(f"{t.ts:%H:%M:%S.%f} {t.symbol} last={t.last} x{t.last_qty} "
                                     f"bid={t.bid}/{t.bid_qty} ask={t.ask}/{t.ask_qty} tv={t.total_volume}"))
        task = asyncio.create_task(feed.run())
        try:
            await asyncio.wait_for(asyncio.shield(task), args.seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            await feed.stop()
            if not task.done():
                task.cancel()
        s = feed.stats()
        print({k: v for k, v in s.items() if k != "per_minute"})
        print(f"ticks_by_symbol: {by_symbol}")
        if args.extra_contract:
            missing = [c for c in [feed.symbol, *feed.extra_contracts] if not by_symbol.get(c)]
            print("✅ nhận tick của mọi mã đã đăng ký" if not missing else f"❌ không có tick của {missing}")
    print(f"Message thô: {out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--krx", nargs="+", required=True, help="mã KRX hợp đồng đang niêm yết (đã kiểm tay)")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--raw", type=int, default=200, help="số message thô đầu tiên được ghi")
    ap.add_argument("--extra-contract", nargs="+", help="tên HĐ ghi thêm (VN30FYYMM; mã KRX phải có trong --krx)")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
