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

## 2. Tạo task (Command Prompt **Run as administrator** — cmd thường có thể báo `Access is denied`)

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

Hoặc làm phần Conditions/Settings bằng PowerShell **Run as administrator** (đã dùng trên laptop 12/11/2026):

```powershell
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew
Set-ScheduledTask -TaskName "VN30F Recorder" -Settings $s
(Get-ScheduledTask -TaskName "VN30F Recorder").Settings | Select StartWhenAvailable, WakeToRun, DisallowStartIfOnBatteries, ExecutionTimeLimit
# mong đợi: True True False PT8H
powercfg /change standby-timeout-ac 0                                   # cắm sạc: không ngủ
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0        # cắm sạc: gập màn hình không ngủ
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1            # cho task đánh thức máy
powercfg /setactive SCHEME_CURRENT
```

> Laptop: task tạo bằng `schtasks /Create` mặc định **chỉ chạy khi cắm sạc**. Chạy pin ⇒ Windows bỏ qua lượt chạy,
> *Last Run Result* `0x800710E0` (PowerShell hiện `-2147020576`), không có log/dữ liệu (gặp 12/11/2026). Lệnh trên sửa.
> *Logon Mode: Interactive only* ⇒ chỉ chạy khi đúng *Run As User* đang đăng nhập (khoá màn hình vẫn chạy).

Và tắt chế độ ngủ trong giờ giao dịch (Control Panel → Power Options → *Put the computer to sleep*:
**Never**, hoặc ít nhất lớn hơn 7 tiếng).

## 2b. Task canh recorder — báo Telegram khi không ghi (thêm 12/11/2026)

Recorder không tự báo được khi nó KHÔNG chạy (12/11/2026: task bị Windows chặn vì laptop chạy pin, không ai
biết). `scripts/check_recorder.py` chạy riêng, mỗi 30′ trong giờ giao dịch: trong phiên liên tục mà
`data/raw/<hôm nay>` không có file, hoặc file mới nhất cũ hơn `recorder.watch_max_age_seconds` (180 s) ⇒ gửi
⚠️ lên Telegram kênh `trading` (cần `TELEGRAM_TRADING_*` trong `.env`). Ngoài phiên liên tục / ngày nghỉ ⇒ im lặng.

Thử kênh trước (trong giờ giao dịch, đang có recorder ⇒ nhận "✅ Recorder đang ghi"):

```bat
.venv\Scripts\python.exe scripts\check_recorder.py --notify-ok
```

Tạo task (PowerShell **Run as administrator**, sửa đường dẫn nếu repo không ở `D:\Future-trading`) — chạy
09:10, 09:40, …, 14:10 các ngày T2–T6; `pythonw.exe` để không bật cửa sổ đen mỗi 30′:

```powershell
$repo = "D:\Future-trading"
$act = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\pythonw.exe" -Argument "`"$repo\scripts\check_recorder.py`"" -WorkingDirectory $repo
$trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 09:10
$trg.Repetition = (New-ScheduledTaskTrigger -Once -At 09:10 -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours 5)).Repetition
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "VN30F Recorder Watch" -Action $act -Trigger $trg -Settings $set -Force
Start-ScheduledTask -TaskName "VN30F Recorder Watch"; Start-Sleep 10
Get-ScheduledTaskInfo -TaskName "VN30F Recorder Watch" | Select LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult`: `0` = recorder ổn (hoặc ngoài giờ), `1` = đã báo Telegram. Recorder chết lúc nghỉ trưa ⇒ báo ở
lần 13:10. Chỉ báo khi có vấn đề ⇒ hỏng kéo dài thì nhận tin mỗi 30′ tới khi sửa.

Báo cáo cuối ngày (thêm 12/11/2026): sau khi dừng ghi (15:00) và kiểm định, recorder gửi một tin lên Telegram
kênh `trading` — "✅ Recorder <ngày>: dữ liệu ổn" hoặc "⚠️ …" kèm số tick, giờ đầu→cuối, khoảng trống, hụt KL, số
bar, tối đa 5 vấn đề (`recorder.notify_end_of_day`). Không nhận tin lúc ~15:01 ⇒ recorder không chạy tới cuối ngày.

## 3. Kiểm tra hằng ngày

- Task Scheduler → *Last Run Result*:
  - `0x0`: êm (kể cả ngày nghỉ — script thoát ngay);
  - `0x1`: feed chết bất thường — dữ liệu tới lúc đó vẫn được lưu, xem log;
  - `0x2`: thiếu khoá trong `.env`;
  - `0x3`: đang có một recorder khác chạy trên máy;
  - `0x41301` (`267009`): đang chạy;
  - `0x800710E0` (`-2147020576`): Windows từ chối chạy (thường do đang dùng pin) — xem mục 2.
- Dữ liệu: `C:\vnbot\data\raw\YYYY-MM-DD\ticks.parquet` (sau 15:00). Còn file `part-*.parquet` nghĩa là
  bước gộp cuối ngày chưa chạy xong — không mất gì, lần chạy sau / `compact_day` sẽ gộp.
- Log: `logs\record_YYYY-MM-DD.jsonl` (PowerShell 5.1: `Get-Content … -Encoding UTF8` để đọc đúng dấu) — tìm `im lặng`, `mất kết nối`, `ghi … thất bại`.
- Cuối log có báo cáo kiểm định (khoảng trống, hụt khớp lệnh, lệch DNSE/SSI, so với bar vnstock);
  bản đầy đủ ở `data\validation\YYYY-MM-DD.json`, bar 1 phút ở `data\bars\YYYY-MM-DD.parquet`.
  Chạy lại tay: `.venv\Scripts\python.exe scripts\validate_day.py YYYY-MM-DD --ref-price <giá TC>`.

## 4. Việc định kỳ

- Đầu năm: cập nhật `config/holidays.yaml` (chạy `python scripts/check_calendar.py`).
- Nếu dùng `--krx`: đổi mã sau mỗi lần roll hợp đồng.
