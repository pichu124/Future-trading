# Chạy bot — `live/runner.py` (paper và live)

> Lệnh Telegram: `/kill`, `/status`, `/pnl`, `/help`, và `/otp [dnse|ssi] <mã>` — chuyển OTP cho
> adapter để lấy token. Không ghi tên broker thì gửi cho broker DUY NHẤT đang chờ OTP; hai broker cùng
> chờ ⇒ bot hỏi lại (mỗi bên một mã). Bot không bao giờ nhắc lại mã; nên xoá tin nhắn chứa mã sau khi dùng.

## Live (đã nối, nhưng còn KHOÁ cho tới khi bạn mở từng chốt)

Live = DNSE chính + SSI dự phòng (chỉ đặt lệnh đối ứng khẩn cấp khi DNSE mất kết nối) + feed tổng hợp
DNSE/SSI (lệch giá / một feed im lặng ⇒ ngừng mở vị thế) + stream lệnh SSI. Lắp ở `live/wiring.py`.

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
lời `/otp dnse <mã>` và `/otp ssi <mã>` (tối đa `live.otp_wait_seconds`, mặc định 600s; quá ⇒ thoát,
không giao dịch; `/kill` lúc này ⇒ thoát) → đối soát vị thế/lệnh chờ trên CẢ HAI broker → chạy.
Khởi động lại giữa phiên ⇒ nhập OTP lại (token SSI không ghi đĩa).

## Paper với feed tổng hợp DNSE + SSI (tuỳ chọn)
`paper.composite_feed: true` ⇒ paper vẫn khớp lệnh mô phỏng nhưng dữ liệu = DNSE + SSI qua `CompositeFeed`
(như live: giá lệch > `feeds.feed_divergence_ticks` hoặc một feed im > `feeds.feed_stale_seconds` ⇒ ngừng mở
vị thế, cảnh báo Telegram). Cần thêm `SSI_API_KEY`/`SSI_API_SECRET` và Telegram: lúc khởi động SSI gửi OTP,
bot nhắn "⏳ Chờ OTP cho ssi", trả lời `/otp ssi <mã>` (hoặc `/otp <mã>` nếu chỉ SSI đang chờ). Quá
`live.otp_wait_seconds` ⇒ thoát, không chạy. Mặc định `false` = chỉ feed DNSE như trước.

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
Runner: khoá phiên (máy được phép + file khoá) → đối soát vị thế → mỗi giây: tick → bar → strategy →
risk → paper broker; heartbeat khoá mỗi `monitoring.heartbeat_seconds`; tự thoát sau 14:45 kèm báo cáo.

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
