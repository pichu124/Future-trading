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

| Ngày | Chiến lược | Tham số | Dữ liệu (IS/OOS) | Kết quả sau phí | p / p_adj | Ghi chú |
|---|---|---|---|---|---|---|
| 2026-11-08 | ORB baseline (`strategy/baseline_orb.py`) — cấu hình #1 | biên 15′, TP 1.5R, stop cạnh đối diện, ≤2 lệnh/ngày | IS 11/05→31/08 · OOS 01/09→06/11/2026 — chưa chạy | — | — | đặc tả chốt trước khi xem dữ liệu; xem research/orb_baseline.md |
| 2026-11-08 | ORB + lọc độ rộng biên (`strategy.orb.range_filter`) — cấu hình #2 | như #1 + chỉ giao dịch khi (đỉnh − đáy biên 15′) / ADR10 ∈ [0,10; 0,60] | như #1 — chưa chạy | — | — | giả thuyết + đặc tả chốt trước khi xem dữ liệu; xem research/orb_range_filter.md |
