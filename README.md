# Kho lưu trữ nến 1 phút SSI (chỉ dữ liệu, không có code)

Nhánh này chỉ chứa nến 1 phút tải qua kết nối SSI của Claude (`marketdata_list_intraday_stock_infor`), để không mất dữ
liệu khi SSI xoá: nến chỉ số VN30 chỉ được giữ khoảng 1 tháng, và một số HĐ cũ đã biến mất khỏi API (VN30F2509,
VN30F2512 — research/log.md 16/11/2026).

- `ssi/ohlc1m/<VN30FYYMM>/<YYYY-MM-DD>.parquet` — HĐ tương lai (F1M + F2M), cùng schema kho DNSE.
- `ssi/index1m/VN30/<YYYY-MM-DD>.parquet` — chỉ số VN30 (nến từ 09:15, khớp ATO HOSE).
- `volume_reliable = False`, `source = "ssi"`.

Cập nhật mỗi sáng thứ Bảy bằng tác vụ hẹn giờ; quy trình ở `docs/ssi_archive.md` trên nhánh code. Dùng khi backtest:
`git worktree add ..\ssi-archive origin/claude/ssi-bar-archive` rồi trỏ `--bars ..\ssi-archive\ssi\ohlc1m`.

**Lưu ý trung thực:** nến từ 02/11/2026 tới 16/11/2026 đã bị nhìn (giá ngày) trong lần chạy thử nhật ký regime shadow ⇒
không dùng làm dữ liệu kiểm định cho #7–#9 (research/log.md, "Đăng ký trước #7, #8, #9").
