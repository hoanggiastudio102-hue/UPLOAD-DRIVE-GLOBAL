"""Isolated entry point inside the Mac launcher bundle; no bundled credentials."""
from pathlib import Path
import os
import sys


def main():
    import certifi
    ca_file = Path(certifi.where())
    if not ca_file.is_file():
        raise RuntimeError("Không tìm thấy chứng chỉ HTTPS của môi trường DriveDrop.")
    os.environ["SSL_CERT_FILE"] = str(ca_file)
    sys.dont_write_bytecode = True
    payload = Path(__file__).resolve().parent / "payload"
    if not (payload / "drivedrop" / "client.py").is_file():
        raise RuntimeError("Gói DriveDrop thiếu mã nguồn Employee.")
    sys.path.insert(0, str(payload))
    from drivedrop.client import main as client_main
    state = Path.home() / "Library" / "Application Support" / "DriveDrop" / "Employee"
    return client_main(["--data-dir", str(state), "gui"])


if __name__ == "__main__":
    raise SystemExit(main())
