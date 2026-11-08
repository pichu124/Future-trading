# Tiến độ

## 2026-11-08 — Nối TCBS vào runner (chọn làm broker chính hoặc dự phòng)

**Quyết định của người dùng:** `brokers.primary` / `brokers.backup` chọn được trong {dnse, ssi, tcbs}; giữ kiến trúc một-chính-một-dự-phòng; mặc định vẫn DNSE/SSI.

**Đã xong**
- `live/wiring.py` `build_live`: dựng đúng broker theo vai (khác nhau, tên không hỗ trợ ⇒ thoát 2). TCBS chỉ dựng khi `brokers.tcbs_enabled` + `api_verified` + đủ `TCBS_*`. Feed tổng hợp LUÔN DNSE + SSI (CLAUDE.md mục 3): DNSE không làm broker ⇒ chỉ cần khoá HMAC cho feed + `/instruments`; SSI không làm broker ⇒ chỉ cần API key/secret, token feed nằm trong `auth` (runner chờ `/otp ssi`). Stream lệnh cho broker SSI/TCBS chạy nền.
- `main()` truyền `auth` vào Runner; startup chờ OTP của mọi broker + token feed (`/otp dnse|ssi|tcbs`).
- Test viết trước (10 test mới trong `tests/test_live_wiring.py`): dnse+tcbs, tcbs+ssi, trùng/không hỗ trợ, TCBS chưa bật/chưa kiểm, thiếu env, feed vẫn cần khoá SSI, chờ ba OTP, `main` truyền đúng tham số. Thử phá 8 chốt ⇒ 1 sống (`main` không truyền `auth`) ⇒ thêm test ⇒ 8/8.
- 974 test.

**Lưu ý**: risk/quy trình khẩn cấp không giả định tên broker (đã kiểm) ⇒ đối ứng khẩn cấp chạy trên broker dự phòng bất kể là ssi hay tcbs. TCBS vẫn tắt mặc định.

## 2026-11-08 — Stream DE_ORDER cho TCBS (`TcbsOrderStream`)

**Đã xong** (theo tài liệu 7.3 chính thức)
- Kết nối `wss://openapi.tcbs.com.vn/ws/nesoi` với ping RFC 6455 TẮT (`tcbs_ws_connector`); `authenticate|base64({"jwt":…})` (JSON gọn đúng như ví dụ tài liệu) → chờ `authenticate|{"success":true}` rồi mới `subscribe` DE_ORDER + DE_OPEN_POSITION (chuỗi base64 khớp nguyên văn tài liệu); heartbeat TEXT `ping|1` mỗi 2s.
- `TcbsBroker.on_stream_frame`: DE_ORDER áp ngay khối lượng khớp cộng dồn (trùng không đếm đôi, bản tin cũ đến muộn không kéo lùi trạng thái); lệnh lạ (DE_ORDER không có refId) ⇒ `resync_requested` ⇒ `ping()` đọc lại REST ngay; DE_OPEN_POSITION ⇒ `stream_net`. Frame hỏng bị bỏ, không ném lỗi.
- Mỗi lần (re)connect ⇒ đọc lại REST (stream không số thứ tự, không phát lại). Xác thực bị từ chối ⇒ huỷ token (cần iOTP mới). Backoff min(2^n, 30)s; phiên đã xác thực rồi mới rớt không tính là thất bại; 10 lần thất bại liên tiếp ⇒ cảnh báo đúng một lần.
- Test viết trước `tests/test_tcbs_stream.py` (15); test bắt được 1 lỗi khi viết (phiên đã chạy tốt rồi rớt bị tính thất bại ⇒ backoff tăng mãi). Thử phá 15 cơ chế ⇒ 1 sống (lệnh lạ bị che bởi resync lúc connect) ⇒ thêm test ⇒ 15/15.
- 964 test.

**Lưu ý**: TCBS vẫn TẮT và chưa nối vào runner; máy chạy bot phải cho phép host `openapi.tcbs.com.vn` (môi trường cloud này chỉ mở `developers.tcbs.com.vn` để đọc tài liệu).

## 2026-11-08 — Adapter TCBS (`brokers/tcbs.py`) — broker thứ 3, TẮT mặc định

**Nguồn:** tài liệu chính thức `developers.tcbs.com.vn` v1.0.0 + đặc tả `openapi-v1.0.0.json` (cần mở host này: đã tạo môi trường cloud riêng "Future-trading" chỉ thêm `developers.tcbs.com.vn`, bạn đã duyệt). Gói PyPI `tcbs` không có provenance ⇒ không dùng. Ghi vào `docs/broker_notes.md` §2b (endpoint, field, mã trạng thái, mã lỗi, bảng ❓).

**Đã xong**
- `TcbsHttp`: Bearer, không retry; lỗi = HTTP ≥ 400 hoặc `rc < 1`; mã lỗi nghiệp vụ trong bảng của TCBS (kể cả kèm HTTP 500, vd -9999) ⇒ chắc chắn chưa tiếp nhận; 5xx/timeout không mã ⇒ "không rõ".
- `TcbsTokenManager`: API Key + **iOTP người dùng đọc trong app TCInvest** (`/otp tcbs <mã>`); token hết hạn = min(8 giờ theo tài liệu, `exp` trong JWT); không refresh. Cảnh báo API Key còn < 7 ngày.
- `TcbsBroker`: `refId` suy từ client id (TCBS KHÔNG chống trùng refId ⇒ không bao giờ gửi lại); lệnh "không rõ" tra sổ lệnh đúng theo `refId` (cách tài liệu chỉ), quá 15s ⇒ REJECTED. Lệnh thị trường = LO giá trần/sàn (API không mô tả MTL ❓). Sửa: giá và KL hai request (TCBS cấm sửa đồng thời), `nvol` là tổng, refId mới mỗi lần. Huỷ: -6004/-8004 (đã huỷ/đã khớp) an toàn, chỉ báo CANCELLED khi sổ lệnh xác nhận. Vị thế từ `data.detailData` (net/side lệch ⇒ lỗi). Khớp cộng dồn (`matchVolume`/`matchPriceBQ`), lệnh ngoài bot được theo dõi, stop do bot giữ.
- Chốt trước MỌI lệnh thay đổi: TRADING_MODE=live + `--live` + `brokers.tcbs_enabled` + `brokers.tcbs.api_verified` + token.
- `scripts/tcbs_trading_probe.py`: `--payload-only` hoặc iOTP → CHỈ ĐỌC (hạn API Key, tiểu khoản, trần/sàn + định dạng mã, vị thế, sổ lệnh trang 0/1/2); không đặt lệnh (không có môi trường thử).
- Test viết trước: `tests/test_broker_tcbs.py` (70), `tests/test_tcbs_probe.py` (3). Thử phá 24 cơ chế ⇒ 1 sống (mã orderStatus 7 bị chuỗi trạng thái che) ⇒ thêm ca ⇒ 24/24.
- 949 test.

**Chưa làm / ❓**: chưa nối vào runner (CLAUDE.md: chỉ bật sau khi DNSE + SSI ổn định); stream `DE_ORDER` chưa dùng (REST đủ, tài liệu nói stream không phát lại); lệnh điều kiện SOU/SOL (stop phía broker) chưa dùng; các ❓ ở broker_notes §2b (MTL, pageNo 0/1, mã cũ trong ví dụ, rate limit).

## 2026-11-08 — Feed tổng hợp DNSE + SSI cho chế độ paper

**Đã xong**
- `live/wiring.py` `build_paper_feed`: `paper.composite_feed: true` ⇒ `CompositeFeed(DNSE, SSI)` giống live (lệch giá / một feed im ⇒ ngừng mở vị thế, cảnh báo Telegram qua `queue_alert`); false (mặc định) ⇒ chỉ feed DNSE như cũ. Thiếu SSI_API_KEY/SECRET ⇒ thoát 2; bật mà không có Telegram ⇒ thoát 2 (không nhập được OTP).
- Runner nhận `auth`: đối tượng cần OTP không phải broker (token SSI cho feed). Khởi động: connect → chờ `/otp` cho cả broker lẫn auth → mới đối soát/giao dịch. `/otp ssi <mã>` tới token manager; trùng tên với broker (live) ⇒ chỉ broker nhận, không gửi mã hai lần. "Đang chờ OTP" giờ = đã xin mã VÀ chưa có token (bền hơn khi broker báo awaiting nhầm).
- `SsiTokenManager`: `name = "ssi"`, `trading_ready`, `connect()` (xin OTP đúng một lần khi chưa có token).
- Test viết trước `tests/test_paper_composite.py` (13 test); thử phá 9 cơ chế ⇒ 9/9 bị bắt.
- 876 test.

**Lưu ý**: paper với feed tổng hợp phải nhập OTP SSI mỗi lần khởi động (token chỉ ở bộ nhớ); SSI token chỉ-dữ-liệu có cần OTP không vẫn ❓ (broker_notes §2.4).

## 2026-11-08 — Hoàn tất phần checklist go-live làm được không cần khoá

**Đã xong**
- §3b: runner từ chối khởi động khi `paths.data_dir` / `state_dir` / `log_dir` nằm trong Dropbox/OneDrive (trước đó chỉ recorder kiểm).
- §4: đối chiếu mã hợp đồng với broker lúc khởi động live — `brokers/dnse.py` `fetch_vn30_futures` (`GET /instruments?symbol=…&marketId=DVX&securityGroupId=FU`, theo đặc tả OpenAPI chính thức `docs/dnseopenapi-en.yaml` của repo SDK) + `verify_contracts`: `symbolType` DNSE báo (VN30F1M/F2M/F1Q/F2Q) phải trùng mã bot tự tính, thiếu F1M ⇒ lỗi; `live/wiring.py` dùng danh sách này cho feed và chạy `preflight` (hợp đồng đang giao dịch sau roll phải có trong danh sách). Runner chạy preflight sau khi có token, TRƯỚC khi đối soát/giao dịch; lỗi ⇒ thoát 2. Live bỏ qua `--krx`/`--assume-computed-contract`.
- #14 (backtest bớt lạc quan): tick giả có bid/ask cách giá ≥ 1 bước mỗi phía (spread lẻ trước đây cho bán ở đúng giá); cực trị giữ lâu hơn độ trễ paper + 1s (trước: độ trễ ≥ 1s ⇒ stop khớp ở cực trị KIA); lệnh còn mở cuối dữ liệu vào số liệu (giá cuối, trừ ước phí đóng) kèm cảnh báo. Ba test cũ đổi kỳ vọng đúng 1 bước giá (Long thoát chịu thêm nửa spread). Chưa có backtest nào trên dữ liệu thật ⇒ không có kết quả nào bị ảnh hưởng.
- `tests/test_execution.py`: 20 test trực tiếp cho `ExecutionManager` với broker giả điều khiển được (khớp một phần, huỷ chưa xác nhận, lỗi mạng, broker chết, đảo chiều ngoài ý muốn, lệnh đóng kẹt, KILL/FLATTEN/khẩn cấp). Thử phá 14 cơ chế ⇒ 2 sống vì risk chặn hộ ⇒ siết test để kiểm chính ExecutionManager ⇒ 14/14.
- Thử phá thêm: đối chiếu hợp đồng + chặn Dropbox 7/7 (sau khi thêm test ngày đáo hạn), #14 6/6.
- `docs/golive_checklist.md`: thêm mục 0 "Trạng thái mới nhất", cập nhật §2/§3/§6.
- 865 test.

**Còn lại là việc của bạn** (không tự làm được): lịch nghỉ 2026–2027 theo thông báo của sở; `fees.*`, `risk.*`, `live.allowed_machine`, `backtest.initial_capital_vnd`; khoá API + ba script thăm dò; dữ liệu lịch sử; paper ≥ vài tuần.
- Tuỳ chọn chưa làm: `brokers/tcbs.py` (CLAUDE.md: chỉ bật sau khi DNSE + SSI ổn định), feed tổng hợp cho paper.

## 2026-11-08 — Logger JSON có cấu trúc (`monitoring/logger.py`)

**Đã xong**
- `JsonFormatter`: một dòng JSON mỗi bản ghi — `ts` (giờ VN, ms), `level`, `logger`, `msg`, mọi field `extra=`; datetime/Enum/dataclass/Decimal/set đều ghi được, kiểu lạ ⇒ repr; lỗi ⇒ `exc_type`, `exc_msg`, `exc` (traceback).
- Bảo mật (CLAUDE.md mục 7): khoá nhạy cảm ở mọi độ sâu (apiSecret, otp, passcode, *token, *secret, password, Authorization, X-Signature…) ⇒ `***`; giá trị bí mật từ `.env` (*_KEY/*_SECRET/*_TOKEN/*_PASSWORD, ≥ 6 ký tự) ⇒ `<secret>` trong message, field VÀ traceback; mẫu URL bot Telegram / `Bearer …` / `"tradingToken": …` bị xoá kể cả khi chưa đăng ký. Console cũng được lọc.
- `DailyJsonlHandler`: `logs/<prefix>_YYYY-MM-DD.jsonl` theo ngày giờ VN (đổi file lúc 00:00), append khi khởi động lại, flush từng dòng, lỗi ghi không làm chết bot, từ chối thư mục trong Dropbox. `setup_logging` gọi lại an toàn (không nhân đôi handler). `read_events(path, event=…)` bỏ dòng hỏng.
- Nối vào: `live/runner.py` (thay basicConfig; sự kiện `signal`, `risk_action`, `notify`), `core/execution.py` (`order` mỗi lần đổi trạng thái, `fill` kèm `net_after`, `exec_rejected` kèm mã risk, các `exec_*` khác), `scripts/record_today.py` (`record_*.jsonl` thay file .log). `log_event` không làm gì khi mức log tắt ⇒ backtest không chậm. Settings `paths.log_dir: logs`.
- Test viết trước `tests/test_logger.py`; test bắt được 2 lỗi thật khi nối: `Side(str, Enum)` bị ghi `"Side.BUY"` (phải xét Enum trước str), và test gọi `runner.main()` ghi log vào `logs/` của repo (chặn trong conftest). Thử phá 17 cơ chế ⇒ 16 bị bắt, 1 là điều kiện thừa ⇒ đã bỏ.
- Golive checklist §2: logger JSON ✔. `docs/runner.md` thêm mục Log + cách dựng timeline.
- 825 test.

**Giả định**
- Log chứa số tài khoản và mã lệnh (không phải bí mật) — vẫn KHÔNG nên chia sẻ công khai.
- Bí mật ngắn < 6 ký tự không được đăng ký (để không xoá nhầm chữ số trong log); OTP vẫn bị che qua tên field (`otp`/`passcode`) và bot không bao giờ log mã.

## 2026-11-08 — Nối LIVE vào runner (`live/wiring.py`) — vẫn KHOÁ tới khi bạn mở từng chốt

**Đã xong**
- `live/wiring.py` `build_live`: DNSE chính (`DnseBroker`) + SSI dự phòng (`SsiBroker`, cho `EmergencyHandler`) + `CompositeFeed` DNSE/SSI (golive #13 — lệch giá / im lặng ⇒ ngừng mở vị thế) + `SsiOrderStream` (tác vụ nền). Feed SSI và broker SSI dùng chung một `SsiTokenManager`. Không gọi mạng; thiếu bất kỳ thứ gì (`api_verified`, giá trị null, biến .env, file khoá SSI, broker không phải dnse/ssi) ⇒ ném lỗi ⇒ runner thoát 2 trước khi kết nối.
- `live/runner.py`:
  - `main()` nhánh live: TRADING_MODE=live + `--live` ⇒ `build_live`; live bắt buộc Telegram. Paper giữ nguyên (PaperBroker + feed DNSE).
  - `startup()`: kết nối mọi broker → **chờ `/otp`** cho broker có `trading_ready=False` (tối đa `live.otp_wait_seconds` = 600s; quá ⇒ thoát 2; `/kill` ⇒ thoát 0) → rồi mới đối soát vị thế/lệnh chờ trên cả hai broker.
  - Tác vụ nền (`background`): chạy sau khởi động, dừng TRƯỚC khi đóng broker; tác vụ chết ⇒ cảnh báo đúng một lần (khớp lệnh vẫn đọc qua REST).
  - `queue_alert`: cảnh báo của feed tổng hợp (callback đồng bộ) được gửi Telegram ở vòng kế tiếp.
- `DnseBroker.trading_ready`, `SsiBroker.trading_ready`; settings `live.otp_wait_seconds: 600`.
- Test viết trước `tests/test_live_wiring.py` (24 test): dựng đủ thành phần, từng chốt từ chối (api_verified mỗi bên, giá trị null, biến .env, file khoá, tên broker, live_flag), `main` từ chối, chờ OTP → đối soát sau, hết giờ, /kill, tác vụ nền. Thử phá 12 chốt ⇒ 11 bị bắt, 1 sống (dừng tác vụ nền) ⇒ thêm test thứ tự dừng ⇒ 12/12.
- 805 test.

**Vẫn chưa chạy live — các chốt còn đóng**
- `brokers.dnse.api_verified` / `brokers.ssi.api_verified` = false; null: `fees.*`, `risk.*`, `brokers.dnse.loan_package_id`, `brokers.dnse.trading_token_ttl_seconds`, `brokers.ssi.order_symbol_format`, `brokers.ssi.market_order_type`, `live.allowed_machine`.
- Chưa có kiểm thử đầu-cuối với broker thật (không có khoá). Paper trade ≥ vài tuần, so với backtest cùng kỳ trước khi cân nhắc live (golive checklist §6).

**Rủi ro còn lại**
- Token SSI/DNSE hết hạn giữa phiên ⇒ adapter tự xin OTP và nhắn Telegram; trong lúc chờ, broker đó bị coi là mất kết nối (DNSE: có vị thế quá `broker_timeout_seconds` ⇒ đối ứng khẩn cấp trên SSI).
- Paper chưa dùng feed tổng hợp (cần OTP SSI kể cả khi paper — giữ paper đơn giản).
- Khởi động lại giữa phiên ⇒ nhập lại hai OTP; stop do bot giữ bị mất cho tới khi risk đòi stop mới / đóng vị thế.

**Người dùng cần làm**: như các mục P4.1/P4.2 (chạy hai script thăm dò, gửi kết quả), điền settings còn null; chỉ đổi `api_verified` khi mình đã chốt các ❓ cùng bạn.

## 2026-11-08 — P4.2 Adapter SSI (`brokers/ssi.py`) — broker dự phòng, live KHOÁ tới khi kiểm API

**Đã xong**
- Đọc mã nguồn SDK `ssi-sdk` 3.3.2 (trading, token_manager, portfolio, rest_client, crypto, stream TRADING) ⇒ `docs/broker_notes.md` §2.6: endpoint, field, mã trạng thái lệnh (PD/QU/PF/FF/CL/RJ…), bảng ❓ và cách xử lý. Phát hiện: SSI có lệnh điều kiện FCO (stop, trailing, OCO) — hỗ trợ phái sinh ❓; SDK không có môi trường UAT; SDK không có phát lại sự kiện theo `notifyId`.
- `brokers/ssi.py`:
  - `SsiHttp`: Bearer + ký RSA-SHA256 (thuần thư viện chuẩn, như SDK) trên đúng chuỗi body gửi đi; không retry; nhận ra lỗi nằm trong HTTP 200 (`{code, msg}`).
  - `SsiTokenManager` (dùng CHUNG cho broker và feed SSI): OTP nhập tay `/otp ssi <mã>`; refresh trước hạn 30s, một luồng (khoá); refresh token bị bỏ khỏi bộ nhớ TRƯỚC khi gửi ⇒ lỗi mạng giữa chừng không bao giờ dẫn tới gửi lại (SSI thu hồi cả phiên); 401101–401108 ⇒ xin OTP. Token chỉ ở bộ nhớ, không ghi đĩa.
  - `SsiBroker`: `clientRequestId` = client_order_id; lệnh "không rõ" (timeout/5xx/409) tìm CHÍNH XÁC theo clientRequestId trong sổ lệnh, không gửi lại; bị coi là chưa tới sàn mà sau đó vẫn hiện ⇒ theo dõi như lệnh ngoài (net position đúng). Sửa = một request cho giá, một cho khối lượng. Huỷ chỉ báo CANCELLED khi sổ lệnh báo CL. Vị thế từ `derOpenPositions` (net ≠ long − short ⇒ lỗi, không đoán). Đọc sổ lệnh theo trang tới khi hết. Stop do bot giữ như DNSE. Lệnh đặt tay được theo dõi.
  - Chốt trước MỌI lệnh thay đổi: TRADING_MODE=live + `--live` + `api_verified` + khoá ký + token; dry-run chỉ log. Hai ❓ `order_symbol_format` (KRX hay VN30FYYMM) và `market_order_type` (MTL hay MP) còn null ⇒ live từ chối chạy.
  - `SsiOrderStream`: kênh TRADING `order.<tk>` + `portfolio.<tk>`; `orderEvent` (cộng dồn) áp ngay, trùng `notifyId` bị bỏ, sự kiện cũ đến muộn không kéo lệnh đã kết thúc về "còn sống"; `orderMatchEvent` ⇒ đọc lại sổ lệnh ngay; mỗi lần (re)connect ⇒ đọc lại sổ lệnh REST (bù sự kiện lỡ — thay cho "lưu notify_id để phát lại" vì SDK không có cơ chế đó).
- Runner: `/otp [dnse|ssi] <mã>` — không ghi tên ⇒ gửi cho broker duy nhất đang chờ OTP; hai broker cùng chờ ⇒ hỏi lại. DNSE thêm `awaiting_otp`.
- `scripts/ssi_trading_probe.py`: `--payload-only` (không mạng) hoặc OTP → CHỈ ĐỌC masterdata/sổ lệnh/vị thế; ghi response thô đã xoá apiSecret/otp/token + `summary.json` (mã hợp đồng SSI trả về, chuỗi trạng thái, có bọc `data` không). Không đặt lệnh (không có UAT).
- `config/settings.yaml` thêm `brokers.ssi.*`; `.env.example` ghi chú `SSI_PRIVATE_KEY_PATH`, `/otp ssi`.
- Test viết trước: `tests/test_broker_ssi.py` (khoá RSA sinh trong test, server giả kiểm chữ ký bằng khoá công khai, mô phỏng refresh-một-lần và thu hồi phiên), `tests/test_ssi_probe.py`, runner/parser. Thử phá 32 cơ chế an toàn ⇒ ban đầu 9 sống sót ⇒ thêm test (refresh song song khi cùng bị 401, OTP sai định dạng, cửa sổ đối soát, huỷ chưa xác nhận, dry-run huỷ, 401 ⇒ lệnh sau dùng token mới, lọc tài khoản trên stream, frame trùng, sự kiện cũ đến muộn) ⇒ 32/32 bị bắt.
- 781 test.

**Rủi ro / giả định còn lại**
- Chưa gọi thử được API SSI (không có khoá, không có UAT): mã hợp đồng khi đặt lệnh, MTL/MP, response có bọc `data`, ngữ nghĩa `quantity` khi sửa, `bidAvgPrice/askAvgPrice` ứng với Long/Short — đều ❓ (broker_notes §2.6).
- Khởi động lại runner ⇒ phải OTP lại SSI (token không ghi đĩa — chủ ý, để không bao giờ gửi lại refresh token cũ).
- Một phiên mỗi apiKey: chạy thêm công cụ khác dùng cùng apiKey (vd script probe trong lúc bot chạy) sẽ làm token của bot mất hiệu lực.
- Runner chưa nối: SSI làm broker dự phòng cho `EmergencyHandler`, `SsiFeed` + `SsiOrderStream` dùng chung token, composite feed (#13).

**Người dùng cần làm**
- [ ] Đăng ký FastConnect V3, tải private key, điền `.env` (SSI_CLIENT_ID/API_KEY/API_SECRET/PRIVATE_KEY_PATH/ACCOUNT).
- [ ] Chạy `python scripts/ssi_trading_probe.py`, gửi lại `data/probe/ssi_trading_*` ⇒ chốt `order_symbol_format`, `market_order_type`. Hỏi SSI lệnh thị trường cho phái sinh qua API là MTL hay MP, và FCO stop có dùng cho phái sinh không.

**Bước tiếp theo:** nối live vào runner (DNSE chính + SSI dự phòng + composite feed, #13) sau khi có kết quả probe của cả hai bên.

## 2026-11-08 — P4.1 Adapter DNSE (`brokers/dnse.py`) — live KHOÁ tới khi kiểm API

**Quyết định của người dùng:** OTP email nhập tay qua Telegram `/otp <mã>` (không dùng Gmail API — tránh thư viện Google lớn + cho bot quyền đọc hộp thư); lớp HTTP tự viết mỏng trên `requests` (không dùng SDK `dnse`); viết đủ adapter nhưng chặn lệnh thật ở production tới khi các mục ❓ được kiểm. PROMPTS ghi "đăng nhập JWT" là bản trước KRX ⇒ làm theo V2: ký HMAC mọi request + trading token.

**Đã xong**
- Đọc mã nguồn SDK `dnse` 0.5.0 (orders, registration, accounts, deals, stream lệnh) ⇒ `docs/broker_notes.md` §1.5 (endpoint, field, bảng ❓ và cách adapter xử lý từng mục); sửa §1.1 (header `date`, ký path không kèm query theo mã SDK).
- `brokers/dnse.py`:
  - `DnseHttp`: ký HMAC như SDK, MỘT request = MỘT lần gửi (không retry 429/5xx/timeout), `trading-token` chỉ gửi kèm lệnh thay đổi, sửa lỗi `metadata` chưa escape của server, `repr` không lộ khoá.
  - `DnseBroker`: đặt LO/MTL/ATO/ATC, sửa (huỷ-đặt ⇒ orderId mới, cùng client id, gửi phần CÒN LẠI), huỷ (chỉ báo CANCELLED khi sàn xác nhận), vị thế (gộp deal, thiếu trang / chiều lạ / deal mâu thuẫn ⇒ lỗi, không đoán), lệnh chờ.
  - Không bao giờ gửi lại lệnh: timeout/lỗi mạng/408/5xx ⇒ "không rõ", đối soát sổ lệnh (chỉ lệnh MỚI xuất hiện sau lúc gửi, khớp mã/chiều/loại/KL/giá); quá `uncertain_resolve_seconds` ⇒ REJECTED. Gọi lại cùng client id chỉ trả lệnh cũ.
  - Chốt trước MỌI lệnh thay đổi: `TRADING_MODE=live` + `--live` + `api_verified` (production) + trading token còn hạn. `dry_run` chỉ log payload.
  - Khớp lệnh: đọc sổ lệnh mỗi `poll_seconds` trong `ping()`, phát Fill (trước) rồi cập nhật lệnh; trạng thái suy từ khối lượng trước, chuỗi `orderStatus` sau (chưa biết giá trị thật). Lệnh đặt tay trên app được theo dõi (`EXT<id>`) để net position đúng; lệnh có sẵn lúc khởi động không phát lại phần đã khớp.
  - Stop do bot giữ (API không có lệnh stop): chạm giá ⇒ gửi MTL đúng một lần, cùng client id.
  - Token: xin OTP lúc `connect()` / khi 401 `INVALID_TRADING_TOKEN` / trước khi hết hạn; không token ⇒ `ping()` = False (risk chặn mở vị thế; có vị thế quá `broker_timeout_seconds` ⇒ quy trình khẩn cấp).
- Runner: lệnh Telegram `/otp <mã>` (không bao giờ nhắc lại mã), chuyển tick cho MỌI broker có `on_tick` (kể cả async). `telegram.parse_commands` giữ tham số chỉ cho `/otp`. Live vẫn bị từ chối (chưa nối, chờ kiểm API + P4.2).
- `scripts/dnse_trading_probe.py`: `--payload-only` (không mạng), mặc định chỉ đọc (tài khoản, gói vay, sổ lệnh, lịch sử, vị thế, secdef), `--uat --order-test [--with-modify] --live` đặt-sửa-huỷ thử một lệnh LO giá sàn trên UAT qua chính adapter; ghi response thô đã xoá passcode/token + `summary.json` (chuỗi `orderStatus` thấy được).
- `config/settings.yaml` thêm `brokers.dnse.*` (`loan_package_id`, `trading_token_ttl_seconds` = null; `api_verified: false`); `.env.example` thêm `DNSE_ACCOUNT_NO`, bỏ `DNSE_OTP_EMAIL`.
- Test viết trước: `tests/test_broker_dnse.py` (89 test), `tests/test_trading_probe.py` (6), runner/parser (4). Thử phá 23 cơ chế an toàn của adapter ⇒ ban đầu 4 cơ chế sống sót ⇒ thêm test (lệnh có sẵn đã khớp một phần lúc khởi động, lệnh giống hệt đã khớp xong không được nhận nhầm, deal mâu thuẫn, trạng thái sau sửa lệnh) ⇒ 23/23 bị bắt.
- 680 test.

**Rủi ro / giả định còn lại**
- Chưa gọi thử được API (không có khoá, proxy chặn) ⇒ mọi mục ❓ ở broker_notes §1.5 chưa kiểm: giá trị `orderStatus`, ngữ nghĩa `quantity` khi sửa, `side`/`status` của deal, thời hạn token, rate limit, server có chấp nhận chữ ký path-không-query.
- Stop do bot giữ: MTL phần dư thành LO (quy định sàn) ⇒ stop đã kích hoạt có thể đứng chờ; `ExecutionManager` chưa đặt lại lệnh tag `stop` như với `exit/flatten`. Bot khởi động lại giữa phiên ⇒ stop cục bộ mất, risk sẽ đòi stop mới hoặc đóng.
- Thời điểm Fill = lúc đọc sổ lệnh (chậm tối đa `poll_seconds`), không phải giờ khớp ở sàn.
- Lệnh "không rõ" khớp nhầm một lệnh đặt tay giống hệt đặt đúng trong cửa sổ đối soát — rất hiếm; khi đó vẫn đúng net position.
- OTP đi qua Telegram (mã sống ~2 phút, một mình mã không đủ để giao dịch); nên xoá tin sau khi dùng.
- Runner chưa nối chế độ live; danh sách hợp đồng REST `/instruments` vẫn chưa có.

**Người dùng cần làm**
- [ ] Điền `DNSE_ACCOUNT_NO` trong `.env`; chạy `python scripts/dnse_trading_probe.py` (chỉ đọc) ⇒ điền `brokers.dnse.loan_package_id` theo gói vay in ra.
- [ ] Trên UAT: `TRADING_MODE=live` rồi `python scripts/dnse_trading_probe.py --uat --order-test --with-modify --live`; gửi lại thư mục `data/probe/dnse_trading_*` để chốt các ❓ và điền `trading_token_ttl_seconds`. Chỉ sau đó mới cân nhắc `api_verified: true`.

**Bước tiếp theo:** P4.2 adapter SSI (broker dự phòng), hoặc chốt ❓ DNSE khi có kết quả probe rồi nối live vào runner.

## 2026-11-08 — P1.6 bước 1: thăm dò REST lịch sử (downloader CHƯA viết)

**Vì sao chưa viết `scripts/download_history.py`:** P1.6 yêu cầu gọi thử bằng tay để chốt các mục ❓ TRƯỚC khi viết code. Môi trường này không có khoá DNSE và không gọi được tới `openapi.dnse.com.vn` (proxy chặn). Đọc SDK `dnse` 0.5.0: `/price/ohlc` không có trong SDK (field hoàn toàn chưa biết); `/price/{symbol}/trades` có field nhưng đơn vị `from`/`to` và định dạng `time` chưa rõ; SDK ký đường dẫn KHÔNG kèm query, trái với ghi chú cũ. Viết downloader lúc này là đoán.

**Đã xong**
- `scripts/dnse_history_probe.py`: ký HMAC đúng như SDK; với hôm qua và 1/3/6/12 tháng trước (mỗi ngày đúng hợp đồng đang giao dịch ngày đó — trả lời "lịch sử hợp đồng đã đáo hạn lùi được bao xa"), thử `from`/`to` dạng epoch giây / mili-giây / ISO / ngày cho cả trades và OHLC; ký không-query trước, 401 thì thử kèm query; tôn trọng `Retry-After` khi 429; ghi response thô (không header/chữ ký) + `summary.json` vào `data/probe/dnse_history_<thời điểm>/`; tóm tắt nhận được list / `{"data": [...]}` / dạng cột.
- `docs/broker_notes.md` §1.3: ghi kết quả đọc SDK cho REST lịch sử + mâu thuẫn về cách ký.
- `tests/test_history_probe.py` (viết trước): 12 test — chữ ký khớp thuật toán SDK, các định dạng thời gian, tóm tắt các kiểu bọc, 401 ⇒ ký kèm query, 429 ⇒ chờ `Retry-After`, đúng hợp đồng theo ngày, không lộ khoá/chữ ký trong file hay màn hình. Thử phá 3 cơ chế chính ⇒ đều có test đỏ.
- 581 test.

**Người dùng cần làm**
- [ ] Có khoá DNSE: chạy `python scripts/dnse_history_probe.py`, gửi lại thư mục `data/probe/dnse_history_*` (không chứa khoá). Có kết quả là viết được downloader (P1.6 bước 2) ngay.

## 2026-11-08 — Luật rủi ro mới: đóng vị thế khi mất dữ liệu (golive #9, theo quyết định của người dùng)

- `risk.flatten_on_feed_silence_seconds: 30` (bắt buộc, null ⇒ từ chối chạy): đang có vị thế mà không có dữ liệu thị trường > 30s trong phiên liên tục ⇒ `FLATTEN_ALL`. Mốc tính = muộn nhất của tick cuối, đầu đoạn phiên (09:00/13:00), lúc runner khởi động ⇒ nghỉ trưa / vào phiên chiều không đóng oan. Chỉ áp cho IM LẶNG; lệch giá DNSE/SSI vẫn chỉ chặn mở vị thế.
- `AccountState.market_data_at`: runner ghi mỗi tick (và lúc khởi động), engine backtest ghi mỗi tick; engine chạy thêm một vòng kiểm khi dữ liệu thủng dài hơn ngưỡng (live kiểm mỗi giây — trước đó backtest không bao giờ thấy luật này kích hoạt; test phát hiện ra).
- CLAUDE.md §5 thêm dòng mô tả luật.
- Test viết trước: 5 test risk (ranh giới 30s, không vị thế, nghỉ trưa + đầu phiên chiều, chưa từng có dữ liệu), 1 test runner, 1 test backtest; thử phá từng điều kiện ⇒ đều có test đỏ.
- Hạn chế: ở chế độ paper, lệnh đóng chỉ khớp khi có tick trở lại (paper khớp theo tick); broker thật khớp MTL độc lập với feed của bot.
- 569 test.

## 2026-11-08 — Sửa lỗi từ checklist go-live (#1–#8, #10–#12, #14)

- Mỗi lỗi một test tái hiện trước (`tests/test_golive_fixes.py`, 15 test, tất cả đỏ trước khi sửa); thử phá từng bản sửa ⇒ đều có test đỏ (siết thêm test phí trong chuỗi thua).
- Luật risk thay đổi: lệnh GIẢM vị thế không còn bị trần "kịch bản xấu nhất" (đã khiến lệnh đối ứng khẩn cấp và lệnh flatten bị chặn); thay bằng `DUPLICATE_CLOSE`. Lệnh mở giữ nguyên trần cũ. Chuỗi thua tính sau phí.
- `ExecutionManager`: `maintain()` (đặt lại lệnh đóng kẹt, giữ stop), phát hiện đảo chiều ngoài ý muốn ⇒ đóng, vị thế không kế hoạch ⇒ đóng, không gửi lệnh tới broker mất kết nối, lỗi mạng không để lại lệnh ma, không để hai lệnh bảo vệ cùng sống.
- Runner: drain sau từng tick, gọi `maintain()`, lưu bộ đếm ngay khi đổi, chịu được lỗi vòng lặp (5 lần ⇒ kill, thoát 1). Engine backtest cũng gọi `maintain()` để hành xử như live.
- Telegram: xoá token khỏi mọi lỗi. Emergency: reset bộ đếm thử lại. Backtest: bar thiếu cột độ tin cậy ⇒ không tin được.
- Còn mở: #9 (chính sách khi feed chết mà đang có vị thế — chờ bạn quyết), #13 (chờ P4.2).
- 560 test.

## 2026-11-08 — P5.2 Checklist go-live

- `docs/golive_checklist.md`: đối chiếu từng mục CLAUDE.md + review độc lập "như người muốn bot thua tiền". Kết luận: **không chạy live, không chạy cả paper** trước khi sửa 3 lỗi CRITICAL + 4 HIGH (đã tái hiện bằng script): đối ứng khẩn cấp bị risk chặn khi có TP; lệnh đóng khớp một phần để lại vị thế không stop; lỗi mạng làm chết runner; stop + TP cùng khớp gây đảo chiều; flatten + đối ứng đóng hai lần; lộ token Telegram trong log; flatten bị chặn khi xác nhận huỷ trễ.

## 2026-11-08 — P5.1 Runner + giám sát

**Đã xong**
- `live/runner.py` — `Runner` (vòng `step()` mỗi giây, `run()` = khởi động + lặp) và `main()`:
  - khởi động: ngày nghỉ ⇒ thoát; `risk.new_day`; nạp bộ đếm trong ngày đã lưu; ĐỐI SOÁT vị thế + lệnh chờ từ broker (broker là sự thật), có vị thế sẵn ⇒ cảnh báo;
  - mỗi vòng: heartbeat khoá phiên TRƯỚC mọi việc (mất khoá ⇒ dừng ngay, mọi vòng sau cũng không gửi lệnh, thoát 3); ping broker ⇒ `broker_last_seen` TRƯỚC khi ra quyết định (sửa lỗi thứ tự: vòng lặp khựng > 10s từng làm lệnh bị từ chối oan); tick ⇒ giá đánh dấu, paper khớp, bar; sức khoẻ feed ⇒ `feed_healthy`; bar đóng theo đồng hồ ⇒ strategy; lệnh Telegram; `risk.check` ⇒ hành động; gỡ hedge khi broker chính sống lại;
  - kết thúc: kill + phẳng ⇒ 0 (quá hạn chưa phẳng ⇒ 1); sau 14:45 ⇒ 0 nếu phẳng, 1 nếu còn vị thế; báo cáo cuối ngày (lệnh, P&L sau phí, lệnh bị risk từ chối + lý do, cảnh báo) gửi Telegram + `data/reports/daily_YYYY-MM-DD.txt`;
  - `main()`: live/`--live` ⇒ kiểm hai chốt rồi từ chối vì chưa có adapter thật; paper ⇒ `check_allowed_machine` + `InstanceLock` + feed DNSE + `PaperBroker` mang tên broker chính; Telegram qua thread.
- `core/bars.py` — `BarBuilder`: đóng bar theo đồng hồ (kể cả không có tick), grace 0,5s cho tick trễ, tick trễ cho bar đã phát bị bỏ và đếm.
- `live/state_store.py`: lưu/nạp bộ đếm trong ngày (lượt, chuỗi thua, lãi/lỗ, phí, khoá ngày, kill, khẩn cấp, id lệnh); sang ngày chỉ giữ khoá tay; file hỏng ⇒ từ chối chạy.
- `monitoring/telegram.py`: `parse_commands` (chỉ nhận từ đúng chat, bỏ tin sửa) + `fetch_updates`.
- `brokers/base.py`: `ping()` (mặc định True; adapter mạng phải ghi đè).
- `brokers/paper.py`: **sửa độ lạc quan** — LO đặt khi giá chưa tới giờ nằm chờ NGAY từ lúc đặt (trước đây nếu tick kế tiếp nhảy qua giá LO thì khớp ở giá tốt hơn LO ⇒ chốt lời trong backtest đẹp hơn thật). Có test tái hiện trước khi sửa.
- `docs/runner.md`: cách chạy paper, lệnh điều khiển, mã thoát.
- Test viết trước: `tests/test_runner_parts.py` (12), `tests/test_runner.py` (18), 2 test paper mới. Thử phá từng cơ chế ⇒ đều có test đỏ (siết 2 test: vòng sau khi mất khoá vẫn không giao dịch; đối soát với state trống thật).
- 546 test.

**Giả định / rủi ro còn mở**
- Chưa chạy thật: cần khoá DNSE (feed) và điền settings; live cần adapter P4.1/P4.2.
- Feed composite chưa nối vào runner (chờ token SSI — P4.2); hiện runner paper chỉ dùng feed DNSE, nên một nguồn im là `feed_healthy=False` theo `is_stale` của feed DNSE.
- Lệnh Telegram đọc mỗi 3 giây bằng getUpdates (không webhook); mất mạng ⇒ bỏ lượt, không ảnh hưởng giao dịch. Chưa có lệnh `/unlock` (mở kill/khẩn cấp) — cố ý để người mở bằng tay.
- Runner chạy trong một ngày; qua nửa đêm không được hỗ trợ (Task Scheduler khởi động lại mỗi sáng).
- Ghi file trạng thái/báo cáo đồng bộ trong vòng lặp (nhỏ, mỗi 30 giây).

**Người dùng cần làm**
- [ ] Điền `live.allowed_machine` (hostname máy desktop) và các khoá rủi ro/phí; tạo bot Telegram riêng cho kênh trading.

## 2026-11-08 — P4.3 Quy trình khẩn cấp

**Đã xong**
- `live/emergency.py` — `EmergencyHandler`, nhận `EMERGENCY_HEDGE` từ risk (qua `ExecutionManager`):
  - khoá mở vị thế mới bằng `state.emergency_reason` (risk từ chối với `RejectCode.EMERGENCY`; KHÔNG tự mở khoá, kể cả sang ngày mới — người gọi `risk.clear_emergency` sau khi đối soát);
  - đặt MTL đối ứng trên broker dự phòng để net = 0; idempotent (`cần = −net − phần đối ứng đang chờ`) vì risk phát tín hiệu mỗi lần `check()`;
  - lệnh đối ứng chờ quá `hedge_requote_s` (5s) ⇒ huỷ và đặt lại MTL — xử lý trường hợp MTL khớp một phần, phần dư thành LO và giá chạy mất (test tái hiện: vị thế lộ một phần cho tới khi đặt lại);
  - broker dự phòng lỗi / từ chối ⇒ thử lại tối đa 3 lần với id mới rồi cảnh báo XỬ LÝ TAY; cả hai broker mất kết nối ⇒ cảnh báo "CẢ HAI BROKER LỖI" (giới hạn 1 lần/60s);
  - gỡ hedge (`unwind`) khi broker chính sống lại hoặc tới giờ flatten: huỷ lệnh chờ, đóng ĐỒNG THỜI hai chân; gọi lặp khi đang gỡ dở không huỷ/gửi trùng; broker chính vẫn chết lúc flatten ⇒ GIỮ hedge (đóng chân dự phòng sẽ lộ vị thế trần) và cảnh báo;
  - `notify` nhận hàm thường hoặc async (để nối Telegram).
- `risk/risk_manager.py`: `approve_unwind` (duyệt cả bộ lệnh gỡ khi mỗi lệnh giảm vị thế trên chính broker của nó và tổng giữ nguyên net; hỏng một ⇒ từ chối tất cả), `RejectCode.EMERGENCY` / `UNWIND_INVALID`, `clear_emergency`, `broker_alive` công khai; `AccountState.emergency_reason`.
- `core/execution.py`: chuyển `EMERGENCY_HEDGE` cho handler; đóng vị thế khi net = 0 nhưng còn vị thế chéo broker ⇒ nhờ handler gỡ; lệnh `unwind` đang chờ được coi như lệnh đóng (không bị huỷ bởi FLATTEN lặp).
- `tests/test_emergency.py` (viết trước): 21 test — mất kết nối giữa chừng, khớp một phần (cả khi giá chạy mất), broker dự phòng ném lỗi / từ chối, cả hai broker lỗi, không có vị thế, gỡ hedge, FLATTEN lặp khi đang gỡ, flatten khi broker chính còn chết, luật `approve_unwind`. Thử phá từng cơ chế ⇒ đều có test đỏ (bổ sung 2 test thiếu về gỡ hedge lặp).
- 514 test.

**Giả định / rủi ro còn mở**
- Phát hiện "mất kết nối" dựa vào `state.broker_last_seen` — runner (P5.1) phải cập nhật bằng ping/thao tác thành công với từng broker; adapter thật (P4.1/P4.2) phải có `ping`.
- Khi broker chính mất kết nối, lệnh stop của nó vẫn có thể đang nằm trên sàn và khớp mà bot không biết ⇒ sau khi đối ứng, net thật có thể lệch; khi kết nối lại PHẢI đối soát vị thế từ broker (runner P5.1) trước khi gỡ hedge.
- Đối ứng dùng MTL; trong ATO/ATC không đặt được MTL ⇒ lỗi được ghi và cảnh báo, không có đường khác.
- Giữ hedge qua đêm nếu broker chính chết tới hết phiên — vi phạm "không giữ vị thế qua đêm" theo nghĩa gộp (net = 0) nhưng an toàn hơn đóng một chân; cần người xử lý.
- Giới hạn tần suất cảnh báo theo khoá; cảnh báo thường (đã đặt đối ứng…) gửi mỗi lần xảy ra.

## 2026-11-08 — P1.5 Kiểm định dữ liệu một ngày

**Đã xong**
- `recorder/validate.py` (hàm thuần) + `scripts/validate_day.py <ngày> [--ref-price] [--reference]`:
  - mỗi nguồn: số tick (khớp/báo giá), khoảng trống > 5 giây trong phiên liên tục (tính cả đầu/cuối đoạn 09:00–11:30, 13:00–14:30), báo giá bid > ask, tick ngoài biên độ ±7% (chỉ khi có `--ref-price` — không tự đoán giá tham chiếu), giá nhảy > 1% giữa hai lần khớp;
  - **đối chiếu KL luỹ kế với tổng KL từng lần khớp** ⇒ phát hiện feed rơi message (giao thức không có số thứ tự nên đây là cách duy nhất);
  - DNSE vs SSI: mỗi giây so giá (mid nếu có, không thì giá khớp, chỉ khi cả hai còn mới) ⇒ số giây lệch > `feed_divergence_ticks`;
  - dựng bar 1 phút từ giá khớp ⇒ `data/bars/<ngày>.parquet` (schema đọc được bởi `backtest.engine --bars data/bars`);
  - so OHLC với bar tham chiếu (mặc định `data/imported/<ngày>.parquet` = vnstock-dashboard), dung sai 2 bước, liệt kê sai lệch và phút thiếu ở mỗi bên;
  - báo cáo chữ + `data/validation/<ngày>.json`; mã thoát 0/1/2.
- `scripts/record_today.py` tự chạy kiểm định sau khi gộp file cuối ngày (lỗi kiểm định không ảnh hưởng dữ liệu đã lưu).
- `tests/test_validate_day.py` (viết trước): 25 test. Thử phá từng luật ⇒ đều có test đỏ (bổ sung 2 test thiếu: nguồn chính cũ không so, lệch đúng ngưỡng không tính).
- 493 test.

**Giả định / rủi ro còn mở**
- Không dùng thư viện vnstock (lớn) — "dữ liệu vnstock" lấy từ bar đã import từ vnstock-dashboard; muốn so trực tiếp với vnstock thì cần cài thêm (hỏi trước).
- Lệch hai nguồn đo theo giây ⇒ giá chạy nhanh + độ trễ khác nhau giữa hai feed có thể cho vài giây lệch "giả"; đọc cùng số liệu độ trễ khi có dữ liệu thật.
- Kiểm KL cần `total_volume` (DNSE `te`, SSI `v`); nếu feed không gửi thì báo "không kiểm được KL".
- Giá tham chiếu phải nhập tay (`--ref-price`) tới khi adapter lấy được secdef (P4.1).

## 2026-11-08 — P1.3 Feed SSI + feed ghép (composite)

**Đã xong**
- Đọc mã nguồn SDK chính thức `ssi-sdk` 3.3.2 để chốt giao thức stream trước khi viết code; ghi vào `docs/broker_notes.md` §2.4. Điểm quan trọng: stream cần **access token Bearer lúc bắt tay** (lấy qua REST `/api/v3/auth/token`), quote có nhiều bước giá, trade có KL luỹ kế `v`.
- `feeds/data_feed_ssi.py` — `SsiFeed` (cùng interface `DataFeed`):
  - đối chiếu hợp đồng với danh sách broker trước khi kết nối; đăng ký `trade.<KRX>` + `quote.<KRX>`; `ping_pong` mỗi 30 giây;
  - chuẩn hoá frame `{channel, topic, data}` về `Tick` (`SsiNormalizer`): số dạng chuỗi `"1,234.0"`, thời gian epoch / `HH:MM:SS` / ISO, giá chữ ("ATO") và giá ngoài khoảng hợp lý bị bỏ, nến `@1m/@5m` bị bỏ, quote lấy mức tốt nhất;
  - token qua `token_provider(force_refresh)` tiêm vào; 401/403 (bắt tay hoặc frame từ chối) ⇒ lấy token mới MỘT lần, lần nữa ⇒ dừng hẳn; cờ thử-lại được xoá khi kết nối đã có dữ liệu (token hết hạn giữa phiên không làm feed chết — lỗi này được test tái hiện trước rồi mới sửa); 429 ⇒ chờ max(30s, retryAfter); ack đăng ký lỗi ⇒ dừng hẳn; backoff lũy thừa có jitter, chặn 60 giây.
- `feeds/composite.py` — `CompositeFeed(DNSE chính, SSI dự phòng)`:
  - `on_tick` chỉ phát tick của nguồn đang dùng; `on_any_tick` phát mọi nguồn (recorder ghi cả hai);
  - `healthy` = cả hai nguồn có tick mới, không nguồn nào chết, không lệch giá quá `feed_divergence_ticks` (so mid nếu có, không thì giá khớp, chỉ so giá còn mới) ⇒ runner gán vào `AccountState.feed_healthy` để risk chặn mở vị thế;
  - DNSE im > `feed_stale_seconds` mà SSI sống ⇒ chuyển sang SSI; DNSE sống lại liên tục `switch_back_after_seconds` (30s) ⇒ chuyển về; SSI đang dùng mà chết ⇒ về ngay; cả hai im ⇒ không chuyển;
  - không báo động giả lúc khởi động và ngay sau nghỉ trưa (im lặng tính từ mốc muộn nhất: tick cuối / lúc bắt đầu theo dõi / 09:00 hoặc 13:00), nhưng `healthy` vẫn False tới khi cả hai có dữ liệu;
  - cảnh báo chỉ phát khi trạng thái đổi (`STALE`, `RECOVERED`, `DIVERGENCE`, `DIVERGENCE_CLEARED`, `SWITCHED`, `SWITCHED_BACK`, `FEED_DOWN`), `on_alert` để nối Telegram; một feed chết thì composite chạy tiếp với feed kia, cả hai chết ⇒ ném lỗi.
- `settings.feeds.switch_back_after_seconds: 30`; `.env.example`: biến SSI V3 (`SSI_CLIENT_ID`, `SSI_API_KEY`, `SSI_API_SECRET`).
- Test viết trước: `tests/test_feed_ssi.py` (27, gồm 1 test WebSocket thật với server cục bộ kiểm header Bearer và mã 401) và `tests/test_feed_composite.py` (24). Thử phá từng luật ⇒ đều có test đỏ (phát hiện và bổ sung 4 test thiếu: không chuyển sang nguồn đã chết, feed chết là không khoẻ ngay, nến có trường giá vẫn bị bỏ, jitter backoff).
- 472 test.

**Giả định / rủi ro còn mở**
- `token_provider` thật cho SSI chưa có — thuộc adapter SSI (P4.2), phải tuân luật refresh token CHỈ DÙNG MỘT LẦN. Chưa rõ server có cấp token chỉ-dữ-liệu không cần OTP (nếu cần OTP mỗi ngày thì feed dự phòng phụ thuộc thao tác tay buổi sáng).
- Frame mẫu SSI dựng theo SDK, chưa phải bản ghi thật: định dạng `t`, đơn vị giá phái sinh, số bước giá còn ❓.
- Khi chuyển nguồn, tick từ hai feed có thể lệch thời gian/giá một chút; strategy dựng bar từ nguồn đang dùng — bar ở phút chuyển nguồn có thể pha trộn hai nguồn.
- So lệch giá dùng mid của mức tốt nhất; khi một bên chỉ có giá khớp, bên kia có mid, so sánh khác loại có thể báo lệch giả lúc thị trường mỏng (ngưỡng 3 bước giảm rủi ro này).
- `scripts/record_today.py` vẫn chỉ dùng feed DNSE; chuyển sang composite khi có `token_provider` SSI.

**Người dùng cần làm**
- [ ] Đăng ký SSI FastConnect V3, điền các biến SSI trong `.env`; khi đăng ký hỏi rõ: lấy token stream dữ liệu có cần OTP không.

**Bước tiếp theo:** P1.5 (kiểm định dữ liệu một ngày — đã có recorder ghi cả hai nguồn để đối chiếu), hoặc P4.1/P4.2 khi có khoá (adapter + lấy danh sách hợp đồng + token SSI).

## 2026-11-08 — P1.4 Recorder

**Đã xong**
- `recorder/tick_recorder.py` — `TickRecorder`: nhận `Tick` từ bất kỳ `DataFeed` nào (hiện là DNSE; composite P1.3 cắm vào được ngay), ghi `data/raw/YYYY-MM-DD/`:
  - mỗi `recorder.flush_seconds` (5 giây) hoặc khi đệm đủ `max_buffer_rows` ⇒ ghi MỘT file `part-<phiên>-<n>.parquet` MỚI bằng tmp-rồi-tráo; không bao giờ ghi đè/nối file cũ ⇒ crash chỉ mất ≤ 5 giây trong RAM, không có file hỏng; ghi lỗi ⇒ giữ nguyên đệm, lần sau thử lại;
  - cuối ngày `compact_day` gộp part → `ticks.parquet` (ghi bản gộp xong mới xoá part; gộp lại nhiều lần không trùng; khử trùng theo id ghi `session`+`rx_seq`, không theo nội dung — hai lần khớp y hệt vẫn giữ cả hai);
  - mỗi dòng: mọi trường `Tick` (gồm bid/ask/khối lượng mức 1 = snapshot sổ lệnh) + `recv_ts` (giờ bot nhận, đo độ trễ) + id ghi;
  - `load_ticks(ngày)` đọc bản gộp + part chưa gộp, theo thời gian ⇒ dùng thẳng cho `backtest.engine.bars_from_ticks` / `run_events`;
  - từ chối thư mục dữ liệu nằm trong Dropbox/OneDrive/Google Drive/iCloud (CLAUDE.md mục 3b).
- `scripts/record_today.py`: bỏ qua ngày nghỉ; chờ tới 08:30; ghi tới 15:00, flush + gộp rồi thoát; feed chết ⇒ vẫn lưu phần đã nhận, thoát mã 1; `InstanceLock` riêng (`state/recorder.lock`) chặn hai recorder trên một máy; cảnh báo khi feed im lặng trong phiên; log ra `logs/record_YYYY-MM-DD.log`. Mã hợp đồng: `--krx` hoặc `--assume-computed-contract` (chưa có REST danh sách hợp đồng — P4.1).
- `docs/recorder_windows.md`: hướng dẫn Task Scheduler (lệnh `schtasks`, thiết lập Conditions/Settings, ý nghĩa mã thoát, kiểm tra hằng ngày).
- `settings.recorder`: `flush_seconds: 5`, `max_buffer_rows: 5000`, `start: "08:30"`, `stop: "15:00"`.
- `tests/test_recorder.py` (viết trước): 31 test — roundtrip đủ trường, mỗi flush một file, đệm đầy, ghi lỗi giữ đệm, crash chỉ mất phần chưa flush, gộp idempotent, gộp bị ngắt không nhân đôi, ghi bản gộp lỗi thì part còn nguyên, khởi động lại cùng ngày, chặn thư mục đồng bộ, lịch chạy, phiên ghi khi feed chết. Thử phá từng cơ chế ⇒ đều có test đỏ (phát hiện thiếu test "ghi bản gộp lỗi" và đã bổ sung).
- 422 test.

**Giả định / rủi ro còn mở**
- Ghi Parquet chạy đồng bộ trong event loop (file nhỏ mỗi 5 giây, ước vài ms); nếu đĩa chậm có thể trễ xử lý tick vài chục ms — đo khi chạy thật, cần thì chuyển sang thread.
- Ghi tick theo ngày của đồng hồ bot (không theo `ts` của sàn) — đủ dùng vì phiên không qua nửa đêm.
- Chỉ có sổ lệnh mức 1 (giới hạn của feed DNSE). Chưa ghi message thô — nếu cần soi lỗi chuẩn hoá thì dùng `scripts/dnse_feed_probe.py`.
- `--assume-computed-contract` chưa đối chiếu danh sách broker (chấp nhận được vì chỉ ghi dữ liệu); sai mã ⇒ feed im lặng và log cảnh báo.
- Backtest CLI (`python -m backtest.engine`) mới đọc bar; đọc tick đã ghi thì dùng `load_ticks` + `run_events` trong Python — thêm cờ `--ticks` khi cần.

**Người dùng cần làm**
- [ ] Đặt `paths.data_dir` / `paths.state_dir` ra ngoài Dropbox (ví dụ `C:/vnbot/data`).
- [ ] Khi có khoá DNSE: chạy thử tay rồi tạo task theo `docs/recorder_windows.md` — càng sớm càng có dữ liệu tick thật cho backtest.

**Bước tiếp theo:** P1.3 (feed SSI + composite: recorder ghi được cả hai nguồn để đối chiếu) hoặc P1.5 (kiểm định dữ liệu một ngày).

## 2026-11-08 — P1.2 Feed DNSE

**Đã xong**
- Đọc mã nguồn SDK chính thức `dnse` 0.5.0 (PyPI) để chốt giao thức WebSocket trước khi viết code; ghi vào `docs/broker_notes.md` §1.4 (định dạng subscribe, lời chào, không có số thứ tự, SDK chưa có `/instruments`).
- `feeds/base.py`: interface `DataFeed` (`on_tick`, `run`, `stop`, `is_stale`, `stats`, `last_message_at`).
- `feeds/data_feed_dnse.py` — `DnseFeed`:
  - khởi động: tính hợp đồng giao dịch hôm nay (F1M, có roll) → mã KRX → **bắt buộc có trong danh sách broker trả về** (`contract_lister` tiêm vào), không có ⇒ từ chối kết nối;
  - lời chào → xác thực HMAC → đăng ký `tick.G1.json`; trả pong khi server ping, tự ping mỗi 30 giây;
  - chuẩn hoá về `Tick` (`DnseNormalizer`): trade/te/quote, nhận cả camelCase lẫn snake_case, tự nhận đơn vị timestamp (s/ms/µs/ns) → giờ VN, tick khớp lệnh mang giá tốt nhất gần nhất; giá ngoài [300, 5000] điểm bị bỏ và đếm (không tự chia 1000);
  - kết nối lại vô hạn, backoff 1s → 60s, có dữ liệu thì backoff về 1s; kết nối im lặng quá `recv_timeout_s` ⇒ kết nối lại; sai khoá (`auth_error`) ⇒ dừng hẳn, không thử lại;
  - `is_stale()` theo `feeds.feed_stale_seconds`, chỉ tính trong phiên liên tục; log số message/phút; lỗi ở bộ nhận tick không làm chết feed; không log/repr khoá hay chữ ký.
- `scripts/dnse_feed_probe.py`: chạy với khoá thật, in tick, ghi message thô ra `data/probe/` để chốt các mục ❓ và thay fixture mẫu.
- `.env.example`: DNSE V2 dùng `DNSE_API_KEY` / `DNSE_API_SECRET` (bỏ username/password kiểu cũ).
- Thêm thư viện `websockets>=12.0` (nhỏ, thuần Python, chính SDK DNSE dùng) — nhập muộn, chỉ cần khi kết nối thật.
- `tests/test_feed_dnse.py` (viết trước): 33 test với message mẫu `tests/fixtures/dnse_stream.json` + transport giả; 1 test chạy qua WebSocket thật với server giả lập cục bộ. Thử phá từng cơ chế (reset backoff, đối chiếu hợp đồng, chặn giá lạ, ngưỡng im lặng, auth lỗi là cố định, pong, cô lập lỗi callback, timeout nhận, gắn báo giá vào trade) ⇒ đều có test đỏ.
- 391 test.

**Giả định / rủi ro còn mở**
- Message mẫu dựng theo model SDK, **chưa phải bản ghi server thật** — tên field, đơn vị timestamp/giá, định dạng phản hồi xác thực đều ❓ tới khi chạy `scripts/dnse_feed_probe.py`.
- Chỉ có giá tốt nhất (bid/ask 1 mức); kênh sổ lệnh nhiều bước giá không có trong SDK.
- Không có số thứ tự message ⇒ mất message lẻ trong một kết nối KHÔNG phát hiện được; chỉ phát hiện im lặng/rớt kết nối. Recorder/validate_day (P1.4/P1.5) nên đối chiếu `total_volume` của `te` để phát hiện hụt khớp lệnh.
- Hợp đồng được chọn lúc `run()`; không tự đổi hợp đồng giữa phiên (roll xảy ra theo ngày; chạy lại mỗi ngày).
- `contract_lister` thật (REST `/instruments` ký HMAC) chưa viết — thuộc adapter DNSE (P4.1); script thăm dò dùng mã KRX bạn tự xác nhận.
- Tick khớp lệnh mang giá tốt nhất GẦN NHẤT, có thể đã cũ vài trăm ms so với lúc khớp.

**Người dùng cần làm**
- [ ] Đăng ký DNSE Open API V2, điền `DNSE_API_KEY` / `DNSE_API_SECRET` vào `.env`.
- [ ] Trong giờ giao dịch chạy `python scripts/dnse_feed_probe.py --krx <mã KRX F1M> --seconds 120`, gửi lại file `data/probe/dnse_raw_*.jsonl` (không chứa khoá) để chốt các mục ❓.
- [ ] `pip install -r requirements.txt` (thêm `websockets`).

**Bước tiếp theo:** P1.3 (feed SSI + composite) hoặc P1.4 (recorder) — cả hai dùng được ngay interface `DataFeed`.

## 2026-11-08 — P3.3 Kiểm định độ bền (công cụ xong; CHƯA kết luận được)

**Kết luận về edge của ORB: CHƯA CÓ BẰNG CHỨNG — coi như KHÔNG có edge cho tới khi chứng minh được.** Lý do: không có dữ liệu bar thật trong môi trường làm việc, `fees.*`/`risk.*` còn null ⇒ engine từ chối chạy. Không có con số nào để làm bằng chứng.

**Đã xong**
- `backtest/robustness.py`: một lần chạy gồm cấu hình gốc ⇒ IS/OOS theo `--split`; walk-forward theo `folds` đoạn ngày; độ nhạy ±20% từng tham số (số nguyên lệch ≥ 1 bước) **chỉ trên in-sample**; cấu hình gốc với trượt giá 2 tick; `verdict()` theo luật cố định `VerdictRules` ⇒ CÓ / KHÔNG / CHƯA ĐỦ BẰNG CHỨNG; HTML; CLI `python -m backtest.robustness --bars … --split …` (`--n-trials` mặc định đếm từ `research/log.md`).
- Luật kết luận chốt trong code + test TRƯỚC khi có dữ liệu (xem bảng trong `research/orb_baseline.md`).
- `tests/test_robustness.py` (viết trước): 21 test — edge cài sẵn trên dữ liệu giả ⇒ CÓ; random walk ⇒ không ra CÓ; dữ liệu snapshot ⇒ CHƯA ĐỦ; từng nhánh luật; độ nhạy không chạm OOS; trượt 2 tick làm giảm lãi. Thử phá từng điều kiện ⇒ đều có test đỏ.
- `backtest/report.py`: `make_section`, `render_section` thành public để dùng lại.
- 358 test.

**Giả định / rủi ro còn mở**
- Ngưỡng luật (≥ 30 lệnh OOS, ≥ 75% biến thể lãi, ≥ 50% đoạn lãi, p × n_trials < 0,10) là lựa chọn thận trọng của tôi, chưa được bạn duyệt — nếu muốn đổi, đổi **trước** khi chạy dữ liệu thật.
- Walk-forward ở đây là đánh giá cấu hình cố định theo từng đoạn (không tối ưu lại từng đoạn) — đúng với baseline không tune; nếu sau này có bước tối ưu thì cần walk-forward optimize-rồi-test thật.
- Độ nhạy chỉ ±20% một tham số mỗi lần, không xét tương tác giữa các tham số.
- Mọi hạn chế của tick giả từ bar (P3.1) vẫn áp dụng; kết luận CÓ trên bar vẫn phải qua paper trade.

**Người dùng cần làm**
- [ ] Điền `fees.*` và `risk.*` còn null.
- [ ] Có dữ liệu thật: cấp API key DNSE/SSI để làm P1.6 (dữ liệu import từ dashboard chỉ cho ra CHƯA ĐỦ BẰNG CHỨNG theo luật).
- [ ] Chốt ngày chia OOS và ghi vào `research/log.md` trước khi chạy.
- [ ] Duyệt (hoặc sửa) ngưỡng trong `VerdictRules` trước khi chạy.

**Bước tiếp theo:** P1.6 (dữ liệu lịch sử thật) là điểm nghẽn của cả P3.2/P3.3 — cần API key. Song song có thể làm P1.2–P1.4 (feed + recorder) hoặc P4.x sau khi có key.

## 2026-11-08 — P3.2 Chiến lược baseline ORB (chưa chạy in-sample)

**Đã xong**
- `strategy/baseline_orb.py` — `OpeningRangeBreakout`: biên = đỉnh/đáy các bar mở trong 15 phút đầu phiên liên tục (đủ bar, thiếu ⇒ bỏ ngày, bỏ qua bar ATO); bar 1 phút đóng ngoài biên khi đang phẳng ⇒ vào; stop cạnh đối diện; TP 1,5R (tròn bước giá); ≤ 2 tín hiệu/ngày. Tham số ở `settings.strategy.orb`. Không I/O, không đọc giờ (có test quét mã nguồn).
- `tests/test_strategy_orb.py` (viết trước): 28 test, gồm test qua toàn pipeline engine (vào ở tick mở 09:16, chốt lời đúng 1,5R). Thử phá từng quy tắc (ngưỡng đóng ngoài biên, cạnh stop, giới hạn 2 lệnh, đủ bar, bỏ bar ATO, chỉ vào khi phẳng) ⇒ đều có test đỏ.
- CLI backtest báo lỗi một dòng khi settings còn null (thay vì traceback).
- `research/orb_baseline.md`: đặc tả, cách chạy in-sample, **5 lý do kết quả có thể đẹp hơn thực tế**. `research/log.md`: ghi cấu hình #1 (n_trials = 1).
- 337 test.

**Chưa làm được: chạy backtest in-sample** — container không có dữ liệu bar (`data/` không commit), DNSE MCP chỉ có OHLC ngày, endpoint biểu đồ công khai bị chặn mạng (và không phải API chính thức), `fees.*`/`risk.*` còn null ⇒ engine từ chối chạy đúng thiết kế. Lệnh chạy sẵn trong `research/orb_baseline.md`.

**Giả định / rủi ro còn mở**
- Đặc tả lấy theo ví dụ trong PROMPTS P3.2; chi tiết tự chọn: đóng **đúng bằng** cạnh biên không tính là phá; tín hiệu bị risk từ chối vẫn tính vào giới hạn 2 lệnh; sau khi chốt lời mà giá vẫn ngoài biên thì bar kế tiếp vào lại cùng chiều (theo đúng câu chữ "đóng ngoài biên"), sau khi dừng lỗ có thể vào ngược chiều nếu bar đóng ngoài cạnh kia.
- R tính từ giá đóng bar tín hiệu, không từ giá khớp thật ⇒ TP thực tế < 1,5R khi trượt giá.
- Chưa có lọc biên quá hẹp/quá rộng — biên hẹp ⇒ R nhỏ, phí chiếm tỷ trọng lớn.

**Người dùng cần làm**
- [ ] Điền `fees.*`, `risk.max_daily_loss_points`, `risk.max_trades_per_day`, `risk.max_consecutive_losses`.
- [ ] Có dữ liệu: chạy `scripts/import_dashboard_bars.py` (chỉ thử nhanh) hoặc cấp API key để làm P1.6.
- [ ] Chốt khoảng ngày in-sample trước khi chạy, ghi vào `research/log.md`.

**Bước tiếp theo:** chạy in-sample khi có dữ liệu; sau đó P3.3 (out-of-sample, walk-forward, độ nhạy ±20%, trượt 2 tick).

## 2026-11-08 — P3.1 Engine backtest

**Đã xong**
- `strategy/base.py`: `Strategy.on_bar(bar, ctx) -> [Signal]`, `StrategyContext(position_qty, avg_price, bars_today)`. Strategy chỉ thấy bar đã đóng.
- `core/execution.py` — `ExecutionManager`, **dùng chung cho backtest và live**: Signal → lệnh MTL qua `risk.approve` → broker; sau mỗi lần khớp tự đặt/cập nhật STOP + LO chốt lời đúng |net|, net về 0 thì huỷ; EXIT / ENTER ngược chiều ⇒ huỷ lệnh chờ rồi đóng; xử lý `FLATTEN_ALL` / `FLATTEN_SYMBOL` / `KILL` (`EMERGENCY_HEDGE` tạm thời chỉ đóng trên chính broker — P4.3).
- `backtest/engine.py`:
  - pipeline giống live: nguồn sự kiện → strategy → ExecutionManager → RiskManager → PaperBroker → `risk.check`;
  - hai nguồn: `run_bars` (bar → tick giả) và `run_events` (tick thật; `bars_from_ticks` dựng bar 1 phút);
  - chống look-ahead: sự kiện lùi thời gian ⇒ `ValueError`; bar chỉ phát lúc đóng; lệnh khớp ở tick sau; `detect_lookahead()` lật ngược dữ liệu sau một mốc rồi so tín hiệu trước mốc — chiến lược "gian lận" nhìn bar kế tiếp bị bắt, chiến lược trung thực qua;
  - tick giả bi quan: bid = giá, ask = giá + `synthetic_spread_ticks`; đường đi theo `bar_high_first`, không quyết được ⇒ về phía bất lợi cho vị thế + lệnh vào đang chờ; cực trị lặp hai tick ⇒ stop khớp tại cực trị, LO chỉ khớp khi xuyên giá;
  - chỉ đưa vào hợp đồng đang giao dịch theo ngày (`trading_contract`, có roll); cảnh báo vị thế còn mở cuối ngày;
  - `load_bars_parquet` đọc schema `data/imported/`; CLI `python -m backtest.engine --bars data/imported --strategy module:Class --split YYYY-MM-DD --n-trials N --out reports/x.html`.
- `backtest/report.py`: `TradeBook` (ghép fill thành lệnh khứ hồi, lý do thoát theo tag), `compute_metrics` (số lệnh, win rate, avg win/loss, PF, max DD, P&L sau phí VNĐ/điểm, theo giờ, theo thứ), `build_report` (toàn bộ / in-sample / out-of-sample, mỗi phần có `validate_trades` + `apply_selection_penalty(n_trials)`), HTML có cảnh báo dữ liệu snapshot ("không dùng để kết luận có edge") và "tick giả", danh sách lệnh bị risk từ chối.
- `utils/atomic_io.write_text_atomic`; `settings.backtest.synthetic_spread_ticks: 1`; `.gitignore` thêm `reports/`.
- 309 test (28 mới). Thử phá từng cơ chế (thứ tự thời gian, tick lặp ở cực trị, chiều bất lợi, lật dữ liệu, lọc hợp đồng, huỷ bảo vệ, phí, drawdown) thì đều có test đỏ. 13 ngày × 240 bar chạy ~0,25 giây.
- Kiểm chứng sơ bộ: chiến lược momentum trên random walk lỗ sau phí (p = 1.0, walk-forward 0/4 đoạn dương) — đúng kỳ vọng "dữ liệu ngẫu nhiên thì không có edge".

**Giả định / rủi ro còn mở**
- Tick giả **bi quan có chủ ý**: stop khớp tại đáy/đỉnh bar − trượt (thường tệ hơn thực tế), thanh khoản vô hạn, spread 1 bước. Kết quả trên bar chỉ là cận dưới thô; kết luận phải dựa trên dữ liệu tick thật (P1.4/P1.6).
- Dữ liệu `data/imported/` là snapshot ~2 giây, volume không tin được ⇒ báo cáo tự cảnh báo; không dùng để kết luận edge.
- `OrderType.STOP` mô phỏng stop phía sàn; nếu P4 cho thấy stop phải do bot tự giữ, cần thêm độ trễ phát hiện + gửi MTL vào mô phỏng.
- Bảo vệ sửa bằng huỷ-rồi-đặt lại ⇒ có khoảng hở ngắn (risk `stop_grace_seconds` che); live có thể cần `modify` thay vì huỷ.
- Lệnh đóng bị broker từ chối (vd MTL lúc nghỉ trưa) chỉ được ghi lại, không tự thử lại; FLATTEN của risk sẽ đặt lại ở lần `check()` sau.
- `ExecutionManager` nằm ở `core/` nhưng phụ thuộc `risk/` và `brokers/` (chấp nhận để live và backtest dùng chung một chỗ).
- `detect_lookahead` chỉ bắt rò rỉ qua dữ liệu đưa vào engine; vẫn phải đọc code chiến lược.
- Chưa dùng `backtest.initial_capital_vnd` (báo cáo theo VNĐ tuyệt đối, chưa có % lợi nhuận/ký quỹ).
- Định dạng Parquet tick (`run_events`) chưa có loader — chờ recorder P1.4 chốt schema.

**Người dùng cần làm**
- [ ] Điền `fees.*` và `risk.*` còn null — engine từ chối chạy khi còn thiếu.
- [ ] Chạy `scripts/import_dashboard_bars.py` nếu chưa (archive dashboard tự xoá sau 30 ngày).

**Bước tiếp theo:** P3.2 — mô tả chiến lược baseline bằng lời (khung `[...]` trong PROMPTS.md).

## 2026-11-07 (khuya, tiếp) — P2.2 Paper broker

**Đã xong**
- `brokers/base.py`: `BrokerAdapter` (async `place` / `cancel` / `modify` / `positions` / `open_orders`, callback `on_fill` / `on_order_update`, `connect` / `close`), `BrokerError`, `assert_live_trading_allowed(live_flag)` — chỉ qua khi `TRADING_MODE=live` **và** có cờ `--live`; adapter thật gọi ngay trước mỗi request lệnh.
- `brokers/paper.py` (`PaperBroker`, `PaperConfig`) — dùng chung cho backtest và paper:
  - khớp ở tick **kế tiếp** (`ts >` lúc đặt và `≥` lúc đặt + độ trễ); mua khớp ask, bán khớp bid, cộng `slippage_ticks`;
  - khối lượng giới hạn theo `ask_qty`/`bid_qty` (chia theo thứ tự đặt); MTL khớp một phần ⇒ phần dư thành LO giá khớp ± 1 bước (quy tắc sau KRX);
  - LO khớp ngay được ⇒ không bao giờ tệ hơn giá LO; LO nằm chờ chỉ khớp khi giá **xuyên qua** (chạm chưa đủ);
  - `OrderType.STOP` mô phỏng: kích hoạt bằng bid (bán) / ask (mua), khớp ở tick sau ⇒ giá nhảy cóc khớp ở giá nhảy cóc;
  - độ trễ `latency_ms` + jitter có seed; phí = số HĐ × tổng `fees.*` một chiều (null ⇒ từ chối chạy);
  - idempotent theo `client_order_id`; sửa lệnh cấp `broker_order_id` mới và tính lại độ trễ (như DNSE huỷ-đặt);
  - có `session`: từ chối đặt khi đóng cửa, MTL ngoài phiên liên tục, huỷ/sửa trong ATO/ATC; chỉ khớp trong phiên liên tục.
- `core/models.py`: `OrderType.STOP`, `Order.reject_reason`. `config/settings.yaml`: mục `paper` (latency 300 ms, jitter 200 ms, seed 0).
- Test nối paper ↔ risk manager: lệnh vào khớp → stop được xác nhận → `check()` không còn yêu cầu đóng vị thế.
- 281 test (54 mới); thử phá từng quy tắc khớp thì đều có test đỏ.

**Giả định / rủi ro còn mở**
- DNSE/SSI không có lệnh stop trong API lệnh thường (❓ đã ghi `docs/broker_notes.md` §4) ⇒ stop có thể phải do bot tự giữ; khi đó backtest với `OrderType.STOP` có thể lạc quan hơn thực tế (độ trễ bot theo dõi giá + gửi MTL). P4 phải thống nhất cách giả lập.
- Không mô phỏng: khớp định kỳ ATO/ATC, MOK/MAK, ký quỹ, biên độ ±7%, hàng chờ theo thứ tự thời gian (dùng luật "xuyên giá" thay thế), tác động giá của chính lệnh mình.
- Tick thiếu bid/ask bị bỏ qua ⇒ dữ liệu chỉ có bar/last (vd import từ dashboard) cần engine P3.1 dựng bid/ask giả định kèm spread — phải ghi rõ giả định đó trong báo cáo.
- Phí thuế đang tính cố định theo HĐ (`tax_vnd_per_contract`); nếu thuế tính theo giá trị giao dịch thì phải đổi công thức.
- Tên broker của paper mặc định `"paper"`; khi chạy paper trading, `brokers.primary` của risk manager phải trùng tên này (runner P5.1 lo).
- Độ trễ 300 ± 200 ms là giá trị tạm, chưa đo.

**Người dùng cần làm**
- [ ] Điền `fees.*` (paper/backtest từ chối chạy khi còn null) và xem lại `paper.latency_ms`.
- [ ] Khi đăng ký API: hỏi DNSE/SSI có lệnh điều kiện (stop) cho phái sinh qua API không.

**Bước tiếp theo:** P3.1 (engine backtest) — cần dữ liệu (P1.6 hoặc dữ liệu import) để chạy thật; hoặc P1.2 nếu đã có API key.

## 2026-11-07 (khuya) — P2.1 Risk manager

**Đã xong**
- `risk/risk_manager.py` (test viết trước — `tests/test_risk_manager.py`, 105 test, mỗi luật mục 5 một lớp test có ca biên; thử phá từng ngưỡng `>=`/`>` thì test bắt được):
  - `approve(order, state) -> Approved | Rejected(code, reason)` — `RejectCode` cố định để log/thống kê, `reason` tiếng Việt.
  - `check(state) -> [RiskAction]`: `KILL`, `FLATTEN_ALL` (lỗ ngày / giờ flatten), `FLATTEN_SYMBOL` (thiếu stop quá `stop_grace_seconds`), `EMERGENCY_HEDGE` (broker im > `broker_timeout_seconds` khi có vị thế). Risk manager không tự gửi lệnh.
  - `record_fill`, `on_order_update`, `new_day`, `daily_pnl_points`, `kill` / `clear_kill`, `OrderIdFactory` (id ≤ 20 ký tự).
- Luật: `max_contracts` theo net **mọi broker** + lệnh chờ cùng chiều (kịch bản xấu nhất); lỗ ngày gồm lỗ chưa chốt và phí ⇒ khoá đến hết ngày; `max_trades_per_day`; `max_consecutive_losses`; chỉ mở vị thế trong phiên liên tục, sau `no_entry_open_minutes`, trước `last_entry_time`, không vào ngày đáo hạn; flatten từ `flatten_time`; lệnh mở bắt buộc `stop_loss` đúng phía; id trùng / lệnh mở thứ hai khi lệnh đầu còn chờ bị chặn; feed không khoẻ, broker mất kết nối, broker không phải broker chính ⇒ chặn mở; file `KILL` hoặc `/kill`.
- Lệnh **giảm** vị thế (theo net, không theo tag) luôn được duyệt kể cả khi khoá/kill/ngoài giờ — chỉ kiểm id trùng và trần hợp đồng.
- `core/models.py`: `Order.stop_loss`; `AccountState` thêm `trading_day`, `working_orders`, `approved_order_ids`, `last_prices`, `broker_last_seen`, `feed_healthy`, `fees_points_today`, `kill_reason`, `round_trip_points`, `unprotected_since`.
- `config/settings.yaml`: thêm `risk.stop_grace_seconds: 5`. `RiskLimits.from_settings` từ chối chạy khi còn null và kiểm `last_entry_time < flatten_time < atc_start`.
- 227 test.

**Giả định / rủi ro còn mở**
- "Có stop" = lệnh `tag="stop"` ngược chiều mà broker **đã xác nhận** (PENDING/PARTIALLY_FILLED). Chưa biết DNSE/SSI có lệnh điều kiện (stop) cho phái sinh qua API không (❓ P4) — nếu stop do bot tự giữ thì P4 phải quyết cách biểu diễn để luật này vẫn đúng.
- Chuỗi thua tính theo điểm **trước phí**; lượt hoà không đổi bộ đếm; đặt lại mỗi ngày (`new_day`). Lượt bị đóng bằng lệnh đối ứng ở broker khác chưa có P&L thực hiện ⇒ tính như hoà.
- Thiếu giá đánh dấu cho vị thế đang mở: chặn mở lệnh mới, nhưng `check()` bỏ qua luật lỗ ngày (không tính được) — runner phải cảnh báo khi feed mất giá.
- `AccountState` nằm trong bộ nhớ; P5.1 phải lưu/đối soát khi khởi động lại (mất `approved_order_ids` ⇒ hai lần chạy trong cùng một giây có thể sinh id trùng; InstanceLock giảm rủi ro này).
- Ngày đáo hạn chặn mở vị thế với **mọi** hợp đồng (kể cả F2M sau roll) — thận trọng có chủ ý.
- Chưa kiểm: ký quỹ, biên độ ±7%, bội số bước giá của giá LO (broker sẽ từ chối; có thể thêm khi làm adapter).
- `no_entry_open_minutes` chỉ áp dụng đầu phiên sáng (theo comment settings).

**Người dùng cần làm**
- [ ] Điền `risk.max_daily_loss_points`, `risk.max_trades_per_day`, `risk.max_consecutive_losses`; xem lại `risk.stop_grace_seconds`.

**Bước tiếp theo:** P2.2 (paper broker).

## 2026-11-07 (tối) — P1.1 Ghi chú tài liệu broker

**Đã xong**
- `docs/broker_notes.md`: quy định VN30F sau KRX, DNSE Open API V2 (đọc từ đặc tả OpenAPI + mã nguồn SDK `dnse-tech/dnse-py`), SSI FastConnect V3 (mã nguồn `ssi-sdk` 3.3.2). Mỗi mục ghi nguồn; chỗ chưa chắc đánh ❓.
- Xác minh bảng mã KRX bằng hai nguồn độc lập (MBS, Vietstock) ⇒ `core/contracts.py` đúng; thêm test với mã thật `41I1F6000`, `41I1G9000`, `41I1G4000` và ngày GDCC 17/09/2026.
- `Order.client_order_id` giới hạn 1–20 ký tự (khoá idempotency của SSI).
- CLAUDE.md mục 3 cập nhật cách xác thực mới của DNSE V2 (HMAC + trading-token) và quy tắc refresh token một lần của SSI.
- PROMPTS: thêm **P1.6** tải dữ liệu lịch sử thật (DNSE có API lịch sử khớp lệnh từng tick và OHLC 1 phút).
- 122 test.

**Phát hiện quan trọng**
- DNSE đã chuyển sang Open API V2; tài liệu "trước KRX" (JWT + MQTT) là bản cũ.
- Đặt lệnh DNSE **không có khoá idempotency** ⇒ adapter không được tự retry lệnh đặt.
- Không huỷ được lệnh trong ATO/ATC; MTL phần dư thành LO ở giá khớp ± 1 bước.
- Giá thanh toán cuối = trung bình VN30 30 phút cuối ngày GDCC ⇒ giữ mặc định không giao dịch ngày đáo hạn.

**Người dùng cần làm** (thêm vào danh sách bên dưới)
- [ ] Đăng ký DNSE LightSpeed V2 tại developers.dnse.com.vn (lấy `api_key`/`api_secret`, đăng ký email OTP riêng) và SSI FastConnect V3.
- [ ] Khi có key: xác nhận các mục ❓ ở `docs/broker_notes.md` §4.

**Bước tiếp theo:** P1.2 (feed DNSE) — hoặc P1.6 trước nếu đã có API key, để có dữ liệu thật cho backtest.

## 2026-11-07 — Phiên khởi tạo (P0.1, P0.2, P0.3, P1.0)

**Đã xong**
- Khung repo đúng CLAUDE.md mục 2, `config/settings.yaml` (giá trị chưa biết để `null`), `config/holidays.yaml`.
- `core/models.py`: Tick, Bar, Signal, Order, Fill, Position (frozen; `Position.apply` tính giá vốn, lãi chốt, đảo chiều), AccountState (`net_qty` cộng mọi broker).
- `core/calendar.py`, `core/clock.py`: trạng thái phiên theo settings, FakeClock cho backtest, chuyển múi giờ.
- `core/contracts.py`: mã KRX ↔ VN30FYYMM, đáo hạn lùi theo ngày nghỉ, F1M/F2M/F1Q/F2Q theo ngày, hợp đồng giao dịch sau roll.
- Port từ vnstock-dashboard: `utils/atomic_io.py`, `backtest/validation.py`, `monitoring/telegram.py`.
- `live/instance_lock.py`: `check_allowed_machine` (chặn chéo hai máy) + `InstanceLock` (chặn tiến trình thứ hai, heartbeat, phát hiện bị chiếm khoá).
- `scripts/import_dashboard_bars.py` (cứu bar VN30F), `scripts/check_calendar.py`.
- 120 test, CI GitHub Actions.

**Người dùng cần làm**
- [ ] Chạy `import_dashboard_bars.py` trên máy có `C:\vnstock\data_warehouse` (càng sớm càng tốt — archive tự xoá sau 30 ngày).
- [ ] Điền `fees.*`, `risk.*`, `backtest.initial_capital_vnd`, `live.allowed_machine` (`python -m core.settings`).
- [ ] Bổ sung `config/holidays.yaml` theo thông báo lịch nghỉ 2026–2027 của HOSE/HNX.
- [ ] Đối chiếu giờ phiên phái sinh trong `settings.session` với quy định hiện hành.

**Giả định / rủi ro còn mở**
- Mã KRX năm ≥ 2028 chưa xác minh (`krx_year_verified`); luôn đối chiếu danh sách hợp đồng từ broker lúc khởi động.
- Lịch nghỉ hiện chỉ có ngày lễ cố định + Tết 2026 suy ra từ lịch đáo hạn; ngày đáo hạn 2027 có thể sai nếu trùng ngày nghỉ chưa khai.
- Giá trong bar import: dashboard có thể ghi theo điểm hoặc nghìn-lần; script tự chia 1000 khi > 50.000 và báo số dòng đã chia — kiểm vài ngày đầu bằng mắt.

**Bước tiếp theo:** P1.1 (ghi chú tài liệu DNSE LightSpeed sau KRX + SSI FC Data vào `docs/broker_notes.md`).
