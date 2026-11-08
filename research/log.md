# Nhật ký nghiên cứu

Mỗi lần thử một cấu hình/chiến lược mới, ghi một dòng — kể cả cấu hình thất bại. Tổng số dòng
là `n_trials` khi gọi `backtest.validation.apply_selection_penalty` (CLAUDE.md mục 6).

| Ngày | Chiến lược | Tham số | Dữ liệu (IS/OOS) | Kết quả sau phí | p / p_adj | Ghi chú |
|---|---|---|---|---|---|---|
| 2026-11-08 | ORB baseline (`strategy/baseline_orb.py`) — cấu hình #1 | biên 15′, TP 1.5R, stop cạnh đối diện, ≤2 lệnh/ngày | chưa chạy (thiếu dữ liệu + fees/risk null) | — | — | đặc tả chốt trước khi xem dữ liệu; xem research/orb_baseline.md |
| 2026-11-08 | ORB + lọc độ rộng biên (`strategy.orb.range_filter`) — cấu hình #2 | như #1 + chỉ giao dịch khi (đỉnh − đáy biên 15′) / ADR10 ∈ [0,10; 0,60] | chưa chạy (thiếu dữ liệu + fees/risk null); dùng cùng mốc chia với #1 | — | — | giả thuyết + đặc tả chốt trước khi xem dữ liệu; xem research/orb_range_filter.md |
