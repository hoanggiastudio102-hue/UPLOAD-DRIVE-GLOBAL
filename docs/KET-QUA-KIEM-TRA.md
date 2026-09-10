# DriveDrop 0.5.0 — Kết quả ngày 10/09/2026

- Đã cài và khởi động bản máy chủ 0.5.0 tại thư mục DriveDrop-Windows/DriveDrop-Boss. Giữ nguyên data-boss, tài khoản web, Google và các cổng Cloudflare.
- HTTPS thật /health trả 200, version 0.5.0, google_connected true. Client dùng public CA gọi /health thành công. /api/dashboard chưa đăng nhập trả 401. Tệp reports.js phục vụ qua tên miền có SHA256 khớp tệp đã kiểm thử.
- EXE chạy runtime-check tại thư mục cài: ok=true, Tk 8.6.12, đủ tài nguyên web. Môi trường sandbox với đường dẫn workspace dài không nạp được Tcl, nên kiểm tra EXE tại đường dẫn cài thật trước khi thay chương trình.
- Bộ kiểm thử tổng: 149 tests, không lỗi, 1 bài symlink bỏ qua do quyền Windows. Thêm 3 bài về heartbeat nhiều thư mục, tương thích máy chủ cũ và file được chuyển vào hàng đợi khi đang quét; đều đạt. Sau sửa đếm trùng, chạy lại 21 bài inventory/monitoring: không lỗi, 1 symlink bỏ qua.
- Giao diện thử với 28 máy / 13 mã nhân viên, một máy khóa và một máy chưa gán: mở Mac01 thấy 10 thư mục, số ảnh/video đúng từng dòng; tìm Bài 04 chỉ còn dòng tương ứng; sửa tên nhân viên được lưu; bộ lọc cần kiểm tra ra đúng 4 máy.
- Bản demo dùng dữ liệu riêng, tên tài khoản hiển thị DEMO — DỮ LIỆU MINH HỌA. Không ghi dữ liệu giả vào hệ thống thật.
- Gói Mac có inventory.py trong ứng dụng, bộ cài mã nguồn và danh sách cài LaunchAgent; các tệp Python biên dịch kiểm tra được và ZIP kiểm tra CRC thành công.

Chưa có phiên kiểm thử macOS thật hoặc thiết bị nhân viên thật. Cần cài thử 0.5.0 trên một Mac, bật chạy nền, kiểm tra số thư mục/ảnh/video và thử upload một tệp. Nhân viên dùng bản cũ cần cập nhật một lần; chưa có tự cập nhật phần mềm trên Mac.

Đã kiểm tra phiên đăng nhập thật sau nâng cấp: trang Tổng quan nhân viên hiển thị 0 máy được cấp quyền và trạng thái chưa có máy nhân viên; lịch sử vẫn giữ đủ 6 tệp SELFTEST đã xác minh. Nhóm kiểm thử đã khóa không được tính vào tổng nhân sự.

Bản chương trình 0.4.0 trước nâng cấp được giữ ở .drivedrop-rollback-program-0.4.0-before-reports để có thể khôi phục. Không dùng bản này để chạy song song.
