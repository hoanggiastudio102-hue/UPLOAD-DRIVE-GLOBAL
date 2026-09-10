# DriveDrop · VinhGlobal 0.5.0

Gửi ảnh/video từ máy nhân viên lên Google Drive và quản lý nhân viên, máy, thư mục qua web.

**Trang quản trị:** https://drive.vinhglobal.vn — cần tài khoản quản trị do sếp cấp.

## Tải bản cài

[Mở trang tải DriveDrop 0.5.0](https://github.com/hoanggiastudio102-hue/UPLOAD-DRIVE-GLOBAL/releases/tag/v0.5.0).

- **Mac nhân viên:** `DriveDrop-Mac-0.5.0-r2.zip` (hướng dẫn mới) hoặc `DriveDrop-Mac-0.5.0.zip` (cùng mã ứng dụng). Đây là ứng dụng mở bằng Python, cần Python 3.12+ có Tkinter; chưa ký/notarize Apple. Đọc [hướng dẫn cài và kích hoạt](docs/HUONG-DAN-CAI-MAC-VA-KET-NOI.txt).
- **Máy chủ Windows:** `DriveDrop-Boss-0.5.0.zip`. Giải nén cả thư mục, giữ nguyên `_internal`, chạy `DriveDrop-Boss.exe`.
- **Mac từ mã nguồn:** `DriveDrop-Mac-Source-0.5.0.zip`.
- Đối chiếu `SHA256.txt` ở trang tải nếu cần kiểm tra gói.

## Luồng upload

1. Máy nhân viên xin phiên upload cho từng file qua máy chủ.
2. Máy chủ tạo phiên bằng tài khoản Google đã được cấp quyền.
3. Máy nhân viên upload nội dung **trực tiếp lên Google Drive**, dùng Internet nơi nhân viên làm việc.
4. Máy chủ xác minh thư mục, dung lượng, SHA256 và MD5 rồi cập nhật báo cáo.

Cloudflare Tunnel chuyển giao diện, yêu cầu cấp phiên và trạng thái; không truyền hộ ảnh/video. Với tên miền hiện tại, nhân viên ở khác mạng vẫn kết nối bằng Internet và không cần NetBird. Máy chủ cần hoạt động để cấp phiên mới và xác minh. Mặc định giữ bản local; chỉ xóa khi người dùng bật lựa chọn tương ứng và file đã được xác minh.

## Báo cáo 0.5.0

- Tổng nhân viên, máy đang kết nối, máy cần kiểm tra và file xác minh hôm nay.
- Nhóm **nhân viên → máy → thư mục**, đếm ảnh/video, dung lượng và hàng đợi.
- Biểu đồ 7 ngày, tìm kiếm, bộ lọc và xuất CSV.
- Mã nhân viên dùng chung giúp gộp nhiều máy của cùng một người.
- [Cách đọc báo cáo và cập nhật Mac](docs/HUONG-DAN-BAO-CAO-0.5.md).

Máy Mac cũ cần cập nhật ứng dụng một lần để gửi số liệu thư mục. Chưa có cơ chế tự tải/cài bản mới trên Mac.

## Thiết lập máy chủ

Xem [hướng dẫn máy chủ và Cloudflare](docs/MAY-CHU-VA-CLOUDFLARE.md).
GitHub lưu mã nguồn và bộ cài; trang DriveDrop vẫn chạy trên máy chủ đã cài ứng dụng.

## Chạy mã nguồn và kiểm thử

Cài Python 3.12 trở lên có Tkinter. Trong thư mục dự án:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python run_boss.py
# Hoặc máy nhân viên:
python run_client.py
```

Kiểm thử dùng Google giả lập và thư mục tạm, không dùng tài khoản Google sản xuất:

```sh
python -m unittest discover -s tests -v
```

Windows: `build_windows.ps1`; macOS: `build_mac.command` phải chạy trên Mac tương ứng. Gói Mac dễ mở dùng các tệp launcher trong `packaging/mac`, cùng các module `__init__`, `client`, `common`, `mac_background`, `inventory` dưới `Contents/Resources/payload/drivedrop`.

## Phạm vi xác nhận

Đã kiểm tra bộ kiểm thử tự động, EXE Windows, giao diện web và kết nối Google/HTTPS trên máy chủ. Đã nhận gói bàn giao từ Mac và ảnh xác nhận kết nối máy chủ/Google; **chưa có bằng chứng đầy đủ về upload E2E và chạy nền sau đăng nhập lại**. Thử các bước này trước khi triển khai toàn bộ. [Chi tiết kiểm tra 0.5.0](docs/KET-QUA-KIEM-TRA.md).

## Dữ liệu riêng

Không đưa thư mục `data-boss`, dữ liệu nhân viên, file kích hoạt, OAuth JSON, token hoặc khóa riêng vào GitHub hay gói nhân viên. `.gitignore` loại các nhóm tệp này. Khi chuyển máy chủ Windows, thông tin được bảo vệ bằng DPAPI có thể cần đăng nhập Google lại trên máy/tài khoản Windows mới.

## Bàn giao Mac và đóng gói lại

Đã đối chiếu gói từ Mac nhân viên: 10/10 tệp ứng dụng giống 0.5.0. Xem [kết quả và quy trình đóng gói](docs/MAC-DONG-GOI-VA-BAN-GIAO.md).

```sh
python tools/package_mac.py --output dist/DriveDrop-Mac-0.5.0-r2.zip
```

Gói r2 cập nhật hướng dẫn và quy trình đóng gói, giữ nguyên mã ứng dụng.
