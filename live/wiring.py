"""Lắp các thành phần cho chế độ LIVE (runner `--live`).

Live = DNSE chính (`brokers/dnse.py`) + SSI dự phòng (`brokers/ssi.py`, chỉ dùng cho đối ứng khẩn
cấp — CLAUDE.md mục 3) + feed tổng hợp DNSE/SSI (`feeds/composite.py`: lệch giá / im lặng ⇒ ngừng
mở vị thế) + stream lệnh SSI (tác vụ nền). Feed SSI và broker SSI dùng CHUNG một
`SsiTokenManager` (SSI chỉ cho một phiên mỗi apiKey).

Không có chế độ "nửa vời": bất kỳ khoá nào thiếu (env, settings còn null, `api_verified` false,
khoá ký không đọc được, broker chính/dự phòng không phải dnse/ssi) ⇒ ném lỗi, runner thoát 2
TRƯỚC khi kết nối bất cứ thứ gì. Hàm này không gọi mạng.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

from brokers.base import LiveTradingNotAllowed
from brokers.dnse import DnseBroker, DnseConfig, DnseHttp, fetch_vn30_futures, verify_contracts
from brokers.ssi import SsiBroker, SsiConfig, SsiHttp, SsiOrderStream, SsiTokenManager, load_private_key
from core.clock import MarketClock
from core.settings import MissingSetting, Settings
from feeds.composite import CompositeFeed
from feeds.data_feed_dnse import DnseFeed, resolve_contract
from feeds.data_feed_ssi import SsiFeed

Notify = Callable[[str], Any]


@dataclass
class LiveSetup:
    primary: DnseBroker
    backup: SsiBroker
    feed: CompositeFeed
    ssi_tokens: SsiTokenManager
    contract_lister: Callable[[], Awaitable[list]]
    background: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    preflight: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def _env(env: Mapping[str, str], name: str, what: str) -> str:
    v = str(env.get(name) or "").strip()
    if not v:
        raise MissingSetting(f"{name} (.env) — {what}")
    return v


def build_live(settings: Settings, env: Mapping[str, str], *, live_flag: bool, market_clock: MarketClock,
               contract_lister: Optional[Callable[[], Awaitable[Iterable[str]]]] = None,
               notify: Optional[Notify] = None, on_feed_alert: Optional[Callable[[Any], None]] = None,
               dnse_transport=None, ssi_transport=None) -> LiveSetup:
    """`contract_lister=None` (mặc định cho live): danh sách hợp đồng lấy từ DNSE `/instruments` và
    ĐỐI CHIẾU với lịch đáo hạn của bot (CLAUDE.md mục 4); lệch ⇒ `preflight` ném lỗi ⇒ không giao dịch."""
    if not live_flag:
        raise LiveTradingNotAllowed("build_live chỉ dùng khi chạy với --live")
    primary_name = str(settings.require("brokers.primary"))
    backup_name = str(settings.require("brokers.backup"))
    if primary_name != "dnse" or backup_name != "ssi":
        raise ValueError(f"live hiện chỉ hỗ trợ brokers.primary=dnse, brokers.backup=ssi "
                         f"(đang là {primary_name}/{backup_name})")

    # ---- DNSE (chính) ----
    dnse_cfg = DnseConfig.from_settings(settings, env, live=True)
    dnse_http = DnseHttp(_env(env, "DNSE_API_KEY", "khoá API DNSE"), _env(env, "DNSE_API_SECRET", "secret DNSE"),
                         base_url=dnse_cfg.base_url, timeout=dnse_cfg.timeout_s, transport=dnse_transport)
    primary = DnseBroker(dnse_http, dnse_cfg, clock=market_clock.clock, name="dnse", live_flag=True,
                         env=env, notify=notify)

    # ---- SSI (dự phòng) ----
    ssi_cfg = SsiConfig.from_settings(settings, env, live=True)
    key = load_private_key(_env(env, "SSI_PRIVATE_KEY_PATH", "file private key RSA của SSI"))
    ssi_http = SsiHttp(ssi_cfg.base_url, ssi_cfg.timeout_s, transport=ssi_transport)
    tokens = SsiTokenManager(ssi_http, _env(env, "SSI_API_KEY", "khoá API SSI"),
                             _env(env, "SSI_API_SECRET", "secret SSI"), notify=notify)
    backup = SsiBroker(ssi_http, tokens, ssi_cfg, key, clock=market_clock.clock, name="ssi", live_flag=True,
                       env=env, notify=notify)

    # ---- danh sách hợp đồng: từ broker, đối chiếu lịch đáo hạn (CLAUDE.md mục 4) ----
    if contract_lister is None:
        async def contract_lister():
            d = market_clock.now().date()
            return verify_contracts(await fetch_vn30_futures(dnse_http, d, market_clock.calendar), d,
                                    market_clock.calendar)

    async def check_contract() -> None:
        listed = await contract_lister()
        resolve_contract(market_clock.now().date(), market_clock.calendar, market_clock.roll_days_before_expiry,
                         listed)

    # ---- feed tổng hợp (đối chiếu DNSE/SSI, CLAUDE.md mục 3) ----
    dnse_feed = DnseFeed.from_settings(settings, env["DNSE_API_KEY"], env["DNSE_API_SECRET"], contract_lister,
                                       market_clock)
    ssi_feed = SsiFeed.from_settings(settings, tokens.get, contract_lister, market_clock, url=ssi_cfg.stream_url)
    feed = CompositeFeed.from_settings(settings, dnse_feed, ssi_feed, market_clock, on_alert=on_feed_alert)

    stream = SsiOrderStream(tokens, backup)
    return LiveSetup(primary, backup, feed, tokens, contract_lister, background=[stream.run],
                     preflight=[check_contract])
