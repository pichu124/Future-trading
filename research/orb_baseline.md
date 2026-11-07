# Baseline ORB — phá biên độ 15 phút đầu phiên

Code: `strategy/baseline_orb.py` · tham số: `settings.strategy.orb` · test: `tests/test_strategy_orb.py`

## Đặc tả (cấu hình #1, chốt TRƯỚC khi nhìn kết quả)

| Thành phần | Quy tắc |
|---|---|
| Biên | đỉnh/đáy các bar 1 phút mở trong [09:00, 09:15) — đủ 15 bar, thiếu bar ⇒ bỏ cả ngày. Bar ATO (08:45–08:59) không tính |
| Vào lệnh | bar 1 phút **đóng** trên đỉnh biên ⇒ Long, dưới đáy ⇒ Short; chỉ khi đang phẳng. Râu xuyên biên nhưng đóng trong ⇒ không vào |
| Khớp | MTL ở tick kế tiếp sau khi bar đóng (engine: tick mở của bar sau, ask/bid + 1 bước trượt) |
| Stop | cạnh đối diện của biên |
| Chốt lời | giá đóng bar tín hiệu ± 1,5 × R (R = khoảng cách giá đóng → stop), tròn bước giá |
| Giới hạn | tối đa 2 tín hiệu vào/ngày (tính cả tín hiệu bị risk từ chối) |
| Thoát khác | risk flatten 14:25; không vào sau 14:15, ngày đáo hạn, … (risk_manager) |

## Chạy in-sample

Chưa chạy được trong phiên làm việc này: container không có dữ liệu bar (thư mục `data/` không
commit), API DNSE cần key (P1.6), và `fees.*` / `risk.*` trong settings còn null nên engine từ
chối chạy. Khi đã có:

1. Dữ liệu: `python scripts/import_dashboard_bars.py` (nhanh, **không** dùng để kết luận) hoặc
   P1.6 (`data/history/…`, dữ liệu thật).
2. Chốt mốc chia **trước khi chạy**, ghi vào `research/log.md`: ví dụ 2/3 số ngày đầu là
   in-sample, phần còn lại để dành cho P3.3 — không mở out-of-sample ở bước này.
3. Chạy (chỉ in-sample):

   ```
   python -m backtest.engine --bars data/imported \
       --strategy strategy.baseline_orb:OpeningRangeBreakout \
       --from <ngày đầu> --to <ngày cuối in-sample> --n-trials 1 \
       --out reports/orb_is.html
   ```

4. Ghi kết quả (kể cả xấu) vào `research/log.md`. Mỗi lần đổi tham số ⇒ thêm dòng, tăng `--n-trials`.

## Kiểm định độ bền (P3.3) — kết luận hiện tại: CHƯA CÓ BẰNG CHỨNG EDGE

**Chưa chạy được trên dữ liệu thật** (không có dữ liệu bar; `fees.*` / `risk.*` còn null). Vì vậy
hiện tại **không có bằng chứng nào cho thấy ORB có edge** — mặc định coi là KHÔNG có cho tới khi
được chứng minh. Không được paper/live chiến lược này dựa trên kỳ vọng.

Khi có dữ liệu thật (không phải snapshot), một lệnh duy nhất:

```
python -m backtest.robustness --bars data/history/... --split <ngày đầu OOS đã chốt> \
    --folds 4 --out reports/orb_robustness.html
```

`--n-trials` mặc định = số dòng cấu hình trong `research/log.md`.

Luật kết luận (chốt trong `backtest/robustness.py::VerdictRules` + test, **trước** khi có dữ liệu):

| Điều kiện | Ngưỡng |
|---|---|
| Dữ liệu tin được (không phải snapshot dashboard) | bắt buộc — nếu không ⇒ CHƯA ĐỦ BẰNG CHỨNG |
| Số lệnh out-of-sample | ≥ 30 — nếu không ⇒ CHƯA ĐỦ BẰNG CHỨNG |
| P&L out-of-sample sau phí | > 0 |
| Kiểm định OOS: p-value sign-flip × n_trials < 0,10, walk-forward theo lệnh ≥ 50% đoạn dương | đạt |
| P&L OOS với trượt giá 2 tick | > 0 |
| Biến thể ±20% từng tham số (chạy trên IN-SAMPLE) có lãi sau phí | ≥ 75% |
| Đoạn walk-forward theo ngày có lãi sau phí | ≥ 50% |

Trượt bất kỳ điều kiện nào (khi đã đủ dữ liệu) ⇒ **KHÔNG CÓ EDGE**. Đạt hết ⇒ CÓ BẰNG CHỨNG EDGE,
vẫn phải paper trade trước live.

Lưu ý chống tự lừa:
- OOS chỉ nhìn **một lần** với cấu hình #1. Nếu sau khi xem kết quả mà đổi sang một biến thể ±20%
  (hay bất kỳ tham số nào khác), phải ghi vào `research/log.md`, `n_trials` tăng ít nhất 7 (gốc + 6
  biến thể đã xem), và cần một đoạn OOS **mới** chưa từng nhìn.
- Bộ kiểm định đã được thử trên dữ liệu giả: có edge cài sẵn ⇒ ra CÓ; random walk ⇒ không ra CÓ;
  dữ liệu snapshot ⇒ CHƯA ĐỦ (`tests/test_robustness.py`).

## 5 lý do kết quả backtest này có thể ĐẸP HƠN thực tế

1. **Khớp lệnh lúc phá biên lạc quan.** Engine khớp ở tick mở bar sau với spread 1 bước + trượt
   1 bước. Thời điểm phá biên là lúc giá chạy nhanh, sổ lệnh mỏng và nhiều người cùng đặt MTL:
   trượt thật có thể vài bước, phần MTL chưa khớp còn thành LO (± 1 bước) và có thể không khớp,
   bị lỡ đúng những lệnh thắng đẹp nhất. Dữ liệu bar không có bid/ask thật ⇒ spread là giả định.
2. **Stop phía sàn chưa chắc tồn tại.** Backtest dùng `OrderType.STOP` kích hoạt ngay tại sàn;
   DNSE/SSI chưa thấy lệnh stop trong API lệnh thường (broker_notes §4 ❓). Nếu bot phải tự theo
   dõi giá rồi gửi MTL, mỗi lần dừng lỗ chậm thêm độ trễ mạng + xử lý ⇒ lỗ to hơn, nhất là khi
   giá giật ngược qua biên. (Ngược lại engine cho stop khớp ở đáy/đỉnh bar − trượt, khá bi quan —
   hai sai số không triệt tiêu nhau một cách đáng tin.)
3. **Dữ liệu import từ dashboard là snapshot ~2 giây.** Đỉnh/đáy bar bị thiếu các cú giật ngắn ⇒
   biên 15 phút **hẹp hơn** thật, stop gần hơn, R nhỏ hơn; nhiều "phá biên" trong dữ liệu không
   phải phá biên thật, và chạm stop/chốt lời trong dữ liệu khác với thật. Volume không tin được
   nên không lọc được phá biên giả bằng khối lượng.
4. **Thiên lệch chọn cấu hình.** "15 phút, 1,5R, 2 lệnh/ngày" là cấu hình ORB phổ biến — đã được
   chọn vì nó từng hiệu quả ở thị trường khác/giai đoạn khác (chọn lọc ngầm, không có trong
   `n_trials`). Mẫu in-sample ngắn (vài chục ngày, một chế độ biến động) nên vài ngày trend mạnh
   có thể quyết định toàn bộ P&L; p-value chưa tính các cấu hình người khác đã thử trước mình.
5. **Chi phí chưa đủ.** `fees.*` đang null — khi điền cần đủ phí môi giới, phí sở, phí VSDC, thuế
   (nếu thuế tính theo giá trị hợp đồng thì công thức cố định/HĐ hiện tại sẽ đánh giá thấp chi
   phí). Backtest bỏ qua chi phí vốn ký quỹ (~18% giá trị HĐ), phí quản lý tài sản ký quỹ VSDC
   theo ngày, và các ngày không giao dịch được (lỗi kết nối, kill-switch, roll hợp đồng) — trong
   khi giả định bot luôn có mặt đúng lúc 09:15.

Thêm (không tính vào 5): risk_manager trong backtest không bao giờ thấy feed lệch / broker mất
kết nối, nên không có ngày nào bị chặn vào lệnh vì lý do kỹ thuật như live.
