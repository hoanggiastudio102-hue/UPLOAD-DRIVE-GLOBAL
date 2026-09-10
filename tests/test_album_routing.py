"""Article-folder routing, immutable upload context, and bulk employee workflow."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from drivedrop.broker import Broker
from drivedrop.channels import ChannelCatalog
from drivedrop.client import Client
from drivedrop.common import ApiError, pinned_request
from tests.fakes import FakeDrive, FakeUploadTransport, MemoryStore


class AlbumCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drivedrop-album-catalog-")
        self.catalog = ChannelCatalog(Path(self.temp.name) / "channels.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_image_folder_wins_over_filename_and_preserves_nested_relative_path(self):
        result = self.catalog.route_context("HG23_001.jpg", "ANH", ["incoming", "kl1_001", "edited", "web"])
        self.assertEqual(result["code"], "KL1")
        self.assertEqual(result["article"], "KL1_001")
        self.assertEqual(result["subfolders"], ["edited", "web"])

    def test_nearest_exact_article_folder_wins_including_watch_root(self):
        direct = self.catalog.route_context("001.jpg", "ANH", ["KL1_001"])
        self.assertEqual(direct["article"], "KL1_001")
        self.assertEqual(direct["subfolders"], [])
        nested = self.catalog.route_context("001.jpg", "ANH", ["KL1_001", "HG23_007", "retouch"])
        self.assertEqual(nested["article"], "HG23_007")
        self.assertEqual(nested["subfolders"], ["retouch"])
        self.assertIsNone(self.catalog.route_context("001.jpg", "ANH", ["KL1_001_extra"]))

    def test_video_folder_wins_over_filename_and_preserves_subfolders(self):
        explicit = self.catalog.route_context("HG23_010.mp4", "VIDEO", ["KL1_001", "assets"])
        self.assertEqual(explicit["code"], "KL1")
        self.assertEqual(explicit["article"], "KL1_001")
        self.assertEqual(explicit["subfolders"], ["assets"])
        inherited = self.catalog.route_context("clip.mp4", "VIDEO", ["KL1_001", "assets"])
        self.assertEqual(inherited["code"], "KL1")
        self.assertEqual(inherited["article"], "KL1_001")
        self.assertEqual(inherited["subfolders"], ["assets"])

    def test_video_article_prefix_boundary_and_internal_underscore(self):
        for filename, article in (("TH9_001.mp4", "TH9_001"),
                                  ("th9_002_part_1.mp4", "TH9_002"),
                                  ("RM4_9_003 final.mp4", "RM4_9_003")):
            self.assertEqual(self.catalog.route_context(filename, "VIDEO", [])["article"], article)
        self.assertIsNone(self.catalog.route_context("TH9_001x.mp4", "VIDEO", []))
        matched = self.catalog.route_context("TH9_001_part.mp4", "VIDEO", ["th9_001", "cuts"])
        self.assertEqual(matched["subfolders"], ["cuts"])

    def test_source_media_container_is_not_repeated_inside_article(self):
        for kind, label in (("VIDEO", "VIDEO"), ("ANH", "ANH"), ("ANH", "ẢNH")):
            route = self.catalog.route_context("clip.mp4" if kind == "VIDEO" else "anh.jpg", kind,
                                               ["VIDEO", "KÊNH 76", "TH9_002", label, "cuts"])
            self.assertEqual(route["article"], "TH9_002")
            self.assertEqual(route["subfolders"], ["cuts"])

    def test_disabled_nearest_article_or_disabled_video_prefix_falls_back(self):
        channel = next(row for row in self.catalog.list_channels() if row["code"] == "HG23")
        self.catalog.set_enabled(channel["id"], False)
        self.assertIsNone(self.catalog.route_context("001.jpg", "ANH", ["KL1_001", "HG23_002"]))
        self.assertEqual(self.catalog.route_context("HG23_010.mp4", "VIDEO", ["KL1_001"])["article"], "KL1_001")
        self.assertIsNone(self.catalog.route_context("KL1_010.mp4", "VIDEO", ["HG23_002"]))


class AlbumBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drivedrop-album-broker-")
        self.root = Path(self.temp.name)
        self.store = MemoryStore()
        self.drive = FakeDrive()
        self.broker = Broker(self.root / "boss", store=self.store, drive=self.drive)
        self.start_server()
        self.device = pinned_request(self.base, self.pin, "POST", "/enroll", {
            "code": self.broker.create_enrollment(self.base)["code"], "name": "Album tests"})

    def start_server(self):
        port = self.broker.start(host="127.0.0.1", port=0)
        self.base = f"https://127.0.0.1:{port}"
        self.pin = self.broker.pin

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def request(self, path, payload):
        return pinned_request(self.base, self.pin, "POST", path, payload,
                              device_id=self.device["device_id"], secret=self.device["secret"])

    def payload(self, filename="001.jpg", folders=("incoming", "KL1_001")):
        data = b"closed-article-photo"
        result = {"request_id": str(uuid.uuid4()), "name": filename, "size": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "md5": hashlib.md5(data).hexdigest()}
        if folders is not None:
            result["source_folders"] = list(folders)
        return result

    def session(self, file_id):
        return next(row for row in reversed(list(self.drive.sessions.values())) if row["file_id"] == file_id)

    def finish(self, upload):
        self.drive.complete(upload["file_id"], b"closed-article-photo")
        return self.request("/verify", {"upload_id": upload["upload_id"]})

    def test_two_albums_with_same_filename_have_distinct_parents_and_ids(self):
        first = self.request("/uploads", self.payload(folders=["incoming", "KL1_001"]))
        second = self.request("/uploads", self.payload(folders=["incoming", "KL1_002"]))
        self.assertNotEqual(first["file_id"], second["file_id"])
        self.assertNotEqual(self.session(first["file_id"])["parent"], self.session(second["file_id"])["parent"])
        self.assertEqual(self.session(first["file_id"])["name"], "001.jpg")
        self.assertEqual(self.session(second["file_id"])["name"], "001.jpg")
        self.assertTrue(self.finish(first)["verified"])
        self.assertTrue(self.finish(second)["verified"])

    def test_watch_root_album_and_nested_subfolders_appear_in_destination(self):
        upload = self.request("/uploads", self.payload(folders=["kl1_001", "edited", "web"]))
        self.assertEqual(upload["destination"], "KÊNH 01 - KL1/ANH/KL1_001/edited/web/001.jpg")
        self.assertEqual(self.drive.article_folder_calls[-1][3:], ("KL1_001", ("edited", "web")))
        self.assertTrue(self.finish(upload)["verified"])

    def test_legacy_clients_keep_filename_routing_and_unknown_folders_use_flat_fallback(self):
        legacy = self.request("/uploads", self.payload("KL1_007.jpg", folders=None))
        self.assertEqual(legacy["destination"], "KÊNH 01 - KL1/ANH/KL1_007.jpg")
        self.assertEqual(self.drive.article_folder_calls, [])
        self.finish(legacy)
        unknown = self.request("/uploads", self.payload(folders=["incoming", "UNKNOWNQA_001", "retouch"]))
        self.assertEqual(unknown["destination"], "KÊNH/001.jpg")
        self.assertEqual(self.session(unknown["file_id"])["parent"], "test-unclassified-folder")
        self.finish(unknown)

    def test_videos_use_article_folder_for_explicit_or_inherited_code(self):
        for name, expected in (("HG23_011.mp4", "KÊNH 01 - KL1/VIDEO/KL1_001/HG23_011.mp4"),
                               ("clip.mp4", "KÊNH 01 - KL1/VIDEO/KL1_001/clip.mp4")):
            with self.subTest(name=name):
                upload = self.request("/uploads", self.payload(name))
                self.assertEqual(upload["destination"], expected)
                parent = self.session(upload["file_id"])["parent"]
                media_parent = self.drive.folders[parent]["parents"][0]
                self.assertEqual(self.drive.folders[media_parent]["name"], "VIDEO")
                self.finish(upload)

    def test_same_video_names_in_two_articles_remain_separate(self):
        parents = []
        for article in ("TH9_001", "TH9_002"):
            upload = self.request("/uploads", self.payload("1.1.1.mp4", [article]))
            self.assertEqual(upload["destination"], f"KÊNH 76 - TH9/VIDEO/{article}/1.1.1.mp4")
            parents.append(self.session(upload["file_id"])["parent"])
            self.finish(upload)
        self.assertNotEqual(*parents)

    def old_video(self, article="TH9_001", verified=True):
        original = self.broker.channels.route_context
        def old_route(*args):
            result = original(*args)
            if result:
                result.pop("article", None)
                result.pop("subfolders", None)
            return result
        with patch.object(self.broker.channels, "route_context", side_effect=old_route):
            upload = self.request("/uploads", self.payload("1.1.1.mp4", [article]))
        if verified:
            self.finish(upload)
        return upload

    def test_regroup_only_verified_videos_for_requested_article_and_is_idempotent(self):
        old = self.old_video()
        other = self.old_video("TH9_002")
        pending = self.old_video(verified=False)
        old_parent = self.drive.files[old["file_id"]]["parents"][:]
        self.assertEqual(self.broker.regroup_verified_videos("TH9_001"), 1)
        self.assertNotEqual(self.drive.files[old["file_id"]]["parents"], old_parent)
        self.assertEqual(self.drive.files[other["file_id"]]["parents"], old_parent)
        self.assertEqual(self.broker.regroup_verified_videos("TH9_001"), 0)
        self.assertTrue(self.request("/verify", {"upload_id": old["upload_id"]})["verified"])
        self.assertEqual(self.session(pending["file_id"])["parent"], old_parent[0])

    def test_regroup_checks_content_and_recovers_after_remote_move_response_loss(self):
        old = self.old_video()
        self.drive.files[old["file_id"]]["sha256Checksum"] = "changed"
        with self.assertRaises(ApiError):
            self.broker.regroup_verified_videos("TH9_001")
        self.drive.files[old["file_id"]]["sha256Checksum"] = hashlib.sha256(b"closed-article-photo").hexdigest()
        real_move = self.drive.move_file_parent
        def lost(*args):
            real_move(*args)
            raise ApiError("lost response", 503)
        with patch.object(self.drive, "move_file_parent", side_effect=lost):
            with self.assertRaises(ApiError):
                self.broker.regroup_verified_videos("TH9_001")
        self.assertEqual(self.broker.regroup_verified_videos("TH9_001"), 1)
        self.assertTrue(self.request("/verify", {"upload_id": old["upload_id"]})["verified"])

    def test_admin_confirmed_batch_can_assign_article_but_never_pending_uploads(self):
        old = self.old_video("TH9_002")
        pending = self.old_video("TH9_002", verified=False)
        self.assertEqual(self.broker.regroup_verified_videos("TH9_001", require_source_match=False), 1)
        self.assertEqual(self.request("/verify", {"upload_id": old["upload_id"]})["verified"], True)
        row = next(r for r in self.broker.list_uploads() if r["id"] == old["upload_id"])
        self.assertEqual(row["destination"], "KÊNH 76 - TH9/VIDEO/TH9_001/1.1.1.mp4")
        self.assertEqual(self.broker.regroup_verified_videos("TH9_001", require_source_match=False), 0)
        self.assertEqual(self.broker.db.execute("SELECT state FROM uploads WHERE id=?", (pending["upload_id"],)).fetchone()[0], "pending")

    def test_legacy_named_video_uses_article_and_unknown_video_uses_fallback(self):
        video = self.request("/uploads", self.payload("TH9_001.mp4", None))
        self.assertEqual(video["destination"], "KÊNH 76 - TH9/VIDEO/TH9_001/TH9_001.mp4")
        self.finish(video)
        unknown = self.request("/uploads", self.payload("clip.mp4", ["unknown"]))
        self.assertEqual(unknown["destination"], "KÊNH/clip.mp4")
        self.finish(unknown)

    def test_invalid_context_has_no_drive_or_quota_side_effect(self):
        for context in (None, "KL1_001", {"folder": "KL1_001"}, [None], [".."], ["."],
                        ["/absolute"], ["a\\b"], ["bad\x00name"], ["x" * 201], ["x"] * 33):
            with self.subTest(context=context):
                payload = self.payload()
                payload["source_folders"] = context
                with self.assertRaises(ApiError) as caught:
                    self.request("/uploads", payload)
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.drive.generated, 0)
        self.assertEqual(self.drive.ensure_folder_calls, 0)
        self.assertEqual(self.broker.list_uploads(), [])

    def test_replay_cannot_change_add_or_drop_original_context(self):
        payload = self.payload()
        first = self.request("/uploads", payload)
        self.assertEqual(self.request("/uploads", payload)["file_id"], first["file_id"])
        changed = dict(payload, source_folders=["incoming", "KL1_002"])
        absent = dict(payload)
        del absent["source_folders"]
        for altered in (changed, absent):
            with self.subTest(payload=altered):
                with self.assertRaises(ApiError) as caught:
                    self.request("/uploads", altered)
                self.assertEqual(caught.exception.status, 409)
        self.finish(first)
        old = self.payload("KL1_005.jpg", folders=None)
        self.request("/uploads", old)
        with self.assertRaises(ApiError) as caught:
            self.request("/uploads", dict(old, source_folders=[]))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.drive.generated, 2)

    def test_restart_pins_article_parent_even_after_catalog_edit_and_restart(self):
        payload = self.payload(folders=["incoming", "KL1_001", "edited"])
        first = self.request("/uploads", payload)
        original_parent = self.session(first["file_id"])["parent"]
        channel = next(row for row in self.broker.channels.list_channels() if row["code"] == "KL1")
        self.broker.channels.upsert("Đã đổi mã", "CHANGEDQA", channel_id=channel["id"])
        self.broker.channels.upsert("Kênh khác", "KL1")
        self.broker.close()
        self.broker = Broker(self.root / "boss", store=self.store, drive=self.drive)
        self.start_server()
        again = self.request("/uploads", payload)
        restarted = self.request("/uploads/restart", {"upload_id": first["upload_id"]})
        self.assertEqual(again["file_id"], first["file_id"])
        self.assertEqual(restarted["destination"], first["destination"])
        self.assertEqual(self.session(first["file_id"])["parent"], original_parent)
        self.assertEqual(len(self.drive.article_folder_calls), 1)
        self.assertTrue(self.finish(first)["verified"])

    def test_employee_uploads_200_generic_photos_to_one_album_and_deletes_after_verify(self):
        watch = self.root / "incoming"
        album = watch / "KL1_001"
        album.mkdir(parents=True)
        expected_names = {f"IMG_{index:04d}.jpg" for index in range(200)}
        for name in expected_names:
            (album / name).write_bytes(("closed-photo:" + name).encode())
        transport = FakeUploadTransport(self.drive)
        client = Client(self.root / "client", store=MemoryStore(), upload_transport=transport, log=lambda message: None)
        try:
            client.enroll(self.broker.create_enrollment(self.base), "200-photo employee")
            client.configure(str(watch), delete_after_verify=True, stable_seconds=0)
            for _ in range(3):
                client.run_once()
            self.assertEqual(len(self.drive.files), 200)
            self.assertEqual(self.drive.generated, 200)
            self.assertEqual({session["name"] for session in self.drive.sessions.values()}, expected_names)
            parents = {session["parent"] for session in self.drive.sessions.values()}
            self.assertEqual(len(parents), 1)
            self.assertEqual(self.drive.folders[parents.pop()]["name"], "KL1_001")
            self.assertEqual(list(watch.rglob("*.jpg")), [])
            self.assertEqual(self.drive.unclassified_folder_calls, 0)
        finally:
            client.close()

    def test_employee_recursive_156_and_236_videos_and_unknown_fallback(self):
        watch = self.root / "VIDEO"
        counts = {"TH9_001": 156, "TH9_002": 236, "TH9_003": 2, "UNKNOWN_001": 1}
        for article, count in counts.items():
            folder = watch / "KÊNH 76" / article / "VIDEO"
            folder.mkdir(parents=True)
            for index in range(count):
                (folder / f"clip_{index:03}.mp4").write_bytes(f"closed:{article}:{index}".encode())
        client = Client(self.root / "bulk-client", store=MemoryStore(),
                        upload_transport=FakeUploadTransport(self.drive), log=lambda message: None)
        try:
            enrolled = client.enroll(self.broker.create_enrollment(self.base), "recursive-video-test")
            client.configure(watch, delete_after_verify=False, stable_seconds=0)
            client.run_once()
            client.run_once()
            rows = self.broker.db.execute("""SELECT r.destination,u.parent,u.state FROM uploads u
                JOIN upload_routes r ON r.upload_id=u.id WHERE u.device=?""", (enrolled["device_id"],)).fetchall()
            self.assertEqual(len(rows), sum(counts.values()))
            for article, count in counts.items():
                prefix = f"KÊNH 76 - TH9/VIDEO/{article}/" if article.startswith("TH9_") else "KÊNH/"
                matches = [r for r in rows if r["destination"].startswith(prefix)]
                self.assertEqual(len(matches), count)
                self.assertTrue(all(r["state"] == "verified" for r in matches))
                self.assertEqual(len({r["parent"] for r in matches}), 1)
            self.assertEqual(len({r["parent"] for r in rows}), 4)
            self.assertEqual(len(list(watch.rglob("*.mp4"))), sum(counts.values()))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
