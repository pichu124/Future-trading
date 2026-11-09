"""Tải dữ liệu lịch sử VN30F từ DNSE (PROMPTS P1.6 bước 2) → `data/history/`.

Theo đặc tả OpenAPI chính thức DNSE (`docs/dnseopenapi-en.yaml` của repo `dnse-tech/dnse-py`, đọc 08/11/2026 —
docs/broker_notes.md §1.3):
- **Nến 1 phút dựng từ KHỚP LỆNH** (mặc định): probe 09/11/2026 cho thấy `GET /price/ohlc` không dùng được — HĐ đã
  đáo hạn ⇒ 400 "invalid symbol", HĐ hiện tại ⇒ trang rỗng `nextTime = from`. Khớp lệnh thì có cả HĐ đã đáo hạn
  (≥ 6 tháng) ⇒ nến dựng như recorder; volume chỉ "tin được" khi KL luỹ kế khớp (`volume_gap = 0`).
  `--kinds ohlc` vẫn gọi endpoint OHLC (dạng cột `{t, o, h, l, c, v, nextTime}`, epoch giây) nếu sau này dùng được.
- Khớp lệnh: `GET /price/{KRX}/trades?boardId=G1&from&to&limit` — `{trades: [...], nextPageToken}`, mới nhất trước.
  Tên tham số gửi `nextPageToken` KHÔNG có trong đặc tả (❓) ⇒ không dùng: cửa sổ trả đủ `limit` dòng thì chia đôi.
  `time` "YYYY-MM-DD HH:MM:SS[.fff]": đặc tả ví dụ giờ UTC, server THẬT trả giờ VN có mili-giây (probe 09/11/2026)
  ⇒ KIỂM với cửa sổ đã hỏi (UTC rồi tới giờ VN), không khớp cách nào ⇒ lỗi. Trùng (cửa sổ chồng nhau) lọc theo `totalVolumeTraded` (luỹ kế trong ngày).
- Chữ ký: thử ký đường dẫn KHÔNG kèm query (như SDK); 401 ⇒ thử kèm query một lần rồi nhớ cách được nhận.
- HTTP 429 ngắn ⇒ chờ `Retry-After` rồi thử lại; HẾT HẠN MỨC NGÀY (DNSE: 10.000 request /price mỗi 24 giờ — `Remaining 0`
  hoặc `Retry-After` > 5 phút) ⇒ dừng cả lượt, báo mốc reset, chạy lại sau đó để tải tiếp (mã thoát 3).
- Cửa sổ đầy `limit` (tối đa 1000) ⇒ phân trang bằng `nextPageToken` (tên tham số tự kiểm); không được ⇒ chia đôi.
- Lỗi khác ⇒ ghi nhận ngày đó lỗi, tải ngày khác tiếp.

Đầu ra (ghi nguyên tử; chạy lại KHÔNG tải trùng — có file hoặc manifest ghi "rỗng" thì bỏ qua):
- `<out>/ohlc1m/<VN30FYYMM>/<YYYY-MM-DD>.parquet` — schema bar của backtest (`load_bars_parquet`), dựng từ khớp lệnh
  (`source = "dnse_trades"`), `volume_reliable` theo kiểm KL luỹ kế;
- `<out>/trades/<VN30FYYMM>/<YYYY-MM-DD>.parquet` — schema tick của recorder (`source = "dnse_history"`);
- `<out>/_manifest.json` — số dòng từng mục, mục rỗng (vd hợp đồng đã đáo hạn quá xa), khối lượng thiếu (trades).

Mặc định mỗi ngày tải hợp đồng bot giao dịch; ngày trước roll tải thêm hợp đồng kế tiếp (đo chênh lệch
back-adjust, `core/continuous.py`). `--all-listed` tải mọi hợp đồng đang niêm yết.

    python scripts/download_history.py --start 2026-10-01 --end 2026-11-06
    python scripts/download_history.py --start 2026-05-01 --end 2026-11-06 --all-listed
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
MAX_LIMIT = 1000                              # server: "limit must be between 1 and 1000" (probe 09/11/2026)
PAGE_PARAMS = ("nextPageToken", "pageToken")  # tên tham số gửi token trang — CHƯA có trong tài liệu ⇒ thử + tự kiểm
SOURCE = "dnse_history"


class HistoryError(RuntimeError):
    pass


class QuotaExhausted(HistoryError):
    """Hết hạn mức request (DNSE: 10.000 request /price mỗi 24 giờ — probe 09/11/2026). Dừng cả lượt tải; chạy lại
    sau mốc reset sẽ tải tiếp phần còn thiếu (ngày dở không bị đánh dấu)."""


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
            if status == 429:
                h = {k.lower(): v for k, v in (resp.headers or {}).items()}
                ra = h.get("retry-after")
                try:
                    ra_s = float(ra) if ra is not None else None
                except ValueError:
                    ra_s = None
                if (ra_s is None and str(h.get("x-ratelimit-remaining", "")).strip() == "0") or \
                        (ra_s is not None and ra_s > 300):
                    reset = h.get("x-ratelimit-reset")
                    when = ""
                    try:
                        when = f" — reset lúc {_vn_naive(int(float(reset))):%Y-%m-%d %H:%M} (giờ VN)" if reset else \
                            f" — chờ {ra_s / 3600:.1f} giờ"
                    except (TypeError, ValueError, OverflowError):
                        pass
                    raise QuotaExhausted(f"Hết hạn mức request DNSE{when}")
            if status == 429 and n429 < self.max_retries_429:
                n429 += 1
                try:
                    wait = float(ra) if ra is not None else 5.0
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
            if not cols[0]:
                break                                  # server thật: trang rỗng trả nextTime = from ⇒ không có nến
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


def unreliable_minutes(rows: Sequence[dict]) -> Optional[set]:
    """Phút có lệnh bị thiếu theo KL luỹ kế: sắp theo `totalVolumeTraded`, lệnh i phải có luỹ kế = luỹ kế(i−1) +
    KL(i) (lệnh đầu tiên của ngày: = KL của nó); lệch ⇒ phút của lệnh i−1 VÀ lệnh i đều bị đánh dấu. Thiếu luỹ kế ở bất kỳ dòng ⇒ None
    (không kiểm được)."""
    if not rows or any(r.get("totalVolumeTraded") is None for r in rows):
        return None
    def minute(r):
        return r["ts"].replace(second=0, microsecond=0)
    srt = sorted(rows, key=lambda r: int(r["totalVolumeTraded"]))
    bad, prev_tv, prev_row = set(), 0, None
    for r in srt:
        tv, q = int(r["totalVolumeTraded"]), int(r.get("matchQtty") or 0)
        if tv != prev_tv + q:
            bad.add(minute(r))                 # lệnh thiếu nằm GIỮA lệnh trước và lệnh này ⇒ đánh dấu cả hai phút
            if prev_row is not None:
                bad.add(minute(prev_row))
        prev_tv, prev_row = tv, r
    return bad


def bars_from_trades(rows: Sequence[dict], contract: str, volume_reliable: bool = True,
                     bad_minutes: Optional[set] = None):
    """Nến 1 phút dựng từ khớp lệnh (rows có `ts`, `matchPrice`, `matchQtty`, theo thời gian) — cùng schema
    `bars_frame`. `/price/ohlc` của DNSE không dùng được (HĐ đã đáo hạn ⇒ "invalid symbol", HĐ hiện tại ⇒ rỗng;
    probe 09/11/2026) nên nến lấy từ khớp lệnh như recorder. Volume của phút có lệnh bị thiếu (`bad_minutes`)
    hoặc khi không kiểm được (`volume_reliable=False`) bị đánh dấu không tin được."""
    import pandas as pd
    groups: dict[datetime, list[dict]] = {}
    for r in rows:
        if r.get("matchPrice") is None:
            continue
        ts = r["ts"]
        groups.setdefault(ts.replace(second=0, microsecond=0), []).append(r)
    recs = []
    for start in sorted(groups):
        g = groups[start]
        px = [float(r["matchPrice"]) for r in g]
        recs.append({"symbol": contract, "start": start, "open": px[0], "high": max(px), "low": min(px),
                     "close": px[-1], "volume": sum(int(r.get("matchQtty") or 0) for r in g), "interval_s": 60,
                     "volume_reliable": bool(volume_reliable) and start not in (bad_minutes or set()),
                     "source": "dnse_trades"})
    return pd.DataFrame(recs, columns=["symbol", "start", "open", "high", "low", "close", "volume", "interval_s",
                                       "volume_reliable", "source"])


def _trade_rows_from_file(path: Path) -> list[dict]:
    import pyarrow.parquet as pq
    out = [{"ts": r["ts"], "matchPrice": r["last"], "matchQtty": r["last_qty"], "totalVolumeTraded": r["total_volume"]}
           for r in pq.read_table(path).to_pylist()]
    return sorted(out, key=lambda r: (r["ts"], r.get("totalVolumeTraded") or 0))


# ------------------------------------------------------------------ khớp lệnh ----

def parse_trade_times(rows: Sequence[dict], f: int, t: int, slack_s: int = 60) -> list[datetime]:
    """`time` không ghi múi giờ: thử UTC (theo ví dụ đặc tả) rồi giờ VN; phải nằm trong cửa sổ đã hỏi.

    Server thật (probe 09/11/2026) trả GIỜ VN có mili-giây: "2026-11-06 10:04:58.705"; đặc tả ví dụ không có
    mili-giây ⇒ nhận cả hai dạng "YYYY-MM-DD HH:MM:SS[.fff]".
    """
    if not rows:
        return []
    raw = []
    for r in rows:
        txt = str(r.get("time"))
        try:
            if len(txt) < 19 or txt[10] not in " T":
                raise ValueError(txt)
            raw.append(datetime.fromisoformat(txt))
        except ValueError as exc:
            raise HistoryError(f"trades: thời gian lạ {r.get('time')!r}") from exc
    for tz in (timezone.utc, VN):
        eps = [x.replace(tzinfo=tz).timestamp() for x in raw]
        if all(f - slack_s <= e <= t + slack_s for e in eps):
            return [_vn_naive(e) for e in eps]
    raise HistoryError(f"trades: không xác định được múi giờ của `time` (vd {rows[0].get('time')!r} ngoài cửa sổ "
                       "đã hỏi cả khi coi là UTC lẫn giờ VN)")


def _trades_page(body) -> tuple[list[dict], str]:
    rows = body.get("trades") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise HistoryError("trades: khuôn response lạ")
    tok = body.get("nextPageToken") if isinstance(body, dict) else None
    return [r for r in rows if isinstance(r, dict)], (str(tok) if tok else "")


def _older_and_disjoint(new: list[dict], seen_totals: set) -> bool:
    """Trang sau hợp lệ ⇔ mọi lệnh có KL luỹ kế, không trùng lệnh đã có, và CŨ hơn (luỹ kế nhỏ hơn) lệnh cũ nhất đã có."""
    totals = [r.get("totalVolumeTraded") for r in new]
    if not new or any(tv is None for tv in totals) or not seen_totals:
        return False
    return not (set(totals) & seen_totals) and max(int(x) for x in totals) < min(int(x) for x in seen_totals)


def fetch_trades_day(client: HistoryClient, krx: str, d: date, limit: int = MAX_LIMIT) -> list[dict]:
    out: list[dict] = []
    saturated_leaf = 0
    page_param: list = [None]                         # tên tham số trang đã kiểm là chạy được (nhớ cho các cửa sổ sau)
    paging_ok: list = [None]                          # None = chưa biết, True/False sau lần thử đầu

    def get(f: int, t: int, extra: Optional[dict] = None):
        return _trades_page(client.get(f"/price/{krx}/trades",
                                       {"boardId": "G1", "from": str(f), "to": str(t), "limit": limit, **(extra or {})}))

    def page_rest(f: int, t: int, rows: list[dict], token: str) -> tuple[list[dict], bool]:
        """≥ limit lệnh trong 1 giây (khớp định kỳ ATO 09:00:00 / ATC 14:45:00): không chia nhỏ hơn giây được ⇒
        đi tiếp bằng `nextPageToken`. Tên tham số gửi token chưa có tài liệu ⇒ thử từng tên, chỉ nhận khi trang
        sau CŨ hơn và không trùng (tự kiểm); server lờ / từ chối ⇒ giữ phần đã có. Trả (rows, đủ hay chưa)."""
        names = [page_param[0]] if page_param[0] else list(PAGE_PARAMS)
        for _ in range(100):                          # chặn vòng lặp vô hạn
            if not token:
                return rows, True
            seen = {r.get("totalVolumeTraded") for r in rows if r.get("totalVolumeTraded") is not None}
            got = None
            for name in names:
                try:
                    more, nxt = get(f, t, {name: token})
                except QuotaExhausted:
                    raise
                except HistoryError as exc:
                    log.info("trades %s %s: tham số trang %r bị từ chối (%s)", krx, d, name, exc)
                    continue
                if _older_and_disjoint(more, seen):
                    if page_param[0] is None:
                        log.info("trades %s %s: phân trang chạy bằng tham số %r", krx, d, name)
                    got, page_param[0], names = (more, nxt), name, [name]
                    paging_ok[0] = True
                    break
            if got is None:
                if page_param[0] is None:
                    paging_ok[0] = False                  # không tên nào chạy ⇒ các cửa sổ sau chỉ chia đôi
                return rows, False
            rows = rows + got[0]
            token = got[1]
            if len(got[0]) < limit:
                return rows, True
        return rows, False

    def window(f: int, t: int) -> None:
        nonlocal saturated_leaf
        try:
            rows, token = get(f, t)
        except QuotaExhausted:
            raise
        except HistoryError as exc:
            raise HistoryError(f"trades {krx} {d}: {exc}") from exc
        if len(rows) >= limit:
            # cửa sổ đầy: phân trang trước (ít request hơn chia đôi ~3 lần); server không hỗ trợ ⇒ chia đôi theo giây
            if paging_ok[0] is not False:
                paged, full = page_rest(f, t, rows, token)
                if full:
                    rows = paged
                    for r, ts in zip(rows, parse_trade_times(rows, f, t)):
                        out.append({**r, "ts": ts})
                    return
                if t - f < 2:
                    rows = paged
            if t - f >= 2:
                mid = (f + t) // 2
                window(f, mid)
                window(mid, t)
                return
            saturated_leaf += 1                       # phút này sẽ bị đánh dấu volume không tin được
        for r, ts in zip(rows, parse_trade_times(rows, f, t)):
            out.append({**r, "ts": ts})

    a, end = _epoch(d, DAY_FROM), _epoch(d, DAY_TO)
    while a < end:
        b = min(a + TRADE_WINDOW_S, end)
        window(a, b)
        a = b
    if saturated_leaf:
        log.warning("trades %s %s: %d giây vẫn đầy %d dòng sau khi thử phân trang — thiếu lệnh, phút đó đánh dấu volume không tin được", krx, d, saturated_leaf, limit)
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
             kinds: Sequence[str] = ("trades",), force: bool = False, retry_empty: bool = False,
             trades_limit: int = MAX_LIMIT) -> dict:
    """Khớp lệnh ⇒ `trades/…` + nến 1 phút dựng từ khớp lệnh ⇒ `ohlc1m/…` (không ghi đè nến đã có trừ khi
    `force`; thiếu nến mà đã có file khớp lệnh ⇒ dựng lại từ file, không gọi mạng). `ohlc` = endpoint OHLC của
    DNSE (tuỳ chọn; trên dây trả rỗng/"invalid symbol")."""
    from utils.atomic_io import read_json, write_dataframe_atomic, write_json_atomic, write_table_atomic
    out = Path(out_dir)
    man_path = out / "_manifest.json"
    manifest: dict = read_json(man_path, default={}) or {}
    summary = {"written": 0, "empty": 0, "skipped": 0, "errors": 0, "bars": 0}
    folder = {"ohlc": "ohlc1m", "trades": "trades"}

    def write_bars(rows, name, d, fresh: bool) -> None:
        bars_path = out / "ohlc1m" / name / f"{d.isoformat()}.parquet"
        if not rows or (bars_path.exists() and not (force and fresh)):
            return
        gap = volume_gap(rows)
        bad = unreliable_minutes(rows)
        write_dataframe_atomic(bars_from_trades(rows, name, volume_reliable=bad is not None, bad_minutes=bad),
                               bars_path)
        manifest[f"ohlc1m/{name}/{d.isoformat()}"] = {
            "rows": len(rows), "from": "trades", "volume_gap": gap,
            "unreliable_minutes": None if bad is None else sorted(m.strftime("%H:%M") for m in bad),
            "at": datetime.now().isoformat(timespec="seconds")}
        summary["bars"] += 1

    for d, name, krx in plan:
        for kind in kinds:
            key = f"{folder[kind]}/{name}/{d.isoformat()}"
            path = out / folder[kind] / name / f"{d.isoformat()}.parquet"
            prev = manifest.get(key)
            if not force and (path.exists() or (prev is not None and prev.get("rows") == 0 and not retry_empty)):
                summary["skipped"] += 1
                if kind == "trades" and path.exists():
                    try:
                        write_bars(_trade_rows_from_file(path), name, d, fresh=False)   # nến thiếu ⇒ dựng lại
                        write_json_atomic(manifest, man_path)
                    except Exception as exc:
                        summary["errors"] += 1
                        log.error("%s: dựng nến từ file khớp lệnh lỗi: %s", key, exc)
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
                        write_bars(rows, name, d, fresh=True)
                    entry = {"rows": len(rows), "volume_gap": volume_gap(rows)}
            except QuotaExhausted as exc:
                summary["stopped"] = f"{exc} — chạy lại đúng lệnh này sau mốc đó để tải tiếp (ngày đã xong được bỏ qua)"
                write_json_atomic(manifest, man_path)
                log.warning("%s", summary["stopped"])
                return summary
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
    ap.add_argument("--kinds", nargs="+", choices=["ohlc", "trades"], default=["trades"],
                    help="trades (mặc định: khớp lệnh + nến 1 phút dựng từ khớp lệnh); ohlc = endpoint OHLC DNSE "
                         "(trên dây trả rỗng / 'invalid symbol' với HĐ đã đáo hạn)")
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
    if s.get("stopped"):
        print(f"⏸  {s['stopped']}")
        return 3
    return 1 if s["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
