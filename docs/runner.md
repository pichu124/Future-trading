# Chạy bot — `live/runner.py` (paper và live)

> Lệnh Telegram: `/kill`, `/status`, `/pnl`, `/help`, và `/otp [dnse|ssi] <mã>` — chuyển OTP cho
> adapter để lấy token. Không ghi tên broker thì gửi cho broker DUY NHẤT đang chờ OTP; hai broker cùng
> chờ ⇒ bot hỏi lại (mỗi bên một mã). Bot không bao giờ nhắc lại mã; nên xoá tin nhắn chứa mã sau khi dùng.

## Live (đã nối, nhưng còn KHOÁ cho tới khi bạn mở từng chốt)

Live = broker chính + broker dự phòng (`brokers.primary` / `brokers.backup` ∈ dnse | ssi | tcbs, khác nhau; mặc định
DNSE chính + SSI dự phòng — dự phòng chỉ đặt lệnh đối ứng khẩn cấp khi broker chính mất kết nối) + feed tổng hợp
LUÔN là DNSE + SSI (lệch giá / một feed im lặng ⇒ ngừng mở vị thế) + stream lệnh của broker SSI/TCBS. Lắp ở
`live/wiring.py`. Chọn TCBS ⇒ thêm `brokers.tcbs_enabled: true`, `brokers.tcbs.api_verified: true`,
`TCBS_API_KEY/CUSTODY_CODE/DERIVATIVE_ACCOUNT`; SSI không làm broker vẫn cần `SSI_API_KEY/SECRET` cho feed
(và `/otp ssi` lúc khởi động); DNSE không làm broker vẫn cần `DNSE_API_KEY/SECRET` cho feed + danh sách hợp đồng.

Runner chỉ chạy live khi qua ĐỦ các chốt, thiếu một ⇒ thoát mã 2 trước khi kết nối gì:
1. `TRADING_MODE=live` trong môi trường **và** cờ `--live`;
2. hostname = `live.allowed_machine`, không có phiên khác giữ file khoá;
3. `brokers.dnse.api_verified: true` và `brokers.ssi.api_verified: true` — chỉ đặt sau khi chạy
   `scripts/dnse_trading_probe.py` / `scripts/ssi_trading_probe.py` và chốt các mục ❓ (broker_notes §1.5, §2.6);
4. không còn null: `fees.*`, `risk.*`, `brokers.dnse.loan_package_id`, `brokers.dnse.trading_token_ttl_seconds`,
   `brokers.ssi.order_symbol_format`, `brokers.ssi.market_order_type`;
5. `.env`: `DNSE_API_KEY/SECRET/ACCOUNT_NO`, `SSI_CLIENT_ID/API_KEY/API_SECRET/ACCOUNT/PRIVATE_KEY_PATH`,
   `TELEGRAM_TRADING_BOT_TOKEN/CHAT_ID` (live bắt buộc Telegram — nhập OTP, `/kill`, cảnh báo).

```bat
set TRADING_MODE=live
.venv\Scripts\python.exe -m live.runner --live --assume-computed-contract
```
Khởi động live: kết nối hai broker → DNSE gửi OTP email, SSI gửi OTP → bot nhắn "⏳ Chờ OTP" → bạn trả
lời `/otp dnse <mã>`, `/otp ssi <mã>` (và `/otp tcbs <mã>` — iOTP trong app TCInvest — nếu dùng TCBS) (tối đa `live.otp_wait_seconds`, mặc định 600s; quá ⇒ thoát,
không giao dịch; `/kill` lúc này ⇒ thoát) → đối soát vị thế/lệnh chờ trên CẢ HAI broker → chạy.
Khởi động lại giữa phiên ⇒ nhập OTP lại (token SSI không ghi đĩa).

## Paper với feed tổng hợp DNSE + SSI (tuỳ chọn)
`paper.composite_feed: true` ⇒ paper vẫn khớp lệnh mô phỏng nhưng dữ liệu = DNSE + SSI qua `CompositeFeed`
(như live: giá lệch > `feeds.feed_divergence_ticks` hoặc một feed im > `feeds.feed_stale_seconds` ⇒ ngừng mở
vị thế, cảnh báo Telegram). Cần thêm `SSI_API_KEY`/`SSI_API_SECRET` và Telegram: lúc khởi động SSI gửi OTP,
bot nhắn "⏳ Chờ OTP cho ssi", trả lời `/otp ssi <mã>` (hoặc `/otp <mã>` nếu chỉ SSI đang chờ). Quá
`live.otp_wait_seconds` ⇒ thoát, không chạy. Mặc định `false` = chỉ feed DNSE như trước.

## Chiến lược cần lịch sử nhiều ngày (`history_days > 0`)

Runner nạp bar 1 phút các phiên trước từ `live.history_bar_dirs` (mặc định `data/history/ohlc1m` rồi `data/bars`)
lúc khởi động, **trước khi kết nối broker**, dựng chuỗi back-adjust bằng đúng code của backtest
(`core.continuous.HistoryBuilder`, chênh lệch roll = trung vị `contract.roll_gap_median_minutes` phút) ⇒
`ctx.history` live = backtest với cùng dữ liệu.

Nghiêm ngặt: phải đủ đúng N phiên giao dịch gần nhất của hợp đồng giao dịch từng ngày; ngày trước roll phải có
cả hợp đồng kế tiếp (đo chênh lệch). Thiếu ⇒ báo Telegram ngày/hợp đồng cần tải + lệnh `download_history.py`,
thoát mã 2, KHÔNG giao dịch. Hợp đồng kế tiếp ngày trước roll lấy từ một trong hai nguồn:
- recorder, khi bật `recorder.record_next_contract_on_roll_eve` (TẮT mặc định — bật sau khi
  `dnse_feed_probe.py --krx <chính> <kế tiếp> --extra-contract <tên>` báo ✅ nhận tick của cả hai mã);
- hoặc downloader (mặc định tải thêm hợp đồng kế tiếp hôm đó), vd sáng ngày roll:

    python scripts/download_history.py --start <ngày trước roll> --end <ngày trước roll>

## Điều kiện trước khi chạy paper
- `config/settings.yaml`: đã điền `risk.*`, `fees.*`, `live.allowed_machine` (= hostname máy này,
  xem bằng `hostname`), `paths.*` ngoài Dropbox. `python -m core.settings` phải không còn khoá rỗng
  liên quan.
- `.env`: `DNSE_API_KEY`, `DNSE_API_SECRET` (feed dữ liệu), `TELEGRAM_TRADING_BOT_TOKEN`,
  `TELEGRAM_TRADING_CHAT_ID` (cảnh báo + lệnh điều khiển; thiếu thì chạy không có Telegram).

## Chạy
```bat
.venv\Scripts\python.exe -m live.runner --assume-computed-contract
```
Chọn chiến lược bằng `--strategy module:Class` (mặc định ORB #1). Tin Telegram lúc khởi động ghi tên chiến lược.
Ví dụ paper chiến lược #3 (đà sáng → chiều — CHƯA có bằng chứng edge, paper để kiểm vận hành):
```bat
.venv\Scripts\python.exe -m live.runner --assume-computed-contract --strategy strategy.morning_momentum:MorningMomentum
```
#3/#4/#5 cần lịch sử 10 phiên (ADR) ⇒ trước mỗi ngày paper, `data/history/ohlc1m` + `data/bars` phải có đủ tới
phiên HÔM QUA (recorder hằng ngày ghi `data/bars/<ngày>`; thiếu ngày nào thì `download_history.py` ngày đó) và
ngày trước roll phải có cả HĐ kế tiếp (mục trên). Thiếu ⇒ runner báo và thoát 2, không giao dịch.
Runner chạy MỘT chiến lược mỗi lần (một broker chính, CLAUDE.md §3) — không chạy hai runner song song.

Runner: khoá phiên (máy được phép + file khoá) → đối soát vị thế → mỗi giây: tick → bar → strategy →
risk → paper broker; heartbeat khoá mỗi `monitoring.heartbeat_seconds`; tự thoát sau 14:45 kèm báo cáo.

## Tự khởi động hằng ngày (Task Scheduler, thêm 12/11/2026)

Runner tự lo phần lịch: ngày nghỉ ⇒ thoát 0 ngay; trước 09:00 chờ (feed không bị coi là im lặng ngoài phiên liên
tục); sau 14:45 gửi báo cáo rồi thoát. Nên chỉ cần Task Scheduler gọi nó **08:40 thứ Hai–Sáu**. Chiến lược #3
cần nến 09:00 ⇒ khởi động sau 09:00 thì hôm đó không vào lệnh (đúng thiết kế).

PowerShell **Run as administrator** (sửa `$repo` nếu cần; `pythonw.exe` ⇒ không có cửa sổ để lỡ tay đóng —
mọi thứ vẫn vào `logs/bot_YYYY-MM-DD.jsonl` và Telegram):

```powershell
$repo = "D:\Future-trading"
$args_ = "-m live.runner --assume-computed-contract --strategy strategy.morning_momentum:MorningMomentum"
$act = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\pythonw.exe" -Argument $args_ -WorkingDirectory $repo
$trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:40
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 7) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "VN30F Paper Runner" -Action $act -Trigger $trg -Settings $set -Force
```

- Kiểm tra mỗi sáng: Telegram nhận "🤖 Runner paper khởi động … · chiến lược Đà sáng → chiều (#3)" lúc ~08:40;
  thiếu lịch sử ⇒ nhận 🚨 nêu ngày cần `download_history.py` (runner thoát 2, không giao dịch).
- Không thấy tin: `Get-ScheduledTaskInfo -TaskName "VN30F Paper Runner" | Select LastRunTime, LastTaskResult`
  (mã thoát như mục dưới; `267009` = đang chạy) và xem `logs/bot_<ngày>.jsonl`.
- Dừng hôm nay: `Stop-ScheduledTask -TaskName "VN30F Paper Runner"` (paper ⇒ không có vị thế thật). Tắt hẳn:
  `Disable-ScheduledTask -TaskName "VN30F Paper Runner"`. Chạy tay song song ⇒ bản sau thoát 3 (khoá phiên).
- Đổi chiến lược: sửa `$args_` rồi chạy lại `Register-ScheduledTask … -Force`.
- CHỈ dùng cho paper. Live không chạy bằng task tự động (cần `/otp` và người theo dõi).

## Điều khiển
- Telegram (chỉ từ đúng `TELEGRAM_TRADING_CHAT_ID`): `/kill`, `/status`, `/pnl`, `/help`.
- File `KILL` ở thư mục gốc repo ⇒ như `/kill`. Kill-switch và khoá khẩn cấp **không tự mở** — xoá
  file và gọi `RiskManager.clear_kill` / `clear_emergency` (sẽ có lệnh tay ở bước sau).

## Mã thoát
`0` hết phiên/kill và đã phẳng · `1` còn vị thế/lệnh chờ khi dừng · `2` cấu hình/an toàn từ chối chạy ·
`3` mất khoá phiên (có phiên khác) — kiểm tra ngay máy còn lại.

## Tệp
- `state/daily_state.json`: bộ đếm rủi ro trong ngày (khởi động lại không xoá giới hạn lỗ ngày).
- `data/reports/daily_YYYY-MM-DD.txt`: báo cáo cuối ngày (lệnh, P&L sau phí, lệnh bị từ chối + lý do).

## Log
- `logs/bot_YYYY-MM-DD.jsonl` (runner) và `logs/record_YYYY-MM-DD.jsonl` (recorder): mỗi dòng một JSON
  `{ts (+07:00), level, logger, msg, event?, …field}`; thư mục đặt ở `paths.log_dir`, không được nằm trong Dropbox.
- Sự kiện có cấu trúc (`event`): `signal`, `order` (mỗi lần đổi trạng thái), `fill` (kèm `net_after`),
  `exec_rejected` (kèm `code` của risk), `exec_broker_*`, `risk_action`, `notify` (mọi tin gửi Telegram).
- Token / OTP / khoá: khoá nhạy cảm ⇒ `***`, giá trị trong `.env` (*_KEY, *_SECRET, *_TOKEN) ⇒ `<secret>`.
- Dựng timeline sau phiên:
  ```python
  from monitoring.logger import read_events
  for e in read_events("logs/bot_2026-11-09.jsonl", event="fill"): print(e["ts"], e["side"], e["qty"], e["price"])
  ```
