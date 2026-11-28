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
| 2026-11-10 | Đà sáng → chiều (`strategy/morning_momentum.py`) — cấu hình #3 | m = C(11:29) − O(09:00); \|m\| ≥ 0,25·ADR10 ⇒ theo m lúc 13:01; stop 0,30·ADR10; EXIT 14:20; 1 lệnh/ngày | IS 11/05→31/08 · OOS 01/09→06/11/2026 (tick) — **chạy 10/11/2026** (Desktop) | IS: 33 lệnh, +8.798.900đ (PF 1,91); OOS: 25 lệnh, +1.492.500đ (PF 1,17); trượt 2 tick OOS +992.500đ | IS p 0,093 (×5 = 0,465); OOS p 0,389 | **CHƯA ĐỦ** (25 < 30) ⇒ **11/11 OOS gộp (thêm 25/03→08/05): KHÔNG CÓ EDGE** — giai đoạn trước −4,26tr, gộp −2,19tr, p 0,656. Độ nhạy 6/6 biến thể IS lãi; walk-forward 2/4 đoạn lãi. Lãi IS dồn vào 24/06→06/08 (+9,94tr). OOS dương nhưng yếu (Sharpe/lệnh 0,06). → theo dõi tiếp trên dữ liệu MỚI, KHÔNG đổi tham số. |
| 2026-11-10 | Ngày xu hướng (`strategy/trend_day.py`) — cấu hình #4 | m = C(09:59) − O(09:00); \|m\| ≥ 0,35·ADR10 ⇒ theo m lúc 10:00; stop 0,30·ADR10; EXIT 14:20; 1 lệnh/ngày | như #3 — **chạy 10/11/2026** | IS: 11 lệnh, +2.566.300đ; OOS: 13 lệnh, +1.992.900đ (PF 1,45); trượt 2 tick OOS +1.732.900đ | OOS p 0,373 | **CHƯA ĐỦ BẰNG CHỨNG** (13 < 30). Độ nhạy chỉ 4/6 biến thể lãi (< 75%: min_move_adr 0,28 ⇒ −1,95tr, 0,42 ⇒ −0,81tr ⇒ đỉnh nhọn); walk-forward 3/4 đoạn lãi. Quá ít lệnh (24 trong 6 tháng). → theo dõi tiếp, KHÔNG đổi tham số. |
| 2026-11-10 | Đảo chiều VWAP (`strategy/vwap_reversion.py`) — cấu hình #5 | \|C − VWAP\| ≥ 0,40·ADR10 (09:30–14:00) ⇒ ngược chiều, TP = VWAP, stop 0,25·ADR10 xa thêm; EXIT 14:20; ≤ 2 lệnh/ngày | như #3 — **chạy 10/11/2026** | IS: 46 lệnh, −10.788.200đ; OOS: 28 lệnh, −5.157.600đ (PF 0,55, win 32%) | OOS p 0,903 | **CHƯA ĐỦ (28 < 30) nhưng rõ ràng KHÔNG CÓ EDGE**: IS và OOS đều lỗ, 6/6 biến thể IS lỗ, 1/4 đoạn lãi, lỗ cả trước phí (−38,5 điểm OOS). DỪNG #5. KHÔNG thử phiên bản đảo ngược (= đào dữ liệu). | Đăng ký trước 10/11/2026. Làm rõ thực thi (10/11, trước khi chạy): bỏ nến đóng đúng 11:30 (risk chặn mở trong giờ trưa — tín hiệu chỉ phí lượt). |
| 2026-11-12 | Lấp gap mở cửa (`strategy/gap_fade.py`) — cấu hình #6 | g = đóng nến 09:15 − đóng phiên trước (back-adjust); \|g\| ≥ 0,30·ADR10 ⇒ vào NGƯỢC g lúc 09:16; TP = đóng phiên trước; stop 0,30·ADR10; EXIT 11:25; 1 lệnh/ngày | IS nến SSI 23/07→31/12/2025 · OOS-A nến SSI 01/01→24/03/2026 · OOS-B tick DNSE 25/03/2026→ — **IS chạy 12/11/2026** (cloud, nến SSI cắt tới 31/12/2025) | IS: 8 lệnh, −2.763.600đ (PF 0,53); trượt 2 tick −2.893.600đ | IS p 0,776 | **KHÔNG CÓ EDGE — trượt cổng IS, KHÔNG mở OOS.** 0/6 biến thể độ nhạy lãi (min_gap 0,24 ⇒ 17 lệnh −6,06tr), 0/4 đoạn walk-forward lãi. Gap ≥ 0,30·ADR hiếm (10/107 ngày; ADR 2025 ~39 điểm). Luật: mục "Đăng ký trước #6". |
| 2026-11-16 | Đảo chiều qua ngày (`strategy/prev_day_reversal.py`) — cấu hình #7 | m = đóng − mở phiên trước; \|m\| ≥ 0,50·ADR10 ⇒ vào NGƯỢC m lúc 09:16; stop 0,40·ADR10; EXIT 14:20; 1 lệnh/ngày | IS nến SSI 22/07→31/12/2025 (kho tải 11/11, laptop) — **IS chạy 16/11/2026**; OOS KHÔNG mở | IS: 41 lệnh, −15.364.700đ (thắng 29%, PF 0,58); trượt 2 tick −17.074.700đ | — | **KHÔNG CÓ EDGE — trượt cổng IS** (cả 3 điều kiện): 0/6 biến thể lãi, 1/4 đoạn walk-forward lãi. Lỗ cả trước phí. Đăng ký trước 16/11/2026 (mục "Đăng ký trước #7, #8, #9"). |
| 2026-11-16 | Basis quá đà (`strategy/basis_reversion.py`) — cấu hình #8 | d = basis − trung bình basis 30′ trước; \|d\| ≥ 0,10·ADR10 (09:30–14:00) ⇒ vào NGƯỢC d; stop 0,15·ADR10; thoát khi d về 0 hoặc giữ 30′; EXIT 14:20; ≤ 2 lệnh/ngày | như #7 + nến 1 phút chỉ số VN30 (SSI) — **KHÔNG KIỂM ĐƯỢC** | — | — | Nến chỉ số SSI chỉ có từ 15/10/2026, DNSE không có OHLC lịch sử ⇒ lỗi dữ liệu, không phải kết luận; chưa viết code. Đăng ký trước 16/11/2026. |
| 2026-11-16 | Đà cuối phiên (`strategy/late_momentum.py`) — cấu hình #9 | r = đóng nến 13:49 − đóng phiên trước; \|r\| ≥ 0,30·ADR10 ⇒ vào THEO r lúc 13:50; stop 0,20·ADR10; EXIT 14:20; 1 lệnh/ngày | như #7 — **IS chạy 16/11/2026**; OOS KHÔNG mở | IS: 40 lệnh, −2.168.000đ (thắng 40%, PF 0,88); trượt 2 tick −2.968.000đ | — | **KHÔNG CÓ EDGE — trượt cổng IS** (cả 3 điều kiện): 3/6 biến thể lãi (< 75%), 1/4 đoạn walk-forward lãi. Trước phí ≈ hoà. Đăng ký trước 16/11/2026. |
| 2026-11-27 | Đo tín hiệu: dòng tiền theo khối lượng (`strategy/signals.py:volume_flow`) — cấu hình #10 | F = Σ dấu(đóng − mở) × KL / Σ KL trên 30 nến gần nhất của hôm nay; kỳ 60 nến; chiều THUẬN (+1); ngưỡng độ lớn = phân vị 85% của \|F\| trên quan sát IS | nến DNSE (`data/history/ohlc1m`): IS 11/05→31/08/2026 · OOS 01/09→30/10/2026 — **IS chạy 28/11/2026** (desktop) | IS: 75 ngày, 10.738 quan sát; tương quan +0,0047 (t 0,09); lợi thế thô 0,10 điểm − chi phí 0,87 = −0,77 | p 0,470 / p_adj 1,000 | **KHÔNG CÓ TÍN HIỆU** — dừng, KHÔNG mở OOS. Đăng ký trước 27/11/2026 |
| 2026-11-27 | Đo tín hiệu: xu hướng trong ngày (`strategy/signals.py:intraday_trend`) — cấu hình #11 | T = (đóng nến hiện tại − mở nến đầu tiên hôm nay) / ADR10; kỳ 60 nến; chiều THUẬN (+1); ngưỡng độ lớn = phân vị 85% của \|T\| trên quan sát IS | nến SSI (`data/ssi/ohlc1m`): IS 22/07→31/12/2025 · OOS-A 01/01→24/03/2026 — **IS chạy 28/11/2026** (desktop) | IS: 89 ngày, 14.329 quan sát; tương quan −0,0206 (t −0,32, ngược chiều); lợi thế thô 0,27 điểm − chi phí 0,87 = −0,59 | p 0,632 / p_adj 1,000 | **KHÔNG CÓ TÍN HIỆU** — dừng, KHÔNG mở OOS (OOS-A vẫn sạch). Đăng ký trước 27/11/2026 |
| 2026-11-27 | Đo tín hiệu: basis lệch khỏi mức quen (`strategy/signals.py:basis_deviation`) — cấu hình #12 | D = (basis − trung bình basis 30 phút khớp được gần nhất trước đó, hôm nay) / ADR10, basis = đóng HĐ − đóng VN30 cùng phút; kỳ 30 nến; chiều NGHỊCH (−1); ngưỡng độ lớn \|D\| ≥ 0,10 | nến SSI HĐ + chỉ số VN30: IS 15/10→31/12/2026 · OOS 04/01→31/03/2027 | CHƯA CHẠY (chờ đủ dữ liệu) | — | đăng ký trước 27/11/2026 |

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

### Kết quả giai đoạn kiểm tra thứ hai (11/11/2026, laptop, `backtest.pooled`, kho 25/03→11/11/2026)

- **#3 Đà sáng → chiều: KHÔNG CÓ EDGE** (theo luật đăng ký trước). Cửa sổ 25/03→08/05: 12 lệnh, **−4.260.400đ**;
  cửa sổ 01/09→11/11: 28 lệnh, +2.072.400đ; gộp 40 lệnh, −2.188.000đ, p 0,656 (×5 = 1,0); trượt 2 tick gộp
  −2.988.000đ. Trượt (b), (c), (d). Lãi IS 05–08 không lặp lại ở giai đoạn trước đó (03–05) ⇒ phù hợp với nhiễu.
  DỪNG #3 như một ứng viên có edge; KHÔNG tune lại. (Paper #3 vẫn chạy được để kiểm VẬN HÀNH, P&L paper không mang
  ý nghĩa bằng chứng.)


### Đăng ký trước: giai đoạn kiểm tra THỨ BA = nến SSI 23/07/2025 → 24/03/2026, chỉ cho #4 (ghi 12/11/2026)

Ghi TRƯỚC khi chạy `backtest.pooled` cho #4 trên DNSE và TRƯỚC khi chạy bất kỳ chiến lược nào trên nến SSI.
Đến lúc ghi, kho SSI mới chỉ được dùng để đối chiếu chất lượng với DNSE (132 ngày trùng 25/03→11/11/2026,
`scripts/compare_bars.py`). Giai đoạn 2025 → 24/03/2026 chưa ai chạy chiến lược nào lên ⇒ là OOS sạch cho #4.

- **Chỉ áp dụng cho #4 Ngày xu hướng**, đúng tham số chốt 10/11/2026 (`decision_bar 09:59`, `min_move_adr 0,35`,
  `stop_adr 0,30`, EXIT 14:20, 1 HĐ, 1 lệnh/ngày). Không phải cấu hình mới ⇒ `n_trials` giữ **5** (không thêm dòng
  bảng). #1, #2, #3, #5 đã KHÔNG CÓ EDGE ⇒ KHÔNG chạy lại trên SSI để "cứu".
- **Thứ tự cố định:**
  1. Chạy #4 trên DNSE theo luật giai đoạn thứ hai (cửa sổ 25/03→08/05 và 01/09→mới nhất, tick) và ghi kết quả.
  2. DNSE ra **KHÔNG CÓ EDGE** ⇒ DỪNG #4, KHÔNG chạy SSI (đã trượt mà tìm thêm dữ liệu = tự lừa mình).
  3. DNSE ra **CHƯA ĐỦ** hoặc **CÓ EDGE** ⇒ chạy bước kiểm tra dữ liệu rồi kết luận 3 cửa sổ dưới đây. Kết luận
     3 cửa sổ là kết luận CUỐI, thay kết luận DNSE (CÓ EDGE trên DNSE mà trượt 3 cửa sổ ⇒ KHÔNG CÓ EDGE).
- **Cửa sổ W0 (SSI):** 23/07/2025 → 24/03/2026 (kết thúc trước ngày đầu có tick DNSE, không trùng W1). Chạy chế độ
  NẾN (`--bars data/ssi/ohlc1m`): bid/ask giả ∓ `synthetic_spread_ticks`, trượt `slippage_ticks`, stop khớp ở cực
  trị nến, nến chứa cả TP lẫn SL ⇒ SL thắng; cùng phí trong `settings.yaml` (biểu phí 01/05/2026 áp cho cả 2025).
  F1M + roll theo `contract.roll_days_before_expiry` như engine; ~10 phiên đầu làm lịch sử ADR, không giao dịch
  được — chấp nhận. Chế độ nến bi quan hơn tick ~0,2 điểm/lệnh (≈ 20.000đ) ⇒ thiên về KẾT LUẬN KHÔNG CÓ EDGE;
  chấp nhận (thà bỏ sót edge còn hơn tin nhầm).
- **Kiểm tra dữ liệu TRƯỚC khi kết luận (không phải bằng chứng edge):** chạy #4 chế độ nến trên SSI ở CHÍNH hai
  cửa sổ DNSE (W1, W2) và so với lệnh tick DNSE: ≥ 80% lệnh DNSE phải có lệnh SSI cùng ngày, cùng chiều. Không đạt ⇒
  đường ống SSI (mã HĐ, roll, lịch 2025, giờ) đáng ngờ ⇒ KHÔNG kết luận từ W0, tìm lỗi trước. Đồng thời báo số ngày
  SSI thiếu so với lịch (`missing_trading_days`, đã biết ~8 ngày quanh roll) và ngày có dữ liệu mà lịch ghi nghỉ
  (lịch nghỉ 2025 chưa kiểm chéo); > 10 ngày thiếu trong W0 ⇒ cũng dừng để kiểm tra.
- **Luật kết luận 3 cửa sổ** (W0 nến SSI ∪ W1 25/03→08/05/2026 tick ∪ W2 01/09/2026→mới nhất tick), gộp lệnh theo
  ngày VÀO: CÓ EDGE ⇔ (a) ≥ 30 lệnh gộp; (b) P&L sau phí > 0 ở **CẢ BA** cửa sổ (0 lệnh = trượt); (c) p sign-flip
  chuỗi gộp × 5 < 0,1; (d) P&L gộp > 0 khi trượt 2 tick (W0 cũng chạy lại với trượt 2 tick). Thiếu (a) ⇒ CHƯA ĐỦ (W2
  tiếp tục kéo dài bằng dữ liệu mới, KHÔNG đổi tham số); trượt bất kỳ (b)(c)(d) ⇒ KHÔNG CÓ EDGE, dừng #4.
- Báo thêm (không đổi kết luận): kết quả riêng W0; P&L theo tháng trong W0; chênh nến−tick trung bình/lệnh ở bước
  kiểm tra dữ liệu.
- **Công cụ:** `backtest.pooled` hiện chỉ nhận `--ticks` ⇒ mở rộng nhận thêm một nguồn nến cho cửa sổ riêng
  (viết test trước), rồi mới chạy. Không đổi gì trong engine/chiến lược.
- CÓ EDGE vẫn phải qua paper ổn định trước khi tính đến live (như giai đoạn thứ hai).

### Kết quả #4 giai đoạn kiểm tra thứ hai (12/11/2026, laptop, `backtest.pooled`, kho 25/03→11/11/2026)

- **#4 Ngày xu hướng: CHƯA ĐỦ BẰNG CHỨNG** theo chữ của luật (20 lệnh gộp < 30). Cửa sổ 25/03→08/05: 4 lệnh,
  **−2.436.800đ**; cửa sổ 01/09→nay: 16 lệnh, +3.062.800đ; gộp 20 lệnh, +626.000đ, p 0,464 (×5 = 1,0); trượt 2 tick
  gộp +226.000đ.
- **Hệ quả logic ⇒ DỪNG #4, không chạy SSI.** Điều kiện (b) "P&L > 0 ở MỌI cửa sổ" đã trượt VĨNH VIỄN: cửa sổ
  25/03→08/05 là dữ liệu cố định, đã lỗ, và có mặt trong cả luật 2 cửa sổ lẫn luật 3 cửa sổ (SSI). Vì vậy không
  còn đường nào ra CÓ EDGE: thêm SSI hay chờ dữ liệu mới chỉ có thể dẫn tới CHƯA ĐỦ (< 30 lệnh) hoặc KHÔNG CÓ EDGE
  (≥ 30 lệnh). Chạy tiếp chỉ tốn công mà không đổi được quyết định ⇒ coi #4 như KHÔNG CÓ EDGE. Đây là kết luận
  thận trọng hơn chữ của luật, không phải nới luật. Ngoài ra p gộp 0,464 cũng ở rất xa ngưỡng 0,02.
- Luật giai đoạn thứ ba (SSI) đăng ký 12/11 vì thế **không dùng cho #4**. Nến SSI 23/07/2025→24/03/2026 vẫn
  CHƯA ai chạy chiến lược nào ⇒ còn sạch cho chiến lược MỚI (phải đăng ký trước, n_trials tăng).
- **Tổng kết 5 cấu hình (n_trials = 5):** ORB #1, #2 KHÔNG CÓ EDGE (nến và tick); #3 KHÔNG CÓ EDGE (gộp); #4 dừng
  (như trên); #5 KHÔNG CÓ EDGE thực tế. Chưa có ứng viên nào ⇒ `max_contracts` giữ 1, scale-out (#3b) chưa đăng ký,
  không paper/live vì lý do edge (paper #3 chỉ để kiểm vận hành).

## Đăng ký trước #6 — Lấp gap mở cửa (12/11/2026, TRƯỚC khi viết code / chạy trên dữ liệu)

Người dùng chọn ý tưởng 1 (12/11/2026) ⇒ **`n_trials` = 6**. Tham số CHỐT từ suy luận, không nhìn dữ liệu; đổi bất
kỳ con số nào = cấu hình MỚI (thêm dòng, tăng `n_trials`).

**Giả thuyết.** VN30F nhiều nhà đầu tư cá nhân ⇒ đầu phiên phản ứng quá đà với tin qua đêm; khoảng chênh giữa giá
đầu phiên và giá đóng phiên trước (gap) hay bị lấp lại một phần trong buổi sáng. Khác #1–#5: không theo đà, không
dùng VWAP — dùng giá đóng HÔM TRƯỚC làm mốc.

**Đặc tả** (`strategy/gap_fade.py`, `settings.strategy.gap_fade`):
- Đóng trước (C₋₁) = giá đóng nến CUỐI của đúng phiên giao dịch liền trước (theo `config/holidays.yaml`) trong lịch
  sử back-adjust (gồm nến ATC nếu dữ liệu có). Thiếu phiên liền trước trong lịch sử (mất dữ liệu) ⇒ không giao dịch
  hôm đó (không lấy phiên cũ hơn).
- ADR10 như #3–#5 (`adr_before`, 10 phiên trước); thiếu ⇒ không giao dịch.
- Quyết định MỘT lần khi nến 09:15 đóng (09:16 — sau `no_entry_open_minutes` 15′). g = đóng nến 09:15 − C₋₁.
  |g| ≥ **0,30 × ADR10** và đang phẳng ⇒ vào NGƯỢC dấu g (gap lên ⇒ Short, gap xuống ⇒ Long), 1 HĐ.
  Thiếu nến 09:15 ⇒ không giao dịch hôm đó.
- Chốt lời = C₋₁ (lấp đủ gap). Stop = đóng nến 09:15 ± **0,30 × ADR10** (xa thêm về phía gap), làm tròn bước giá.
- Còn vị thế khi nến 11:24 đóng (11:25) ⇒ EXIT (trước nghỉ trưa 11:30). Tối đa 1 lệnh/ngày.
- Luật rủi ro cứng giữ nguyên (ngày đáo hạn không vào lệnh, lỗ ngày 15 điểm…).
- Tham số kiểm độ nhạy ±20%: `adr_days` 10, `min_gap_adr` 0,30, `stop_adr` 0,30.

**Dữ liệu & thứ tự (cố định):**
1. **IS** = nến SSI 23/07/2025 → 31/12/2025, chế độ NẾN (bid/ask giả ∓ 1 bước, trượt 1 bước, stop khớp ở cực trị
   nến, nến chạm cả TP lẫn SL ⇒ SL thắng). Chạy với dữ liệu CẮT tới 31/12/2025 (không nạp phần sau). Báo: số lệnh,
   P&L sau phí, độ nhạy ±20% (6 biến thể), walk-forward 4 đoạn.
   **Cổng IS:** P&L IS sau phí ≤ 0 **hoặc** < 75% biến thể độ nhạy lãi **hoặc** < 50% đoạn walk-forward lãi
   ⇒ KHÔNG CÓ EDGE, DỪNG — KHÔNG mở OOS.
2. Qua cổng IS ⇒ **kiểm tra dữ liệu** (như luật SSI ghi 12/11): chạy #6 nến SSI trên các ngày của OOS-B và so với
   lệnh tick DNSE cùng ngày: ≥ 80% lệnh DNSE có lệnh SSI cùng ngày, cùng chiều; SSI thiếu > 10 ngày so với lịch trong
   IS/OOS-A ⇒ dừng tìm lỗi đường ống trước. (Bước này nhìn OOS-B nhưng không dùng để kết luận; thực hiện cùng lúc
   với bước 3, không có cổng chọn lọc nào ở giữa.)
3. **OOS gộp** = OOS-A (nến SSI 01/01 → 24/03/2026, kết thúc trước ngày đầu có tick DNSE) ∪ OOS-B (tick DNSE
   25/03/2026 → mới nhất, ~10 phiên đầu làm lịch sử ADR). Lệnh tính theo ngày VÀO.
   **CÓ EDGE** ⇔ (a) ≥ 30 lệnh gộp; (b) P&L sau phí > 0 ở CẢ HAI cửa sổ (0 lệnh = trượt); (c) p sign-flip chuỗi
   gộp × 6 < 0,1 (⇔ p < 0,0167); (d) P&L gộp > 0 khi trượt 2 tick. Thiếu (a) ⇒ CHƯA ĐỦ (OOS-B kéo dài bằng dữ liệu
   mới, không đổi tham số); trượt bất kỳ (b)(c)(d) ⇒ KHÔNG CÓ EDGE, dừng #6.
- Báo thêm (không đổi kết luận): P&L theo tháng, theo chiều (Long/Short), tỉ lệ chạm TP / stop / thoát 11:25.
- Ghi chú trung thực: OOS-B (25/03→nay) đã được xem qua kết quả #1–#5 — với chiến lược KHÁC, không với giả thuyết
  gap; vẫn coi là OOS nhưng yếu hơn OOS-A (chưa ai xem). Nến SSI không có bid/ask ⇒ IS và OOS-A bi quan hơn tick
  (~0,2 điểm/lệnh).
- CÓ EDGE vẫn phải paper ổn định trước khi tính đến live; khi đó mới đăng ký biến thể 1–3 HĐ chốt từng phần.

### Kết quả #6 bước 1 — IS (12/11/2026, `backtest.robustness --bars` nến SSI 22/07→31/12/2025, `--split 2025-12-31`)

- Dữ liệu nạp CẮT tới 31/12/2025 (126 file) ⇒ OOS-A/OOS-B KHÔNG được nạp, chưa ai nhìn. `--split 2025-12-31` chỉ để
  công cụ chạy được (cần ≥ 1 ngày "OOS"); 31/12 thuộc IS, không có lệnh.
- **IS: 8 lệnh, −2.763.600đ** (thắng 3/8, PF 0,53; 5 stop, 3 chốt lời); Long 4 lệnh −2,42tr, Short 4 lệnh −0,35tr;
  trượt 2 tick −2,89tr. Walk-forward 0/4 đoạn lãi. Độ nhạy 0/6 biến thể lãi (adr_days 8/12: −1,88/−1,57tr;
  min_gap_adr 0,24/0,36: 17 lệnh −6,06tr / 5 lệnh −3,51tr; stop_adr 0,24/0,36: −2,15/−3,81tr).
- Kiểm tra không phải lỗi: đếm độc lập trên IS (giá đóng 09:15 so với nến cuối 14:45 phiên trước, bỏ ngày roll) ⇒
  10/107 ngày có |g| ≥ 0,30·ADR (trung vị |g| 0,12·ADR; ADR TB 39 điểm); 8 lệnh khớp với số đó sau khi trừ ngày bị
  luật chặn. Dữ liệu: 1 ngày giao dịch thiếu hẳn (17/10/2025), 16/10 thiếu HĐ giao dịch, 1 roll không đo được chênh.
- **Kết luận theo cổng IS đăng ký trước: KHÔNG CÓ EDGE ⇒ DỪNG #6, KHÔNG mở OOS.** 8 lệnh là mẫu rất nhỏ (IS gần
  như chỉ là nhiễu), nhưng luật đã chốt; nới ngưỡng gap để có thêm lệnh = cấu hình MỚI, và biến thể 0,24 (17 lệnh)
  còn lỗ nặng hơn ⇒ không có lý do gì để thử tiếp hướng này.
- Hệ quả: OOS-A (nến SSI 01/01→24/03/2026) VẪN CHƯA ai nhìn ⇒ còn sạch cho chiến lược sau. `n_trials` = 6.

## Nhật ký regime shadow của nhóm AI — đăng ký trước (16/11/2026, TRƯỚC khi có nhận định nào được ghi)

Không phải cấu hình chiến lược ⇒ KHÔNG thêm dòng bảng, `n_trials` giữ **6**. Đây là thí nghiệm đo riêng: nhóm AI
tranh luận mỗi sáng có dự báo được kiểu phiên hôm nay tốt hơn một baseline thống kê không. Quy trình:
`research/regime_shadow.md`; công cụ: `scripts/regime_call.py`, `scripts/score_regime_calls.py`; số ở settings
`research.regime_shadow`. Người dùng chọn chạy bằng tác vụ Claude hẹn giờ trên cloud (16/11/2026).

- **Vì sao làm kiểu "shadow":** nhận định ghi TRƯỚC giờ ATO (script đóng dấu giờ, từ chối từ 08:45, không ghi đè;
  thời điểm push lên GitHub là bằng chứng thứ hai) ⇒ là dự báo thật về tương lai, tránh được lỗi LLM "nhớ" lịch sử
  khi backtest. Bot không đọc các file này (CLAUDE.md mục 8 giữ nguyên; `tests/test_regime_shadow_isolation.py`).
- **Ba câu hỏi, nhãn thực tế** (nến 1 phút của HĐ bot giao dịch hôm đó, 09:00 → 14:45; ADR10 = trung bình đỉnh − đáy
  10 phiên liền trước): `vol` = (đỉnh − đáy)/ADR10 < 0,8 low · > 1,2 high · còn lại normal; `day_type` =
  |đóng − mở|/(đỉnh − đáy) ≥ 0,5 trend · còn lại range; `direction` = (đóng − mở) ≥ +0,2·ADR10 up · ≤ −0,2·ADR10 down
  · còn lại flat. Ngưỡng chốt từ suy luận, CHƯA tính tần suất nhãn trên bất kỳ dữ liệu nào.
- **Người dự báo:** `debate` (3 vai + trọng tài) và `single` (một agent, làm trước tranh luận). **Baseline** (chỉ dùng
  nhãn các ngày trước ngày chấm): `climatology` = tần suất làm trơn Laplace; `markov` = tần suất có điều kiện theo nhãn
  phiên liền trước, làm trơn Laplace.
- **Điểm:** Brier đa lớp Σ(p − o)² theo ngày.
- **Luật kết luận (cố định):** chỉ câu hỏi chính `day_type` (thứ quyết định nên dùng bot theo xu hướng hay bot đảo
  chiều), trên ĐÚNG **60 phiên chấm được đầu tiên** (chạy lại lúc nào cũng ra một kết quả). **CÓ GIÁ TRỊ** ⇔ Brier
  trung bình của `debate` thấp hơn CẢ `climatology` lẫn `markov` VÀ p sign-flip một phía của chuỗi (Brier baseline tốt
  hơn − Brier `debate`) theo ngày < 0,05. Chưa đủ 60 phiên ⇒ **CHƯA ĐỦ**. Còn lại ⇒ **KHÔNG CÓ GIÁ TRỊ**, dừng.
- **Chỉ báo cáo thêm, không đổi kết luận:** `vol`, `direction`; `debate` so với `single` (tranh luận có thêm gì so với
  một agent không); số ngày giao dịch bị lỡ nhận định.
- **CÓ GIÁ TRỊ thì sao:** vẫn KHÔNG nối vào bot. Bước kế tiếp là người dùng quyết có sửa CLAUDE.md mục 8 để cho nhận
  định quyền GIẢM rủi ro (chặn mở vị thế / giảm khối lượng) hay không; dùng nó làm cổng cho một chiến lược = cấu hình
  MỚI, đăng ký trước, tăng `n_trials`.
- **Đổi bất kỳ thứ gì dưới đây = thí nghiệm MỚI, chấm lại từ phiên đầu sau khi đổi:** định nghĩa nhãn/ngưỡng, câu hỏi
  chính, số phiên, cách tranh luận (số vai, số vòng), mô hình chạy tác vụ, danh sách nguồn dữ liệu bắt buộc.
- **Giới hạn đã biết (ghi trung thực):** (1) không connector nào có chứng khoán thế giới qua đêm, dầu, vàng ⇒ nhóm AI
  thiếu đúng loại thông tin dễ có ích nhất buổi sáng; kết quả đo "nhóm AI với dữ liệu hiện có", không phải giới hạn
  trên. (2) 60 phiên chỉ đủ phát hiện lợi thế LỚN; lợi thế nhỏ sẽ ra KHÔNG CÓ GIÁ TRỊ — chấp nhận (thà bỏ sót còn hơn
  tin nhầm). (3) Ngày tác vụ lỡ giờ hoặc lỗi thì không có nhận định, không bù sau. (4) Công thức gom dữ liệu mới kiểm
  trong phiên ngày 16/11, hành vi các tool lúc sáng sớm chưa kiểm.

## Đăng ký trước #7, #8, #9 (16/11/2026 — TRƯỚC khi viết code, tải hay chạy trên dữ liệu)

Người dùng chọn thử cả ba ý tưởng cùng lúc (16/11/2026) ⇒ **`n_trials` = 9** (thêm 3 dòng bảng),
`apply_selection_penalty` và ngưỡng p tính theo 9. Tham số CHỐT từ suy luận, KHÔNG nhìn kết quả; số nằm ở settings
`strategy.prev_day_reversal`, `strategy.basis_reversion`, `strategy.late_momentum`. Đổi bất kỳ số nào = cấu hình MỚI
(thêm dòng, tăng `n_trials`). Ba ý tưởng chọn để KHÁC sáu cấu hình đã trượt (4 theo đà / phá biên trong ngày, 2 đảo
chiều trong ngày): #7 đảo chiều QUA NGÀY, #8 đảo chiều so với chỉ số cơ sở, #9 đà cuối phiên.

Chung cho cả ba:
- Nến 1 phút đã đóng; lệnh khớp ở nến kế tiếp; phí/thuế theo `fees.*`, trượt 1 bước, spread giả 1 bước.
- ADR10 = trung bình (đỉnh − đáy) 10 phiên TRƯỚC (lịch sử nối chuỗi back-adjust); thiếu lịch sử ⇒ không giao dịch.
- "Phiên trước" = đúng phiên giao dịch liền trước theo lịch nghỉ; thiếu phiên đó trong lịch sử ⇒ không giao dịch
  (như #6, không lấy phiên cũ hơn).
- Luật rủi ro cứng giữ nguyên (lỗ ngày 15 điểm, 2 lượt/ngày, thua liên tiếp 2, 1 HĐ, không vào trước 09:15 và sau
  14:15, đóng hết 14:25, không giao dịch ngày đáo hạn).

**#7 — Đảo chiều qua ngày** (`strategy/prev_day_reversal.py`). Giả thuyết: VN30F chủ yếu là nhà đầu tư cá nhân (DNSE
27/10/2026: cá nhân ≈ 95% khối lượng) ⇒ phiên đi mạnh một chiều là phản ứng quá đà, phiên sau quay lại một phần
(đảo chiều chuỗi thời gian qua ngày, ghi nhận trên hợp đồng tương lai CSI 300).
- m = đóng nến CUỐI − mở nến ĐẦU của phiên trước. Quyết định MỘT lần khi nến 09:15 đóng (09:16).
- |m| ≥ 0,50 × ADR10 và đang phẳng ⇒ vào NGƯỢC dấu m. Stop = đóng nến 09:15 ± 0,40 × ADR10. Không chốt lời.
  EXIT khi nến 14:19 đóng. Tối đa 1 lệnh/ngày.
- Kiểm độ nhạy: `adr_days` 10, `min_move_adr` 0,50, `stop_adr` 0,40.

**#8 — Basis quá đà** (`strategy/basis_reversion.py`). Giả thuyết: HĐ tương lai và chỉ số đồng liên kết, độ lệch được
điều chỉnh dần; khi HĐ chạy quá xa chỉ số trong vài phút (đám đông cá nhân đẩy giá HĐ), HĐ bị kéo về. Rủi ro đã biết
TRƯỚC khi chạy: nếu HĐ DẪN chỉ số (chỉ số tính từ giá khớp cuối của cổ phiếu, hay chậm), độ lệch tự đóng nhờ chỉ số
đuổi theo và chiến lược này lỗ — các nghiên cứu về VN30F mâu thuẫn nhau về chiều dẫn dắt.
- basis(t) = đóng HĐ(t) − đóng chỉ số VN30(t), chỉ ở phút có CẢ HAI nến. Tham chiếu B(t) = trung bình basis của 30
  phút khớp được GẦN NHẤT TRƯỚC t trong hôm nay (chưa đủ 30 ⇒ không xét). d(t) = basis(t) − B(t).
- Xét nến có giờ mở 09:30 … 13:59 (bỏ nến 11:29). |d| ≥ 0,10 × ADR10 và đang phẳng ⇒ vào NGƯỢC dấu d (d > 0 ⇒ bán).
  Stop = đóng HĐ ± 0,15 × ADR10 (xa thêm về phía lệch). Không có giá chốt lời: thoát khi basis(t) − B(lúc vào) về 0
  hoặc đổi dấu, hoặc đã giữ ≥ 30 phút, hoặc nến 14:19 đóng. Tối đa 2 lệnh/ngày. Phút thiếu nến chỉ số ⇒ không vào,
  không xét thoát theo basis (thoát theo giờ vẫn chạy).
- Kiểm độ nhạy: `lookback_minutes` 30, `entry_dev_adr` 0,10, `stop_adr` 0,15.

**#9 — Đà cuối phiên** (`strategy/late_momentum.py`). Giả thuyết: lợi suất từ đóng cửa hôm trước tới gần cuối phiên dự
báo cùng chiều lợi suất nửa giờ cuối (Baltussen, Da, Lammers, Martens 2021, trên hơn 60 HĐ tương lai; có bài báo hiệu
ứng phiên bản qua đêm biến mất ngoài mẫu). Ghi nhận trung thực: họ hàng gần của #3 (đà sáng → chiều, đã KHÔNG CÓ
EDGE); Việt Nam không có quyền chọn chỉ số nên cơ chế phòng hộ gamma của bài gốc không áp dụng.
- r = đóng nến 13:49 − đóng nến cuối phiên trước. Quyết định MỘT lần khi nến 13:49 đóng (13:50).
- |r| ≥ 0,30 × ADR10 và đang phẳng ⇒ vào THEO dấu r. Stop = đóng nến 13:49 ∓ 0,20 × ADR10. Không chốt lời. EXIT khi
  nến 14:19 đóng (giữ 30 phút; nửa giờ cuối thật 14:15–14:45 gồm ATC, bot không được giữ qua 14:25).
- Kiểm độ nhạy: `adr_days` 10, `min_move_adr` 0,30, `stop_adr` 0,20.

**Dữ liệu (cố định):** nến 1 phút SSI, tải lại qua kết nối SSI của Claude như ngày 11/11/2026 (`marketdata_list_intraday_stock_infor`,
theo từng HĐ; HĐ kế tiếp thêm ở ngày trước roll), chế độ NẾN (bid/ask giả ∓ 1 bước, stop khớp ở cực trị nến, nến chạm
cả TP lẫn SL ⇒ SL thắng). #8 thêm nến 1 phút chỉ số VN30 cùng nguồn. Toàn bộ kết luận chạy trên MỘT nguồn (SSI) để
chạy được trên cloud; chế độ nến bi quan hơn tick ~0,2 điểm/lệnh ⇒ thiên về KHÔNG CÓ EDGE — chấp nhận.
- **IS** = 22/07/2025 → 31/12/2025 (~10 phiên đầu làm lịch sử ADR). Đã được #6 dùng làm IS (giả thuyết khác).
- **OOS-A** = 01/01/2026 → 24/03/2026: CHƯA ai chạy chiến lược nào ⇒ sạch.
- **OOS-B** = 25/03/2026 → 30/10/2026: đã bị #1–#5 nhìn qua (trên tick DNSE, giả thuyết khác) ⇒ yếu hơn OOS-A.
- **Loại khỏi kiểm định:** từ 02/11/2026 trở đi. OHLC ngày VN30F 02/11–13/11/2026 (và vài con số chỉ số, basis) đã
  được nhìn trong lần chạy thử nhật ký regime shadow ngày 16/11, TRƯỚC khi đề xuất ba ý tưởng này — kể cả tần suất
  "khoảng 1 lệnh / 3 phiên" của #7 nêu với người dùng là ước từ 10 phiên đó. Dữ liệu MỚI sau 16/11 dùng được để kéo
  dài OOS khi cần.

**Thứ tự và luật kết luận (cố định, cho TỪNG chiến lược):**
1. **Kiểm tra dữ liệu trước:** số ngày giao dịch thiếu so với lịch trong mỗi cửa sổ (thiếu > 10 ngày trong một cửa sổ ⇒
   dừng tìm lỗi đường ống, không kết luận). #8: ngày thiếu nến chỉ số > 10% ngày của một cửa sổ ⇒ #8 **KHÔNG KIỂM
   ĐƯỢC** ở cửa sổ đó (lỗi dữ liệu, không phải kết luận; `n_trials` vẫn tính 9).
2. **Cổng IS** (`backtest.robustness --bars`, dữ liệu nạp CẮT tới 31/12/2025, `--split 2025-12-31` như #6): P&L IS sau
   phí ≤ 0 **hoặc** < 75% biến thể độ nhạy ±20% lãi (6 biến thể) **hoặc** < 50% đoạn walk-forward lãi (4 đoạn) ⇒
   **KHÔNG CÓ EDGE, DỪNG — KHÔNG mở OOS** cho chiến lược đó.
3. **OOS gộp** (`backtest.pooled`, nến): OOS-A ∪ OOS-B, lệnh tính theo ngày VÀO. **CÓ EDGE** ⇔ (a) ≥ 30 lệnh gộp;
   (b) P&L sau phí > 0 ở CẢ HAI cửa sổ (0 lệnh = trượt); (c) p sign-flip chuỗi gộp × 9 < 0,1 (⇔ p < 0,0111);
   (d) P&L gộp > 0 khi trượt 2 tick. Thiếu (a) ⇒ **CHƯA ĐỦ** (kéo dài OOS bằng dữ liệu sau 16/11, KHÔNG đổi tham số);
   trượt bất kỳ (b)(c)(d) ⇒ **KHÔNG CÓ EDGE**, dừng chiến lược đó.
- Báo thêm (không đổi kết luận): P&L theo tháng, theo chiều Long/Short, tỉ lệ thoát theo stop / theo giờ / (#8) theo basis.
- Ba chiến lược độc lập: kết quả của chiến lược này KHÔNG được dùng để sửa chiến lược kia.
- CÓ EDGE vẫn phải paper ổn định (feed thật, tick, bid/ask) trước khi tính đến live.
- **Công cụ cần làm trước khi chạy** (test trước, không đổi engine cho #7/#9): `backtest.pooled` nhận `--bars`;
  #8 cần engine cấp nến chỉ số đã đóng của hôm nay cho chiến lược (`StrategyContext`) và `--ref-bars` ở robustness /
  pooled; runner live/paper từ chối chiến lược cần nến tham chiếu.

### Kết quả bước 1 — kiểm tra dữ liệu #7, #8, #9 (16/11/2026, cloud, TRƯỚC khi chạy bất kỳ chiến lược nào)

- Tải lại nến 1 phút SSI qua kết nối Claude (20 lệnh gọi theo HĐ, `scripts/import_ssi_intraday.py` ⇒ `data/ssi`, 74.431
  nến): SSI hiện trả RỖNG cho VN30F2509 (21/08→17/09/2025), VN30F2512 (20/11→17/12/2025) và VN30F2608 khung tháng 7
  (thử lại 1d/1w/1m và mã liên tục VN30F1M — vẫn rỗng), dù lần tải 11/11/2026 lấy được. `scripts/check_bar_store.py`:
  **IS thiếu 40/115 phiên** ❌ (> 10 ⇒ theo luật: dừng tìm lỗi đường ống, không kết luận); OOS-A thiếu 1/52 ✅; OOS-B
  thiếu 5/152 ✅. Thêm: thiếu HĐ kế tiếp ở 8 ngày trước roll ⇒ không đo được chênh roll ⇒ ~10 phiên sau mỗi roll đó
  không có ADR.
- **Cách sửa (không đổi tham số, cửa sổ hay luật):** dùng kho SSI tải ngày 11/11/2026 (cùng nguồn, cùng tool; theo
  `docs/progress.md` chỉ thiếu 8 ngày, có HĐ kế tiếp 5 ngày trước mỗi roll) đang nằm trên máy người dùng
  (`data/ssi/ohlc1m`). Chạy lại bước 1 trên kho đó TRƯỚC cổng IS. Bản tải lại 16/11 không dùng để kết luận.
- **#8: KHÔNG KIỂM ĐƯỢC** trên IS, OOS-A, OOS-B — nến 1 phút chỉ số VN30 của SSI chỉ có từ 15/10/2026 (có vẻ SSI giữ
  ~1 tháng cuộn; "VN30INDEX" rỗng, "VN30-INDEX" lỗi), DNSE `/price/ohlc` không dùng được (docs/broker_notes.md). Đúng
  luật đăng ký trước: lỗi dữ liệu, không phải kết luận; `n_trials` vẫn tính 9. Chưa viết code #8 (chưa có dữ liệu thì
  không có gì để kiểm). Hướng khả thi duy nhất: lưu nến chỉ số mỗi ngày từ nay, kiểm #8 trên dữ liệu MỚI (đăng ký cửa
  sổ trước khi có đủ dữ liệu).
- Công cụ thêm hôm nay (test trước): `backtest.pooled --bars`, `backtest.robustness --until` (cổng IS không nạp nến sau
  mốc), `scripts/check_bar_store.py`, `scripts/import_ssi_intraday.py`. Chạy thử trên dữ liệu GIẢ ngẫu nhiên: #7 vào
  lệnh 09:16, #9 vào 13:50, không cảnh báo — chưa chạy chiến lược nào trên dữ liệu thật.

### Kết quả #7 — cổng IS (16/11/2026, laptop, `scripts/is_gate.py`, kho SSI tải 11/11/2026)

- Bước 1 trên kho laptop: IS thiếu 2/115 phiên (16–17/10/2025), thiếu HĐ kế tiếp ngày trước roll 15/10 ✅; OOS-A thiếu
  1/52 ✅; OOS-B thiếu 5/152 ✅ ⇒ được chạy cổng IS.
- **#7: KHÔNG CÓ EDGE — DỪNG, KHÔNG mở OOS.** IS 41 lệnh, −15.364.700đ (thắng 29%, PF 0,58); trượt 2 tick −17,07tr.
  Độ nhạy 0/6 lãi (adr_days 8/12: −19,46/−13,77tr; min_move_adr 0,4/0,6: −20,91/−17,93tr; stop_adr 0,32/0,48:
  −11,49/−15,38tr). Walk-forward 1/4 (−0,85 / −2,58 / +1,83 / −13,76tr). Theo tháng: 08 −0,85 · 09 −1,61 · 10 −2,69 ·
  11 +2,22 · 12 −12,43tr. Long 15 lệnh −1,88tr; Short 26 lệnh −13,48tr. Thoát: theo giờ 19, risk đóng ép 15, stop 7.
- Ghi nhận (không đổi kết luận): (1) stop 0,40·ADR (ADR IS ~39 điểm ⇒ ~15,6 điểm) xa hơn lỗ ngày 15 điểm ⇒ 15 lệnh bị
  risk đóng ép trước stop; biến thể stop 0,32 vẫn lỗ 11,5tr. (2) Thua chủ yếu ở phía Short: sau phiên tăng mạnh giá
  thường tăng tiếp ⇒ giai đoạn này theo đà qua ngày, NGƯỢC giả thuyết. KHÔNG thử bản đảo ngược trên dữ liệu này (ý
  tưởng sinh ra SAU khi nhìn kết quả = đào dữ liệu); muốn thử phải là cấu hình mới đăng ký trước, kiểm trên dữ liệu
  chưa nhìn. OOS-A vẫn sạch với #7.

### Kết quả #9 — cổng IS (16/11/2026, laptop, `scripts/is_gate.py`, kho SSI tải 11/11/2026)

- **#9: KHÔNG CÓ EDGE — DỪNG, KHÔNG mở OOS.** IS 40 lệnh, −2.168.000đ (thắng 40%, PF 0,88); trượt 2 tick −2,97tr. Phí
  ~1,87tr ⇒ trước phí ≈ −0,3tr (hoà). Độ nhạy 3/6 lãi (adr_days 8/12: +0,64/−2,25tr; min_move_adr 0,24/0,36:
  −1,85/+4,03tr; stop_adr 0,16/0,24: −1,99/+0,14tr) — dấu đổi qua lại quanh 0 = nhiễu. Walk-forward 1/4 (−0,25 /
  −3,48 / +3,55 / −2,13tr). Theo tháng: 08 −0,25 · 09 −4,38 · 10 +0,90 · 11 +2,86 · 12 −1,30tr. Long 29 lệnh +3,84tr;
  Short 11 lệnh −6,00tr. Thoát: theo giờ 21, stop 19. Lệnh vào đúng 31/12/2025 (công cụ tính là OOS): 1 lệnh −0,14tr.
- Ghi nhận (không đổi kết luận): lãi dồn vào phía Long trong giai đoạn thị trường tăng (cuối 2025) — giống phơi nhiễm
  thị trường hơn là đà cuối phiên. KHÔNG thử bản "chỉ Long" hay ngưỡng 0,36 (biến thể tốt nhất) — chọn sau khi nhìn =
  đào dữ liệu.

### Tổng kết gói 2 (16/11/2026): 9 cấu hình, CHƯA có cấu hình nào có edge

- #7 trượt cổng IS (lỗ cả trước phí); #8 KHÔNG KIỂM ĐƯỢC (thiếu dữ liệu chỉ số); #9 trượt cổng IS (trước phí ≈ hoà).
- OOS-A (nến SSI 01/01→24/03/2026) vẫn CHƯA ai chạy chiến lược nào ⇒ còn sạch cho ý tưởng sau (đăng ký trước,
  `n_trials` = 10 trở lên).
- Mẫu hình chung của 9 lần thử: các ý tưởng giữ lệnh trong ngày, 1 HĐ, chi phí ~0,9 điểm/lượt, chưa lần nào có lãi
  trước phí đủ lớn để bù phí một cách ổn định. `max_contracts` giữ 1; không paper/live vì lý do edge.

### Sửa nhật ký regime shadow — thêm thế giới qua đêm + tin tới sáng (18/11/2026, người dùng đồng ý)

- **Vì sao thiếu dữ liệu:** không phải lỗi `vnstock-dashboard`. Dashboard lấy số thế giới từ Yahoo/FRED/gold-api và
  tin từ RSS (cafef, vnexpress, CNBC, MarketWatch); môi trường cloud chỉ mở `developers.tcbs.com.vn` và
  `developers.dnse.com.vn` ⇒ proxy chặn hết. Connector Finhay đóng băng số quốc tế từ 24/10/2026 (lỗi phía Finhay);
  tin Finhay gần như trống từ ~21:30 tới sáng.
- **Sửa:** mở `query1.finance.yahoo.com`, `cafef.vn`, `vnexpress.net`, `feeds.content.dowjones.io` cho môi trường cloud
  (CNBC bị Akamai chặn máy chủ cloud ⇒ bỏ); `scripts/overnight_pack.py` (test viết trước, không test nào ra mạng)
  in giá kèm thời điểm và tiêu đề tin trong cửa sổ [15:00 phiên trước, lúc chạy]; `research/regime_shadow.md` Bước 1
  mục 9 bắt buộc chạy script.
- **Hệ quả theo luật đăng ký ("đổi danh sách nguồn dữ liệu = thí nghiệm MỚI"):** cửa sổ kết luận 60 phiên tính từ
  phiên đầu tiên có gói mới = **19/11/2026** (`research.regime_shadow.verdict_from`). Nhận định 17/11 và 18/11 (gói cũ,
  thiếu số qua đêm) vẫn được chấm để theo dõi, KHÔNG vào kết luận. Mọi luật khác (nhãn, ngưỡng, câu hỏi chính, 60
  phiên, p < 0,05, hai baseline) giữ nguyên.
- Ghi nhận trung thực: nhận định 18/11 ghi lúc 07:56 khi chưa có số qua đêm; script chạy thử lúc 08:05 cùng ngày cho
  thấy S&P 500 −1,03%, Dow −1,64%, VIX +11% phiên 17/11 — đúng loại thông tin nhóm AI đã thiếu. Không ghi lại nhận
  định 18/11 (luật không ghi đè).
- Bổ sung cùng ngày (18/11, trước phiên 19/11 nên thuộc gói mới, KHÔNG dời `verdict_from`): khối ngoại trên phái sinh
  lấy được qua SSI `marketdata_list_daily_stock_infor` khi gọi bằng mã KRX (đo: 17/11 VN30F2611 mua 6.407 / bán 7.945,
  ròng −1.538 HĐ, −283 tỷ) — quy trình Bước 1 mục 5b. Tự doanh và OI phái sinh: vnstock-dashboard cũng không có
  (`vnstock_futures_foreign_view.py`: nguồn không có OI) ⇒ vẫn thiếu.

### Thêm vị thế ròng khối ngoại phái sinh vào gói sáng (19/11/2026, người dùng yêu cầu) ⇒ `verdict_from` = 20/11/2026

- `scripts/foreign_futures_position.py` (test viết trước): vị thế ròng ước tính mỗi HĐ đang lưu hành = Σ (KL mua − KL
  bán) khối ngoại từ phiên niêm yết, nguồn SSI `marketdata_list_daily_stock_infor` theo mã KRX (range 1y, phân trang).
  Đối chiếu: VN30F2612 từ SSI riêng (147 phiên 17/04→18/11/2026) = +1.642 HĐ, khớp seed vnstock-dashboard + SSI;
  VN30F2611 = −11.479 HĐ. Quy trình sáng Bước 1 mục 5b.
- Đổi nguồn dữ liệu sau khi cửa sổ đã mở 19/11 ⇒ theo luật đăng ký: thí nghiệm mới, **cửa sổ kết luận 60 phiên tính
  từ 20/11/2026**. Nhận định 19/11 (gói chưa có vị thế ròng) chỉ theo dõi, như 17–18/11. Đề xuất từ đây CHỐT gói dữ
  liệu: mỗi lần thêm nguồn lại mất cửa sổ đã chạy.

### CHỐT gói dữ liệu nhật ký regime shadow (19/11/2026, người dùng quyết)

- Gói dữ liệu buổi sáng được CHỐT như `research/regime_shadow.md` Bước 1 ở commit này (mục 1–9, gồm 5b vị thế ròng
  khối ngoại và 9 thế giới qua đêm). Cửa sổ kết luận: 60 phiên chấm được đầu tiên từ **20/11/2026**.
- Từ nay KHÔNG thêm, bớt hay đổi nguồn trong gói cho tới khi có kết luận 60 phiên. Nguồn nào hỏng giữa chừng ⇒ ghi vào
  `inputs.missing` của ngày đó và chạy tiếp, KHÔNG thay nguồn khác và KHÔNG dời mốc. Ý tưởng nguồn mới (OI, tự doanh…)
  ghi lại để dùng cho thí nghiệm SAU, không chen vào thí nghiệm đang chạy.


## Cổng lực thống kê — áp dụng từ ý tưởng #10 (27/11/2026, người dùng đồng ý làm cổng)

KHÔNG phải một lần thử: không thêm dòng bảng, `n_trials` giữ 9. Đây là luật về việc ĐƯỢC đăng ký hay không.

- **Vì sao:** kiểm định quyết định là p sign-flip × `n_trials` < 0,1 trên lệnh OOS gộp (`backtest.pooled`). Với ý tưởng
  thứ 10 mỗi phép thử cần p < 0,01. Công thức chuẩn, một phía, lực 80%: lợi thế nhỏ nhất bắt được =
  (z₀.₉₉ + z₀.₈) × sd/√n = 3,17 × sd/√n. Với sd = 10 điểm/lệnh: 60 lệnh ⇒ 4,09 điểm/lệnh; 100 ⇒ 3,17; 300 ⇒ 1,83;
  1.000 ⇒ 1,00. Chi phí mỗi lượt ~0,9 điểm. Tức là các lần thử kiểu "≤ 2 lệnh/ngày, vài chục lệnh OOS" chỉ có thể
  phát hiện lợi thế rất lớn; chúng KHÔNG bác được lợi thế cỡ 1–2 điểm/lệnh, và mỗi lần thử lại làm ngưỡng p chặt thêm.
- **Công cụ:** `backtest/power.py` (`python -m backtest.power`), test viết trước (`tests/test_power.py`).
  `power_normal`, `min_detectable_edge`, `trades_needed` (công thức); `simulated_power` (lấy mẫu lại P&L của một chiến
  lược tham chiếu ĐÃ TRỪ TRUNG BÌNH + lợi thế giả định, chạy đúng `monte_carlo_pvalue`); `power_gate` (kết luận).
- **Luật (CHỐT trước khi xét #10):**
  1. Bản đăng ký trước phải ghi: (a) lợi thế giả định SAU phí, điểm/lệnh, kèm lý do; (b) số lệnh OOS gộp dự kiến và
     cách ước lượng; (c) độ phân tán lấy từ đâu — `--sd`, hoặc một chiến lược tham chiếu đã thử, chạy trên dữ liệu ĐÃ
     dùng (`--until`, không chạm cửa sổ còn sạch như OOS-A).
  2. Chạy `python -m backtest.power` với `n_trials` = số dòng bảng + 1 (mặc định của công cụ); dán kết quả vào bản
     đăng ký.
  3. ĐỦ LỰC ⇔ số lệnh dự kiến ≥ 30 VÀ kiểm định còn đạt được (p nhỏ nhất 1/(mc_sims+1) × n_trials < 0,1) VÀ lực ≥ 80%.
     KHÔNG ĐỦ LỰC ⇒ không đăng ký, không viết code chiến lược, không thêm dòng bảng, không tăng `n_trials`.
  4. Không nâng lợi thế giả định hay số lệnh cho vừa cổng: lợi thế thật nhỏ hơn giả định thì lần thử vẫn trượt và vẫn
     tốn một `n_trials`. Đổi giả định phải có lý do mới, ghi tại đây.
  5. Ngưỡng 80% và α = 0,1/`n_trials` là mức quy ước Claude đề xuất; đổi ngưỡng phải ghi tại đây TRƯỚC khi xét ý tưởng
     kế tiếp.
- **Giới hạn đã biết:** lực là CẬN TRÊN (luật gộp còn đòi mọi cửa sổ lãi và chịu trượt giá căng); công thức là xấp xỉ
  chuẩn, mô phỏng mới dùng hình dạng P&L thật; độ phân tán của chiến lược tham chiếu có thể khác ý tưởng mới;
  `mc_sims` = 1.000 ⇒ từ `n_trials` = 101 kiểm định không thể đạt (công cụ báo).
- **Chạy thử đường mô phỏng (cloud, kho SSI thiếu ngày, nến tới hết 31/12/2025 — dữ liệu IS đã dùng của #6/#7/#9):** ORB
  làm tham chiếu, 136 lệnh, độ lệch chuẩn 12,28 điểm/lệnh (P&L của ORB KHÔNG được in hay xem — công cụ chỉ lấy độ phân
  tán). Lợi thế giả định 2 điểm, 100 lệnh, `n_trials` 10 ⇒ lực mô phỏng 22% (công thức 24%), báo động giả 1,8% (mức
  danh nghĩa 1%, 400 lần lặp). Số chính thức phải chạy lại trên laptop với kho đầy đủ.

## Giai đoạn đo tín hiệu — tách "tín hiệu có dự báo được không" khỏi "chiến lược có lãi không" (27/11/2026, người dùng đồng ý)

KHÔNG phải một lần thử: không thêm dòng bảng, `n_trials` giữ 9. Đây là luật về TRÌNH TỰ nghiên cứu từ ý tưởng #10.

- **Vì sao:** chín lần thử trước gộp tín hiệu + stop + chốt lời + giới hạn lệnh và kiểm trên vài chục lệnh ⇒ lực thấp
  (mục "Cổng lực thống kê") và trượt mà không biết trượt ở đâu. Đo riêng tín hiệu dùng MỌI nến: mỗi ngày ~200 quan sát.
- **Công cụ:** `backtest/signal_study.py` (`python -m backtest.signal_study`), test viết trước (`tests/test_signal_study.py`).
  Tín hiệu là hàm thuần `(bars_today, history) -> số | None`, chạy trong `BacktestEngine` qua một "chiến lược" không ra
  lệnh ⇒ nhận đúng dữ liệu chiến lược thật nhận (hợp đồng giao dịch, back-adjust qua roll, chỉ nến đã đóng). Quan sát:
  tín hiệu ở nến i ⇒ vào giá MỞ nến i+1, ra giá ĐÓNG nến i+h, trong cửa sổ được phép của luật rủi ro (vào 09:15 →
  trước 14:15, ra trước 14:25, bỏ ngày đáo hạn). Thống kê theo NGÀY: IC Spearman từng ngày, p sign-flip một phía theo
  chiều đã đăng ký × `n_trials` < 0,1, ≥ 50% trong 4 đoạn walk-forward đúng chiều, ≥ 30 ngày. Độ lớn: lợi suất kỳ sau lấy
  dấu theo tín hiệu tại các nến |tín hiệu| ≥ ngưỡng, đã trừ trôi giá trung bình trong ngày, trừ chi phí một lượt của
  engine (phí + thuế hai chiều + mỗi chiều trượt 1 bước và nửa spread = 0,87 điểm với settings hiện tại).
- **Luật (CHỐT trước khi xét #10):**
  1. Trước mỗi phép đo, ghi và commit bản đăng ký: công thức tín hiệu (hàm trong `strategy/`), kỳ `h` (số nến), chiều
     (+1 thuận / −1 nghịch), ngưỡng |tín hiệu| dùng để đo độ lớn, cửa sổ IS và cửa sổ OOS, lý do.
  2. Mỗi (tín hiệu, tham số, kỳ, ngưỡng) đo trên dữ liệu thật = MỘT dòng bảng, `n_trials` + 1 — chung một sổ với chiến
     lược. Không quét nhiều kỳ hay ngưỡng rồi chọn cái đẹp nhất.
  3. Đo trên IS. KHÔNG CÓ TÍN HIỆU hoặc KHÔNG ĐỦ TRẢ PHÍ ⇒ dừng, không dựng chiến lược. ĐỦ LỚN ⇒ đo lại ĐÚNG MỘT lần
     trên cửa sổ OOS đã đăng ký, cùng tham số và cùng `n_trials`; phải ĐỦ LỚN lần nữa.
  4. Qua cả hai ⇒ chạy cổng lực thống kê với lợi thế giả định KHÔNG cao hơn lợi thế ròng đo ở OOS, rồi mới đăng ký
     chiến lược (một lần thử riêng, kiểm định như cũ).
  5. Giả dược (`placebo_signal`) không mang giả thuyết ⇒ chạy để thử đường ống không tính là lần thử.
- **Giới hạn đã biết:** độ lớn là ước lượng SÀNG LỌC (bỏ qua luật một vị thế, stop, số lệnh/ngày; các nến vượt ngưỡng
  liền nhau không phải các lệnh riêng); `h` tính theo số nến nên phút thiếu và giờ nghỉ trưa không được bù; kiểm định
  coi các NGÀY là độc lập; "chung một sổ `n_trials`" là đề xuất của Claude — đổi phải ghi tại đây trước phép đo đầu tiên.
- **Thử đường ống bằng giả dược (cloud, kho SSI thiếu ngày, nến tới hết 31/12/2025, kỳ 5 nến):** 74 ngày, 15.540 quan
  sát (~210/ngày), IC trung bình −0,0019, độ lệch chuẩn IC ngày 0,0495, p 0,616 ⇒ KHÔNG CÓ TÍN HIỆU, đúng như phải thế.
  Từ độ lệch chuẩn này: với 74 ngày và `n_trials` 10 (p < 0,01, lực 80%), IC trung bình nhỏ nhất bắt được ≈
  3,17 × 0,0495/√74 ≈ 0,018. Trôi giá trung bình 0,13 điểm mỗi 5 nến trong giai đoạn đó (đã trừ khỏi phép đo độ lớn).
- **⚠ ĐÍNH CHÍNH (27/11/2026, cùng ngày):** cách tính "IC Spearman từng ngày" và "trừ trôi giá trong ngày" mô tả ở mục
  này bị LỆCH với tín hiệu chậm và đã được thay; con số 0,018 chỉ đúng cho tín hiệu đổi độc lập từng nến. Xem mục
  "Sửa phương pháp đo tín hiệu" bên dưới.

## Đăng ký trước #10, #11, #12 — ba phép ĐO TÍN HIỆU (27/11/2026 — TRƯỚC khi viết code tín hiệu hay chạy trên dữ liệu)

Người dùng chọn đo cả ba ứng viên (27/11/2026) ⇒ **`n_trials` = 12** (thêm 3 dòng bảng); cả ba phép đo dùng
`--n-trials 12` ⇒ mỗi phép cần p < 0,1/12 = 0,0083. Theo luật "Giai đoạn đo tín hiệu": đây là phép đo tín hiệu
(`backtest.signal_study`), CHƯA phải chiến lược. Mọi con số dưới đây CHỐT từ suy luận; đổi bất kỳ số nào = cấu hình MỚI.

Chung cho cả ba:
- Tín hiệu là hàm thuần `(bars_today, history)` trong `strategy/signals.py`, chỉ dùng nến đã đóng. Quan sát: tín hiệu ở
  nến i ⇒ vào giá MỞ nến i+1, ra giá ĐÓNG nến i+h; nến vào mở trong [09:15, 14:15), nến ra đóng không muộn hơn 14:25;
  bỏ ngày đáo hạn. Chi phí một lượt theo settings (0,87 điểm).
- Kết luận theo `backtest.signal_study.study` với luật mặc định (≥ 30 ngày có IC; IC trung bình đúng chiều; p sign-flip
  × 12 < 0,1; ≥ 50% trong 4 đoạn đúng chiều; ≥ 30 ngày có tín hiệu vượt ngưỡng; lợi thế thô − chi phí > 0).
- **Trình tự cho TỪNG phép đo:** (1) kiểm tra dữ liệu — số ngày giao dịch thiếu trong mỗi cửa sổ (`scripts/check_bar_store.py`;
  thiếu > 10 ngày ⇒ dừng, không kết luận); (2) đo IS; kết luận khác "ĐỦ LỚN" ⇒ DỪNG, không mở OOS, không sửa tham số
  để cứu; (3) "ĐỦ LỚN" ⇒ ghi con số ngưỡng của IS vào đây, rồi đo OOS ĐÚNG MỘT lần với ngưỡng đó và mọi tham số giữ
  nguyên; phải "ĐỦ LỚN" lần nữa; (4) qua cả hai ⇒ cổng lực thống kê (lợi thế giả định ≤ lợi thế ròng đo ở OOS) rồi
  mới đăng ký chiến lược (lần thử riêng).
- Ngưỡng "phân vị 85% của |tín hiệu| trên quan sát IS": tính CHỈ từ giá trị tín hiệu (không dùng lợi suất kỳ sau), bằng
  tuỳ chọn `--threshold-quantile 0.85` (thêm vào công cụ trước khi chạy). Chọn 85% vì ngưỡng hoà vốn ước lượng trước
  đó tính cho khoảng 13% số nến mạnh nhất. `--n-trials` của công cụ đổi mặc định thành số dòng bảng (phép đo đã có dòng).
- Ba phép đo độc lập: kết quả phép này KHÔNG được dùng để sửa phép kia.

**#10 — Dòng tiền theo khối lượng** (`volume_flow`). Giả thuyết: thị trường ~95% khối lượng là cá nhân ⇒ dòng lệnh một
chiều có quán tính; khối lượng dồn vào các nến tăng (giảm) trong nửa giờ qua báo trước giá đi tiếp cùng chiều trong
giờ tới. Nguồn thông tin MỚI: chín lần thử trước chỉ dùng giá. Điểm yếu biết trước: dấu của nến 1 phút là xấp xỉ thô
của dòng lệnh chủ động.
- W = 30 nến đã đóng gần nhất của HÔM NAY, gồm nến hiện tại. Hôm nay chưa đủ 30 nến ⇒ không có tín hiệu. Bất kỳ nến
  nào trong W có `volume_reliable = False` ⇒ không có tín hiệu. Tổng khối lượng W = 0 ⇒ không có tín hiệu.
- F = Σ_W dấu(đóng − mở) × khối lượng / Σ_W khối lượng ∈ [−1, 1] (nến đóng = mở góp 0 vào tử số).
- Kỳ h = 60 nến. Chiều +1. Ngưỡng độ lớn = phân vị 85% của |F| trên quan sát IS.
- Dữ liệu: nến 1 phút DNSE dựng từ khớp lệnh (`data/history/ohlc1m`, khối lượng tin được). KHÔNG dùng nến SSI: khối
  lượng SSI chưa đối chiếu từng phút (`volume_reliable = False`). **IS** = 11/05/2026 → 31/08/2026. **OOS** =
  01/09/2026 → 30/10/2026 (đã bị #1–#5 nhìn qua với giả thuyết khác, chỉ dùng giá ⇒ OOS yếu hơn OOS-A; chấp nhận vì
  không có cửa sổ nào khác có khối lượng tin được). Từ 02/11/2026 trở đi loại khỏi phép đo (đã ghi 16/11).

**#11 — Xu hướng trong ngày** (`intraday_trend`). Giả thuyết: đà trong ngày (lợi suất từ đầu phiên dự báo phần còn lại
của phiên — ghi nhận trên chỉ số Mỹ và hợp đồng tương lai chỉ số Trung Quốc). Bốn chiến lược theo đà (#1–#4) và #9 đã
trượt, nhưng mỗi lần chỉ vài chục lệnh ở một khung giờ cố định; phép đo này hỏi câu tổng quát trên mọi nến để đóng
hoặc mở lại cả họ ý tưởng. Xác suất thành công thấp nhất trong ba — biết trước.
- T = (đóng nến hiện tại − mở nến ĐẦU TIÊN của hôm nay) / ADR10. ADR10 = trung bình (đỉnh − đáy) 10 phiên TRƯỚC
  (`strategy.indicators.adr_before`, lịch sử back-adjust); thiếu ⇒ không có tín hiệu. `history_days` = 10.
- Kỳ h = 60 nến. Chiều +1. Ngưỡng độ lớn = phân vị 85% của |T| trên quan sát IS.
- Dữ liệu: nến 1 phút SSI (`data/ssi/ohlc1m`, kho đầy đủ trên laptop). **IS** = 22/07/2025 → 31/12/2025 (dữ liệu nạp
  CẮT tới 31/12/2025; ~10 phiên đầu làm lịch sử ADR). **OOS** = OOS-A 01/01/2026 → 24/03/2026 (chưa chiến lược nào
  chạy ⇒ sạch).

**#12 — Basis lệch khỏi mức quen** (`basis_deviation`). Giả thuyết như #8 (16/11): hợp đồng và chỉ số bị buộc vào nhau;
khi đám đông đẩy hợp đồng lệch xa chỉ số trong vài phút, hợp đồng bị kéo về. Rủi ro biết trước: nếu hợp đồng DẪN chỉ
số thì độ lệch tự đóng nhờ chỉ số đuổi theo và chiều đăng ký sai. #8 (chiến lược) chưa từng chạy vì thiếu dữ liệu;
phép đo này là bước trước của nó, tính là lần thử riêng.
- basis(t) = đóng HĐ(t) − đóng VN30(t), chỉ ở phút có CẢ HAI nến. B(t) = trung bình basis của 30 phút khớp được GẦN
  NHẤT TRƯỚC t trong hôm nay (chưa đủ 30 ⇒ không có tín hiệu). D(t) = (basis(t) − B(t)) / ADR10. Phút thiếu nến chỉ số
  ⇒ không có tín hiệu. (Đúng định nghĩa d và B của #8.)
- Kỳ h = 30 nến. Chiều −1 (D > 0 ⇒ kỳ vọng HĐ giảm). Ngưỡng độ lớn |D| ≥ 0,10 (mức vào lệnh đã đăng ký của #8).
- Dữ liệu: nến 1 phút SSI của HĐ và của chỉ số VN30 (cùng nguồn, cùng mốc phút; chỉ số chỉ có từ 15/10/2026).
  **IS** = 15/10/2026 → 31/12/2026. **OOS** = 04/01/2027 → 31/03/2027. Đo IS khi cửa sổ IS đã kết thúc và công cụ đã
  nhận được chuỗi thứ hai (việc riêng, test trước); đo OOS sau 31/03/2027. Ngày thiếu nến chỉ số > 10% số ngày của một
  cửa sổ ⇒ KHÔNG ĐO ĐƯỢC ở cửa sổ đó (lỗi dữ liệu, `n_trials` vẫn tính).

**Ghi nhận trung thực — những gì đã được nhìn TRƯỚC khi đăng ký:**
- Số mô tả từ giả dược trên nến SSI tới 31/12/2025 (kho cloud thiếu ngày): độ lệch chuẩn lợi suất theo kỳ (5/15/30/60/120
  nến: 3,20/5,55/7,62/10,36/13,32 điểm), độ lệch chuẩn IC ngày (0,049/0,055/0,060/0,084/0,124), trôi giá 0,13 điểm mỗi 5
  nến. Không phụ thuộc tín hiệu nào; dùng để chọn kỳ 60 nến (kỳ ngắn không thể hoà vốn với chi phí 0,87 điểm).
- Kết quả chín lần thử trước trên chính các cửa sổ IS này (vd ORB trước phí ≈ +0,2 điểm/lượt trên IS DNSE; #9 trước phí
  ≈ hoà và #7 lỗ trước phí trên IS SSI) ⇒ chiều THUẬN của #11 không hoàn toàn "mù" với IS. Vì vậy IS chỉ là sàng lọc;
  bằng chứng là cửa sổ OOS.
- #12: từ 16/11 tới 27/11, nhật ký regime shadow mỗi sáng đã nhìn OHLC ngày, basis lúc đóng cửa và mô tả dáng phiên
  trước của hợp đồng. CHƯA ai tính độ lệch basis trong ngày so với trung bình 30 phút. Nhật ký đó sẽ tiếp tục nhìn số
  NGÀY trong suốt cửa sổ IS/OOS của #12; cấu hình #12 đã chốt nên việc nhìn không đổi được gì.

## Sửa phương pháp đo tín hiệu — TRƯỚC khi bất kỳ tín hiệu thật nào chạy trên dữ liệu thật (27/11/2026)

KHÔNG phải một lần thử. Công thức tín hiệu, kỳ, chiều, ngưỡng, cửa sổ và `n_trials` = 12 của #10–#12 GIỮ NGUYÊN.

- **Phát hiện:** thử đường ống #10 và #11 trên nến GIẢ là bước ngẫu nhiên (không có gì để dự báo), công cụ báo IC
  Spearman theo ngày −0,16 (t = −3,6) và −0,50 (t = −5,3), lợi thế thô −1,70 và −5,97 điểm. Ba nguyên nhân, đều là dùng
  thông tin TƯƠNG LAI của chính ngày đó: (1) xếp hạng / định tâm tín hiệu và lợi suất TRONG ngày; (2) trừ lợi suất kỳ
  sau trung bình CỦA NGÀY để khử trôi giá; (3) lấy trung bình của trung bình ngày cho lợi thế thô, trong khi số nến
  vượt ngưỡng của một ngày phụ thuộc diễn biến sau đó. Với tín hiệu chậm (mức giá, dòng tiền 30 nến, độ lệch) điều này
  tạo "đảo chiều" giả (lệch kiểu Dickey–Fuller). Giả dược không lộ lỗi vì nó đổi độc lập từng nến.
- **Chưa có gì bị nhiễm:** trước khi sửa chỉ có giả dược chạy trên dữ liệu thật; #10/#11 mới chạy trên nến giả.
- **Sửa (test viết kèm, có test "bước ngẫu nhiên + tín hiệu chậm ⇒ không ra đảo chiều"):** thống kê kiểm định là "điểm
  ngày" = trung bình trong ngày của (tín hiệu × lợi suất kỳ sau) chia hằng số chuẩn hoá của cả mẫu — không xếp hạng,
  không định tâm trong ngày; trôi giá trừ đi là trung bình của CÁC NGÀY KHÁC; lợi thế thô là trung bình trên mọi nến
  vượt ngưỡng. Trong luật chung của #10–#12, cụm "≥ 30 ngày có IC; IC trung bình đúng chiều" đọc là "≥ 30 ngày đủ
  quan sát; điểm ngày trung bình đúng chiều"; mọi điều kiện khác giữ nguyên.
- **Hiệu chuẩn trên bước ngẫu nhiên** (300 bộ × 110 ngày × 240 nến, 1,34 điểm/nến ≈ độ lệch chuẩn lợi suất 60 nến 10,4
  điểm đo trên nến SSI; ngưỡng phân vị 85%):
  - kiểu #11 (mức giá so với giá mở, kỳ 60): t trung bình −0,14; báo động giả ở p < 0,10: thuận 5,3% / nghịch 10,7%; ở
    p < 0,0083: 1,0% / 1,7%; độ lệch chuẩn điểm ngày 0,529; lợi thế thô trung bình −0,13, độ lệch chuẩn giữa các bộ 1,13 điểm.
  - kiểu #10 (khối lượng theo chiều nến 30 nến, kỳ 60): t −0,04; 10,3% / 12,7%; 1,0% / 1,0%; 0,389; −0,03 và 0,85 điểm.
  - kiểu #12 (lệch khỏi trung bình 30 nến, kỳ 30): t −0,06; 10,0% / 11,3%; 0,0% / 2,0%; 0,296; −0,02 và 0,46 điểm.
  ⇒ gần mức danh nghĩa; hơi rộng tay ở chiều NGHỊCH (chiều của #12) ⇒ trước khi đo #12 phải hiệu chuẩn lại với nhiều
  bộ hơn. Kịch bản hiệu chuẩn chưa nằm trong repo (việc riêng, cần test).
- **Lực THẬT của ba phép đo** (p < 0,0083, lực 80% ⇒ 3,24 × sai số chuẩn; thay cho con số 0,018–0,046 suy từ giả dược):
  lợi thế thô nhỏ nhất bắt được ≈ 3,7 điểm/lượt cho #11 (~105 ngày IS), 3,3 điểm cho #10 (78 ngày), 2,1 điểm cho #12
  (~54 ngày); theo tương quan: 0,17 / 0,14 / 0,13. Hoà vốn là 0,87 điểm (tương quan ước 0,04–0,06). Tức là ba phép đo
  chỉ bắt được lợi thế gấp ~2,5–4 lần chi phí; "KHÔNG CÓ TÍN HIỆU" phải đọc là "không có lợi thế RẤT LỚN", không phải
  "không có lợi thế". Nguyên nhân gốc: số lượt đặt cược độc lập ≈ số ngày × 240 / kỳ; kỳ dài thì ít lượt, kỳ ngắn thì
  không trả nổi phí.
- **Lỗi quy trình (Claude):** #10–#12 được đăng ký TRƯỚC khi có con số lực này; bảng "tương quan nhỏ nhất đo được" đưa
  cho người dùng ngày 27/11 là quá lạc quan với tín hiệu chậm. Người dùng quyết định chạy hay rút; `n_trials` giữ 12
  trong cả hai trường hợp (như tiền lệ #8).
- **Luật bổ sung cho các phép đo SAU #12:** trước khi đăng ký, chạy công thức tín hiệu trên bước ngẫu nhiên để (a) xác
  nhận không lệch và (b) biết lợi thế nhỏ nhất bắt được với số ngày hiện có; không đủ lực ⇒ không đăng ký.

### Kết quả #10 và #11 — đo IS (28/11/2026, desktop `E:\ft-research` ở commit de43eb7)

- **Kiểm tra dữ liệu (bước 1) ✅:** SSI IS 115 phiên thiếu 2 (16–17/10/2025), thiếu HĐ kế tiếp ngày trước roll 15/10;
  OOS-A 52 phiên thiếu 1 (13/02), thiếu HĐ kế tiếp 14/01 và 12/02. DNSE IS 80 phiên và OOS 42 phiên, thiếu 0.
- **#11 IS** (nến SSI tới 31/12/2025, kỳ 60, chiều +1): 113 ngày nạp, bỏ 4 ngày đáo hạn; 89 ngày có quan sát (thiếu lịch sử
  10 phiên quanh roll và đầu kho), 14.329 quan sát. Tương quan trung bình −0,0206, độ lệch chuẩn theo ngày 0,6086,
  t = −0,32; p 0,632 × 12 = 1,000; walk-forward 50%. Ngưỡng phân vị 85% |T| = 0,459210; 42 ngày, 24,2 nến/ngày vượt ngưỡng;
  lợi thế thô 0,27 − chi phí 0,87 = −0,59 điểm. Trôi giá cả mẫu 1,52 điểm mỗi 60 nến. ⇒ **KHÔNG CÓ TÍN HIỆU, dừng.**
- **#10 IS** (nến DNSE tới 31/08/2026, kỳ 60, chiều +1): 80 ngày nạp, bỏ 4 ngày đáo hạn; 75 ngày, 10.738 quan sát. Tương
  quan +0,0047, độ lệch chuẩn theo ngày 0,4369, t = 0,09; p 0,470 × 12 = 1,000; walk-forward 50%. Ngưỡng 0,365250; 73 ngày,
  21,5 nến/ngày; lợi thế thô 0,10 − 0,87 = −0,77 điểm. Trôi giá 0,24 điểm mỗi 60 nến. ⇒ **KHÔNG CÓ TÍN HIỆU, dừng.**
- Cả hai tương quan nằm sát 0 (|t| < 0,35), lợi thế thô ≤ 0,27 điểm — xa cả ngưỡng phát hiện (~3,3–3,7 điểm) lẫn chi phí.
  Theo luật: không mở OOS, không sửa tham số. OOS-A (SSI 01/01→24/03/2026) VẪN SẠCH; OOS DNSE 01/09→30/10/2026 không bị
  #10 nhìn. Bảng nhóm tín hiệu của #11 (mô tả) có hình chữ U — KHÔNG được dùng làm giả thuyết mới từ chính dữ liệu này.
- **Độ phân tán thật cho cổng lực** (`backtest.power`, ORB làm tham chiếu, nến DNSE tới 06/11/2026, 222 lệnh): độ lệch
  chuẩn 9,49 điểm/lệnh (P&L của ORB không xem). Với `n_trials` 13: 60 lệnh ⇒ lợi thế nhỏ nhất bắt được 4,00 điểm/lệnh;
  1.000 lệnh ⇒ 0,98. Lợi thế giả định 1,5 điểm cần ~427 lệnh OOS.
- Tổng: 12 cấu hình đã thử (9 chiến lược + 3 phép đo; #12 chờ dữ liệu), chưa có tín hiệu/edge nào.

## Kiểm toán chi phí khớp lệnh từ dữ liệu recorder (28/11/2026 — luật dùng kết quả CHỐT TRƯỚC khi có số thật)

KHÔNG phải một lần thử (không thêm dòng bảng, `n_trials` giữ 12): đo chi phí, không đo lợi thế của chiến lược nào.

- **Vì sao:** chi phí mỗi lượt của backtest = phí + thuế 0,467 điểm (cố định) + khớp lệnh 0,4 điểm (mỗi chiều trượt 1 bước
  + nửa spread giả làm tròn 1 bước). Phần 0,4 điểm chưa từng được đo; 12 lần thử đều chết vì chi phí.
- **Công cụ:** `backtest/cost_audit.py` (`python -m backtest.cost_audit --from … --to …`), test viết trước
  (`tests/test_cost_audit.py`). Dữ liệu: tick + giá tốt nhất recorder ghi (`data/raw/`, feed DNSE), HĐ giao dịch, phiên
  liên tục. Mỗi đầu phút trong cửa sổ vào lệnh (09:15 → trước 14:15), CẢ HAI chiều, 1 HĐ, độ trễ `paper.latency_ms`:
  lệnh thị trường; lệnh giới hạn ở giá tốt nhất cùng phía xếp sau toàn bộ hàng chờ (không tính huỷ phía trước), chờ 10 /
  30 / 60 s rồi đuổi. Thiếu hụt so với mid lúc ra quyết định; sai số chuẩn theo ngày. Kèm spread theo thời gian, độ sâu,
  nửa spread hiệu dụng của giao dịch thật.
- **Luật dùng kết quả (chốt trước khi chạy trên dữ liệu thật):**
  1. Kết quả CHỈ dùng để đặt giả định chi phí cho các lần thử SAU; 12 lần thử đã kết luận giữ nguyên kết luận, kể cả khi
     chi phí đo được thấp hơn giả định (không chạy lại để "cứu"). Muốn kiểm lại một ý tưởng cũ với chi phí mới = đăng ký
     như cấu hình MỚI, cửa sổ OOS mới, tăng `n_trials`.
  2. Đổi `backtest.slippage_ticks` / `backtest.synthetic_spread_ticks` chỉ khi đo trên ≥ 10 phiên và cận trên (trung bình
     + 2 sai số chuẩn) của chi phí lệnh thị trường mỗi chiều vẫn thấp hơn giả định hiện tại — và chỉ hạ tới mức làm tròn
     LÊN theo bước giá của cận trên đó. Chi phí đo được CAO hơn giả định ⇒ nâng giả định ngay (bảo thủ).
  3. Lệnh giới hạn chỉ được đưa vào giả định của một chiến lược khi chiến lược đó dùng lệnh giới hạn thật trong paper, và
     dùng con số của chính kiểu tín hiệu đó (quyết định đầu phút không gắn tín hiệu là cận dưới cho tín hiệu theo đà).
  4. Mọi thay đổi settings từ kết quả này ghi tại đây kèm con số, trước khi dùng.
- **Thử đường ống (cloud, dữ liệu recorder GIẢ 2 phiên × 510 nghìn dòng, bước ngẫu nhiên, spread 1 bước 85% thời gian):**
  chạy ~1 giây; lệnh thị trường 0,058 điểm/chiều (= nửa spread trung bình, đúng như phải thế); lệnh giới hạn chờ 10/30/60 s
  khớp 83/91/94% nhưng thiếu hụt 0,10–0,13 — trên bước ngẫu nhiên, lệnh giới hạn khớp đúng lúc giá đi ngược và bị lỡ đúng
  lúc giá đi xuôi, nên rẻ hơn lệnh thị trường là KHÔNG tự nhiên; số thật sẽ cho biết thị trường có khác bước ngẫu nhiên không.
