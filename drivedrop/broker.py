"""Small HTTPS control plane. Accepts JSON only, never employee media data."""
import hashlib
import hmac
import json
import re
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .common import ApiError, SecretStore, canonical_json, make_certificate, sign_request, secure_directory
from .drive import GoogleDrive
from .channels import ChannelCatalog, validate_source_folders

MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
         ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
         ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif", ".dng": "image/x-adobe-dng",
         ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
         ".webm": "video/webm", ".m4v": "video/x-m4v", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
         ".mpeg": "video/mpeg", ".mpg": "video/mpeg", ".3gp": "video/3gpp"}


class Broker:
    def __init__(self, state_dir, store=None, drive=None, route_channels=True):
        self.state_dir = secure_directory(Path(state_dir))
        self.store = store if store is not None else SecretStore(self.state_dir / "secrets", "DriveDropBoss")
        self.drive = drive if drive is not None else GoogleDrive(self.state_dir, self.store)
        self.channels = ChannelCatalog(self.state_dir / "channels.json")
        # The legacy mode is for transport regression tests; production uses routing.
        self.route_channels = route_channels
        self.cert_path, self.key_path, self.pin = make_certificate(self.state_dir / "tls")
        self.db = sqlite3.connect(self.state_dir / "broker.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS enrollments(code_hash TEXT PRIMARY KEY, expires REAL, name TEXT, used INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, name TEXT, created REAL, revoked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS nonces(device TEXT, nonce TEXT, created REAL, PRIMARY KEY(device,nonce));
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY, device TEXT, request_id TEXT, file_id TEXT,
                name TEXT, size INTEGER, sha256 TEXT, md5 TEXT, parent TEXT, created REAL, state TEXT,
                UNIQUE(device,request_id));
            CREATE TABLE IF NOT EXISTS upload_routes(upload_id TEXT PRIMARY KEY,
                channel_id TEXT, channel_code TEXT, channel_name TEXT, media_kind TEXT,
                remote_name TEXT NOT NULL, destination TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS upload_context(upload_id TEXT PRIMARY KEY,
                source_folders TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS device_status(device TEXT PRIMARY KEY, received REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS device_alerts(device TEXT PRIMARY KEY, token TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
                first_seen REAL NOT NULL,last_seen REAL NOT NULL,acknowledged INTEGER NOT NULL,ack_queued INTEGER NOT NULL,ack_failed INTEGER NOT NULL,queued INTEGER NOT NULL,failed INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS employees(id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS device_profiles(device TEXT PRIMARY KEY, employee_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS upload_verified(upload_id TEXT PRIMARY KEY, verified_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS verified_time ON upload_verified(verified_at);
            CREATE INDEX IF NOT EXISTS upload_device_state ON uploads(device,state);
        """)
        self.db.commit()
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.daily_limit_bytes = 100 * 1024 ** 3
        self.max_file_bytes = 50 * 1024 ** 3
        self.max_pending = 2
        self.max_daily_uploads = 2000
        self.server = None
        self.thread = None
        self.last_error = ""
        self.enroll_attempts = {}

    def create_enrollment(self, server_url, name=""):
        parsed = urlsplit(server_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ApiError("Địa chỉ phải dạng https://IP-hoac-ten-may:48765", 400)
        code = secrets.token_urlsafe(32)
        with self.lock:
            self.db.execute("INSERT INTO enrollments(code_hash,expires,name) VALUES(?,?,?)",
                (hashlib.sha256(code.encode()).hexdigest(), time.time() + 900, str(name).strip()[:60]))
            self.db.commit()
        return {"server_url": server_url.rstrip("/"), "certificate_sha256": self.pin, "code": code}

    def list_devices(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM devices ORDER BY created DESC")]

    def list_uploads(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("""SELECT u.id,u.device,u.name,u.size,u.state,u.created,
                r.channel_code,r.channel_name,r.media_kind,r.destination
                FROM uploads u LEFT JOIN upload_routes r ON r.upload_id=u.id
                ORDER BY u.created DESC LIMIT 100""")]

    def upload_page(self, payload):
        page, count = payload.get('page', 1), payload.get('page_size', 25)
        query, snapshot = payload.get('query', ''), payload.get('snapshot')
        if (type(page) is not int or not 1 <= page <= 1000000000
                or type(count) is not int or count not in (25, 50, 100)
                or not isinstance(query, str) or len(query) > 200
                or snapshot is not None and (type(snapshot) is not int or not 0 <= snapshot <= 9223372036854775807)):
            raise ApiError('Tham số trang lịch sử không hợp lệ.', 400)
        filters = {key: payload.get(key, '') for key in ('device', 'employee', 'media', 'state', 'date_from', 'date_to')}
        if any(not isinstance(v, str) or len(v) > 100 for v in filters.values()) or filters['media'] not in ('', 'ANH', 'VIDEO') or filters['state'] not in ('', 'issued', 'verified'):
            raise ApiError('Bộ lọc lịch sử không hợp lệ.', 400)
        from datetime import datetime, timedelta, timezone
        bounds = {}
        for key in ('date_from', 'date_to'):
            if filters[key]:
                try:
                    bounds[key] = datetime.strptime(filters[key], '%Y-%m-%d').replace(tzinfo=timezone(timedelta(hours=7)))
                except ValueError:
                    raise ApiError('Ngày lọc không hợp lệ.', 400)
        if len(bounds) == 2 and bounds['date_from'] > bounds['date_to']:
            raise ApiError('Ngày bắt đầu phải trước hoặc bằng ngày kết thúc.', 400)
        with self.lock:
            self.db.create_function('history_fold', 1, lambda value: str(value or '').casefold())
            if snapshot is None:
                snapshot = self.db.execute('SELECT coalesce(max(rowid),0) FROM uploads').fetchone()[0]
            joins = ''' FROM uploads u LEFT JOIN upload_routes r ON r.upload_id=u.id
                LEFT JOIN devices d ON d.id=u.device'''
            where = ' WHERE u.rowid <= ?'
            parameters = [snapshot]
            if query.strip():
                where += " AND instr(history_fold(coalesce(u.name,'') || ' ' || coalesce(d.name,'') || ' ' || coalesce(r.destination,'') || ' ' || u.device || ' ' || u.state),?) > 0"
                parameters.append(query.strip().casefold())
            for key, column in (('device', 'u.device'), ('media', 'r.media_kind'), ('state', 'u.state')):
                if filters[key]:
                    where += ' AND ' + column + '=?'
                    parameters.append(filters[key])
            if filters['employee']:
                where += ' AND EXISTS(SELECT 1 FROM device_profiles p WHERE p.device=u.device AND p.employee_id=?)'
                parameters.append(filters['employee'])
            if 'date_from' in bounds:
                where += ' AND u.created>=?'
                parameters.append(bounds['date_from'].timestamp())
            if 'date_to' in bounds:
                where += ' AND u.created<?'
                parameters.append((bounds['date_to']+timedelta(days=1)).timestamp())
            total = self.db.execute('SELECT count(*)' + joins + where, parameters).fetchone()[0]
            pages = max(1, (total + count - 1) // count)
            page = min(page, pages)
            rows = self.db.execute('''SELECT u.id,u.device,u.name,u.size,u.state,u.created,
                d.name AS device_name,r.channel_code,r.channel_name,r.media_kind,r.destination'''
                + joins + where + ' ORDER BY u.created DESC,u.rowid DESC LIMIT ? OFFSET ?',
                parameters + [count, (page - 1) * count]).fetchall()
            return dict(items=[dict(row) for row in rows], total=total, page=page,
                        page_size=count, pages=pages, snapshot=snapshot)

    def sync_channel_folders(self, log=lambda message: None):
        """Prepare the folders on Drive. Catalog edits themselves remain local."""
        channels = [row for row in self.channels.list_channels() if row["enabled"]]
        self.drive.ensure_unclassified_folder()
        log("Đã sẵn sàng thư mục KÊNH cho file chưa khớp mã.")
        for index, channel in enumerate(channels, 1):
            self.drive.ensure_channel_folders(channel["id"], channel["name"], channel["code"])
            log(f"Thư mục {index}/{len(channels)}: {channel['name']} - {channel['code']} / ANH, VIDEO.")
        log(f"Đã chuẩn bị xong {len(channels)} kênh và thư mục chung KÊNH trên Drive.")
        return {"channels": len(channels), "fallback": True}

    def regroup_verified_videos(self, article, log=lambda message: None, require_source_match=True):
        """Local admin action: regroup verified flat uploads with recorded article context."""
        route = self.channels.route_context("clip.mp4", "VIDEO", [article])
        if not route or route.get("article") != article.upper():
            raise ApiError("Nhập đúng mã bài đang bật, ví dụ TH9_001.", 400)
        moved = 0
        with self.lock:
            flat = self.drive.resolve_upload_folder(route["id"], route["name"], route["code"], "VIDEO")
            self.drive.resolve_article_folder(route["id"], route["name"], route["code"], route["article"], kind="VIDEO")
            rows = self.db.execute("""SELECT u.*,c.source_folders FROM uploads u
                JOIN upload_routes r ON r.upload_id=u.id
                LEFT JOIN upload_context c ON c.upload_id=u.id
                WHERE u.state='verified' AND u.parent=? AND r.channel_id=? AND r.media_kind='VIDEO'""",
                (flat, route["id"])).fetchall()
            log(f"Tìm thấy {len(rows)} video đã xác minh nằm trực tiếp trong VIDEO của {route['code']}.")
            for row in rows:
                context = json.loads(row["source_folders"]) if row["source_folders"] else []
                target_route = self.channels.route_context(row["name"], "VIDEO", context)
                if require_source_match and (not target_route or target_route.get("article") != route["article"]):
                    continue
                if not require_source_match:
                    target_route = dict(route, subfolders=[])
                parent = self.drive.resolve_article_folder(route["id"], route["name"], route["code"],
                    route["article"], target_route["subfolders"], kind="VIDEO")
                def validate(metadata, allowed_parents):
                    if (not metadata or metadata.get("id") != row["file_id"] or metadata.get("trashed") is not False
                            or metadata.get("parents") not in [[p] for p in allowed_parents]
                            or str(metadata.get("size")) != str(row["size"])
                            or metadata.get("sha256Checksum") != row["sha256"]
                            or metadata.get("md5Checksum") != row["md5"]):
                        raise ApiError("File đã thay đổi hoặc nằm ngoài thư mục dự kiến; dừng gom để kiểm tra.", 409)
                before = self.drive.get_file(row["file_id"])
                validate(before, [flat, parent])
                if before["parents"] == [flat]:
                    self.drive.move_file_parent(row["file_id"], flat, parent)
                validate(self.drive.get_file(row["file_id"]), [parent])
                destination = "/".join([route["folder_name"], "VIDEO", route["article"],
                    *target_route["subfolders"], row["name"]])
                self.db.execute("UPDATE uploads SET parent=? WHERE id=?", (parent, row["id"]))
                self.db.execute("UPDATE upload_routes SET destination=? WHERE upload_id=?", (destination, row["id"]))
                self.db.commit()
                moved += 1
                log("Đã gom và kiểm tra: " + destination)
        log(f"Hoàn tất: {moved} video đã xác minh được gom theo bài {route['article']}.")
        return moved

    def revoke_device(self, device_id):
        with self.lock:
            self.db.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,))
            self.db.commit()

    def enroll(self, payload, peer):
        now = time.time()
        with self.lock:
            # Bound unauthenticated work and dictionary memory.
            self.enroll_attempts = {k: v for k, v in self.enroll_attempts.items() if v[0] > now - 60}
            since, count = self.enroll_attempts.get(peer, (now, 0))
            if count >= 10 or len(self.enroll_attempts) >= 10000:
                raise ApiError("Quá nhiều lần kích hoạt. Chờ một phút.", 429)
            self.enroll_attempts[peer] = (since, count + 1)
            code = payload.get("code", "")
            if not isinstance(code, str) or len(code) > 200:
                raise ApiError("Mã kích hoạt không hợp lệ.", 403)
            digest = hashlib.sha256(code.encode()).hexdigest()
            row = self.db.execute("SELECT * FROM enrollments WHERE code_hash=?", (digest,)).fetchone()
            if not row or row["used"] or row["expires"] < now:
                raise ApiError("Mã kích hoạt đã dùng, hết hạn hoặc không hợp lệ.", 403)
            name = row["name"] or str(payload.get("name", "Máy nhân viên"))[:60]
            name = "".join(c for c in name if c.isprintable()).strip() or "Nhân viên"
            device_id, secret = uuid.uuid4().hex, secrets.token_urlsafe(48)
            self.store.set("device-" + device_id, secret)
            self.db.execute("INSERT INTO devices(id,name,created) VALUES(?,?,?)", (device_id, name, now))
            self.db.execute("UPDATE enrollments SET used=1 WHERE code_hash=?", (digest,))
            self.db.commit()
            return {"device_id": device_id, "secret": secret, "daily_limit_bytes": self.daily_limit_bytes}

    def authenticate(self, method, path, headers, body):
        device_id, ts, nonce, signature = [headers.get(k, "") for k in ("X-Device-Id", "X-Timestamp", "X-Nonce", "X-Signature")]
        if not re.fullmatch(r"[a-f0-9]{32}", device_id) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,100}", nonce):
            raise ApiError("Thiếu thông tin xác thực thiết bị.", 401)
        try:
            if abs(time.time() - int(ts)) > 120:
                raise ValueError()
        except (ValueError, TypeError):
            raise ApiError("Giờ máy lệch hoặc yêu cầu hết hạn. Đồng bộ đồng hồ hệ điều hành.", 401) from None
        with self.lock:
            row = self.db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if not row or row["revoked"]:
                raise ApiError("Thiết bị chưa được cấp quyền hoặc đã bị khóa.", 403)
            secret = self.store.get("device-" + device_id)
            expected = sign_request(secret or "", method, path, ts, nonce, body)
            if not secret or not hmac.compare_digest(signature, expected):
                raise ApiError("Chữ ký thiết bị không hợp lệ.", 401)
            self.db.execute("DELETE FROM nonces WHERE created<?", (time.time() - 300,))
            try:
                self.db.execute("INSERT INTO nonces VALUES(?,?,?)", (device_id, nonce, time.time()))
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                raise ApiError("Yêu cầu đã được sử dụng.", 409) from None
            return device_id

    def _validate_upload(self, payload):
        name, size = payload.get("name"), payload.get("size")
        if not isinstance(name, str) or not name or len(name) > 200 or any(c in name for c in '/\\\x00:') or not all(c.isprintable() for c in name):
            raise ApiError("Tên file không hợp lệ.", 400)
        if Path(name).suffix.lower() not in MEDIA:
            raise ApiError("Chỉ nhận ảnh và video trong danh sách định dạng hỗ trợ.", 400)
        if type(size) is not int or not 0 < size <= self.max_file_bytes:
            raise ApiError("Dung lượng file vượt giới hạn hoặc bằng 0.", 400)
        for key, length in (("sha256", 64), ("md5", 32)):
            if not isinstance(payload.get(key), str) or not re.fullmatch("[a-f0-9]{" + str(length) + "}", payload[key]):
                raise ApiError("Thiếu mã kiểm tra nội dung file.", 400)
        try:
            uuid.UUID(payload.get("request_id", ""))
        except (ValueError, TypeError, AttributeError):
            raise ApiError("Mã yêu cầu không hợp lệ.", 400) from None
        if "source_folders" in payload:
            validate_source_folders(payload["source_folders"])

    def _result(self, row):
        result = {"upload_id": row["id"], "file_id": row["file_id"], "state": row["state"],
                "size": row["size"], "sha256": row["sha256"],
                "session_url": self.store.get("session-" + row["id"]) if row["state"] != "verified" else ""}
        route = self.db.execute("SELECT destination FROM upload_routes WHERE upload_id=?", (row["id"],)).fetchone()
        if route:
            result["destination"] = route["destination"]
        return result

    def create_upload(self, device_id, payload):
        self._validate_upload(payload)
        with self.lock:
            row = self.db.execute("SELECT * FROM uploads WHERE device=? AND request_id=?", (device_id, payload["request_id"])).fetchone()
            if row:
                if any(row[k] != payload[k] for k in ("name", "size", "sha256", "md5")):
                    raise ApiError("Mã yêu cầu đã gắn với một nội dung khác.", 409)
                context = self.db.execute("SELECT source_folders FROM upload_context WHERE upload_id=?", (row["id"],)).fetchone()
                if ((context is None) != ("source_folders" not in payload)
                        or (context is not None and json.loads(context["source_folders"]) != payload["source_folders"])):
                    raise ApiError("Mã yêu cầu đã gắn với thư mục nguồn khác.", 409)
                if row["state"] == "verified" or self.store.get("session-" + row["id"]):
                    return self._result(row)
                return self._start(row)
            now = time.time()
            totals = self.db.execute("SELECT COALESCE(SUM(size),0),COUNT(*) FROM uploads WHERE device=? AND created>?", (device_id, now - 86400)).fetchone()
            if totals[0] + payload["size"] > self.daily_limit_bytes or totals[1] >= self.max_daily_uploads:
                raise ApiError("Đã đạt hạn mức upload của máy trong 24 giờ.", 429)
            pending = self.db.execute("SELECT COUNT(*) FROM uploads WHERE device=? AND state='pending' AND created>?", (device_id, now - 7 * 86400)).fetchone()[0]
            if pending >= self.max_pending:
                raise ApiError("Máy có phiên chưa hoàn tất. Hoàn tất phiên cũ trước.", 429)
            kind = "ANH" if MEDIA[Path(payload["name"]).suffix.lower()].startswith("image/") else "VIDEO"
            channel = self.channels.route_context(payload["name"], kind, payload.get("source_folders", [])) if self.route_channels else None
            if not self.route_channels:
                parent = self.drive.ensure_folder()
            elif channel:
                if channel.get("article"):
                    parent = self.drive.resolve_article_folder(channel["id"], channel["name"], channel["code"],
                                                               channel["article"], channel["subfolders"], kind=kind)
                else:
                    parent = self.drive.resolve_upload_folder(channel["id"], channel["name"], channel["code"], kind)
            else:
                parent = self.drive.resolve_unclassified_folder()
            file_id = self.drive.generate_id()
            upload_id = uuid.uuid4().hex
            # File ID, destination and reservation are one durable transaction
            # BEFORE network session creation. Catalog edits cannot reroute retries.
            with self.db:
                self.db.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,?)", (upload_id, device_id, payload["request_id"],
                    file_id, payload["name"], payload["size"], payload["sha256"], payload["md5"], parent, now, "pending"))
                if self.route_channels:
                    parts = [channel["folder_name"], kind] if channel else ["KÊNH"]
                    if channel and channel.get("article"):
                        parts.extend([channel["article"], *channel["subfolders"]])
                    destination = "/".join([*parts, payload["name"]])
                    self.db.execute("INSERT INTO upload_routes VALUES(?,?,?,?,?,?,?)", (upload_id,
                        channel["id"] if channel else "", channel["code"] if channel else "",
                        channel["name"] if channel else "KÊNH", kind, payload["name"], destination))
                if "source_folders" in payload:
                    self.db.execute("INSERT INTO upload_context VALUES(?,?)", (upload_id,
                        json.dumps(payload["source_folders"], ensure_ascii=False)))
            row = self.db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
            return self._start(row)

    def _start(self, row):
        existing = self.drive.get_file(row["file_id"])
        if existing:
            self._verify_metadata(row, existing)
            return self._result(self.db.execute("SELECT * FROM uploads WHERE id=?", (row["id"],)).fetchone())
        # Reuse pre-generated id after ambiguous responses, so retries cannot create another Drive file.
        route = self.db.execute("SELECT remote_name FROM upload_routes WHERE upload_id=?", (row["id"],)).fetchone()
        # Sessions created by older versions retain their original naming and
        # parent. New sessions retain the producer's channel/post filename.
        remote_name = route["remote_name"] if route else row["device"][:8] + "_" + row["name"]
        url = self.drive.start_upload(row["file_id"], remote_name, row["size"], MEDIA[Path(row["name"]).suffix.lower()], row["parent"])
        self.store.set("session-" + row["id"], url)
        return self._result(row)

    def restart_upload(self, device_id, payload):
        with self.lock:
            row = self._own_upload(device_id, payload)
            # An already verified item gets independently rechecked before any deletion receipt.
            return self._start(row)

    def _own_upload(self, device_id, payload):
        upload_id = payload.get("upload_id", "")
        if not isinstance(upload_id, str):
            raise ApiError("Mã upload không hợp lệ.", 400)
        row = self.db.execute("SELECT * FROM uploads WHERE id=? AND device=?", (upload_id, device_id)).fetchone()
        if not row:
            raise ApiError("Không tìm thấy phiên của thiết bị này.", 404)
        return row

    def _verify_metadata(self, row, metadata):
        if not metadata or metadata.get("trashed") or metadata.get("id") != row["file_id"]:
            raise ApiError("File chưa có trên Drive; giữ bản local.", 409)
        if row["parent"] not in metadata.get("parents", []) or str(metadata.get("size", "")) != str(row["size"]):
            raise ApiError("Dung lượng hoặc thư mục Drive chưa khớp; giữ bản local.", 409)
        # Require SHA256 too, fail closed until Google exposes it. Never delete on MD5 alone.
        if metadata.get("md5Checksum") != row["md5"] or metadata.get("sha256Checksum") != row["sha256"]:
            raise ApiError("Mã kiểm tra nội dung chưa khớp; giữ bản local.", 409)
        if row['state'] != 'verified':
            self.db.execute('INSERT OR IGNORE INTO upload_verified VALUES(?,?)', (row['id'], time.time()))
        self.db.execute("UPDATE uploads SET state='verified' WHERE id=?", (row["id"],))
        self.db.commit()
        self.store.delete("session-" + row["id"])
        return {"verified": True, "upload_id": row["id"], "file_id": row["file_id"], "size": row["size"],
                "sha256": row["sha256"], "md5": row["md5"]}

    def verify_upload(self, device_id, payload):
        with self.lock:
            row = self._own_upload(device_id, payload)
            return self._verify_metadata(row, self.drive.get_file(row["file_id"]))

    def device_request(self, path, headers, body, payload, peer):
        if path == "/enroll":
            return self.enroll(payload, peer)
        device_id = self.authenticate("POST", path, headers, body)
        if path == "/heartbeat":
            from .monitoring import save_status
            return save_status(self, device_id, payload)
        actions = {"/uploads": self.create_upload, "/verify": self.verify_upload,
                   "/uploads/restart": self.restart_upload}
        if path not in actions:
            raise ApiError("Không có chức năng này.", 404)
        return actions[path](device_id, payload)

    def start(self, host="0.0.0.0", port=48765):
        if self.server:
            return self.server.server_port
        broker = self
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.cert_path), str(self.key_path))

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False
            request_queue_size = 16
            slots = threading.BoundedSemaphore(16)

            def server_bind(self):
                if sys.platform == "win32":
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

            def get_request(self):
                sock, address = super().get_request()
                sock.settimeout(10)
                # Accept only here. A peer that never sends a TLS ClientHello
                # must not block the listener and every other device.
                return sock, address

            def process_request(self, request, client_address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except Exception:
                    self.slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                tls_request = None
                try:
                    # The semaphore slot is acquired before this worker starts;
                    # handshake and HTTP reads are both bounded by the timeout.
                    tls_request = context.wrap_socket(request, server_side=True)
                    super().process_request_thread(tls_request, client_address)
                except Exception as exc:
                    broker.last_error = type(exc).__name__
                    self.shutdown_request(tls_request if tls_request is not None else request)
                finally:
                    self.slots.release()

            def handle_error(self, request, client_address):
                error_type = sys.exc_info()[0]
                broker.last_error = error_type.__name__ if error_type else "RequestError"
                # Only the exception class: no traceback, message, URL or token.

        class Handler(BaseHTTPRequestHandler):
            server_version = "DriveDrop"
            sys_version = ""

            def log_message(self, *args):
                pass

            def reply(self, status, obj):
                raw = canonical_json(obj)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/health":
                    self.reply(200, {"ok": True, "service": "DriveDrop", "version": __version__, "worker_status": True,
                                     "album_routing": True, "google_connected": broker.drive.connected()})
                else:
                    self.reply(404, {"error": "Không có chức năng này."})

            def do_POST(self):
                try:
                    if self.headers.get("Transfer-Encoding"):
                        raise ApiError("Chỉ nhận yêu cầu JSON có Content-Length.", 400)
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= (524288 if self.path == '/heartbeat' else 16384):
                        raise ApiError("Yêu cầu JSON quá lớn hoặc rỗng.", 413)
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ApiError("Yêu cầu chưa đủ dữ liệu.", 400)
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise ApiError("Yêu cầu phải là JSON object.", 400)
                    result = broker.device_request(self.path, self.headers, body, payload, self.client_address[0])
                    self.reply(200, result)
                except ApiError as exc:
                    self.reply(exc.status if 400 <= exc.status <= 599 else 500, {"error": str(exc)})
                except (ValueError, UnicodeError, TypeError):
                    self.reply(400, {"error": "Dữ liệu yêu cầu không hợp lệ."})
                except Exception:
                    self.reply(500, {"error": "Máy chủ chưa xử lý được. File local được giữ lại."})

        self.server = Server((host, port), Handler)
        self.last_error = ""
        server = self.server
        def serve():
            try:
                server.serve_forever()
            except Exception as exc:
                self.last_error = type(exc).__name__
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        return self.server.server_port

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread:
            self.thread.join(timeout=3)
            self.thread = None

    def close(self):
        self.stop()
        self.db.close()
