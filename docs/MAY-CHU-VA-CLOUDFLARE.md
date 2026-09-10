# Máy chủ và Cloudflare

## Cấu hình hiện tại

DriveDrop Boss và dịch vụ Cloudflare Tunnel cùng chạy trên máy chủ Windows. Tên miền công khai: `https://drive.vinhglobal.vn`.

| Chức năng | Địa chỉ |
|---|---|
| Thiết lập/quản trị cục bộ | `http://127.0.0.1:48801` |
| Origin HTTPS cho Cloudflare | `https://localhost:48800` |
| Broker HTTPS trực tiếp | Cổng `48765` |

Trong ứng dụng Boss: nhập OAuth JSON loại Desktop app, đăng nhập tài khoản Google quản lý Drive, bấm **Thử upload thật**. Dùng nút **Quản trị web** để tạo tài khoản quản trị lần đầu trên máy chủ. Không đặt mật khẩu Google vào tài khoản web.

## Route Cloudflare

Với connector chạy cùng máy với DriveDrop:

| Thiết lập | Giá trị |
|---|---|
| Hostname | `drive.vinhglobal.vn` |
| Service URL | `https://localhost:48800` |
| HTTP Host Header | `drive.vinhglobal.vn` |
| Origin Server Name | `localhost` |
| CA Pool | Đường dẫn tuyệt đối đến chứng chỉ công khai của chính origin trên máy chạy connector |
| Disable TLS certificate verification | Tắt |

CA Pool chỉ chứa chứng chỉ công khai, không đưa khóa riêng lên GitHub. Dịch vụ cloudflared phải đọc được tệp này. Không cần mở cổng trên router với Tunnel đi ra ngoài.

Ứng dụng Mac gọi API thiết bị trên cùng tên miền; một trang đăng nhập hoặc JavaScript challenge đặt trước toàn bộ hostname sẽ cản app. Trang quản trị DriveDrop dùng đăng nhập riêng; API thiết bị dùng kích hoạt một lần và chữ ký từng thiết bị.

## Vận hành và chuyển máy

- Máy chủ phải bật, có Internet, chạy DriveDrop và cloudflared. Đóng cửa sổ Boss thường ẩn xuống khay; **Thoát và dừng máy chủ** sẽ ngắt dịch vụ.
- Nhân viên chuẩn bị Mac trước, sau đó sếp tạo file kích hoạt trên web. Mã dùng một lần và hết hạn sau 15 phút.
- Khi chuyển về máy chủ khác: hoàn tất hàng đợi, sao lưu dữ liệu riêng ở nơi nội bộ, cài bản phù hợp, thiết lập lại quyền Google/kho bí mật khi cần, rồi chuyển Tunnel/origin và chứng chỉ. Giữ tên miền cũ giúp giữ địa chỉ truy cập; có thể cần kích hoạt lại thiết bị nếu khóa máy chủ thay đổi.
- Không sao chép dữ liệu DPAPI sang máy/tài khoản Windows khác rồi coi là đã đăng nhập được. Kiểm tra upload thật sau khi chuyển.

[Tham khảo cấu hình origin của Cloudflare](https://developers.cloudflare.com/tunnel/advanced/origin-parameters/).
