#!/bin/bash
set -euo pipefail
app_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
state_dir="$HOME/Library/Application Support/DriveDrop/Employee"
venv_python="$app_dir/.venv/bin/python"

failed() {
  result=$?
  if [ "$result" -ne 0 ]; then
    printf '\nChưa mở được DriveDrop. Đọc dòng lỗi ở trên và HUONG-DAN-NHAN-VIEN-MAC.md.\n'
    printf 'Nếu lỗi SSL: mở Install Certificates.command trong Applications/Python 3.x.\n'
    if [ -t 0 ]; then read -r -p 'Nhấn Enter để đóng.' _answer || true; fi
  fi
}
trap failed EXIT

if [ "$(uname -s)" != Darwin ]; then
  printf 'File này chỉ chạy trên macOS.\n' >&2
  exit 1
fi
cd -- "$app_dir"
if [ ! -f run_client.py ] || [ ! -f requirements.txt ]; then
  printf 'Hãy giải nén toàn bộ gói DriveDrop; không mở riêng file .command.\n' >&2
  exit 1
fi

if [ ! -x "$venv_python" ]; then
  python_bin='/Library/Frameworks/Python.framework/Versions/Current/bin/python3'
  if [ ! -x "$python_bin" ]; then python_bin="$(command -v python3 || true)"; fi
  if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
    printf 'Cần cài Python 3.12 trở lên từ https://www.python.org/downloads/macos/\n' >&2
    exit 1
  fi
  "$python_bin" -c 'import sys, tkinter; assert sys.version_info >= (3, 12), "Cần Python 3.12+ có Tkinter"'
  printf 'Đang tạo môi trường Python riêng cho DriveDrop...\n'
  "$python_bin" -m venv "$app_dir/.venv"
fi

"$venv_python" -c 'import sys, tkinter; assert sys.version_info >= (3, 12), "Cần Python 3.12+ có Tkinter"; tkinter.Tcl()'
if ! "$venv_python" -c 'import cryptography,certifi; assert 46 <= int(cryptography.__version__.split(".")[0]) < 51' >/dev/null 2>&1; then
  printf 'Đang cài thư viện bảo mật từ PyPI; lần đầu cần Internet...\n'
  "$venv_python" -m pip install --disable-pip-version-check --no-input --index-url https://pypi.org/simple -r "$app_dir/requirements.txt"
fi

if [ "${1:-}" = '--setup-only' ]; then
  printf 'Môi trường Python + Tkinter + cryptography đã sẵn sàng.\n'
  exit 0
fi
printf 'Đang mở DriveDrop Employee. Giữ cửa sổ này trong khi ứng dụng chạy.\n'
printf 'Dữ liệu thiết bị: %s\n' "$state_dir"
export SSL_CERT_FILE="$("$venv_python" -c 'import certifi; print(certifi.where())')"
"$venv_python" "$app_dir/run_client.py" --data-dir "$state_dir" gui
