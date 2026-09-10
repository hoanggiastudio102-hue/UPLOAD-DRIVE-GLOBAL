"""Channel rules and actual TLS/client workflows; Google is an explicit fake."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid

from drivedrop.broker import Broker
from drivedrop.channels import ChannelCatalog
from drivedrop.client import Client
from drivedrop.common import ApiError, pinned_request
from tests.fakes import FakeDrive, FakeUploadTransport, MemoryStore


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drivedrop-catalog-")
        self.path = Path(self.temp.name) / "channels.json"
        self.catalog = ChannelCatalog(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_has_57_unique_codes_and_normalizes_trailing_underscore(self):
        rows = self.catalog.list_channels()
        self.assertEqual(len(rows), 57)
        self.assertEqual(len({row["id"] for row in rows}), 57)
        self.assertEqual(len({row["code"] for row in rows}), 57)
        self.assertIn("NA13", {row["code"] for row in rows})
        self.assertNotIn("NA13_", {row["code"] for row in rows})
        self.assertEqual(ChannelCatalog(self.path).list_channels(), rows)

    def test_case_insensitive_exact_code_and_post_number_boundaries(self):
        channel = self.catalog.upsert("Kênh thử", "UNITQA")
        for filename in ("UNITQA_1.jpg", "unitqa_001_scene.JPG", "UNITQA_7-preview.png", "UNITQA_9 scene.mp4"):
            with self.subTest(filename=filename):
                route = self.catalog.route(filename, "VIDEO" if filename.endswith(".mp4") else "ANH")
                self.assertEqual(route["id"], channel["id"])
                self.assertEqual(route["filename"], filename)
        for filename in ("XUNITQA_1.jpg", "UNITQA_1x.jpg", "UNITQA_x1.jpg", "UNITQA_.jpg", "photo.jpg", "UNKNOWN_1.mp4"):
            with self.subTest(filename=filename):
                self.assertIsNone(self.catalog.route(filename, "ANH"))

    def test_disabled_channel_falls_back_and_can_be_reenabled_persistently(self):
        channel = self.catalog.upsert("Tạm dừng", "PAUSEQA_")
        self.assertEqual(channel["code"], "PAUSEQA")
        self.catalog.set_enabled(channel["id"], False)
        self.assertIsNone(self.catalog.route("PAUSEQA_01.jpg", "ANH"))
        catalog = ChannelCatalog(self.path)
        self.assertIsNone(catalog.route("PAUSEQA_01.jpg", "ANH"))
        catalog.set_enabled(channel["id"], True)
        self.assertEqual(catalog.route("PAUSEQA_01.jpg", "ANH")["id"], channel["id"])

    def test_unsafe_filename_or_media_kind_still_rejected(self):
        for filename, kind in (("../NA13_1.jpg", "ANH"), ("NA13_1\x00.jpg", "ANH"), ("NA13_1.jpg", "DOCUMENT")):
            with self.subTest(filename=filename, kind=kind):
                with self.assertRaises(ApiError):
                    self.catalog.route(filename, kind)

    def test_longest_code_wins_and_disabled_longest_does_not_leak_to_shorter(self):
        shorter = self.catalog.upsert("Ngắn", "OVERQA")
        longer = self.catalog.upsert("Dài", "OVERQA_12")
        self.assertEqual(self.catalog.route("OVERQA_12_34.jpg", "ANH")["id"], longer["id"])
        self.assertEqual(self.catalog.route("OVERQA_12.jpg", "ANH")["id"], shorter["id"])
        self.catalog.set_enabled(longer["id"], False)
        self.assertIsNone(self.catalog.route("OVERQA_12_34.jpg", "ANH"))

    def test_import_is_atomic_and_renaming_retains_channel_identity_and_disabled_state(self):
        existing = self.catalog.upsert("Tên cũ", "IMPORTQA")
        self.catalog.set_enabled(existing["id"], False)
        before = self.path.read_bytes()
        with self.assertRaises(ApiError):
            self.catalog.import_text("Kênh mới\tNEWQA\nTrùng mã\tnewqa_\n")
        self.assertEqual(self.path.read_bytes(), before)
        result = self.catalog.import_text("Tên mới\tIMPORTQA\nKênh thêm\tNEWQA\n")
        self.assertEqual(result, {"added": 1, "updated": 1})
        changed = next(row for row in self.catalog.list_channels() if row["code"] == "IMPORTQA")
        self.assertEqual(changed["id"], existing["id"])
        self.assertEqual(changed["name"], "Tên mới")
        self.assertFalse(changed["enabled"])


class ChannelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drivedrop-routing-")
        self.root = Path(self.temp.name)
        self.store = MemoryStore()
        self.drive = FakeDrive()
        self.broker = Broker(self.root / "boss", store=self.store, drive=self.drive)
        port = self.broker.start(host="127.0.0.1", port=0)
        self.base = f"https://127.0.0.1:{port}"
        self.enrollment = self.broker.create_enrollment(self.base)
        self.pin = self.enrollment["certificate_sha256"]
        self.device = pinned_request(self.base, self.pin, "POST", "/enroll", {"code": self.enrollment["code"], "name": "Routing test"})
        self.channel = self.broker.channels.upsert("Kênh kiểm thử", "ROUTEQA")

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def request(self, path, payload):
        return pinned_request(self.base, self.pin, "POST", path, payload, device_id=self.device["device_id"], secret=self.device["secret"])

    def payload(self, filename, data=b"closed-test-media"):
        return {"request_id": str(uuid.uuid4()), "name": filename, "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "md5": hashlib.md5(data).hexdigest()}

    def session(self, file_id):
        return next(session for session in reversed(list(self.drive.sessions.values())) if session["file_id"] == file_id)

    def finish(self, upload, data=b"closed-test-media"):
        self.drive.complete(upload["file_id"], data)
        return self.request("/verify", {"upload_id": upload["upload_id"]})

    def test_image_video_split_and_original_filename_preserved(self):
        for filename, kind in (("ROUTEQA_001_scene.jpg", "ANH"), ("routeqa_002_scene.MP4", "VIDEO")):
            with self.subTest(filename=filename):
                upload = self.request("/uploads", self.payload(filename))
                session = self.session(upload["file_id"])
                folder_ids = self.drive.channel_folders[self.channel["id"]]
                if kind == "VIDEO":
                    self.assertEqual(self.drive.folders[session["parent"]]["parents"], [folder_ids[kind]])
                else:
                    self.assertEqual(session["parent"], folder_ids[kind])
                self.assertEqual(session["name"], filename)
                article_path = "ROUTEQA_002/" if kind == "VIDEO" else ""
                self.assertEqual(upload["destination"], f"Kênh kiểm thử - ROUTEQA/{kind}/{article_path}{filename}")
                self.assertTrue(self.finish(upload)["verified"])
        rows = self.broker.list_uploads()
        self.assertEqual({row["media_kind"] for row in rows}, {"ANH", "VIDEO"})
        self.assertEqual({row["channel_code"] for row in rows}, {"ROUTEQA"})

    def test_missing_unknown_malformed_and_disabled_codes_upload_to_shared_folder(self):
        self.broker.channels.set_enabled(self.channel["id"], False)
        for filename in ("photo.jpg", "UNKNOWNQA_001.mp4", "ROUTEQA_1x.jpg", "ROUTEQA_001.jpg"):
            with self.subTest(filename=filename):
                upload = self.request("/uploads", self.payload(filename))
                self.assertEqual(self.session(upload["file_id"])["parent"], "test-unclassified-folder")
                self.assertEqual(self.session(upload["file_id"])["name"], filename)
                self.assertEqual(upload["destination"], "KÊNH/" + filename)
                self.assertTrue(self.finish(upload)["verified"])
        self.assertEqual(len(self.drive.files), 4)
        self.assertEqual(self.drive.channel_folder_calls, [])

    def test_invalid_path_still_rejected_without_drive_or_quota_side_effect(self):
        for filename in ("../ROUTEQA_1.jpg", "ROUTEQA_1.jpg\x00", "ROUTEQA_1.exe"):
            with self.subTest(filename=filename):
                with self.assertRaises(ApiError) as caught:
                    self.request("/uploads", self.payload(filename))
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.drive.generated, 0)
        self.assertEqual(self.drive.ensure_folder_calls, 0)
        self.assertEqual(self.broker.list_uploads(), [])

    def test_mapping_edits_do_not_redirect_existing_upload_or_retry(self):
        payload = self.payload("ROUTEQA_123.jpg")
        first = self.request("/uploads", payload)
        original_parent = self.session(first["file_id"])["parent"]
        self.broker.channels.upsert("Kênh cũ đổi mã", "MOVEDQA", channel_id=self.channel["id"])
        replacement = self.broker.channels.upsert("Kênh mới", "ROUTEQA")
        self.assertNotEqual(replacement["id"], self.channel["id"])
        again = self.request("/uploads", payload)
        restarted = self.request("/uploads/restart", {"upload_id": first["upload_id"]})
        self.assertEqual(first["upload_id"], again["upload_id"])
        self.assertEqual(first["file_id"], restarted["file_id"])
        self.assertEqual(restarted["destination"], first["destination"])
        self.assertEqual(self.session(first["file_id"])["parent"], original_parent)
        self.assertEqual(self.session(first["file_id"])["name"], payload["name"])
        self.assertEqual(len(self.drive.channel_folder_calls), 1)
        self.assertEqual(self.drive.generated, 1)
        new_upload = self.request("/uploads", self.payload("ROUTEQA_124.jpg"))
        new_parent = self.session(new_upload["file_id"])["parent"]
        self.assertNotEqual(new_parent, original_parent)
        self.drive.complete(first["file_id"], b"closed-test-media", parents=[new_parent])
        with self.assertRaises(ApiError) as caught:
            self.request("/verify", {"upload_id": first["upload_id"]})
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(self.finish(first)["verified"])

    def test_fallback_retry_stays_pinned_after_code_is_added_and_broker_restarts(self):
        payload = self.payload("LATERQA_1.jpg")
        first = self.request("/uploads", payload)
        self.broker.channels.upsert("Thêm sau", "LATERQA")
        self.broker.close()
        self.broker = Broker(self.root / "boss", store=self.store, drive=self.drive)
        port = self.broker.start(host="127.0.0.1", port=0)
        self.base = f"https://127.0.0.1:{port}"
        again = self.request("/uploads", payload)
        restarted = self.request("/uploads/restart", {"upload_id": first["upload_id"]})
        self.assertEqual(again["file_id"], first["file_id"])
        self.assertEqual(restarted["destination"], "KÊNH/" + payload["name"])
        self.assertEqual(self.session(first["file_id"])["parent"], "test-unclassified-folder")
        self.assertEqual(self.drive.channel_folder_calls, [])
        self.assertTrue(self.finish(first)["verified"])

    def test_client_fallback_deletes_only_after_matching_server_metadata(self):
        watch = self.root / "watch"
        watch.mkdir()
        transport = FakeUploadTransport(self.drive)
        client = Client(self.root / "client", store=MemoryStore(), upload_transport=transport, log=lambda message: None)
        try:
            client.enroll(self.broker.create_enrollment(self.base), "Fallback client")
            client.configure(str(watch), delete_after_verify=True, stable_seconds=0)
            data = b"do-not-delete-with-wrong-metadata"
            (watch / "no_code.jpg").write_bytes(data)
            transport.corrupt_metadata = True
            client.run_once()
            self.assertEqual(len(self.drive.files), 1)
            self.assertTrue(any(path.read_bytes() == data for path in watch.rglob("*.jpg")))
            self.assertEqual({session["parent"] for session in self.drive.sessions.values()}, {"test-unclassified-folder"})
            file_id = next(iter(self.drive.files))
            self.drive.complete(file_id, data)
            client.run_once()
            self.assertEqual(list(watch.rglob("*.jpg")), [])
            self.assertEqual(self.drive.generated, 1)
        finally:
            client.close()


class LegacyUploadMigrationTests(unittest.TestCase):
    def test_legacy_rows_keep_original_parent_and_device_prefixed_name(self):
        with tempfile.TemporaryDirectory(prefix="drivedrop-migration-") as directory:
            state = Path(directory)
            device = "a" * 32
            upload_id = "b" * 32
            filename = "NA13_001.jpg"
            data = b"legacy-in-flight-media"
            with sqlite3.connect(state / "broker.sqlite") as db:
                db.execute("""CREATE TABLE uploads(id TEXT PRIMARY KEY, device TEXT, request_id TEXT, file_id TEXT,
                    name TEXT, size INTEGER, sha256 TEXT, md5 TEXT, parent TEXT, created REAL, state TEXT,
                    UNIQUE(device,request_id))""")
                db.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,?)", (upload_id, device, str(uuid.uuid4()),
                    "legacy-drive-id", filename, len(data), hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest(),
                    "legacy-parent", time.time(), "pending"))
            db.close()
            drive = FakeDrive()
            broker = Broker(state, store=MemoryStore(), drive=drive)
            try:
                restarted = broker.restart_upload(device, {"upload_id": upload_id})
                session = next(iter(drive.sessions.values()))
                self.assertEqual(session["file_id"], "legacy-drive-id")
                self.assertEqual(session["parent"], "legacy-parent")
                self.assertEqual(session["name"], device[:8] + "_" + filename)
                self.assertEqual(drive.channel_folder_calls, [])
                self.assertNotIn("destination", restarted)
                drive.complete("legacy-drive-id", data)
                self.assertTrue(broker.verify_upload(device, {"upload_id": upload_id})["verified"])
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM upload_routes").fetchone()[0], 0)
            finally:
                broker.close()


if __name__ == "__main__":
    unittest.main()
