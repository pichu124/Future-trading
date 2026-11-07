# Tiến độ

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
