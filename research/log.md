# Nhật ký nghiên cứu

Mỗi lần thử một cấu hình/chiến lược mới, ghi một dòng — kể cả cấu hình thất bại. Tổng số dòng
là `n_trials` khi gọi `backtest.validation.apply_selection_penalty` (CLAUDE.md mục 6).

## Mốc chia & dữ liệu (CHỐT 09/11/2026, TRƯỚC khi chạy bất kỳ backtest nào trên dữ liệu thật)

- Dữ liệu: khớp lệnh DNSE → nến 1 phút (`data/history/ohlc1m`), **2026-05-11 → 2026-11-06**, 127 ngày giao dịch,
  `scripts/check_history.py` ✅ (53 ngày có vài phút thiếu lệnh, ~0,4% nến; 09/06 thiếu 452 HĐ lúc 09:39–09:43).
- **In-sample: 2026-05-11 → 2026-08-31 · Out-of-sample: 2026-09-01 → 2026-11-06** (người dùng chọn). OOS chỉ xem
  MỘT lần với cấu hình đã chốt; mọi cấu hình (#1, #2, …) dùng chung mốc này.
- Luật "dữ liệu tin được" của `backtest.robustness`: ≤ 1% nến volume không tin được (người dùng chọn cùng lúc,
  trước khi xem kết quả; luật cũ "0 nến" luôn ra CHƯA ĐỦ với dữ liệu thật).
- Phí theo biểu phí DNSE 01/05/2026 (~23.350đ/HĐ/chiều), trượt giá 1 bước, spread giả 1 bước.

## Mô hình khớp lệnh: nến → tick (09/11/2026)

- ORB #1, IS (11/05→31/08), cùng tham số, chỉ đổi mô hình khớp: **nến 1 phút** 141 lệnh, −3.784.700đ;
  **tick thật** (`--ticks data/history/trades`, báo giá giả last ∓ 1 bước) 142 lệnh, **+3.208.600đ**. Chênh
  ≈ 0,5 điểm/lệnh.
- Kiểm tra mô phỏng trên **random walk** (không edge, 4 seed × 125 ngày, ~230 lệnh/seed, settings thật): tick
  trước phí −0,51 / −0,80 / −1,04 / −0,69 điểm/lệnh (chi phí khớp kỳ vọng ≈ −0,4) ⇒ **mô hình tick không tạo lãi
  giả**; mô hình nến luôn kém hơn tick 0,20–0,23 điểm/lệnh trên cùng dữ liệu (stop khớp ở cực trị nến — giả định
  xấu nhất, cố ý). Trên dữ liệu thật nến rộng hơn ⇒ chênh lớn hơn là hợp lý.
- **Quyết định (ghi TRƯỚC khi xem OOS theo tick):** từ nay mô hình tick là mô hình chính khi có khớp lệnh thật;
  đánh giá lại #1 và #2 bằng `backtest.robustness --ticks` với ĐÚNG tham số, mốc chia và luật kết luận cũ.
  Không phải cấu hình mới ⇒ `n_trials` giữ 2. Lưu ý trung thực: việc đổi mô hình được kích hoạt SAU khi thấy #1/#2
  thất bại theo nến và IS theo tick dương; OOS đã bị nhìn 2 lần (theo nến) — kết quả tick trên OOS chỉ là bằng
  chứng yếu hơn một OOS sạch, cần dữ liệu recorder mới để xác nhận.
- **Kết quả (10/11/2026, laptop, `backtest.robustness --ticks data/history/trades --split 2026-09-01`, 10.989.044 tick
  khớp, báo giá giả ∓ 1 bước):**
  - #1: **KHÔNG CÓ EDGE** — OOS −1.302.700đ; p = 0,553; trượt 2 tick OOS −2.772.700đ. Độ nhạy ±20% và walk-forward
    KHÔNG nằm trong lý do trượt (đạt ngưỡng) — nhưng độ nhạy chạy trên IS, mà IS theo tick đã dương (+3,21tr), nên
    điều này chỉ nói "IS dương khá ổn định", không phải bằng chứng ngoài mẫu.
  - #2: **KHÔNG CÓ EDGE** — OOS −669.300đ; p = 0,551; trượt 2 tick OOS −1.829.300đ.
  - Kết luận: đổi sang mô hình tick làm IS từ lỗ thành lãi, nhưng **OOS vẫn lỗ ở cả hai** ⇒ lãi IS không mang sang
    giai đoạn mới. Cùng với nến (OOS #1 −2,29tr, #2 −1,46tr): 4/4 lần xem OOS đều lỗ. **ORB dừng ở đây — không tune
    thêm trên dữ liệu này, không paper/live.** OOS 01/09→06/11 đã bị nhìn 4 lần ⇒ không còn dùng để kết luận được;
    kiểm định sau phải dùng dữ liệu MỚI (recorder từ khi bật).
- Mô hình tick vẫn còn giả định: báo giá giả (dữ liệu tải về không có bid/ask), thanh khoản vô hạn, độ trễ
  300–500 ms. Tick thật có bid/ask từ recorder sẽ chính xác hơn.

| Ngày | Chiến lược | Tham số | Dữ liệu (IS/OOS) | Kết quả sau phí | p / p_adj | Ghi chú |
|---|---|---|---|---|---|---|
| 2026-11-08 | ORB baseline (`strategy/baseline_orb.py`) — cấu hình #1 | biên 15′, TP 1.5R, stop cạnh đối diện, ≤2 lệnh/ngày | IS 11/05→31/08 · OOS 01/09→06/11/2026 — **chạy 09/11/2026** | IS: 141 lệnh, −3.784.700đ; OOS: −2.292.700đ; trượt 2 tick OOS −3.652.700đ | OOS p = 0,609 | **KHÔNG CÓ EDGE** (robustness): OOS lỗ, p không đạt, 0% biến thể ±20% lãi, 25% đoạn walk-forward lãi. Theo tick (10/11): OOS −1.302.700đ, p 0,553 ⇒ vẫn KHÔNG CÓ EDGE. Phí ~46.700đ/lượt ⇒ 141 lượt IS ≈ 6,58tr phí ⇒ trước phí IS ≈ +2,8tr (≈ +0,2 điểm/lượt) — lợi thế thô (nếu có) nhỏ hơn chi phí. KHÔNG paper/live. |
| 2026-11-08 | ORB + lọc độ rộng biên (`strategy.orb.range_filter`) — cấu hình #2 | như #1 + chỉ giao dịch khi (đỉnh − đáy biên 15′) / ADR10 ∈ [0,10; 0,60] | như #1 — **chạy 09/11/2026** (`--set strategy.orb.range_filter.enabled=true`) | OOS: −1.459.300đ; trượt 2 tick OOS −2.779.300đ | OOS p = 0,588 | **KHÔNG CÓ EDGE** (robustness): OOS lỗ (ít hơn #1 nhưng vẫn lỗ), p không đạt, 0% biến thể ±20% lãi (gồm cả 3 tham số lọc), 25% đoạn walk-forward lãi. Theo tick (10/11): OOS −669.300đ, p 0,551 ⇒ vẫn KHÔNG CÓ EDGE. KHÔNG paper/live. |
