"""Google credentials stay on the boss computer. No media bytes pass this module."""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .common import ApiError, atomic_json, load_json

SCOPE = "https://www.googleapis.com/auth/drive.file"
API = "https://www.googleapis.com/drive/v3/files"
ABOUT = "https://www.googleapis.com/drive/v3/about?fields=user(permissionId,emailAddress)"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
TOKEN = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_FIELDS = "id,name,mimeType,trashed,parents,appProperties"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=40) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ApiError("Google trả phản hồi quá lớn.", 502)
            return response.status, dict(response.headers), json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        # Do not propagate URLs, access tokens, or raw OAuth responses to logs.
        raise ApiError(f"Google API HTTP {exc.code}. Kiểm tra quyền, dung lượng hoặc thử lại.", exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiError("Không kết nối được Google qua HTTPS. File local được giữ lại.", 503) from None


class GoogleDrive:
    def __init__(self, state_dir, store):
        self.state_dir = Path(state_dir)
        self.store = store
        self.lock = threading.RLock()
        self.access = None
        self.access_until = 0
        self.folder_id = load_json(self.state_dir / "drive.json", {}).get("folder_id", "")

    def connected(self):
        return bool(self.store.get("google-refresh") and self.store.get("google-client"))

    def import_client(self, filename):
        value = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
        client = value.get("installed")
        if not client or not client.get("client_id", "").endswith(".apps.googleusercontent.com"):
            raise ApiError("Chọn JSON của OAuth Client loại Desktop app; không dùng service account.", 400)
        candidate = {k: client.get(k, "") for k in ("client_id", "client_secret")}
        with self.lock:
            current_raw = self.store.get("google-client")
            current = json.loads(current_raw) if current_raw else {}
            linked = bool(self.store.get("google-refresh") or self.store.get("google-account-id"))
            if linked and candidate["client_id"] != current.get("client_id"):
                raise ApiError("Thư mục data-boss đã gắn với OAuth Client khác. "
                               "Giữ nguyên Client hiện tại; cấu hình Client mới trong thư mục dữ liệu riêng.", 409)
            self.store.set("google-client", json.dumps(candidate))

    @staticmethod
    def _account_id_from_token(access_token, expected_email=None):
        # drive.file already permits about.get; request only an opaque account
        # identifier and the local administrator's optional account check, with
        # no broader OAuth permission and no profile data sent to employees.
        _, _, about = request("GET", ABOUT, headers={"Authorization": "Bearer " + access_token})
        user = about.get("user", {})
        identity = user.get("permissionId") if isinstance(user, dict) else None
        if not isinstance(identity, str) or not identity or len(identity) > 512:
            raise ApiError("Google chưa trả mã tài khoản để xác nhận đúng nơi nhận file.", 502)
        if expected_email:
            email = user.get("emailAddress")
            if not isinstance(email, str) or email.strip().casefold() != expected_email.strip().casefold():
                raise ApiError("Tài khoản Google vừa chọn không đúng email tài khoản tổng đã nhập. "
                               "Chưa lưu quyền đăng nhập; hãy chọn lại đúng tài khoản.", 409)
        return identity

    def authorize(self, on_message=lambda text: None, timeout=240, expected_email=None):
        if expected_email is not None:
            if (not isinstance(expected_email, str) or len(expected_email) > 254
                    or expected_email.strip().count("@") != 1
                    or any(char.isspace() for char in expected_email.strip())):
                raise ApiError("Email tài khoản tổng không hợp lệ.", 400)
            expected_email = expected_email.strip()
        raw = self.store.get("google-client")
        if not raw:
            raise ApiError("Cần nhập OAuth Client JSON trước khi đăng nhập.", 400)
        client = json.loads(raw)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        result = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                valid = parsed.path == "/callback" and secrets.compare_digest(query.get("state", [""])[0], state)
                if not valid:
                    self.send_error(400)
                    return
                result.update(code=query.get("code", [None])[0], error=bool(query.get("error")))
                message = "Đã nhận phản hồi. Quay lại DriveDrop trên máy sếp."
                body = message.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Callback)
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        params = {"client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
                  "scope": SCOPE, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                  "access_type": "offline", "prompt": "consent select_account"}
        if expected_email:
            params["login_hint"] = expected_email
        on_message("Đang mở Google trong trình duyệt. Chọn đúng tài khoản tổng 30 TB.")
        webbrowser.open("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))
        try:
            until = time.monotonic() + timeout
            while not result and time.monotonic() < until:
                server.handle_request()
        finally:
            server.server_close()
        if not result.get("code") or result.get("error"):
            raise ApiError("Chưa hoàn thành đăng nhập Google hoặc đã hết thời gian chờ.", 401)
        _, _, token = request("POST", TOKEN, urllib.parse.urlencode({"client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""), "code": result["code"], "code_verifier": verifier,
            "redirect_uri": redirect, "grant_type": "authorization_code"}).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"})
        if not token.get("refresh_token"):
            raise ApiError("Google không cấp quyền chạy nền. Thử kết nối lại và chấp nhận quyền.", 401)
        candidate_id = self._account_id_from_token(token["access_token"], expected_email=expected_email)
        with self.lock:
            current_client = json.loads(self.store.get("google-client") or "{}")
            if current_client.get("client_id") != client["client_id"]:
                raise ApiError("OAuth Client đã thay đổi trong lúc đăng nhập. Hãy đăng nhập lại.", 409)
            bound_id = self.store.get("google-account-id")
            if not bound_id and self.store.get("google-refresh"):
                # Upgrade old state safely: resolve the previous account before
                # replacing any credential, even if the new login selected a
                # different account. Failure leaves old state untouched.
                bound_id = self._account_id_from_token(self.access_token())
                self.store.set("google-account-id", bound_id)
            if bound_id and not secrets.compare_digest(bound_id, candidate_id):
                raise ApiError("Tài khoản vừa chọn khác tài khoản đã gắn với data-boss. "
                               "Chọn lại tài khoản tổng cũ; dùng thư mục dữ liệu riêng nếu muốn đổi tài khoản.", 409)
            # Persist the binding first. A failed token write can then only be
            # retried with this same account, never redirect pending sessions.
            if not bound_id:
                self.store.set("google-account-id", candidate_id)
            self.store.set("google-refresh", token["refresh_token"])
            self.access = token["access_token"]
            self.access_until = time.time() + int(token.get("expires_in", 3600)) - 120
            # Same-account reauthorization retains the existing folder and all
            # pending broker rows. Account changes above fail before token write.
        self.ensure_folder()
        on_message("Google đã kết nối; thư mục DriveDrop Inbox đã sẵn sàng.")

    def access_token(self):
        with self.lock:
            if self.access and self.access_until > time.time():
                return self.access
            refresh = self.store.get("google-refresh")
            client_raw = self.store.get("google-client")
            if not refresh or not client_raw:
                raise ApiError("Máy sếp chưa kết nối Google. Giữ file và đợi sếp đăng nhập.", 503)
            client = json.loads(client_raw)
            try:
                _, _, token = request("POST", TOKEN, urllib.parse.urlencode({"client_id": client["client_id"],
                    "client_secret": client.get("client_secret", ""), "refresh_token": refresh,
                    "grant_type": "refresh_token"}).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
            except ApiError as exc:
                if exc.status in (400, 401):
                    raise ApiError("Quyền Google hết hiệu lực; sếp cần đăng nhập lại. File local được giữ.", 503) from None
                raise
            self.access = token["access_token"]
            self.access_until = time.time() + int(token.get("expires_in", 3600)) - 120
            return self.access

    def api(self, method, url, body=None):
        headers = {"Authorization": "Bearer " + self.access_token()}
        encoded = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            encoded = json.dumps(body).encode()
        return request(method, url, encoded, headers)[2]

    def ensure_folder(self):
        with self.lock:
            if self.folder_id:
                metadata = self.get_file(self.folder_id)
                if metadata and not metadata.get("trashed") and metadata.get("mimeType") == "application/vnd.google-apps.folder":
                    return self.folder_id
            # Recover a prior app-created folder after interrupted setup.
            query = urllib.parse.urlencode({"q": "trashed=false and mimeType='application/vnd.google-apps.folder' and appProperties has { key='drivedropRoot' and value='v1' }", "fields": "files(id)", "pageSize": 10})
            found = self.api("GET", API + "?" + query).get("files", [])
            if found:
                self.folder_id = found[0]["id"]
            else:
                self.folder_id = self.api("POST", API + "?fields=id", {"name": "DriveDrop Inbox",
                    "mimeType": "application/vnd.google-apps.folder", "appProperties": {"drivedropRoot": "v1"}})["id"]
            atomic_json(self.state_dir / "drive.json", {"folder_id": self.folder_id})
            return self.folder_id

    def generate_id(self):
        return self.api("GET", API + "/generateIds?count=1&space=drive&type=files")["ids"][0]

    def _folder_journal(self):
        try:
            journal = load_json(self.state_dir / "drive-folders.json",
                                {"version": 1, "channels": {}, "special": {}})
        except (ValueError, UnicodeError) as exc:
            raise ApiError("Sổ thư mục Drive bị lỗi; cần khôi phục trước khi tạo thư mục.", 500) from exc
        if (not isinstance(journal, dict) or journal.get("version") != 1
                or not isinstance(journal.get("channels"), dict)
                or not isinstance(journal.get("special", {}), dict)
                or not isinstance(journal.get("articles", {}), dict)):
            raise ApiError("Sổ thư mục Drive không đúng định dạng; chưa tạo thư mục mới.", 500)
        journal.setdefault("special", {})
        journal.setdefault("articles", {})
        return journal

    def _save_folder_journal(self, journal):
        atomic_json(self.state_dir / "drive-folders.json", journal)

    @staticmethod
    def _query_literal(value):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    @staticmethod
    def _managed_folder_matches(metadata, parent, properties, expected_id=None):
        if not isinstance(metadata, dict):
            return False
        file_id = metadata.get("id")
        actual_properties = metadata.get("appProperties")
        return bool(isinstance(file_id, str) and file_id
            and (expected_id is None or file_id == expected_id)
            and metadata.get("mimeType") == FOLDER_MIME
            and metadata.get("trashed") is False
            and metadata.get("parents") == [parent]
            and isinstance(actual_properties, dict)
            and all(actual_properties.get(key) == value for key, value in properties.items()))

    def _managed_folder_candidates(self, parent, properties, direct=None):
        """Search stable private properties, checking every candidate by ID.

        Pagination/incomplete results must never be mistaken for an empty folder
        search, and an already-known valid ID covers Drive search indexing lag.
        """
        clauses = ["trashed=false", "mimeType=" + self._query_literal(FOLDER_MIME),
                   self._query_literal(parent) + " in parents"]
        clauses.extend("appProperties has { key=" + self._query_literal(key)
                       + " and value=" + self._query_literal(value) + " }"
                       for key, value in sorted(properties.items()))
        matches = {}
        if self._managed_folder_matches(direct, parent, properties):
            matches[direct["id"]] = direct
        page_token = None
        seen_tokens = set()
        for _ in range(100):
            params = {"q": " and ".join(clauses), "spaces": "drive", "corpora": "user",
                      "fields": "nextPageToken,incompleteSearch,files(id)", "pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            response = self.api("GET", API + "?" + urllib.parse.urlencode(params))
            if (not isinstance(response, dict) or response.get("incompleteSearch")
                    or not isinstance(response.get("files", []), list)):
                raise ApiError("Google chưa trả đủ kết quả thư mục; hãy thử lại.", 503)
            for item in response.get("files", []):
                file_id = item.get("id") if isinstance(item, dict) else None
                if not isinstance(file_id, str) or not file_id:
                    raise ApiError("Google trả danh sách thư mục không hợp lệ.", 502)
                metadata = self.get_file(file_id)
                if self._managed_folder_matches(metadata, parent, properties, file_id):
                    matches[file_id] = metadata
                else:
                    matches.pop(file_id, None)
                if len(matches) > 1:
                    raise ApiError("Có nhiều thư mục Drive cùng mã nhận diện. "
                                   "Sếp cần xử lý thư mục trùng trước khi tiếp tục.", 409)
            page_token = response.get("nextPageToken")
            if not page_token:
                return matches
            if not isinstance(page_token, str) or page_token in seen_tokens:
                raise ApiError("Google trả phân trang thư mục không hợp lệ.", 502)
            seen_tokens.add(page_token)
        raise ApiError("Chưa kiểm tra hết thư mục Drive; chưa tạo thư mục mới.", 503)

    def _ensure_managed_folder(self, journal, entries, role, parent, name, properties):
        record = entries.get(role)
        if record is not None and (not isinstance(record, dict)
                or not isinstance(record.get("id"), str) or not record["id"]
                or not isinstance(record.get("parent"), str) or not record["parent"]
                or record.get("state") not in ("reserved", "ready")):
            raise ApiError("Bản ghi thư mục Drive không hợp lệ; chưa tạo thư mục mới.", 500)
        previous = self.get_file(record["id"]) if record else None
        if previous and previous.get("id") != record["id"]:
            raise ApiError("Google trả mã thư mục không khớp yêu cầu.", 502)
        matches = self._managed_folder_candidates(parent, properties, previous)
        if matches:
            metadata = next(iter(matches.values()))
        else:
            # Only an unconfirmed, still-missing reservation can reuse its ID.
            # Ready IDs that were deleted, or IDs that moved/changed ownership,
            # get a new reservation; old folders are never moved or repurposed.
            reuse = bool(record and record["state"] == "reserved"
                         and record["parent"] == parent and previous is None)
            file_id = record["id"] if reuse else self.generate_id()
            if not isinstance(file_id, str) or not file_id:
                raise ApiError("Google chưa cấp mã thư mục hợp lệ.", 502)
            entries[role] = {"id": file_id, "parent": parent, "state": "reserved"}
            self._save_folder_journal(journal)  # durable BEFORE any create request
            post_error = None
            try:
                created = self.api("POST", API + "?fields=id", {"id": file_id,
                    "name": name, "mimeType": FOLDER_MIME, "parents": [parent],
                    "appProperties": properties})
                if not isinstance(created, dict) or created.get("id") != file_id:
                    raise ApiError("Google trả mã thư mục tạo mới không khớp.", 502)
            except ApiError as exc:
                # A lost response or 409 can mean this exact ID already exists.
                # Independent metadata verification is the only success signal.
                post_error = exc
            metadata = self.get_file(file_id)
            if not self._managed_folder_matches(metadata, parent, properties, file_id):
                if post_error:
                    raise post_error
                raise ApiError("Thư mục Drive mới chưa được xác minh; hãy thử lại.", 409)
            matches = self._managed_folder_candidates(parent, properties, metadata)
            if len(matches) != 1 or file_id not in matches:
                raise ApiError("Thư mục Drive thay đổi trong lúc tạo; chưa sử dụng để upload.", 409)
            metadata = matches[file_id]
        file_id = metadata["id"]
        if metadata.get("name") != name:
            # The stable UUID/role, parent and ownership markers were verified.
            # Rename this same folder; never identify a channel by display name.
            self.api("PATCH", API + "/" + urllib.parse.quote(file_id, safe="")
                     + "?fields=" + FOLDER_FIELDS, {"name": name})
        metadata = self.get_file(file_id)
        if (not self._managed_folder_matches(metadata, parent, properties, file_id)
                or metadata.get("name") != name):
            raise ApiError("Thư mục Drive đã đổi vị trí hoặc thông tin; chưa sử dụng để upload.", 409)
        entries[role] = {"id": file_id, "parent": parent, "state": "ready"}
        self._save_folder_journal(journal)
        return file_id

    def _checked_inbox(self):
        parent = self.ensure_folder()
        metadata = self.get_file(parent)
        if (not isinstance(metadata, dict) or metadata.get("id") != parent
                or metadata.get("trashed") is not False or metadata.get("mimeType") != FOLDER_MIME
                or not isinstance(metadata.get("appProperties"), dict)
                or metadata["appProperties"].get("drivedropRoot") != "v1"):
            raise ApiError("Chưa xác minh được thư mục DriveDrop Inbox thuộc ứng dụng.", 409)
        return parent

    @staticmethod
    def _channel_folder_identity(channel_id, name, code):
        try:
            if not isinstance(channel_id, str):
                raise ValueError()
            identity = uuid.UUID(channel_id).hex
        except (ValueError, AttributeError) as exc:
            raise ApiError("Mã nhận diện kênh không hợp lệ.", 400) from exc
        for value in (name, code):
            if (not isinstance(value, str) or not value.strip() or len(value) > 200
                    or not value.isprintable() or any(char in value for char in "/\\\x00")):
                raise ApiError("Tên hoặc mã kênh không hợp lệ.", 400)
        return identity

    def ensure_channel_folders(self, channel_id, name, code):
        """Return current managed channel/ANH/VIDEO IDs without changing old uploads."""
        identity = self._channel_folder_identity(channel_id, name, code)
        with self.lock:
            parent = self._checked_inbox()
            journal = self._folder_journal()
            entries = journal["channels"].setdefault(identity, {})
            if not isinstance(entries, dict):
                raise ApiError("Bản ghi kênh trong sổ thư mục không hợp lệ.", 500)
            base = {"drivedropChannel": identity, "drivedropSchema": "v1"}
            channel_properties = dict(base, drivedropRole="channel")
            channel = self._ensure_managed_folder(journal, entries, "channel", parent,
                f"{name.strip()} - {code.strip()}", channel_properties)
            result = {"channel": channel}
            for role in ("ANH", "VIDEO"):
                result[role] = self._ensure_managed_folder(journal, entries, role, channel,
                    role, dict(base, drivedropRole=role))
            # Detect an ancestor move during the child-folder operations too.
            if not self._managed_folder_matches(self.get_file(channel), parent, channel_properties, channel):
                raise ApiError("Thư mục kênh đã đổi vị trí; chưa cấp phiên upload.", 409)
            return result

    def ensure_unclassified_folder(self):
        """Files without a known channel go directly into Inbox/KÊNH."""
        with self.lock:
            parent = self._checked_inbox()
            journal = self._folder_journal()
            return self._ensure_managed_folder(journal, journal["special"], "fallback", parent,
                "KÊNH", {"drivedropRole": "fallback", "drivedropSchema": "v1"})

    def _existing_inbox_for_upload(self):
        """Validate the canonical Inbox once; do not search or update state."""
        if not isinstance(self.folder_id, str) or not self.folder_id:
            return None
        metadata = self.get_file(self.folder_id)
        if (not isinstance(metadata, dict) or metadata.get("id") != self.folder_id
                or metadata.get("trashed") is not False or metadata.get("mimeType") != FOLDER_MIME
                or not isinstance(metadata.get("appProperties"), dict)
                or metadata["appProperties"].get("drivedropRoot") != "v1"):
            return None
        return self.folder_id

    @staticmethod
    def _ready_folder_record(entries, role, parent):
        record = entries.get(role) if isinstance(entries, dict) else None
        if (isinstance(record, dict) and record.get("state") == "ready"
                and record.get("parent") == parent
                and isinstance(record.get("id"), str) and record["id"]):
            return record
        return None

    def resolve_upload_folder(self, channel_id, name, code, kind):
        """Resolve one healthy media branch with three fresh metadata reads.

        Canonical, previously verified journal IDs avoid arbitrary selection.
        Full duplicate-search recovery remains in the administrator ensure path
        and is also used here whenever a record, name or metadata is unhealthy.
        """
        identity = self._channel_folder_identity(channel_id, name, code)
        if kind not in ("ANH", "VIDEO"):
            raise ApiError("Loại thư mục upload không hợp lệ.", 400)
        with self.lock:
            parent = self._existing_inbox_for_upload()
            journal = self._folder_journal()
            entries = journal["channels"].get(identity)
            channel_record = self._ready_folder_record(entries, "channel", parent) if parent else None
            if channel_record:
                channel_id_on_drive = channel_record["id"]
                properties = {"drivedropChannel": identity, "drivedropSchema": "v1"}
                channel = self.get_file(channel_id_on_drive)
                if (self._managed_folder_matches(channel, parent,
                        dict(properties, drivedropRole="channel"), channel_id_on_drive)
                        and channel.get("name") == f"{name.strip()} - {code.strip()}"):
                    media_record = self._ready_folder_record(entries, kind, channel_id_on_drive)
                    if media_record:
                        media = self.get_file(media_record["id"])
                        if (self._managed_folder_matches(media, channel_id_on_drive,
                                dict(properties, drivedropRole=kind), media_record["id"])
                                and media.get("name") == kind):
                            return media_record["id"]
            # A missing/corrupt/renamed/moved/trashed node gets the existing
            # durable, duplicate-aware repair. Never silently trust an old ID.
            return self.ensure_channel_folders(channel_id, name, code)[kind]

    def resolve_unclassified_folder(self):
        """Resolve Inbox/KÊNH with two fresh reads when both IDs are healthy."""
        with self.lock:
            parent = self._existing_inbox_for_upload()
            journal = self._folder_journal()
            record = self._ready_folder_record(journal["special"], "fallback", parent) if parent else None
            if record:
                metadata = self.get_file(record["id"])
                if (self._managed_folder_matches(metadata, parent,
                        {"drivedropRole": "fallback", "drivedropSchema": "v1"}, record["id"])
                        and metadata.get("name") == "KÊNH"):
                    return record["id"]
            return self.ensure_unclassified_folder()

    @staticmethod
    def _article_folder_parts(article, subfolders):
        if not isinstance(subfolders, (list, tuple)) or len(subfolders) > 32:
            raise ApiError("Đường dẫn thư mục bài viết không hợp lệ hoặc quá sâu.", 400)
        parts = (article, *subfolders)
        for value in parts:
            if (not isinstance(value, str) or not value.strip() or len(value) > 200
                    or value in (".", "..") or not value.isprintable()
                    or any(char in value for char in "/\\\x00")):
                raise ApiError("Tên thư mục bài viết không hợp lệ.", 400)
        try:
            path_size = len("/".join(parts).encode("utf-8"))
        except UnicodeError as exc:
            raise ApiError("Tên thư mục bài viết không phải Unicode hợp lệ.", 400) from exc
        if path_size > 6000:
            raise ApiError("Đường dẫn thư mục bài viết quá dài.", 400)
        return parts

    def resolve_article_folder(self, channel_id, name, code, article, subfolders=(), kind="ANH"):
        """Return kind/article/subfolders using durable private path identities.

        Ready journal IDs require only one fresh metadata read per article path
        segment. Recovery uses the same reserved-ID and duplicate checks as the
        channel folders; a folder with only a matching display name is unrelated.
        """
        identity = self._channel_folder_identity(channel_id, name, code)
        parts = self._article_folder_parts(article, subfolders)
        with self.lock:
            parent = self.resolve_upload_folder(channel_id, name, code, kind)
            journal = self._folder_journal()
            entries = journal["articles"].setdefault(identity, {})
            if not isinstance(entries, dict):
                raise ApiError("Bản ghi thư mục bài viết không hợp lệ.", 500)
            for depth, part in enumerate(parts, start=1):
                # Preserve existing image identities; namespace video paths separately.
                identity_parts = parts[:depth] if kind == "ANH" else (kind, *parts[:depth])
                path_bytes = json.dumps(identity_parts, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8")
                path_key = hashlib.sha256(path_bytes).hexdigest()
                properties = {"drivedropChannel": identity, "drivedropSchema": "v1",
                              "drivedropRole": "article", "drivedropPath": path_key}
                record = self._ready_folder_record(entries, path_key, parent)
                metadata = self.get_file(record["id"]) if record else None
                if (record and self._managed_folder_matches(metadata, parent,
                        properties, record["id"]) and metadata.get("name") == part):
                    parent = record["id"]
                else:
                    parent = self._ensure_managed_folder(journal, entries, path_key,
                        parent, part, properties)
            return parent

    def start_upload(self, file_id, name, size, mime, parent):
        headers = {"Authorization": "Bearer " + self.access_token(), "Content-Type": "application/json; charset=utf-8",
                   "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": mime}
        body = json.dumps({"id": file_id, "name": name, "parents": [parent], "mimeType": mime,
                           "appProperties": {"drivedrop": "v1"}}).encode()
        _, response_headers, _ = request("POST", UPLOAD, body, headers)
        url = next((v for k, v in response_headers.items() if k.lower() == "location"), "")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com" or parsed.path != "/upload/drive/v3/files" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ApiError("Google trả địa chỉ phiên không hợp lệ.", 502)
        return url

    def get_file(self, file_id):
        fields = "id,name,size,md5Checksum,sha256Checksum,parents,trashed,mimeType,appProperties"
        try:
            return self.api("GET", API + "/" + urllib.parse.quote(file_id, safe="") + "?fields=" + fields)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def move_file_parent(self, file_id, old_parent, new_parent):
        query = urllib.parse.urlencode({"addParents": new_parent, "removeParents": old_parent,
                                        "fields": "id,parents"})
        return self.api("PATCH", API + "/" + urllib.parse.quote(file_id, safe="") + "?" + query, {})
