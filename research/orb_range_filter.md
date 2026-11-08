# ORB cấu hình #2 — lọc ngày theo độ rộng biên so với biên độ ngày trung bình

Code: `strategy/baseline_orb.py` (`RangeFilter`) · tham số: `settings.strategy.orb.range_filter` ·
test: `tests/test_strategy_orb_filter.py` · baseline: [orb_baseline.md](orb_baseline.md)

## Giả thuyết (ghi TRƯỚC khi nhìn dữ liệu)

Biên 15 phút đầu phiên so với biên độ ngày điển hình nói lên chất lượng của cú phá:
- biên **quá hẹp** (so với ADR) ⇒ phá biên chủ yếu là nhiễu, stop sát dễ bị quét;
- biên **quá rộng** ⇒ phần lớn biên độ của ngày đã đi hết, stop (cạnh đối diện) xa, R/R xấu.

Lọc bỏ hai đầu có thể cải thiện ORB baseline. Có thể SAI — mặc định coi là KHÔNG có edge cho tới khi
`backtest.robustness` nói khác.

## Đặc tả (chốt trước — không chỉnh theo kết quả)

| Thành phần | Quy tắc |
|---|---|
| Mọi phần khác | y hệt cấu hình #1 (biên 15′, vào khi đóng ngoài biên, stop cạnh đối diện, TP 1,5R, ≤ 2 lệnh/ngày) |
| ADR | trung bình (đỉnh − đáy) của **10** phiên gần nhất trước hôm nay, mọi bar của phiên (gồm ATO/ATC). Lịch sử back-adjust cộng điểm ⇒ đỉnh − đáy không đổi qua roll |
| Lọc | tỉ lệ = (đỉnh − đáy biên 15′) / ADR; chỉ giao dịch hôm đó khi **0,10 ≤ tỉ lệ ≤ 0,60** (biên tính là trong) |
| Dữ liệu thiếu | chưa đủ 10 phiên lịch sử hoặc ADR = 0 ⇒ không giao dịch hôm đó (backtest); live/paper từ chối khởi động nếu thiếu (live/history.py) |

Ngưỡng 0,10 / 0,60 và 10 phiên chọn theo lập luận (loại hai đuôi rõ rệt, cửa sổ ~2 tuần), **không** tối ưu
trên dữ liệu. Mọi thay đổi ngưỡng sau khi xem kết quả là một cấu hình MỚI (thêm dòng `research/log.md`).

## Chạy

1. Bật trong `config/settings.yaml`: `strategy.orb.range_filter.enabled: true`.
2. Cùng mốc chia IS/OOS với cấu hình #1 (đã chốt trong `research/log.md`) — không chọn mốc mới.
3. `python -m backtest.robustness --bars data/history/ohlc1m --split <ngày đầu OOS> --folds 4 --out reports/orb_rf_robustness.html`
   (`--n-trials` mặc định = số dòng `research/log.md` = 2 ⇒ p-value đã phạt chọn lọc cho cả hai cấu hình;
   độ nhạy ±20% gồm cả `adr_days`, `min_ratio`, `max_ratio`).
4. Ghi kết quả (kể cả xấu) vào `research/log.md`, dòng #2.

Lưu ý: 10 phiên đầu dữ liệu không có lệnh (chưa đủ lịch sử) ⇒ ít lệnh hơn baseline; nếu OOS < 30 lệnh,
`verdict` trả CHƯA ĐỦ BẰNG CHỨNG.
