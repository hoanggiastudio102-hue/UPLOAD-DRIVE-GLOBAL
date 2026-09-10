"""Explicit offline substitutes; never imported by production entry points."""
from __future__ import annotations

import hashlib
import re
import threading
from urllib.parse import parse_qs, urlsplit


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


class FakeDrive:
    """Stores metadata only when explicit upload bytes reach FakeUploadTransport."""

    folder_id = "test-private-folder"

    def __init__(self):
        self.files = {}
        self.sessions = {}
        self.started = []
        self.generated = 0
        self.folders = {}
        self.channel_folders = {}
        self.channel_folder_calls = []
        self.ensure_folder_calls = 0
        self.unclassified_folder_calls = 0
        self.article_folder_calls = []
        self.available = True
        self.lock = threading.RLock()

    def connected(self):
        return self.available

    def ensure_folder(self):
        self.ensure_folder_calls += 1
        self.folders[self.folder_id] = {"id": self.folder_id, "name": "DriveDrop Inbox", "mimeType": "application/vnd.google-apps.folder", "parents": [], "trashed": False}
        return self.folder_id

    def ensure_channel_folders(self, channel_id, name, code):
        self.channel_folder_calls.append((channel_id, name, code))
        self.ensure_folder()
        folder_id = "test-channel-" + hashlib.sha256(channel_id.encode()).hexdigest()[:12]
        result = {"channel": folder_id, "ANH": folder_id + "-image", "VIDEO": folder_id + "-video"}
        self.folders[folder_id] = {"id": folder_id, "name": f"{name} - {code}", "mimeType": "application/vnd.google-apps.folder", "parents": [self.folder_id], "trashed": False}
        for kind, label in (("ANH", "ANH"), ("VIDEO", "VIDEO")):
            self.folders[result[kind]] = {"id": result[kind], "name": label, "mimeType": "application/vnd.google-apps.folder", "parents": [folder_id], "trashed": False}
        self.channel_folders[channel_id] = result
        return dict(result)

    def ensure_unclassified_folder(self):
        self.unclassified_folder_calls += 1
        self.ensure_folder()
        folder_id = "test-unclassified-folder"
        self.folders[folder_id] = {"id": folder_id, "name": "KÊNH", "mimeType": "application/vnd.google-apps.folder", "parents": [self.folder_id], "trashed": False}
        return folder_id

    def resolve_upload_folder(self, channel_id, name, code, media_kind):
        return self.ensure_channel_folders(channel_id, name, code)[media_kind]

    def resolve_unclassified_folder(self):
        return self.ensure_unclassified_folder()

    def resolve_article_folder(self, channel_id, name, code, article, subfolders=(), kind="ANH"):
        self.article_folder_calls.append((channel_id, name, code, article, tuple(subfolders)))
        parent = self.ensure_channel_folders(channel_id, name, code)[kind]
        for label in (article, *subfolders):
            folder_id = "test-article-" + hashlib.sha256((parent + "/" + label).encode()).hexdigest()[:16]
            self.folders[folder_id] = {"id": folder_id, "name": label, "mimeType": "application/vnd.google-apps.folder",
                                       "parents": [parent], "trashed": False}
            parent = folder_id
        return parent

    def generate_id(self):
        with self.lock:
            self.generated += 1
            return f"test-file-{self.generated}"

    def start_upload(self, file_id, name, size, mime, parent):
        with self.lock:
            session_id = f"test-session-{len(self.started) + 1}"
            self.started.append(file_id)
            self.sessions[session_id] = {
                "file_id": file_id, "name": name, "size": size,
                "mime": mime, "parent": parent, "data": bytearray(),
            }
        return f"https://www.googleapis.com/upload/drive/v3/files?upload_id={session_id}"

    def get_file(self, file_id):
        with self.lock:
            result = self.files.get(file_id, self.folders.get(file_id))
            return dict(result) if result is not None else None

    def move_file_parent(self, file_id, old_parent, new_parent):
        assert self.files[file_id]["parents"] == [old_parent]
        self.files[file_id]["parents"] = [new_parent]

    def complete(self, file_id, data, **overrides):
        session = next((session for session in self.sessions.values() if session["file_id"] == file_id), None)
        metadata = {
            "id": file_id, "parents": [session["parent"] if session else self.folder_id], "size": str(len(data)),
            "md5Checksum": hashlib.md5(data).hexdigest(),
            "sha256Checksum": hashlib.sha256(data).hexdigest(), "trashed": False,
        }
        metadata.update(overrides)
        with self.lock:
            self.files[file_id] = metadata


class FakeUploadTransport:
    """Implements Drive's status query and Content-Range over an in-memory sink."""

    def __init__(self, drive):
        self.drive = drive
        self.calls = []
        self.fail_after_receiving_once = False
        self.corrupt_metadata = False
        self.on_data = None

    def put(self, url, data, content_range):
        self.calls.append((content_range, len(data)))
        key = parse_qs(urlsplit(url).query)["upload_id"][0]
        session = self.drive.sessions[key]
        if session["file_id"] in self.drive.files:
            return 200, {}
        if content_range.startswith("bytes */"):
            length = len(session["data"])
            return 308, ({"Range": f"bytes=0-{length-1}"} if length else {})
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
        if not match:
            raise AssertionError(f"Invalid Content-Range {content_range!r}")
        start, end, total = map(int, match.groups())
        assert start == len(session["data"]), "Client resumed at wrong offset"
        assert end - start + 1 == len(data)
        assert total == session["size"]
        session["data"].extend(data)
        if self.on_data:
            self.on_data()
        if len(session["data"]) == total:
            self.drive.complete(session["file_id"], bytes(session["data"]))
            if self.corrupt_metadata:
                self.drive.files[session["file_id"]]["md5Checksum"] = "0" * 32
        if self.fail_after_receiving_once:
            self.fail_after_receiving_once = False
            raise OSError("Simulated connection dropped after Google received bytes")
        if len(session["data"]) == total:
            return 200, {}
        return 308, {"Range": f"bytes=0-{len(session['data'])-1}"}
