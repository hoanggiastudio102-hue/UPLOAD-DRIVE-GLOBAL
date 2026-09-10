"""Local channel catalog and filename routing. No Google calls occur here."""
from __future__ import annotations

from pathlib import Path
import re
import threading
import uuid

from .common import ApiError, atomic_json, load_json


MAX_CHANNELS = 2000
MAX_TEXT_BYTES = 1024 * 1024
MAX_CATALOG_BYTES = 2 * 1024 * 1024
DEFAULT_CHANNEL_TEXT = """KÊNH 01\tKL1
KÊNH 09\tHG23
KÊNH 29\tHG22
KÊNH 32\tRM4_9
KÊNH 38\tKL2
KÊNH 40\tRM5_3
KÊNH 57\tBP6
KÊNH 69\tRM4_15
KÊNH 76\tTH9
KÊNH 79\tVEO79
KÊNH 81\tHG5
KÊNH 82\tBP25
KÊNH 84\tRM4_17
KÊNH 86\tVEO86
KÊNH 99\tLQ27
KÊNH 104\tLQ12
Kênh 107\tHG18
Kênh 115\tHG14
Kênh 141\tBP26
Kênh 150\tLQ35
Kênh 163\tLQ36
Kênh 165\tNA8
Kênh 168\tRM5_15
Kênh 169\tHG20
Kênh 179\tBP24
Kênh 183\tAT6
Kênh 190\tRM5_17
Kênh 195\tTH12
Kênh 198\tNA10
Kênh 199\tBP22
KÊNH 214\tNA13_
KÊNH 215\tRM4_26
KÊNH 216\tRM4_27
KÊNH 217\tRM4_28
KÊNH 218\tRM4_29
Kênh 221\tLQ34
KÊNH 226\tRM4_20
KÊNH 228\tRM4_22
KÊNH 230\tRM4_23
KÊNH 231\tRM4_24
KÊNH 232\tRM4_25
KÊNH 233\tRM4_31
KÊNH 234\tRM4_32
KÊNH 235\tRM4_33
KÊNH 236\tRM4_34
KÊNH 237\tLQ38
KÊNH 239\tLQ40
KÊNH 240\tLQ41
KÊNH 241\tLQ42
KÊNH 242\tRM4_35
KÊNH 246\tLQ37
KÊNH 247\tAT8
Kênh 249\tRM4_30
Kênh 258\tKL15
KÊNH 668\tVEO668
kênh 789\tVEO789
Kênh 791\tAT7
"""


def _error(message):
    return ApiError(message, 400)


def validate_source_folders(value):
    """Accept folder names only, never a filesystem path or a Drive parent ID."""
    if not isinstance(value, list) or len(value) > 32:
        raise _error("Danh sách thư mục nguồn không hợp lệ; tối đa 32 cấp.")
    total = 0
    for part in value:
        if (not isinstance(part, str) or not part.strip() or len(part) > 200
                or part in (".", "..") or not part.isprintable()
                or any(char in part for char in "/\\")):
            raise _error("Tên thư mục nguồn không hợp lệ.")
        total += len(part.encode("utf-8"))
    if total + max(0, len(value) - 1) > 6000:
        raise _error("Tên các thư mục nguồn vượt giới hạn 6000 byte.")
    return list(value)


def normalize_code(value):
    if not isinstance(value, str):
        raise _error("Mã kênh phải là chữ và số, ví dụ KL1 hoặc RM4_9.")
    value = value.strip().rstrip("_")
    if not value or len(value) > 64 or not value.isascii() or not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise _error("Mã kênh chỉ dùng chữ ASCII, số và dấu gạch dưới; tối đa 64 ký tự.")
    return value.upper()


def normalize_name(value):
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _error("Tên kênh không được chứa ký tự điều khiển hoặc xuống dòng.")
    value = " ".join(value.strip().split())
    if not value or len(value) > 100 or value in (".", "..") or value.endswith("."):
        raise _error("Tên kênh cần từ 1 đến 100 ký tự và không kết thúc bằng dấu chấm.")
    if any(char in value for char in '<>:"/\\|?*'):
        raise _error('Tên kênh không được chứa các ký tự đường dẫn: < > : " / \\ | ? *')
    return value


def _parse_text(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise _error("Danh sách TXT phải là văn bản và không vượt quá 1 MiB.")
    rows, used = [], set()
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        fields = line.rsplit(None, 1)
        if len(fields) != 2:
            raise _error(f"Dòng {number}: cần tên kênh và mã, cách nhau bằng Tab hoặc khoảng trắng.")
        try:
            name, code = normalize_name(fields[0]), normalize_code(fields[1])
        except ApiError as exc:
            raise _error(f"Dòng {number}: {exc}") from None
        if code in used:
            raise _error(f"Dòng {number}: mã {code} bị trùng trong danh sách TXT.")
        rows.append({"name": name, "code": code})
        used.add(code)
        if len(rows) > MAX_CHANNELS:
            raise _error(f"Danh sách chỉ được tối đa {MAX_CHANNELS} kênh.")
    if not rows:
        raise _error("Danh sách TXT chưa có kênh nào.")
    return rows


class ChannelCatalog:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        with self.lock:
            if self.path.exists() and self.path.stat().st_size > MAX_CATALOG_BYTES:
                raise _error("File danh sách kênh vượt giới hạn 2 MiB; cần kiểm tra trước khi mở.")
            try:
                document = load_json(self.path)
            except (ValueError, UnicodeError):
                raise _error("File danh sách kênh bị lỗi; không tự ghi đè danh sách hiện có.") from None
            if document is None and not self.path.exists():
                rows = [{"id": str(uuid.uuid4()), **row, "enabled": True} for row in _parse_text(DEFAULT_CHANNEL_TEXT)]
                self._commit(rows)
            else:
                if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("channels"), list):
                    raise _error("Cấu trúc file danh sách kênh không hợp lệ; không tự thay bằng danh sách mẫu.")
                self._channels = self._validate_rows(document["channels"])
                self._rebuild_matchers()

    @staticmethod
    def _validate_rows(rows):
        if len(rows) > MAX_CHANNELS:
            raise _error(f"Danh sách chỉ được tối đa {MAX_CHANNELS} kênh.")
        result, ids, codes = [], set(), set()
        for row in rows:
            if not isinstance(row, dict) or type(row.get("enabled")) is not bool:
                raise _error("Một mục trong danh sách kênh không hợp lệ.")
            identifier = row.get("id")
            try:
                if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
                    raise ValueError()
            except (ValueError, AttributeError):
                raise _error("ID của một kênh không hợp lệ.") from None
            code, name = normalize_code(row.get("code")), normalize_name(row.get("name"))
            if identifier in ids or code in codes:
                raise _error("Danh sách có ID hoặc mã kênh trùng nhau, kể cả kênh đang tắt.")
            ids.add(identifier)
            codes.add(code)
            result.append({"id": identifier, "name": name, "code": code, "enabled": row["enabled"]})
        return result

    def _rebuild_matchers(self):
        self._matchers = [(row, re.compile(r"^" + re.escape(row["code"]) + r"_[0-9]+(?=$|[_ -])", re.IGNORECASE | re.ASCII))
                          for row in sorted(self._channels, key=lambda item: len(item["code"]), reverse=True)]

    def _commit(self, rows):
        validated = self._validate_rows(rows)
        atomic_json(self.path, {"version": 1, "channels": validated})
        self._channels = validated
        self._rebuild_matchers()

    def list_channels(self):
        with self.lock:
            return [dict(row) for row in self._channels]

    def upsert(self, name, code, channel_id=None):
        name, code = normalize_name(name), normalize_code(code)
        with self.lock:
            rows = self.list_channels()
            existing = next((row for row in rows if row["id"] == channel_id), None) if channel_id is not None else None
            if channel_id is not None and existing is None:
                raise _error("Không tìm thấy kênh cần sửa; hãy làm mới danh sách.")
            if any(row["code"] == code and row["id"] != channel_id for row in rows):
                raise _error(f"Mã {code} đã thuộc một kênh khác, kể cả khi kênh đó đang tắt.")
            if existing is None:
                if len(rows) >= MAX_CHANNELS:
                    raise _error(f"Danh sách chỉ được tối đa {MAX_CHANNELS} kênh.")
                existing = {"id": str(uuid.uuid4()), "name": name, "code": code, "enabled": True}
                rows.append(existing)
            else:
                existing.update(name=name, code=code)
            self._commit(rows)
            return dict(existing)

    def set_enabled(self, channel_id, enabled):
        if type(enabled) is not bool:
            raise _error("Trạng thái kênh phải là bật hoặc tắt.")
        with self.lock:
            rows = self.list_channels()
            existing = next((row for row in rows if row["id"] == channel_id), None)
            if existing is None:
                raise _error("Không tìm thấy kênh cần đổi trạng thái.")
            existing["enabled"] = enabled
            self._commit(rows)
            return dict(existing)

    def import_text(self, text):
        parsed = _parse_text(text)  # Validate the entire import before any mutation.
        with self.lock:
            rows = self.list_channels()
            by_code = {row["code"]: row for row in rows}
            added = updated = 0
            for row in parsed:
                if row["code"] in by_code:
                    by_code[row["code"]]["name"] = row["name"]
                    updated += 1
                else:
                    item = {"id": str(uuid.uuid4()), **row, "enabled": True}
                    rows.append(item)
                    by_code[item["code"]] = item
                    added += 1
            self._commit(rows)
            return {"added": added, "updated": updated}

    def route(self, filename, media_kind):
        if (not isinstance(filename, str) or not filename.strip() or len(filename) > 255 or
                filename in (".", "..") or any(char in filename for char in "/\\") or
                any(ord(char) < 32 or ord(char) == 127 for char in filename)):
            raise _error("Tên file không hợp lệ; chỉ truyền tên file, không kèm đường dẫn.")
        kinds = {"ANH": "ANH", "ẢNH": "ANH", "IMAGE": "ANH", "VIDEO": "VIDEO"}
        kind = kinds.get(media_kind.strip().upper()) if isinstance(media_kind, str) else None
        if kind is None:
            raise _error("Loại file phải là ANH hoặc VIDEO.")
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        with self.lock:
            for channel, pattern in self._matchers:
                if pattern.match(stem):
                    if not channel["enabled"]:
                        return None
                    return {**channel, "folder_name": f'{channel["name"]} - {channel["code"]}',
                            "media_kind": kind, "filename": filename}
        return None  # Broker sends unknown/malformed/disabled prefixes to Inbox/KÊNH.

    def route_context(self, filename, media_kind, source_folders):
        """The nearest article folder wins for both images and videos."""
        folders = validate_source_folders(source_folders)
        file_route = self.route(filename, media_kind)  # Also validates the filename/kind.
        kind = "VIDEO" if media_kind.strip().upper() == "VIDEO" else "ANH"
        with self.lock:
            for index in range(len(folders) - 1, -1, -1):
                for channel, _ in self._matchers:
                    match = re.fullmatch(re.escape(channel["code"]) + r"_([0-9]+)",
                                         folders[index], re.IGNORECASE | re.ASCII)
                    if match:
                        if not channel["enabled"]:
                            return None
                        result = {**channel, "folder_name": f'{channel["name"]} - {channel["code"]}',
                                  "media_kind": kind, "filename": filename}
                        subfolders = folders[index + 1:]
                        media_labels = {"VIDEO"} if kind == "VIDEO" else {"ANH", "ẢNH"}
                        if subfolders and subfolders[0].upper() in media_labels:
                            subfolders = subfolders[1:]
                        result.update(article=channel["code"] + "_" + match.group(1), subfolders=subfolders)
                        return result
            if kind == "VIDEO" and file_route:
                stem = filename.rsplit(".", 1)[0] if "." in filename else filename
                article = re.match(re.escape(file_route["code"]) + r"_([0-9]+)",
                                   stem, re.IGNORECASE | re.ASCII)
                file_route.update(article=file_route["code"] + "_" + article.group(1), subfolders=[])
            return file_route
