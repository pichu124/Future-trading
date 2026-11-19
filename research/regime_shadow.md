# Nhật ký regime "shadow" của nhóm AI

**Chỉ nghiên cứu.** Mỗi sáng trước giờ mở cửa, một nhóm AI tranh luận rồi ghi dự báo về phiên hôm nay vào một file
JSON. Không module nào của bot đọc các file này (`tests/test_regime_shadow_isolation.py`); CLAUDE.md mục 8 giữ nguyên:
LLM không nằm trong đường ra quyết định live. Sau đủ số phiên, `scripts/score_regime_calls.py` trả lời một câu hỏi:
**nhận định của nhóm AI có dự báo tốt hơn một baseline thống kê đơn giản không?**

Luật kết luận đăng ký trước ở `research/log.md` ("Nhật ký regime shadow"); số nằm ở `config/settings.yaml` mục
`research.regime_shadow`.

## 1. Ba câu hỏi (cố định)

Thực tế tính trên nến 1 phút của hợp đồng bot giao dịch hôm đó (script in ra tên HĐ), trong khoảng 09:00 → 14:45:
**mở** = giá mở nến 09:00, **đỉnh/đáy** = cực trị cả phiên, **đóng** = giá đóng nến ATC 14:45.
**ADR10** = trung bình (đỉnh − đáy) của 10 phiên liền trước.

| Câu hỏi | Lớp | Định nghĩa thực tế |
|---|---|---|
| `vol` | `low` / `normal` / `high` | (đỉnh − đáy) / ADR10: < 0,8 ⇒ low; > 1,2 ⇒ high; còn lại normal |
| `day_type` (câu hỏi chính) | `trend` / `range` | \|đóng − mở\| / (đỉnh − đáy) ≥ 0,5 ⇒ trend; còn lại range |
| `direction` | `up` / `flat` / `down` | (đóng − mở): ≥ +0,2 × ADR10 ⇒ up; ≤ −0,2 × ADR10 ⇒ down; còn lại flat |

Mỗi câu trả lời là một **phân phối xác suất** trên các lớp (tổng = 1), không phải một nhãn cứng. Chấm bằng Brier nên
nói đúng mức tự tin thì được điểm tốt nhất: không chắc thì để xác suất gần tần suất thường gặp, đừng dồn về một lớp.

## 2. Quy trình mỗi sáng (phiên Claude hẹn giờ làm theo đúng thứ tự này)

Giới hạn cứng — vi phạm bất kỳ điều nào thì DỪNG, không ghi gì:

- Chỉ được thêm đúng MỘT file `research/regime_calls/YYYY-MM-DD.json` (do script tạo). Không sửa code, cấu hình, tài
  liệu, hay nhận định của ngày khác. Không mở pull request.
- Không gọi bất kỳ tool nào đặt/sửa/huỷ lệnh, tạo/kích hoạt/xoá chiến lược, hay đụng tới tài khoản (kể cả các tool
  `strategy_*` của DNSE và các tool tài khoản/danh mục của Finhay). Chỉ đọc dữ liệu thị trường.
- Chỉ dùng thông tin có TRƯỚC giờ mở cửa hôm nay. Thiếu dữ liệu thì ghi "không có", không bịa số.
- Không tự đặt giờ, ngày hay hợp đồng trong payload; không sửa tay file JSON sau khi script ghi.

### Bước 0 — Chuẩn bị và kiểm cửa sổ

Làm trong một worktree riêng (`../regime-calls`, cạnh thư mục repo) để không đụng nhánh đang làm việc của phiên. Chạy
từ thư mục gốc repo:

```bash
git fetch origin claude/regime-shadow-calls
git worktree prune
[ -e ../regime-calls/.git ] || git worktree add --force -B claude/regime-shadow-calls ../regime-calls origin/claude/regime-shadow-calls
cd ../regime-calls
git merge --ff-only origin/claude/regime-shadow-calls
python scripts/regime_call.py --check
```

Mọi lệnh `python scripts/…` và `git add/commit/push` ở các bước sau đều chạy TRONG `../regime-calls`.

Mã thoát: `0` ghi được (dòng in ra có ngày và hợp đồng) · `3` ngày nghỉ · `4` đã từ 08:45 trở đi · `5` hôm nay đã có
nhận định. Khác `0` ⇒ kết thúc ngay, không làm gì thêm.

### Bước 1 — Gom "gói dữ liệu trước giờ mở"

Ký hiệu: D0 = hôm nay, D-1 = phiên giao dịch liền trước (lấy từ dữ liệu, đừng tự trừ 1 ngày lịch). Công thức đã kiểm
bằng gọi thử ngày 16/11/2026 (trong phiên — hành vi lúc sáng sớm chưa kiểm, nên luôn tự lọc theo ngày):

1. `mcp__DNSE_MCP__get_workflow_guide {"workflow":"market_analysis"}` và
   `mcp__Finhay__get_workflow_guide {"workflow":"market-briefing"}` (hai connector yêu cầu gọi trước; bỏ qua mọi bước
   về tài khoản/danh mục).
2. `mcp__SSI_MCP__marketdata_list_stock_daily_data {"symbol":"VN30F1M","range":"3m","pageSize":20}` — OHLC ngày của
   HĐ tháng gần nhất (giá theo điểm, `trading_date` dạng DD/MM/YYYY, mới nhất trước). Bỏ dòng có ngày = D0 nếu có.
   Tính ADR10 = trung bình (high − low) 10 phiên gần nhất; ghi giá đóng D-1 và biên độ từng phiên. Ngày đáo hạn
   (script in ra HĐ tháng kế tiếp) ⇒ gọi thêm với `"symbol":"VN30F2M"` để lấy giá đóng D-1 của đúng hợp đồng đó.
3. `mcp__DNSE_MCP__price_list_derivative_contracts {}` — dòng `symbolType = "VN30F1M"`: mã KRX, `basicPrice`,
   trần/sàn, `deListedDate`. `basicPrice` lệch giá đóng D-1 ở bước 2 ⇒ ghi chú (có thể chưa sang ngày).
4. `mcp__SSI_MCP__marketdata_list_index_daily_data {"indexCode":"VN30","range":"3m"}` và cùng lệnh với
   `"indexCode":"VNINDEX"` — 15 phiên gần nhất trước D0. Basis D-1 = đóng VN30F1M − đóng VN30.
5. `mcp__Finhay__get_index_foreign_trading_history {"symbol":"VN30","from":"<D0 − 21 ngày>","to":"<D-1>"}` (ngày dạng
   YYYY-MM-DD) — `net.total.value` từng phiên (khối ngoại trên cổ phiếu cơ sở).
5b. **Khối ngoại trên phái sinh — dòng lệnh và vị thế ròng (từ 20/11/2026):**

   ```bash
   python scripts/foreign_futures_position.py --plan --date <D0 dạng YYYY-MM-DD>
   ```

   in mã KRX của mọi HĐ đang lưu hành (2 tháng gần + 2 quý gần). Với MỖI mã gọi
   `mcp__SSI_MCP__marketdata_list_daily_stock_infor {"symbol":"<mã>","range":"1y","toDate":"<D-1 DD/MM/YYYY>","pageSize":200}`;
   trả đúng 200 dòng thì gọi thêm `"pageIndex":2` (rồi 3…) tới khi < 200 dòng. Lưu nguyên văn từng kết quả vào một thư
   mục tạm NGOÀI repo (`cp` file harness lưu ra; kết quả nhỏ thì ghi JSON nguyên văn), rồi chạy

   ```bash
   python scripts/foreign_futures_position.py <thư mục tạm> --date <D0>
   ```

   và dán NGUYÊN VĂN kết quả vào gói dữ liệu (mục 3b). Script giữ dòng `close_raw` ≠ "0" (SSI trả 2 dòng/ngày), bỏ mọi
   phiên từ D0, in: vị thế ròng ước tính từng HĐ (Σ mua − bán từ phiên niêm yết, cách của vnstock-dashboard; nguồn
   KHÔNG có OI), ròng 5 phiên gần nhất, tổng mọi HĐ và tổng không tính HĐ đáo hạn hôm nay. Quanh ngày đáo hạn đọc cả hai
   tháng: khối ngoại chuyển vị thế sang tháng sau (17/11/2026: F2611 ròng −1.538 nhưng F2612 +1.150). Script in
   "CẢNH BÁO … còn trang" ⇒ tải tiếp trang rồi chạy lại. OI và tự doanh không có ⇒ ghi vào `inputs.missing`.
6. `mcp__SSI_MCP__marketdata_list_foreign_currency_converter {"bank":"VCB","range":"1w"}` — dòng
   `currency_name = "VCB_VND-USD"` (kết quả xếp ngày TĂNG dần, các cặp tiền xáo trộn, có thể có cả dòng D0): lấy
   dòng mới nhất và dòng liền trước; ghi rõ ngày của số đang dùng.
   Số VCB của chính ngày D0 KHÔNG lấy (người dùng quyết 18/11/2026: không mở host vietcombank.com.vn); USD/VND sáng D0
   xem ở `overnight_pack.py` (Yahoo). Không ghi "thiếu tỷ giá VCB D0" vào `inputs.missing`.

7. `mcp__Finhay__list_news {"page_size":50}` — chỉ giữ bài có `published_at` trong [15:00 D-1, giờ chạy]; loại bài ghi
   ngày tương lai (hay nổi lên đầu); ghi nguồn và giờ đăng. Một trang chỉ phủ khoảng một giờ tin ⇒ lật thêm bằng
   `page` tối đa 4 trang; chưa phủ hết khoảng thời gian (nhất là qua cuối tuần) thì ghi rõ vào `inputs.missing`.
8. Tuỳ chọn, nếu còn thời gian: nến 1 phút phiên D-1 qua
   `mcp__SSI_MCP__marketdata_list_intraday_stock_infor {"symbol":"VN30F1M","range":"1d","toDate":"<D-1 dạng DD/MM/YYYY>"}`
   (kết quả rất dài, bị lưu ra file ⇒ đọc bằng python, `data.ticks`, đảo lại thứ tự thời gian; `trading_date` thực tế
   dạng DD/MM/YYYY HH:mm:ss dù mô tả tool ghi MM/dd) để mô tả dáng phiên
   trước: hướng buổi sáng/chiều, đóng gần đỉnh hay đáy.
9. **Thế giới qua đêm + tin tới sáng (BẮT BUỘC từ 19/11/2026):** chạy trong worktree `../regime-calls`

   ```bash
   python scripts/overnight_pack.py
   ```

   Script đọc web công khai (đã mở mạng cho môi trường cloud: `query1.finance.yahoo.com`, `cafef.vn`, `vnexpress.net`,
   `feeds.content.dowjones.io`): S&P 500, Nasdaq, Dow, SOX, VIX, lợi suất Mỹ 10 năm, dầu WTI/Brent, vàng, DXY,
   USD/VND, Nikkei, Kospi (mỗi số kèm thời điểm) và tiêu đề tin cafef / vnexpress / MarketWatch từ 15:00 phiên trước
   tới lúc chạy. Dán NGUYÊN VĂN kết quả vào gói dữ liệu (mục 7 "Thế giới qua đêm" và bổ sung mục 6 "Tin"); dòng
   `THIẾU:` của script chép vào `inputs.missing`. Connector Finhay đóng băng số quốc tế từ 24/10/2026 ⇒ không dùng
   Finhay cho mục này. Script lỗi hẳn ⇒ ghi "không có dữ liệu thế giới qua đêm" và làm tiếp (không dừng cả quy trình).

Cạm bẫy đã gặp: SSI trả mảng RỖNG kèm `status: ok` khi tham số không hợp (kiểm số dòng); `range:"1d"` không kèm ngày
vào thứ Hai trả rỗng; Finhay `get_index_history` có đỉnh/đáy sai và trả cả dòng hôm nay dở dang ⇒ không dùng;
DNSE `price_get_history` không nhận mã phái sinh; DNSE `price_get_derivatives` dừng ở 27/10 rồi trả số 0; các tool khối
ngoại phái sinh của DNSE cần đăng nhập. Mỗi lần chạy kiểm lại ngày/`updated_at` của dữ liệu trước khi tin.

Bước này không quá ~12 phút. Nên giao cả bước cho MỘT agent phụ (kết quả các tool rất dài) và chỉ nhận về gói dữ
liệu. Viết gói dữ liệu thành MỘT khối văn bản (số liệu + ngày của từng số + những gì thiếu) để
phát nguyên văn cho các agent ở bước 2–3.

### Bước 2 — Một agent đơn (độc lập với tranh luận)

Gọi một agent phụ, đưa cho nó: mục 1 của tài liệu này (ba câu hỏi), gói dữ liệu, và yêu cầu trả về ba phân phối xác
suất cùng lập luận ngắn. Không cho nó dùng tool. Kết quả là `single` và `transcript.single`. Có thể chạy song song với
vòng 1 của bước 3 cho kịp giờ, miễn là nó không thấy lập luận của ai và không agent nào ở bước 3 (kể cả trọng tài)
thấy kết quả của nó.

### Bước 3 — Tranh luận

Ba agent phụ, mỗi agent nhận mục 1 + gói dữ liệu, không dùng tool:

- `trend_advocate`: tìm lý lẽ mạnh nhất cho một ngày xu hướng / biến động cao, và hướng của nó.
- `reversion_advocate`: tìm lý lẽ mạnh nhất cho một ngày đi ngang / biến động thấp, quay về giữa biên.
- `skeptic`: chỉ ra chỗ hai bên suy diễn quá dữ liệu, nhắc tần suất nền (ngày thường là gì khi không biết gì thêm) và
  những gì gói dữ liệu KHÔNG cho biết.

Vòng 1: ba agent chạy song song, mỗi agent nêu lập luận + ba phân phối. Vòng 2: mỗi agent đọc lập luận vòng 1 của hai
agent kia, phản biện và chốt ba phân phối của mình. Sau đó một agent `judge` nhận toàn bộ hai vòng, cân nhắc và đưa
ra ba phân phối cuối cùng cùng lý do — đó là `debate`. Trọng tài không được lấy trung bình máy móc, và không có nghĩa
vụ phải nghiêng về bên nói tự tin hơn.

### Bước 4 — Ghi

Chạy lại `python scripts/regime_call.py --check` (tranh luận có thể đã lố giờ). Vẫn `0` thì viết payload ra một file
tạm NGOÀI repo rồi ghi:

```json
{
  "model": "<mã mô hình phiên này đang chạy>",
  "inputs": {"d_minus_1": "YYYY-MM-DD", "adr10": 0.0, "prev_close": 0.0, "basis_prev": 0.0,
             "sources": ["<tool đã gọi>"], "missing": ["<thứ không lấy được>"], "data_pack": "<khối văn bản bước 1>"},
  "single": {"vol": {"low": 0.0, "normal": 0.0, "high": 0.0}, "day_type": {"trend": 0.0, "range": 0.0},
             "direction": {"up": 0.0, "flat": 0.0, "down": 0.0}},
  "debate": {"vol": {}, "day_type": {}, "direction": {}},
  "transcript": {"single": "…", "trend_advocate": "…", "reversion_advocate": "…", "skeptic": "…", "judge": "…"}
}
```

```bash
python scripts/regime_call.py --payload /đường/dẫn/payload.json
```

Script kiểm payload, đóng dấu giờ + ngày + hợp đồng, và từ chối nếu đã 08:45, là ngày nghỉ, hoặc đã có file. Mã thoát
`2` (payload lỗi) ⇒ sửa payload rồi chạy lại; `4`/`5` ⇒ dừng, không commit.

### Bước 5 — Commit và push đúng một file

```bash
git add research/regime_calls/<ngày>.json
git commit -m "regime shadow: nhận định <ngày>"
git push -u origin claude/regime-shadow-calls
```

Kết thúc bằng 3–4 dòng: ngày, hợp đồng, ba phân phối `debate`, dữ liệu nào thiếu. Push bị từ chối ⇒ báo nguyên văn
lỗi, không thử nhánh khác.

## 3. Chấm điểm (người dùng chạy trên máy có nến 1 phút)

Nhận định nằm trên nhánh `claude/regime-shadow-calls`. Tạo một worktree riêng để không đụng thư mục làm việc chính:

```bash
git fetch origin claude/regime-shadow-calls
```

```bash
git worktree add ../regime-calls claude/regime-shadow-calls
```

Những lần sau chỉ cần `git -C ../regime-calls pull`. Rồi chấm (đường dẫn nến là của thư mục làm việc chính):

```bash
python scripts/score_regime_calls.py --bars data/history/ohlc1m data/bars --calls ../regime-calls/research/regime_calls
```

Khi code shadow chưa được merge vào nhánh bạn đang đứng, chạy lệnh trên từ trong `../regime-calls` (nhánh đó có sẵn
code) và trỏ `--bars` bằng đường dẫn tuyệt đối tới kho nến.

Bảng in Brier (thấp hơn = tốt hơn) của `debate`, `single` và hai baseline `climatology`, `markov` cho từng câu hỏi.
Trước 60 phiên chấm được, kết luận luôn là **CHƯA ĐỦ**; các con số trước đó chỉ để theo dõi.

## 4. Thử đường ống

`python scripts/regime_call.py --payload x.json --dry-run` bỏ chốt giờ và ghi vào `research/regime_calls/dryrun/`. Bản
dry-run không bao giờ được chấm (thư mục con bị bỏ qua; bản dry-run đặt lẫn vào thư mục chính làm bộ chấm báo lỗi).
