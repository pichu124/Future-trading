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

## Đăng ký trước #3, #4, #5 (10/11/2026 — TRƯỚC khi viết code / chạy trên dữ liệu)

Người dùng chọn thử cả ba ý tưởng ⇒ `n_trials` = 5 (2 ORB + 3 mới), `apply_selection_penalty` tính theo 5. Tham số
dưới đây CHỐT từ suy luận (chi phí ~0,9 điểm/lượt ⇒ ít lệnh, biên độ lớn), KHÔNG chỉnh sau khi xem kết quả. Đổi
bất kỳ con số nào = cấu hình MỚI (thêm dòng, tăng `n_trials`).

Chung cho cả ba:
- Nến 1 phút đã đóng; lệnh khớp ở tick kế tiếp; mô hình khớp **tick** (`--ticks`); phí/thuế/trượt như ORB.
- ADR10 = trung bình (đỉnh − đáy) 10 phiên TRƯỚC (lịch sử nối chuỗi back-adjust); thiếu lịch sử ⇒ không giao dịch.
- Giờ mở 09:00 = giá mở nến 09:00. Thoát theo giờ: EXIT ở nến 14:19 đóng (14:20); risk vẫn `flatten_time` 14:25.
- Luật rủi ro cứng giữ nguyên (lỗ ngày 15 điểm, 2 lượt/ngày, thua liên tiếp 2, 1 HĐ).
- Kiểm định: `backtest.robustness --ticks`, mốc chia 01/09/2026 như #1/#2, luật kết luận cố định. Mốc này đã bị
  nhìn qua kết quả ORB (không qua tham số của #3–#5 — các tham số này chốt không nhìn dữ liệu) ⇒ dùng làm kết
  luận chính; **CÓ EDGE vẫn phải xác nhận lại trên dữ liệu recorder MỚI** trước khi paper.

**#3 — Đà sáng → chiều** (`strategy/morning_momentum.py`). Giả thuyết: biến động phiên sáng dự báo hướng phiên chiều
(intraday momentum, Gao et al. 2018 — "first half-hour predicts last half-hour", ở đây áp cho phiên sáng/chiều).
- m = đóng nến 11:29 − mở 09:00. Quyết định khi nến 13:00 đóng (13:01; 11:30 đã nghỉ trưa, risk chặn mở).
- |m| ≥ 0,25 × ADR10 ⇒ vào theo dấu m. Stop = đóng nến 13:00 ∓ 0,30 × ADR10. Không chốt lời. Tối đa 1 lệnh/ngày.
- Tham số kiểm độ nhạy: `adr_days` 10, `min_move_adr` 0,25, `stop_adr` 0,30.

**#4 — Ngày xu hướng** (`strategy/trend_day.py`). Giả thuyết: giờ đầu đi mạnh (so với ADR) ⇒ ngày xu hướng, theo đến cuối phiên.
- m = đóng nến 09:59 − mở 09:00. Quyết định khi nến 09:59 đóng (10:00).
- |m| ≥ 0,35 × ADR10 ⇒ vào theo dấu m. Stop = đóng nến 09:59 ∓ 0,30 × ADR10. Không chốt lời. Tối đa 1 lệnh/ngày.
- Tham số kiểm độ nhạy: `adr_days` 10, `min_move_adr` 0,35, `stop_adr` 0,30.

**#5 — Đảo chiều về VWAP** (`strategy/vwap_reversion.py`). Giả thuyết: lệch xa VWAP trong ngày (so với ADR) ⇒ quay về VWAP.
- VWAP từ nến 09:00: Σ((H+L+C)/3 × KL) / ΣKL (bỏ nến KL = 0). Quyết định ở nến đóng từ 09:30 tới 14:00 (nến 09:29…13:59).
- d = đóng − VWAP; |d| ≥ 0,40 × ADR10 và đang phẳng ⇒ vào NGƯỢC dấu d. Chốt lời = VWAP lúc ra tín hiệu.
  Stop = đóng ± 0,25 × ADR10 (xa thêm về phía lệch). Tối đa 2 lệnh/ngày.
- Tham số kiểm độ nhạy: `adr_days` 10, `entry_dev_adr` 0,40, `stop_adr` 0,25.

Thứ tự làm: #3 → #4 → #5, mỗi chiến lược một module (test trước), chạy robustness xong mới làm cái tiếp theo.

| Ngày | Chiến lược | Tham số | Dữ liệu (IS/OOS) | Kết quả sau phí | p / p_adj | Ghi chú |
|---|---|---|---|---|---|---|
| 2026-11-08 | ORB baseline (`strategy/baseline_orb.py`) — cấu hình #1 | biên 15′, TP 1.5R, stop cạnh đối diện, ≤2 lệnh/ngày | IS 11/05→31/08 · OOS 01/09→06/11/2026 — **chạy 09/11/2026** | IS: 141 lệnh, −3.784.700đ; OOS: −2.292.700đ; trượt 2 tick OOS −3.652.700đ | OOS p = 0,609 | **KHÔNG CÓ EDGE** (robustness): OOS lỗ, p không đạt, 0% biến thể ±20% lãi, 25% đoạn walk-forward lãi. Theo tick (10/11): OOS −1.302.700đ, p 0,553 ⇒ vẫn KHÔNG CÓ EDGE. Phí ~46.700đ/lượt ⇒ 141 lượt IS ≈ 6,58tr phí ⇒ trước phí IS ≈ +2,8tr (≈ +0,2 điểm/lượt) — lợi thế thô (nếu có) nhỏ hơn chi phí. KHÔNG paper/live. |
| 2026-11-08 | ORB + lọc độ rộng biên (`strategy.orb.range_filter`) — cấu hình #2 | như #1 + chỉ giao dịch khi (đỉnh − đáy biên 15′) / ADR10 ∈ [0,10; 0,60] | như #1 — **chạy 09/11/2026** (`--set strategy.orb.range_filter.enabled=true`) | OOS: −1.459.300đ; trượt 2 tick OOS −2.779.300đ | OOS p = 0,588 | **KHÔNG CÓ EDGE** (robustness): OOS lỗ (ít hơn #1 nhưng vẫn lỗ), p không đạt, 0% biến thể ±20% lãi (gồm cả 3 tham số lọc), 25% đoạn walk-forward lãi. Theo tick (10/11): OOS −669.300đ, p 0,551 ⇒ vẫn KHÔNG CÓ EDGE. KHÔNG paper/live. |
| 2026-11-10 | Đà sáng → chiều (`strategy/morning_momentum.py`) — cấu hình #3 | m = C(11:29) − O(09:00); \|m\| ≥ 0,25·ADR10 ⇒ theo m lúc 13:01; stop 0,30·ADR10; EXIT 14:20; 1 lệnh/ngày | IS 11/05→31/08 · OOS 01/09→06/11/2026 (tick) — **chạy 10/11/2026** (Desktop) | IS: 33 lệnh, +8.798.900đ (PF 1,91); OOS: 25 lệnh, +1.492.500đ (PF 1,17); trượt 2 tick OOS +992.500đ | IS p 0,093 (×5 = 0,465); OOS p 0,389 | **CHƯA ĐỦ BẰNG CHỨNG** (25 < 30 lệnh OOS). Độ nhạy 6/6 biến thể IS lãi; walk-forward 2/4 đoạn lãi. Lãi IS dồn vào 24/06→06/08 (+9,94tr). OOS dương nhưng yếu (Sharpe/lệnh 0,06). → theo dõi tiếp trên dữ liệu MỚI, KHÔNG đổi tham số. |
| 2026-11-10 | Ngày xu hướng (`strategy/trend_day.py`) — cấu hình #4 | m = C(09:59) − O(09:00); \|m\| ≥ 0,35·ADR10 ⇒ theo m lúc 10:00; stop 0,30·ADR10; EXIT 14:20; 1 lệnh/ngày | như #3 — **chạy 10/11/2026** | IS: 11 lệnh, +2.566.300đ; OOS: 13 lệnh, +1.992.900đ (PF 1,45); trượt 2 tick OOS +1.732.900đ | OOS p 0,373 | **CHƯA ĐỦ BẰNG CHỨNG** (13 < 30). Độ nhạy chỉ 4/6 biến thể lãi (< 75%: min_move_adr 0,28 ⇒ −1,95tr, 0,42 ⇒ −0,81tr ⇒ đỉnh nhọn); walk-forward 3/4 đoạn lãi. Quá ít lệnh (24 trong 6 tháng). → theo dõi tiếp, KHÔNG đổi tham số. |
| 2026-11-10 | Đảo chiều VWAP (`strategy/vwap_reversion.py`) — cấu hình #5 | \|C − VWAP\| ≥ 0,40·ADR10 (09:30–14:00) ⇒ ngược chiều, TP = VWAP, stop 0,25·ADR10 xa thêm; EXIT 14:20; ≤ 2 lệnh/ngày | như #3 — **chạy 10/11/2026** | IS: 46 lệnh, −10.788.200đ; OOS: 28 lệnh, −5.157.600đ (PF 0,55, win 32%) | OOS p 0,903 | **CHƯA ĐỦ (28 < 30) nhưng rõ ràng KHÔNG CÓ EDGE**: IS và OOS đều lỗ, 6/6 biến thể IS lỗ, 1/4 đoạn lãi, lỗ cả trước phí (−38,5 điểm OOS). DỪNG #5. KHÔNG thử phiên bản đảo ngược (= đào dữ liệu). | Đăng ký trước 10/11/2026. Làm rõ thực thi (10/11, trước khi chạy): bỏ nến đóng đúng 11:30 (risk chặn mở trong giờ trưa — tín hiệu chỉ phí lượt). |

### Kết quả #3–#5 và kế hoạch (10/11/2026)

- #5 bỏ. #3, #4: OOS dương nhưng mẫu nhỏ, p xa ngưỡng (cần p × 5 < 0,1 ⇔ p < 0,02). Thành thật: nhiều khả năng là
  nhiễu; nhưng là ứng viên duy nhất còn lại.
- **Không đổi tham số.** OOS của #3/#4 tiếp tục kéo dài từ 01/09 bằng dữ liệu MỚI (downloader bù 07/11→ nay +
  recorder từ khi bật; recorder có bid/ask thật). Chạy lại `backtest.robustness` định kỳ với cùng luật; kết luận
  chính thức khi OOS ≥ 30 lệnh (#3 cần thêm ~5 lệnh ≈ 2–3 tuần; #4 cần thêm ~17 ≈ 2–3 tháng).
- Song song có thể chạy PAPER #3 trên laptop để kiểm vận hành (feed, lệnh, stop, Telegram) — paper KHÔNG phải bằng
  chứng edge thay cho robustness.

### Đăng ký trước: giai đoạn kiểm tra THỨ HAI = dữ liệu TRƯỚC 11/05/2026 (ghi 11/11/2026, TRƯỚC khi tải/xem)

- DNSE lưu ~7 tháng cuộn ⇒ tải được ~16/03→08/05/2026 (chưa ai xem; tham số #3/#4 chốt 10/11 không nhìn giai đoạn này)
  ⇒ là OOS sạch thứ hai cho #3 và #4 (KHÔNG cho ORB #1/#2, #5 — đã dừng).
- Chạy: `backtest.engine --ticks … --from <ngày đầu có dữ liệu> --to 2026-05-08 --strategy <#3|#4>`, cùng phí/trượt/mô
  hình tick, KHÔNG đổi tham số. 10 phiên đầu dùng làm lịch sử ADR (không giao dịch được) — chấp nhận.
- Luật kết luận (cố định, cho từng chiến lược): gộp lệnh OOS = (giai đoạn trước) ∪ (01/09/2026 → mới nhất);
  CÓ EDGE ⇔ (a) ≥ 30 lệnh gộp; (b) P&L sau phí > 0 ở CẢ HAI giai đoạn; (c) p sign-flip của chuỗi gộp × 5 < 0,1;
  (d) P&L gộp vẫn > 0 khi trượt 2 tick. Thiếu (a) ⇒ CHƯA ĐỦ; trượt bất kỳ (b)(c)(d) ⇒ KHÔNG CÓ EDGE.
- CÓ EDGE vẫn phải chạy paper ổn định trước khi tính đến live; cấu hình #3b (1–3 HĐ, chốt từng phần) là thử nghiệm MỚI
  (n_trials 6), chỉ đăng ký sau khi có kết luận này.

