# Future-trading — bot intraday VN30F

Bot giao dịch intraday hợp đồng tương lai chỉ số VN30. Feed và thực thi chính là DNSE LightSpeed, dự phòng là SSI FastConnect.
Luật dự án ở [`CLAUDE.md`](CLAUDE.md), lộ trình và prompt từng bước ở [`PROMPTS.md`](PROMPTS.md), tiến độ ở [`docs/progress.md`](docs/progress.md).

> ⚠️ Chưa có chiến lược, chưa kết nối broker. Mặc định `TRADING_MODE=paper`.

## Cài đặt (Windows)

```powershell
git clone https://github.com/pichu124/Future-trading.git C:\future-trading
cd C:\future-trading
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # rồi điền secret
python -m pytest
```

Đặt repo **ngoài** thư mục Dropbox (xem CLAUDE.md mục 3b).

## Việc cần làm ngay

1. **Cứu dữ liệu VN30F từ vnstock-dashboard.** Thư mục `_live/archive` tự xoá sau 30 ngày:
   ```powershell
   python scripts\import_dashboard_bars.py --warehouse C:\vnstock\data_warehouse
   ```
   Lệnh này ghi `data\imported\YYYY-MM-DD.parquet`. Đây là dữ liệu snapshot ~2 giây và không có volume, chỉ dùng để thử nhanh.
2. **Điền cấu hình.** `python -m core.settings` liệt kê các khoá còn `null` (phí, ngưỡng rủi ro, tên máy được chạy bot).
3. **Bổ sung lịch nghỉ** vào `config/holidays.yaml`. `python scripts\check_calendar.py` cảnh báo khi còn thiếu và in lịch đáo hạn để đối chiếu.
4. Sang **P1.1** trong `PROMPTS.md`.

## Cấu trúc

```
core/        models, calendar, clock (lịch phiên), contracts (mã HĐ, đáo hạn, roll), settings
backtest/    validation.py — p-value sign-flip, CI Sharpe, walk-forward, phạt đa thử nghiệm
live/        instance_lock.py — một máy, một tiến trình
monitoring/  telegram.py — kênh cảnh báo trading
utils/       atomic_io.py — ghi Parquet/JSON nguyên tử
scripts/     import_dashboard_bars.py, check_calendar.py
config/      settings.yaml, holidays.yaml
feeds/ recorder/ strategy/ risk/ brokers/   (giai đoạn sau)
```
