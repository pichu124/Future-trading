"""`scripts/overnight_pack.py` — gói "thế giới qua đêm + tin tới sáng" cho nhật ký regime shadow. Viết TRƯỚC khi
implement. Không test nào ra mạng thật (HTTP được tiêm giả).

- Giá: Yahoo chart (`/v8/finance/chart/<mã>?range=5d&interval=1d`). Mỗi mã in: giá mới nhất + giờ của nó (giờ VN) và
  các mốc đóng ngày gần nhất (ngày theo giờ sàn), bỏ nến `close = null`. Không bao giờ in số mà không kèm thời điểm.
- Tin: RSS (cafef, vnexpress, MarketWatch). Chỉ giữ bài có giờ đăng trong [từ, tới] (giờ VN); bài ghi giờ tương lai bị
  bỏ và đếm. Mỗi nguồn tối đa `limit` bài, mới nhất trước.
- Nguồn nào lỗi ⇒ ghi vào "THIẾU" kèm lý do, không dừng cả gói.
"""
import json
from datetime import datetime, timezone

import pytest

from scripts.overnight_pack import QUOTES, build, main, parse_chart, parse_rss

NOW = datetime(2026, 11, 18, 7, 52)          # giờ VN, naive


def chart(sym, tz, rows, price, t):
    return json.dumps({"chart": {"result": [{"meta": {"symbol": sym, "exchangeTimezoneName": tz,
                                                      "regularMarketPrice": price, "regularMarketTime": t},
                                             "timestamp": [r[0] for r in rows],
                                             "indicators": {"quote": [{"close": [r[1] for r in rows]}]}}],
                                 "error": None}})


def ts(y, m, d, hh=14, mm=30):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())


SPX = chart("^GSPC", "America/New_York",
            [(ts(2026, 11, 13), 7561.67), (ts(2026, 11, 16), 7416.59), (ts(2026, 11, 17), 7340.53)],
            7340.53, ts(2026, 11, 17, 21, 42))
CL = chart("CL=F", "America/New_York",
           [(ts(2026, 11, 13, 5, 0), 88.56), (ts(2026, 11, 16, 5, 0), 85.72), (ts(2026, 11, 17, 5, 0), None)],
           85.59, ts(2026, 11, 18, 0, 53))

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Tin tương lai</title><pubDate>Sat, 21 Nov 2026 09:00:00 +0700</pubDate></item>
<item><title>Nvidia doanh thu tăng</title><pubDate>Wed, 18 Nov 2026 06:32:00 +0700</pubDate></item>
<item><title>Khối ngoại bán FPT</title><pubDate>Tue, 17 Nov 2026 18:06:00 +0700</pubDate></item>
<item><title>Tin cũ hôm qua</title><pubDate>Tue, 17 Nov 2026 10:00:00 +0700</pubDate></item>
<item><title>Phố Wall giảm</title><pubDate>Tue, 17 Nov 2026 23:30:00 GMT</pubDate></item>
</channel></rss>"""


def test_parse_chart_index_after_close():
    q = parse_chart(SPX, "^GSPC", "S&P 500")
    assert q["last"] == pytest.approx(7340.53) and q["last_time_vn"] == datetime(2026, 11, 18, 4, 42)
    assert q["closes"] == [("2026-11-13", 7561.67), ("2026-11-16", 7416.59), ("2026-11-17", 7340.53)]


def test_parse_chart_futures_drops_null_close_and_keeps_live_price():
    q = parse_chart(CL, "CL=F", "Dầu WTI")
    assert q["closes"] == [("2026-11-13", 88.56), ("2026-11-16", 85.72)]
    assert q["last"] == pytest.approx(85.59) and q["last_time_vn"] == datetime(2026, 11, 18, 7, 53)


def test_parse_chart_error_raises():
    with pytest.raises(ValueError):
        parse_chart(json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}}), "X", "X")


def test_parse_rss_window_future_and_limit():
    items, future = parse_rss(RSS, since=datetime(2026, 11, 17, 15, 0), until=NOW, limit=10)
    assert [t for _, t in items] == ["Nvidia doanh thu tăng", "Phố Wall giảm", "Khối ngoại bán FPT"]
    assert items[1][0] == datetime(2026, 11, 18, 6, 30)                    # 23:30 GMT ⇒ 06:30 giờ VN
    assert future == 1
    assert len(parse_rss(RSS, since=datetime(2026, 11, 17, 15, 0), until=NOW, limit=2)[0]) == 2


def test_build_text_has_times_and_missing():
    def http(url):
        if "GSPC" in url:
            return 200, SPX
        if "CL%3DF" in url or "CL=F" in url:
            return 200, CL
        if url.endswith(".rss") or "rss" in url:
            return 200, RSS
        return 404, ""
    text = build(NOW, http, since=datetime(2026, 11, 17, 15, 0))
    assert "S&P 500" in text and "7,340.53" in text and "18/11 04:42" in text and "17/11 7,340.53" in text
    assert "Dầu WTI" in text and "85.59" in text and "18/11 07:53" in text
    assert "giá tạm nếu phiên chưa đóng" in text and "so với ngày trước" in text
    assert "THIẾU" in text and "HTTP 404" in text                          # mã khác trả 404 ⇒ ghi thiếu, không dừng
    assert "Nvidia doanh thu tăng" in text and "Tin tương lai" not in text and "Tin cũ hôm qua" not in text
    assert "(06:32 18/11)" in text


def test_quotes_cover_us_commodities_fx_asia():
    syms = {s for s, _ in QUOTES}
    assert {"^GSPC", "^IXIC", "^DJI", "CL=F", "GC=F", "DX-Y.NYB", "^N225"} <= syms


def test_cli_uses_injected_http(capsys):
    assert main([], now=NOW, http=lambda url: (500, "")) == 0
    out = capsys.readouterr().out
    assert "THẾ GIỚI QUA ĐÊM" in out and "HTTP 500" in out
