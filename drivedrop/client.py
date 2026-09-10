"""Employee app: no Google OAuth token ever enters this process.

Producer contract: close files, then atomically move them into the watch folder.
The stability timer is a convenience, not proof that a producer has closed a file.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

from .common import ApiError, SecretStore, atomic_json, load_json, pinned_request, secure_directory


MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".dng", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts"}
CHUNK_SIZE = 8 * 1024 * 1024
PENDING = ".drivedrop-pending"
REPARSE_POINT = 0x400


def _validate_source_folders(value):
    if not isinstance(value, list) or len(value) > 32:
        raise ClientError("Thư mục ảnh/video không được lồng quá 32 cấp tính từ thư mục đã chọn.")
    total = max(0, len(value) - 1)  # Include separators used by the Drive source path.
    for component in value:
        if (not isinstance(component, str) or not component.strip() or len(component) > 200 or
                component in (".", "..") or any(char in component for char in "/\\") or
                not component.isprintable()):
            raise ClientError("Tên thư mục nguồn không hợp lệ; tối đa 200 ký tự, không chứa đường dẫn hoặc ký tự điều khiển.")
        try:
            total += len(component.encode("utf-8"))
        except UnicodeError:
            raise ClientError("Tên thư mục nguồn không phải Unicode hợp lệ.") from None
    if total > 6000:
        raise ClientError("Tổng tên các thư mục nguồn vượt giới hạn 6000 byte UTF-8.")
    return list(value)


def _source_folders(root: Path, source: Path):
    try:
        relative_parent = source.parent.relative_to(root)
    except ValueError:
        raise ClientError("File nguồn nằm ngoài thư mục đã chọn.") from None
    return _validate_source_folders([root.name, *relative_parent.parts])


def _hidden(name, info):
    return (name.startswith(".") or bool(getattr(info, "st_file_attributes", 0) & 0x2) or
            bool(getattr(info, "st_flags", 0) & getattr(stat, "UF_HIDDEN", 0)))


class ClientError(Exception):
    """Messages of this class must never contain session URLs or credentials."""


class FileChanged(ClientError):
    pass


class SessionExpired(ClientError):
    pass


class Stopped(ClientError):
    pass


def safe_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc)
    if isinstance(exc, ApiError):
        status = getattr(exc, "status", 0)
        return {401: "Thiết bị chưa được xác thực hoặc đã bị khóa.", 403: "Máy chủ từ chối quyền truy cập.", 409: "File chưa được Drive xác minh; giữ nguyên bản local.", 429: "Đã chạm hạn mức; sẽ thử lại sau."}.get(status, f"Lỗi kết nối máy chủ (mã {status}); giữ nguyên file.")
    if isinstance(exc, (OSError, ssl.SSLError, http.client.HTTPException)):
        return "Lỗi mạng hoặc truy cập file; sẽ thử lại, giữ nguyên bản local."
    return "Không hoàn tất thao tác; giữ nguyên file và kiểm tra cấu hình."


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def _clean_path(path: Path, *, must_exist: bool = True) -> Path:
    """Reject symlinks/reparse points in every existing path component."""
    path = Path(os.path.abspath(path))
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if must_exist or part != path:
                raise ClientError("Đường dẫn không tồn tại.")
            continue
        if _is_link(info):
            raise ClientError("Không dùng liên kết tượng trưng hoặc junction cho thư mục upload.")
    return path


def _identity(info: os.stat_result) -> dict:
    return {"size": info.st_size, "mtime": info.st_mtime_ns, "ctime": info.st_ctime_ns, "dev": info.st_dev, "ino": info.st_ino}


def _snapshot(path: Path) -> dict:
    _clean_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ClientError("Chỉ hỗ trợ file thông thường.")
    if sys.platform == "win32":
        # CPython's Windows path stat may report creation time as st_ctime,
        # while fstat reports NTFS change time. Use handle metadata consistently.
        with open(path, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (info.st_ino, info.st_dev, info.st_size, info.st_mtime_ns) != (opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns):
                raise FileChanged("File đổi trong lúc kiểm tra; giữ nguyên.")
            info = opened
    return _identity(info)


def _console_log(message):
    if sys.stdout is None:
        return
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(str(message).encode(encoding, errors="replace").decode(encoding))


def _open_regular(path: Path):
    _clean_path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or _is_link(path.lstat()) or _identity(info) != _snapshot(path):
            raise FileChanged("File đã đổi trong lúc mở; giữ nguyên để kiểm tra.")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _hash_file(path: Path, stop_event=None) -> tuple[dict, str, str]:
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with _open_regular(path) as stream:
        before = _identity(os.fstat(stream.fileno()))
        while True:
            if stop_event and stop_event.is_set():
                raise Stopped("Đã dừng; file sẽ được tiếp tục lần sau.")
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            sha.update(block)
            md5.update(block)
        if before != _identity(os.fstat(stream.fileno())) or before != _snapshot(path):
            raise FileChanged("File đang thay đổi; không upload hoặc xóa.")
    return before, sha.hexdigest(), md5.hexdigest()


def _delete_verified_windows(path: Path, job, stop_event, record_intent):
    """Hash and delete the same NTFS handle, denying concurrent writes/replaces."""
    import msvcrt
    from ctypes import wintypes
    _clean_path(path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    # GENERIC_READ | DELETE; FILE_SHARE_READ only; OPEN_EXISTING;
    # OPEN_REPARSE_POINT ensures a last-component symlink cannot be followed.
    handle = kernel.CreateFileW(str(path), 0x80010000, 1, None, 3, 0x08200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ClientError("File còn mở hoặc không khóa được để xóa an toàn; sẽ thử lại.")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:  # Closing the descriptor owns CloseHandle.
        before = _identity(os.fstat(stream.fileno()))
        expected = json.loads(job["identity"])
        if before != expected or not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise FileChanged("File đổi trước khi xóa; giữ nguyên để kiểm tra.")
        sha = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        while True:
            if stop_event and stop_event.is_set():
                raise Stopped("Đã dừng; chưa xóa file.")
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            sha.update(block)
            md5.update(block)
        _clean_path(path)
        current = path.lstat()
        if (_is_link(current) or _identity(os.fstat(stream.fileno())) != before or
                (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) !=
                (before["dev"], before["ino"], before["size"], before["mtime"]) or
                sha.hexdigest() != job["sha256"] or md5.hexdigest() != job["md5"]):
            raise FileChanged("File đổi khi đối chiếu trước xóa; giữ nguyên bản local.")
        if stop_event and stop_event.is_set():
            raise Stopped("Đã dừng; chưa xóa file.")
        record_intent()
        disposition = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile BOOLEAN
        if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ClientError("Windows chưa cho phép xóa file đã xác minh; giữ để thử lại.")


def _rename_no_replace(source: Path, target: Path) -> None:
    """Atomic rename without replacing a producer's new file."""
    _clean_path(source)
    _clean_path(target.parent)
    if target.exists() or target.is_symlink():
        raise FileExistsError(str(target))
    if sys.platform == "win32":
        os.rename(source, target)  # Windows rename fails when destination exists.
    elif sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(os.fsencode(source), os.fsencode(target), 0x4) != 0:  # RENAME_EXCL
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise ClientError("Hệ điều hành không hỗ trợ đổi tên an toàn.")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
    else:
        raise ClientError("Chỉ hỗ trợ Windows và macOS.")


class GoogleUploadTransport:
    """TLS to the allowlisted Google host; never sends broker/Google bearer tokens."""

    def put(self, url: str, data: bytes, content_range: str) -> tuple[int, dict]:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != "www.googleapis.com" or
                parts.port not in (None, 443) or parts.username or parts.password or
                parts.fragment or not parts.path.startswith("/upload/drive/v3/files")):
            raise ClientError("Máy chủ trả về địa chỉ upload không hợp lệ.")
        connection = http.client.HTTPSConnection("www.googleapis.com", 443, timeout=60, context=ssl.create_default_context())
        path = parts.path + ("?" + parts.query if parts.query else "")
        try:
            connection.request("PUT", path, body=data, headers={"Content-Length": str(len(data)), "Content-Range": content_range, "Content-Type": "application/octet-stream"})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            # Responses can contain metadata. Never print or save their bodies.
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ClientError("Phản hồi Google vượt giới hạn an toàn.")
            return response.status, headers
        finally:
            connection.close()


def _next_offset(status: int, headers: dict, size: int) -> int:
    if status in (200, 201):
        return size
    if status in (404, 410):
        raise SessionExpired("Phiên upload hết hạn; đang kiểm tra lại với máy chủ.")
    if status != 308:
        raise ClientError(f"Google chưa nhận xong dữ liệu (mã {status}); giữ file để tiếp tục.")
    value = next((value for key, value in headers.items() if key.lower() == "range"), None)
    if value is None:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", str(value).strip())
    if not match:
        raise ClientError("Phản hồi tiến độ upload không hợp lệ; giữ nguyên file.")
    offset = int(match.group(1)) + 1
    if not 0 <= offset <= size:
        raise ClientError("Tiến độ upload ngoài dung lượng file; giữ nguyên file.")
    return offset


class Client:
    def __init__(self, data_dir, store=None, request=None, upload_transport=None, log=None):
        self.data_dir = Path(data_dir).absolute()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _clean_path(self.data_dir)
        secure_directory(self.data_dir)
        self.config_path = self.data_dir / "client.json"
        self.db_path = self.data_dir / "journal.sqlite3"
        self.store = store if store is not None else SecretStore(self.data_dir, "DriveDropClient")
        self.request = request if request is not None else pinned_request
        self.upload_transport = upload_transport if upload_transport is not None else GoogleUploadTransport()
        self.log = log if log is not None else _console_log
        self.config = load_json(self.config_path, {}) or {}
        self.lock = threading.Lock()
        self.activity_lock = threading.Lock()
        self.inventory_lock = threading.Lock()
        self.inventory_cache = None
        self.inventory_next = 0
        self.inventory_root = None
        self.activity = {"state": "starting", "file": "", "destination": "", "error": "", "sent": 0, "size": 0}
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                  request_id TEXT PRIMARY KEY, root TEXT NOT NULL, name TEXT NOT NULL,
                  source_path TEXT NOT NULL, claimed_path TEXT NOT NULL,
                  status TEXT NOT NULL, identity TEXT NOT NULL,
                  size INTEGER, sha256 TEXT, md5 TEXT, upload_id TEXT,
                  final_path TEXT, final_identity TEXT, error TEXT, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                  path TEXT PRIMARY KEY, identity TEXT NOT NULL, first_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_context (
                  request_id TEXT PRIMARY KEY, source_folders TEXT NOT NULL
                );
            """)

    @contextmanager
    def _db(self):
        _clean_path(self.db_path, must_exist=False)
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        """Connections are scoped to each transaction; retained for embedding/tests."""
        return None

    def report_activity(self, **values):
        with self.activity_lock:
            self.activity.update(values)

    def heartbeat(self):
        from . import __version__
        with self.activity_lock:
            payload = dict(self.activity, version=__version__)
        with self._db() as db:
            rows = db.execute("SELECT status,count(*) total FROM jobs WHERE root=? GROUP BY status",
                              (self.config.get("watch_folder", ""),)).fetchall()
        counts = {r["status"]: r["total"] for r in rows}
        payload.update(queued=sum(counts.get(k,0) for k in ("claiming","claimed","uploading","verified","finalizing_delete","finalizing_preserve")),
                       verified=sum(counts.get(k,0) for k in ("deleted","preserved")),
                       failed=sum(counts.get(k,0) for k in ("changed","missing")))
        # Metadata scanning is throttled and never runs in the upload thread.
        if self.inventory_lock.acquire(blocking=False):
            try:
                watch = self.config.get('watch_folder')
                if watch != self.inventory_root:
                    self.inventory_cache = None
                    self.inventory_next = 0
                    self.inventory_root = watch
                if watch and time.monotonic() >= self.inventory_next:
                    from .inventory import scan_inventory, ACTIVE
                    self.inventory_next = time.monotonic() + 60
                    try:
                        root = self._root()
                        with self._db() as db:
                            jobs = db.execute("SELECT source_path,claimed_path,name,status FROM jobs WHERE root=? AND status NOT IN ('deleted','preserved') LIMIT 100001", (str(root),)).fetchall()
                        self.inventory_cache = scan_inventory(root, jobs[:100000])
                        if len(jobs)>100000:
                            self.inventory_cache['complete'] = False
                    except (OSError, ClientError):
                        pass  # Retain old snapshot with its original timestamp; never report a false zero.
                if self.inventory_cache:
                    payload['inventory'] = self.inventory_cache
            finally:
                self.inventory_lock.release()
        try:
            return self._api("POST", "/heartbeat", payload)
        except ApiError as exc:
            if 'inventory' not in payload or exc.status not in (400,413):
                raise
            # Older servers still receive basic progress during a staged upgrade.
            payload.pop('inventory')
            return self._api("POST", "/heartbeat", payload)

    @contextmanager
    def _process_lock(self):
        lock_path = self.data_dir / "worker.lock"
        _clean_path(lock_path, must_exist=False)
        with open(lock_path, "a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ClientError("Một bản DriveDrop khác đang dùng hàng đợi này.") from None
            try:
                yield
            finally:
                handle.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _update(self, request_id: str, **values):
        allowed = {"status", "identity", "size", "sha256", "md5", "upload_id", "final_path", "final_identity", "error"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid journal fields")
        with self._db() as db:
            db.execute("UPDATE jobs SET " + ", ".join(key + "=?" for key in values) + " WHERE request_id=?", (*values.values(), request_id))

    def _save_config(self):
        atomic_json(self.config_path, self.config)

    @staticmethod
    def _tls_options(config):
        mode = config.get("tls_mode", "pinned")
        if mode not in ("pinned", "public_ca"):
            raise ClientError("Chế độ xác minh HTTPS không hợp lệ; cần file kích hoạt mới.")
        return {"tls_mode": mode} if mode == "public_ca" else {}

    def enroll(self, config: dict, name: str = "Employee") -> dict:
        if self.config.get("device_id"):
            raise ClientError("Máy này đã kích hoạt; dùng thư mục dữ liệu mới nếu cần đổi thiết bị.")
        if not isinstance(config, dict) or not all(isinstance(config.get(key), str) and config[key] for key in ("server_url", "certificate_sha256", "code")):
            raise ClientError("File kích hoạt không hợp lệ.")
        result = self.request(config["server_url"], config["certificate_sha256"], "POST", "/enroll", {"code": config["code"], "name": name[:80]}, **self._tls_options(config))
        if not isinstance(result.get("device_id"), str) or not isinstance(result.get("secret"), str):
            raise ClientError("Máy chủ không trả về thông tin kích hoạt hợp lệ.")
        self.store.set("device_secret", result["secret"])
        self.config.update({"server_url": config["server_url"], "certificate_sha256": config["certificate_sha256"], "device_id": result["device_id"], "tls_mode": config.get("tls_mode", "pinned"), "delete_after_verify": False, "poll_seconds": 30, "stable_seconds": 30})
        self._save_config()
        return {"device_id": result["device_id"], "daily_limit_bytes": result.get("daily_limit_bytes")}

    def migrate_server(self, config, name="Employee"):
        """Switch only an idle, settled queue; retain journal and old protected key."""
        if not self.config.get("device_id"):
            return self.enroll(config, name)
        if not self.lock.acquire(blocking=False):
            raise ClientError("Hãy dừng upload trước khi đổi máy chủ.")
        try:
            with self._process_lock():
                if sys.platform == "darwin":
                    from . import mac_background
                    if mac_background.active():
                        raise ClientError("Bấm Tắt chạy nền trước khi đổi máy chủ.")
                with self._db() as db:
                    if db.execute("SELECT count(*) FROM jobs WHERE status NOT IN ('deleted','preserved')").fetchone()[0]:
                        raise ClientError("Còn file chưa xử lý xong ở máy chủ cũ. Hoàn tất hàng đợi trước khi đổi.")
                    roots = {r[0] for r in db.execute("SELECT DISTINCT root FROM jobs")}
                if self.config.get("watch_folder"):
                    roots.add(self.config["watch_folder"])
                for root in roots:
                    pending = Path(root) / PENDING
                    if pending.exists() and any(pending.iterdir()):
                        raise ClientError("Còn dữ liệu trong .drivedrop-pending; cần kiểm tra trên máy chủ cũ trước.")
                if not isinstance(config, dict) or not all(isinstance(config.get(k), str) and config[k] for k in ("server_url", "certificate_sha256", "code")):
                    raise ClientError("File kích hoạt không hợp lệ.")
                options = self._tls_options(config)
                # Keep a recovery reference before consuming the new enrollment.
                atomic_json(self.data_dir / ("server-history-" + uuid.uuid4().hex + ".json"), self.config)
                result = self.request(config["server_url"], config["certificate_sha256"], "POST", "/enroll",
                                      {"code": config["code"], "name": name[:80]}, **options)
                if not isinstance(result.get("device_id"), str) or not isinstance(result.get("secret"), str):
                    raise ClientError("Máy chủ không trả về thông tin kích hoạt hợp lệ.")
                secret_key = "device-secret-" + uuid.uuid4().hex
                self.store.set(secret_key, result["secret"])
                updated = dict(self.config, server_url=config["server_url"], certificate_sha256=config["certificate_sha256"],
                               tls_mode=config.get("tls_mode", "pinned"), device_id=result["device_id"], device_secret_key=secret_key,
                               delete_after_verify=False)
                atomic_json(self.config_path, updated)
                self.config = updated
                return {"device_id": result["device_id"]}
        finally:
            self.lock.release()

    def configure(self, watch_folder, delete_after_verify=False, poll_seconds=30, stable_seconds=30):
        folder = _clean_path(Path(watch_folder).expanduser())
        if not folder.is_dir():
            raise ClientError("Thư mục theo dõi không hợp lệ.")
        _validate_source_folders([folder.name])
        if not 1 <= float(poll_seconds) <= 3600 or not 0 <= float(stable_seconds) <= 86400:
            raise ClientError("Chu kỳ quét phải từ 1–3600 giây; thời gian ổn định từ 0–86400 giây.")
        # Never claim credentials, journal or application binaries as user media.
        if folder == self.data_dir or folder in self.data_dir.parents or self.data_dir in folder.parents:
            raise ClientError("Chọn thư mục ảnh/video riêng, nằm ngoài thư mục dữ liệu ứng dụng.")
        self.config.update({"watch_folder": str(folder), "delete_after_verify": bool(delete_after_verify), "poll_seconds": float(poll_seconds), "stable_seconds": float(stable_seconds)})
        self._save_config()

    def _api(self, method, path, payload=None):
        if not self.config.get("device_id"):
            raise ClientError("Hãy nhập file kích hoạt do sếp cấp trước.")
        secret = self.store.get(self.config.get("device_secret_key", "device_secret"))
        if not secret:
            raise ClientError("Không mở được khóa thiết bị trong kho bảo mật hệ điều hành.")
        return self.request(self.config["server_url"], self.config["certificate_sha256"], method, path, payload, device_id=self.config["device_id"], secret=secret, **self._tls_options(self.config))

    def health(self):
        if not self.config.get("server_url"):
            raise ClientError("Hãy nhập file kích hoạt trước.")
        result = self.request(self.config["server_url"], self.config["certificate_sha256"], "GET", "/health", **self._tls_options(self.config))
        self._require_album_protocol(result)
        return result

    @staticmethod
    def _require_album_protocol(health):
        if not isinstance(health, dict) or health.get("album_routing") is not True:
            raise ClientError("Máy chủ Boss chưa hỗ trợ phân ảnh theo thư mục bài. Cần cập nhật Boss trước; chưa nhận, upload hoặc xóa file.")

    def _root(self):
        if not self.config.get("watch_folder"):
            raise ClientError("Chưa chọn thư mục ảnh/video.")
        root = _clean_path(Path(self.config["watch_folder"]))
        if not root.is_dir():
            raise ClientError("Thư mục theo dõi không còn tồn tại.")
        return root

    def _job_path(self, job) -> Path:
        root = _clean_path(Path(job["root"]))
        path = Path(job["claimed_path"])
        expected = root / PENDING / job["request_id"] / job["name"]
        if path != expected or Path(job["name"]).name != job["name"] or root != self._root():
            raise ClientError("Đường dẫn hàng đợi không khớp thư mục đã cấu hình; giữ nguyên file.")
        _clean_path(path)
        return path

    def _source_path(self, job, *, must_exist=False):
        root = self._root()
        source = Path(job["source_path"])
        if (Path(job["root"]) != root or not source.is_absolute() or source.name != job["name"] or
                ".." in source.parts or source == root):
            raise ClientError("Đường dẫn nguồn trong hàng đợi không hợp lệ.")
        try:
            relative = source.relative_to(root)
        except ValueError:
            raise ClientError("Đường dẫn nguồn trong hàng đợi nằm ngoài thư mục đã chọn.") from None
        if PENDING in relative.parts[:-1]:
            raise ClientError("Đường dẫn nguồn không được trỏ vào hàng đợi nội bộ.")
        _clean_path(source, must_exist=must_exist)
        if not source.parent.is_dir():
            raise ClientError("Thư mục chứa file nguồn không còn hợp lệ.")
        return source

    def _iter_media(self, root):
        """Walk only ordinary descendant directories; never follow links/reparse points."""
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                _clean_path(directory)
                if not directory.is_relative_to(root) or not directory.is_dir():
                    raise ClientError("Thư mục quét nằm ngoài đường dẫn đã chọn.")
                entries = sorted(directory.iterdir())
            except (OSError, ClientError):
                self.log("Bỏ qua một thư mục không truy cập được hoặc không an toàn.")
                continue
            subdirectories = []
            for entry in entries:
                try:
                    info = entry.lstat()
                    if _is_link(info) or _hidden(entry.name, info):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        # A directory's own name is included in descendant file context.
                        _validate_source_folders([root.name, *entry.relative_to(root).parts])
                        subdirectories.append(entry)
                    elif stat.S_ISREG(info.st_mode) and entry.suffix.lower() in MEDIA_EXTENSIONS:
                        yield entry, _source_folders(root, entry)
                except (OSError, ClientError):
                    self.log("Bỏ qua một đường dẫn nguồn không hợp lệ hoặc quá sâu.")
            stack.extend(reversed(subdirectories))

    def _observe_and_claim(self, root: Path) -> int:
        claimed = 0
        stable = float(self.config.get("stable_seconds", 30))
        for source, source_folders in self._iter_media(root):
            request_id = None
            try:
                current = _snapshot(source)
                if current["size"] == 0:
                    continue
                identity = json.dumps(current, sort_keys=True)
                with self._db() as db:
                    if db.execute("SELECT 1 FROM jobs WHERE final_path=? AND final_identity=? AND status='preserved'", (str(source), identity)).fetchone():
                        continue
                    if db.execute("SELECT 1 FROM jobs WHERE source_path=? AND status IN ('claiming','claimed','uploading','verified','finalizing_delete','finalizing_preserve','changed','missing')", (str(source),)).fetchone():
                        continue
                    old = db.execute("SELECT * FROM observations WHERE path=?", (str(source),)).fetchone()
                    now = time.time()
                    if old is None or old["identity"] != identity or now < old["first_seen"]:
                        db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?)", (str(source), identity, now))
                        if stable > 0:
                            continue
                    elif now - old["first_seen"] < stable:
                        continue
                pending = root / PENDING
                pending.mkdir(exist_ok=True)
                _clean_path(pending)
                request_id = str(uuid.uuid4())
                directory = pending / request_id
                directory.mkdir(exist_ok=False)
                target = directory / source.name
                with self._db() as db:
                    db.execute("INSERT INTO jobs(request_id,root,name,source_path,claimed_path,status,identity,created) VALUES (?,?,?,?,?,'claiming',?,?)", (request_id, str(root), source.name, str(source), str(target), identity, time.time()))
                    db.execute("INSERT INTO job_context(request_id,source_folders) VALUES (?,?)", (request_id, json.dumps(source_folders, ensure_ascii=False)))
                if _snapshot(source) != current:
                    raise FileChanged("File đổi trước khi nhận vào hàng đợi; giữ nguyên.")
                _rename_no_replace(source, target)
                self._update(request_id, status="claimed", identity=json.dumps(_snapshot(target), sort_keys=True))
                claimed += 1
            except FileChanged:
                # This claim intent cannot safely be retried against different bytes.
                if request_id is not None:
                    self._update(request_id, status="changed", error="File changed during claim")
                self.log("File đang thay đổi; giữ nguyên và bỏ qua.")
            except (OSError, ClientError):
                self.log("Bỏ qua một file chưa sẵn sàng hoặc đường dẫn không an toàn.")
        return claimed

    def _recover_claim(self, job):
        target = Path(job["claimed_path"])
        source = self._source_path(job)
        if target.exists():
            self._job_path(job)
            current = _snapshot(target)
            original = json.loads(job["identity"])
            if any(current[key] != original[key] for key in ("size", "mtime", "dev", "ino")):
                raise FileChanged("File đổi trong khi ứng dụng tắt; giữ nguyên để kiểm tra.")
            self._update(job["request_id"], status="claimed", identity=json.dumps(current, sort_keys=True))
        elif source.exists() and _snapshot(source) == json.loads(job["identity"]):
            if target.parent != Path(job["root"]) / PENDING / job["request_id"]:
                raise ClientError("Đường dẫn khôi phục không hợp lệ.")
            _clean_path(target.parent)
            _rename_no_replace(source, target)
            self._update(job["request_id"], status="claimed", identity=json.dumps(_snapshot(target), sort_keys=True))
        else:
            self._update(job["request_id"], status="missing", error="Claim source changed or missing")
            raise FileChanged("File hàng đợi bị chuyển hoặc đổi; cần kiểm tra thủ công.")

    def _upload(self, path, url, size, expected_identity, stop_event=None):
        status, headers = self.upload_transport.put(url, b"", f"bytes */{size}")
        offset = _next_offset(status, headers, size)
        self.report_activity(state="uploading", sent=offset, size=size)
        last_progress = time.monotonic()
        if 0 < offset < size:
            self.log(f"Tiếp tục file từ {offset / size:.0%} ({offset / 1048576:.1f} MiB đã nhận).")
        with _open_regular(path) as stream:
            if _identity(os.fstat(stream.fileno())) != expected_identity:
                raise FileChanged("File đổi trước khi upload; giữ nguyên bản local.")
            while offset < size:
                if stop_event and stop_event.is_set():
                    raise Stopped("Đã dừng; file sẽ được tiếp tục lần sau.")
                if _identity(os.fstat(stream.fileno())) != expected_identity or _snapshot(path) != expected_identity:
                    raise FileChanged("File đổi trong khi upload; giữ nguyên bản local.")
                stream.seek(offset)
                block = stream.read(min(CHUNK_SIZE, size - offset))
                if not block:
                    raise FileChanged("File ngắn hơn dự kiến; giữ nguyên bản local.")
                status, headers = self.upload_transport.put(url, block, f"bytes {offset}-{offset + len(block) - 1}/{size}")
                next_offset = _next_offset(status, headers, size)
                if next_offset <= offset or (status == 308 and next_offset > offset + len(block)):
                    raise ClientError("Tiến độ upload không tăng đúng; giữ file để kiểm tra lại lần sau.")
                offset = next_offset
                self.report_activity(sent=offset)
                if time.monotonic() - last_progress >= 5 or offset == size:
                    self.log(f"Google đã nhận {offset / size:.0%} · {offset / 1048576:.1f}/{size / 1048576:.1f} MiB; chờ xác minh trước khi xóa.")
                    last_progress = time.monotonic()
            if _identity(os.fstat(stream.fileno())) != expected_identity or _snapshot(path) != expected_identity:
                raise FileChanged("File đổi sau khi upload; không xóa bản local.")

    @staticmethod
    def _receipt_matches(receipt, job):
        return (receipt.get("verified") is True and receipt.get("upload_id") == job["upload_id"] and
                isinstance(receipt.get("file_id"), str) and bool(receipt["file_id"]) and
                type(receipt.get("size")) is int and receipt["size"] == job["size"] and
                receipt.get("sha256") == job["sha256"] and receipt.get("md5") == job["md5"])

    def _finish(self, job, receipt, stop_event):
        if not self._receipt_matches(receipt, job):
            raise ClientError("Biên nhận máy chủ không khớp chính xác file; không xóa.")
        path = self._job_path(job)
        source = self._source_path(job)
        if self.config.get("delete_after_verify", False):
            if sys.platform == "win32":
                _delete_verified_windows(path, job, stop_event, lambda: self._update(job["request_id"], status="finalizing_delete"))
            else:
                self._delete_verified_posix(path, job, stop_event)
            self._update(job["request_id"], status="deleted", error=None)
            self.log("Đã upload, đối chiếu dung lượng + SHA-256 + MD5 và xóa bản local.")
        else:
            identity, sha256, md5 = _hash_file(path, stop_event)
            if identity != json.loads(job["identity"]) or sha256 != job["sha256"] or md5 != job["md5"]:
                raise FileChanged("Bản local đã thay đổi; không tự xóa hoặc upload lại.")
            if stop_event and stop_event.is_set():
                raise Stopped("Đã dừng; giữ nguyên file.")
            self._update(job["request_id"], status="finalizing_preserve", final_path=str(source))
            try:
                _rename_no_replace(path, source)
                path = source
            except FileExistsError:
                self.log("Tên file gốc đã có file mới; bản đã upload được giữ trong .drivedrop-pending.")
            self._update(job["request_id"], status="preserved", final_path=str(path), final_identity=json.dumps(_snapshot(path), sort_keys=True), error=None)
            self.log("Đã upload và xác minh; giữ bản local.")
        try:
            Path(job["claimed_path"]).parent.rmdir()
        except OSError:
            pass

    def _delete_verified_posix(self, path, job, stop_event):
        # The directory descriptor prevents an ancestor path swap redirecting unlink.
        # flock is advisory: macOS producers must obey the closed-file contract.
        import fcntl
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            with _open_regular(path) as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise ClientError("File đang bị khóa; giữ để thử lại.") from None
                before = _identity(os.fstat(stream.fileno()))
                if before != json.loads(job["identity"]):
                    raise FileChanged("File đổi trước khi xóa; giữ nguyên.")
                sha = hashlib.sha256()
                md5 = hashlib.md5(usedforsecurity=False)
                while True:
                    if stop_event and stop_event.is_set():
                        raise Stopped("Đã dừng; chưa xóa file.")
                    block = stream.read(CHUNK_SIZE)
                    if not block:
                        break
                    sha.update(block)
                    md5.update(block)
                _clean_path(path)
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (_is_link(current) or _identity(current) != before or
                        _identity(os.fstat(stream.fileno())) != before or
                        sha.hexdigest() != job["sha256"] or md5.hexdigest() != job["md5"]):
                    raise FileChanged("File đổi khi đối chiếu trước xóa; giữ nguyên bản local.")
                if stop_event and stop_event.is_set():
                    raise Stopped("Đã dừng; chưa xóa file.")
                self._update(job["request_id"], status="finalizing_delete")
                os.unlink(path.name, dir_fd=directory)
        finally:
            os.close(directory)

    def _process(self, job, stop_event=None):
        source = self._source_path(job)
        if job["status"] in ("finalizing_delete", "finalizing_preserve") and not Path(job["claimed_path"]).exists():
            if job["status"] == "finalizing_delete":
                self._update(job["request_id"], status="deleted", error=None)
                return
            current, sha256, md5 = _hash_file(source, stop_event)
            original = json.loads(job["identity"])
            if sha256 != job["sha256"] or md5 != job["md5"] or any(current[key] != original[key] for key in ("dev", "ino", "size", "mtime")):
                raise FileChanged("Bản local đổi khi khôi phục; giữ nguyên để kiểm tra.")
            self._update(job["request_id"], status="preserved", final_path=str(source), final_identity=json.dumps(current, sort_keys=True), error=None)
            return
        if job["status"] == "claiming":
            self._recover_claim(job)
            with self._db() as db:
                job = dict(db.execute("SELECT * FROM jobs WHERE request_id=?", (job["request_id"],)).fetchone())
        path = self._job_path(job)
        self.report_activity(state="hashing", file="/".join(Path(job["source_path"]).parts[-4:])[:500],
                             sent=0, size=0, error="")
        current, sha256, md5 = _hash_file(path, stop_event)
        if job["sha256"]:
            if current != json.loads(job["identity"]) or sha256 != job["sha256"] or md5 != job["md5"]:
                raise FileChanged("File hàng đợi đã thay đổi; giữ nguyên, cần kiểm tra thủ công.")
        else:
            job.update(identity=json.dumps(current, sort_keys=True), size=current["size"], sha256=sha256, md5=md5)
            self._update(job["request_id"], identity=job["identity"], size=job["size"], sha256=sha256, md5=md5)
        payload = {"request_id": job["request_id"], "name": job["name"], "size": job["size"], "sha256": sha256, "md5": md5}
        with self._db() as db:
            context = db.execute("SELECT source_folders FROM job_context WHERE request_id=?", (job["request_id"],)).fetchone()
        if context is not None:
            try:
                payload["source_folders"] = _validate_source_folders(json.loads(context["source_folders"]))
            except (ValueError, TypeError):
                raise ClientError("Thông tin thư mục của hàng đợi bị lỗi; giữ nguyên file để kiểm tra.") from None
        self.report_activity(state="hashing", file="/".join(Path(job["source_path"]).parts[-4:])[:500],
                             sent=0, size=job["size"], error="")
        session = self._api("POST", "/uploads", payload)
        self.report_activity(destination=str(session.get("destination", ""))[:1000])
        if not isinstance(session.get("upload_id"), str) or session.get("size") != job["size"] or session.get("sha256") != sha256:
            raise ClientError("Phiên upload không khớp file; giữ nguyên bản local.")
        if job["upload_id"] and job["upload_id"] != session["upload_id"]:
            raise ClientError("Máy chủ trả về mã upload khác; giữ nguyên bản local.")
        job["upload_id"] = session["upload_id"]
        self._update(job["request_id"], upload_id=job["upload_id"], status="uploading", error=None)
        if session.get("state") != "verified":
            try:
                self._upload(path, session["session_url"], job["size"], current, stop_event)
            except SessionExpired:
                session = self._api("POST", "/uploads/restart", {"upload_id": job["upload_id"]})
                if (session.get("upload_id") != job["upload_id"] or session.get("size") != job["size"] or session.get("sha256") != sha256):
                    raise ClientError("Phiên tiếp tục không khớp; giữ nguyên file.")
                if session.get("state") != "verified":
                    self._upload(path, session["session_url"], job["size"], current, stop_event)
        self.report_activity(state="verifying")
        receipt = self._api("POST", "/verify", {"upload_id": job["upload_id"]})
        if not self._receipt_matches(receipt, job):
            raise ClientError("Google chưa xác minh đầy đủ file; không xóa bản local.")
        self._update(job["request_id"], status="verified")
        self._finish(job, receipt, stop_event)

    def run_once(self, stop_event=None) -> dict:
        if not self.lock.acquire(blocking=False):
            raise ClientError("Đang có một lượt upload chạy.")
        try:
            with self._process_lock():
                return self._run_once_locked(stop_event)
        finally:
            self.lock.release()

    def _run_once_locked(self, stop_event):
        stats = {"claimed": 0, "completed": 0, "failed": 0}
        self.report_activity(state="scanning", error="", file="", sent=0, size=0)
        root = self._root()
        health = self._api("GET", "/health")  # Resolve authentication secret before moving files.
        self._require_album_protocol(health)
        if health.get("ok") is not True or health.get("google_connected") is not True:
            raise ClientError("Máy chủ chưa kết nối Google Drive; sếp cần đăng nhập trước.")
        stats["claimed"] = self._observe_and_claim(root)
        with self._db() as db:
            jobs = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE status IN ('claiming','claimed','uploading','verified','finalizing_delete','finalizing_preserve') AND root=? ORDER BY created", (str(root),))]
        for job in jobs:
            if stop_event and stop_event.is_set():
                break
            try:
                self._process(job, stop_event)
                stats["completed"] += 1
            except Stopped:
                break
            except FileChanged as exc:
                self._update(job["request_id"], status="changed", error=safe_error(exc))
                self.report_activity(state="error", error=safe_error(exc)[:300])
                self.log(safe_error(exc))
                stats["failed"] += 1
            except Exception as exc:
                self._update(job["request_id"], error=safe_error(exc))
                self.report_activity(state="error", error=safe_error(exc)[:300])
                self.log(safe_error(exc))
                stats["failed"] += 1
        if not stats["failed"]:
            self.report_activity(state="waiting", file="", sent=0, size=0)
        return stats

    def run(self, stop_event=None):
        stop_event = stop_event if stop_event is not None else threading.Event()
        self.log("Đang theo dõi thư mục. Chỉ đưa vào các file đã ghi xong và đóng.")
        heartbeat_stop = threading.Event()
        def send_status():
            while not heartbeat_stop.is_set():
                try:
                    self.heartbeat()
                except Exception:
                    pass  # Monitoring failures never authorize deletion or block uploads.
                heartbeat_stop.wait(15)
        # Hold the worker lock through the idle wait too, preventing a second GUI/agent.
        with self._process_lock():
            reporter = threading.Thread(target=send_status, daemon=True)
            reporter.start()
            try:
                while not stop_event.is_set():
                    try:
                        with self.lock:
                            self._run_once_locked(stop_event)
                    except Exception as exc:
                        self.report_activity(state="error", error=safe_error(exc)[:300])
                        self.log(safe_error(exc))
                    stop_event.wait(float(self.config.get("poll_seconds", 30)))
            finally:
                self.report_activity(state="stopped")
                heartbeat_stop.set()
                reporter.join(timeout=2)
                try:
                    self.heartbeat()
                except Exception:
                    pass


def default_data_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return base / "data-client"


def gui(data_dir):
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except ImportError:
        raise ClientError("Python chưa có Tkinter. Hãy dùng bản cài Python có Tk hoặc chạy CLI.")
    root = tk.Tk()
    root.title("DriveDrop — Máy nhân viên")
    root.geometry("760x610")
    root.minsize(650, 520)
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="DriveDrop · Upload ảnh và video", font=("Segoe UI", 18, "bold")).pack(anchor="w")
    ttk.Label(frame, text="Dữ liệu đi thẳng từ máy này lên Google Drive.").pack(anchor="w", pady=(4, 14))
    import queue
    messages = queue.Queue()
    client = Client(data_dir, log=messages.put)
    folder = tk.StringVar(value=client.config.get("watch_folder", ""))
    delete = tk.BooleanVar(value=bool(client.config.get("delete_after_verify", False)))
    status = tk.StringVar(value="Đã kích hoạt" if client.config.get("device_id") else "Chưa kích hoạt")
    ttk.Label(frame, textvariable=status).pack(anchor="w", pady=(0, 8))
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x")
    worker = {"thread": None, "stop": threading.Event(), "busy": False}
    migrated = threading.Event()

    def background(action):
        if worker["busy"] or worker["thread"] and worker["thread"].is_alive():
            messages.put("Hãy dừng upload trước khi đổi cấu hình.")
            return
        worker["busy"] = True
        def invoke():
            try:
                action()
            except Exception as exc:
                messages.put(safe_error(exc))
            finally:
                worker["busy"] = False
        threading.Thread(target=invoke, daemon=True).start()

    def enroll():
        filename = filedialog.askopenfilename(title="Chọn file kích hoạt sếp cấp", filetypes=[("JSON", "*.json")])
        if filename:
            def action():
                client.enroll(load_json(Path(filename)), name=os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "Employee")
                messages.put("Kích hoạt thành công. Có thể xóa file mã kích hoạt đã dùng.")
            background(action)

    def migrate():
        if not messagebox.askyesno("Đổi máy chủ", "Chuyển sang máy chủ trong file mới? Hàng đợi phải hoàn tất và chạy nền phải tắt. File local được giữ sau khi chuyển.", parent=root):
            return
        filename = filedialog.askopenfilename(title="File kích hoạt của máy chủ mới", filetypes=[("JSON", "*.json")])
        if filename:
            def action():
                client.migrate_server(load_json(Path(filename)), name=os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "Employee")
                migrated.set()
                messages.put("Đã đổi máy chủ; giữ lịch sử và file local. Hãy kiểm tra kết nối rồi bật lại chạy nền.")
            background(action)

    def health():
        def action():
            result = client.health()
            messages.put("Kết nối máy chủ thành công. " + ("Google đã kết nối." if result.get("google_connected") else "Trạng thái Google xem trong trang quản trị."))
        background(action)

    ttk.Button(buttons, text="1. Nhập file kích hoạt", command=enroll).pack(side="left")
    ttk.Button(buttons, text="Kiểm tra kết nối", command=health).pack(side="left", padx=8)
    ttk.Button(buttons, text="Đổi máy chủ", command=migrate).pack(side="left")
    ttk.Label(frame, text="2. Thư mục ảnh/video (quét cả thư mục con, ảnh phân theo thư mục bài)").pack(anchor="w", pady=(18, 5))
    folder_row = ttk.Frame(frame)
    folder_row.pack(fill="x")
    ttk.Entry(folder_row, textvariable=folder).pack(side="left", fill="x", expand=True)
    def pick_folder():
        chosen = filedialog.askdirectory(title="Chọn thư mục ảnh/video đã đóng")
        if chosen:
            folder.set(chosen)
    ttk.Button(folder_row, text="Chọn…", command=pick_folder).pack(side="left", padx=(8, 0))
    ttk.Checkbutton(frame, text="Tự xóa bản local sau khi máy chủ xác minh đủ dung lượng và checksum", variable=delete).pack(anchor="w", pady=(14, 4))
    ttk.Label(frame, text="Quét cả thư mục con; bỏ thư mục ẩn và liên kết. Ảnh dùng tên thư mục bài để phân kênh.\nMặc định giữ file. Chỉ đưa vào các file đã đóng; không sửa file khi đang upload.", wraplength=700).pack(anchor="w")
    controls = ttk.Frame(frame)
    controls.pack(fill="x", pady=14)
    agent_mode = {"active": False}
    close_for_agent = threading.Event()
    if sys.platform == "darwin":
        from . import mac_background
        agent_mode["active"] = mac_background.active()

    def start():
        if worker["busy"] or worker["thread"] and worker["thread"].is_alive():
            return
        try:
            if agent_mode["active"]:
                raise ClientError("Chạy nền đang bật. Bấm Tắt chạy nền trước khi sửa cấu hình hoặc chạy cửa sổ.")
            client.configure(folder.get(), delete.get())
            if not client.config.get("device_id"):
                raise ClientError("Hãy nhập file kích hoạt trước.")
            worker["stop"] = threading.Event()
            worker["thread"] = threading.Thread(target=client.run, args=(worker["stop"],), daemon=True)
            worker["thread"].start()
        except Exception as exc:
            messages.put(safe_error(exc))

    def stop():
        worker["stop"].set()
        messages.put("Đang dừng sau thao tác mạng hiện tại; dữ liệu có thể tiếp tục lần sau.")

    ttk.Button(controls, text="3. Lưu và bắt đầu", command=start).pack(side="left")
    ttk.Button(controls, text="Dừng", command=stop).pack(side="left", padx=8)
    if sys.platform == "darwin":
        def enable_agent():
            if worker["busy"] or worker["thread"] and worker["thread"].is_alive():
                messages.put("Bấm Dừng và chờ kết thúc lượt hiện tại trước khi bật chạy nền.")
                return
            chosen, deleting = folder.get(), delete.get()
            def action():
                # Stop the old agent before changing shared config or executable files.
                mac_background.stop()
                agent_mode["active"] = False
                client.configure(chosen, deleting, poll_seconds=30)
                mac_background.install(client)
                agent_mode["active"] = True
                close_for_agent.set()
            background(action)
        def disable_agent():
            def action():
                mac_background.stop()
                agent_mode["active"] = False
                messages.put("Đã tắt chạy nền và tự mở khi đăng nhập. Hàng đợi và kích hoạt được giữ nguyên.")
            background(action)
        ttk.Button(controls, text="Bật chạy nền tự động", command=enable_agent).pack(side="left", padx=4)
        ttk.Button(controls, text="Tắt chạy nền", command=disable_agent).pack(side="left", padx=4)
    output = scrolledtext.ScrolledText(frame, height=12, state="disabled", wrap="word")
    output.pack(fill="both", expand=True)
    def pump():
        if close_for_agent.is_set():
            root.destroy()
            return
        while True:
            try:
                message = messages.get_nowait()
            except queue.Empty:
                break
            output.configure(state="normal")
            output.insert("end", time.strftime("%H:%M:%S ") + str(message) + "\n")
            if int(output.index("end-1c").split(".")[0]) > 1000:
                output.delete("1.0", "101.0")
            output.see("end")
            output.configure(state="disabled")
        if migrated.is_set():
            delete.set(False)
            migrated.clear()
        running = worker["thread"] is not None and worker["thread"].is_alive()
        status.set("Chạy nền tự động đang bật" if agent_mode["active"] else ("Đang upload / theo dõi" if running else ("Đã kích hoạt · Đã dừng" if client.config.get("device_id") else "Chưa kích hoạt")))
        root.after(200, pump)
    def close():
        worker["stop"].set()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    pump()
    root.mainloop()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="DriveDrop — app upload trực tiếp cho máy nhân viên")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    commands = parser.add_subparsers(dest="command")
    enroll = commands.add_parser("enroll", help="Kích hoạt máy bằng file JSON sếp cấp")
    enroll.add_argument("config", type=Path)
    enroll.add_argument("--name", default="Employee")
    configure = commands.add_parser("configure", help="Cấu hình thư mục và lựa chọn xóa")
    configure.add_argument("--folder", required=True)
    configure.add_argument("--delete-after-verify", action="store_true")
    configure.add_argument("--poll-seconds", type=float, default=30)
    configure.add_argument("--stable-seconds", type=float, default=30)
    commands.add_parser("health")
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true", help="Quét một lượt; file mới cần đủ thời gian ổn định ở lượt sau")
    commands.add_parser("gui")
    args = parser.parse_args(argv)
    try:
        if args.command in (None, "gui"):
            gui(args.data_dir)
            return 0
        client = Client(args.data_dir)
        if args.command == "enroll":
            client.enroll(load_json(args.config), args.name)
            print("Kích hoạt thành công; khóa thiết bị được lưu trong kho bảo mật hệ điều hành.")
        elif args.command == "configure":
            client.configure(args.folder, args.delete_after_verify, args.poll_seconds, args.stable_seconds)
            print("Đã lưu cấu hình. Chỉ đưa vào thư mục các file đã đóng.")
        elif args.command == "health":
            result = client.health()
            print("Máy chủ: kết nối thành công. Google: " + ("đã kết nối" if result.get("google_connected") else "chưa kết nối"))
        elif args.command == "run":
            if args.once:
                result = client.run_once()
                print(json.dumps(result, ensure_ascii=False))
                return 1 if result["failed"] else 0
            client.run()
        return 0
    except KeyboardInterrupt:
        print("Đã dừng; file chưa xác minh sẽ được giữ lại.")
        return 0
    except Exception as exc:
        print(safe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
