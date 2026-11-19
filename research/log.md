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

