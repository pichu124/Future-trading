# Bộ prompt xây bot VN30F — dùng tuần tự trong Claude Code

Dán từng prompt vào Claude Code (mở trong thư mục repo này), xong một bước, review và chạy `python -m pytest -q` rồi mới sang bước tiếp.
Chỗ `[...]` là phần bạn tự điền. Trạng thái từng bước xem `docs/progress.md`.

> ✅ Đã làm: P0.1, P0.2, P0.3, P1.0–P1.5, P2.1, P2.2, P3.1, P3.2, P3.3, P4.1, P4.2, P4.3, P5.1, P5.2 (P4.1/P4.2: adapter xong, live khoá tới khi kiểm API; P3.2/P3.3: code + luật kết luận xong, CHƯA chạy vì thiếu dữ liệu). Điểm nghẽn: API key DNSE/SSI (chốt ❓ của feed, bật recorder hằng ngày, làm **P1.6**). Lỗi #1–#12, #14 trong `docs/golive_checklist.md` đã xử lý (#13 đã nối cho live). Phần còn lại cần API key.

---

## Giai đoạn 0 — Khung dự án

**P0.1 · Scaffold** ✅
> Đọc CLAUDE.md. Tạo cấu trúc thư mục đúng mục 2, `pyproject.toml`, `.gitignore` (có `.env`, `data/`, `*.key`), `config/settings.yaml` với đầy đủ các khoá ở mục 4–5 (để giá trị placeholder kèm comment). Tạo `core/models.py` với dataclass Tick, Bar, Signal, Order, Fill, Position. Chưa viết logic nào khác. Kết thúc bằng danh sách các giá trị trong settings.yaml tôi phải tự điền.

**P0.2 · Lịch phiên** ✅
> Viết `core/clock.py`: lấy lịch phiên từ settings.yaml, hàm `session_state(ts)` trả về PRE_OPEN / ATO / CONTINUOUS / LUNCH / ATC / CLOSED, hàm `is_expiry_day(date)` (thứ Năm tuần thứ 3), `next_contract_roll_date()`. Clock phải inject được thời gian giả cho backtest. Viết test cho mọi ranh giới phiên và cho các ngày đáo hạn năm 2026–2027.

**P0.3 · Port từ vnstock-dashboard** ✅
> Port các hàm liệt kê ở mục 9 CLAUDE.md sang `core/contracts.py`, `utils/atomic_io.py`, `backtest/validation.py`, `monitoring/telegram.py`, kèm `live/instance_lock.py` (mục 3b). Viết lại gọn, có test riêng, không import gì từ repo dashboard.

---

## Giai đoạn 1 — Dữ liệu (bắt đầu ghi càng sớm càng tốt)

**P1.0 · Cứu dữ liệu từ dashboard** ✅
> Viết `scripts/import_dashboard_bars.py`: đọc `bars_*.parquet` trong `_live/archive` (và `_live`) của vnstock-dashboard, lọc dòng phái sinh VN30, đổi mã `41I1…`/`VN30F1M` về tên hợp đồng thật theo ngày, ghi `data/imported/YYYY-MM-DD.parquet`, cột `volume_reliable=False`. Chạy idempotent, không sửa file nguồn.

**P1.1 · Ghi chú tài liệu broker** ✅
> Đọc tài liệu chính thức DNSE LightSpeed Market Data (bản sau KRX) và SSI FastConnect Data. Ghi vào `docs/broker_notes.md`: cách xác thực, giao thức streaming, cách subscribe tick khớp lệnh và sổ lệnh (top 3/top 10) cho hợp đồng tương lai VN30, tên field, đơn vị giá, cách lấy danh sách mã hợp đồng hiện hành. Đánh dấu rõ chỗ nào chưa chắc chắn. Không viết code.

**P1.2 · Feed DNSE** ✅ (message mẫu theo SDK; chờ khoá để kiểm với server thật)
> Dựa trên broker_notes.md, viết `feeds/data_feed_dnse.py`: async, tự lấy mã F1M lúc khởi động, subscribe tick + sổ lệnh, chuẩn hoá về `Tick`, tự reconnect với backoff, phát hiện gap theo sequence nếu có, log số message/phút. Interface chung `DataFeed` ở `feeds/base.py`. Viết test với message mẫu (fixture JSON).

**P1.3 · Feed SSI** ✅ (frame mẫu theo SDK; token SSI thật chờ P4.2)
> Viết `feeds/data_feed_ssi.py` theo cùng interface `DataFeed`. Sau đó viết `feeds/composite.py`: chạy song song DNSE (chính) và SSI (dự phòng), phát cảnh báo khi lệch > `feed_divergence_ticks` hoặc một feed im > `feed_stale_seconds`, tự chuyển nguồn chính khi DNSE chết.

**P1.4 · Recorder** ✅ (hướng dẫn Task Scheduler: `docs/recorder_windows.md`)
> Viết `recorder/`: ghi tick và snapshot sổ lệnh từ composite feed ra Parquet theo ngày, flush mỗi N giây, không mất dữ liệu khi crash (ghi file tạm rồi rename). Script `scripts/record_today.py` chạy từ 8:30 đến 15:00 rồi tự thoát. Hướng dẫn tôi đặt Windows Task Scheduler chạy mỗi ngày giao dịch.

**P1.5 · Kiểm định dữ liệu** ✅ (tự chạy cuối `record_today.py`)
> Viết `scripts/validate_day.py`: với một ngày dữ liệu, báo cáo số tick, khoảng trống > 5 giây trong phiên liên tục, tick ngoài biên độ, chênh lệch DNSE vs SSI, và dựng bar 1 phút. So khớp OHLC 1 phút với dữ liệu vnstock của cùng ngày, liệt kê sai lệch.

**P1.6 · Tải dữ liệu lịch sử thật** — bước 1 ✅ `scripts/dnse_history_probe.py` (chạy với khoá để chốt ❓); bước 2 downloader chờ kết quả thăm dò
> Theo `docs/broker_notes.md` §1.3 và §2.5: viết `scripts/download_history.py` tải OHLC 1 phút (`GET /price/ohlc`, `type=DERIVATIVE`) và lịch sử khớp lệnh (`GET /price/{symbol}/trades`, `boardId=G1`) của DNSE cho từng hợp đồng VN30F (mã KRX từ `core.contracts`), ghi `data/history/<loại>/<hợp đồng>/<ngày>.parquet` (ghi nguyên tử, chạy lại không tải trùng). Trước khi viết code: gọi thử bằng tay để chốt các mục ❓ (đơn vị `from`/`to`, độ sâu lịch sử hợp đồng đã đáo hạn) và cập nhật broker_notes. Tôn trọng HTTP 429 `retry_after`. Viết test với response mẫu.

---

## Giai đoạn 2 — Rủi ro & paper broker (trước mọi chiến lược)

**P2.1 · Risk manager** ✅
> Viết `risk/risk_manager.py` thực thi từng luật ở mục 5 CLAUDE.md. Viết test TRƯỚC cho từng luật (một test/luật, cả trường hợp biên), rồi mới implement. Theo dõi net position trên nhiều broker. Trả về `Rejected(reason)` có lý do rõ ràng để log.

**P2.2 · Paper broker** ✅
> Viết `brokers/base.py` (interface BrokerAdapter: place, cancel, modify, positions, on_fill callback) và `brokers/paper.py`: khớp theo bid/ask của tick kế tiếp, trượt giá cấu hình được, tính phí theo settings.yaml, mô phỏng độ trễ. Đây là broker dùng chung cho backtest và paper trading.

---

## Giai đoạn 3 — Backtest & chiến lược

**P3.1 · Engine** ✅
> Viết `backtest/engine.py` replay dữ liệu Parquet qua đúng pipeline live: feed giả → strategy → risk_manager → paper broker. Xuất báo cáo theo mục 6 CLAUDE.md ra HTML. Thêm test chứng minh không có look-ahead: chiến lược "gian lận" dùng giá tương lai phải bị engine chặn hoặc lộ ra.

**P3.2 · Chiến lược baseline** ✅ (`strategy/baseline_orb.py`, chưa chạy in-sample)
> Viết `strategy/baseline_[tên].py`: [mô tả chiến lược bằng lời của bạn — ví dụ: breakout biên độ 15 phút đầu phiên liên tục, vào lệnh khi bar 1 phút đóng ngoài biên, stop ở cạnh đối diện, chốt lời 1.5R, tối đa 2 lệnh/ngày]. Hàm thuần, tham số đọc từ settings. Chạy backtest in-sample [khoảng ngày], báo cáo, rồi liệt kê 5 lý do kết quả này có thể đẹp hơn thực tế.

**P3.3 · Kiểm định độ bền** ✅ (`backtest/robustness.py`, chưa chạy dữ liệu thật)
> Với chiến lược baseline: chạy out-of-sample và walk-forward, phân tích độ nhạy ±20% cho từng tham số, chạy lại với trượt giá 2 tick. Kết luận chiến lược có edge hay không, kèm bằng chứng. Nếu không có edge, nói thẳng.

---

## Giai đoạn 4 — Kết nối broker thật (paper trước)

**P4.1 · Adapter DNSE** ✅ (`brokers/dnse.py`; OTP nhập tay qua Telegram `/otp` thay cho Gmail API — quyết định của người dùng; live khoá tới khi chạy `scripts/dnse_trading_probe.py` và đặt `api_verified`)
> Đọc tài liệu LightSpeed Trading (bản sau KRX), cập nhật broker_notes.md, rồi viết `brokers/dnse.py`: đăng nhập JWT, lấy trading token qua emailOTP (đọc OTP từ Gmail API), tự gia hạn token, đặt/huỷ/sửa lệnh phái sinh, nhận trạng thái lệnh và vị thế. Idempotent theo client_order_id. Kiểm tra TRADING_MODE trước mọi lệnh thật. Viết chế độ `--dry-run` chỉ log payload.

**P4.2 · Adapter SSI** ✅ (`brokers/ssi.py`; OTP nhập tay `/otp ssi`; live khoá tới khi chạy `scripts/ssi_trading_probe.py`, điền `order_symbol_format`/`market_order_type` và `api_verified`)
> Tương tự cho `brokers/ssi.py` với FastConnect Trading: ký RSA, OTP, stream trạng thái lệnh và danh mục phái sinh, lưu notify_id để reconnect không mất sự kiện.

**P4.3 · Quy trình khẩn cấp** ✅ (`live/emergency.py`)
> Viết `live/emergency.py`: khi broker chính mất kết nối quá `broker_timeout_seconds` mà đang có vị thế, đặt lệnh đối ứng trên broker dự phòng để net = 0, cảnh báo Telegram, khoá mở vị thế mới. Viết test mô phỏng các kịch bản: mất kết nối giữa chừng, khớp một phần, cả hai broker đều lỗi.

---

## Giai đoạn 5 — Chạy live

**P5.1 · Runner + giám sát** ✅ (paper + live đã nối qua `live/wiring.py`; live còn khoá tới khi kiểm API — `docs/runner.md`)
> Viết `live/runner.py`: khởi động (kiểm tra kết nối, lấy mã hợp đồng, đối soát vị thế thực tế với state), vòng lặp sự kiện, heartbeat mỗi 30 giây, kill-switch (file KILL và lệnh Telegram /kill /status /pnl). Cuối ngày gửi báo cáo Telegram: lệnh, P&L sau phí, lệnh bị risk từ chối và lý do.

**P5.2 · Checklist trước live** ✅ (`docs/golive_checklist.md`)
> Rà toàn bộ repo theo CLAUDE.md và lập checklist go-live: test nào chưa có, luật rủi ro nào chưa được thực thi, chỗ nào có thể gửi lệnh trùng, chỗ nào log lộ secret. Chưa sửa gì, chỉ báo cáo.

---

## Prompt dùng lại bất cứ lúc nào

- **Review lỗi:** "Review [file] như một người muốn bot này thua tiền: tìm race condition, lệnh trùng, look-ahead, xử lý lỗi thiếu, giả định ngầm về dữ liệu."
- **Debug sau phiên:** "Đây là log ngày [ngày]. Dựng lại timeline từng lệnh, chỉ ra chỗ hành vi khác với backtest, và vì sao."
- **Thêm chiến lược mới:** "Viết chiến lược [mô tả] theo đúng interface strategy hiện có. Không sửa risk/ hay brokers/. Backtest out-of-sample trước khi báo kết quả in-sample."
