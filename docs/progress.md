# Tiến độ

## 2026-11-12 — Sandbox DNSE: đặt lệnh thử chạy được

- `--sandbox` chỉ đọc ✅ (ký HMAC + header version được nhận; secdef 404 trên Sandbox). `--order-test --price 1900` ✅: token OTP 666666, đặt lệnh `POST /accounts/{acc}/orders` 200, trạng thái `Pending → New → Filled`, vị thế đọc đúng, lệnh đối ứng về phẳng.
- Phát hiện: item sổ lệnh REST (Sandbox) không có `averagePrice` ⇒ giá khớp đang tạm lấy giá lệnh / giá feed (có cảnh báo). Cần kiểm REST production hoặc lấy giá từ vị thế / WebSocket lệnh trước khi live.
- Còn ❓: sửa/huỷ lệnh (Sandbox khớp ngay nên chưa thử được), MTL không `price`, `quantity` khi sửa lệnh khớp một phần.

## 2026-11-12 — DNSE: Sandbox + sửa adapter theo SDK chính thức 2.4.1 / tài liệu

- DNSE trả lời: không có UAT cho khách hàng, dùng Sandbox (`sb-openapi.dnse.com.vn`, key riêng, OTP 666666, token 8 giờ, lệnh tự khớp). Đọc trang Sandbox, Xác thực, place-order và mã nguồn `dnse-sdk-openapi` 2.4.1 (thêm `developers.dnse.com.vn` vào mạng của môi trường cloud).
- **Sửa lỗi adapter** `brokers/dnse.py`: đặt lệnh `POST /accounts/{accountNo}/orders` (trước: `/accounts/orders` theo SDK cũ — lệnh thật sẽ hỏng); body bỏ `accountNo`, MTL bỏ `price`; gửi header `version` (`brokers.dnse.api_version`, mặc định 2026-07-23); nhận token cả `tradingToken` lẫn `trading-token`; `BASE_URL_SANDBOX` được coi là môi trường thử (như UAT). `live/wiring.py` truyền `api_version`.
- `config/settings.yaml`: `trading_token_ttl_seconds: 28800` (✅✅ tài liệu + SDK), `api_version`. `.env.example`: `DNSE_SANDBOX_API_KEY/SECRET/ACCOUNT_NO`.
- `scripts/dnse_trading_probe.py --sandbox`: key `DNSE_SANDBOX_*` (không bao giờ lấy key/tài khoản production), OTP 666666 tự điền, gói vay lấy từ `/loan-packages` Sandbox, `--order-test` cho Sandbox/UAT (production vẫn bị từ chối), Sandbox tự khớp ⇒ đặt lệnh đối ứng để phẳng.
- Test viết trước (adapter: Sandbox, header version, token có gạch nối, body; probe: key/tài khoản Sandbox, OTP, đối ứng); đột biến 9/9 bị bắt (một đột biến sống lúc đầu ⇒ thêm test tài khoản).
- Rủi ro/giả định: MTL không có `price` và `quantity` khi sửa lệnh khớp một phần còn ❓ (kiểm trên Sandbox); Sandbox là mô phỏng — không chứng minh hành vi khớp thật. Lệnh STOP/OCO phía sàn có cho phái sinh ⇒ cân nhắc module riêng.

## 2026-11-12 — UAT DNSE: không kết nối được; probe báo lỗi gọn

- `dnse_trading_probe.py --uat` trên laptop: `openapi-uat.dnse.com.vn:443` ConnectTimeout ⇒ chưa thử đặt lệnh được; chờ DNSE trả lời (broker_notes §1.2).
- Probe: lỗi kết nối (`DnseTransportError`) ⇒ in "❌ Không kết nối được <host>" + lệnh kiểm `Test-NetConnection`/`Resolve-DnsName`, thoát 2, vẫn ghi `summary.json` (trước: traceback dài). Test `test_unreachable_host_exits_2_with_clear_message` viết trước.

## 2026-11-12 — Hạ tầng: tự khởi động paper runner 08:40 (Task Scheduler)

- Không cần code mới: runner đã thoát 0 ngày nghỉ, chờ trước giờ mở (feed chỉ bị coi im lặng trong phiên liên tục), tự thoát sau 14:45 kèm báo cáo; lịch sử thiếu ⇒ 🚨 Telegram + thoát 2.
- `docs/runner.md` mục "Tự khởi động hằng ngày": PowerShell tạo task "VN30F Paper Runner" (T2–T6 08:40, `pythonw.exe`, chạy pin được, chạy bù khi lỡ, tối đa 7 giờ), cách kiểm, dừng, đổi chiến lược. Chỉ cho paper.
- Rủi ro/giả định: chưa từng chạy runner từ trước 09:00 trên máy thật (test có từ 08:59:30); lỗi trước khi Telegram sẵn sàng (vd thiếu khoá) chỉ vào log; pythonw nuốt traceback chưa bắt ⇒ xem LastTaskResult + log.

## 2026-11-12 — Hạ tầng: canh recorder từ bên ngoài (`scripts/check_recorder.py`)

- Người dùng chọn tạm dừng tìm chiến lược, chuyển sang hạ tầng. Việc đầu: sự cố sáng nay (task recorder bị chặn khi laptop chạy pin, không ai biết tới 09:10).
- `scripts/check_recorder.py`: trong phiên LIÊN TỤC, `data/raw/<hôm nay>` không có parquet hoặc file mới nhất cũ hơn `recorder.watch_max_age_seconds` (180 s; sát 09:00/13:00 tính từ giờ mở phiên) ⇒ Telegram `trading` + thoát 1; ngày nghỉ / ngoài phiên ⇒ im lặng, thoát 0; `--notify-ok` để thử kênh. Lỗi Telegram ⇒ log, vẫn thoát 1.
- `tests/test_check_recorder.py` (18 test, viết trước); đột biến 6/6 bị bắt. `docs/recorder_windows.md` §2b: lệnh PowerShell tạo task "VN30F Recorder Watch" (09:10→14:10 mỗi 30′, `pythonw.exe`).
- Rủi ro/giả định: dựa vào mtime file (đồng hồ máy đúng); laptop ngủ/tắt thì task canh cũng không chạy (StartWhenAvailable chạy bù khi bật lại); mất mạng ⇒ không gửi được Telegram.

## 2026-11-12 — Chiến lược #6 lấp gap mở cửa: KHÔNG CÓ EDGE ở cổng IS (dừng, không mở OOS)

- Đăng ký trước trong `research/log.md` (n_trials = 6), `config/settings.yaml` `strategy.gap_fade`.
- `strategy/gap_fade.py` (`GapFade`): C₋₁ = nến cuối của đúng phiên liền trước theo lịch nghỉ (thiếu ⇒ không giao dịch); quyết định ở nến 09:15; |g| ≥ 0,30·ADR10 ⇒ vào ngược gap, TP = C₋₁, stop 0,30·ADR; EXIT 11:25. Lịch nghỉ đọc lúc dựng (`TradingCalendar.default()`), `on_bar` thuần.
- `tests/test_strategy_gap_fade.py` (30 test, viết trước): cấu hình, ngưỡng, mốc = nến cuối phiên trước, thiếu phiên trước, ngày nghỉ, thiếu nến 09:15, một lần/ngày, thoát 11:24, engine chốt lời/thoát giờ, CLI robustness. Đột biến 7/7 bị bắt.
- IS (nến SSI 22/07→31/12/2025, dữ liệu cắt): 8 lệnh, −2,76tr, 0/6 độ nhạy, 0/4 walk-forward ⇒ dừng. OOS-A SSI 2026 vẫn chưa nhìn.
- Rủi ro/giả định: IS chỉ 8 lệnh (nhiễu lớn); chế độ nến bi quan; lịch nghỉ 2025 chưa kiểm chéo (17/10/2025 không có dữ liệu).

## 2026-11-12 — #4 kiểm định gộp DNSE: CHƯA ĐỦ (20 lệnh) nhưng dừng #4

- 25/03→08/05: 4 lệnh, −2,44tr; 01/09→nay: 16 lệnh, +3,06tr; gộp 20 lệnh +0,63tr, p 0,464, trượt 2 tick +0,23tr.
- Cửa sổ 25/03→08/05 đã lỗ (dữ liệu cố định) ⇒ điều kiện "lãi mọi cửa sổ" trượt vĩnh viễn ⇒ không thể ra CÓ EDGE ⇒ dừng #4, KHÔNG làm phần nến SSI cho `backtest.pooled`.
- Cả 5 cấu hình đều không có edge. Nến SSI 07/2025→03/2026 còn sạch cho chiến lược mới (đăng ký trước).

## 2026-11-12 — Đăng ký trước giai đoạn kiểm tra THỨ BA (nến SSI) cho #4

- `research/log.md`: luật cho W0 = nến SSI 23/07/2025→24/03/2026 (chế độ nến), chỉ cho #4, tham số chốt 10/11, `n_trials` giữ 5. Ghi TRƯỚC khi chạy pooled #4 trên DNSE.
- Thứ tự: pooled DNSE trước; KHÔNG CÓ EDGE ⇒ dừng, không chạy SSI; CHƯA ĐỦ/CÓ EDGE ⇒ kiểm tra dữ liệu (≥ 80% lệnh DNSE có lệnh SSI cùng ngày/chiều ở W1/W2) rồi kết luận 3 cửa sổ (≥ 30 lệnh, lãi cả 3 cửa sổ, p×5 < 0,1, trượt 2 tick > 0).
- Việc còn: mở rộng `backtest.pooled` nhận thêm nguồn nến cho cửa sổ riêng (test trước).
- Rủi ro/giả định: chế độ nến bi quan ~0,2 điểm/lệnh (thiên về KHÔNG CÓ EDGE); lịch nghỉ 2025 chưa kiểm chéo; ~8 ngày SSI thiếu quanh roll.

## 2026-11-11 — Đối chiếu nến SSI với DNSE (Desktop): dùng được cho nghiên cứu

- `compare_bars.py data/ssi data/history`: 132 ngày trùng, 31.811 phút chung; giá đóng lệch TB 0,079 điểm (< 1 bước), max 2,9; volume khớp hệt 98,5% phút (tổng 24,99tr / 24,97tr); OHLC khớp hệt 56,7% (lệch ở mở/cao/thấp — gán khớp lệnh ở biên phút khác nhau).
- ⇒ SSI `volume_reliable=true` (zip mới; Desktop đổi tại chỗ bằng lệnh một dòng). Dùng chung: `--bars data/history/ohlc1m data/ssi/ohlc1m` — trùng thì bản đọc TRƯỚC (DNSE) thắng.
- `compare_bars.py`: dùng `merge(suffixes=…)` thay `join(lsuffix/rsuffix)` (pandas mới báo deprecated).

## 2026-11-11 — Nến 1 phút VN30F từ SSI (kết nối Claude, nghiên cứu): 23/07/2025 → 11/11/2026 + `scripts/compare_bars.py`

- Tải qua `marketdata_list_intraday_stock_infor` (25 lần × 30 ngày): 365 (HĐ, ngày), 90.516 nến, 326 ngày giao dịch, thiếu 8 ngày (đầu HĐ mới sau roll) + HĐ kế tiếp ở 5 ngày trước roll. Ghi `ssi/ohlc1m/<HĐ>/<ngày>.parquet` (cùng schema kho DNSE, `source=ssi`, `volume_reliable=false`) + `_manifest.json`; gửi người dùng dạng zip (2,3 MB) ⇒ giải nén vào `data/` (⇒ `data/ssi/ohlc1m`).
- `scripts/compare_bars.py <kho1> <kho2>`: so (HĐ, ngày) trùng — khớp OHLC, lệch giá đóng, khớp volume, ngày lệch nhiều nhất. Test `tests/test_compare_bars.py` (5). Chạy `python scripts/compare_bars.py data/ssi data/history` trước khi dùng SSI.
- Dùng chung với DNSE: `--bars data/history/ohlc1m data/ssi/ohlc1m` ⇒ trùng thì giữ bản DNSE (volume tin được). Backtest trên SSI là chế độ NẾN (bi quan hơn tick). Kho SSI KHÔNG dùng lại để kết luận #1–#5.

## 2026-11-11 — #3 kiểm định OOS gộp: KHÔNG CÓ EDGE

- `backtest.pooled` #3: 25/03→08/05 12 lệnh −4,26tr; 01/09→11/11 28 lệnh +2,07tr; gộp 40 lệnh −2,19tr, p 0,656, trượt 2 tick −2,99tr ⇒ KHÔNG CÓ EDGE (research/log.md). #4 đang chạy.

## 2026-11-11 — Tải thêm 25/03→08/05/2026; `backtest/pooled.py` (kiểm định OOS gộp theo luật đăng ký trước)

- `download_history.py --start 2026-03-16 --end 2026-05-10` (laptop): 29 ngày, 0 lỗi, 3.169 request; 17/03–24/03 rỗng ⇒ DNSE lưu tới ~25/03 (≈ 7,5 tháng cuộn). Kho: 25/03 → 10/11/2026.
- `backtest/pooled.py`: lệnh OOS = ngày vào thuộc các `--window`; CÓ EDGE ⇔ ≥ 30 lệnh gộp, P&L > 0 ở MỌI cửa sổ, p sign-flip gộp × n_trials < 0,1, P&L gộp trượt 2 tick > 0 (đúng luật research/log.md 11/11). Chạy engine tick trên toàn kho 2 lần (trượt thường / căng). Test `tests/test_pooled_oos.py` (8).
- Recorder ngày đầu: 865.246 tick (70.981 khớp) 09:42→14:45; cảnh báo "khoảng trống 2.562 s" = trước khi bật recorder, "hụt 429 HĐ" ≈ 75 s chạy lại 09:56→09:58 — không phải lỗi feed; kiểm lại ngày chạy liền.

## 2026-11-11 — Paper #3 ngày đầu: kết thúc phiên đúng, báo cáo Telegram OK

- Runner chạy 10:06 → 14:45 không lỗi; `/status`, `/pnl` trả lời; 14:45:01 tự thoát + báo cáo ("hết phiên", 0 lệnh — khởi động sau 09:00 nên #3 không xét vào, đúng thiết kế). Feed runner nhận ~756 nghìn tick cả phiên, song song recorder.

## 2026-11-11 — Chốt lời từng phần (scale-out) trong đường đặt lệnh — CHƯA bật cho chiến lược nào

- `Signal.take_profits = ((giá, KL), …)` (tổng KL ≤ qty, phần dư = runner chạy tới stop/EXIT/flatten) + `breakeven_after_first_tp`; kiểm hợp lệ trong `Signal.__post_init__`; không dùng chung với `take_profit` (giữ nguyên hành vi một mức).
- `core/execution.py`: mỗi mức một LO `take_profit`; stop luôn MỘT lệnh KL = |net|; tổng KL TP đang chờ ≤ |net| (phân bổ mức gần trước, cắt cả trong một mức) ⇒ vào lệnh khớp một phần / stop khớp một phần không bao giờ làm đảo chiều; mức đã khớp không đặt lại, lệnh mức không đổi thì giữ; mức đầu khớp ⇒ dời stop về giá vốn (nếu bật). Mức chốt lời sai phía so với stop ⇒ bỏ tín hiệu.
- `risk/risk_manager.py`: nhiều lệnh `take_profit` cùng chiều được nếu tổng KL chờ ≤ |net| (1 HĐ ⇒ TP thứ hai vẫn bị chặn như cũ); stop và lệnh đóng vẫn chỉ một.
- Test `tests/test_scale_out.py` (22, gồm backtest: vào 3, chốt 1360 + 1370, dời hoà vốn, runner thoát 14:20); mutation 4 chốt (trần tổng TP, ghi KL mức, hoà vốn, risk) ⇒ đỏ.
- Chưa bật: `risk.max_contracts` vẫn 1; chiến lược #3 vẫn 1 HĐ. Dùng cho cấu hình #3b (đăng ký trước, kiểm định) sau khi #3 qua kiểm định trên dữ liệu thêm.
- Dữ liệu: DNSE lưu khớp lệnh ~7 tháng cuộn (15/04/2026 có, 16/03/2026 trống) ⇒ tải được thêm ~16/03→08/05/2026, phải tải SỚM (ngày cũ bị xoá dần).

## 2026-11-11 — Paper #3 chạy lần đầu trên laptop (kiểm vận hành)

- 10:06 `live.runner --strategy strategy.morning_momentum:MorningMomentum` (paper): nạp lịch sử 10 phiên OK, Telegram báo khởi động kèm tên chiến lược, `/status` trả lời (phẳng, feed khoẻ, ping DNSE 1s). Khởi động sau 09:00 ⇒ hôm nay không vào lệnh (thiếu nến 09:00). Đang kiểm recorder + runner cùng mở feed.

## 2026-11-11 — Recorder chạy thật trên laptop ✅

- `record_today.py --assume-computed-contract` 09:42–09:56: 69.282 dòng, 169 part (5 s/part), 0 lỗi ghi, Ctrl+C ⇒ gộp `ticks.parquet` OK; 3.000–7.400 message/phút. Tiếp: hẹn giờ Task Scheduler.

## 2026-11-11 — Feed DNSE chạy thật ✅ (probe lần 2): 4.450 tick/120 s, board G1

- Sau khi chuyển theo SDK 2.4.1: xác thực qua, 1 kết nối, 0 ngắt; 200 message đầu: 153 sổ lệnh + 28 khớp lệnh, mọi message `boardId: G1` ⇒ chỉ đăng ký G1. `time` = `{Seconds, Nanos}`. Bản ghi thật vào fixture + `test_real_messages_end_to_end`.
- Tiếp: chạy tay recorder 10–15′, rồi hẹn giờ.

## 2026-11-11 — Feed DNSE: chuyển giao thức theo SDK chính thức 2.4.1 (probe thật: 0 tick, AUTH_FAILED)

- Probe laptop 09:24: 5 kết nối, 0 tick; message thô: `{"action":"error","code":"AUTH_FAILED","message":"authentication failed: nonce is required"}` mỗi lần. Hai lỗi: nonce gửi dạng số; feed chỉ nhận `auth_error` là lỗi ⇒ coi `error` là thành công ⇒ đăng ký ⇒ server đóng ⇒ lặp.
- SDK `dnse` 0.5.0 (nguồn cũ của feed) có cùng lỗi; SDK chính thức `dnse-sdk-openapi` 2.4.1: nonce chuỗi, `auth_success`, đăng ký `{"action":"subscribe","channels":[…]}` mỗi kênh, khớp lệnh `tick.<board>.json` (`matchPrice`/`matchQtty`/`totalVolumeTraded`/`time`), giá tốt nhất `top_price.<board>.json` (`bid`/`offer` danh sách `{price,qtty}`).
- `feeds/data_feed_dnse.py`: theo 2.4.1; phản hồi xác thực ≠ `auth_success` ⇒ `FeedAuthError` (dừng, không lặp); `error` giữa phiên ⇒ đếm `server_errors` + log; `time` chuỗi không múi giờ ⇒ chọn VN/UTC gần lúc nhận (≤ 10′); backoff chặn tràn số. Giữ đọc dạng cũ để tương thích.
- Test: fixture thêm bản ghi THẬT (`real_welcome`, `real_auth_failed`) + mẫu 2.4.1; `tests/test_feed_dnse.py`, `tests/test_recorder_roll_eve.py` cập nhật.
- ❓ còn chờ probe lần 2: xác thực có qua không, board của VN30F, tên trường/định dạng `time` thật.

## 2026-11-10 — Runner paper chạy chiến lược chọn bằng `--strategy` (#3)

- Runner đã có `--strategy module:Class` + nạp lịch sử nhiều phiên cho chiến lược `history_days > 0`. Thêm: sai module/class ⇒ log rõ + thoát 2 (trước đây văng traceback); cấu hình chiến lược sai (`ValueError`) ⇒ thoát 2; tin khởi động ghi tên chiến lược (`TITLE` / tên class).
- Test `tests/test_runner_strategy_choice.py`: #3 chạy cả ngày qua runner + PaperBroker thật (tick → nến → Long sau 13:01, stop 0,30·ADR, EXIT sau 14:20), tin khởi động, `--strategy` sai.
- `docs/runner.md`: lệnh paper #3 + điều kiện lịch sử (đủ tới phiên hôm qua; ngày trước roll cần HĐ kế tiếp — 18/11/2026 ⇒ tải VN30F2612 ngày 18/11 trước phiên 19/11).

## 2026-11-10 — Tải bù 09–10/11 (Desktop); chốt ❓ tham số phân trang DNSE

- `download_history.py --start 2026-11-07 --end 2026-11-10`: 09/11 103.726 dòng, 10/11 109.197 dòng, 0 lỗi, 230 request. Phân trang chạy bằng `nextPageToken` ⇒ chốt ❓ trong broker_notes. Dữ liệu này hiện chỉ ở Desktop (laptop còn tới 06/11).

## 2026-11-10 — Robustness tick #3/#4/#5: CHƯA ĐỦ (mẫu nhỏ); #5 rõ ràng không có edge

- #3 IS +8,80tr (33 lệnh) / OOS +1,49tr (25 lệnh, p 0,389), độ nhạy 6/6, walk-forward 2/4. #4 IS +2,57tr (11) / OOS +1,99tr (13, p 0,373), độ nhạy 4/6. #5 IS −10,79tr / OOS −5,16tr ⇒ dừng.
- Kế hoạch (research/log.md): kéo dài OOS bằng dữ liệu mới, không đổi tham số; kết luận khi OOS ≥ 30 lệnh.
- Nhận xét phụ: báo cáo robustness để trống "Max drawdown %" và "Lợi nhuận / vốn" (không truyền `initial_capital_vnd` như `backtest.engine`).

## 2026-11-10 — Chiến lược #5 đảo chiều về VWAP (`strategy/vwap_reversion.py`)

- Theo đăng ký trước: VWAP ngày = Σ(TP × KL)/ΣKL nến từ 09:00 (bỏ KL 0); xét nến giờ mở 09:29…13:59, BỎ nến đóng đúng 11:30 (risk chặn mở — làm rõ thực thi, ghi log); |đóng − VWAP| ≥ 0,40·ADR10 và phẳng ⇒ vào ngược chiều, TP = VWAP, stop 0,25·ADR10 xa thêm; ≤ 2 tín hiệu vào/ngày; EXIT khi nến ≥ 14:19 đóng. `settings.strategy.vwap_reversion`.
- Test `tests/test_strategy_vwap_reversion.py` (29, gồm tích hợp engine: Short chốt lời ở VWAP; CLI robustness); mutation (đảo chiều, bỏ chặn nến 11:30, bỏ giới hạn lượt) ⇒ đỏ.
- **Chạy thật (người dùng):** `python -m backtest.robustness --ticks data/history/trades --split 2026-09-01 --strategy strategy.vwap_reversion:VwapReversion --out reports/s5_robust_ticks.html`.

## 2026-11-10 — Chiến lược #4 ngày xu hướng (`strategy/trend_day.py`)

- Theo đăng ký trước: m = đóng nến quyết định (đầu tiên trong [09:59, 10:04)) − mở nến đầu [09:00, 09:05); |m| ≥ 0,35·ADR10 và phẳng ⇒ theo m lúc 10:00, stop 0,30·ADR10, không TP; quyết định MỘT lần (giờ đầu yếu ⇒ cả ngày không vào); EXIT khi nến ≥ 14:19 đóng. `settings.strategy.trend_day`; `TITLE`/`ROBUSTNESS_PARAMS` cho robustness.
- Test `tests/test_strategy_trend_day.py` (25, gồm tích hợp engine vào 10:00/thoát 14:20, CLI robustness); mutation (đảo hướng, bỏ chốt một-lần) ⇒ đỏ.
- **Chạy thật (người dùng):** `python -m backtest.robustness --ticks data/history/trades --split 2026-09-01 --strategy strategy.trend_day:TrendDay --out reports/s4_robust_ticks.html`.
- Làm #4 trước khi có kết quả #3 — không ảnh hưởng: tham số #4 đã chốt trong đăng ký trước.

## 2026-11-10 — Chiến lược #3 đà sáng → chiều (`strategy/morning_momentum.py`) + robustness `--strategy`

- Đăng ký trước #3/#4/#5 trong `research/log.md` (tham số chốt trước khi viết code; `n_trials` = 5).
- `strategy/indicators.py::adr_before` (ADR N phiên trước, bỏ bar hôm nay, thiếu ⇒ None) — dùng chung cho #3–#5.
- `MorningMomentum`: m = đóng nến cuối [11:25,11:30) − mở nến đầu [09:00,09:05); quyết định MỘT lần ở nến đầu phiên chiều [13:00,13:05) (11:30 risk chặn mở); |m| ≥ 0,25·ADR10 ⇒ theo m, stop 0,30·ADR10 từ đóng nến quyết định, không TP; EXIT khi nến ≥ 14:19 đóng. Thiếu nến mốc / thiếu lịch sử ⇒ không giao dịch. `settings.strategy.morning_momentum`.
- `backtest.robustness --strategy module:Class` (mặc định ORB); class có `ROBUSTNESS_PARAMS` ⇒ độ nhạy trên đúng các khoá đó, `TITLE` ⇒ tiêu đề báo cáo.
- Test: `tests/test_strategy_morning_momentum.py` (30, gồm tích hợp engine: vào 13:01, thoát 14:20). Chạy thử robustness tick trên dữ liệu giả: OK.
- **Chạy thật (người dùng):** `python -m backtest.robustness --ticks data/history/trades --split 2026-09-01 --strategy strategy.morning_momentum:MorningMomentum --out reports/s3_robust_ticks.html`.
- Chưa: runner live/paper chọn chiến lược theo cấu hình (hiện chỉ ORB) — chỉ làm nếu một chiến lược CÓ EDGE.

## 2026-11-10 — Telegram kênh trading đã cài (laptop)

- Bot `@binh_vn30f_bot`; `TELEGRAM_TRADING_BOT_TOKEN/_CHAT_ID` trong `.env` laptop (không commit); `monitoring.telegram.send_message` gửi thử thành công. `/kill`, `/status`, `/pnl`, `/otp` chỉ nhận từ chat id này.

## 2026-11-10 — ORB #1/#2 kiểm định theo tick thật: vẫn KHÔNG CÓ EDGE

- `backtest.robustness --ticks` (laptop, 10,99 triệu tick): #1 OOS −1.302.700đ (p 0,553, trượt 2 tick −2.772.700đ); #2 OOS −669.300đ (p 0,551, trượt 2 tick −1.829.300đ). Độ nhạy và walk-forward đạt ngưỡng nhưng OOS lỗ + p không đạt ⇒ KHÔNG CÓ EDGE.
- 4/4 lần xem OOS (nến + tick, #1 + #2) đều lỗ ⇒ dừng ORB; OOS hiện tại đã "bẩn", cần dữ liệu recorder mới. Ghi `research/log.md`.

## 2026-11-10 — Sửa: đóng vị thế lúc nghỉ trưa (stop bị huỷ + gửi MTL mỗi vòng)

- **Lỗi** (phát hiện khi đo RAM `TickStore`): chạm lỗ ngày/khoá ngay trước 11:30 ⇒ `_close` huỷ stop/TP và xoá kế hoạch RỒI gửi MTL ⇒ MTL bị từ chối (ngoài phiên liên tục) ⇒ vị thế không stop suốt giờ trưa; vòng sau "không có kế hoạch ⇒ đóng" ⇒ MTL mới mỗi vòng (live mỗi giây ⇒ ~5.400 lệnh bị từ chối gửi lên DNSE). `/kill` lúc trưa ⇒ runner thoát sau `kill_flat_timeout_s` ⇒ mất luôn stop do bot giữ.
- **Sửa** `core/execution.py`: ngoài phiên liên tục `_close` hoãn (không huỷ, không gửi, giữ kế hoạch), sự kiện `deferred` MỘT lần (nói giờ sẽ đóng); `maintain()` đứng yên; hành động risk lặp lại chỉ ghi ≤ 1 lần/phút.
- **Sửa** `live/runner.py`: chuyển `deferred` lên Telegram (⚠️); bộ đếm chờ đóng sau `/kill` tạm dừng ngoài phiên liên tục, đếm lại từ lúc vào phiên.
- Test: `tests/test_execution.py::TestOutsideContinuous` (6), `tests/test_runner.py::test_kill_at_lunch_waits_for_pm_open_instead_of_timing_out`; mutation (bỏ chốt) ⇒ đỏ. Kịch bản giả cũ: 27.331 lệnh bị từ chối ⇒ 0.
- Còn ❓: sở có nhận LO/huỷ trong giờ trưa không (broker_notes §3.8) — hiện không dựa vào. Sau 14:30 (ATC) mà còn vị thế ⇒ báo "KHÔNG còn phiên liên tục — xử lý tay" (flatten_time 14:25 vốn để tránh tình huống này).

## 2026-11-10 — `TickStore`: backtest/robustness `--ticks` đọc dần theo ngày (RAM ~1 ngày thay cho cả kho)

- Đo thực tế ~570 B/tick ⇒ kho 6 tháng (~14,5 triệu tick) ≈ 8 GB nếu nạp hết; laptop 15 GB không đủ an toàn.
- `backtest.ticks.TickStore(paths, source, tick_size=, spread_ticks=)`: lúc tạo chỉ liệt kê file theo tên `YYYY-MM-DD` + kiểm nguồn (đọc riêng cột `source`); mỗi lượt `iter(date_from, date_to)` đọc lại TỪNG NGÀY, sắp theo thời gian, gắn báo giá giả. Tên không phải ngày ⇒ lỗi; dòng khác ngày tên file ⇒ lỗi.
- `run_robustness` nhận `TickStore` (IS = `iter(date_to=split−1)`), kết quả y hệt khi đưa list (test). CLI `backtest.engine --ticks` và `backtest.robustness --ticks` dùng `TickStore`.
- Thêm: `fill_missing_quotes_inplace`; `Tick` dùng `slots=True`.
- Đo (10 ngày giả cỡ thật, đúng lịch phiên): robustness đỉnh RAM 274 MB, 174 s (bản cũ ~2 GB). Ước tính kho thật 127 ngày: < 1 GB, ~40–90 phút/cấu hình.
- **Phát hiện khi đo (CHƯA sửa — module khác):** chạm lỗ ngày ngay trước 11:30 ⇒ huỷ stop, MTL đóng bị từ chối trong giờ nghỉ trưa, rồi mỗi vòng `risk.check` lại gửi MTL mới ⇒ vị thế KHÔNG có stop suốt giờ trưa + bắn lệnh liên tục lên broker (live: mỗi giây). Backtest dữ liệu thật không gặp (không có tick giờ trưa). Cần xử lý trong `core/execution.py`/`risk` trước khi chạy paper.

## 2026-11-10 — Laptop setup; `live.allowed_machine = CFA-BINHLD-LT`

- Laptop: repo ở `D:\Future-trading`, `.venv`, pytest qua hết. Chỉ laptop được chạy runner paper/live; Desktop chỉ backtest / tải dữ liệu / probe.
- Còn null: `paths.dashboard_warehouse` (không cần), `brokers.dnse.trading_token_ttl_seconds` (sau UAT), `brokers.ssi.*` (khi có khoá SSI).

## 2026-11-09 — ORB #1 IS theo tick thật: +3,21tr (nến: −3,78tr) — kiểm mô hình khớp

- Người dùng chạy `backtest.engine --ticks data/history/trades --to 2026-08-31`: 142 lệnh, +3.208.600đ sau phí.
- Random walk (4 seed × 125 ngày): mô hình tick không tạo lãi giả; mô hình nến bi quan hơn 0,20–0,23 điểm/lệnh.
  Ghi `research/log.md` mục "Mô hình khớp lệnh"; quyết định đánh giá lại #1/#2 bằng `robustness --ticks` (n_trials giữ 2).

## 2026-11-09 — Backtest tick: bỏ qua file ngoài khoảng ngày trước khi đọc

- `backtest.ticks.load_tick_files(..., date_from, date_to)`: file/thư mục tên `YYYY-MM-DD` ngoài khoảng ⇒ không đọc; các dòng còn lại vẫn lọc theo ngày; lọc hết ⇒ danh sách rỗng (không lỗi). `backtest.engine --ticks --from/--to` dùng nó — trước đây đọc cả 6 tháng (~14 triệu dòng, vài GB RAM) rồi mới lọc.
- Ước lượng RAM: ~240 B/tick ⇒ ~26 MB/ngày khớp lệnh, IS (~77 ngày) ≈ 2 GB, đỉnh ~4 GB lúc gắn báo giá giả. `backtest.robustness --ticks` vẫn đọc toàn bộ (cần cả OOS) — chưa đổi.

## 2026-11-09 — ORB #2 (lọc độ rộng biên / ADR) trên dữ liệu thật: KHÔNG CÓ EDGE

- `backtest.robustness --set strategy.orb.range_filter.enabled=true` (OOS từ 01/09, n_trials 2): OOS −1.459.300đ, p = 0,588, trượt 2 tick −2.779.300đ, 0% biến thể ±20% lãi, 25% đoạn walk-forward lãi ⇒ **KHÔNG CÓ EDGE**. Ghi `research/log.md` dòng #2.
- Cả hai cấu hình ORB đã đăng ký đều thất bại; nguyên nhân chính: chi phí ~0,47 điểm/lượt (phí + thuế) + trượt/nửa spread lớn hơn lợi thế thô. Không paper/live.

## 2026-11-09 — `--set` cho backtest/robustness (bật ORB #2 không cần sửa settings)

- `backtest.engine` và `backtest.robustness` nhận `--set khoá.chấm=giá_trị` (lặp được; giá trị đọc như YAML; khoá phải tồn tại, sai ⇒ thoát 2). Đổi cấu hình CHỈ cho lần chạy — không bật `range_filter` trong `settings.yaml` (bật ở đó thì runner live/paper cũng đòi lịch sử). Tiêu đề báo cáo robustness ghi ORB #1/#2 theo cấu hình.
- Test viết trước `tests/test_cli_set_override.py` (7). 1267 test.

## 2026-11-09 — ORB #1 trên dữ liệu thật: KHÔNG CÓ EDGE

- In-sample (11/05–31/08): 141 lệnh, −3.784.700đ sau phí. `backtest.robustness` (OOS từ 01/09, n_trials 2): **KHÔNG CÓ EDGE** — OOS −2.292.700đ, p = 0,609, trượt 2 tick −3.652.700đ, 0% biến thể ±20% lãi, 25% đoạn walk-forward lãi. Ghi vào `research/log.md` dòng #1.
- Ước tính: phí ~46.700đ/lượt (≈ 0,47 điểm) ⇒ IS trước phí ≈ +2,8tr (≈ +0,2 điểm/lượt, đã gồm trượt + nửa spread giả) — chi phí ăn hết.
- ⇒ ORB #1 KHÔNG được paper/live. Phần OOS 01/09–06/11 đã dùng cho #1.

## 2026-11-09 — Chốt mốc chia IS/OOS + luật dữ liệu ≤ 1% (người dùng chọn, trước khi xem kết quả)

- `scripts/check_history.py` trên kho thật: 133/133 mục, 127 ngày, 32.053 nến, không ngày đáng ngờ; 53 ngày có vài phút thiếu lệnh (chủ yếu 09:00/14:45/13:00); 09/06 thiếu 452 HĐ lúc 09:39–09:43 (giữa phiên, thuộc IS).
- Người dùng chọn: **OOS từ 2026-09-01**; luật "dữ liệu tin được" = **≤ 1% nến** volume không tin được. Ghi vào `research/log.md` (mục riêng, không thêm dòng ⇒ `n_trials` vẫn 2).
- `backtest/robustness.py`: `VerdictRules.max_unreliable_volume_frac = 0.01`, `data_reliable()`; HTML ghi tỉ lệ. `backtest/report.py`: cảnh báo mạnh chỉ khi > 1%, dưới ngưỡng thì ghi chú. 10 test mới; thử phá 4 cơ chế ⇒ 0 sống. 1260 test.

## 2026-11-09 — Tải xong 6 tháng; `scripts/check_history.py`

- Lượt 2: 24 mục còn lại tải xong, 0 lỗi ⇒ đủ 133 mục kế hoạch 11/05–06/11/2026 (≈127 ngày giao dịch + HĐ kế tiếp các ngày trước roll), tổng ~12.300 request qua 2 ngày hạn mức.
- `scripts/check_history.py --start … --end …`: đối chiếu kho với `plan_days` — mục ok / rỗng / lỗi / thiếu, ngày thiếu lệnh hoặc có phút volume không tin được (chỉ báo), ngày đáng ngờ (< 150 nến, nến đầu sau 09:05, nến cuối trước 14:25; không kiểm độ phủ HĐ kế tiếp ngày trước roll). Mã thoát 0/1.
- Test viết trước `tests/test_check_history.py` (9). Thử phá 9 cơ chế ⇒ 2 sống ⇒ thêm 2 test ⇒ hết. 1250 test.

## 2026-11-09 — Tải cả dải lần 1: 108 ngày; sửa nhận diện hết hạn mức (429 không header)

- Lượt tải 11/05–06/11: 108 ngày ghi xong (≈ 93 request/ngày nhờ phân trang), 10.024 request; 24 ngày lỗi `429 [OA-400] Rate Limit Exceeded` vì 429 thật KHÔNG kèm `X-Ratelimit-Remaining` ⇒ downloader cũ ghi lỗi từng ngày (ngày lỗi không bị đánh dấu xong ⇒ lần sau tải lại, không mất gì).
- Sửa `HistoryClient`: nhớ `X-Ratelimit-Remaining/Reset` từ mọi response thành công; còn 0 ⇒ `QuotaExhausted` TRƯỚC khi gửi (qua mốc reset thì quên); 429 lặp lại sau các lần chờ ngắn ⇒ `QuotaExhausted` (dừng cả lượt) thay vì lỗi từng ngày.
- 4 test mới (server giả gửi Remaining trên 200 và 429 không header như thật; đồng hồ tường cố định trong test). Thử phá 6 cơ chế ⇒ 0 sống. 1241 test.

## 2026-11-09 — Dữ liệu 1 ngày ĐẦY ĐỦ; phân trang mọi cửa sổ; dừng gọn khi hết hạn mức

- Tải lại 06/11 với phân trang: **110.501 lệnh, `volume_gap` 0, không phút nào bị đánh dấu, 369 request** ⇒ không cần đổi luật "dữ liệu tin được" của robustness.
- Hạn mức DNSE nhóm /price = 10.000 request / 24 giờ (reset đúng 24h sau) ⇒ ~27 ngày dữ liệu / ngày đêm nếu chia đôi cửa sổ.
- Downloader: phân trang cho MỌI cửa sổ đầy (không chỉ giây ATO/ATC; ước ~3× ít request hơn), không được ⇒ chia đôi (thử phân trang đúng 1 lần mỗi tên); log tên tham số trang chạy được. Hết hạn mức (`Remaining 0` không `Retry-After`, hoặc `Retry-After` > 5 phút) ⇒ `QuotaExhausted`: dừng cả lượt, KHÔNG đánh dấu ngày dở, báo mốc reset, mã thoát 3; chạy lại để tải tiếp.
- 7 test mới; thử phá 7 cơ chế ⇒ 2 sống ⇒ thêm 2 test ⇒ hết. 1237 test.

## 2026-11-09 — `limit` tối đa 1000; phân trang tự kiểm cho giây ATO/ATC

- Tải lại 06/11: server từ chối `limit` 2000 ("limit must be between 1 and 1000"); manifest đánh dấu đúng 3 phút 09:00, 14:29, 14:45 (thiếu 2.986 HĐ).
- Downloader: `limit` mặc định = 1000 (giảm ~½ số request). Giây vẫn đầy ⇒ phân trang bằng `nextPageToken`: thử tên tham số `nextPageToken` rồi `pageToken`, chỉ nhận trang sau khi nó CŨ hơn + không trùng (`_older_and_disjoint`), nhớ tên đã chạy; server lờ / 400 ⇒ giữ phần đã có, phút đó bị đánh dấu. Tối đa 100 trang/giây.
- 10 test mới (server giả có tham số trang cấu hình được, trả token cả trang cuối, từ chối tham số). Thử phá 8 cơ chế ⇒ 5 sống ⇒ thêm 6 test ⇒ hết. 1231 test.

## 2026-11-09 — Tải thử 1 ngày: giây khớp định kỳ ATO/ATC bị cắt ⇒ xin lại limit lớn + đánh dấu theo phút

- Người dùng tải 06/11/2026: 108.976 lệnh / 705 request, `volume_gap` 2.986 HĐ — chỉ ở giây 09:00:00 (ATO) và 14:45:00 (ATC), mỗi giây > 500 lệnh cùng lúc.
- Hậu quả nếu để nguyên: cả ngày bị đánh dấu volume không tin được ⇒ ngày nào cũng vậy ⇒ `robustness` luôn "CHƯA ĐỦ".
- Sửa: giây đầy ⇒ thử lại `limit` 2.000 rồi 10.000 (server từ chối ⇒ giữ phần đã có); `unreliable_minutes` đánh dấu đúng phút quanh chỗ hụt KL luỹ kế (cả phút trước và sau chỗ hụt; lệnh đầu ngày phải có luỹ kế = KL của nó); không có luỹ kế ⇒ cả ngày không tin. Manifest ghi `unreliable_minutes`. Server giả trong test trả mới-nhất-trước như thật.
- 6 test mới; thử phá 8 cơ chế ⇒ 3 sống ⇒ thêm 2 test ⇒ hết. 1223 test.
- Nếu DNSE không cho limit lớn: phút 09:00 và 14:45 (± phút liền kề) vẫn bị đánh dấu mỗi ngày ⇒ cần xem lại luật "dữ liệu tin được" của robustness (hiện yêu cầu 0 bar không tin được) — chờ kết quả tải lại.

## 2026-11-09 — Probe lịch sử DNSE trên dây: sửa đọc thời gian khớp lệnh

- `dnse_history_probe.py`: trades nhận epoch giây (định dạng khác ⇒ 400); `time` là giờ VN CÓ mili-giây ⇒ downloader cũ sẽ lỗi ở MỌI cửa sổ khớp lệnh ⇒ đã sửa `parse_trade_times` (nhận có/không mili-giây), test với chuỗi thật. Lịch sử khớp lệnh: ≥ 6 tháng, < 12 tháng.
- OHLC: HĐ đã đáo hạn ⇒ 400 "invalid symbol"; HĐ hiện tại ⇒ rỗng với `nextTime = from` (downloader cũ coi là lỗi "nextTime không tăng" ⇒ đã sửa). ⇒ **Nến 1 phút giờ dựng từ khớp lệnh** (`bars_from_trades`, mặc định `--kinds trades`, `source = dnse_trades`, `volume_reliable` = KL luỹ kế khớp); thiếu nến mà có file khớp lệnh ⇒ dựng lại không gọi mạng; không ghi đè nến có sẵn trừ `--force`. 6 test mới, thử phá 10 cơ chế ⇒ 0 sống. 1218 test.
- Sửa test chập chờn `test_feed_down_is_unhealthy_immediately` (ngủ cứng 50ms ⇒ chờ điều kiện tối đa 2s). 1212 test.

## 2026-11-09 — Probe DNSE (chỉ đọc) chạy trên dây lần đầu

- `dnse_trading_probe.py` trên production: 7/7 request 200 — chữ ký HMAC path không kèm query được chấp nhận; có 1 tiểu khoản phái sinh (số đặt ở `.env`); gói vay id 1306 (initialRate 0,1848) ⇒ `brokers.dnse.loan_package_id: 1306`; secdef nhận KRX 41I1GB000 (VN30F2611).
- **Lỗi thật bắt được nhờ probe**: `/positions` trả khoá `positions` (không phải `deals` như SDK) ⇒ adapter cũ báo lỗi mỗi lần đọc vị thế (runner sẽ từ chối khởi động). Đã sửa (nhận cả hai, khuôn khác ⇒ lỗi rõ), probe cũng đọc `positions`; test với body thật `tests/test_dnse_real_responses.py` (7). Sổ lệnh `{"orders": []}` khớp; secdef xác nhận biên ±7%, đơn vị điểm. 1210 test.
- Còn ❓: chuỗi `orderStatus` thật và trường của từng vị thế (cần UAT order test), `trading_token_ttl_seconds` (cần xin OTP).

## 2026-11-09 — Vốn + giới hạn rủi ro (người dùng chọn)

- `backtest.initial_capital_vnd: 100000000`, `risk.max_daily_loss_points: 15` (1,5 triệu/HĐ ≈ 1,5% vốn), `risk.max_trades_per_day: 2`, `risk.max_consecutive_losses: 2`.
- Test "null ⇒ từ chối" của engine/risk chuyển sang bản sao settings (từng khoá); thêm test khoá giá trị người dùng chọn. 1203 test.
- Còn null: `live.allowed_machine` (người dùng), 4 khoá broker (sau khi có khoá API), `paths.dashboard_warehouse` (tuỳ chọn).

## 2026-11-09 — Phí + thuế theo biểu phí DNSE (01/05/2026)

- Người dùng gửi biểu phí DNSE áp dụng từ 01/05/2026 ⇒ `fees`: môi giới 1.000đ (< 100 HĐ/ngày), sở 2.700đ, bù trừ VSDC 2.550đ, thuế TNCN ≈ 17.100đ/HĐ/chiều (0,1% × giá × 100.000 × 18,48% / 2 ở giá ~1.850). Tổng ≈ 23.350đ ≈ 0,23 điểm/chiều.
- Thuế phụ thuộc giá (≈ 9,24đ × giá) — đang ước lượng cố định; cập nhật khi giá đi xa ~1.850 hoặc tỷ lệ ký quỹ đổi.
- Hai test cũ giả định phí trong repo còn null ⇒ đổi sang kiểm "null ⇒ từ chối" trên bản sao settings; thêm test khoá giá trị biểu phí. 1199 test.

## 2026-11-09 — `backtest.robustness` trên tick thật (`--ticks`)

- `run_robustness` nhận tick thay cho bar: mỗi lần chạy = `run_events(bars_from_ticks(…))`; chia IS/OOS, walk-forward, độ nhạy (chỉ IS) theo ngày của tick. CLI `--ticks` (loại trừ `--bars`), `--source` khi nhiều nguồn; khớp lệnh downloader được gắn bid/ask giả (`fill_missing_quotes`) trước khi chạy, có dòng ghi chú.
- Test viết trước `tests/test_robustness_ticks.py` (6). Thử phá 4 cơ chế ⇒ 1 sống (`--source` không được truyền) ⇒ thêm test ⇒ hết. 1198 test.

## 2026-11-09 — Lượt đóng khẩn cấp tính là THUA

**Quyết định của người dùng:** lượt kết thúc nhờ lệnh đối ứng khẩn cấp (broker chính mất kết nối) luôn tính là một lượt thua trong chuỗi thua.

**Đã xong** (`risk/risk_manager.py` `record_fill`)
- Lượt kết thúc bởi fill của lệnh tag `emergency`, hoặc đóng CHÉO broker (net mọi broker về 0 mà broker của fill vẫn còn vị thế) ⇒ `consecutive_losses += 1` bất kể P&L (P&L thật chưa chốt được khi hai chân còn mở). Không cộng hai lần khi vừa đối ứng vừa lỗ.
- Sửa lỗi kèm theo: gỡ hedge (`unwind`) trước đây tạo một **lượt ma** (chân gỡ đầu làm net rời 0 ⇒ `trades_today` +1, chân sau kết thúc lượt và được tính thắng/thua theo một chân). Nay fill tag `unwind` — hoặc fill khi net = 0 mà còn chân mở — không mở/kết thúc lượt ⇒ cả đợt khẩn cấp = đúng MỘT lượt thua, `trades_today` không đổi. Lãi/lỗ thực hiện của các chân vẫn vào lãi/lỗ ngày (`daily_pnl_points`).
- Sau khi `clear_emergency`, lượt khẩn cấp vẫn tính vào `max_consecutive_losses` (có test).
- Test viết trước `tests/test_emergency_streak.py` (11). Thử phá 7 cơ chế ⇒ 1 sống (tag `emergency` thừa khi đã có nhận dạng đóng chéo) ⇒ thêm test tag-trên-cùng-broker ⇒ hết. 1189 test.

~~Giả định: chân gỡ thứ hai mất tag sẽ bị coi là kết thúc lượt~~ ⇒ đã xử lý: `AccountState.unwinding` nhớ HĐ đang gỡ dở (giữ qua `new_day` chỉ khi còn chân mở), chân sau không tag vẫn không kết thúc lượt (3 test mới, thử phá 4 cơ chế ⇒ hết).

## 2026-11-08 — Phí sở + phí bù trừ VSDC (`fees.*`)

- `fees.exchange_fee_vnd_per_contract: 2700` (phí HNX HĐTL chỉ số, mỗi lượt mở/đóng — biểu phí công bố của nhiều CTCK) và `fees.clearing_fee_vnd_per_contract: 2550` (phí bù trừ VSDC sau KRX, FAQ KRX của DNSE; phí qua đêm hệ thống mới = 0). Ghi nguồn trong `settings.yaml`.
- Còn null (việc của bạn): `broker_fee_vnd_per_contract` (theo gói phí tài khoản), `tax_vnd_per_contract` (0,1% × giá trị chuyển nhượng, phụ thuộc giá + tỷ lệ ký quỹ) ⇒ backtest vẫn từ chối chạy tới khi điền.
- Không tính phí quản lý tài sản ký quỹ (0,0024%/tháng) — không theo HĐ, nhỏ với intraday.

## 2026-11-08 — Lịch nghỉ 2025–2026 (`config/holidays.yaml`)

**Đã xong**
- 2026 ĐỦ: 13 ngày thường (01–02/01, 16–20/02, 27/04, 30/04, 01/05, 31/08–02/09) theo thông báo HNX/HOSE (qua báo chí) và **kiểm chéo bằng dữ liệu ngày VN30** từ connector DNSE (10/11/2025–06/11/2026): đúng 13 ngày này không có phiên, mọi ngày thường khác có phiên. 02/01/2026: một bài báo ghi HNX vẫn giao dịch, nhưng VN30 không có phiên ⇒ ghi là nghỉ (❓, hướng an toàn).
- 2025: 12 ngày theo thông báo HNX qua báo chí (CHƯA kiểm chéo bằng dữ liệu — connector chỉ có 1 năm) ⇒ backtest dữ liệu 2025 không còn coi ngày nghỉ là "thiếu dữ liệu".
- `tests/test_holidays_config.py`: khoá danh sách 2026 theo dữ liệu đã quan sát; VN30F2602 đáo hạn 13/02.
- `check_calendar.py`: chỉ còn cảnh báo 2027.

**Việc của bạn**
- [ ] Khi HNX công bố lịch nghỉ 2027 (thường cuối năm nay): bổ sung Tết Đinh Mùi (mùng 1 = 06/02/2027), Giỗ Tổ, ngày nghỉ bù, 02/09. Bắt buộc TRƯỚC 2027 — lịch sai làm lệch ngày đáo hạn/roll.
- [ ] (tuỳ chọn) xem lại 02/01/2026 trên thông báo gốc của HNX.

## 2026-11-08 — Báo cáo backtest theo % vốn (`backtest.initial_capital_vnd`)

**Đã xong**
- `backtest/report.py`: `compute_metrics(trades, initial_capital_vnd)` thêm `return_pct` (P&L sau phí / vốn) và `max_drawdown_pct` (so với giá trị tài khoản tại đỉnh = vốn + lãi luỹ kế tới đỉnh); áp cho toàn bộ / IS / OOS; HTML có hai dòng mới + vốn ở đầu trang. Vốn null ⇒ không tính %, có cảnh báo; vốn ≤ 0 / không phải số ⇒ `ValueError` (CLI thoát 2).
- `backtest.engine` CLI đọc `backtest.initial_capital_vnd`. Sửa chú thích `synthetic_spread_ticks` trong settings cho đúng (±nửa spread từ golive #14).
- Test viết trước `tests/test_report_capital.py` (11). Thử phá 8 cơ chế ⇒ 0 sống. 1176 test.

**Việc của bạn**: điền `backtest.initial_capital_vnd` (vốn dự định cho bot).

## 2026-11-08 — Backtest trên tick thật (`backtest/ticks.py`, CLI `--ticks`)

**Đã xong**
- `load_tick_files(paths, source=None)`: đọc đệ quy; thư mục ngày recorder ⇒ `load_ticks` (gộp part, không đếm đôi khi gộp bị ngắt); file lẻ (khớp lệnh downloader) ⇒ đọc thẳng theo schema tick; file không phải tick ⇒ lỗi; nhiều nguồn mà không `--source` ⇒ lỗi (trộn nguồn ⇒ tick đôi).
- `fill_missing_quotes`: tick khớp thiếu CẢ bid lẫn ask (downloader) ⇒ bid/ask giả = last ∓ max(1, ⌈spread/2⌉) bước (`backtest.synthetic_spread_ticks`, giả định bi quan như tick giả từ bar); tick có báo giá giữ nguyên; báo cáo cảnh báo số tick bị giả báo giá.
- `python -m backtest.engine --ticks data/raw data/history/trades --source dnse …` (loại trừ với `--bars`; `--from/--to` áp cho tick) ⇒ `run_events` (bar dựng từ tick, khớp lệnh trên tick thật).
- Test viết trước `tests/test_backtest_ticks.py` (20). Thử phá 12 cơ chế ⇒ 2 sống (đọc thư mục recorder qua `load_ticks`; lọc ngày cho tick) ⇒ thêm 2 test ⇒ hết.
- 1165 test.

**Rủi ro / giả định còn lại**
- Khớp lệnh downloader không có báo giá ⇒ spread là GIẢ ĐỊNH (như bar); chỉ recorder cho bid/ask thật.
- Khối lượng sổ lệnh coi như vô hạn khi tick không có `bid_qty/ask_qty`.
- ~~`backtest.robustness` vẫn chỉ chạy trên bar~~ ⇒ có `--ticks` (09/11).

## 2026-11-08 — Chênh lệch roll = trung vị nhiều phút

**Đã xong**
- `core/continuous.py` `measure_gap(old, new, minutes=K)`: trung vị của (đóng HĐ mới − HĐ cũ) trên K phút CHUNG cuối của ngày trước roll (ít hơn K thì dùng hết số có); `RollGap.n_minutes` ghi số phút thực dùng. `minutes=1` ⇒ y hệt cách cũ. `HistoryBuilder(gap_minutes=K)`.
- Khoá `contract.roll_gap_median_minutes: 15` dùng CHUNG cho backtest (`BacktestEngine`) và live (`live/history.py`, `runner.main`) ⇒ cùng chuỗi. Chỉ bắt buộc khi chiến lược cần lịch sử; null ⇒ `MissingSetting` (backtest) / thoát 2 (runner); không phải số nguyên ≥ 1 ⇒ lỗi.
- `data_quality.roll_gaps` ghi `(trung vị N phút)`; `assumptions["history"]` ghi K.
- Test viết trước `tests/test_roll_gap_median.py` (19): phút méo bị loại, K phút CUỐI (không phải đầu), chỉ phút chung, số chẵn ⇒ trung bình hai giữa, backtest và live cho cùng chuỗi, `runner.main` truyền K. Thử phá 12 cơ chế ⇒ 0 sống.
- 1145 test.

**Ảnh hưởng nghiên cứu:** ORB #2 chỉ dùng (đỉnh − đáy) ⇒ không đổi theo chênh lệch roll ⇒ kết quả #2 không phụ thuộc thay đổi này, không tính là cấu hình mới (`n_trials` giữ 2). Chiến lược sau này dùng MỨC giá lịch sử (MA, đỉnh/đáy hôm qua…) mới chịu ảnh hưởng.

**Rủi ro / giả định còn lại**
- HĐ kế tiếp mỏng ⇒ K phút chung cuối có thể rải trên nhiều giờ (lẫn biến động trong ngày). `n_minutes` trong báo cáo cho thấy khi ít phút chung.

## 2026-11-08 — ORB cấu hình #2: lọc theo độ rộng biên / ADR (dùng lịch sử nhiều ngày)

**Quyết định của người dùng:** dùng lịch sử để lọc ngày theo độ rộng biên; biến thể MỚI, baseline giữ nguyên.

**Đã xong**
- `strategy/baseline_orb.py`: `RangeFilter` (`strategy.orb.range_filter`: `enabled`, `adr_days`, `min_ratio`, `max_ratio`). Tắt / không có khoá ⇒ y hệt baseline, `history_days = 0`. Bật ⇒ `history_days = adr_days`; ADR = trung bình (đỉnh − đáy) của đúng `adr_days` phiên gần nhất trong `ctx.history` (bỏ bar hôm nay nếu lẫn vào); chỉ giao dịch khi `min_ratio ≤ biên/ADR ≤ max_ratio`; thiếu lịch sử / ADR = 0 ⇒ không giao dịch; lý do tín hiệu ghi tỉ lệ + ADR. Cấu hình sai ⇒ `ValueError`, null ⇒ `MissingSetting`.
- `config/settings.yaml`: `range_filter.enabled: false`, `adr_days: 10`, `min_ratio: 0.10`, `max_ratio: 0.60` — chốt trước khi xem dữ liệu.
- `backtest/robustness.py`: `params_for(settings)` — khi bật, độ nhạy ±20% gồm cả ba tham số lọc; CLI dùng nó.
- `research/orb_range_filter.md` (giả thuyết + đặc tả + cách chạy), `research/log.md` dòng #2 ⇒ `n_trials = 2` (p-value của CẢ HAI cấu hình bị phạt chọn lọc).
- Test viết trước `tests/test_strategy_orb_filter.py` (26), gồm qua `BacktestEngine` (ngày chưa đủ lịch sử không vào lệnh). Thử phá 18 cơ chế ⇒ 2 sống (bỏ bar hôm nay khỏi ADR; CLI robustness dùng tham số lọc) ⇒ thêm 2 test ⇒ hết.
- 1127 test.

**Rủi ro / giả định còn lại**
- CHƯA chạy trên dữ liệu thật — không có bằng chứng edge cho cả #1 lẫn #2.
- ADR tính trên mọi bar của phiên (gồm ATO/ATC); dữ liệu recorder và downloader có thể khác nhau ở bar ATO ⇒ lệch nhỏ.
- `adr_days` phiên đầu dữ liệu không có lệnh ⇒ ít lệnh hơn baseline, dễ rơi vào "CHƯA ĐỦ BẰNG CHỨNG".
- Bật trong live/paper ⇒ runner cần đủ 10 phiên trong `live.history_bar_dirs` (và HĐ kế tiếp ngày trước roll), không thì từ chối chạy.

## 2026-11-08 — Recorder ghi thêm hợp đồng kế tiếp ngày trước roll

**Đã xong**
- `feeds/data_feed_dnse.py`: `DnseFeed(extra_contracts=[…])` đăng ký thêm trên CÙNG kết nối (một message `symbols: [chính, kế tiếp]`) — không mở kết nối thứ hai (giới hạn số kết nối/khoá ❓, kết nối thừa có thể làm rớt kết nối của runner). Độ "im lặng" chỉ tính tick HĐ chính. HĐ phụ không có trong danh sách broker ⇒ bỏ + cảnh báo, vẫn ghi HĐ chính. Không truyền ⇒ y hệt trước (runner không đổi).
- `scripts/record_today.py`: `roll_eve_extras`, `feed_kwargs`, `computed_listing`; `main` truyền HĐ kế tiếp cho feed khi bật **`recorder.record_next_contract_on_roll_eve` (mặc định `false`)**.
- Kiểm định cuối ngày tách theo HĐ (`validate_day(primary_symbol=…)`, `validate_day.run` truyền HĐ giao dịch của ngày): khoảng trống, giá nhảy, KL luỹ kế, lệch hai nguồn, so tham chiếu chỉ trên HĐ chính; HĐ phụ chỉ đếm (`extras`) + dựng bar vào cùng `data/bars/<ngày>.parquet`. Dữ liệu nhiều HĐ mà không chỉ định HĐ chính ⇒ báo lỗi (trước đây kiểm lẫn HĐ).
- `scripts/dnse_feed_probe.py --extra-contract`: in số tick theo HĐ, báo ✅/❌ đã nhận tick của mọi mã.
- Test viết trước `tests/test_recorder_roll_eve.py` (18), gồm đầu-cuối: ghi 18/11 có 2612 ⇒ `validate_day.run` ⇒ `load_live_history` ngày roll đo được chênh lệch. Thử phá 16 cơ chế ⇒ 1 sống (`main` không truyền HĐ phụ cho feed) ⇒ thêm test `main` ⇒ hết.
- 1102 test.

**Việc của bạn**
- [ ] Có khoá DNSE, trong giờ giao dịch: `python scripts/dnse_feed_probe.py --krx <mã chính> <mã kế tiếp> --extra-contract <tên HĐ kế tiếp> --seconds 120` ⇒ thấy ✅ thì đặt `recorder.record_next_contract_on_roll_eve: true`. Chưa bật ⇒ ngày trước roll chạy downloader như cũ.

**Rủi ro / giả định còn lại**
- Nếu server chỉ nhận một mã (hoặc mã cuối thay mã trước), bật cờ sẽ làm recorder mất dữ liệu HĐ chính ngày trước roll ⇒ vì vậy cờ TẮT tới khi probe xác nhận. Runner không bị ảnh hưởng (kết nối riêng, không dùng HĐ phụ).
- HĐ kế tiếp thanh khoản mỏng ⇒ phút chung cuối có thể sớm hơn giờ đóng cửa; vẫn là cùng một phút cho hai HĐ.

## 2026-11-08 — Nối lịch sử nhiều ngày vào runner live/paper

**Đã xong**
- `core/continuous.py` `HistoryBuilder` (`observe` mọi bar, `close_day` ⇒ `DayClose` có gap/ngày trước roll): logic theo ngày DÙNG CHUNG; backtest đã chuyển sang dùng (hành vi không đổi, test cũ vẫn qua).
- `live/history.py` `load_live_history(dirs, hôm nay, N, lịch, trading_contract)`: đọc bar đã lưu, chỉ phiên trước hôm nay, dựng chuỗi như backtest. **Nghiêm ngặt**: đủ đúng N phiên giao dịch gần nhất + mọi roll trong khoảng đo được chênh lệch; thiếu ⇒ `HistoryUnavailable` nêu ngày/HĐ + lệnh downloader. Test: cùng dữ liệu ⇒ `ctx.history` live GIỐNG HỆT backtest.
- `live/runner.py`: `Runner(history_loader=…)`; `startup` gọi sau kiểm ngày giao dịch, TRƯỚC khi kết nối broker; lỗi bất kỳ ⇒ báo + thoát 2. Cấp `ctx.history` (cache theo HĐ trong ngày). Chiến lược không cần lịch sử ⇒ không gọi loader. Không có loader mà cần lịch sử ⇒ vẫn từ chối.
- `main()`: chiến lược có `history_days > 0` ⇒ đọc `live.history_bar_dirs` (null ⇒ thoát 2; thư mục trong Dropbox ⇒ thoát 2) và truyền loader.
- `config/settings.yaml`: `live.history_bar_dirs: [data/history/ohlc1m, data/bars]` (downloader trước — dữ liệu sở, đủ hơn recorder).
- Test viết trước `tests/test_live_history.py` (22). Thử phá 20 cơ chế ⇒ 6 sống: 4 tương đương (điều kiện thừa ⇒ đã bỏ khỏi code) + 2 lỗ thật (thông báo thiếu ngày phải nêu HĐ; thư mục có nhưng rỗng/sai loại) ⇒ thêm test ⇒ hết.
- 1084 test.

**Rủi ro / giả định còn lại**
- ~~Recorder chỉ ghi HĐ đang giao dịch~~ ⇒ đã thêm (mục bên trên), bật sau khi probe xác nhận.
- Downloader chưa được chạy trên dây (P1.6) ⇒ đường này cũng chưa được thử với dữ liệu thật.
- Lịch sử nạp một lần lúc khởi động, cố định trong ngày (bot intraday ⇒ đủ).

## 2026-11-08 — Chuỗi back-adjust cho chiến lược nhiều ngày (`core/continuous.py` + backtest)

**Quyết định của người dùng:** làm module + backtest trước, live/paper TỪ CHỐI chiến lược cần lịch sử tới khi nối riêng; dịch giá bằng CỘNG/TRỪ điểm.

**Đã xong**
- `core/continuous.py` (thuần, dùng chung): `measure_gap` = giá đóng HĐ mới − HĐ cũ ở phút chung CUỐI CÙNG của ngày trước roll; `ContinuousHistory(max_days)` lưu bar thô theo ngày + chênh lệch; `view(hđ hiện tại)` cộng dồn chênh lệch qua các roll, quy về giá HĐ hiện tại (bar mang `symbol` HĐ hiện tại, volume giữ nguyên). Thiếu chênh lệch ⇒ lịch sử DỪNG tại roll đó (không đoán).
- **Không look-ahead:** chênh lệch đo lúc HẾT ngày trước roll, chỉ dùng từ phiên sau; lịch sử ngày X chỉ chịu các roll tới X (có test: chạy dữ liệu cắt tại X cho đúng cùng lịch sử như chạy đủ).
- `StrategyContext.history` (mặc định `()`): chiến lược khai báo `history_days = N` ⇒ engine cấp N phiên gần nhất trước hôm nay. `data_quality`: `roll_gaps` (vd `2026-11-18: VN30F2611 → VN30F2612 +50.0 điểm`), `roll_gaps_missing` + cảnh báo nói cần tải HĐ nào ngày nào; `assumptions["history"]` ghi rõ cách điều chỉnh. Cần `trading_contract_only=True`.
- `live/runner.py`: chiến lược có `history_days > 0` ⇒ `startup` thoát mã 2 trước khi kết nối broker (backtest có lịch sử còn live chưa ⇒ không cho chạy lệch).
- `scripts/download_history.py`: ngày trước roll tải thêm HĐ kế tiếp (cần để đo chênh lệch).
- ORB hiện tại không dùng lịch sử ⇒ kết quả backtest cũ không đổi.
- Test viết trước `tests/test_continuous.py` (15) + `tests/test_backtest_history.py` (13). Thử phá 22 cơ chế ⇒ 2 sống (cắt-tại-roll vs bỏ-qua-roll; dữ liệu `--all-listed` ngày trước roll làm lẫn HĐ) ⇒ thêm 2 test ⇒ hết.
- 1062 test.

**Rủi ro / giả định còn lại**
- ~~Chênh lệch đo ở MỘT phút~~ ⇒ đã đổi sang trung vị `contract.roll_gap_median_minutes` phút (mục bên trên).
- Dữ liệu đã tải trước thay đổi này thiếu HĐ kế tiếp ngày trước roll ⇒ chạy lại downloader (mục đã có bị bỏ qua, chỉ tải phần thiếu).
- ~~Live/paper chưa cấp lịch sử~~ ⇒ đã nối (mục bên trên); recorder vẫn chỉ ghi HĐ đang giao dịch.

## 2026-11-08 — Backtest nối chuỗi qua roll (`backtest/engine.py`)

**Thiết kế**: bot intraday, phẳng mỗi đêm ⇒ chuỗi liên tục = mỗi ngày đưa vào pipeline ĐÚNG hợp đồng bot giao dịch (`trading_contract`, cùng hàm live dùng, theo `roll_days_before_expiry`). **Không back-adjust** giá: P&L tính trên giá thật của từng hợp đồng, khoảng chênh giữa hai hợp đồng không bao giờ lọt vào lệnh vì không có vị thế qua roll.

**Đã xong**
- `load_bars_parquet`: đọc thư mục ĐỆ QUY (`data/history/ohlc1m/<hợp đồng>/<ngày>.parquet`) và nhiều đường dẫn một lúc; bar trùng (symbol, start) giữa các nguồn ⇒ giữ bản volume tin được (hoà ⇒ đường dẫn đưa trước); `stats` đếm file/trùng/trùng-khác-giá; file không phải bar (vd `trades/`) ⇒ lỗi nêu tên file; thư mục rỗng ⇒ lỗi; file `.tmp` đang ghi dở bị bỏ qua.
- Engine thêm vào `data_quality`: `rolls` (vd `2026-11-19: VN30F2611 → VN30F2612`), `other_contract_bars` (bar hợp đồng khác bị bỏ), `days_missing_trading_contract` (có dữ liệu nhưng thiếu đúng hợp đồng giao dịch ⇒ bỏ cả ngày + cảnh báo nêu hợp đồng cần tải), `missing_trading_days` (ngày giao dịch theo lịch không có dữ liệu nào — cuối tuần/ngày nghỉ không tính).
- CLI `backtest.engine` và `backtest.robustness`: `--bars` nhận nhiều đường dẫn; báo cáo cảnh báo bar trùng giữa các nguồn.
- Test viết trước `tests/test_backtest_roll.py` (16). Thử phá 14 cơ chế ⇒ 3 sống (hoà giữa hai nguồn tin được, đếm bar chứ không đếm tick ở chế độ tick, chế độ mọi hợp đồng nhiều ngày) ⇒ thêm 3 test ⇒ hết.
- 1035 test.

**Dùng**: `python -m backtest.engine --bars data/history/ohlc1m --strategy strategy.baseline_orb:OpeningRangeBreakout --split YYYY-MM-DD --n-trials N` (thêm `data/imported` sau nếu muốn lấp chỗ trống — bản có volume thật luôn thắng).

**Rủi ro / giả định còn lại**
- Ngày đáo hạn (`roll_days_before_expiry = 1`): bot đã ở hợp đồng tháng sau, và risk vẫn chặn mở vị thế trong ngày đáo hạn (`trade_on_expiry_day = false`) — đúng như live, nhưng nghĩa là ngày đó không có lệnh.
- Chế độ `trading_contract_only=False` với bar nhiều hợp đồng cùng phút vẫn không chạy được trên tick giả (giới hạn cũ: tick giả của hai hợp đồng không xen kẽ theo thời gian). Không dùng cho bot hiện tại.
- ~~Chuỗi back-adjust cho chiến lược nhiều ngày~~ ⇒ đã làm (mục bên trên), live/paper chưa cấp.

## 2026-11-08 — P1.6 bước 2: `scripts/download_history.py` (tải lịch sử DNSE) — code xong, CHƯA chạy trên dây

**Cơ sở**: đặc tả OpenAPI chính thức DNSE (`dnseopenapi-en.yaml`) chốt được phần lớn ❓ của bước 1 (from/to epoch giây, OHLC dạng cột + `nextTime`, khuôn trades). Phần còn ❓ trên dây được downloader **tự kiểm** thay vì đoán (broker_notes §1.3, bảng mới).

**Đã xong**
- `HistoryClient`: ký HMAC path không kèm query (như SDK), 401 ⇒ thử kèm query một lần rồi nhớ; 429 ⇒ chờ `Retry-After` rồi thử lại (tối đa N lần); giãn cách request; lỗi khác ⇒ `HistoryError` kèm mã `OA-…`; secret không lọt vào lỗi/repr.
- OHLC 1 phút mỗi ngày 08:30–15:00 VN, phân trang `nextTime` (không tăng ⇒ lỗi, vượt cửa sổ ⇒ dừng), lọc trùng/sắp xếp, bỏ giá ngoài khoảng hợp lý ⇒ `ohlc1m/<VN30FYYMM>/<ngày>.parquet` đúng schema `load_bars_parquet` (`volume_reliable=True`).
- Khớp lệnh theo cửa sổ 30 phút, cửa sổ đầy `limit` ⇒ chia đôi; lọc trùng theo `totalVolumeTraded`; kiểm múi giờ `time` (UTC rồi VN, phải nằm trong cửa sổ) ⇒ `trades/<VN30FYYMM>/<ngày>.parquet` schema tick recorder (`source=dnse_history`); `volume_gap` ghi vào manifest để biết thiếu lệnh.
- Kế hoạch theo lịch giao dịch + hợp đồng đang giao dịch (sau roll), `--all-listed` thêm F2M/F1Q/F2Q. Ghi nguyên tử, chạy lại không tải trùng (file có sẵn hoặc manifest ghi rỗng), `--force`, `--retry-empty`; lỗi một ngày không dừng các ngày khác; thư mục đích chặn nếu nằm trong Dropbox.
- Test viết trước `tests/test_download_history.py` (21). Thử phá 16 cơ chế ⇒ 4 sống, trong đó 2 tương đương (thứ tự thử múi giờ — cửa sổ 30 phút không thể khớp cả UTC lẫn VN; giữ bản đầu/cuối khi trùng — hai bản giống nhau) ⇒ thêm 2 test cho 2 lỗ thật (`nextTime == from`, `nextTime` vượt cửa sổ) ⇒ hết.
- 1019 test.

**Rủi ro / giả định còn lại**
- CHƯA gọi trên dây lần nào (môi trường này không có khoá và không tới được `openapi.dnse.com.vn`). Theo CLAUDE.md mục 8, phải chạy thử trước khi dựa vào dữ liệu: `dnse_history_probe.py`, rồi downloader 1–2 ngày, đối chiếu với tick recorder (`validate_day.py`).
- `t` của OHLC được coi là giờ MỞ nến (ví dụ đặc tả) — nếu là giờ đóng, mọi bar lệch 1 phút (lookahead nhẹ trong backtest) ⇒ phải kiểm.
- Độ sâu lịch sử của hợp đồng đã đáo hạn chưa biết: ngày rỗng ghi manifest.
- ~~`backtest.engine --bars` không đọc đệ quy~~ ⇒ đã xử lý ở mục "Backtest nối chuỗi qua roll" bên trên.

**Việc của bạn**
- [ ] Có khoá DNSE: `python scripts/dnse_history_probe.py` rồi `python scripts/download_history.py --start <hôm qua> --end <hôm qua>`; gửi `_manifest.json` + log (không chứa khoá).

## 2026-11-08 — Stop điều kiện phía TCBS (SOU/SOL) — sau cờ `brokers.tcbs.broker_side_stops` (mặc định TẮT)

**Đã xong** (tài liệu 6.5/6.7/6.9/6.11/7.3)
- `OrderType.STOP` khi bật cờ ⇒ `POST /khronos/v1/order/condition/place`: bán ⇒ SOU (giá ≤ soPrice), mua ⇒ SOL (≥), giá lệnh sinh ra = sàn/trần; `broker_order_id = cond:<pk>`. Không bật cờ ⇒ stop do bot giữ như cũ.
- Đồng bộ sổ lệnh điều kiện (0 chờ / 1 kích hoạt / 2 huỷ / 3 từ chối) + stream `DE_CONDITIONAL_ORDER` (đã thêm vào subscribe). Sổ REST không có refId lẫn số lệnh kích hoạt ⇒ liên kết lệnh thường sinh ra qua `details` của stream, hoặc lệnh MỚI cùng mã/chiều/KL; lệnh như vậy được GIỮ lại tối đa `uncertain_resolve_s` chứ không nhận ngay là lệnh tay. Kích hoạt mà chưa liên kết ⇒ stop vẫn PENDING (risk không thấy "mất stop" ⇒ không đóng hai lần).
- Huỷ: còn chờ ⇒ `condition/cancel` (pk), chỉ CANCELLED khi sổ báo đã huỷ; đã kích hoạt ⇒ huỷ lệnh thường sinh ra. Sửa giá kích hoạt ⇒ `v2/order/condition/change`, theo pk MỚI; không sửa KL. Lệnh "không rõ" ⇒ đối soát pk mới cùng nội dung trong sổ điều kiện. Lệnh điều kiện còn chờ mà bot không biết (vd bot chết) ⇒ báo là STOP đang sống (tag external) để bot xử lý.
- Test viết trước `tests/test_tcbs_condition_stop.py` (24). Test bắt được 1 lỗi thật: lệnh do stop sinh ra bị giữ lại ở lần đọc trước lại bị loại khỏi ứng viên liên kết ⇒ không bao giờ nối được. Thử phá 20 cơ chế ⇒ ban đầu 6 sống ⇒ thêm 6 test ⇒ 20/20.
- 998 test.

**❓ phải kiểm trước khi bật** (broker_notes §2b): thời hạn hiệu lực mặc định của lệnh điều kiện — nếu sống qua ngày, stop bán kích hoạt khi đang phẳng sẽ MỞ vị thế Short; mã hợp đồng KRX hay mã cũ cho lệnh điều kiện; refId lệnh điều kiện có chống trùng không.

## 2026-11-08 — Nối TCBS vào runner (chọn làm broker chính hoặc dự phòng)

**Quyết định của người dùng:** `brokers.primary` / `brokers.backup` chọn được trong {dnse, ssi, tcbs}; giữ kiến trúc một-chính-một-dự-phòng; mặc định vẫn DNSE/SSI.

**Đã xong**
- `live/wiring.py` `build_live`: dựng đúng broker theo vai (khác nhau, tên không hỗ trợ ⇒ thoát 2). TCBS chỉ dựng khi `brokers.tcbs_enabled` + `api_verified` + đủ `TCBS_*`. Feed tổng hợp LUÔN DNSE + SSI (CLAUDE.md mục 3): DNSE không làm broker ⇒ chỉ cần khoá HMAC cho feed + `/instruments`; SSI không làm broker ⇒ chỉ cần API key/secret, token feed nằm trong `auth` (runner chờ `/otp ssi`). Stream lệnh cho broker SSI/TCBS chạy nền.
- `main()` truyền `auth` vào Runner; startup chờ OTP của mọi broker + token feed (`/otp dnse|ssi|tcbs`).
- Test viết trước (10 test mới trong `tests/test_live_wiring.py`): dnse+tcbs, tcbs+ssi, trùng/không hỗ trợ, TCBS chưa bật/chưa kiểm, thiếu env, feed vẫn cần khoá SSI, chờ ba OTP, `main` truyền đúng tham số. Thử phá 8 chốt ⇒ 1 sống (`main` không truyền `auth`) ⇒ thêm test ⇒ 8/8.
- 974 test.

**Lưu ý**: risk/quy trình khẩn cấp không giả định tên broker (đã kiểm) ⇒ đối ứng khẩn cấp chạy trên broker dự phòng bất kể là ssi hay tcbs. TCBS vẫn tắt mặc định.

## 2026-11-08 — Stream DE_ORDER cho TCBS (`TcbsOrderStream`)

**Đã xong** (theo tài liệu 7.3 chính thức)
- Kết nối `wss://openapi.tcbs.com.vn/ws/nesoi` với ping RFC 6455 TẮT (`tcbs_ws_connector`); `authenticate|base64({"jwt":…})` (JSON gọn đúng như ví dụ tài liệu) → chờ `authenticate|{"success":true}` rồi mới `subscribe` DE_ORDER + DE_OPEN_POSITION (chuỗi base64 khớp nguyên văn tài liệu); heartbeat TEXT `ping|1` mỗi 2s.
- `TcbsBroker.on_stream_frame`: DE_ORDER áp ngay khối lượng khớp cộng dồn (trùng không đếm đôi, bản tin cũ đến muộn không kéo lùi trạng thái); lệnh lạ (DE_ORDER không có refId) ⇒ `resync_requested` ⇒ `ping()` đọc lại REST ngay; DE_OPEN_POSITION ⇒ `stream_net`. Frame hỏng bị bỏ, không ném lỗi.
- Mỗi lần (re)connect ⇒ đọc lại REST (stream không số thứ tự, không phát lại). Xác thực bị từ chối ⇒ huỷ token (cần iOTP mới). Backoff min(2^n, 30)s; phiên đã xác thực rồi mới rớt không tính là thất bại; 10 lần thất bại liên tiếp ⇒ cảnh báo đúng một lần.
- Test viết trước `tests/test_tcbs_stream.py` (15); test bắt được 1 lỗi khi viết (phiên đã chạy tốt rồi rớt bị tính thất bại ⇒ backoff tăng mãi). Thử phá 15 cơ chế ⇒ 1 sống (lệnh lạ bị che bởi resync lúc connect) ⇒ thêm test ⇒ 15/15.
- 964 test.

**Lưu ý**: TCBS vẫn TẮT và chưa nối vào runner; máy chạy bot phải cho phép host `openapi.tcbs.com.vn` (môi trường cloud này chỉ mở `developers.tcbs.com.vn` để đọc tài liệu).

## 2026-11-08 — Adapter TCBS (`brokers/tcbs.py`) — broker thứ 3, TẮT mặc định

**Nguồn:** tài liệu chính thức `developers.tcbs.com.vn` v1.0.0 + đặc tả `openapi-v1.0.0.json` (cần mở host này: đã tạo môi trường cloud riêng "Future-trading" chỉ thêm `developers.tcbs.com.vn`, bạn đã duyệt). Gói PyPI `tcbs` không có provenance ⇒ không dùng. Ghi vào `docs/broker_notes.md` §2b (endpoint, field, mã trạng thái, mã lỗi, bảng ❓).

**Đã xong**
- `TcbsHttp`: Bearer, không retry; lỗi = HTTP ≥ 400 hoặc `rc < 1`; mã lỗi nghiệp vụ trong bảng của TCBS (kể cả kèm HTTP 500, vd -9999) ⇒ chắc chắn chưa tiếp nhận; 5xx/timeout không mã ⇒ "không rõ".
- `TcbsTokenManager`: API Key + **iOTP người dùng đọc trong app TCInvest** (`/otp tcbs <mã>`); token hết hạn = min(8 giờ theo tài liệu, `exp` trong JWT); không refresh. Cảnh báo API Key còn < 7 ngày.
- `TcbsBroker`: `refId` suy từ client id (TCBS KHÔNG chống trùng refId ⇒ không bao giờ gửi lại); lệnh "không rõ" tra sổ lệnh đúng theo `refId` (cách tài liệu chỉ), quá 15s ⇒ REJECTED. Lệnh thị trường = LO giá trần/sàn (API không mô tả MTL ❓). Sửa: giá và KL hai request (TCBS cấm sửa đồng thời), `nvol` là tổng, refId mới mỗi lần. Huỷ: -6004/-8004 (đã huỷ/đã khớp) an toàn, chỉ báo CANCELLED khi sổ lệnh xác nhận. Vị thế từ `data.detailData` (net/side lệch ⇒ lỗi). Khớp cộng dồn (`matchVolume`/`matchPriceBQ`), lệnh ngoài bot được theo dõi, stop do bot giữ.
- Chốt trước MỌI lệnh thay đổi: TRADING_MODE=live + `--live` + `brokers.tcbs_enabled` + `brokers.tcbs.api_verified` + token.
- `scripts/tcbs_trading_probe.py`: `--payload-only` hoặc iOTP → CHỈ ĐỌC (hạn API Key, tiểu khoản, trần/sàn + định dạng mã, vị thế, sổ lệnh trang 0/1/2); không đặt lệnh (không có môi trường thử).
- Test viết trước: `tests/test_broker_tcbs.py` (70), `tests/test_tcbs_probe.py` (3). Thử phá 24 cơ chế ⇒ 1 sống (mã orderStatus 7 bị chuỗi trạng thái che) ⇒ thêm ca ⇒ 24/24.
- 949 test.

**Chưa làm / ❓**: chưa nối vào runner (CLAUDE.md: chỉ bật sau khi DNSE + SSI ổn định); stream `DE_ORDER` chưa dùng (REST đủ, tài liệu nói stream không phát lại); lệnh điều kiện SOU/SOL (stop phía broker) chưa dùng; các ❓ ở broker_notes §2b (MTL, pageNo 0/1, mã cũ trong ví dụ, rate limit).

## 2026-11-08 — Feed tổng hợp DNSE + SSI cho chế độ paper

**Đã xong**
- `live/wiring.py` `build_paper_feed`: `paper.composite_feed: true` ⇒ `CompositeFeed(DNSE, SSI)` giống live (lệch giá / một feed im ⇒ ngừng mở vị thế, cảnh báo Telegram qua `queue_alert`); false (mặc định) ⇒ chỉ feed DNSE như cũ. Thiếu SSI_API_KEY/SECRET ⇒ thoát 2; bật mà không có Telegram ⇒ thoát 2 (không nhập được OTP).
- Runner nhận `auth`: đối tượng cần OTP không phải broker (token SSI cho feed). Khởi động: connect → chờ `/otp` cho cả broker lẫn auth → mới đối soát/giao dịch. `/otp ssi <mã>` tới token manager; trùng tên với broker (live) ⇒ chỉ broker nhận, không gửi mã hai lần. "Đang chờ OTP" giờ = đã xin mã VÀ chưa có token (bền hơn khi broker báo awaiting nhầm).
- `SsiTokenManager`: `name = "ssi"`, `trading_ready`, `connect()` (xin OTP đúng một lần khi chưa có token).
- Test viết trước `tests/test_paper_composite.py` (13 test); thử phá 9 cơ chế ⇒ 9/9 bị bắt.
- 876 test.

**Lưu ý**: paper với feed tổng hợp phải nhập OTP SSI mỗi lần khởi động (token chỉ ở bộ nhớ); SSI token chỉ-dữ-liệu có cần OTP không vẫn ❓ (broker_notes §2.4).

## 2026-11-08 — Hoàn tất phần checklist go-live làm được không cần khoá

**Đã xong**
- §3b: runner từ chối khởi động khi `paths.data_dir` / `state_dir` / `log_dir` nằm trong Dropbox/OneDrive (trước đó chỉ recorder kiểm).
- §4: đối chiếu mã hợp đồng với broker lúc khởi động live — `brokers/dnse.py` `fetch_vn30_futures` (`GET /instruments?symbol=…&marketId=DVX&securityGroupId=FU`, theo đặc tả OpenAPI chính thức `docs/dnseopenapi-en.yaml` của repo SDK) + `verify_contracts`: `symbolType` DNSE báo (VN30F1M/F2M/F1Q/F2Q) phải trùng mã bot tự tính, thiếu F1M ⇒ lỗi; `live/wiring.py` dùng danh sách này cho feed và chạy `preflight` (hợp đồng đang giao dịch sau roll phải có trong danh sách). Runner chạy preflight sau khi có token, TRƯỚC khi đối soát/giao dịch; lỗi ⇒ thoát 2. Live bỏ qua `--krx`/`--assume-computed-contract`.
- #14 (backtest bớt lạc quan): tick giả có bid/ask cách giá ≥ 1 bước mỗi phía (spread lẻ trước đây cho bán ở đúng giá); cực trị giữ lâu hơn độ trễ paper + 1s (trước: độ trễ ≥ 1s ⇒ stop khớp ở cực trị KIA); lệnh còn mở cuối dữ liệu vào số liệu (giá cuối, trừ ước phí đóng) kèm cảnh báo. Ba test cũ đổi kỳ vọng đúng 1 bước giá (Long thoát chịu thêm nửa spread). Chưa có backtest nào trên dữ liệu thật ⇒ không có kết quả nào bị ảnh hưởng.
- `tests/test_execution.py`: 20 test trực tiếp cho `ExecutionManager` với broker giả điều khiển được (khớp một phần, huỷ chưa xác nhận, lỗi mạng, broker chết, đảo chiều ngoài ý muốn, lệnh đóng kẹt, KILL/FLATTEN/khẩn cấp). Thử phá 14 cơ chế ⇒ 2 sống vì risk chặn hộ ⇒ siết test để kiểm chính ExecutionManager ⇒ 14/14.
- Thử phá thêm: đối chiếu hợp đồng + chặn Dropbox 7/7 (sau khi thêm test ngày đáo hạn), #14 6/6.
- `docs/golive_checklist.md`: thêm mục 0 "Trạng thái mới nhất", cập nhật §2/§3/§6.
- 865 test.

**Còn lại là việc của bạn** (không tự làm được): lịch nghỉ 2026–2027 theo thông báo của sở; `fees.*`, `risk.*`, `live.allowed_machine`, `backtest.initial_capital_vnd`; khoá API + ba script thăm dò; dữ liệu lịch sử; paper ≥ vài tuần.
- Tuỳ chọn chưa làm: `brokers/tcbs.py` (CLAUDE.md: chỉ bật sau khi DNSE + SSI ổn định), feed tổng hợp cho paper.

## 2026-11-08 — Logger JSON có cấu trúc (`monitoring/logger.py`)

**Đã xong**
- `JsonFormatter`: một dòng JSON mỗi bản ghi — `ts` (giờ VN, ms), `level`, `logger`, `msg`, mọi field `extra=`; datetime/Enum/dataclass/Decimal/set đều ghi được, kiểu lạ ⇒ repr; lỗi ⇒ `exc_type`, `exc_msg`, `exc` (traceback).
- Bảo mật (CLAUDE.md mục 7): khoá nhạy cảm ở mọi độ sâu (apiSecret, otp, passcode, *token, *secret, password, Authorization, X-Signature…) ⇒ `***`; giá trị bí mật từ `.env` (*_KEY/*_SECRET/*_TOKEN/*_PASSWORD, ≥ 6 ký tự) ⇒ `<secret>` trong message, field VÀ traceback; mẫu URL bot Telegram / `Bearer …` / `"tradingToken": …` bị xoá kể cả khi chưa đăng ký. Console cũng được lọc.
- `DailyJsonlHandler`: `logs/<prefix>_YYYY-MM-DD.jsonl` theo ngày giờ VN (đổi file lúc 00:00), append khi khởi động lại, flush từng dòng, lỗi ghi không làm chết bot, từ chối thư mục trong Dropbox. `setup_logging` gọi lại an toàn (không nhân đôi handler). `read_events(path, event=…)` bỏ dòng hỏng.
- Nối vào: `live/runner.py` (thay basicConfig; sự kiện `signal`, `risk_action`, `notify`), `core/execution.py` (`order` mỗi lần đổi trạng thái, `fill` kèm `net_after`, `exec_rejected` kèm mã risk, các `exec_*` khác), `scripts/record_today.py` (`record_*.jsonl` thay file .log). `log_event` không làm gì khi mức log tắt ⇒ backtest không chậm. Settings `paths.log_dir: logs`.
- Test viết trước `tests/test_logger.py`; test bắt được 2 lỗi thật khi nối: `Side(str, Enum)` bị ghi `"Side.BUY"` (phải xét Enum trước str), và test gọi `runner.main()` ghi log vào `logs/` của repo (chặn trong conftest). Thử phá 17 cơ chế ⇒ 16 bị bắt, 1 là điều kiện thừa ⇒ đã bỏ.
- Golive checklist §2: logger JSON ✔. `docs/runner.md` thêm mục Log + cách dựng timeline.
- 825 test.

**Giả định**
- Log chứa số tài khoản và mã lệnh (không phải bí mật) — vẫn KHÔNG nên chia sẻ công khai.
- Bí mật ngắn < 6 ký tự không được đăng ký (để không xoá nhầm chữ số trong log); OTP vẫn bị che qua tên field (`otp`/`passcode`) và bot không bao giờ log mã.

## 2026-11-08 — Nối LIVE vào runner (`live/wiring.py`) — vẫn KHOÁ tới khi bạn mở từng chốt

**Đã xong**
- `live/wiring.py` `build_live`: DNSE chính (`DnseBroker`) + SSI dự phòng (`SsiBroker`, cho `EmergencyHandler`) + `CompositeFeed` DNSE/SSI (golive #13 — lệch giá / im lặng ⇒ ngừng mở vị thế) + `SsiOrderStream` (tác vụ nền). Feed SSI và broker SSI dùng chung một `SsiTokenManager`. Không gọi mạng; thiếu bất kỳ thứ gì (`api_verified`, giá trị null, biến .env, file khoá SSI, broker không phải dnse/ssi) ⇒ ném lỗi ⇒ runner thoát 2 trước khi kết nối.
- `live/runner.py`:
  - `main()` nhánh live: TRADING_MODE=live + `--live` ⇒ `build_live`; live bắt buộc Telegram. Paper giữ nguyên (PaperBroker + feed DNSE).
  - `startup()`: kết nối mọi broker → **chờ `/otp`** cho broker có `trading_ready=False` (tối đa `live.otp_wait_seconds` = 600s; quá ⇒ thoát 2; `/kill` ⇒ thoát 0) → rồi mới đối soát vị thế/lệnh chờ trên cả hai broker.
  - Tác vụ nền (`background`): chạy sau khởi động, dừng TRƯỚC khi đóng broker; tác vụ chết ⇒ cảnh báo đúng một lần (khớp lệnh vẫn đọc qua REST).
  - `queue_alert`: cảnh báo của feed tổng hợp (callback đồng bộ) được gửi Telegram ở vòng kế tiếp.
- `DnseBroker.trading_ready`, `SsiBroker.trading_ready`; settings `live.otp_wait_seconds: 600`.
- Test viết trước `tests/test_live_wiring.py` (24 test): dựng đủ thành phần, từng chốt từ chối (api_verified mỗi bên, giá trị null, biến .env, file khoá, tên broker, live_flag), `main` từ chối, chờ OTP → đối soát sau, hết giờ, /kill, tác vụ nền. Thử phá 12 chốt ⇒ 11 bị bắt, 1 sống (dừng tác vụ nền) ⇒ thêm test thứ tự dừng ⇒ 12/12.
- 805 test.

**Vẫn chưa chạy live — các chốt còn đóng**
- `brokers.dnse.api_verified` / `brokers.ssi.api_verified` = false; null: `fees.*`, `risk.*`, `brokers.dnse.loan_package_id`, `brokers.dnse.trading_token_ttl_seconds`, `brokers.ssi.order_symbol_format`, `brokers.ssi.market_order_type`, `live.allowed_machine`.
- Chưa có kiểm thử đầu-cuối với broker thật (không có khoá). Paper trade ≥ vài tuần, so với backtest cùng kỳ trước khi cân nhắc live (golive checklist §6).

**Rủi ro còn lại**
- Token SSI/DNSE hết hạn giữa phiên ⇒ adapter tự xin OTP và nhắn Telegram; trong lúc chờ, broker đó bị coi là mất kết nối (DNSE: có vị thế quá `broker_timeout_seconds` ⇒ đối ứng khẩn cấp trên SSI).
- Paper chưa dùng feed tổng hợp (cần OTP SSI kể cả khi paper — giữ paper đơn giản).
- Khởi động lại giữa phiên ⇒ nhập lại hai OTP; stop do bot giữ bị mất cho tới khi risk đòi stop mới / đóng vị thế.

**Người dùng cần làm**: như các mục P4.1/P4.2 (chạy hai script thăm dò, gửi kết quả), điền settings còn null; chỉ đổi `api_verified` khi mình đã chốt các ❓ cùng bạn.

## 2026-11-08 — P4.2 Adapter SSI (`brokers/ssi.py`) — broker dự phòng, live KHOÁ tới khi kiểm API

**Đã xong**
- Đọc mã nguồn SDK `ssi-sdk` 3.3.2 (trading, token_manager, portfolio, rest_client, crypto, stream TRADING) ⇒ `docs/broker_notes.md` §2.6: endpoint, field, mã trạng thái lệnh (PD/QU/PF/FF/CL/RJ…), bảng ❓ và cách xử lý. Phát hiện: SSI có lệnh điều kiện FCO (stop, trailing, OCO) — hỗ trợ phái sinh ❓; SDK không có môi trường UAT; SDK không có phát lại sự kiện theo `notifyId`.
- `brokers/ssi.py`:
  - `SsiHttp`: Bearer + ký RSA-SHA256 (thuần thư viện chuẩn, như SDK) trên đúng chuỗi body gửi đi; không retry; nhận ra lỗi nằm trong HTTP 200 (`{code, msg}`).
  - `SsiTokenManager` (dùng CHUNG cho broker và feed SSI): OTP nhập tay `/otp ssi <mã>`; refresh trước hạn 30s, một luồng (khoá); refresh token bị bỏ khỏi bộ nhớ TRƯỚC khi gửi ⇒ lỗi mạng giữa chừng không bao giờ dẫn tới gửi lại (SSI thu hồi cả phiên); 401101–401108 ⇒ xin OTP. Token chỉ ở bộ nhớ, không ghi đĩa.
  - `SsiBroker`: `clientRequestId` = client_order_id; lệnh "không rõ" (timeout/5xx/409) tìm CHÍNH XÁC theo clientRequestId trong sổ lệnh, không gửi lại; bị coi là chưa tới sàn mà sau đó vẫn hiện ⇒ theo dõi như lệnh ngoài (net position đúng). Sửa = một request cho giá, một cho khối lượng. Huỷ chỉ báo CANCELLED khi sổ lệnh báo CL. Vị thế từ `derOpenPositions` (net ≠ long − short ⇒ lỗi, không đoán). Đọc sổ lệnh theo trang tới khi hết. Stop do bot giữ như DNSE. Lệnh đặt tay được theo dõi.
  - Chốt trước MỌI lệnh thay đổi: TRADING_MODE=live + `--live` + `api_verified` + khoá ký + token; dry-run chỉ log. Hai ❓ `order_symbol_format` (KRX hay VN30FYYMM) và `market_order_type` (MTL hay MP) còn null ⇒ live từ chối chạy.
  - `SsiOrderStream`: kênh TRADING `order.<tk>` + `portfolio.<tk>`; `orderEvent` (cộng dồn) áp ngay, trùng `notifyId` bị bỏ, sự kiện cũ đến muộn không kéo lệnh đã kết thúc về "còn sống"; `orderMatchEvent` ⇒ đọc lại sổ lệnh ngay; mỗi lần (re)connect ⇒ đọc lại sổ lệnh REST (bù sự kiện lỡ — thay cho "lưu notify_id để phát lại" vì SDK không có cơ chế đó).
- Runner: `/otp [dnse|ssi] <mã>` — không ghi tên ⇒ gửi cho broker duy nhất đang chờ OTP; hai broker cùng chờ ⇒ hỏi lại. DNSE thêm `awaiting_otp`.
- `scripts/ssi_trading_probe.py`: `--payload-only` (không mạng) hoặc OTP → CHỈ ĐỌC masterdata/sổ lệnh/vị thế; ghi response thô đã xoá apiSecret/otp/token + `summary.json` (mã hợp đồng SSI trả về, chuỗi trạng thái, có bọc `data` không). Không đặt lệnh (không có UAT).
- `config/settings.yaml` thêm `brokers.ssi.*`; `.env.example` ghi chú `SSI_PRIVATE_KEY_PATH`, `/otp ssi`.
- Test viết trước: `tests/test_broker_ssi.py` (khoá RSA sinh trong test, server giả kiểm chữ ký bằng khoá công khai, mô phỏng refresh-một-lần và thu hồi phiên), `tests/test_ssi_probe.py`, runner/parser. Thử phá 32 cơ chế an toàn ⇒ ban đầu 9 sống sót ⇒ thêm test (refresh song song khi cùng bị 401, OTP sai định dạng, cửa sổ đối soát, huỷ chưa xác nhận, dry-run huỷ, 401 ⇒ lệnh sau dùng token mới, lọc tài khoản trên stream, frame trùng, sự kiện cũ đến muộn) ⇒ 32/32 bị bắt.
- 781 test.

**Rủi ro / giả định còn lại**
- Chưa gọi thử được API SSI (không có khoá, không có UAT): mã hợp đồng khi đặt lệnh, MTL/MP, response có bọc `data`, ngữ nghĩa `quantity` khi sửa, `bidAvgPrice/askAvgPrice` ứng với Long/Short — đều ❓ (broker_notes §2.6).
- Khởi động lại runner ⇒ phải OTP lại SSI (token không ghi đĩa — chủ ý, để không bao giờ gửi lại refresh token cũ).
- Một phiên mỗi apiKey: chạy thêm công cụ khác dùng cùng apiKey (vd script probe trong lúc bot chạy) sẽ làm token của bot mất hiệu lực.
- Runner chưa nối: SSI làm broker dự phòng cho `EmergencyHandler`, `SsiFeed` + `SsiOrderStream` dùng chung token, composite feed (#13).

**Người dùng cần làm**
- [ ] Đăng ký FastConnect V3, tải private key, điền `.env` (SSI_CLIENT_ID/API_KEY/API_SECRET/PRIVATE_KEY_PATH/ACCOUNT).
- [ ] Chạy `python scripts/ssi_trading_probe.py`, gửi lại `data/probe/ssi_trading_*` ⇒ chốt `order_symbol_format`, `market_order_type`. Hỏi SSI lệnh thị trường cho phái sinh qua API là MTL hay MP, và FCO stop có dùng cho phái sinh không.

**Bước tiếp theo:** nối live vào runner (DNSE chính + SSI dự phòng + composite feed, #13) sau khi có kết quả probe của cả hai bên.

## 2026-11-08 — P4.1 Adapter DNSE (`brokers/dnse.py`) — live KHOÁ tới khi kiểm API

**Quyết định của người dùng:** OTP email nhập tay qua Telegram `/otp <mã>` (không dùng Gmail API — tránh thư viện Google lớn + cho bot quyền đọc hộp thư); lớp HTTP tự viết mỏng trên `requests` (không dùng SDK `dnse`); viết đủ adapter nhưng chặn lệnh thật ở production tới khi các mục ❓ được kiểm. PROMPTS ghi "đăng nhập JWT" là bản trước KRX ⇒ làm theo V2: ký HMAC mọi request + trading token.

**Đã xong**
- Đọc mã nguồn SDK `dnse` 0.5.0 (orders, registration, accounts, deals, stream lệnh) ⇒ `docs/broker_notes.md` §1.5 (endpoint, field, bảng ❓ và cách adapter xử lý từng mục); sửa §1.1 (header `date`, ký path không kèm query theo mã SDK).
- `brokers/dnse.py`:
  - `DnseHttp`: ký HMAC như SDK, MỘT request = MỘT lần gửi (không retry 429/5xx/timeout), `trading-token` chỉ gửi kèm lệnh thay đổi, sửa lỗi `metadata` chưa escape của server, `repr` không lộ khoá.
  - `DnseBroker`: đặt LO/MTL/ATO/ATC, sửa (huỷ-đặt ⇒ orderId mới, cùng client id, gửi phần CÒN LẠI), huỷ (chỉ báo CANCELLED khi sàn xác nhận), vị thế (gộp deal, thiếu trang / chiều lạ / deal mâu thuẫn ⇒ lỗi, không đoán), lệnh chờ.
  - Không bao giờ gửi lại lệnh: timeout/lỗi mạng/408/5xx ⇒ "không rõ", đối soát sổ lệnh (chỉ lệnh MỚI xuất hiện sau lúc gửi, khớp mã/chiều/loại/KL/giá); quá `uncertain_resolve_seconds` ⇒ REJECTED. Gọi lại cùng client id chỉ trả lệnh cũ.
  - Chốt trước MỌI lệnh thay đổi: `TRADING_MODE=live` + `--live` + `api_verified` (production) + trading token còn hạn. `dry_run` chỉ log payload.
  - Khớp lệnh: đọc sổ lệnh mỗi `poll_seconds` trong `ping()`, phát Fill (trước) rồi cập nhật lệnh; trạng thái suy từ khối lượng trước, chuỗi `orderStatus` sau (chưa biết giá trị thật). Lệnh đặt tay trên app được theo dõi (`EXT<id>`) để net position đúng; lệnh có sẵn lúc khởi động không phát lại phần đã khớp.
  - Stop do bot giữ (API không có lệnh stop): chạm giá ⇒ gửi MTL đúng một lần, cùng client id.
  - Token: xin OTP lúc `connect()` / khi 401 `INVALID_TRADING_TOKEN` / trước khi hết hạn; không token ⇒ `ping()` = False (risk chặn mở vị thế; có vị thế quá `broker_timeout_seconds` ⇒ quy trình khẩn cấp).
- Runner: lệnh Telegram `/otp <mã>` (không bao giờ nhắc lại mã), chuyển tick cho MỌI broker có `on_tick` (kể cả async). `telegram.parse_commands` giữ tham số chỉ cho `/otp`. Live vẫn bị từ chối (chưa nối, chờ kiểm API + P4.2).
- `scripts/dnse_trading_probe.py`: `--payload-only` (không mạng), mặc định chỉ đọc (tài khoản, gói vay, sổ lệnh, lịch sử, vị thế, secdef), `--uat --order-test [--with-modify] --live` đặt-sửa-huỷ thử một lệnh LO giá sàn trên UAT qua chính adapter; ghi response thô đã xoá passcode/token + `summary.json` (chuỗi `orderStatus` thấy được).
- `config/settings.yaml` thêm `brokers.dnse.*` (`loan_package_id`, `trading_token_ttl_seconds` = null; `api_verified: false`); `.env.example` thêm `DNSE_ACCOUNT_NO`, bỏ `DNSE_OTP_EMAIL`.
- Test viết trước: `tests/test_broker_dnse.py` (89 test), `tests/test_trading_probe.py` (6), runner/parser (4). Thử phá 23 cơ chế an toàn của adapter ⇒ ban đầu 4 cơ chế sống sót ⇒ thêm test (lệnh có sẵn đã khớp một phần lúc khởi động, lệnh giống hệt đã khớp xong không được nhận nhầm, deal mâu thuẫn, trạng thái sau sửa lệnh) ⇒ 23/23 bị bắt.
- 680 test.

**Rủi ro / giả định còn lại**
- Chưa gọi thử được API (không có khoá, proxy chặn) ⇒ mọi mục ❓ ở broker_notes §1.5 chưa kiểm: giá trị `orderStatus`, ngữ nghĩa `quantity` khi sửa, `side`/`status` của deal, thời hạn token, rate limit, server có chấp nhận chữ ký path-không-query.
- Stop do bot giữ: MTL phần dư thành LO (quy định sàn) ⇒ stop đã kích hoạt có thể đứng chờ; `ExecutionManager` chưa đặt lại lệnh tag `stop` như với `exit/flatten`. Bot khởi động lại giữa phiên ⇒ stop cục bộ mất, risk sẽ đòi stop mới hoặc đóng.
- Thời điểm Fill = lúc đọc sổ lệnh (chậm tối đa `poll_seconds`), không phải giờ khớp ở sàn.
- Lệnh "không rõ" khớp nhầm một lệnh đặt tay giống hệt đặt đúng trong cửa sổ đối soát — rất hiếm; khi đó vẫn đúng net position.
- OTP đi qua Telegram (mã sống ~2 phút, một mình mã không đủ để giao dịch); nên xoá tin sau khi dùng.
- Runner chưa nối chế độ live; danh sách hợp đồng REST `/instruments` vẫn chưa có.

**Người dùng cần làm**
- [ ] Điền `DNSE_ACCOUNT_NO` trong `.env`; chạy `python scripts/dnse_trading_probe.py` (chỉ đọc) ⇒ điền `brokers.dnse.loan_package_id` theo gói vay in ra.
- [ ] Trên UAT: `TRADING_MODE=live` rồi `python scripts/dnse_trading_probe.py --uat --order-test --with-modify --live`; gửi lại thư mục `data/probe/dnse_trading_*` để chốt các ❓ và điền `trading_token_ttl_seconds`. Chỉ sau đó mới cân nhắc `api_verified: true`.

**Bước tiếp theo:** P4.2 adapter SSI (broker dự phòng), hoặc chốt ❓ DNSE khi có kết quả probe rồi nối live vào runner.

## 2026-11-08 — P1.6 bước 1: thăm dò REST lịch sử (downloader CHƯA viết)

**Vì sao chưa viết `scripts/download_history.py`:** P1.6 yêu cầu gọi thử bằng tay để chốt các mục ❓ TRƯỚC khi viết code. Môi trường này không có khoá DNSE và không gọi được tới `openapi.dnse.com.vn` (proxy chặn). Đọc SDK `dnse` 0.5.0: `/price/ohlc` không có trong SDK (field hoàn toàn chưa biết); `/price/{symbol}/trades` có field nhưng đơn vị `from`/`to` và định dạng `time` chưa rõ; SDK ký đường dẫn KHÔNG kèm query, trái với ghi chú cũ. Viết downloader lúc này là đoán.

**Đã xong**
- `scripts/dnse_history_probe.py`: ký HMAC đúng như SDK; với hôm qua và 1/3/6/12 tháng trước (mỗi ngày đúng hợp đồng đang giao dịch ngày đó — trả lời "lịch sử hợp đồng đã đáo hạn lùi được bao xa"), thử `from`/`to` dạng epoch giây / mili-giây / ISO / ngày cho cả trades và OHLC; ký không-query trước, 401 thì thử kèm query; tôn trọng `Retry-After` khi 429; ghi response thô (không header/chữ ký) + `summary.json` vào `data/probe/dnse_history_<thời điểm>/`; tóm tắt nhận được list / `{"data": [...]}` / dạng cột.
- `docs/broker_notes.md` §1.3: ghi kết quả đọc SDK cho REST lịch sử + mâu thuẫn về cách ký.
- `tests/test_history_probe.py` (viết trước): 12 test — chữ ký khớp thuật toán SDK, các định dạng thời gian, tóm tắt các kiểu bọc, 401 ⇒ ký kèm query, 429 ⇒ chờ `Retry-After`, đúng hợp đồng theo ngày, không lộ khoá/chữ ký trong file hay màn hình. Thử phá 3 cơ chế chính ⇒ đều có test đỏ.
- 581 test.

**Người dùng cần làm**
- [ ] Có khoá DNSE: chạy `python scripts/dnse_history_probe.py`, gửi lại thư mục `data/probe/dnse_history_*` (không chứa khoá). Có kết quả là viết được downloader (P1.6 bước 2) ngay.

## 2026-11-08 — Luật rủi ro mới: đóng vị thế khi mất dữ liệu (golive #9, theo quyết định của người dùng)

- `risk.flatten_on_feed_silence_seconds: 30` (bắt buộc, null ⇒ từ chối chạy): đang có vị thế mà không có dữ liệu thị trường > 30s trong phiên liên tục ⇒ `FLATTEN_ALL`. Mốc tính = muộn nhất của tick cuối, đầu đoạn phiên (09:00/13:00), lúc runner khởi động ⇒ nghỉ trưa / vào phiên chiều không đóng oan. Chỉ áp cho IM LẶNG; lệch giá DNSE/SSI vẫn chỉ chặn mở vị thế.
- `AccountState.market_data_at`: runner ghi mỗi tick (và lúc khởi động), engine backtest ghi mỗi tick; engine chạy thêm một vòng kiểm khi dữ liệu thủng dài hơn ngưỡng (live kiểm mỗi giây — trước đó backtest không bao giờ thấy luật này kích hoạt; test phát hiện ra).
- CLAUDE.md §5 thêm dòng mô tả luật.
- Test viết trước: 5 test risk (ranh giới 30s, không vị thế, nghỉ trưa + đầu phiên chiều, chưa từng có dữ liệu), 1 test runner, 1 test backtest; thử phá từng điều kiện ⇒ đều có test đỏ.
- Hạn chế: ở chế độ paper, lệnh đóng chỉ khớp khi có tick trở lại (paper khớp theo tick); broker thật khớp MTL độc lập với feed của bot.
- 569 test.

## 2026-11-08 — Sửa lỗi từ checklist go-live (#1–#8, #10–#12, #14)

- Mỗi lỗi một test tái hiện trước (`tests/test_golive_fixes.py`, 15 test, tất cả đỏ trước khi sửa); thử phá từng bản sửa ⇒ đều có test đỏ (siết thêm test phí trong chuỗi thua).
- Luật risk thay đổi: lệnh GIẢM vị thế không còn bị trần "kịch bản xấu nhất" (đã khiến lệnh đối ứng khẩn cấp và lệnh flatten bị chặn); thay bằng `DUPLICATE_CLOSE`. Lệnh mở giữ nguyên trần cũ. Chuỗi thua tính sau phí.
- `ExecutionManager`: `maintain()` (đặt lại lệnh đóng kẹt, giữ stop), phát hiện đảo chiều ngoài ý muốn ⇒ đóng, vị thế không kế hoạch ⇒ đóng, không gửi lệnh tới broker mất kết nối, lỗi mạng không để lại lệnh ma, không để hai lệnh bảo vệ cùng sống.
- Runner: drain sau từng tick, gọi `maintain()`, lưu bộ đếm ngay khi đổi, chịu được lỗi vòng lặp (5 lần ⇒ kill, thoát 1). Engine backtest cũng gọi `maintain()` để hành xử như live.
- Telegram: xoá token khỏi mọi lỗi. Emergency: reset bộ đếm thử lại. Backtest: bar thiếu cột độ tin cậy ⇒ không tin được.
- Còn mở: #9 (chính sách khi feed chết mà đang có vị thế — chờ bạn quyết), #13 (chờ P4.2).
- 560 test.

## 2026-11-08 — P5.2 Checklist go-live

- `docs/golive_checklist.md`: đối chiếu từng mục CLAUDE.md + review độc lập "như người muốn bot thua tiền". Kết luận: **không chạy live, không chạy cả paper** trước khi sửa 3 lỗi CRITICAL + 4 HIGH (đã tái hiện bằng script): đối ứng khẩn cấp bị risk chặn khi có TP; lệnh đóng khớp một phần để lại vị thế không stop; lỗi mạng làm chết runner; stop + TP cùng khớp gây đảo chiều; flatten + đối ứng đóng hai lần; lộ token Telegram trong log; flatten bị chặn khi xác nhận huỷ trễ.

## 2026-11-08 — P5.1 Runner + giám sát

**Đã xong**
- `live/runner.py` — `Runner` (vòng `step()` mỗi giây, `run()` = khởi động + lặp) và `main()`:
  - khởi động: ngày nghỉ ⇒ thoát; `risk.new_day`; nạp bộ đếm trong ngày đã lưu; ĐỐI SOÁT vị thế + lệnh chờ từ broker (broker là sự thật), có vị thế sẵn ⇒ cảnh báo;
  - mỗi vòng: heartbeat khoá phiên TRƯỚC mọi việc (mất khoá ⇒ dừng ngay, mọi vòng sau cũng không gửi lệnh, thoát 3); ping broker ⇒ `broker_last_seen` TRƯỚC khi ra quyết định (sửa lỗi thứ tự: vòng lặp khựng > 10s từng làm lệnh bị từ chối oan); tick ⇒ giá đánh dấu, paper khớp, bar; sức khoẻ feed ⇒ `feed_healthy`; bar đóng theo đồng hồ ⇒ strategy; lệnh Telegram; `risk.check` ⇒ hành động; gỡ hedge khi broker chính sống lại;
  - kết thúc: kill + phẳng ⇒ 0 (quá hạn chưa phẳng ⇒ 1); sau 14:45 ⇒ 0 nếu phẳng, 1 nếu còn vị thế; báo cáo cuối ngày (lệnh, P&L sau phí, lệnh bị risk từ chối + lý do, cảnh báo) gửi Telegram + `data/reports/daily_YYYY-MM-DD.txt`;
  - `main()`: live/`--live` ⇒ kiểm hai chốt rồi từ chối vì chưa có adapter thật; paper ⇒ `check_allowed_machine` + `InstanceLock` + feed DNSE + `PaperBroker` mang tên broker chính; Telegram qua thread.
- `core/bars.py` — `BarBuilder`: đóng bar theo đồng hồ (kể cả không có tick), grace 0,5s cho tick trễ, tick trễ cho bar đã phát bị bỏ và đếm.
- `live/state_store.py`: lưu/nạp bộ đếm trong ngày (lượt, chuỗi thua, lãi/lỗ, phí, khoá ngày, kill, khẩn cấp, id lệnh); sang ngày chỉ giữ khoá tay; file hỏng ⇒ từ chối chạy.
- `monitoring/telegram.py`: `parse_commands` (chỉ nhận từ đúng chat, bỏ tin sửa) + `fetch_updates`.
- `brokers/base.py`: `ping()` (mặc định True; adapter mạng phải ghi đè).
- `brokers/paper.py`: **sửa độ lạc quan** — LO đặt khi giá chưa tới giờ nằm chờ NGAY từ lúc đặt (trước đây nếu tick kế tiếp nhảy qua giá LO thì khớp ở giá tốt hơn LO ⇒ chốt lời trong backtest đẹp hơn thật). Có test tái hiện trước khi sửa.
- `docs/runner.md`: cách chạy paper, lệnh điều khiển, mã thoát.
- Test viết trước: `tests/test_runner_parts.py` (12), `tests/test_runner.py` (18), 2 test paper mới. Thử phá từng cơ chế ⇒ đều có test đỏ (siết 2 test: vòng sau khi mất khoá vẫn không giao dịch; đối soát với state trống thật).
- 546 test.

**Giả định / rủi ro còn mở**
- Chưa chạy thật: cần khoá DNSE (feed) và điền settings; live cần adapter P4.1/P4.2.
- Feed composite chưa nối vào runner (chờ token SSI — P4.2); hiện runner paper chỉ dùng feed DNSE, nên một nguồn im là `feed_healthy=False` theo `is_stale` của feed DNSE.
- Lệnh Telegram đọc mỗi 3 giây bằng getUpdates (không webhook); mất mạng ⇒ bỏ lượt, không ảnh hưởng giao dịch. Chưa có lệnh `/unlock` (mở kill/khẩn cấp) — cố ý để người mở bằng tay.
- Runner chạy trong một ngày; qua nửa đêm không được hỗ trợ (Task Scheduler khởi động lại mỗi sáng).
- Ghi file trạng thái/báo cáo đồng bộ trong vòng lặp (nhỏ, mỗi 30 giây).

**Người dùng cần làm**
- [ ] Điền `live.allowed_machine` (hostname máy desktop) và các khoá rủi ro/phí; tạo bot Telegram riêng cho kênh trading.

## 2026-11-08 — P4.3 Quy trình khẩn cấp

**Đã xong**
- `live/emergency.py` — `EmergencyHandler`, nhận `EMERGENCY_HEDGE` từ risk (qua `ExecutionManager`):
  - khoá mở vị thế mới bằng `state.emergency_reason` (risk từ chối với `RejectCode.EMERGENCY`; KHÔNG tự mở khoá, kể cả sang ngày mới — người gọi `risk.clear_emergency` sau khi đối soát);
  - đặt MTL đối ứng trên broker dự phòng để net = 0; idempotent (`cần = −net − phần đối ứng đang chờ`) vì risk phát tín hiệu mỗi lần `check()`;
  - lệnh đối ứng chờ quá `hedge_requote_s` (5s) ⇒ huỷ và đặt lại MTL — xử lý trường hợp MTL khớp một phần, phần dư thành LO và giá chạy mất (test tái hiện: vị thế lộ một phần cho tới khi đặt lại);
  - broker dự phòng lỗi / từ chối ⇒ thử lại tối đa 3 lần với id mới rồi cảnh báo XỬ LÝ TAY; cả hai broker mất kết nối ⇒ cảnh báo "CẢ HAI BROKER LỖI" (giới hạn 1 lần/60s);
  - gỡ hedge (`unwind`) khi broker chính sống lại hoặc tới giờ flatten: huỷ lệnh chờ, đóng ĐỒNG THỜI hai chân; gọi lặp khi đang gỡ dở không huỷ/gửi trùng; broker chính vẫn chết lúc flatten ⇒ GIỮ hedge (đóng chân dự phòng sẽ lộ vị thế trần) và cảnh báo;
  - `notify` nhận hàm thường hoặc async (để nối Telegram).
- `risk/risk_manager.py`: `approve_unwind` (duyệt cả bộ lệnh gỡ khi mỗi lệnh giảm vị thế trên chính broker của nó và tổng giữ nguyên net; hỏng một ⇒ từ chối tất cả), `RejectCode.EMERGENCY` / `UNWIND_INVALID`, `clear_emergency`, `broker_alive` công khai; `AccountState.emergency_reason`.
- `core/execution.py`: chuyển `EMERGENCY_HEDGE` cho handler; đóng vị thế khi net = 0 nhưng còn vị thế chéo broker ⇒ nhờ handler gỡ; lệnh `unwind` đang chờ được coi như lệnh đóng (không bị huỷ bởi FLATTEN lặp).
- `tests/test_emergency.py` (viết trước): 21 test — mất kết nối giữa chừng, khớp một phần (cả khi giá chạy mất), broker dự phòng ném lỗi / từ chối, cả hai broker lỗi, không có vị thế, gỡ hedge, FLATTEN lặp khi đang gỡ, flatten khi broker chính còn chết, luật `approve_unwind`. Thử phá từng cơ chế ⇒ đều có test đỏ (bổ sung 2 test thiếu về gỡ hedge lặp).
- 514 test.

**Giả định / rủi ro còn mở**
- Phát hiện "mất kết nối" dựa vào `state.broker_last_seen` — runner (P5.1) phải cập nhật bằng ping/thao tác thành công với từng broker; adapter thật (P4.1/P4.2) phải có `ping`.
- Khi broker chính mất kết nối, lệnh stop của nó vẫn có thể đang nằm trên sàn và khớp mà bot không biết ⇒ sau khi đối ứng, net thật có thể lệch; khi kết nối lại PHẢI đối soát vị thế từ broker (runner P5.1) trước khi gỡ hedge.
- Đối ứng dùng MTL; trong ATO/ATC không đặt được MTL ⇒ lỗi được ghi và cảnh báo, không có đường khác.
- Giữ hedge qua đêm nếu broker chính chết tới hết phiên — vi phạm "không giữ vị thế qua đêm" theo nghĩa gộp (net = 0) nhưng an toàn hơn đóng một chân; cần người xử lý.
- Giới hạn tần suất cảnh báo theo khoá; cảnh báo thường (đã đặt đối ứng…) gửi mỗi lần xảy ra.

## 2026-11-08 — P1.5 Kiểm định dữ liệu một ngày

**Đã xong**
- `recorder/validate.py` (hàm thuần) + `scripts/validate_day.py <ngày> [--ref-price] [--reference]`:
  - mỗi nguồn: số tick (khớp/báo giá), khoảng trống > 5 giây trong phiên liên tục (tính cả đầu/cuối đoạn 09:00–11:30, 13:00–14:30), báo giá bid > ask, tick ngoài biên độ ±7% (chỉ khi có `--ref-price` — không tự đoán giá tham chiếu), giá nhảy > 1% giữa hai lần khớp;
  - **đối chiếu KL luỹ kế với tổng KL từng lần khớp** ⇒ phát hiện feed rơi message (giao thức không có số thứ tự nên đây là cách duy nhất);
  - DNSE vs SSI: mỗi giây so giá (mid nếu có, không thì giá khớp, chỉ khi cả hai còn mới) ⇒ số giây lệch > `feed_divergence_ticks`;
  - dựng bar 1 phút từ giá khớp ⇒ `data/bars/<ngày>.parquet` (schema đọc được bởi `backtest.engine --bars data/bars`);
  - so OHLC với bar tham chiếu (mặc định `data/imported/<ngày>.parquet` = vnstock-dashboard), dung sai 2 bước, liệt kê sai lệch và phút thiếu ở mỗi bên;
  - báo cáo chữ + `data/validation/<ngày>.json`; mã thoát 0/1/2.
- `scripts/record_today.py` tự chạy kiểm định sau khi gộp file cuối ngày (lỗi kiểm định không ảnh hưởng dữ liệu đã lưu).
- `tests/test_validate_day.py` (viết trước): 25 test. Thử phá từng luật ⇒ đều có test đỏ (bổ sung 2 test thiếu: nguồn chính cũ không so, lệch đúng ngưỡng không tính).
- 493 test.

**Giả định / rủi ro còn mở**
- Không dùng thư viện vnstock (lớn) — "dữ liệu vnstock" lấy từ bar đã import từ vnstock-dashboard; muốn so trực tiếp với vnstock thì cần cài thêm (hỏi trước).
- Lệch hai nguồn đo theo giây ⇒ giá chạy nhanh + độ trễ khác nhau giữa hai feed có thể cho vài giây lệch "giả"; đọc cùng số liệu độ trễ khi có dữ liệu thật.
- Kiểm KL cần `total_volume` (DNSE `te`, SSI `v`); nếu feed không gửi thì báo "không kiểm được KL".
- Giá tham chiếu phải nhập tay (`--ref-price`) tới khi adapter lấy được secdef (P4.1).

## 2026-11-08 — P1.3 Feed SSI + feed ghép (composite)

**Đã xong**
- Đọc mã nguồn SDK chính thức `ssi-sdk` 3.3.2 để chốt giao thức stream trước khi viết code; ghi vào `docs/broker_notes.md` §2.4. Điểm quan trọng: stream cần **access token Bearer lúc bắt tay** (lấy qua REST `/api/v3/auth/token`), quote có nhiều bước giá, trade có KL luỹ kế `v`.
- `feeds/data_feed_ssi.py` — `SsiFeed` (cùng interface `DataFeed`):
  - đối chiếu hợp đồng với danh sách broker trước khi kết nối; đăng ký `trade.<KRX>` + `quote.<KRX>`; `ping_pong` mỗi 30 giây;
  - chuẩn hoá frame `{channel, topic, data}` về `Tick` (`SsiNormalizer`): số dạng chuỗi `"1,234.0"`, thời gian epoch / `HH:MM:SS` / ISO, giá chữ ("ATO") và giá ngoài khoảng hợp lý bị bỏ, nến `@1m/@5m` bị bỏ, quote lấy mức tốt nhất;
  - token qua `token_provider(force_refresh)` tiêm vào; 401/403 (bắt tay hoặc frame từ chối) ⇒ lấy token mới MỘT lần, lần nữa ⇒ dừng hẳn; cờ thử-lại được xoá khi kết nối đã có dữ liệu (token hết hạn giữa phiên không làm feed chết — lỗi này được test tái hiện trước rồi mới sửa); 429 ⇒ chờ max(30s, retryAfter); ack đăng ký lỗi ⇒ dừng hẳn; backoff lũy thừa có jitter, chặn 60 giây.
- `feeds/composite.py` — `CompositeFeed(DNSE chính, SSI dự phòng)`:
  - `on_tick` chỉ phát tick của nguồn đang dùng; `on_any_tick` phát mọi nguồn (recorder ghi cả hai);
  - `healthy` = cả hai nguồn có tick mới, không nguồn nào chết, không lệch giá quá `feed_divergence_ticks` (so mid nếu có, không thì giá khớp, chỉ so giá còn mới) ⇒ runner gán vào `AccountState.feed_healthy` để risk chặn mở vị thế;
  - DNSE im > `feed_stale_seconds` mà SSI sống ⇒ chuyển sang SSI; DNSE sống lại liên tục `switch_back_after_seconds` (30s) ⇒ chuyển về; SSI đang dùng mà chết ⇒ về ngay; cả hai im ⇒ không chuyển;
  - không báo động giả lúc khởi động và ngay sau nghỉ trưa (im lặng tính từ mốc muộn nhất: tick cuối / lúc bắt đầu theo dõi / 09:00 hoặc 13:00), nhưng `healthy` vẫn False tới khi cả hai có dữ liệu;
  - cảnh báo chỉ phát khi trạng thái đổi (`STALE`, `RECOVERED`, `DIVERGENCE`, `DIVERGENCE_CLEARED`, `SWITCHED`, `SWITCHED_BACK`, `FEED_DOWN`), `on_alert` để nối Telegram; một feed chết thì composite chạy tiếp với feed kia, cả hai chết ⇒ ném lỗi.
- `settings.feeds.switch_back_after_seconds: 30`; `.env.example`: biến SSI V3 (`SSI_CLIENT_ID`, `SSI_API_KEY`, `SSI_API_SECRET`).
- Test viết trước: `tests/test_feed_ssi.py` (27, gồm 1 test WebSocket thật với server cục bộ kiểm header Bearer và mã 401) và `tests/test_feed_composite.py` (24). Thử phá từng luật ⇒ đều có test đỏ (phát hiện và bổ sung 4 test thiếu: không chuyển sang nguồn đã chết, feed chết là không khoẻ ngay, nến có trường giá vẫn bị bỏ, jitter backoff).
- 472 test.

**Giả định / rủi ro còn mở**
- `token_provider` thật cho SSI chưa có — thuộc adapter SSI (P4.2), phải tuân luật refresh token CHỈ DÙNG MỘT LẦN. Chưa rõ server có cấp token chỉ-dữ-liệu không cần OTP (nếu cần OTP mỗi ngày thì feed dự phòng phụ thuộc thao tác tay buổi sáng).
- Frame mẫu SSI dựng theo SDK, chưa phải bản ghi thật: định dạng `t`, đơn vị giá phái sinh, số bước giá còn ❓.
- Khi chuyển nguồn, tick từ hai feed có thể lệch thời gian/giá một chút; strategy dựng bar từ nguồn đang dùng — bar ở phút chuyển nguồn có thể pha trộn hai nguồn.
- So lệch giá dùng mid của mức tốt nhất; khi một bên chỉ có giá khớp, bên kia có mid, so sánh khác loại có thể báo lệch giả lúc thị trường mỏng (ngưỡng 3 bước giảm rủi ro này).
- `scripts/record_today.py` vẫn chỉ dùng feed DNSE; chuyển sang composite khi có `token_provider` SSI.

**Người dùng cần làm**
- [ ] Đăng ký SSI FastConnect V3, điền các biến SSI trong `.env`; khi đăng ký hỏi rõ: lấy token stream dữ liệu có cần OTP không.

**Bước tiếp theo:** P1.5 (kiểm định dữ liệu một ngày — đã có recorder ghi cả hai nguồn để đối chiếu), hoặc P4.1/P4.2 khi có khoá (adapter + lấy danh sách hợp đồng + token SSI).

## 2026-11-08 — P1.4 Recorder

**Đã xong**
- `recorder/tick_recorder.py` — `TickRecorder`: nhận `Tick` từ bất kỳ `DataFeed` nào (hiện là DNSE; composite P1.3 cắm vào được ngay), ghi `data/raw/YYYY-MM-DD/`:
  - mỗi `recorder.flush_seconds` (5 giây) hoặc khi đệm đủ `max_buffer_rows` ⇒ ghi MỘT file `part-<phiên>-<n>.parquet` MỚI bằng tmp-rồi-tráo; không bao giờ ghi đè/nối file cũ ⇒ crash chỉ mất ≤ 5 giây trong RAM, không có file hỏng; ghi lỗi ⇒ giữ nguyên đệm, lần sau thử lại;
  - cuối ngày `compact_day` gộp part → `ticks.parquet` (ghi bản gộp xong mới xoá part; gộp lại nhiều lần không trùng; khử trùng theo id ghi `session`+`rx_seq`, không theo nội dung — hai lần khớp y hệt vẫn giữ cả hai);
  - mỗi dòng: mọi trường `Tick` (gồm bid/ask/khối lượng mức 1 = snapshot sổ lệnh) + `recv_ts` (giờ bot nhận, đo độ trễ) + id ghi;
  - `load_ticks(ngày)` đọc bản gộp + part chưa gộp, theo thời gian ⇒ dùng thẳng cho `backtest.engine.bars_from_ticks` / `run_events`;
  - từ chối thư mục dữ liệu nằm trong Dropbox/OneDrive/Google Drive/iCloud (CLAUDE.md mục 3b).
- `scripts/record_today.py`: bỏ qua ngày nghỉ; chờ tới 08:30; ghi tới 15:00, flush + gộp rồi thoát; feed chết ⇒ vẫn lưu phần đã nhận, thoát mã 1; `InstanceLock` riêng (`state/recorder.lock`) chặn hai recorder trên một máy; cảnh báo khi feed im lặng trong phiên; log ra `logs/record_YYYY-MM-DD.log`. Mã hợp đồng: `--krx` hoặc `--assume-computed-contract` (chưa có REST danh sách hợp đồng — P4.1).
- `docs/recorder_windows.md`: hướng dẫn Task Scheduler (lệnh `schtasks`, thiết lập Conditions/Settings, ý nghĩa mã thoát, kiểm tra hằng ngày).
- `settings.recorder`: `flush_seconds: 5`, `max_buffer_rows: 5000`, `start: "08:30"`, `stop: "15:00"`.
- `tests/test_recorder.py` (viết trước): 31 test — roundtrip đủ trường, mỗi flush một file, đệm đầy, ghi lỗi giữ đệm, crash chỉ mất phần chưa flush, gộp idempotent, gộp bị ngắt không nhân đôi, ghi bản gộp lỗi thì part còn nguyên, khởi động lại cùng ngày, chặn thư mục đồng bộ, lịch chạy, phiên ghi khi feed chết. Thử phá từng cơ chế ⇒ đều có test đỏ (phát hiện thiếu test "ghi bản gộp lỗi" và đã bổ sung).
- 422 test.

**Giả định / rủi ro còn mở**
- Ghi Parquet chạy đồng bộ trong event loop (file nhỏ mỗi 5 giây, ước vài ms); nếu đĩa chậm có thể trễ xử lý tick vài chục ms — đo khi chạy thật, cần thì chuyển sang thread.
- Ghi tick theo ngày của đồng hồ bot (không theo `ts` của sàn) — đủ dùng vì phiên không qua nửa đêm.
- Chỉ có sổ lệnh mức 1 (giới hạn của feed DNSE). Chưa ghi message thô — nếu cần soi lỗi chuẩn hoá thì dùng `scripts/dnse_feed_probe.py`.
- `--assume-computed-contract` chưa đối chiếu danh sách broker (chấp nhận được vì chỉ ghi dữ liệu); sai mã ⇒ feed im lặng và log cảnh báo.
- Backtest CLI (`python -m backtest.engine`) mới đọc bar; đọc tick đã ghi thì dùng `load_ticks` + `run_events` trong Python — thêm cờ `--ticks` khi cần.

**Người dùng cần làm**
- [ ] Đặt `paths.data_dir` / `paths.state_dir` ra ngoài Dropbox (ví dụ `C:/vnbot/data`).
- [ ] Khi có khoá DNSE: chạy thử tay rồi tạo task theo `docs/recorder_windows.md` — càng sớm càng có dữ liệu tick thật cho backtest.

**Bước tiếp theo:** P1.3 (feed SSI + composite: recorder ghi được cả hai nguồn để đối chiếu) hoặc P1.5 (kiểm định dữ liệu một ngày).

## 2026-11-08 — P1.2 Feed DNSE

**Đã xong**
- Đọc mã nguồn SDK chính thức `dnse` 0.5.0 (PyPI) để chốt giao thức WebSocket trước khi viết code; ghi vào `docs/broker_notes.md` §1.4 (định dạng subscribe, lời chào, không có số thứ tự, SDK chưa có `/instruments`).
- `feeds/base.py`: interface `DataFeed` (`on_tick`, `run`, `stop`, `is_stale`, `stats`, `last_message_at`).
- `feeds/data_feed_dnse.py` — `DnseFeed`:
  - khởi động: tính hợp đồng giao dịch hôm nay (F1M, có roll) → mã KRX → **bắt buộc có trong danh sách broker trả về** (`contract_lister` tiêm vào), không có ⇒ từ chối kết nối;
  - lời chào → xác thực HMAC → đăng ký `tick.G1.json`; trả pong khi server ping, tự ping mỗi 30 giây;
  - chuẩn hoá về `Tick` (`DnseNormalizer`): trade/te/quote, nhận cả camelCase lẫn snake_case, tự nhận đơn vị timestamp (s/ms/µs/ns) → giờ VN, tick khớp lệnh mang giá tốt nhất gần nhất; giá ngoài [300, 5000] điểm bị bỏ và đếm (không tự chia 1000);
  - kết nối lại vô hạn, backoff 1s → 60s, có dữ liệu thì backoff về 1s; kết nối im lặng quá `recv_timeout_s` ⇒ kết nối lại; sai khoá (`auth_error`) ⇒ dừng hẳn, không thử lại;
  - `is_stale()` theo `feeds.feed_stale_seconds`, chỉ tính trong phiên liên tục; log số message/phút; lỗi ở bộ nhận tick không làm chết feed; không log/repr khoá hay chữ ký.
- `scripts/dnse_feed_probe.py`: chạy với khoá thật, in tick, ghi message thô ra `data/probe/` để chốt các mục ❓ và thay fixture mẫu.
- `.env.example`: DNSE V2 dùng `DNSE_API_KEY` / `DNSE_API_SECRET` (bỏ username/password kiểu cũ).
- Thêm thư viện `websockets>=12.0` (nhỏ, thuần Python, chính SDK DNSE dùng) — nhập muộn, chỉ cần khi kết nối thật.
- `tests/test_feed_dnse.py` (viết trước): 33 test với message mẫu `tests/fixtures/dnse_stream.json` + transport giả; 1 test chạy qua WebSocket thật với server giả lập cục bộ. Thử phá từng cơ chế (reset backoff, đối chiếu hợp đồng, chặn giá lạ, ngưỡng im lặng, auth lỗi là cố định, pong, cô lập lỗi callback, timeout nhận, gắn báo giá vào trade) ⇒ đều có test đỏ.
- 391 test.

**Giả định / rủi ro còn mở**
- Message mẫu dựng theo model SDK, **chưa phải bản ghi server thật** — tên field, đơn vị timestamp/giá, định dạng phản hồi xác thực đều ❓ tới khi chạy `scripts/dnse_feed_probe.py`.
- Chỉ có giá tốt nhất (bid/ask 1 mức); kênh sổ lệnh nhiều bước giá không có trong SDK.
- Không có số thứ tự message ⇒ mất message lẻ trong một kết nối KHÔNG phát hiện được; chỉ phát hiện im lặng/rớt kết nối. Recorder/validate_day (P1.4/P1.5) nên đối chiếu `total_volume` của `te` để phát hiện hụt khớp lệnh.
- Hợp đồng được chọn lúc `run()`; không tự đổi hợp đồng giữa phiên (roll xảy ra theo ngày; chạy lại mỗi ngày).
- `contract_lister` thật (REST `/instruments` ký HMAC) chưa viết — thuộc adapter DNSE (P4.1); script thăm dò dùng mã KRX bạn tự xác nhận.
- Tick khớp lệnh mang giá tốt nhất GẦN NHẤT, có thể đã cũ vài trăm ms so với lúc khớp.

**Người dùng cần làm**
- [ ] Đăng ký DNSE Open API V2, điền `DNSE_API_KEY` / `DNSE_API_SECRET` vào `.env`.
- [ ] Trong giờ giao dịch chạy `python scripts/dnse_feed_probe.py --krx <mã KRX F1M> --seconds 120`, gửi lại file `data/probe/dnse_raw_*.jsonl` (không chứa khoá) để chốt các mục ❓.
- [ ] `pip install -r requirements.txt` (thêm `websockets`).

**Bước tiếp theo:** P1.3 (feed SSI + composite) hoặc P1.4 (recorder) — cả hai dùng được ngay interface `DataFeed`.

## 2026-11-08 — P3.3 Kiểm định độ bền (công cụ xong; CHƯA kết luận được)

**Kết luận về edge của ORB: CHƯA CÓ BẰNG CHỨNG — coi như KHÔNG có edge cho tới khi chứng minh được.** Lý do: không có dữ liệu bar thật trong môi trường làm việc, `fees.*`/`risk.*` còn null ⇒ engine từ chối chạy. Không có con số nào để làm bằng chứng.

**Đã xong**
- `backtest/robustness.py`: một lần chạy gồm cấu hình gốc ⇒ IS/OOS theo `--split`; walk-forward theo `folds` đoạn ngày; độ nhạy ±20% từng tham số (số nguyên lệch ≥ 1 bước) **chỉ trên in-sample**; cấu hình gốc với trượt giá 2 tick; `verdict()` theo luật cố định `VerdictRules` ⇒ CÓ / KHÔNG / CHƯA ĐỦ BẰNG CHỨNG; HTML; CLI `python -m backtest.robustness --bars … --split …` (`--n-trials` mặc định đếm từ `research/log.md`).
- Luật kết luận chốt trong code + test TRƯỚC khi có dữ liệu (xem bảng trong `research/orb_baseline.md`).
- `tests/test_robustness.py` (viết trước): 21 test — edge cài sẵn trên dữ liệu giả ⇒ CÓ; random walk ⇒ không ra CÓ; dữ liệu snapshot ⇒ CHƯA ĐỦ; từng nhánh luật; độ nhạy không chạm OOS; trượt 2 tick làm giảm lãi. Thử phá từng điều kiện ⇒ đều có test đỏ.
- `backtest/report.py`: `make_section`, `render_section` thành public để dùng lại.
- 358 test.

**Giả định / rủi ro còn mở**
- Ngưỡng luật (≥ 30 lệnh OOS, ≥ 75% biến thể lãi, ≥ 50% đoạn lãi, p × n_trials < 0,10) là lựa chọn thận trọng của tôi, chưa được bạn duyệt — nếu muốn đổi, đổi **trước** khi chạy dữ liệu thật.
- Walk-forward ở đây là đánh giá cấu hình cố định theo từng đoạn (không tối ưu lại từng đoạn) — đúng với baseline không tune; nếu sau này có bước tối ưu thì cần walk-forward optimize-rồi-test thật.
- Độ nhạy chỉ ±20% một tham số mỗi lần, không xét tương tác giữa các tham số.
- Mọi hạn chế của tick giả từ bar (P3.1) vẫn áp dụng; kết luận CÓ trên bar vẫn phải qua paper trade.

**Người dùng cần làm**
- [ ] Điền `fees.*` và `risk.*` còn null.
- [ ] Có dữ liệu thật: cấp API key DNSE/SSI để làm P1.6 (dữ liệu import từ dashboard chỉ cho ra CHƯA ĐỦ BẰNG CHỨNG theo luật).
- [ ] Chốt ngày chia OOS và ghi vào `research/log.md` trước khi chạy.
- [ ] Duyệt (hoặc sửa) ngưỡng trong `VerdictRules` trước khi chạy.

**Bước tiếp theo:** P1.6 (dữ liệu lịch sử thật) là điểm nghẽn của cả P3.2/P3.3 — cần API key. Song song có thể làm P1.2–P1.4 (feed + recorder) hoặc P4.x sau khi có key.

## 2026-11-08 — P3.2 Chiến lược baseline ORB (chưa chạy in-sample)

**Đã xong**
- `strategy/baseline_orb.py` — `OpeningRangeBreakout`: biên = đỉnh/đáy các bar mở trong 15 phút đầu phiên liên tục (đủ bar, thiếu ⇒ bỏ ngày, bỏ qua bar ATO); bar 1 phút đóng ngoài biên khi đang phẳng ⇒ vào; stop cạnh đối diện; TP 1,5R (tròn bước giá); ≤ 2 tín hiệu/ngày. Tham số ở `settings.strategy.orb`. Không I/O, không đọc giờ (có test quét mã nguồn).
- `tests/test_strategy_orb.py` (viết trước): 28 test, gồm test qua toàn pipeline engine (vào ở tick mở 09:16, chốt lời đúng 1,5R). Thử phá từng quy tắc (ngưỡng đóng ngoài biên, cạnh stop, giới hạn 2 lệnh, đủ bar, bỏ bar ATO, chỉ vào khi phẳng) ⇒ đều có test đỏ.
- CLI backtest báo lỗi một dòng khi settings còn null (thay vì traceback).
- `research/orb_baseline.md`: đặc tả, cách chạy in-sample, **5 lý do kết quả có thể đẹp hơn thực tế**. `research/log.md`: ghi cấu hình #1 (n_trials = 1).
- 337 test.

**Chưa làm được: chạy backtest in-sample** — container không có dữ liệu bar (`data/` không commit), DNSE MCP chỉ có OHLC ngày, endpoint biểu đồ công khai bị chặn mạng (và không phải API chính thức), `fees.*`/`risk.*` còn null ⇒ engine từ chối chạy đúng thiết kế. Lệnh chạy sẵn trong `research/orb_baseline.md`.

**Giả định / rủi ro còn mở**
- Đặc tả lấy theo ví dụ trong PROMPTS P3.2; chi tiết tự chọn: đóng **đúng bằng** cạnh biên không tính là phá; tín hiệu bị risk từ chối vẫn tính vào giới hạn 2 lệnh; sau khi chốt lời mà giá vẫn ngoài biên thì bar kế tiếp vào lại cùng chiều (theo đúng câu chữ "đóng ngoài biên"), sau khi dừng lỗ có thể vào ngược chiều nếu bar đóng ngoài cạnh kia.
- R tính từ giá đóng bar tín hiệu, không từ giá khớp thật ⇒ TP thực tế < 1,5R khi trượt giá.
- Chưa có lọc biên quá hẹp/quá rộng — biên hẹp ⇒ R nhỏ, phí chiếm tỷ trọng lớn.

**Người dùng cần làm**
- [ ] Điền `fees.*`, `risk.max_daily_loss_points`, `risk.max_trades_per_day`, `risk.max_consecutive_losses`.
- [ ] Có dữ liệu: chạy `scripts/import_dashboard_bars.py` (chỉ thử nhanh) hoặc cấp API key để làm P1.6.
- [ ] Chốt khoảng ngày in-sample trước khi chạy, ghi vào `research/log.md`.

**Bước tiếp theo:** chạy in-sample khi có dữ liệu; sau đó P3.3 (out-of-sample, walk-forward, độ nhạy ±20%, trượt 2 tick).

## 2026-11-08 — P3.1 Engine backtest

**Đã xong**
- `strategy/base.py`: `Strategy.on_bar(bar, ctx) -> [Signal]`, `StrategyContext(position_qty, avg_price, bars_today)`. Strategy chỉ thấy bar đã đóng.
- `core/execution.py` — `ExecutionManager`, **dùng chung cho backtest và live**: Signal → lệnh MTL qua `risk.approve` → broker; sau mỗi lần khớp tự đặt/cập nhật STOP + LO chốt lời đúng |net|, net về 0 thì huỷ; EXIT / ENTER ngược chiều ⇒ huỷ lệnh chờ rồi đóng; xử lý `FLATTEN_ALL` / `FLATTEN_SYMBOL` / `KILL` (`EMERGENCY_HEDGE` tạm thời chỉ đóng trên chính broker — P4.3).
- `backtest/engine.py`:
  - pipeline giống live: nguồn sự kiện → strategy → ExecutionManager → RiskManager → PaperBroker → `risk.check`;
  - hai nguồn: `run_bars` (bar → tick giả) và `run_events` (tick thật; `bars_from_ticks` dựng bar 1 phút);
  - chống look-ahead: sự kiện lùi thời gian ⇒ `ValueError`; bar chỉ phát lúc đóng; lệnh khớp ở tick sau; `detect_lookahead()` lật ngược dữ liệu sau một mốc rồi so tín hiệu trước mốc — chiến lược "gian lận" nhìn bar kế tiếp bị bắt, chiến lược trung thực qua;
  - tick giả bi quan: bid = giá, ask = giá + `synthetic_spread_ticks`; đường đi theo `bar_high_first`, không quyết được ⇒ về phía bất lợi cho vị thế + lệnh vào đang chờ; cực trị lặp hai tick ⇒ stop khớp tại cực trị, LO chỉ khớp khi xuyên giá;
  - chỉ đưa vào hợp đồng đang giao dịch theo ngày (`trading_contract`, có roll); cảnh báo vị thế còn mở cuối ngày;
  - `load_bars_parquet` đọc schema `data/imported/`; CLI `python -m backtest.engine --bars data/imported --strategy module:Class --split YYYY-MM-DD --n-trials N --out reports/x.html`.
- `backtest/report.py`: `TradeBook` (ghép fill thành lệnh khứ hồi, lý do thoát theo tag), `compute_metrics` (số lệnh, win rate, avg win/loss, PF, max DD, P&L sau phí VNĐ/điểm, theo giờ, theo thứ), `build_report` (toàn bộ / in-sample / out-of-sample, mỗi phần có `validate_trades` + `apply_selection_penalty(n_trials)`), HTML có cảnh báo dữ liệu snapshot ("không dùng để kết luận có edge") và "tick giả", danh sách lệnh bị risk từ chối.
- `utils/atomic_io.write_text_atomic`; `settings.backtest.synthetic_spread_ticks: 1`; `.gitignore` thêm `reports/`.
- 309 test (28 mới). Thử phá từng cơ chế (thứ tự thời gian, tick lặp ở cực trị, chiều bất lợi, lật dữ liệu, lọc hợp đồng, huỷ bảo vệ, phí, drawdown) thì đều có test đỏ. 13 ngày × 240 bar chạy ~0,25 giây.
- Kiểm chứng sơ bộ: chiến lược momentum trên random walk lỗ sau phí (p = 1.0, walk-forward 0/4 đoạn dương) — đúng kỳ vọng "dữ liệu ngẫu nhiên thì không có edge".

**Giả định / rủi ro còn mở**
- Tick giả **bi quan có chủ ý**: stop khớp tại đáy/đỉnh bar − trượt (thường tệ hơn thực tế), thanh khoản vô hạn, spread 1 bước. Kết quả trên bar chỉ là cận dưới thô; kết luận phải dựa trên dữ liệu tick thật (P1.4/P1.6).
- Dữ liệu `data/imported/` là snapshot ~2 giây, volume không tin được ⇒ báo cáo tự cảnh báo; không dùng để kết luận edge.
- `OrderType.STOP` mô phỏng stop phía sàn; nếu P4 cho thấy stop phải do bot tự giữ, cần thêm độ trễ phát hiện + gửi MTL vào mô phỏng.
- Bảo vệ sửa bằng huỷ-rồi-đặt lại ⇒ có khoảng hở ngắn (risk `stop_grace_seconds` che); live có thể cần `modify` thay vì huỷ.
- Lệnh đóng bị broker từ chối (vd MTL lúc nghỉ trưa) chỉ được ghi lại, không tự thử lại; FLATTEN của risk sẽ đặt lại ở lần `check()` sau.
- `ExecutionManager` nằm ở `core/` nhưng phụ thuộc `risk/` và `brokers/` (chấp nhận để live và backtest dùng chung một chỗ).
- `detect_lookahead` chỉ bắt rò rỉ qua dữ liệu đưa vào engine; vẫn phải đọc code chiến lược.
- ~~Chưa dùng `backtest.initial_capital_vnd`~~ ⇒ báo cáo có % vốn (08/11).
- ~~Định dạng Parquet tick chưa có loader~~ ⇒ `backtest/ticks.py` + `--ticks` (08/11).

**Người dùng cần làm**
- [ ] Điền `fees.*` và `risk.*` còn null — engine từ chối chạy khi còn thiếu.
- [ ] Chạy `scripts/import_dashboard_bars.py` nếu chưa (archive dashboard tự xoá sau 30 ngày).

**Bước tiếp theo:** P3.2 — mô tả chiến lược baseline bằng lời (khung `[...]` trong PROMPTS.md).

## 2026-11-07 (khuya, tiếp) — P2.2 Paper broker

**Đã xong**
- `brokers/base.py`: `BrokerAdapter` (async `place` / `cancel` / `modify` / `positions` / `open_orders`, callback `on_fill` / `on_order_update`, `connect` / `close`), `BrokerError`, `assert_live_trading_allowed(live_flag)` — chỉ qua khi `TRADING_MODE=live` **và** có cờ `--live`; adapter thật gọi ngay trước mỗi request lệnh.
- `brokers/paper.py` (`PaperBroker`, `PaperConfig`) — dùng chung cho backtest và paper:
  - khớp ở tick **kế tiếp** (`ts >` lúc đặt và `≥` lúc đặt + độ trễ); mua khớp ask, bán khớp bid, cộng `slippage_ticks`;
  - khối lượng giới hạn theo `ask_qty`/`bid_qty` (chia theo thứ tự đặt); MTL khớp một phần ⇒ phần dư thành LO giá khớp ± 1 bước (quy tắc sau KRX);
  - LO khớp ngay được ⇒ không bao giờ tệ hơn giá LO; LO nằm chờ chỉ khớp khi giá **xuyên qua** (chạm chưa đủ);
  - `OrderType.STOP` mô phỏng: kích hoạt bằng bid (bán) / ask (mua), khớp ở tick sau ⇒ giá nhảy cóc khớp ở giá nhảy cóc;
  - độ trễ `latency_ms` + jitter có seed; phí = số HĐ × tổng `fees.*` một chiều (null ⇒ từ chối chạy);
  - idempotent theo `client_order_id`; sửa lệnh cấp `broker_order_id` mới và tính lại độ trễ (như DNSE huỷ-đặt);
  - có `session`: từ chối đặt khi đóng cửa, MTL ngoài phiên liên tục, huỷ/sửa trong ATO/ATC; chỉ khớp trong phiên liên tục.
- `core/models.py`: `OrderType.STOP`, `Order.reject_reason`. `config/settings.yaml`: mục `paper` (latency 300 ms, jitter 200 ms, seed 0).
- Test nối paper ↔ risk manager: lệnh vào khớp → stop được xác nhận → `check()` không còn yêu cầu đóng vị thế.
- 281 test (54 mới); thử phá từng quy tắc khớp thì đều có test đỏ.

**Giả định / rủi ro còn mở**
- DNSE/SSI không có lệnh stop trong API lệnh thường (❓ đã ghi `docs/broker_notes.md` §4) ⇒ stop có thể phải do bot tự giữ; khi đó backtest với `OrderType.STOP` có thể lạc quan hơn thực tế (độ trễ bot theo dõi giá + gửi MTL). P4 phải thống nhất cách giả lập.
- Không mô phỏng: khớp định kỳ ATO/ATC, MOK/MAK, ký quỹ, biên độ ±7%, hàng chờ theo thứ tự thời gian (dùng luật "xuyên giá" thay thế), tác động giá của chính lệnh mình.
- Tick thiếu bid/ask bị bỏ qua ⇒ dữ liệu chỉ có bar/last (vd import từ dashboard) cần engine P3.1 dựng bid/ask giả định kèm spread — phải ghi rõ giả định đó trong báo cáo.
- Phí thuế đang tính cố định theo HĐ (`tax_vnd_per_contract`); nếu thuế tính theo giá trị giao dịch thì phải đổi công thức.
- Tên broker của paper mặc định `"paper"`; khi chạy paper trading, `brokers.primary` của risk manager phải trùng tên này (runner P5.1 lo).
- Độ trễ 300 ± 200 ms là giá trị tạm, chưa đo.

**Người dùng cần làm**
- [ ] Điền `fees.*` (paper/backtest từ chối chạy khi còn null) và xem lại `paper.latency_ms`.
- [ ] Khi đăng ký API: hỏi DNSE/SSI có lệnh điều kiện (stop) cho phái sinh qua API không.

**Bước tiếp theo:** P3.1 (engine backtest) — cần dữ liệu (P1.6 hoặc dữ liệu import) để chạy thật; hoặc P1.2 nếu đã có API key.

## 2026-11-07 (khuya) — P2.1 Risk manager

**Đã xong**
- `risk/risk_manager.py` (test viết trước — `tests/test_risk_manager.py`, 105 test, mỗi luật mục 5 một lớp test có ca biên; thử phá từng ngưỡng `>=`/`>` thì test bắt được):
  - `approve(order, state) -> Approved | Rejected(code, reason)` — `RejectCode` cố định để log/thống kê, `reason` tiếng Việt.
  - `check(state) -> [RiskAction]`: `KILL`, `FLATTEN_ALL` (lỗ ngày / giờ flatten), `FLATTEN_SYMBOL` (thiếu stop quá `stop_grace_seconds`), `EMERGENCY_HEDGE` (broker im > `broker_timeout_seconds` khi có vị thế). Risk manager không tự gửi lệnh.
  - `record_fill`, `on_order_update`, `new_day`, `daily_pnl_points`, `kill` / `clear_kill`, `OrderIdFactory` (id ≤ 20 ký tự).
- Luật: `max_contracts` theo net **mọi broker** + lệnh chờ cùng chiều (kịch bản xấu nhất); lỗ ngày gồm lỗ chưa chốt và phí ⇒ khoá đến hết ngày; `max_trades_per_day`; `max_consecutive_losses`; chỉ mở vị thế trong phiên liên tục, sau `no_entry_open_minutes`, trước `last_entry_time`, không vào ngày đáo hạn; flatten từ `flatten_time`; lệnh mở bắt buộc `stop_loss` đúng phía; id trùng / lệnh mở thứ hai khi lệnh đầu còn chờ bị chặn; feed không khoẻ, broker mất kết nối, broker không phải broker chính ⇒ chặn mở; file `KILL` hoặc `/kill`.
- Lệnh **giảm** vị thế (theo net, không theo tag) luôn được duyệt kể cả khi khoá/kill/ngoài giờ — chỉ kiểm id trùng và trần hợp đồng.
- `core/models.py`: `Order.stop_loss`; `AccountState` thêm `trading_day`, `working_orders`, `approved_order_ids`, `last_prices`, `broker_last_seen`, `feed_healthy`, `fees_points_today`, `kill_reason`, `round_trip_points`, `unprotected_since`.
- `config/settings.yaml`: thêm `risk.stop_grace_seconds: 5`. `RiskLimits.from_settings` từ chối chạy khi còn null và kiểm `last_entry_time < flatten_time < atc_start`.
- 227 test.

**Giả định / rủi ro còn mở**
- "Có stop" = lệnh `tag="stop"` ngược chiều mà broker **đã xác nhận** (PENDING/PARTIALLY_FILLED). Chưa biết DNSE/SSI có lệnh điều kiện (stop) cho phái sinh qua API không (❓ P4) — nếu stop do bot tự giữ thì P4 phải quyết cách biểu diễn để luật này vẫn đúng.
- ~~Chuỗi thua tính theo điểm trước phí~~ ⇒ đã đổi sang SAU phí (phí mở + phí đóng) ở golive #11, test `test_11_consecutive_losses_count_fees`; lượt hoà không đổi bộ đếm; đặt lại mỗi ngày (`new_day`). ~~Lượt bị đóng bằng lệnh đối ứng ⇒ tính như hoà~~ ⇒ nay luôn tính là THUA (09/11).
- Thiếu giá đánh dấu cho vị thế đang mở: chặn mở lệnh mới, nhưng `check()` bỏ qua luật lỗ ngày (không tính được) — runner phải cảnh báo khi feed mất giá.
- `AccountState` nằm trong bộ nhớ; P5.1 phải lưu/đối soát khi khởi động lại (mất `approved_order_ids` ⇒ hai lần chạy trong cùng một giây có thể sinh id trùng; InstanceLock giảm rủi ro này).
- Ngày đáo hạn chặn mở vị thế với **mọi** hợp đồng (kể cả F2M sau roll) — thận trọng có chủ ý.
- Chưa kiểm: ký quỹ, biên độ ±7%, bội số bước giá của giá LO (broker sẽ từ chối; có thể thêm khi làm adapter).
- `no_entry_open_minutes` chỉ áp dụng đầu phiên sáng (theo comment settings).

**Người dùng cần làm**
- [ ] Điền `risk.max_daily_loss_points`, `risk.max_trades_per_day`, `risk.max_consecutive_losses`; xem lại `risk.stop_grace_seconds`.

**Bước tiếp theo:** P2.2 (paper broker).

## 2026-11-07 (tối) — P1.1 Ghi chú tài liệu broker

**Đã xong**
- `docs/broker_notes.md`: quy định VN30F sau KRX, DNSE Open API V2 (đọc từ đặc tả OpenAPI + mã nguồn SDK `dnse-tech/dnse-py`), SSI FastConnect V3 (mã nguồn `ssi-sdk` 3.3.2). Mỗi mục ghi nguồn; chỗ chưa chắc đánh ❓.
- Xác minh bảng mã KRX bằng hai nguồn độc lập (MBS, Vietstock) ⇒ `core/contracts.py` đúng; thêm test với mã thật `41I1F6000`, `41I1G9000`, `41I1G4000` và ngày GDCC 17/09/2026.
- `Order.client_order_id` giới hạn 1–20 ký tự (khoá idempotency của SSI).
- CLAUDE.md mục 3 cập nhật cách xác thực mới của DNSE V2 (HMAC + trading-token) và quy tắc refresh token một lần của SSI.
- PROMPTS: thêm **P1.6** tải dữ liệu lịch sử thật (DNSE có API lịch sử khớp lệnh từng tick và OHLC 1 phút).
- 122 test.

**Phát hiện quan trọng**
- DNSE đã chuyển sang Open API V2; tài liệu "trước KRX" (JWT + MQTT) là bản cũ.
- Đặt lệnh DNSE **không có khoá idempotency** ⇒ adapter không được tự retry lệnh đặt.
- Không huỷ được lệnh trong ATO/ATC; MTL phần dư thành LO ở giá khớp ± 1 bước.
- Giá thanh toán cuối = trung bình VN30 30 phút cuối ngày GDCC ⇒ giữ mặc định không giao dịch ngày đáo hạn.

**Người dùng cần làm** (thêm vào danh sách bên dưới)
- [ ] Đăng ký DNSE LightSpeed V2 tại developers.dnse.com.vn (lấy `api_key`/`api_secret`, đăng ký email OTP riêng) và SSI FastConnect V3.
- [ ] Khi có key: xác nhận các mục ❓ ở `docs/broker_notes.md` §4.

**Bước tiếp theo:** P1.2 (feed DNSE) — hoặc P1.6 trước nếu đã có API key, để có dữ liệu thật cho backtest.

## 2026-11-07 — Phiên khởi tạo (P0.1, P0.2, P0.3, P1.0)

**Đã xong**
- Khung repo đúng CLAUDE.md mục 2, `config/settings.yaml` (giá trị chưa biết để `null`), `config/holidays.yaml`.
- `core/models.py`: Tick, Bar, Signal, Order, Fill, Position (frozen; `Position.apply` tính giá vốn, lãi chốt, đảo chiều), AccountState (`net_qty` cộng mọi broker).
- `core/calendar.py`, `core/clock.py`: trạng thái phiên theo settings, FakeClock cho backtest, chuyển múi giờ.
- `core/contracts.py`: mã KRX ↔ VN30FYYMM, đáo hạn lùi theo ngày nghỉ, F1M/F2M/F1Q/F2Q theo ngày, hợp đồng giao dịch sau roll.
- Port từ vnstock-dashboard: `utils/atomic_io.py`, `backtest/validation.py`, `monitoring/telegram.py`.
- `live/instance_lock.py`: `check_allowed_machine` (chặn chéo hai máy) + `InstanceLock` (chặn tiến trình thứ hai, heartbeat, phát hiện bị chiếm khoá).
- `scripts/import_dashboard_bars.py` (cứu bar VN30F), `scripts/check_calendar.py`.
- 120 test, CI GitHub Actions.

**Người dùng cần làm**
- [ ] Chạy `import_dashboard_bars.py` trên máy có `C:\vnstock\data_warehouse` (càng sớm càng tốt — archive tự xoá sau 30 ngày).
- [ ] Điền `fees.*`, `risk.*`, `backtest.initial_capital_vnd`, `live.allowed_machine` (`python -m core.settings`).
- [ ] Bổ sung `config/holidays.yaml` theo thông báo lịch nghỉ 2026–2027 của HOSE/HNX.
- [ ] Đối chiếu giờ phiên phái sinh trong `settings.session` với quy định hiện hành.

**Giả định / rủi ro còn mở**
- Mã KRX năm ≥ 2028 chưa xác minh (`krx_year_verified`); luôn đối chiếu danh sách hợp đồng từ broker lúc khởi động.
- Lịch nghỉ hiện chỉ có ngày lễ cố định + Tết 2026 suy ra từ lịch đáo hạn; ngày đáo hạn 2027 có thể sai nếu trùng ngày nghỉ chưa khai.
- Giá trong bar import: dashboard có thể ghi theo điểm hoặc nghìn-lần; script tự chia 1000 khi > 50.000 và báo số dòng đã chia — kiểm vài ngày đầu bằng mắt.

**Bước tiếp theo:** P1.1 (ghi chú tài liệu DNSE LightSpeed sau KRX + SSI FC Data vào `docs/broker_notes.md`).
