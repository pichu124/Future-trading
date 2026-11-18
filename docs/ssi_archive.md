# Lưu trữ nến 1 phút SSI hằng tuần

**Vì sao:** SSI chỉ giữ nến 1 phút của chỉ số VN30 khoảng 1 tháng (có từ 15/10/2026 khi kiểm ngày 16/11), và đã làm mất
dữ liệu một số HĐ cũ (VN30F2509, VN30F2512 trả rỗng dù ngày 11/11 còn tải được). Kho riêng giữ dữ liệu lại để kiểm #8
(basis quá đà) và các ý tưởng sau trên dữ liệu MỚI. Chỉ nghiên cứu; bot không đọc kho này.

**Ở đâu:** nhánh dữ liệu `claude/ssi-bar-archive` (không có code), thư mục `ssi/ohlc1m/<HĐ>/<ngày>.parquet` và
`ssi/index1m/VN30/<ngày>.parquet`. Bắt đầu từ 15/10/2026.

## Quy trình (tác vụ hẹn giờ sáng thứ Bảy làm đúng thứ tự này)

Giới hạn: chỉ ĐỌC dữ liệu thị trường qua `mcp__SSI_MCP__marketdata_list_intraday_stock_infor`; chỉ thêm / thay file
trong `ssi/` của nhánh `claude/ssi-bar-archive`; không sửa code, không đụng nhánh code hay `main`, không mở pull request.

1. Thư mục làm việc: repo code (có `scripts/`). Mở worktree nhánh lưu trữ cạnh đó:

```bash
git fetch origin claude/ssi-bar-archive
git worktree prune
[ -e ../ssi-archive/.git ] || git worktree add --force -B claude/ssi-bar-archive ../ssi-archive origin/claude/ssi-bar-archive
git -C ../ssi-archive merge --ff-only origin/claude/ssi-bar-archive
```

2. Danh sách lệnh tải cho tuần vừa xong (`--to` = thứ Sáu vừa qua, YYYY-MM-DD):

```bash
python scripts/ssi_archive_plan.py --to <thứ Sáu>
```

   Mỗi dòng `<mã> <range> <toDate>` là một lệnh gọi tool với `{"symbol": mã, "range": range, "toDate": toDate}`.

3. Lưu kết quả thô của MỖI lệnh vào một thư mục tạm NGOÀI hai repo, tên `<mã>_<YYYYMMDD>.json`: kết quả lớn bị harness
   lưu ra file ⇒ `cp` nguyên file đó; kết quả nhỏ (rỗng) ⇒ ghi nguyên văn JSON. Lỗi `RATE_LIMITED` ⇒ chờ ~1 phút, gọi
   lại một lần. Không đọc nội dung nến vào ngữ cảnh; chỉ in số tick và ngày đầu/cuối bằng python ngắn.

4. Chuyển sang Parquet vào kho lưu trữ:

```bash
python scripts/import_ssi_intraday.py <thư mục tạm> --out ../ssi-archive/ssi
```

5. Commit và push đúng các file `ssi/` mới:

```bash
git -C ../ssi-archive add ssi
git -C ../ssi-archive commit -m "lưu trữ SSI: tuần tới <thứ Sáu>"
git -C ../ssi-archive push -u origin claude/ssi-bar-archive
```

   Không có gì mới (cả tuần nghỉ) ⇒ không commit.

6. Báo 3–5 dòng: mỗi mã bao nhiêu tick / ngày; ngày giao dịch trong tuần thiếu nến chỉ số hoặc thiếu HĐ F1M; push có
   thành công không. Mã chỉ số trả rỗng cả tuần trong khi có ngày giao dịch ⇒ nói rõ (có thể SSI đổi chính sách).
