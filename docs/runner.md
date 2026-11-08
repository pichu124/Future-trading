# Chạy bot (paper) — `live/runner.py`

> Live CHƯA chạy được: adapter DNSE (`brokers/dnse.py`, P4.1) và SSI (`brokers/ssi.py`, P4.2) đã có
> nhưng phải kiểm API trước (`scripts/dnse_trading_probe.py`, `scripts/ssi_trading_probe.py` ⇒
> `brokers.*.api_verified`) và runner chưa nối chế độ live.
> `--live` hoặc `TRADING_MODE=live` hiện bị từ chối có chủ đích.
>
> Lệnh Telegram: `/kill`, `/status`, `/pnl`, `/help`, và `/otp [dnse|ssi] <mã>` — chuyển OTP cho
> adapter để lấy token. Không ghi tên broker thì gửi cho broker DUY NHẤT đang chờ OTP; hai broker cùng
> chờ ⇒ bot hỏi lại (mỗi bên một mã). Bot không bao giờ nhắc lại mã; nên xoá tin nhắn chứa mã sau khi dùng.

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
