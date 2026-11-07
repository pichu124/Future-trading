# CLAUDE.md — Bot intraday VN30F

File này là "hiến pháp" của dự án. Mọi code bạn (Claude) viết phải tuân thủ các luật dưới đây.
Nếu một yêu cầu mâu thuẫn với file này, DỪNG LẠI và hỏi trước khi viết code.

## 1. Mục tiêu

- Bot giao dịch intraday hợp đồng tương lai VN30 (tháng gần nhất, F1M), không giữ vị thế qua đêm.
- Ưu tiên theo thứ tự: **(1) không mất tiền vì lỗi kỹ thuật → (2) đo lường trung thực → (3) lợi nhuận.**
- Python 3.11+, chạy thật trên Windows trong `.venv`; CI chạy Linux. Async (`asyncio`) cho mọi I/O mạng.

## 2. Kiến trúc (bắt buộc)

```
core/         models.py (Tick, Bar, Signal, Order, Fill, Position), clock.py (lịch phiên),
              contracts.py (mã hợp đồng, đáo hạn, roll), settings.py (nạp + kiểm settings.yaml)
feeds/        data_feed_dnse.py, data_feed_ssi.py   → chuẩn hoá về cùng 1 schema Tick/Bar
recorder/     ghi tick + sổ lệnh ra Parquet theo ngày (data/raw/YYYY-MM-DD/)
strategy/     hàm THUẦN: nhận Bar/Tick + state → trả Signal. KHÔNG I/O, KHÔNG gọi API, KHÔNG đọc giờ hệ thống
risk/         risk_manager.py — cổng duy nhất giữa Signal và Order. Có quyền phủ quyết mọi lệnh
brokers/      base.py (interface BrokerAdapter), dnse.py, ssi.py, tcbs.py, paper.py
backtest/     engine dùng chính strategy/ + risk/ + brokers/paper.py; validation.py (kiểm định thống kê)
live/         runner.py — vòng lặp sự kiện, heartbeat, kill-switch; instance_lock.py
monitoring/   logger có cấu trúc (JSON), telegram.py
utils/        atomic_io.py (ghi Parquet/JSON tmp-rồi-tráo)
config/       settings.yaml (không chứa secret), holidays.yaml; secret ở .env (gitignore)
scripts/      công cụ chạy tay (import dữ liệu, kiểm lịch…)
tests/
```

Luật kiến trúc:
- **Cùng một code strategy chạy cho backtest, paper và live.** Chỉ đổi `BrokerAdapter` và nguồn dữ liệu.
- Strategy không bao giờ biết đang chạy live hay backtest.
- Mọi thời gian dùng `core/clock.py` (giờ Asia/Ho_Chi_Minh), có thể giả lập trong backtest.
- Mọi lệnh đi qua `risk_manager.approve(order, state) -> Approved | Rejected(reason)`.
- Mọi giá trị cấu hình đọc qua `core/settings.py`; giá trị còn `null` (chưa điền) ⇒ module cần nó phải từ chối chạy, không tự đoán.

## 3. Vai trò từng nguồn

| Nguồn | Vai trò | Ghi chú |
|---|---|---|
| DNSE Open API V2 (LightSpeed V2) | Feed realtime **chính** + broker thực thi **chính** | Ký HMAC-SHA256 mọi request (`api_key`/`api_secret`); lệnh thay đổi cần `trading-token` lấy qua email OTP (sống ~2 phút) hoặc Smart OTP. **Không có khoá idempotency** khi đặt lệnh ⇒ không tự retry. Tài liệu "trước KRX" (JWT + MQTT) là bản cũ |
| SSI FastConnect V3 (`ssi-sdk`) | Feed **dự phòng/đối chiếu** + broker **dự phòng** | Token qua OTP/Smart OTP; refresh token **chỉ dùng một lần** (dùng lại ⇒ thu hồi cả phiên). `client_request_id` ≤ 20 ký tự là khoá idempotency. Stream lệnh có `notify_id` để reconnect không mất sự kiện |
| TCBS Open API | Broker thứ 3 (tuỳ chọn), không bật mặc định | Chỉ bật sau khi DNSE + SSI ổn định |
| Finhay / vnstock | Dữ liệu tham khảo, EOD, lịch sự kiện | **Không** dùng cho tín hiệu intraday hay thực thi. Live Mode của vnstock-dashboard là poll HTTP ~2 giây, không phải stream tick |

Luật đa broker:
- Mỗi chiến lược chỉ có **một broker chính** tại một thời điểm. Không chia vị thế của một chiến lược cho nhiều broker.
- Broker dự phòng chỉ dùng cho: (a) đặt lệnh **đối ứng khẩn cấp** để triệt tiêu net exposure khi broker chính mất kết nối, (b) chạy chiến lược khác độc lập.
- `risk_manager` theo dõi **net position tổng trên mọi broker**.
- Feed DNSE và SSI phải được đối chiếu liên tục: lệch giá > `feed_divergence_ticks` hoặc một feed im lặng > `feed_stale_seconds` → ngừng mở vị thế mới và cảnh báo.

## 3b. Máy chạy — chỉ MỘT phiên live

Người dùng có hai máy (desktop + laptop) và thư mục dữ liệu của vnstock-dashboard đồng bộ qua Dropbox.
- Hai phiên cùng chạy = lệnh đôi. Runner live/paper **phải** qua hai chốt trước khi kết nối broker (`live/instance_lock.py`):
  1. `check_allowed_machine(settings.live.allowed_machine)` — chỉ đúng một hostname được chạy bot (chặn chéo hai máy, không cần thư mục dùng chung);
  2. `InstanceLock` — file khoá `{machine, pid, mode, started_at, heartbeat_at}` trong `state_dir`, chặn tiến trình thứ hai trên cùng máy; runner gọi `heartbeat()` mỗi `monitoring.heartbeat_seconds`, mất khoá ⇒ dừng ngay.
- Thư mục `data/` và `state/` của bot **không** được nằm trong thư mục đồng bộ Dropbox (tránh "conflicted copy" và hai máy cùng ghi). Đường dẫn đặt trong `settings.yaml`.
- Không nhận dạng máy bằng đường dẫn hay tên venv; dùng hostname (`socket.gethostname()`).

## 4. Đặc tả hợp đồng & phiên (KIỂM TRA LẠI trước khi live)

- Hệ số nhân: 100.000 VNĐ/điểm. Bước giá: 0,1 điểm. Biên độ ngày: ±7% so với giá tham chiếu.
- Ngày đáo hạn: thứ Năm tuần thứ 3 của tháng; trùng ngày nghỉ ⇒ lùi về phiên liền trước (Tết 2026: VN30F2602 đáo hạn 13/02 thay vì 19/02). Roll sang hợp đồng kế tiếp theo `roll_days_before_expiry`.
- **Mã hợp đồng**: sau KRX, mã có dạng `41I1` + chữ năm (A=2020, … E=2024, H=2027) + chữ tháng (`1`–`9`, `A`/`B`/`C` = 10/11/12) + `000`; tên cũ `VN30FYYMM`. `core/contracts.py` chuyển qua lại, nhưng lúc khởi động **vẫn phải** đối chiếu với danh sách hợp đồng broker trả về. Năm ≥ 2028 chưa được xác minh.
- Lịch phiên và ngày nghỉ đặt trong `config/settings.yaml` và `config/holidays.yaml`, không hardcode trong code. `holidays.yaml` phải được cập nhật theo thông báo của sở mỗi năm; `scripts/check_calendar.py` cảnh báo khi năm hiện tại/năm tới còn thiếu.
- Phí sàn, phí VSD, phí môi giới, thuế: đặt trong `settings.yaml`. Backtest bắt buộc dùng các giá trị này.

## 5. Luật rủi ro cứng (risk_manager phải thực thi, có unit test cho từng luật)

- `max_contracts`: số hợp đồng tối đa net tại mọi thời điểm.
- `max_daily_loss_points`: chạm ngưỡng → đóng hết vị thế, khoá bot đến hết ngày.
- `max_trades_per_day`, `max_consecutive_losses`.
- Không mở vị thế mới: trong `no_entry_open_minutes` đầu phiên, sau `last_entry_time`, trong ngày đáo hạn (trừ khi bật cờ riêng).
- `flatten_time`: đóng mọi vị thế trước phiên ATC.
- Mọi lệnh vào phải có stop-loss tương ứng được đặt ngay sau khi khớp. Không có stop → huỷ vị thế.
- Chống lệnh trùng: mỗi lệnh có `client_order_id` duy nhất, idempotent khi retry.
- Mất kết nối broker > `broker_timeout_seconds` khi đang có vị thế → quy trình khẩn cấp (mục 3).
- Kill-switch thủ công: file `KILL` trong thư mục gốc hoặc lệnh Telegram `/kill` → huỷ lệnh chờ, đóng vị thế, dừng.

## 6. Backtest — chống tự lừa mình

- Khớp lệnh theo bid/ask (mua khớp ask, bán khớp bid), cộng trượt giá ≥ `slippage_ticks`.
- Tín hiệu tính trên bar đã **đóng**; lệnh khớp ở bar/tick **kế tiếp**. Cấm dùng giá đóng cửa của chính bar sinh tín hiệu.
- Bar chứa cả TP lẫn SL: dùng `backtest.validation.bar_high_first` (cực trị gần giá mở chạm trước); không quyết được ⇒ SL thắng.
- Chia dữ liệu: in-sample / out-of-sample / walk-forward. Báo cáo cả ba.
- Báo cáo tối thiểu: số lệnh, win rate, avg win/loss, profit factor, max drawdown, P&L **sau phí**, P&L theo giờ trong ngày và theo ngày trong tuần, cùng `validate_trades()` (p-value sign-flip, CI Sharpe bootstrap, walk-forward).
- Mỗi khi thử thêm cấu hình tham số, tăng `n_trials` và gọi `apply_selection_penalty`; ghi lại vào `research/log.md` kèm lý do.
- Dữ liệu nhập từ vnstock-dashboard (`data/imported/`) là snapshot ~2 giây, volume không tin được: chỉ dùng để thử nhanh, **không** dùng để kết luận có edge.

## 7. Bảo mật

- Secret chỉ ở `.env` (gitignore). Không bao giờ log token, OTP, private key.
- Mặc định `TRADING_MODE=paper`. Muốn live phải đổi biến môi trường **và** truyền cờ `--live` khi chạy.
- Hàm đặt lệnh thật trong adapter phải kiểm tra lại `TRADING_MODE` trước khi gọi API.

## 8. Cách làm việc với Claude

- Mỗi lần chỉ làm **một module**. Viết interface + test trước, rồi mới implement.
- Trước khi viết code gọi API broker: đọc tài liệu chính thức mới nhất, ghi endpoint/field vào `docs/broker_notes.md`. Không đoán tên field; mục đánh dấu ❓ trong đó phải được kiểm bằng gọi thử trước khi dựa vào.
- Kết thúc mỗi task: chạy `python -m pytest -q`, liệt kê rủi ro/giả định còn lại, cập nhật `docs/progress.md`.
- Không tự ý thêm thư viện lớn; hỏi trước.
- Không bao giờ đề xuất "tối ưu tham số cho lợi nhuận cao nhất" trên toàn bộ dữ liệu.
- LLM không nằm trong đường ra quyết định live. Connector MCP (SSI/TCBS/Finhay qua tài khoản Claude) chỉ dùng cho research.
- Luôn hỏi người dùng trước khi: push/merge vào `main`, force-push, xoá dữ liệu, bật `TRADING_MODE=live`.

## 9. Tái dùng từ vnstock-dashboard

Các module sau được port (viết lại gọn, có test riêng) từ repo `pichu124/vnstock-dashboard`. Khi sửa, giữ đúng hành vi đã được kiểm chứng ở đó:

| Ở đây | Nguồn gốc | Ghi chú |
|---|---|---|
| `core/contracts.py` | `vnstock_futures_foreign.py` (`krx_symbol`, `symbol_contract`, `third_thursday`, `candidates`) | Thêm: lùi ngày đáo hạn theo lịch nghỉ, chọn F1M/F2M theo ngày |
| `utils/atomic_io.py` | `vnstock_parquet_io.py` | tmp-rồi-tráo, retry khi Windows khoá file |
| `backtest/validation.py` | `vnstock_strategy.py` (`monte_carlo_pvalue`, `bootstrap_sharpe_ci`, `walk_forward_consistency`, `apply_selection_penalty`, `_bar_high_first`) | Thuần numpy, không trạng thái |
| `monitoring/telegram.py` | `vnstock_telegram.send_message` | Cắt tin > 4096 ký tự, lùi về plain text khi HTML lỗi; kênh `trading` riêng |
| `scripts/import_dashboard_bars.py` | đọc `data_warehouse/_live/archive/bars_*.parquet` | Cứu bar 1 phút VN30F trước khi dashboard tự xoá (30 ngày) |

**Không** port: `vnstock_wss` (poll, không phải stream), engine backtest bar ngày và vòng LLM tự sinh/tự tune chiến lược của `vnstock_strategy`.
