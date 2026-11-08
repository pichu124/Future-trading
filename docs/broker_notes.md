# Ghi chú API broker

> Đọc ngày **07/11/2026** từ tài liệu và mã nguồn SDK chính thức. Không đoán tên field — chỗ nào
> tài liệu chưa nói rõ được đánh dấu ❓ và PHẢI kiểm bằng gọi thử (UAT hoặc dry-run) trước P4.
> Ký hiệu: ✅ đã xác minh từ ≥1 nguồn chính thức · ✅✅ hai nguồn độc lập khớp nhau · ❓ chưa chắc.

## 0. Quy định sản phẩm VN30F sau KRX

| Mục | Giá trị | Nguồn |
|---|---|---|
| Mã hợp đồng | `4` (CKPS) `1` (HĐTL) `I1` (VN30) + chữ năm + chữ tháng + `000`. Năm: A=2020, B=2021 … (F=2025, G=2026, H=2027). Tháng: `1`–`9`, `A`/`B`/`C` = 10/11/12. VN100 dùng `I2` | ✅✅ MBS (ví dụ `41I1F6000` = 06/2025) + Vietstock (`41I1G9000` = 09/2026) |
| Hợp đồng niêm yết | tháng hiện tại, tháng kế tiếp, 2 tháng cuối quý của 2 quý gần nhất | ✅ MBS |
| Hệ số nhân / bước giá / biên độ | 100.000 · 0,1 điểm · ±7% giá tham chiếu | ✅✅ MBS + Vietstock (biên độ) |
| Ngày giao dịch cuối | thứ Năm tuần 3 của tháng đáo hạn; trùng ngày nghỉ ⇒ phiên liền trước | ✅✅ MBS + Vietstock (`41I1G9000`: GDCC 17/09/2026, khớp `core/contracts.py`) |
| Giá thanh toán cuối cùng | trung bình số học chỉ số VN30 trong **30 phút cuối** ngày GDCC (15′ liên tục cuối + 15′ ATC), bỏ 3 giá trị cao nhất và 3 thấp nhất của phiên liên tục | ✅ MBS — lý do mặc định `trade_on_expiry_day: false` |
| Giờ phiên | ATO 08:45–09:00 · liên tục 09:00–11:30 · nghỉ 11:30–13:00 · liên tục 13:00–14:30 · ATC 14:30–14:45 · thoả thuận 08:45–11:30 và 13:00–14:45 | ✅✅ MBS + FAQ KRX DNSE — khớp `settings.session` |
| Huỷ lệnh | **không** huỷ được trong ATO và ATC; được huỷ trong phiên liên tục | ✅ MBS |
| Loại lệnh | ATO/LO (phiên mở) · LO, MTL, MOK, MAK (liên tục) · ATC/LO (phiên đóng) | ✅ MBS |
| MTL sau KRX | phần chưa khớp chuyển thành LO tại **giá khớp gần nhất ± 1 bước giá** (mua +, bán −) | ✅ FAQ KRX DNSE |
| KL tối đa / lệnh | 500 hợp đồng | ✅ MBS |
| Giới hạn vị thế | cá nhân 5.000 · tổ chức 10.000 · CTCK chuyên nghiệp 20.000 | ✅ MBS |
| Ký quỹ | ban đầu = giá × 100.000 × số HĐ × tỷ lệ ký quỹ (ví dụ 18,48%); phải nộp lên VSDC trước khi giao dịch | ✅ FAQ KRX DNSE |
| Phí bù trừ VSDC | 2.550đ/HĐ **mỗi giao dịch** (thay phí quản lý vị thế qua đêm cũ) | ✅ FAQ KRX DNSE — ❓ kiểm lại biểu phí hiện hành trước khi điền `fees.clearing_fee_vnd_per_contract` |

Mã cũ `VN30FYYMM` chỉ còn là tên hiển thị; hợp đồng niêm yết sau KRX giao dịch bằng mã mới.
DNSE dùng mã KRX khi đặt lệnh (ví dụ trong đặc tả: `"symbol": "41I1G4000"`).

## 1. DNSE — Open API V2 (LightSpeed V2)

**Nguồn:** đặc tả OpenAPI `dnseopenapi-en.yaml` trong repo SDK chính thức `github.com/dnse-tech/dnse-py`
(commit `d0d5c8c`, 01/04/2026; gói PyPI `dnse` 0.5.0, phát hành bằng Trusted Publishing từ org `dnse-tech`).
Trang hướng dẫn: hdsd.dnse.com.vn → "DNSE LightSpeed API V2"; đăng ký dịch vụ tại developers.dnse.com.vn
(ký đề nghị sử dụng dịch vụ với DNSE). Tài liệu "LightSpeed API trước KRX" (JWT + MQTT) là bản **cũ**, không dùng.

### 1.1 Xác thực
- Mỗi request ký **HMAC-SHA256** bằng `api_secret` ✅. Header: `x-api-key`, `date` (SDK mặc định; tuỳ chọn `x-aux-date`), `X-Signature` ✅ (SDK 0.5.0 `_hmac_signer`).
- Chuỗi ký (theo MÃ NGUỒN SDK — docstring cũ ghi "path kèm query", mã thật ký path KHÔNG kèm query, xem §1.3 ❓):
  ```
  (request-target): <method thường> <path>
  date: <RFC 2822 UTC, vd "Sat, 07 Nov 2026 13:00:00 +0000">
  nonce: <uuid4 hex>
  ```
  `X-Signature: Signature keyId="<api_key>",algorithm="hmac-sha256",headers="(request-target) date",signature="<base64, URL-encode>",nonce="<nonce>"` ✅ (SDK)
- **Trading token** (bắt buộc cho đặt/sửa/huỷ lệnh, đóng vị thế) ✅:
  1. `POST /registration/send-email-otp` → DNSE gửi OTP vào email;
  2. `POST /registration/trading-token` body `{"otpType": "email_otp" | "smart_otp", "passcode": "123456"}` → nhận token;
  3. gửi kèm header `trading-token` ở các lệnh thay đổi.
- Hết hạn token ⇒ SDK ném `DnseSessionExpiredError`, phải xác thực OTP lại ✅. **Thời hạn token** ❓.
- Rate limit: HTTP 429 kèm `retry_after`; header hạn mức được SDK đọc ✅. **Hạn mức cụ thể** ❓.

### 1.2 Môi trường
- UAT: `https://openapi-uat.dnse.com.vn` ✅ (servers trong đặc tả).
- Production: `https://openapi.dnse.com.vn` — SDK dùng làm mặc định nhưng tự ghi chú "placeholder" ❓ → xác nhận khi đăng ký.
- WebSocket: `wss://ws-openapi.dnse.com.vn/v1/stream?encoding=json|msgpack` ✅ (SDK).

### 1.3 REST — endpoint dùng cho bot (đều ký HMAC)
| Việc | Method + path | Tham số chính |
|---|---|---|
| Tài khoản | `GET /accounts` | — |
| Tiền | `GET /accounts/{accountNo}/balances` | — |
| Sức mua/bán | `GET /accounts/{accountNo}/ppse` | `marketType=DERIVATIVE`, `symbol`, `loanPackageId`, `price` |
| Gói vay | `GET /accounts/{accountNo}/loan-packages` | `marketType=DERIVATIVE`, `symbol` |
| **Đặt lệnh** | `POST /accounts/orders` + `trading-token` | query `marketType=DERIVATIVE`, `orderCategory=NORMAL`; body `accountNo`, `symbol` (mã KRX), `side` (`NB` mua / `NS` bán), `orderType` (`LO`, `MOK`, `MAK`, `MTL`, `ATO`, `ATC`), `price`, `quantity`, `loanPackageId` |
| Sửa lệnh | `PUT /accounts/{accountNo}/orders/{orderId}` + `trading-token` | SDK: huỷ-rồi-đặt, **trả orderId MỚI** |
| Huỷ lệnh | `DELETE /accounts/{accountNo}/orders/{orderId}` + `trading-token` | `marketType`, `orderCategory` |
| Chi tiết / sổ lệnh / lịch sử | `GET …/orders/{orderId}`, `GET …/orders`, `GET …/orders/history` | `marketType`, `orderCategory` / `from`,`to` |
| Vị thế | `GET /accounts/{accountNo}/positions` | `marketType=DERIVATIVE`, `pageSize` |
| **Đóng vị thế** | `POST /accounts/positions/{positionId}/close` + `trading-token` | `marketType=DERIVATIVE` (hiện chỉ hỗ trợ phái sinh) |
| Danh sách HĐTL | `GET /instruments` | `marketId=DVX`, `securityGroupId=FU`, `symbol`, `limit`, `page` |
| Trần/sàn/tham chiếu | `GET /price/{symbol}/secdef` | `boardId=G1` |
| **Lịch sử khớp lệnh (tick)** | `GET /price/{symbol}/trades` | `boardId=G1`, `from`, `to` (timestamp), `limit` |
| Khớp gần nhất | `GET /price/{symbol}/trades/latest` | `boardId=G1` |
| **Lịch sử OHLC** | `GET /price/ohlc` | `symbol`, `type=DERIVATIVE`, `resolution` (`1`,`3`,`5`,`15`,`30`,`1h`,`1D`,`1W`), `from`, `to` |

⚠️ **Body đặt lệnh KHÔNG có khoá idempotency** (không có client order id) ✅. Retry sau timeout có thể sinh
lệnh đôi ⇒ adapter DNSE phải: không tự retry lệnh đặt; khi không chắc lệnh đã tới sàn thì tra sổ lệnh
(khớp `symbol`, `side`, `quantity`, `price`, thời điểm) trước khi gửi lại.

❓ Độ sâu lịch sử của `/price/{symbol}/trades` và `/price/ohlc` cho hợp đồng ĐÃ đáo hạn; đơn vị `from`/`to`
(giây hay mili-giây); timezone của timestamp trả về.

**Đối chiếu SDK `dnse` 0.5.0 cho REST lịch sử (08/11/2026):**
- `/price/{symbol}/trades` có trong SDK (`resources/market.py`): tham số `boardId`, `from`, `to` (SDK chỉ ghi
  "timestamp string" ⇒ đơn vị ❓), `limit`; không thấy tham số phân trang ❓. Model `Trade`: `market_id`,
  `board_id`, `isin`, `symbol`, `match_price`, `match_qtty`, `side`, `avg_price`, `total_volume_traded`,
  `gross_trade_amount`, `highest_price`, `lowest_price`, `open_price`, `time` (chuỗi, định dạng ❓); trên dây
  nhiều khả năng camelCase (`matchPrice`…) ❓.
- `/price/ohlc` **không có trong SDK** (chỉ có trong đặc tả OpenAPI) ⇒ tên field, kiểu bọc response ❓.
- ⚠️ Chữ ký: SDK ký `(request-target)` bằng **đường dẫn KHÔNG kèm query** (tham số gửi riêng), trái với dòng
  "path kèm query" ở §1.1 (lấy từ docstring SDK). Chưa biết server chấp nhận cách nào ❓.
- Cả ba điểm trên được chốt bằng `scripts/dnse_history_probe.py` (thử 4 định dạng from/to, hôm qua + 1/3/6/12
  tháng trước với đúng hợp đồng từng ngày, ký không-query rồi kèm-query khi 401, ghi response thô).

### 1.4 WebSocket stream
- Xác thực ngay sau khi kết nối ✅:
  `{"action": "auth", "api_key": …, "signature": hex(HMAC-SHA256(secret, f"{api_key}:{timestamp}:{nonce}")), "timestamp": <giây>, "nonce": <micro-giây>}`.
  Lỗi ⇒ server gửi `{"action": "auth_error", "message": …}`.
- Ping/pong: client gửi `{"action": "ping"}` mỗi 30s; server gửi `ping` thì trả `{"action": "pong"}` ✅.
- Kênh thị trường (gửi kèm `symbols`) ✅: `tick.G1.json` (khớp lệnh `t`/`te`, giá tốt nhất `q`, giá dự kiến `e`, secdef `sd`) · `ohlc.<timeframe>.json` (nến `b`).
- Kênh riêng tài khoản ✅: `orders` (`o`), `positions` (`p`), `account` (`a`).
- Phân loại message theo trường `T` (hoặc `t`) ✅. Trường dữ liệu theo model SDK (snake_case; wire là camelCase ❓):
  - trade: `symbol`, `price`, `volume` (KL lần khớp), `timestamp`, `side`; bản `te` thêm `total_volume`, `total_value`;
  - quote: `bid_price`, `bid_volume`, `ask_price`, `ask_volume`, `timestamp` (chỉ mức giá TỐT NHẤT);
  - order: `order_id`, `symbol`, `side`, `qty`, `price`, `status`, `timestamp`;
  - position: `symbol`, `qty`, `avg_price`, `market_value`.
- SDK tự reconnect 10 lần (backoff 1s → 60s) rồi ném lỗi ✅ ⇒ runner phải tự xử lý khi hết lượt.
- **Đối chiếu mã nguồn SDK `dnse` 0.5.0 (`dnse/stream/*.py`, đọc 08/11/2026)** — áp vào `feeds/data_feed_dnse.py`:
  - sau khi mở kết nối, server gửi một lời chào (SDK đọc `session_id`) TRƯỚC khi xác thực ✅;
  - message đăng ký là `{"name": "tick.G1.json", "symbols": [<mã KRX>]}` — **không** có trường `action` ✅;
    trade, quote, giá dự kiến, secdef đều đi chung kênh `tick.G1.json` ✅;
  - phản hồi xác thực thành công: SDK chỉ kiểm `action != "auth_error"` ⇒ định dạng bản thành công ❓;
  - model dùng `alias_generator=to_camel` + `populate_by_name` ⇒ field trên dây nhiều khả năng camelCase
    (`bidPrice`, `askVolume`, `totalVolume`, `refPrice`) ❓ — parser của bot nhận cả hai kiểu;
  - model không có trường số thứ tự ⇒ **không phát hiện gap theo sequence được** ✅ (SDK) — bot dùng phát hiện im lặng;
  - SDK coi `recv` timeout 5s là bình thường (chỉ lặp lại), không coi kết nối im lặng là chết — bot tự đặt ngưỡng;
  - SDK 0.5.0 **chưa cài** endpoint `GET /instruments` (chỉ có trong đặc tả OpenAPI) ⇒ danh sách hợp đồng để đối chiếu
    lúc khởi động phải tự gọi REST ký HMAC (P4.1); field trả về ❓.
- Kiểm bằng `scripts/dnse_feed_probe.py --krx <mã>` khi có khoá: ghi message thô ra `data/probe/` để chốt các ❓ trên.
- ❓ Kênh có sổ lệnh nhiều bước giá (top 3/top 10) hay không; trường `side` của trade là bên chủ động?;
  đơn vị `timestamp` (giây/ms); kênh `orders` có cần trading token không; có số thứ tự message để phát hiện gap không.

### 1.5 Giao dịch — SDK `dnse` 0.5.0 (`resources/orders.py`, `registration.py`, `accounts.py`, `deals.py`; đọc 08/11/2026) → `brokers/dnse.py`
- **OTP / token** ✅: `POST /registration/send-email-otp` (không body; 2xx = đã gửi mail) → `POST /registration/trading-token`
  body `{"otpType": "email_otp", "passcode": "<mã>"}` → `{"tradingToken": "…"}`. SDK chỉ thêm header `trading-token`
  cho POST/PUT/DELETE có `/orders` trong path. Token hết hạn ⇒ HTTP 401 với `code`/`errorCode` = `INVALID_TRADING_TOKEN` ✅.
  **Không có endpoint gia hạn token** trong SDK ⇒ "tự gia hạn" = xin OTP mới trước khi hết hạn và người dùng nhập lại.
- **Retry trong SDK**: `orders.place/update/cancel` gọi `_send` trực tiếp (KHÔNG retry); chỉ `client.request()` chung mới
  retry 429 (tối đa 3 lần) ✅ ⇒ adapter tự viết, không dùng đường có retry.
- **Đặt lệnh** ✅: `POST /accounts/orders?marketType=DERIVATIVE&orderCategory=NORMAL`, body (camelCase, SDK gửi cả `null`)
  `accountNo, symbol (KRX), side NB|NS, orderType LO|MTL|ATO|ATC|MOK|MAK, quantity, price (null nếu không phải LO), loanPackageId`
  → `PlaceOrderResponse`: `id, accountNo, symbol, side, orderType, orderStatus, orderCategory, marketType, quantity,
  fillQuantity, leaveQuantity, canceledQuantity, price, averagePrice, …`.
- **Sổ lệnh** ✅: `GET /accounts/{acc}/orders?marketType&orderCategory` → `{"orders": [OrderItem]}`; một lệnh:
  `GET /accounts/{acc}/orders/{id}` → `OrderItem`. `OrderItem` = các field trên + `priceSecure, loanPackageId, transDate,
  createdDate, modifiedDate, metadata, error`. ⚠️ Server nhét JSON chưa escape vào chuỗi `metadata` — SDK sửa bằng regex
  trước khi parse; adapter làm y như vậy (`sanitize_metadata`).
- **Sửa** ✅: `PUT /accounts/{acc}/orders/{id}` body `{"price"?, "quantity"?}` (bỏ field null) → `OrderItem` của lệnh **MỚI**
  (huỷ-rồi-đặt, id mới). **Huỷ** ✅: `DELETE /accounts/{acc}/orders/{id}` → 204.
- **Vị thế** ✅: `GET /accounts/{acc}/positions?marketType=DERIVATIVE` → `{"deals": [DealItem], "total", "pageSize", …}`;
  `DealItem`: `id, symbol, side ("BUY"|"SELL" theo chú thích SDK), status ("OPEN"), openQuantity, costPrice, …`.
- **Tài khoản** ✅: `GET /accounts` → `{"accounts": [{"id", "dealAccount", "derivativeAccount"}], "investorId", …}`;
  gói vay `GET /accounts/{acc}/loan-packages?marketType=DERIVATIVE&symbol=` → `{"loanPackages": [{id, name, …}]}`.
- **Stream lệnh** (`orders`, T=`o`): model chỉ có `orderId, symbol, side, qty, price, status, timestamp` — KHÔNG có khối
  lượng đã khớp ⇒ adapter đọc sổ lệnh REST mỗi `poll_seconds` để lấy khớp lệnh (stream để dành tối ưu sau).

**Adapter `brokers/dnse.py` quyết định gì khi tài liệu chưa nói (❓ — kiểm bằng `scripts/dnse_trading_probe.py`):**
| ❓ | Adapter đang làm | Cách chốt |
|---|---|---|
| Giá trị `orderStatus` (SDK không liệt kê) | suy từ `fillQuantity/leaveQuantity/canceledQuantity` trước, chuỗi sau; chuỗi lạ ⇒ coi còn sống + log cảnh báo | probe `--uat --order-test` in danh sách chuỗi thấy được |
| `quantity` khi sửa lệnh là tổng hay phần còn lại | gửi **phần còn lại** (lệnh mới là lệnh riêng — gửi tổng có thể khớp quá số) | probe `--with-modify` trên lệnh đã khớp một phần (UAT) |
| `side` của deal (`BUY/SELL` hay `NB/NS`) | nhận cả hai; giá trị lạ ⇒ lỗi, không đoán | probe đọc `/positions` |
| deal `status` khác `OPEN` mà còn `openQuantity` | lỗi, không đoán | như trên |
| Thời hạn trading token | `trading_token_ttl_seconds` (null ⇒ không chạy live) | hỏi DNSE / đo bằng probe |
| Hạn mức rate limit cho đọc sổ lệnh mỗi giây | `poll_seconds: 1` | header `x-ratelimit-*` probe ghi lại |
| Sổ lệnh trả cả lệnh đã xong trong ngày? | lệnh đang theo dõi mà vắng ⇒ đọc riêng theo id | probe |
| Đơn vị `price` phái sinh (điểm) | gửi điểm, bước 0,1 | probe đọc secdef + đặt UAT |

## 2. SSI — FastConnect V3 (SDK `ssi-sdk`)

**Nguồn:** mã nguồn gói PyPI `ssi-sdk` **3.3.2** (20/10/2026, tác giả "SSI Team", maintainer `ssi.developer`,
homepage developers.ssi.com.vn) + trang hướng dẫn developers.ssi.com.vn. FastConnect cũ (guide.ssi.com.vn,
SignalR, `fc-tradeapi`) là thế hệ trước.

### 2.1 Xác thực
- Config: `client_id`, `api_key`, `api_secret`, `private_key` (ký lệnh) ✅.
- `POST /api/v3/auth/token` với OTP (SMS/email) **hoặc** Smart OTP (`transaction_id` sau khi duyệt trên app; trạng thái chờ duyệt = mã `401114`) ✅.
- Refresh: `POST /api/v3/auth/refresh`. ⚠️ **Dùng lại refresh token đã dùng ⇒ server coi là lộ và thu hồi TOÀN BỘ phiên của apiKey**; các mã `401101–401108` ⇒ chỉ còn cách đăng nhập OTP lại, **không retry** ✅.
- Header: `Authorization: Bearer …`, `X-Signature`; rate limit header `X-RateLimit-Limit`/`-Remaining`, `Retry-After` ✅. SDK mặc định tự giới hạn 10 req/giây ✅.
- Stream dữ liệu thị trường không cần OTP ✅ (developers.ssi.com.vn).

### 2.2 Môi trường
- REST: `https://api.ssi.com.vn` · stream dữ liệu: `wss://stream.ssi.com.vn/ws/v3` · stream giao dịch: `wss://api.ssi.com.vn/ws/v3/trading` ✅ (hằng số SDK 3.3.2).

### 2.3 Giao dịch phái sinh
- Tài khoản phái sinh có số kết thúc bằng **8** ✅.
- `POST /api/v3/trading/order`: `account_no`, `symbol`, `side`, `quantity`, `price`, `order_type` (`LO`, `MTL`, `MP`, `ATO`, `ATC`, `MOK`, `MAK`, `PLO`), **`client_request_id` ≤ 20 ký tự** — khoá idempotency: gửi trùng trong ngày ⇒ HTTP 409 `DuplicateRequestError`; SDK không bao giờ tự retry lệnh ✅.
- Sửa giá/khối lượng, huỷ theo `client_request_id` hoặc `order_id` ✅.
- Vị thế phái sinh: `get_derivative_positions`, `get_open_derivative_positions`, `get_derivative_balance`, `get_derivative_ppmmr` ✅.
- Trạng thái lệnh: `PENDING_APPROVAL, READY, SENT, QUEUED, FILLED, PARTIAL_FILLED, PARTIAL_CANCELLED, PENDING_MODIFY, PENDING_CANCEL, CANCELLED, REJECTED, EXPIRED, PRE_SESSION` ✅.
- ❓ Mã hợp đồng SSI V3 nhận khi đặt lệnh: docstring SDK ghi ví dụ `VN30F2606`, còn dữ liệu SSI mà vnstock-dashboard đọc trả mã KRX ⇒ lấy danh sách từ `masterdata` lúc khởi động, không tự đoán.
- ❓ `price` của phái sinh tính bằng điểm (docstring SDK chỉ nói "VND" cho cổ phiếu).

### 2.4 Stream
- Topic: `<loại>.<mã>` với loại ∈ `trade`, `quote`, `room`, `put`, `oddlot`, `market`, `order`, `portfolio`, `trade.index`, `indexsummary`; nến trên topic `trade`: `@tick`, `@1m`, `@5m` (vd `trade.41I1GB000@tick`) ✅.
- `TradeMessage`: `trading_time`, `symbol`, `price`, `quantity`, `side`, `total_volume`, `open`, `high`, `low`, `avg` ✅.
- `QuoteMessage`: `bid_prices[]`, `bid_volumes[]`, `ask_prices[]`, `ask_volumes[]` (nhiều bước giá; số bước ❓) ✅.
- `OrderStatusMessage`: `client_request_id`, `order_id`, `status`, `filled_quantity`, `avg_price`, `reject_reason`, **`notify_id`** … ✅.
- `PortfolioMessage` (chỉ tài khoản phái sinh): `long_qty`, `short_qty`, `net`, `bid_avg_price`, `ask_avg_price`, `floating_pl`, `trading_pl` ✅.
- Frame gửi lên ≤ 4.096 byte ✅.
- **Đối chiếu mã nguồn SDK `ssi-sdk` 3.3.2 (`transport/websocket_client.py`, `transport/dispatch.py`,
  `services/streaming.py`, `models/streaming.py`, đọc 08/11/2026)** — áp vào `feeds/data_feed_ssi.py`:
  - xác thực stream bằng header **`Authorization: Bearer <access token>`** lúc bắt tay ✅; token lấy qua REST
    `POST /api/v3/auth/token` body `{"apiKey","apiSecret"[,"otp"|"transactionId"]}` ✅ — OTP là tuỳ chọn trong
    SDK; server có cấp token chỉ-dữ-liệu KHÔNG cần OTP không ❓ (ghi chú cũ "không cần OTP" chưa kiểm);
  - yêu cầu: `{"method": "subscribe"|"unsubscribe"|"ping_pong"|"list_subscription", "channel": "DATA"|"TRADING"|"HEARTBEAT", "topics": [...]}` ✅;
    ack `{"method","channel","status","message"}` (không có `topic`); `status:"error"` với "Invalid"/"Denied" ✅;
  - dữ liệu: `{"channel":"DATA","topic":"trade.<mã>","data":{...}}` ✅; trade: `t` thời gian (định dạng ❓), `s` mã,
    `p` giá (có thể là chữ như "ATO"), `q` KL lần khớp, `si` chiều, `v` KL luỹ kế; số có thể là chuỗi `"1,234.0"` ✅;
    quote: `bids`/`asks` = `[[giá, KL], …]`, tốt nhất đứng đầu ✅ (số mức ❓); nến `trade.<mã>@1m|@5m` ✅;
  - server từ chối kết nối: frame `{"code","msg"[,"retryAfter"]}` rồi đóng; 401/403 = xác thực, 429 = quá hạn mức
    (SDK chờ ≥ 30s) ✅; bắt tay HTTP 401/403 ⇒ SDK lấy token mới MỘT lần rồi bỏ cuộc ✅;
  - backoff kết nối lại: lũy thừa, chặn 60s, jitter 50–100% ✅; tối đa 10 socket / client ✅.

### 2.5 Dữ liệu lịch sử
- `get_ohlc_{1minute…1hour}_historical(symbol, from_date, to_date, page, size≤1000)` ✅. ❓ có trả dữ liệu cho hợp đồng đã đáo hạn không.

### 2.6 Giao dịch — mã nguồn SDK `ssi-sdk` 3.3.2 (`services/trading.py`, `token_manager.py`, `portfolio.py`, `transport/rest_client.py`, `utils/crypto.py`, `models/*`; đọc 08/11/2026) → `brokers/ssi.py`
- **Token** ✅: `POST /api/v3/auth/requestOtp` body `{"apiKey","apiSecret"}` (SMS/email OTP; Smart OTP trả `transactionId`) →
  `POST /api/v3/auth/token` body `{"apiKey","apiSecret","otp"}` → `{accessToken, expiresAt (epoch giây; ≥1e10 là ms), refreshToken,
  refreshExpiresAt}` (có thể bọc trong `data`). Refresh `POST /api/v3/auth/refresh` body `{"refreshToken"}`; mã `401101–401108`
  ⇒ đăng nhập OTP lại, không retry. SDK coi token hết hạn sớm 30s. Một phiên mỗi apiKey: đăng nhập/refresh ở tiến trình khác
  làm token này mất hiệu lực ⇒ feed và broker SSI dùng CHUNG một `SsiTokenManager`.
- **Ký lệnh** ✅: RSA PKCS#1 v1.5 SHA-256 trên đúng chuỗi body (`json.dumps` mặc định), hex, header `X-Signature`; khoá RSA dạng XML
  (`<RSAKeyValue><Modulus>…<D>…`) mã hoá base64. Mọi request kèm `Authorization: Bearer <access>`.
- **Lỗi** ✅: HTTP ≥ 400, HOẶC HTTP 2xx có `{code, msg}` với `code` ≠ 200/204 (vd 200 + `{"code":400123,"msg":…}`). 204 /
  `{"code":204}` = rỗng. 409 = trùng `clientRequestId`. SDK chỉ retry GET; POST/PUT/DELETE không bao giờ.
- **Đặt** ✅: `POST /api/v3/trading/order` body (thứ tự khoá như SDK) `accountNo, symbol, side (B|S), quantity, price (chuỗi thập
  phân; "0" cho lệnh thị trường/ATO/ATC), orderType, clientRequestId (≤ 20), deviceId (machine id của hệ điều hành), userAgent`
  → `{orderId, clientRequestId, orderStatus}`.
- **Sửa** ✅: `PUT /api/v3/trading/order` body `accountNo, clientModifyId (ngẫu nhiên), deviceId, userAgent, clientRequestId|orderId,
  price|quantity` — MỘT thay đổi mỗi request (SDK cấm sửa cả giá lẫn khối lượng một lần). **Huỷ** ✅: `DELETE` cùng path, body
  `accountNo, clientCancelId, deviceId, userAgent, clientRequestId|orderId`.
- **Sổ lệnh** ✅: `GET /api/v3/trading/orderBook?accountNo&from&to (YYYY/MM/DD)&pageIndex&pageSize` → `{accountNo, orderList, totalRecord}`;
  dòng lệnh: `orderId, clientRequestId, instrumentId|symbol, buySell|side, orderType, price, avgPrice, quantity, filledQty, cancelQty,
  orderStatus, inputTime, modifiedTime, message`. SDK: "đừng tin totalRecord, đọc tới khi trang rỗng".
- **Mã trạng thái** ✅: PD chờ xử lý · WA chờ duyệt · RS sẵn gửi · SD đã gửi sàn · QU nằm sổ · PF khớp một phần · FF khớp hết ·
  FFPC khớp một phần, phần còn lại huỷ · WM chờ sửa · WC chờ huỷ · CL huỷ · RJ từ chối · EX hết hạn · IAV chờ phiên · SOI/PAS/CPL/RQ/ERR
  (engine). Adapter: chuỗi lạ ⇒ suy từ khối lượng.
- **Vị thế** ✅: `GET /api/v3/trading/position?clientId&querySummary=true&accountNo` → `derivative.derOpenPositions[]`:
  `symbol, long, short, net, bidAvgPrice, askAvgPrice, tradePrice, floatingPL, tradingPL`.
- **Stream TRADING** ✅ (cùng `wss://stream.ssi.com.vn/ws/v3`): `{"method":"subscribe","channel":"TRADING","topics":["order.<tk>","portfolio.<tk>"]}`;
  frame `{"channel":"TRADING","topic":"order.<tk>","data":{eventType, notifyId, …}}` với `eventType` ∈ `orderEvent` (trạng thái,
  `filledQty`/`avgPrice` CỘNG DỒN), `orderMatchEvent` (một lần khớp: `matchQty`, `matchPrice`), `clientPortfolioEvent`
  (`longQty, shortQty, net, bidAvgPrice, askAvgPrice`). **SDK không có cơ chế phát lại theo `notifyId`** ⇒ bot đọc lại sổ lệnh
  REST sau mỗi lần (re)connect; `notifyId` chỉ dùng để bỏ frame trùng.
- **Lệnh điều kiện (FCO)**: SDK có `place_fco_stop`, `stop_limit`, `trailing_stop`, `oco` (`/api/v3/trading/fco/order`) ⇒ trả lời
  một phần câu hỏi §4 cho SSI; hỗ trợ phái sinh ❓ ⇒ adapter vẫn dùng stop do bot giữ.
- Không thấy môi trường UAT trong SDK ⇒ không có chỗ đặt lệnh thử bằng tiền ảo.

**Adapter `brokers/ssi.py` quyết định gì khi chưa rõ (❓ — chốt bằng `scripts/ssi_trading_probe.py`):**
| ❓ | Adapter đang làm | Cách chốt |
|---|---|---|
| Mã hợp đồng khi đặt lệnh (KRX hay VN30FYYMM) | `brokers.ssi.order_symbol_format` (null ⇒ không chạy live) | probe in mã masterdata trả về |
| Lệnh thị trường phái sinh là `MTL` hay `MP` | `brokers.ssi.market_order_type` (null ⇒ không chạy live) | hỏi SSI / tài liệu phái sinh |
| Response có bọc `data` không | nhận cả hai | probe ghi `wrapped_in_data` |
| `quantity` khi sửa là tổng hay phần còn lại | gửi tổng (sửa tại chỗ, cùng clientRequestId) | gọi thử khi được phép |
| `bidAvgPrice`/`askAvgPrice` ứng với Long/Short | Long ⇒ `bidAvgPrice`, Short ⇒ `askAvgPrice` | đối chiếu app khi có vị thế |
| Đơn vị giá phái sinh (điểm) | gửi điểm, bước 0,1 | probe masterdata |
| Token chỉ-dữ-liệu có cần OTP | feed dùng chung token có OTP | probe |

## 3. Hệ quả cho thiết kế (đã/ sẽ áp vào code)

1. `core/contracts.py`: bảng chữ năm A=2020 đã được hai nguồn độc lập xác nhận ⇒ giữ nguyên. Danh sách hợp đồng thật vẫn lấy từ `GET /instruments?marketId=DVX&securityGroupId=FU` (DNSE) / `masterdata` (SSI) lúc khởi động.
2. `Order.client_order_id` ≤ 20 ký tự (giới hạn SSI) — đã thêm kiểm tra vào `core/models.py`.
3. Adapter DNSE không có idempotency phía server ⇒ không auto-retry lệnh đặt; lệnh "không rõ" (timeout/5xx) được đối soát với sổ lệnh (lệnh MỚI xuất hiện sau lúc gửi, khớp mã/chiều/loại/KL/giá), quá `uncertain_resolve_seconds` không thấy ⇒ REJECTED — đã làm ở `brokers/dnse.py` (P4.1).
4. Không đặt/huỷ được lệnh trong ATO/ATC ⇒ `flatten_time` (14:25) phải trước ATC đủ xa để còn huỷ/đặt lại trong phiên liên tục.
5. Sửa lệnh DNSE trả orderId mới ⇒ broker adapter phải cập nhật ánh xạ `client_order_id → broker_order_id` sau mỗi lần sửa.
6. Dữ liệu backtest thật: DNSE `/price/{symbol}/trades` (tick) và `/price/ohlc` (1 phút), SSI OHLC 1 phút lịch sử ⇒ thêm bước P1.6 tải lịch sử, tốt hơn nhiều so với dữ liệu snapshot import từ dashboard.
7. Refresh token SSI chỉ dùng MỘT lần ⇒ `SsiTokenManager` (P4.2) bỏ refresh token khỏi bộ nhớ TRƯỚC khi gửi; lỗi mạng giữa chừng ⇒ coi như đã dùng, xin OTP. Token chỉ ở bộ nhớ (không ghi đĩa) ⇒ khởi động lại cần OTP, đổi lại không bao giờ có token cũ trên đĩa để bị gửi lại.

## 4. Việc cần người dùng xác nhận khi đăng ký API
- [ ] DNSE: base URL production, thời hạn trading token, hạn mức rate limit, có kênh sổ lệnh nhiều bước giá không.
- [ ] DNSE/SSI: lịch sử tick/OHLC của hợp đồng đã đáo hạn lùi được bao xa.
- [ ] SSI: mã hợp đồng nhận khi đặt lệnh (KRX hay VN30FYYMM), đơn vị giá phái sinh.
- [ ] Biểu phí phái sinh hiện hành (môi giới, sở, VSDC) để điền `fees.*`.
- [ ] ❓ DNSE/SSI có lệnh điều kiện (stop / stop-limit) cho phái sinh qua API không? Danh sách
  `orderType` ở §1.3 và §2.3 **không có** stop ⇒ hiện giả định bot phải tự giữ stop (theo dõi giá
  rồi gửi MTL). **DNSE (P4.1):** adapter giữ `OrderType.STOP` ở bot (`broker_order_id = "local:…"`,
  trạng thái PENDING ⇒ risk coi là stop đã xác nhận), tick chạm giá ⇒ gửi MTL cùng client id; phòng
  stop mù bằng luật `flatten_on_feed_silence_seconds`. Nếu DNSE có lệnh điều kiện thật thì nên chuyển sang.
