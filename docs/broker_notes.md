# Ghi chú API broker

> Điền ở bước P1.1 / P4.1 / P4.2, từ tài liệu CHÍNH THỨC mới nhất. Không đoán tên field.
> Mỗi mục ghi nguồn (URL + ngày đọc) và đánh dấu ❓ chỗ chưa chắc.

## DNSE LightSpeed (bản sau KRX)
- Tài liệu: https://hdsd.dnse.com.vn (mục LightSpeed API — dùng bản SAU KRX, có bản "trước KRX" riêng)
- Xác thực: ❓
- Market Data (streaming): ❓
- Trading (đặt/huỷ/sửa lệnh phái sinh): ❓
- Danh sách hợp đồng hiện hành: ❓

## SSI FastConnect
- Tài liệu: https://guide.ssi.com.vn — SDK Python mới: https://developers.ssi.com.vn (`pip install ssi-sdk`)
- FC Data (streaming phái sinh): ❓
- FC Trading: ký RSA + OTP; stream lệnh dùng `notify_id` (lưu để reconnect, reset 0 lúc 00:00)

## Mã hợp đồng (đã biết)
- Sau KRX: `41I1` + chữ năm (A=2020) + chữ tháng (1–9, A/B/C) + `000` — đã đo ở SSI cho 2024–2027 (vnstock-dashboard, 15/10/2026).
