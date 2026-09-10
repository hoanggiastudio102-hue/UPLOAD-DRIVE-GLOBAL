"""Local setup and authenticated web administration of the existing Broker."""
import hashlib
import hmac
import json
import secrets
import socket
import ssl
import sys
import threading
import time
from collections import OrderedDict
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .common import ApiError, atomic_json, canonical_json, load_json
from .monitoring import list_status, list_articles
from .reporting import add_reports, save_profile
from .alerts import acknowledge

DEVICE_PATHS = {"/enroll", "/heartbeat", "/uploads", "/verify", "/uploads/restart"}
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/reports.js": ("reports.js", "text/javascript"),
          "/style.css": ("style.css", "text/css")}


class WebAdmin:
    def __init__(self, broker, public_url="https://drive.vinhglobal.vn", operations=None):
        parsed = urlsplit(public_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Public URL must be an HTTPS origin")
        self.broker = broker
        self.public_url = public_url.rstrip("/")
        self.public_host = parsed.netloc.lower()
        self.config_file = broker.state_dir / "web-admin.json"
        self.config = load_json(self.config_file, {})
        self.lock = threading.RLock()
        self.setup_token = secrets.token_urlsafe(32)
        self.setup_deadline = time.monotonic() + 900
        self.sessions = OrderedDict()
        self.attempts = OrderedDict()
        self.job = {"state": "idle", "name": "", "log": []}
        self.operations = operations or {}
        self.servers = []
        self.local_port = None

    @property
    def configured(self):
        return bool(self.config.get("password_hash"))

    def setup_url(self):
        with self.lock:
            url = f"http://127.0.0.1:{self.local_port}/"
            if not self.configured:
                self.setup_token = secrets.token_urlsafe(32)
                self.setup_deadline = time.monotonic() + 900
                url += "#setup=" + self.setup_token
            return url

    @staticmethod
    def password_hash(password, salt):
        return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()

    def credentials(self, payload, setup=False):
        username, password = payload.get("username"), payload.get("password")
        if (not isinstance(username, str) or not 1 <= len(username.strip()) <= 80
                or not isinstance(password, str) or not 12 <= len(password) <= 256):
            raise ApiError("Nhập tài khoản và mật khẩu từ 12 đến 256 ký tự.", 400 if setup else 401)
        return username.strip(), password

    def limit_login(self, peer):
        now = time.monotonic()
        with self.lock:
            values = [t for t in self.attempts.pop(peer, []) if t > now - 300]
            if len(values) >= 8:
                self.attempts[peer] = values
                raise ApiError("Đã thử quá nhiều lần. Đợi 5 phút rồi đăng nhập lại.", 429)
            self.attempts[peer] = values + [now]
            while len(self.attempts) > 1024:
                self.attempts.popitem(last=False)

    def session(self, cookie, audience):
        try:
            jar = SimpleCookie(); jar.load(cookie or "")
            token = jar["dd_session"].value
            key = hashlib.sha256(token.encode()).hexdigest()
        except (KeyError, ValueError):
            return None, None
        now = time.monotonic()
        with self.lock:
            item = self.sessions.get(key)
            if not item or item["audience"] != audience:
                return None, None
            if now - item["created"] > 8 * 3600 or now - item["seen"] > 1800:
                self.sessions.pop(key, None)
                return None, None
            item["seen"] = now
            return key, dict(item)

    def new_session(self, audience):
        token = secrets.token_urlsafe(32)
        item = {"created": time.monotonic(), "seen": time.monotonic(),
                "csrf": secrets.token_urlsafe(32), "audience": audience}
        with self.lock:
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = item
            while len(self.sessions) > 200:
                self.sessions.popitem(last=False)
        return token, item

    def dashboard(self):
        devices = list_status(self.broker)
        report = add_reports(self.broker, devices)
        with self.broker.lock:
            counts = dict(self.broker.db.execute("SELECT state,count(*) FROM uploads GROUP BY state").fetchall())
        with self.lock:
            job = json.loads(json.dumps(self.job))
        return {"version": __version__, "public_url": self.public_url,
                "google_connected": self.broker.drive.connected(), "devices": devices,
                "uploads": self.broker.list_uploads(), "articles": list_articles(self.broker),
                "channels": self.broker.channels.list_channels(), "counts": counts, "job": job, "report": report}

    def start_job(self, name):
        actions = {"sync": self.broker.sync_channel_folders, **self.operations}
        if name not in actions:
            raise ApiError("Không có tác vụ này.", 404)
        with self.lock:
            if self.job["state"] == "running":
                raise ApiError("Một tác vụ đang chạy. Đợi hoàn tất trước.", 409)
            if not self.broker.operation_lock.acquire(blocking=False):
                raise ApiError("Máy chủ đang xử lý tác vụ khác. Đợi hoàn tất trước.", 409)
            self.job = {"state": "running", "name": name, "log": []}
        def log(message):
            with self.lock:
                self.job["log"] = (self.job["log"] + [str(message)[:500]])[-100:]
        def run():
            try:
                actions[name](log)
                state = "done"
            except ApiError:
                log("Chưa hoàn tất. Kiểm tra kết nối Google và chẩn đoán trên máy chủ.")
                state = "error"
            except Exception:
                log("Tác vụ gặp lỗi. Kiểm tra ứng dụng trên máy chủ.")
                state = "error"
            with self.lock:
                self.job["state"] = state
            self.broker.operation_lock.release()
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def start(self, host="0.0.0.0", tls_port=48800, local_port=48801):
        if self.servers:
            return
        owner = self
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.broker.cert_path), str(self.broker.key_path))

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = sys.platform != "win32"
            request_queue_size = 32
            def __init__(self, address, local):
                self.local = local
                self.slots = threading.BoundedSemaphore(32)
                super().__init__(address, Handler)
            def server_bind(self):
                if sys.platform == "win32":
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()
            def get_request(self):
                request, address = super().get_request()
                request.settimeout(15)
                return request, address
            def process_request(self, request, address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, address)
                except Exception:
                    self.slots.release()
                    self.shutdown_request(request)
            def process_request_thread(self, request, address):
                wrapped = request
                try:
                    if not self.local:
                        wrapped = context.wrap_socket(request, server_side=True)
                    super().process_request_thread(wrapped, address)
                except Exception:
                    self.shutdown_request(wrapped)
                finally:
                    self.slots.release()
            def handle_error(self, *args):
                pass

        class Handler(BaseHTTPRequestHandler):
            server_version = "DriveDrop"
            sys_version = ""
            def log_message(self, *args):
                pass
            def reply(self, status, obj=None, raw=None, mime="application/json", cookie=None):
                raw = canonical_json(obj) if raw is None else raw
                self.send_response(status)
                self.send_header("Content-Type", mime + "; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
                self.send_header("Connection", "close")
                if cookie is not None:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(raw)
            def origin(self):
                hosts = self.headers.get_all("Host", [])
                if len(hosts) != 1:
                    raise ApiError("Host không hợp lệ.", 400)
                host = hosts[0].lower()
                if self.server.local:
                    allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
                    scheme = "http"
                else:
                    allowed = {owner.public_host}
                    scheme = "https"
                if host not in allowed:
                    raise ApiError("Tên miền chưa được cấu hình.", 421)
                return scheme + "://" + host
            def body(self):
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    raise ApiError("Yêu cầu JSON không hợp lệ.", 400)
                if self.headers.get_content_type() != "application/json":
                    raise ApiError("Chỉ nhận application/json.", 415)
                length = int(self.headers["Content-Length"])
                if not 0 < length <= (524288 if self.path == '/heartbeat' else 16384):
                    raise ApiError("Yêu cầu vượt giới hạn hoặc rỗng.", 413)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ApiError("Yêu cầu chưa đủ dữ liệu.", 400)
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ApiError("Yêu cầu phải là JSON object.", 400)
                return raw, payload
            def cookie(self, token):
                return "dd_session=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" + ("28800" if token else "0") + ("" if self.server.local else "; Secure")
            def handle_route(self, post=False):
                origin = self.origin()
                if self.path == "/health" and not post:
                    return self.reply(200, {"ok": True, "service": "DriveDrop", "version": __version__, "album_routing": True, "worker_status": True, "google_connected": owner.broker.drive.connected()})
                if post and self.path in DEVICE_PATHS:
                    if self.server.local:
                        raise ApiError("Thiết bị phải kết nối HTTPS.", 403)
                    body, payload = self.body()
                    return self.reply(200, owner.broker.device_request(self.path, self.headers, body, payload, self.client_address[0]))
                if not post and self.path in ASSETS:
                    filename, mime = ASSETS[self.path]
                    return self.reply(200, raw=(Path(__file__).parent / "web" / filename).read_bytes(), mime=mime)
                key, session = owner.session(self.headers.get("Cookie"), origin)
                if not post and self.path == "/api/auth":
                    return self.reply(200, {"configured": owner.configured, "local": self.server.local,
                        "authenticated": bool(session), "username": owner.config.get("username") if session else None,
                        "csrf": session["csrf"] if session else None})
                if post:
                    if self.headers.get("Origin") != origin:
                        raise ApiError("Nguồn yêu cầu không hợp lệ.", 403)
                    _, payload = self.body()
                    if self.path == "/api/setup":
                        with owner.lock:
                            if (not self.server.local or owner.configured or time.monotonic() > owner.setup_deadline
                                    or not hmac.compare_digest(self.headers.get("X-Setup-Token", ""), owner.setup_token)):
                                raise ApiError("Mở lại Quản trị web trên máy chủ để thiết lập.", 403)
                            username, password = owner.credentials(payload, setup=True)
                            salt = secrets.token_hex(16)
                            config = {"username": username, "salt": salt, "password_hash": owner.password_hash(password, salt)}
                            atomic_json(owner.config_file, config)
                            owner.config = config
                            owner.setup_token = ""
                        return self.reply(200, {"ok": True})
                    if self.path == "/api/login":
                        owner.limit_login(self.client_address[0])
                        username, password = owner.credentials(payload)
                        candidate = owner.password_hash(password, owner.config.get("salt", "00" * 16))
                        if (not owner.configured or not hmac.compare_digest(candidate, owner.config["password_hash"])
                                or not hmac.compare_digest(username.encode(), owner.config["username"].encode())):
                            raise ApiError("Tài khoản hoặc mật khẩu chưa đúng.", 401)
                        token, item = owner.new_session(origin)
                        return self.reply(200, {"ok": True, "csrf": item["csrf"]}, cookie=self.cookie(token))
                if not session:
                    raise ApiError("Cần đăng nhập quản trị.", 401)
                if not post and self.path == "/api/dashboard":
                    return self.reply(200, owner.dashboard())
                if post:
                    if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                        raise ApiError("Phiên làm việc không hợp lệ. Tải lại trang.", 403)
                    if self.path == "/api/alerts/acknowledge":
                        return self.reply(200, acknowledge(owner.broker, payload))
                    if self.path == "/api/uploads/page":
                        return self.reply(200, owner.broker.upload_page(payload))
                    if self.path == "/api/logout":
                        with owner.lock:
                            owner.sessions.pop(key, None)
                        return self.reply(200, {"ok": True}, cookie=self.cookie(""))
                    if self.path == "/api/enroll":
                        if not owner.broker.drive.connected():
                            raise ApiError("Kết nối Google trên máy chủ trước khi cấp máy nhân viên.", 409)
                        result = owner.broker.create_enrollment(owner.public_url, payload.get("name", ""))
                        result["tls_mode"] = "public_ca"
                        return self.reply(200, result)
                    if self.path == "/api/revoke":
                        device = payload.get("id")
                        if not any(d["id"] == device for d in owner.broker.list_devices()):
                            raise ApiError("Không tìm thấy máy.", 404)
                        owner.broker.revoke_device(device)
                        return self.reply(200, {"ok": True})
                    if self.path == '/api/devices/profile':
                        return self.reply(200, save_profile(owner.broker, payload))
                    if self.path == "/api/channels/save":
                        row = owner.broker.channels.upsert(payload.get("name"), payload.get("code"), channel_id=payload.get("id"))
                        return self.reply(200, {"ok": True, "channel": row})
                    if self.path == "/api/channels/enable":
                        if type(payload.get("enabled")) is not bool:
                            raise ApiError("Trạng thái kênh không hợp lệ.", 400)
                        owner.broker.channels.set_enabled(payload.get("id"), payload["enabled"])
                        return self.reply(200, {"ok": True})
                    if self.path == "/api/job":
                        return self.reply(200, owner.start_job(payload.get("name")))
                raise ApiError("Không có chức năng này.", 404)
            def dispatch(self, post=False):
                try:
                    self.handle_route(post)
                except ApiError as exc:
                    self.reply(exc.status if 400 <= exc.status <= 599 else 500, {"error": str(exc)[:512]})
                except (ValueError, TypeError, UnicodeError):
                    self.reply(400, {"error": "Dữ liệu yêu cầu không hợp lệ."})
                except (OSError, ConnectionError):
                    pass
                except Exception:
                    self.reply(500, {"error": "Chưa xử lý được. Kiểm tra máy chủ DriveDrop."})
            def do_GET(self):
                self.dispatch()
            def do_POST(self):
                self.dispatch(True)

        try:
            self.servers.append(Server(("127.0.0.1", local_port), True))
            self.local_port = self.servers[0].server_port
            self.servers.append(Server((host, tls_port), False))
        except Exception:
            for server in self.servers:
                server.server_close()
            self.servers.clear()
            raise
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.servers.clear()
