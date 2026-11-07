# Chạy recorder mỗi ngày bằng Windows Task Scheduler

`scripts/record_today.py` tự biết hôm nay có giao dịch không (cuối tuần + `config/holidays.yaml`),
chờ tới `recorder.start` (08:30), ghi tới `recorder.stop` (15:00), gộp file ngày rồi thoát.
Chỉ cần cho Task Scheduler gọi nó **mỗi ngày thường lúc ~08:20**.

## 1. Chuẩn bị (một lần)

1. Chọn **một** máy (desktop — máy luôn bật). Không cần chạy trên laptop.
2. Đặt thư mục dữ liệu **ngoài Dropbox/OneDrive** (recorder từ chối chạy nếu nằm trong đó), ví dụ
   trong `config/settings.yaml`:
   ```yaml
   paths:
     data_dir: C:/vnbot/data
     state_dir: C:/vnbot/state
   ```
3. Cài môi trường và khoá:
   ```bat
   cd C:\vnbot\Future-trading
   py -3.11 -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   copy .env.example .env      &REM rồi điền DNSE_API_KEY, DNSE_API_SECRET
   ```
4. Chạy thử bằng tay trong giờ giao dịch (Ctrl+C để dừng sớm cũng không mất dữ liệu đã flush):
   ```bat
   .venv\Scripts\python.exe scripts\record_today.py --assume-computed-contract
   ```
   Kiểm tra `C:\vnbot\data\raw\<ngày>\` có file `part-*.parquet` mỗi 5 giây và log ở `C:\vnbot\logs\`.

> `--assume-computed-contract`: dùng mã hợp đồng do bot tự tính (tự đổi khi roll). Khi adapter DNSE
> (P4.1) có REST danh sách hợp đồng, script sẽ tự đối chiếu và bỏ cờ này. Nếu mã tính sai, feed sẽ
> im lặng và log báo "feed … im lặng". Muốn chắc chắn hơn thì dùng `--krx <mã>` (phải sửa mỗi tháng).

## 2. Tạo task (Command Prompt, đăng nhập bằng user sẽ chạy bot)

```bat
schtasks /Create /F /TN "VN30F Recorder" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:20 ^
  /TR "\"C:\vnbot\Future-trading\.venv\Scripts\python.exe\" \"C:\vnbot\Future-trading\scripts\record_today.py\" --assume-computed-contract"
```

Sau đó mở **Task Scheduler** → *VN30F Recorder* → *Properties* và chỉnh:

| Tab | Thiết lập |
|---|---|
| General | *Run whether user is logged on or not* (máy khoá màn hình vẫn chạy); bỏ *Run with highest privileges* |
| Conditions | Bỏ *Start the task only if the computer is on AC power*; tích *Wake the computer to run this task*; tích *Start only if the following network connection is available: Any connection* |
| Settings | Tích *Run task as soon as possible after a scheduled start is missed*; *Stop the task if it runs longer than*: **8 hours**; *If the task is already running*: **Do not start a new instance** |

Và tắt chế độ ngủ trong giờ giao dịch (Control Panel → Power Options → *Put the computer to sleep*:
**Never**, hoặc ít nhất lớn hơn 7 tiếng).

## 3. Kiểm tra hằng ngày

- Task Scheduler → *Last Run Result*:
  - `0x0`: êm (kể cả ngày nghỉ — script thoát ngay);
  - `0x1`: feed chết bất thường — dữ liệu tới lúc đó vẫn được lưu, xem log;
  - `0x2`: thiếu khoá trong `.env`;
  - `0x3`: đang có một recorder khác chạy trên máy.
- Dữ liệu: `C:\vnbot\data\raw\YYYY-MM-DD\ticks.parquet` (sau 15:00). Còn file `part-*.parquet` nghĩa là
  bước gộp cuối ngày chưa chạy xong — không mất gì, lần chạy sau / `compact_day` sẽ gộp.
- Log: `C:\vnbot\logs\record_YYYY-MM-DD.log` — tìm `im lặng`, `mất kết nối`, `ghi … thất bại`.

## 4. Việc định kỳ

- Đầu năm: cập nhật `config/holidays.yaml` (chạy `python scripts/check_calendar.py`).
- Nếu dùng `--krx`: đổi mã sau mỗi lần roll hợp đồng.
