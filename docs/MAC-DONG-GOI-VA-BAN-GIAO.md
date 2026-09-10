# Đóng gói Mac và kết quả bàn giao

## Đối chiếu ngày 10/09/2026

Đã đối chiếu SHA256 của toàn bộ 10 tệp trong ứng dụng Mac bàn giao với ứng dụng phát hành 0.5.0: **10/10 giống nhau**. Bao gồm launcher, bootstrap, entry point, Info.plist, PkgInfo và 5 module Employee. Không có bản vá từ Mac cần nhập vào mã ứng dụng.

Gói bàn giao chỉ chứa ứng dụng và BAN-GIAO.md; không có môi trường Python riêng, danh sách phiên bản cryptography/certifi thực tế hoặc nhật ký E2E để tái hiện toàn bộ máy nhân viên.

Theo BAN-GIAO.md (thông tin do bên Mac cung cấp, chưa đo độc lập): macOS 26.6.2, Apple Silicon arm64, Homebrew Python 3.13.15 ở /opt/homebrew/bin/python3, Tk 9.0. Ứng dụng mở được và truy cập HTTPS được. Báo cáo đó đánh dấu chưa hoàn tất upload E2E và chạy nền/đăng nhập lại.

Ảnh chụp tiếp theo trong phiên hỗ trợ xác nhận đổi máy chủ thành công và “Kết nối máy chủ thành công. Google đã kết nối.” vào 16:59:38. Đây là kiểm tra kết nối, chưa thay thế bằng chứng file đã upload và xác minh trên Mac.

## Những điều cần giữ trong bộ cài

- Hỗ trợ Python chính thức có Tk và Python Homebrew có Tk; launcher hiện đã tìm cả hai. Python phải từ 3.12 trở lên.
- Python hệ thống/Homebrew dùng để tạo môi trường riêng. Bootstrap tự cài **cryptography >=46,<51** và **certifi** vào môi trường đó. Các tên altgraph, future, macholib, six trong bàn giao không chứng minh đây là phụ thuộc runtime của Employee; không thêm chúng chỉ dựa trên danh sách của máy.
- Entry point thiết lập SSL_CERT_FILE từ certifi. Vì thế không bắt mọi máy Homebrew chạy Install Certificates.command; lệnh này dành cho bản Python chính thức có cung cấp nó.
- Chế độ HTTPS của file kích hoạt phải phù hợp địa chỉ: tên miền Cloudflare dùng tls_mode=public_ca; cấu hình pinned cho chứng chỉ LAN không dùng thay thế qua Cloudflare. Cấp file trên web, hoặc bổ sung đúng chế độ khi quản trị viên xuất bằng ứng dụng desktop.
- Phát gói chứa inventory.py để có báo cáo thư mục và chạy nền đúng phiên bản.
- Giữ dữ liệu Employee, Keychain và hàng đợi khi nâng cấp. Không đóng gói chúng vào ZIP.

## Cảnh báo macOS và phụ thuộc

Bàn giao ghi đã xóa thuộc tính quarantine trên máy thử. Đây là thao tác riêng của máy đó, **không phải bản vá mã nguồn hay bằng chứng ứng dụng đã được Apple ký/notarize**. Bộ cài không tự xóa thuộc tính bảo vệ và không tự cài Homebrew/Python ngầm. Với gói chưa ký, người dùng xác nhận mở đúng ứng dụng trong Privacy & Security nếu macOS cho phép. Muốn phân phối thuận tiện hơn cần ký Developer ID và notarize trên môi trường Apple phù hợp.

## Đóng gói lại từ nguồn chính

Tại thư mục gốc repository, dùng Python 3.12+:

```sh
python tools/package_mac.py --output dist/DriveDrop-Mac-0.5.0-r2.zip
```

Script tạo ZIP có quyền thực thi launcher, chuẩn hóa xuống dòng LF và tạo file SHA256. Chỉ lấy danh sách tệp nguồn cố định; không quét/copy cả thư mục máy đang chạy. Gói r2 giữ nguyên mã ứng dụng 0.5.0, cập nhật tài liệu và cách dựng gói; không phải bộ cài độc lập có sẵn Python. Windows có thể tạo ZIP launcher, nhưng không thể qua đó xác nhận chạy macOS hay tạo bản ký/notarize.

## Thử trên Mac tiếp theo

1. Giải nén và đưa app vào Applications. Xác nhận Python có Tk; mở app để chuẩn bị môi trường.
2. Tạo file kích hoạt khi Mac đã sẵn sàng. Mã dùng một lần, hết hạn sau 15 phút.
3. Kiểm tra kết nối; thử một ảnh và một video riêng, giữ bản local.
4. Đối chiếu trạng thái Đã xác minh, nơi lưu Drive và số lượng trên web.
5. Dừng, bật chạy nền; kiểm tra báo cáo vẫn cập nhật sau khi cửa sổ đóng.
6. Đăng xuất/đăng nhập lại macOS, thử tiếp và ghi kết quả. Chưa làm bước nào thì để “chưa thử”.

Lần bàn giao sau nên ghi đường dẫn Python của chính môi trường DriveDrop, phiên bản cryptography/certifi/Tk và kết quả các bước trên. Không gửi Keychain, mã kích hoạt, cấu hình thiết bị, cơ sở dữ liệu, hàng đợi hoặc nội dung ảnh/video.
