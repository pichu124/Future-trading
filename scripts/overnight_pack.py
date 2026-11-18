"""Gói "thế giới qua đêm + tin tới sáng" cho nhật ký regime shadow (research/regime_shadow.md, Bước 1). Chỉ đọc web
công khai; chỉ nghiên cứu. Nguồn giống vnstock-dashboard (`vnstock_assets_sources.py`, `vnstock_news_feeds.py`):

- Giá: Yahoo chart (không chính thức) — chỉ số Mỹ, VIX, lợi suất 10 năm, dầu, vàng, DXY, USD/VND, Nikkei, Kospi.
  Mỗi số in kèm thời điểm của nó (giờ VN); nến đóng ngày `null` bị bỏ.
- Tin: RSS cafef (thị trường chứng khoán), vnexpress (kinh doanh), MarketWatch (top stories, market pulse). Chỉ giữ bài
  đăng trong [15:00 phiên trước, bây giờ]; bài ghi giờ tương lai bị bỏ.
- Nguồn lỗi ⇒ ghi vào THIẾU, không dừng.

    python scripts/overnight_pack.py                       # mặc định: từ 15:00 phiên giao dịch liền trước tới bây giờ
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VN = ZoneInfo("Asia/Ho_Chi_Minh")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
QUOTES: list[tuple[str, str]] = [
    ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow Jones"), ("^SOX", "Chỉ số bán dẫn SOX"),
    ("^VIX", "VIX"), ("^TNX", "Lợi suất TPCP Mỹ 10 năm (%)"), ("CL=F", "Dầu WTI"), ("BZ=F", "Dầu Brent"),
    ("GC=F", "Vàng"), ("DX-Y.NYB", "DXY"), ("VND=X", "USD/VND"), ("^N225", "Nikkei 225"), ("^KS11", "Kospi"),
]
FEEDS: list[tuple[str, str]] = [
    ("cafef", "https://cafef.vn/thi-truong-chung-khoan.rss"),
    ("vnexpress", "https://vnexpress.net/rss/kinh-doanh.rss"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MarketWatch pulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
]
Http = Callable[[str], "tuple[int, str]"]


def _urllib_http(url: str) -> "tuple[int, str]":
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def _vn(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(VN).replace(tzinfo=None)


def parse_chart(text: str, sym: str, label: str) -> dict:
    try:
        payload = json.loads(text)
        res = (payload.get("chart") or {}).get("result")
        r = res[0] if res else None
    except (ValueError, AttributeError, IndexError, TypeError) as e:
        raise ValueError(f"{sym}: JSON hỏng ({e})") from e
    if not r:
        raise ValueError(f"{sym}: không có kết quả ({(payload.get('chart') or {}).get('error')})")
    meta = r.get("meta") or {}
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
    closes = []
    for t, c in zip(r.get("timestamp") or [], ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(tz).date().isoformat()
        closes.append((d, round(float(c), 4)))
    last, t = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if last is None or t is None:
        raise ValueError(f"{sym}: thiếu giá mới nhất")
    return {"sym": sym, "label": label, "last": float(last), "last_time_vn": _vn(int(t)), "closes": closes}


def parse_rss(text: str, since: datetime, until: datetime, limit: int) -> "tuple[list[tuple[datetime, str]], int]":
    root = ET.fromstring(text)
    items, future = [], 0
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        raw = it.findtext("pubDate")
        if not title or not raw:
            continue
        try:
            d = parsedate_to_datetime(raw.strip())
        except (TypeError, ValueError):
            continue
        d = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(VN).replace(tzinfo=None)
        if d > until:
            future += 1
            continue
        if d >= since:
            items.append((d, title))
    items.sort(key=lambda x: x[0], reverse=True)
    return items[:limit], future


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def build(now: datetime, http: Http, since: datetime, limit: int = 8) -> str:
    lines = [f"THẾ GIỚI QUA ĐÊM + TIN TỚI SÁNG — lấy lúc {now:%H:%M %d/%m/%Y} (giờ VN); mọi số kèm thời điểm của nó"]
    missing = []
    lines.append("Giá (Yahoo, không chính thức):")
    for sym, label in QUOTES:
        code, body = http(YAHOO.format(sym=quote(sym, safe="")))
        if code != 200:
            missing.append(f"{label} ({sym}): HTTP {code}")
            continue
        try:
            q = parse_chart(body, sym, label)
        except ValueError as e:
            missing.append(str(e))
            continue
        cl = q["closes"][-3:]
        chg = ""
        if len(cl) >= 2 and cl[-2][1]:
            chg = f" ({(cl[-1][1] / cl[-2][1] - 1) * 100:+.2f}% so với ngày trước)"
        closes = ", ".join(f"{d[8:10]}/{d[5:7]} {_fmt(c)}" for d, c in cl)
        lines.append(f"- {label}: mới nhất {_fmt(q['last'])} lúc {q['last_time_vn']:%d/%m %H:%M}; "
                     f"giá cuối mỗi ngày (ngày theo giờ sàn; ngày cuối là giá tạm nếu phiên chưa đóng): {closes}{chg}")
    lines.append(f"Tin từ {since:%H:%M %d/%m} tới {now:%H:%M %d/%m} (giờ VN):")
    for name, url in FEEDS:
        code, body = http(url)
        if code != 200:
            missing.append(f"tin {name}: HTTP {code}")
            continue
        try:
            items, future = parse_rss(body, since, now, limit)
        except ET.ParseError as e:
            missing.append(f"tin {name}: RSS hỏng ({e})")
            continue
        extra = f" (bỏ {future} bài ghi giờ tương lai)" if future else ""
        lines.append(f"- {name}: {len(items)} bài{extra}")
        lines += [f"    • {t} ({d:%H:%M %d/%m})" for d, t in items]
    lines.append("THIẾU: " + ("; ".join(missing) if missing else "không"))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None, now: Optional[datetime] = None, http: Optional[Http] = None) -> int:
    ap = argparse.ArgumentParser(description="Gói thế giới qua đêm + tin tới sáng (chỉ đọc web công khai)")
    ap.add_argument("--limit", type=int, default=8, help="số bài tối đa mỗi nguồn tin")
    args = ap.parse_args(argv)
    now = now or datetime.now(VN).replace(tzinfo=None)
    from core.calendar import TradingCalendar
    prev = TradingCalendar.default().previous_trading_day(now.date())
    since = datetime.combine(prev, time(15, 0))
    print(build(now, http or _urllib_http, since=since, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
