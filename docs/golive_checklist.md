# Checklist go-live (P5.2) — rà soát 08/11/2026

> Chỉ báo cáo, chưa sửa gì trong bước này. Nguồn: (a) rà từng mục CLAUDE.md đối chiếu repo;
> (b) một lượt review độc lập "như người muốn bot thua tiền" (agent riêng, không xem kết luận của
> tôi), các lỗi nặng đã được TÁI HIỆN bằng script rồi tôi chạy lại xác nhận.

> **Cập nhật cùng ngày:** đã sửa #1–#8, #10–#12, #14 (mỗi lỗi có test tái hiện trong
> `tests/test_golive_fixes.py`, thử phá từng bản sửa đều có test đỏ). #9 đã làm theo quyết định của
> bạn (đóng khi mất dữ liệu > 30s). #13 đã nối cho live (08/11). #14 sửa hết (08/11). Phần 2: mọi mục làm được không cần khoá đã xong —
> còn lại là việc của bạn (lịch nghỉ, phí, giới hạn risk, khoá API, chạy script thăm dò). Live đã nối nhưng còn
> KHOÁ (`api_verified`, giá trị null) — **chưa chạy live**. Bảng trạng thái mới nhất: mục 0 ngay dưới.

## 0. Trạng thái mới nhất (09/11/2026)

| Hạng mục | Trạng thái |
|---|---|
| Lỗi #1–#14 (rà soát go-live) | ✅ tất cả đã sửa, mỗi lỗi có test tái hiện |
| Adapter DNSE / SSI / TCBS | ✅ có; live KHOÁ tới khi chạy `scripts/*_trading_probe.py` và đặt `api_verified`; TCBS tắt mặc định |
| Nối live (broker chính + dự phòng chọn được trong dnse/ssi/tcbs, feed tổng hợp DNSE+SSI) | ✅ `live/wiring.py`; chờ `/otp` rồi mới đối soát |
| Đối chiếu mã hợp đồng với broker lúc khởi động (§4) | ✅ DNSE `/instruments` + `symbolType` phải khớp lịch đáo hạn bot tính |
| data/state/logs ngoài Dropbox (§3b), logger JSON không lộ bí mật | ✅ |
| Risk: chuỗi thua SAU phí; lượt đóng khẩn cấp = thua; gỡ hedge không tạo lượt ma | ✅ |
| Dữ liệu: downloader lịch sử DNSE, recorder (+ HĐ kế tiếp ngày trước roll, sau cờ), kiểm định theo HĐ | ✅ code — CHƯA chạy trên dây (cần khoá) |
| Backtest: bar + tick thật, nối chuỗi qua roll, back-adjust, % vốn; robustness trên bar/tick | ✅ |
| Chiến lược: ORB #1 baseline, ORB #2 lọc biên/ADR (tắt) | ✅ code — **KHÔNG CÓ EDGE** trên dữ liệu thật (nến + tick, 10/11/2026) |
| Lịch nghỉ 2025–2026, phí sở + VSDC | ✅ (2026 kiểm chéo dữ liệu VN30) |
| Đóng vị thế ngoài phiên liên tục (chạm lỗ ngày / kill / EXIT lúc nghỉ trưa, ATC) | ✅ 10/11/2026: hoãn tới phiên liên tục, giữ stop/TP, không gửi lệnh; `/kill` trưa không thoát; báo Telegram (`TestOutsideContinuous`, `test_kill_at_lunch_…`) |

### Việc của bạn (theo thứ tự)

1. **Điền `config/settings.yaml`** — ✅ phí/thuế (biểu phí DNSE 01/05/2026), vốn 100 triệu, lỗ ngày 15 điểm, 2 lượt/ngày, thua liên tiếp 2.
   `live.allowed_machine` = `CFA-BINHLD-LT` (laptop, 10/11/2026). `paths.dashboard_warehouse` chỉ cần nếu import dữ liệu cũ.
2. **Khoá API** vào `.env` (DNSE, SSI; TCBS tuỳ chọn) rồi chạy thăm dò, gửi kết quả (không chứa khoá):
   `dnse_feed_probe.py` (kèm `--extra-contract`), `dnse_history_probe.py`, `dnse_trading_probe.py`,
   `ssi_trading_probe.py` ⇒ mình chốt các ❓ và điền `brokers.dnse.loan_package_id`,
   `trading_token_ttl_seconds`, `brokers.ssi.order_symbol_format`, `market_order_type`, `api_verified`.
3. **Dữ liệu**: `download_history.py` 1–2 ngày (kiểm), rồi cả dải; bật recorder hằng ngày (`docs/recorder_windows.md`).
4. **Nghiên cứu**: chốt mốc chia IS/OOS trong `research/log.md`, chạy `backtest.robustness` cho ORB #1 (và #2 nếu muốn).
5. **Lịch nghỉ 2027** khi HNX công bố (bắt buộc trước 2027).
6. **Paper** ≥ vài tuần (`TRADING_MODE=paper`) — chỉ khi robustness nói CÓ edge mới tính tới live.

## Kết luận

**KHÔNG được chạy live.** Đường đặt lệnh đã sửa hết lỗi CRITICAL/HIGH của lượt rà soát và có test, nhưng:
API broker chưa được gọi thử (live còn khoá), chưa có dữ liệu thật, và chiến lược **chưa có bằng chứng edge**.
Paper chạy được sau bước 1 (settings) + khoá DNSE cho feed.

## 1. Lỗi phát hiện (theo mức độ)

Cột "Xác minh" là trạng thái lúc rà; trạng thái sửa ở bảng 1b.

| # | Mức | Vị trí | Vấn đề | Kịch bản | Xác minh |
|---|---|---|---|---|---|
| 1 | CRITICAL | `live/emergency.py` (hedge) + `risk/risk_manager.py` `_exposure_breach` | Lệnh đối ứng khẩn cấp **luôn bị risk từ chối** khi đang có cả stop và chốt lời: trần hợp đồng cộng stop + TP (cùng chiều SELL) như "lệnh chờ" ⇒ xấu nhất −2 > `max_contracts`=1 | Long 1 + stop + TP (ORB luôn có TP), DNSE im 11s ⇒ 0 lệnh tới SSI, 3 lần MAX_CONTRACTS, rồi "XỬ LÝ TAY"; bộ đếm thử lại cạn ⇒ quy trình khẩn cấp chết cả ngày | tái hiện ✔ |
| 2 | CRITICAL | `core/execution.py` `_close` / `_ensure_protection` | Lệnh đóng MTL khớp **một phần** ⇒ phần dư thành LO (quy tắc KRX), không ai đặt lại; stop đã bị huỷ ⇒ **vị thế còn lại không có stop**, qua giờ flatten/ATC | Long 2, flatten 14:25 khớp 1, giá rơi ⇒ 14:30:02 net vẫn +1, LO kẹt 1349,9 | tái hiện ✔ |
| 3 | CRITICAL | `core/execution.py` `_submit` / `_cancel`, `live/runner.py` `run()` | Ngoại lệ không phải `BrokerError` (timeout mạng…) từ `place`/`cancel` **làm chết runner**; lệnh "ma" NEW kẹt trong `working_orders`; stop là lệnh mô phỏng phía bot ⇒ tiến trình chết là mất stop | Entry khớp, đặt stop ném ConnectionError ⇒ drain ném ⇒ net +1, `stop NEW` ma | tái hiện ✔ |
| 4 | HIGH | `core/execution.py` (stop & TP không liên kết OCO), `live/runner.py` (đưa hết tick cho paper trước `drain`) | Stop và chốt lời **cùng khớp** ⇒ đảo chiều ngoài ý muốn; stop mới cho vị thế Short đặt ở giá stop cũ (sai phía) | Đã tái hiện: net −1, `trades_today` = 2. Live: độ trễ huỷ lệnh mở cùng cửa sổ | agent tái hiện |
| 5 | HIGH | `risk.check` trả `[FLATTEN_ALL, EMERGENCY_HEDGE]` cùng lúc; `emergency.hedge` không trừ lệnh đóng/stop/TP đang chờ ở broker chính | **Đóng hai lần**: lệnh flatten gửi tới broker "chết" + lệnh đối ứng ở SSI ⇒ net −1 | Lúc 14:25 DNSE im | tái hiện ✔ |
| 6 | HIGH | `monitoring/telegram.py` `fetch_updates`/`notify`, `live/runner.py` log lỗi đọc lệnh | **Lộ token Telegram trong log**: thông báo lỗi của `requests` chứa URL `/bot<TOKEN>/…`; mất mạng ⇒ ghi mỗi 3s. Có token, người khác đọc được và "ăn" mất lệnh `/kill` | Mất kết nối Telegram | đọc code ✔ |
| 7 | HIGH | `core/execution.py` `_close` | Flatten bị từ chối MAX_CONTRACTS khi xác nhận huỷ stop/TP chưa về (adapter thật huỷ bất đồng bộ); lệnh "cũ" trong state ⇒ từ chối lặp mãi | Huỷ được xác nhận trễ ⇒ `tối đa −2 HĐ` | tái hiện ✔ |
| 8 | MEDIUM | `core/execution.py` `_sync` | Huỷ-rồi-đặt-lại: huỷ lỗi bị nuốt mà vẫn đặt stop mới ⇒ có thể hai stop sống | — | đọc code |
| 9 | MEDIUM | `live/runner.py`, risk | Feed chết chỉ chặn MỞ vị thế; không luật nào đóng vị thế đang mở; stop mô phỏng chỉ kích hoạt khi có tick; task feed chết không được khởi động lại | Feed rớt khi đang Long | đọc code |
| 10 | MEDIUM | `live/emergency.py` `_attempts` | Bộ đếm thử lại không bao giờ reset ⇒ chỉ 3 lần lỗi cho CẢ vòng đời tiến trình | Hai sự cố trong ngày | đọc code |
| 11 | MEDIUM | `risk.record_fill` | Chuỗi thua tính theo điểm TRƯỚC phí ⇒ lệnh +0,1 điểm nhưng lỗ sau phí lại xoá chuỗi thua | — | đọc code (đã ghi là giả định ở P2.1) |
| 12 | MEDIUM | `live/runner.py` | Bộ đếm ngày chỉ lưu mỗi 30s ⇒ mất điện ngay sau khi chạm ngưỡng lỗ ⇒ khởi động lại giao dịch tiếp | Mất điện | đọc code |
| 13 | MEDIUM | `live/runner.py` `main` | Đối chiếu giá DNSE/SSI (CLAUDE.md §3) **chưa được áp** — runner chỉ dùng feed DNSE | ✅ live: `live/wiring.py` dùng `CompositeFeed` DNSE+SSI (token SSI chung). Paper vẫn chỉ feed DNSE | `tests/test_live_wiring.py` |
| 14 | LOW | `backtest/engine.py`, `backtest/report.py` | Lạc quan backtest: file bar thiếu cột `volume_reliable` mặc định là tin được; lệnh còn mở cuối dữ liệu không vào số liệu; spread 1 bước ⇒ bán ở bid = last không mất nửa spread; tick lặp cực trị cách 1s ⇒ độ trễ ≥ 1s thì stop khớp ở cực trị kia | ✅ sửa cả 4 (xem 1b) | `TestGolive14` |

## 1b. Đã sửa

| # | Cách sửa |
|---|---|
| 1, 7 | Lệnh GIẢM vị thế không còn bị trần "kịch bản xấu nhất" (vẫn áp cho lệnh mở); thay bằng chặn hai lệnh đóng (hoặc hai lệnh bảo vệ cùng loại) cùng chiều trên cùng broker (`RejectCode.DUPLICATE_CLOSE`) |
| 2 | `ExecutionManager.maintain()` mỗi vòng: lệnh đóng chờ quá 3s ⇒ huỷ, đóng lại bằng MTL; vị thế mở không có lệnh đóng ⇒ đảm bảo stop |
| 3 | Lỗi bất kỳ từ `place` ⇒ xoá lệnh ma khỏi sổ, ghi `broker_error`, vòng sau tự đặt lại stop; `cancel` bắt mọi lỗi; runner bắt lỗi vòng lặp, lặp 5 lần ⇒ kill + cố đóng vị thế, thoát mã 1 |
| 4 | Runner xử lý từng tick rồi mới tới tick sau; đảo chiều ngoài ý muốn (net khác chiều lệnh vào) ⇒ đóng ngay, không đặt stop sai phía; vị thế không có kế hoạch (vd nhận lúc khởi động) ⇒ đóng |
| 5 | Không gửi lệnh tới broker đang mất kết nối; quy trình khẩn cấp lo đối ứng |
| 6 | Mọi lỗi Telegram đi qua `redact()` (xoá token và đường dẫn `/bot…`) |
| 8 | Huỷ chưa được xác nhận ⇒ không đặt lệnh bảo vệ mới (đợi vòng sau) |
| 10 | Bộ đếm thử lại đối ứng về 0 khi net đã về 0 |
| 11 | Chuỗi thua tính SAU phí (phí mở + phí đóng) |
| 12 | Runner lưu bộ đếm ngay khi đổi (không đợi heartbeat 30s) |
| 14 | File bar thiếu cột `volume_reliable` ⇒ coi là không tin được; tick giả: mỗi phía cách giá ≥ 1 bước (cả mua lẫn bán chịu spread); cực trị giữ lâu hơn độ trễ paper + 1s; lệnh còn mở cuối dữ liệu vào số liệu (giá cuối, trừ ước phí đóng) kèm cảnh báo |

| 9 | Luật mới `risk.flatten_on_feed_silence_seconds` (30s): đang có vị thế, trong phiên liên tục, không có dữ liệu thị trường quá ngưỡng ⇒ FLATTEN_ALL; mốc tính = muộn nhất của tick cuối / đầu đoạn phiên / lúc runner khởi động (nghỉ trưa và đầu phiên chiều không đóng oan). Engine backtest chạy vòng kiểm tại mốc ngưỡng khi dữ liệu thủng (live kiểm mỗi giây). Ghi vào CLAUDE.md §5 |

## 2. Đối chiếu CLAUDE.md

| Mục | Trạng thái |
|---|---|
| §2 Kiến trúc | Đủ `brokers/` (TCBS có nhưng tắt). `brokers/dnse.py` (P4.1), `brokers/ssi.py` (P4.2) đã có — live khoá tới khi kiểm API; logger JSON có cấu trúc ✔ (`monitoring/logger.py`). Còn lại đủ |
| §2 Một code strategy cho backtest/paper/live | ✔ `ExecutionManager` + `RiskManager` dùng chung; chỉ đổi broker và nguồn dữ liệu |
| §2 Giá trị null ⇒ từ chối chạy | ✔ (risk, paper, engine, runner); còn 10 khoá null — xem mục 5 |
| §3 DNSE chính / SSI dự phòng, net tổng mọi broker | Feed: ✔ cả hai + composite. Broker: DNSE ✔ (`brokers/dnse.py`), SSI ✔ (`brokers/ssi.py`) — chưa nối live vào runner. Net tổng: ✔ (adapter DNSE theo dõi cả lệnh đặt tay) |
| §3 Đối chiếu feed (lệch/im) ⇒ ngừng mở vị thế | ✔ live (`live/wiring.py` → `CompositeFeed`); paper: bật `paper.composite_feed: true` (cần OTP SSI) |
| §3b Hai chốt máy/khoá | ✔ runner + recorder; runner từ chối `data_dir`/`state_dir`/`log_dir` trong Dropbox/OneDrive |
| §4 Đối chiếu mã hợp đồng với danh sách broker lúc khởi động | ✔ live: DNSE `/instruments` (`symbolType` phải khớp lịch đáo hạn bot tính) — lệch ⇒ thoát 2. Paper/recorder vẫn `--krx`/`--assume-computed-contract` (không cần khoá giao dịch) |
| §4 Lịch nghỉ | `scripts/check_calendar.py`: 2026 và 2027 **thiếu ngày nghỉ** (Tết, Giỗ Tổ, nghỉ bù) |
| §4 Phí trong settings | null ⇒ backtest/paper từ chối chạy (đúng thiết kế) |
| §5 Luật rủi ro | Tất cả có trong `risk_manager` + unit test `approve/check`; lỗ hổng đầu-cuối #1, #2, #4, #5, #7 đã sửa |
| §5 Stop ngay sau khi khớp | ✔ phía bot; broker thật không có lệnh stop qua API (❓ broker_notes §4) ⇒ DNSE: stop do bot giữ trong adapter (chạm giá ⇒ MTL), chống stop mù bằng luật mất dữ liệu (#9) |
| §5 Chống lệnh trùng | `client_order_id` ✔; DNSE (không idempotency): không bao giờ gửi lại, đối soát sổ lệnh; SSI: `clientRequestId` = client id, 409 ⇒ tra sổ |
| §5 Mất kết nối broker ⇒ khẩn cấp | ✔ `live/emergency.py` (#1 đã sửa); live nối SSI làm broker dự phòng (`live/wiring.py`) |
| §5 Kill-switch file + /kill | ✔ (#6 lộ token Telegram đã sửa) |
| §6 Backtest chống tự lừa | ✔ bid/ask + trượt, bar kế tiếp, SL thắng, IS/OOS/WF, báo cáo đủ mục, n_trials, cảnh báo dữ liệu snapshot; #14 đã sửa |
| §7 Bảo mật | `.env` ✔, mặc định paper ✔, live cần env + `--live` + `api_verified` ✔; adapter kiểm TRADING_MODE trước MỌI lệnh thay đổi ✔; logger che token/OTP/khoá ✔; #6 đã sửa |
| §8 Mục ❓ broker_notes | Chưa mục nào được kiểm bằng gọi thử (chưa có khoá) |

## 3. Test còn thiếu (lúc rà) — ✅ đã bổ sung: `tests/test_golive_fixes.py` (từng lỗi), `tests/test_execution.py` (ExecutionManager trực tiếp)

- Không có `tests/test_execution.py` — `ExecutionManager` chỉ được test gián tiếp.
- Stop sau mỗi lần khớp: thiếu khớp một phần lệnh vào, stop + TP cùng khớp, đặt stop thất bại.
- Flatten: thiếu khớp một phần lệnh đóng, xác nhận huỷ trễ.
- Lỗi mạng từ `place`/`cancel` (ngoại lệ không phải `BrokerError`).
- Khẩn cấp: khi có TP (#1), cùng lúc với flatten (#5), sự cố thứ hai trong ngày (#10).
- Chuỗi thua có tính phí (#11).
- Không lộ token Telegram khi lỗi mạng (#6).

## 4. Chỗ có thể gửi lệnh trùng / sai

#4 (stop + TP cùng khớp), #5 (flatten + đối ứng), #8 (hai stop sống); adapter DNSE tương lai (không
idempotency — cấm tự retry lệnh đặt, phải tra sổ lệnh trước khi gửi lại).

## 5. Việc của người dùng trước go-live

- [ ] Điền: `fees.*` (4 khoá), `risk.max_daily_loss_points`, `risk.max_trades_per_day`,
      `risk.max_consecutive_losses`, `live.allowed_machine`, `backtest.initial_capital_vnd`,
      (`paths.dashboard_warehouse` nếu import).
- [ ] Bổ sung `config/holidays.yaml` 2026–2027 theo thông báo của sở.
- [ ] Đăng ký DNSE Open API V2 + SSI FastConnect V3; hỏi SSI token dữ liệu có cần OTP không, hỏi
      cả hai có lệnh điều kiện (stop) cho phái sinh qua API không.
- [ ] Chạy `scripts/dnse_feed_probe.py` để chốt các mục ❓; bật recorder hằng ngày.

## 6. Thứ tự đề xuất (cập nhật 08/11)

1. ~~Sửa #1–#14~~ ✅. ~~P4.1/P4.2 adapter, stop do bot giữ, nối composite + broker dự phòng~~ ✅ (live còn khoá).
2. Bạn: điền settings còn null, lịch nghỉ 2026–2027; có khoá ⇒ chạy `dnse_history_probe.py`, `dnse_trading_probe.py`, `ssi_trading_probe.py`, gửi kết quả ⇒ mình chốt các ❓; chạy `download_history.py` (P1.6, code đã có) 1–2 ngày rồi cả dải.
3. Tải lịch sử thật ⇒ `backtest.robustness` cho ORB (kết luận theo luật đã đăng ký trước).
4. Paper trade ≥ vài tuần, so với backtest cùng kỳ; rồi mới cân nhắc live (hỏi người dùng trước khi bật).
