# DriveDrop 0.5.0 — Báo cáo nhân viên, máy và thư mục

Trên web drive.vinhglobal.vn, mở Tổng quan nhân viên.

1. Máy Mac mới: Cấp máy nhân viên → gửi riêng file kích hoạt → nhân viên nhập file trong 15 phút, chọn thư mục cần gửi và bật chạy nền.
2. Bấm tên máy → Gán nhân viên / đổi tên máy. Đặt Mac01, Mac02… và mã nhân viên NV001. Các máy của cùng một người dùng cùng mã. Máy chưa gán không được tính là một nhân viên.
3. Bấm Mac01 để mở danh sách thư mục: từng dòng có số ảnh, video, tệp khác, dung lượng và hàng đợi. Thư mục rỗng vẫn xuất hiện. Thư mục con có dòng riêng, không cộng trùng vào thư mục cha.
4. Xem tổng nhân viên, máy kết nối, máy cần kiểm tra, tệp xác minh hôm nay và biểu đồ 7 ngày. Tìm theo người/máy/thư mục, lọc máy có lỗi, hoặc Xuất CSV để mở bằng Excel.

## Cập nhật Mac đang dùng bản cũ (một lần)

- Trong bản cũ, bấm Dừng, chờ lượt mạng hiện tại kết thúc; nếu đang chạy nền, bấm Tắt chạy nền. Đóng cửa sổ DriveDrop.
- Giải nén DriveDrop-Mac-0.5.0.zip. Mở DriveDrop Employee.app mới. Không xóa thư mục dữ liệu, hàng đợi hoặc Keychain; không cần kích hoạt lại nếu dùng cùng tài khoản macOS và đường dẫn dữ liệu cũ.
- Kiểm tra thư mục đã chọn. Bấm Bật chạy nền tự động trong bản mới để thay mã chương trình của LaunchAgent bằng 0.5.0.
- Đợi khoảng 1 phút, xem web: phiên bản 0.5.0 và báo cáo thư mục xuất hiện. Nếu dùng gói mã nguồn, chạy Install-Employee-Mac.command rồi mở lại và bật chạy nền.
- Gói Mac hiện là launcher Python chưa ký/notarize, cần Python 3.12+ có Tk. Chưa có máy Mac thật để xác nhận cuối cùng; thử một máy trước khi phát cho toàn bộ nhân viên.

## Cách đọc số liệu

- Báo cáo chỉ quét thư mục nhân viên đã chọn, không quét toàn bộ máy. Chỉ đọc tên/kích thước tệp, không đọc nội dung ảnh/video để lập báo cáo.
- Trạng thái gửi mỗi 15 giây; quét khoảng 60 giây/lần. Quá 60 giây không gửi trạng thái là mất kết nối; bản quét quá 180 giây hoặc máy mất kết nối được đánh dấu dữ liệu cũ.
- Mỗi lần quét giới hạn 300 thư mục gồm thư mục gốc, 100.000 mục và khoảng 2 giây. Nếu chạm giới hạn hoặc không đọc được đường dẫn, hiện quét chưa đầy đủ. Tệp/thư mục ẩn và liên kết được bỏ qua.
- Số ảnh/video trên máy bao gồm tệp đã upload nhưng vẫn giữ bản local và tệp trong hàng đợi riêng của DriveDrop. Đây không phải số tệp mới tạo, cũng không phải năng suất hoặc giờ làm việc.
- Hàng đợi chỉ gồm tệp đã được DriveDrop nhận xử lý; tệp mới chưa ổn định có thể chưa vào hàng đợi.
- Upload hôm nay và 7 ngày dùng thời điểm máy chủ xác minh, theo giờ Việt Nam. Bản cũ không lưu thời điểm này nên lịch sử cũ chỉ giữ trong tổng đã xác minh, không suy đoán lại ngày. Không cộng máy đã khóa vào tổng hoạt động.
- Máy 0.4.0 vẫn gửi tiến độ cũ, nhưng không có số liệu thư mục cho tới khi cập nhật. Chưa có cơ chế tự tải và cài bản mới trên Mac.

## Máy chủ

Bản 0.5.0 giữ các cổng hiện có, tài khoản web, kết nối Google, chứng chỉ và dữ liệu data-boss. Không cần tạo lại Tunnel Cloudflare. Máy chủ phải đang bật và DriveDrop đang chạy.
