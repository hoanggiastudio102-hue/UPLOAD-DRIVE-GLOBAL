"""User-owned LaunchAgent. Keeps existing state/Keychain identity in place."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from .common import atomic_json, secure_directory

LABEL = "local.drivedrop.employee"

def agent_path():
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")

def active():
    if sys.platform != "darwin":
        return False
    return subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0

def stop():
    if sys.platform != "darwin":
        raise RuntimeError("Chức năng này dành cho macOS.")
    target = f"gui/{os.getuid()}/{LABEL}"
    if active():
        subprocess.run(["/bin/launchctl", "bootout", target], check=True, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path = agent_path()
    if path.is_symlink():
        raise RuntimeError("Tệp chạy nền là liên kết; cần kiểm tra.")
    path.unlink(missing_ok=True)

def install(client):
    if sys.platform != "darwin" or not client.config.get("device_id") or not client.config.get("watch_folder"):
        raise RuntimeError("Cần kích hoạt và chọn thư mục trước khi bật chạy nền trên Mac.")
    # Validate the source folder before changing an existing agent.
    client._root()
    from .client import _clean_path
    _clean_path(agent_path().parent, must_exist=False)
    agent_path().parent.mkdir(exist_ok=True)
    _clean_path(agent_path(), must_exist=False)
    stop()
    worker = Path.home() / "Library" / "Application Support" / "DriveDrop" / "Worker"
    secure_directory(worker)
    package = worker / "drivedrop"
    secure_directory(package)
    for name in ("__init__.py", "client.py", "common.py", "mac_background.py", "inventory.py"):
        target = package / name
        _clean_path(target, must_exist=False)
        temporary = target.with_suffix(".new")
        _clean_path(temporary, must_exist=False)
        temporary.write_bytes((Path(__file__).parent / name).read_bytes())
        os.chmod(temporary, 0o600)
        temporary.replace(target)
    entry = worker / "run_worker.py"
    _clean_path(entry, must_exist=False)
    entry.write_text('''import os, sys, signal, threading, logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import certifi
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["SSL_CERT_FILE"] = certifi.where()
from drivedrop.client import Client
state = Path(sys.argv[1])
logger = logging.getLogger("DriveDrop")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(state / "worker.log", maxBytes=1048576, backupCount=3, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger.addHandler(handler)
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *args: stop.set())
signal.signal(signal.SIGINT, lambda *args: stop.set())
Client(state, log=logger.info).run(stop)
''', encoding="utf-8")
    os.chmod(entry, 0o600)
    document = {
        "Label": LABEL, "ProgramArguments": [sys.executable, "-I", str(entry), str(client.data_dir)],
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
        "ProcessType": "Background", "Nice": 10, "ExitTimeOut": 45,
        "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
    }
    path = agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(document))
    os.chmod(path, 0o600)
    subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    if not active():
        raise RuntimeError("Chưa bật được chạy nền; kiểm tra Login Items trên Mac.")
    return path
