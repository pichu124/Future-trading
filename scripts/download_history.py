"""Tải dữ liệu lịch sử VN30F từ DNSE (PROMPTS P1.6 bước 2) → `data/history/`.

Theo đặc tả OpenAPI chính thức DNSE (`docs/dnseopenapi-en.yaml` của repo `dnse-tech/dnse-py`, đọc 08/11/2026 —
docs/broker_notes.md §1.3):
- OHLC 1 phút: `GET /price/ohlc?symbol=<KRX>&type=DERIVATIVE&resolution=1&from&to` — from/to epoch GIÂY; response
  dạng cột `{t, o, h, l, c, v, nextTime}`; `nextTime` ≠ 0 ⇒ còn trang, gọi tiếp từ đó. `t` = thời điểm MỞ nến
  (suy từ ví dụ trong đặc tả — ❓, `scripts/validate_day.py`/backtest sẽ lộ ra nếu lệch).
- Khớp lệnh: `GET /price/{KRX}/trades?boardId=G1&from&to&limit` — `{trades: [...], nextPageToken}`, mới nhất trước.
  Tên tham số gửi `nextPageToken` KHÔNG có trong đặc tả (❓) ⇒ không dùng: cửa sổ trả đủ `limit` dòng thì chia đôi.
  `time` "YYYY-MM-DD HH:MM:SS": ví dụ trong đặc tả là giờ UTC (❓) ⇒ KIỂM với cửa sổ đã hỏi (UTC rồi tới giờ VN),
  không khớp cách nào ⇒ lỗi. Trùng (cửa sổ chồng nhau) lọc theo `totalVolumeTraded` (luỹ kế trong ngày).
- Chữ ký: thử ký đường dẫn KHÔNG kèm query (như SDK); 401 ⇒ thử kèm query một lần rồi nhớ cách được nhận.
- HTTP 429 ⇒ chờ `Retry-After` rồi thử lại (GET, an toàn); lỗi khác ⇒ ghi nhận ngày đó lỗi, tải ngày khác tiếp.

Đầu ra (ghi nguyên tử; chạy lại KHÔNG tải trùng — có file hoặc manifest ghi "rỗng" thì bỏ qua):
- `<out>/ohlc1m/<VN30FYYMM>/<YYYY-MM-DD>.parquet` — schema bar của backtest (`load_bars_parquet`), volume tin được;
- `<out>/trades/<VN30FYYMM>/<YYYY-MM-DD>.parquet` — schema tick của recorder (`source = "dnse_history"`);
- `<out>/_manifest.json` — số dòng từng mục, mục rỗng (vd hợp đồng đã đáo hạn quá xa), khối lượng thiếu (trades).

Mặc định mỗi ngày tải hợp đồng bot giao dịch; ngày trước roll tải thêm hợp đồng kế tiếp (đo chênh lệch
back-adjust, `core/continuous.py`). `--all-listed` tải mọi hợp đồng đang niêm yết.

    python scripts/download_history.py --start 2026-10-01 --end 2026-11-06
    python scripts/download_history.py --start 2026-01-01 --end 2026-11-06 --kinds ohlc --all-listed
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time as _time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brokers.dnse import sign_headers  # noqa: E402
from core import contracts  # noqa: E402
from core.calendar import TradingCalendar  # noqa: E402
from feeds.data_feed_dnse import PRICE_MAX, PRICE_MIN  # noqa: E402

log = logging.getLogger("download_history")

BASE_URL = "https://openapi.dnse.com.vn"
VN = timezone(timedelta(hours=7))
DAY_FROM, DAY_TO = (8, 30), (15, 0)          # cửa sổ tải mỗi ngày (giờ VN) — gồm ATO/ATC
TRADE_WINDOW_S = 30 * 60
SOURCE = "dnse_history"


class HistoryError(RuntimeError):
    pass


# ------------------------------------------------------------------ HTTP ----

class HistoryClient:
    def __init__(self, api_key: str, api_secret: str, *, base_url: str = BASE_URL, session: Any = None,
                 sleep: Callable[[float], None] = _time.sleep, min_interval_s: float = 0.25,
                 max_retries_429: int = 5, timeout: float = 30.0, now: Callable[[], float] = _time.monotonic):
        if not api_key or not api_secret:
            raise ValueError("Thiếu DNSE_API_KEY / DNSE_API_SECRET")
        if session is None:
            import requests
            session = requests.Session()
        self._key, self._secret = api_key, api_secret
        self.base_url = base_url.rstrip("/")
        self.session, self._sleep, self._now = session, sleep, now
        self.min_interval_s, self.max_retries_429, self.timeout = min_interval_s, max_retries_429, timeout
        self.sign_query = False
        self._sign_decided = False
        self._last: Optional[float] = None
        self.requests = 0

    def __repr__(self) -> str:
        return f"HistoryClient(base_url={self.base_url!r}, sign_query={self.sign_query})"

    def _pace(self) -> None:
        if self.min_interval_s > 0 and self._last is not None:
            wait = self.min_interval_s - (self._now() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._now()

    def get(self, path: str, params: dict) -> Any:
        n429 = 0
        while True:
            self._pace()
            if self.sign_query:
                target = f"{path}?{urllib.parse.urlencode(params)}"
                url, send_params = self.base_url + target, None
            else:
                target, url, send_params = path, self.base_url + path, params
            headers = sign_headers("GET", target, self._key, self._secret)
            headers["User-Agent"] = "future-trading-history"
            try:
                resp = self.session.get(url, params=send_params, headers=headers, timeout=self.timeout)
            except Exception as exc:
                raise HistoryError(f"GET {path}: lỗi mạng {type(exc).__name__}: {str(exc)[:120]}") from exc
            self.requests += 1
            status = int(resp.status_code)
            if status == 401 and not self.sign_query and not self._sign_decided:
                log.warning("401 khi ký path không kèm query ⇒ thử ký kèm query")
                self.sign_query = True
                continue
            if 200 <= status < 300:
                self._sign_decided = True
                try:
                    return resp.json()
                except ValueError as exc:
                    raise HistoryError(f"GET {path}: response không phải JSON") from exc
            if status == 429 and n429 < self.max_retries_429:
                n429 += 1
                h = {k.lower(): v for k, v in (resp.headers or {}).items()}
                try:
                    wait = float(h.get("retry-after") or 5)
                except ValueError:
                    wait = 5.0
                log.info("429 — chờ %.1fs", wait)
                self._sleep(wait)
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {}
            b = body if isinstance(body, dict) else {}
            raise HistoryError(f"GET {path}: HTTP {status} [{b.get('code', '')}] {str(b.get('message', ''))[:150]}")


def _epoch(d: date, hm: tuple[int, int]) -> int:
    return int(datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=VN).timestamp())


def _vn_naive(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, VN).replace(tzinfo=None)


# ------------------------------------------------------------------ OHLC ----

def fetch_ohlc_day(client: HistoryClient, krx: str, d: date, resolution: str = "1") -> list[dict]:
    f, to = _epoch(d, DAY_FROM), _epoch(d, DAY_TO)
    rows: dict[int, dict] = {}
    for _ in range(200):
        body = client.get("/price/ohlc", {"symbol": krx, "type": "DERIVATIVE", "resolution": resolution,
                                          "from": str(f), "to": str(to)})
        if not isinstance(body, dict) or not isinstance(body.get("t"), list):
            raise HistoryError(f"OHLC {krx} {d}: khuôn response lạ (khoá: {sorted(body) if isinstance(body, dict) else type(body).__name__})")
        cols = [body.get(k) for k in "tohlcv"]
        if not all(isinstance(c, list) and len(c) == len(cols[0]) for c in cols):
            raise HistoryError(f"OHLC {krx} {d}: các cột t/o/h/l/c/v không cùng độ dài")
        for t, o, h, l, c, v in zip(*cols):
            try:
                o, h, l, c = float(o), float(h), float(l), float(c)
            except (TypeError, ValueError):
                continue
            if not all(PRICE_MIN <= x <= PRICE_MAX for x in (o, h, l, c)):
                continue                               # sai đơn vị / dữ liệu hỏng: không tự chia
            rows[int(t)] = {"start": _vn_naive(int(t)), "open": o, "high": h, "low": l, "close": c,
                            "volume": int(v or 0)}
        nt = int(body.get("nextTime") or 0)
        if nt == 0 or nt > to:
            break
        if nt <= f:
            raise HistoryError(f"OHLC {krx} {d}: nextTime {nt} không tăng (from {f})")
        f = nt
    else:
        raise HistoryError(f"OHLC {krx} {d}: quá 200 trang")
    return [rows[k] for k in sorted(rows)]


def bars_frame(rows: Sequence[dict], contract: str):
    import pandas as pd
    df = pd.DataFrame(list(rows), columns=["start", "open", "high", "low", "close", "volume"])
    df.insert(0, "symbol", contract)
    df["interval_s"] = 60
    df["volume_reliable"] = True                       # khối lượng thật từ sở (khác dữ liệu snapshot dashboard)
    return df


# ------------------------------------------------------------------ khớp lệnh ----

def parse_trade_times(rows: Sequence[dict], f: int, t: int, slack_s: int = 60) -> list[datetime]:
    """`time` không ghi múi giờ: thử UTC (theo ví dụ đặc tả) rồi giờ VN; phải nằm trong cửa sổ đã hỏi."""
    if not rows:
        return []
    raw = []
    for r in rows:
        try:
            raw.append(datetime.strptime(str(r.get("time")), "%Y-%m-%d %H:%M:%S"))
        except ValueError as exc:
            raise HistoryError(f"trades: thời gian lạ {r.get('time')!r}") from exc
    for tz in (timezone.utc, VN):
        eps = [x.replace(tzinfo=tz).timestamp() for x in raw]
        if all(f - slack_s <= e <= t + slack_s for e in eps):
            return [_vn_naive(e) for e in eps]
    raise HistoryError(f"trades: không xác định được múi giờ của `time` (vd {rows[0].get('time')!r} ngoài cửa sổ "
                       "đã hỏi cả khi coi là UTC lẫn giờ VN)")


def fetch_trades_day(client: HistoryClient, krx: str, d: date, limit: int = 500) -> list[dict]:
    out: list[dict] = []
    saturated_leaf = 0

    def window(f: int, t: int) -> None:
        nonlocal saturated_leaf
        body = client.get(f"/price/{krx}/trades", {"boardId": "G1", "from": str(f), "to": str(t), "limit": limit})
        rows = body.get("trades") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise HistoryError(f"trades {krx} {d}: khuôn response lạ")
        rows = [r for r in rows if isinstance(r, dict)]
        if len(rows) >= limit:
            if t - f >= 2:
                mid = (f + t) // 2
                window(f, mid)
                window(mid, t)
                return
            saturated_leaf += 1                         # ≥ limit lệnh trong ~1 giây: có thể thiếu
        for r, ts in zip(rows, parse_trade_times(rows, f, t)):
            out.append({**r, "ts": ts})

    a, end = _epoch(d, DAY_FROM), _epoch(d, DAY_TO)
    while a < end:
        b = min(a + TRADE_WINDOW_S, end)
        window(a, b)
        a = b
    if saturated_leaf:
        log.warning("trades %s %s: %d cửa sổ 1 giây vẫn đầy %d dòng — có thể thiếu", krx, d, saturated_leaf, limit)
    seen: dict = {}
    for r in out:
        key = r.get("totalVolumeTraded")
        key = ("tv", key) if key is not None else ("row", r.get("time"), r.get("matchPrice"), r.get("matchQtty"))
        seen.setdefault(key, r)
    return sorted(seen.values(), key=lambda r: (r["ts"], r.get("totalVolumeTraded") or 0))


def volume_gap(rows: Sequence[dict]) -> Optional[int]:
    """KL bị thiếu giữa lệnh đầu và cuối theo `totalVolumeTraded` (0 = đủ); không có luỹ kế ⇒ None."""
    tv = [r for r in rows if r.get("totalVolumeTraded") is not None]
    if len(tv) < 2:
        return 0 if tv else None
    tv.sort(key=lambda r: int(r["totalVolumeTraded"]))
    expected = int(tv[-1]["totalVolumeTraded"]) - int(tv[0]["totalVolumeTraded"])
    got = sum(int(r.get("matchQtty") or 0) for r in tv[1:])
    return expected - got


def ticks_table(rows: Sequence[dict], contract: str):
    import pyarrow as pa
    from recorder.tick_recorder import SCHEMA
    recs = [{"symbol": contract, "ts": r["ts"], "last": float(r["matchPrice"]) if r.get("matchPrice") is not None else None,
             "last_qty": int(r["matchQtty"]) if r.get("matchQtty") is not None else None, "bid": None, "ask": None,
             "bid_qty": None, "ask_qty": None,
             "total_volume": int(r["totalVolumeTraded"]) if r.get("totalVolumeTraded") is not None else None,
             "source": SOURCE, "seq": None, "recv_ts": None, "session": None, "rx_seq": None} for r in rows]
    return pa.Table.from_pylist(recs, schema=SCHEMA)


# ------------------------------------------------------------------ kế hoạch + tải ----

def plan_days(start: date, end: date, calendar: TradingCalendar, roll_days_before_expiry: int = 1,
              all_listed: bool = False) -> list[tuple[date, str, str]]:
    out = []
    d = start
    while d <= end:
        if calendar.is_trading_day(d):
            names = [contracts.trading_contract(d, calendar, roll_days_before_expiry)]
            # ngày trước roll: cần cả HĐ kế tiếp để đo chênh lệch back-adjust (core/continuous.py)
            nxt = contracts.trading_contract(calendar.next_trading_day(d), calendar, roll_days_before_expiry)
            if nxt not in names:
                names.append(nxt)
            if all_listed:
                names += [n for n in contracts.listed_contracts(d, calendar).values() if n not in names]
            for n in names:
                out.append((d, n, contracts.krx_symbol(*contracts.contract_ym(n))))
        d += timedelta(days=1)
    return out


def download(client: HistoryClient, plan: Iterable[tuple[date, str, str]], out_dir: Path | str, *,
             kinds: Sequence[str] = ("ohlc", "trades"), force: bool = False, retry_empty: bool = False,
             trades_limit: int = 500) -> dict:
    from utils.atomic_io import read_json, write_dataframe_atomic, write_json_atomic, write_table_atomic
    out = Path(out_dir)
    man_path = out / "_manifest.json"
    manifest: dict = read_json(man_path, default={}) or {}
    summary = {"written": 0, "empty": 0, "skipped": 0, "errors": 0}
    folder = {"ohlc": "ohlc1m", "trades": "trades"}
    for d, name, krx in plan:
        for kind in kinds:
            key = f"{folder[kind]}/{name}/{d.isoformat()}"
            path = out / folder[kind] / name / f"{d.isoformat()}.parquet"
            prev = manifest.get(key)
            if not force and (path.exists() or (prev is not None and prev.get("rows") == 0 and not retry_empty)):
                summary["skipped"] += 1
                continue
            try:
                if kind == "ohlc":
                    rows = fetch_ohlc_day(client, krx, d)
                    if rows:
                        write_dataframe_atomic(bars_frame(rows, name), path)
                    entry = {"rows": len(rows)}
                else:
                    rows = fetch_trades_day(client, krx, d, limit=trades_limit)
                    if rows:
                        write_table_atomic(ticks_table(rows, name), path)
                    entry = {"rows": len(rows), "volume_gap": volume_gap(rows)}
            except Exception as exc:
                summary["errors"] += 1
                log.error("%s: %s", key, exc)
                manifest[key] = {"error": str(exc)[:200], "at": datetime.now().isoformat(timespec="seconds")}
                write_json_atomic(manifest, man_path)
                continue
            entry["at"] = datetime.now().isoformat(timespec="seconds")
            manifest[key] = entry
            write_json_atomic(manifest, man_path)
            summary["written" if entry["rows"] else "empty"] += 1
            log.info("%s: %d dòng", key, entry["rows"])
    return summary


def main(argv=None) -> int:
    from core.clock import VN_TZ
    from core.settings import REPO_ROOT, load_settings

    ap = argparse.ArgumentParser(description="Tải lịch sử VN30F từ DNSE (OHLC 1 phút + khớp lệnh)")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD (mặc định hôm qua)")
    ap.add_argument("--kinds", nargs="+", choices=["ohlc", "trades"], default=["ohlc", "trades"])
    ap.add_argument("--all-listed", action="store_true", help="tải cả F2M/F1Q/F2Q, không chỉ hợp đồng giao dịch")
    ap.add_argument("--force", action="store_true", help="tải lại cả mục đã có")
    ap.add_argument("--retry-empty", action="store_true", help="thử lại các mục lần trước rỗng")
    ap.add_argument("--out", help="thư mục đích (mặc định <data_dir>/history)")
    ap.add_argument("--base-url", default=BASE_URL)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else datetime.now(VN_TZ).date() - timedelta(days=1)
    out = Path(args.out) if args.out else settings.path("paths.data_dir") / "history"
    from recorder.tick_recorder import assert_not_in_sync_folder
    assert_not_in_sync_folder(out)
    plan = plan_days(start, end, TradingCalendar.default(), int(settings.get("contract.roll_days_before_expiry", 1)),
                     all_listed=args.all_listed)
    client = HistoryClient(key, secret, base_url=args.base_url)
    s = download(client, plan, out, kinds=args.kinds, force=args.force, retry_empty=args.retry_empty)
    print(f"Xong: {s} · {client.requests} request · ký {'kèm' if client.sign_query else 'không kèm'} query · {out}")
    return 1 if s["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
