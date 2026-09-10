#!/bin/bash
set -euo pipefail
source_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
install_dir="$HOME/Applications/DriveDrop-Employee"
shortcut="$HOME/Desktop/DriveDrop-Employee.command"

failed() {
  result=$?
  if [ "$result" -ne 0 ]; then
    printf '\nCài đặt chưa hoàn tất. Đọc lỗi ở trên và HUONG-DAN-NHAN-VIEN-MAC.md.\n'
    if [ -t 0 ]; then read -r -p 'Nhấn Enter để đóng.' _answer || true; fi
  fi
}
trap failed EXIT

if [ "$(uname -s)" != Darwin ]; then
  printf 'Bộ cài thử này chỉ chạy trên macOS.\n' >&2
  exit 1
fi

if [ -L "$install_dir" ] || [ -L "$HOME/Applications" ] || [ -L "$install_dir/drivedrop" ] || [ -L "$install_dir/.drivedrop-employee-install" ]; then
  printf 'Thư mục cài đặt là liên kết; hãy nhờ sếp kiểm tra đường dẫn.\n' >&2
  exit 1
fi
if [ -e "$install_dir" ]; then
  if [ ! -f "$install_dir/.drivedrop-employee-install" ]; then
    printf 'Thư mục đích đã tồn tại và không phải bản cài này; không ghi đè.\n' >&2
    exit 1
  fi
  printf 'Đã có bản cài DriveDrop. Chuẩn bị cập nhật chương trình; giữ dữ liệu thiết bị và Keychain.\n'
fi

printf 'Trước khi tiếp tục, hãy bấm Dừng và đóng mọi cửa sổ DriveDrop Employee.\n'
if [ -t 0 ]; then read -r -p 'Đã đóng Employee: nhấn Enter để cài/cập nhật.' _answer; fi
# Validate the whole allowlist before replacing program files. Never copy or
# remove device state, pending media, Keychain entries, or the whole app tree.
for relative in run_client.py requirements.txt Start-Employee-Mac.command drivedrop/__init__.py drivedrop/client.py drivedrop/common.py drivedrop/mac_background.py drivedrop/inventory.py; do
  if [ ! -f "$source_dir/$relative" ] || [ -L "$source_dir/$relative" ] || [ -L "$source_dir/drivedrop" ]; then
    printf 'Gói thiếu file hoặc có liên kết không hợp lệ: %s\n' "$relative" >&2
    exit 1
  fi
  if [ -L "$install_dir/$relative" ] || { [ -e "$install_dir/$relative" ] && [ ! -f "$install_dir/$relative" ]; }; then
    printf 'Đường dẫn chương trình đích không hợp lệ: %s\n' "$relative" >&2
    exit 1
  fi
done
mkdir -p -- "$install_dir/drivedrop"
for relative in run_client.py requirements.txt Start-Employee-Mac.command drivedrop/__init__.py drivedrop/client.py drivedrop/common.py drivedrop/mac_background.py drivedrop/inventory.py; do
  update_file="$(mktemp "$install_dir/.drivedrop-core.XXXXXX")"
  cp -- "$source_dir/$relative" "$update_file"
  case "$relative" in *.command) chmod 755 "$update_file" ;; *) chmod 644 "$update_file" ;; esac
  mv -f -- "$update_file" "$install_dir/$relative"
done
printf 'DriveDrop Employee source installer 0.5.0\n' > "$install_dir/.drivedrop-employee-install"

printf 'Thư mục ứng dụng: %s\n' "$install_dir"
/bin/bash "$install_dir/Start-Employee-Mac.command" --setup-only

if [ -e "$shortcut" ] || [ -L "$shortcut" ]; then
  printf 'Desktop đã có DriveDrop-Employee.command; giữ nguyên file đó.\n'
else
  mkdir -p -- "$HOME/Desktop"
  cat > "$shortcut" <<'LAUNCHER'
#!/bin/bash
set -euo pipefail
exec /bin/bash "$HOME/Applications/DriveDrop-Employee/Start-Employee-Mac.command"
LAUNCHER
  chmod 755 "$shortcut"
fi

printf '\nCÀI ĐẶT XONG. Mở DriveDrop-Employee.command trên Desktop để dùng.\n'
printf 'Đây là launcher chạy mã nguồn Python; chưa phải ứng dụng .app được ký.\n'
printf 'Nhân viên chỉ nhập file kích hoạt do sếp cấp, không đăng nhập Gmail tổng.\n'
if [ -t 0 ]; then read -r -p 'Nhấn Enter để đóng cửa sổ cài đặt.' _answer || true; fi
