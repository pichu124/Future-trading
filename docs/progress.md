# Tiến độ

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
- Chuỗi thua tính theo điểm **trước phí**; lượt hoà không đổi bộ đếm; đặt lại mỗi ngày (`new_day`). Lượt bị đóng bằng lệnh đối ứng ở broker khác chưa có P&L thực hiện ⇒ tính như hoà.
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
