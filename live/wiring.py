"""Lắp các thành phần cho chế độ LIVE (runner `--live`).

Live = broker chính + broker dự phòng, chọn trong {dnse, ssi, tcbs} và phải khác nhau (mặc định DNSE chính +
SSI dự phòng; dự phòng chỉ dùng cho đối ứng khẩn cấp — CLAUDE.md mục 3; TCBS chỉ khi `brokers.tcbs_enabled`
và `api_verified`) + feed tổng hợp LUÔN là DNSE + SSI (`feeds/composite.py`: lệch giá / im lặng ⇒ ngừng mở vị
thế) + stream lệnh của broker SSI/TCBS (tác vụ nền). Token SSI dùng CHUNG cho feed và broker SSI (một phiên mỗi
apiKey); SSI không phải broker ⇒ token feed nằm trong `auth` (runner chờ `/otp ssi`).

Không có chế độ "nửa vời": bất kỳ khoá nào thiếu (env, settings còn null, `api_verified` false, TCBS chưa bật,
khoá ký không đọc được, broker không hỗ trợ / trùng nhau) ⇒ ném lỗi, runner thoát 2 TRƯỚC khi kết nối bất cứ
thứ gì. Hàm này không gọi mạng.
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


def build_paper_feed(settings: Settings, env: Mapping[str, str], *, market_clock: MarketClock,
                     contract_lister: Callable[[], Awaitable[Iterable[str]]],
                     on_feed_alert: Optional[Callable[[Any], None]] = None, notify: Optional[Notify] = None,
                     ssi_transport=None) -> tuple[Any, list]:
    """Feed cho PAPER: `paper.composite_feed: true` ⇒ DNSE + SSI qua `CompositeFeed` (như live; trả kèm
    token manager SSI để runner chờ `/otp ssi`), ngược lại chỉ feed DNSE. Không gọi mạng."""
    dnse_feed = DnseFeed.from_settings(settings, _env(env, "DNSE_API_KEY", "khoá API DNSE (feed)"),
                                       _env(env, "DNSE_API_SECRET", "secret DNSE (feed)"), contract_lister,
                                       market_clock)
    if not bool(settings.get("paper.composite_feed", False)):
        return dnse_feed, []
    ssi_http = SsiHttp(str(settings.get("brokers.ssi.base_url") or "https://api.ssi.com.vn"),
                       float(settings.get("brokers.ssi.request_timeout_seconds", 10)), transport=ssi_transport)
    tokens = SsiTokenManager(ssi_http, _env(env, "SSI_API_KEY", "khoá API SSI (feed dự phòng)"),
                             _env(env, "SSI_API_SECRET", "secret SSI (feed dự phòng)"), notify=notify)
    kw = {"url": settings.get("brokers.ssi.stream_url")} if settings.get("brokers.ssi.stream_url") else {}
    ssi_feed = SsiFeed.from_settings(settings, tokens.get, contract_lister, market_clock, **kw)
    feed = CompositeFeed.from_settings(settings, dnse_feed, ssi_feed, market_clock, on_alert=on_feed_alert)
    return feed, [tokens]


@dataclass
class LiveSetup:
    primary: Any                                   # DnseBroker | SsiBroker | TcbsBroker
    backup: Any
    feed: CompositeFeed
    ssi_tokens: SsiTokenManager
    contract_lister: Callable[[], Awaitable[list]]
    background: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    preflight: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    auth: list = field(default_factory=list)       # cần OTP mà không phải broker (token SSI cho feed)


def _env(env: Mapping[str, str], name: str, what: str) -> str:
    v = str(env.get(name) or "").strip()
    if not v:
        raise MissingSetting(f"{name} (.env) — {what}")
    return v


SUPPORTED_BROKERS = ("dnse", "ssi", "tcbs")


def build_live(settings: Settings, env: Mapping[str, str], *, live_flag: bool, market_clock: MarketClock,
               contract_lister: Optional[Callable[[], Awaitable[Iterable[str]]]] = None,
               notify: Optional[Notify] = None, on_feed_alert: Optional[Callable[[Any], None]] = None,
               dnse_transport=None, ssi_transport=None, tcbs_transport=None) -> LiveSetup:
    """Lắp live: `brokers.primary` / `brokers.backup` ∈ {dnse, ssi, tcbs}, khác nhau (TCBS cần
    `brokers.tcbs_enabled` + `api_verified`). Feed LUÔN là tổng hợp DNSE + SSI (CLAUDE.md mục 3) — broker nào
    không có mặt thì phần feed của nó vẫn cần khoá API (DNSE: HMAC; SSI: token ⇒ `/otp ssi`, nằm trong `auth`).

    `contract_lister=None` (mặc định cho live): danh sách hợp đồng lấy từ DNSE `/instruments` và ĐỐI CHIẾU với
    lịch đáo hạn của bot (CLAUDE.md mục 4); lệch ⇒ `preflight` ném lỗi ⇒ không giao dịch."""
    if not live_flag:
        raise LiveTradingNotAllowed("build_live chỉ dùng khi chạy với --live")
    primary_name = str(settings.require("brokers.primary")).strip().lower()
    backup_name = str(settings.require("brokers.backup")).strip().lower()
    for n in (primary_name, backup_name):
        if n not in SUPPORTED_BROKERS:
            raise ValueError(f"broker {n!r} không hỗ trợ — chọn trong: {', '.join(SUPPORTED_BROKERS)}")
    if primary_name == backup_name:
        raise ValueError(f"brokers.primary và brokers.backup phải khác nhau (đang cùng là {primary_name})")
    roles = (primary_name, backup_name)
    clock = market_clock.clock
    brokers: dict[str, Any] = {}
    background: list = []
    auth: list = []

    # ---- DNSE: broker (nếu được chọn) + luôn có HTTP ký HMAC cho feed và /instruments ----
    dnse_key, dnse_secret = _env(env, "DNSE_API_KEY", "khoá API DNSE"), _env(env, "DNSE_API_SECRET", "secret DNSE")
    if "dnse" in roles:
        dnse_cfg = DnseConfig.from_settings(settings, env, live=True)
        dnse_http = DnseHttp(dnse_key, dnse_secret, base_url=dnse_cfg.base_url, timeout=dnse_cfg.timeout_s,
                             transport=dnse_transport)
        brokers["dnse"] = DnseBroker(dnse_http, dnse_cfg, clock=clock, name="dnse", live_flag=True, env=env,
                                     notify=notify)
    else:
        dnse_http = DnseHttp(dnse_key, dnse_secret, base_url=str(settings.get("brokers.dnse.base_url") or
                                                                  "https://openapi.dnse.com.vn"),
                             timeout=float(settings.get("brokers.dnse.request_timeout_seconds", 10)),
                             transport=dnse_transport)

    # ---- SSI: broker (nếu được chọn) + luôn có token cho feed (một phiên mỗi apiKey ⇒ dùng chung) ----
    ssi_url = str(settings.get("brokers.ssi.stream_url") or "wss://stream.ssi.com.vn/ws/v3")
    if "ssi" in roles:
        ssi_cfg = SsiConfig.from_settings(settings, env, live=True)
        key = load_private_key(_env(env, "SSI_PRIVATE_KEY_PATH", "file private key RSA của SSI"))
        ssi_http = SsiHttp(ssi_cfg.base_url, ssi_cfg.timeout_s, transport=ssi_transport)
        tokens = SsiTokenManager(ssi_http, _env(env, "SSI_API_KEY", "khoá API SSI"),
                                 _env(env, "SSI_API_SECRET", "secret SSI"), notify=notify)
        brokers["ssi"] = SsiBroker(ssi_http, tokens, ssi_cfg, key, clock=clock, name="ssi", live_flag=True,
                                   env=env, notify=notify)
        background.append(SsiOrderStream(tokens, brokers["ssi"]).run)
        ssi_url = ssi_cfg.stream_url
    else:
        ssi_http = SsiHttp(str(settings.get("brokers.ssi.base_url") or "https://api.ssi.com.vn"),
                           float(settings.get("brokers.ssi.request_timeout_seconds", 10)), transport=ssi_transport)
        tokens = SsiTokenManager(ssi_http, _env(env, "SSI_API_KEY", "khoá API SSI (feed)"),
                                 _env(env, "SSI_API_SECRET", "secret SSI (feed)"), notify=notify)
        auth.append(tokens)                              # không phải broker: runner chờ /otp ssi cho feed

    # ---- TCBS (chỉ khi được chọn; TcbsConfig live chặn nếu chưa bật / chưa kiểm) ----
    if "tcbs" in roles:
        from brokers.tcbs import TcbsBroker, TcbsConfig, TcbsHttp, TcbsOrderStream, TcbsTokenManager
        tcbs_cfg = TcbsConfig.from_settings(settings, env, live=True)
        tcbs_http = TcbsHttp(tcbs_cfg.base_url, tcbs_cfg.timeout_s, transport=tcbs_transport)
        tcbs_tokens = TcbsTokenManager(tcbs_http, tcbs_cfg.api_key, notify=notify)
        brokers["tcbs"] = TcbsBroker(tcbs_http, tcbs_tokens, tcbs_cfg, clock=clock, name="tcbs", live_flag=True,
                                     env=env, notify=notify)
        background.append(TcbsOrderStream(tcbs_tokens, brokers["tcbs"]).run)

    # ---- danh sách hợp đồng: từ DNSE, đối chiếu lịch đáo hạn (CLAUDE.md mục 4) ----
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
    dnse_feed = DnseFeed.from_settings(settings, dnse_key, dnse_secret, contract_lister, market_clock)
    ssi_feed = SsiFeed.from_settings(settings, tokens.get, contract_lister, market_clock, url=ssi_url)
    feed = CompositeFeed.from_settings(settings, dnse_feed, ssi_feed, market_clock, on_alert=on_feed_alert)

    return LiveSetup(brokers[primary_name], brokers[backup_name], feed, tokens, contract_lister,
                     background=background, preflight=[check_contract], auth=auth)
