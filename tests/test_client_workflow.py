"""Real client + local HTTPS broker. Only Google's network transport is substituted."""
from __future__ import annotations

from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from drivedrop.broker import Broker
from drivedrop.client import CHUNK_SIZE, PENDING, Client, ClientError, GoogleUploadTransport, _validate_source_folders
from tests.fakes import FakeDrive, FakeUploadTransport, MemoryStore


class ClientWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drivedrop-workflow-")
        self.root = Path(self.temp.name)
        self.watch = self.root / "watch"
        self.watch.mkdir()
        self.drive = FakeDrive()
        self.broker = Broker(self.root / "boss", store=MemoryStore(), drive=self.drive, route_channels=False)
        self.port = self.broker.start(host="127.0.0.1", port=0)
        enrollment = self.broker.create_enrollment(f"https://127.0.0.1:{self.port}")
        self.transport = FakeUploadTransport(self.drive)
        self.client_store = MemoryStore()
        self.client = Client(self.root / "client", store=self.client_store,
                             upload_transport=self.transport, log=lambda message: None)
        self.client.enroll(enrollment, name="Integration employee")
        self.client.configure(str(self.watch), stable_seconds=0)

    def tearDown(self):
        self.client.close()
        self.broker.close()
        self.temp.cleanup()

    def scan(self, count=4):
        for _ in range(count):
            self.client.run_once()

    def media(self):
        return [p for p in self.watch.rglob("*") if p.is_file() and p.suffix.lower() in (".jpg", ".mp4")]

    def test_default_preserves_local_bytes_and_does_not_upload_twice(self):
        data = b"local-photo-for-preserve-test"
        source = self.watch / "photo.jpg"
        source.write_bytes(data)
        self.scan()
        self.assertEqual(len(self.drive.files), 1)
        self.assertEqual(len(self.drive.started), 1)
        self.assertTrue(any(p.read_bytes() == data for p in self.media()))
        self.scan()
        self.assertEqual(len(self.drive.started), 1)

    def test_opt_in_deletes_only_after_independent_verification(self):
        self.client.configure(str(self.watch), delete_after_verify=True, stable_seconds=0)
        (self.watch / "video.mp4").write_bytes(b"verified-video-data")
        self.scan()
        self.assertEqual(len(self.drive.files), 1)
        self.assertEqual(self.media(), [])

    def test_wrong_drive_hash_retains_local_file(self):
        data = b"keep-this-if-Google-metadata-does-not-match"
        self.transport.corrupt_metadata = True
        self.client.configure(str(self.watch), delete_after_verify=True, stable_seconds=0)
        (self.watch / "photo.jpg").write_bytes(data)
        self.scan()
        self.assertEqual(len(self.drive.files), 1)
        self.assertTrue(any(p.read_bytes() == data for p in self.media()))

    def test_changed_claimed_file_is_never_deleted(self):
        self.client.configure(str(self.watch), delete_after_verify=True, stable_seconds=0)
        (self.watch / "photo.jpg").write_bytes(b"first-closed-version")
        changed = b"producer-broke-contract-and-wrote-new-bytes"
        def modify_claimed_file():
            for path in self.media():
                path.write_bytes(changed)
            self.transport.on_data = None
        self.transport.on_data = modify_claimed_file
        self.scan()
        self.assertTrue(any(p.read_bytes() == changed for p in self.media()))

    def test_lost_final_response_recovers_without_duplicate(self):
        self.client.configure(str(self.watch), delete_after_verify=True, stable_seconds=0)
        self.transport.fail_after_receiving_once = True
        (self.watch / "photo.jpg").write_bytes(b"response-lost-after-upload")
        self.scan(6)
        self.assertEqual(len(self.drive.files), 1)
        self.assertEqual(len(self.drive.started), 1)
        self.assertEqual(self.media(), [])

    def test_multichunk_resume_starts_after_bytes_already_received(self):
        self.client.configure(str(self.watch), delete_after_verify=True, stable_seconds=0)
        self.transport.fail_after_receiving_once = True
        data = b"v" * (CHUNK_SIZE + 19)
        (self.watch / "large.mp4").write_bytes(data)
        self.scan(6)
        self.assertEqual(len(self.drive.started), 1)
        self.assertEqual(len(self.drive.files), 1)
        self.assertEqual(self.media(), [])
        data_ranges = [value for value, length in self.transport.calls if length]
        self.assertEqual(data_ranges, [f"bytes 0-{CHUNK_SIZE-1}/{len(data)}",
                                       f"bytes {CHUNK_SIZE}-{len(data)-1}/{len(data)}"])

    def test_journal_survives_client_restart(self):
        data = b"journal-restart-file"
        (self.watch / "photo.jpg").write_bytes(data)
        self.scan()
        self.client.close()
        self.client = Client(self.root / "client", store=self.client_store,
                             upload_transport=self.transport, log=lambda message: None)
        self.scan()
        self.assertEqual(len(self.drive.started), 1)
        self.assertTrue(any(p.read_bytes() == data for p in self.media()))

    def test_incomplete_extension_is_ignored(self):
        partial = self.watch / "photo.jpg.part"
        partial.write_bytes(b"still-copying")
        self.scan()
        self.assertTrue(partial.exists())
        self.assertEqual(self.drive.started, [])

    def _record_upload_payloads(self):
        payloads = []
        original = self.client._api
        def record(method, path, payload=None):
            if path == "/uploads":
                payloads.append(json.loads(json.dumps(payload)))
            return original(method, path, payload)
        self.client._api = record
        return payloads

    def test_recursive_duplicate_names_preserve_original_directories_and_context(self):
        sources = {self.watch / "KL1_001" / "photo.jpg": b"article-one",
                   self.watch / "KL1_002" / "edited" / "photo.jpg": b"article-two"}
        for path, data in sources.items():
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
        payloads = self._record_upload_payloads()
        self.scan()
        self.assertEqual(len(self.drive.started), 2)
        self.assertEqual([p["source_folders"] for p in payloads],
                         [["watch", "KL1_001"], ["watch", "KL1_002", "edited"]])
        for path, data in sources.items():
            self.assertEqual(path.read_bytes(), data)
        self.scan()
        self.assertEqual(len(self.drive.started), 2)

    def test_selected_article_folder_is_in_context_and_delete_keeps_directories(self):
        selected = self.watch / "KL1_001"
        source = selected / "edited" / "photo.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"delete-file-only")
        self.client.configure(str(selected), delete_after_verify=True, stable_seconds=0)
        payloads = self._record_upload_payloads()
        self.scan()
        self.assertEqual(payloads[0]["source_folders"], ["KL1_001", "edited"])
        self.assertEqual(len(self.drive.files), 1)
        self.assertFalse(source.exists())
        self.assertTrue(source.parent.is_dir())
        self.assertTrue(selected.is_dir())

    def test_nested_context_survives_restart_after_lost_response(self):
        source = self.watch / "KL1_002" / "edited" / "photo.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"nested-restart")
        self.transport.fail_after_receiving_once = True
        first = self._record_upload_payloads()
        self.assertEqual(self.client.run_once()["failed"], 1)
        with self.client._db() as db:
            context = db.execute("SELECT source_folders FROM job_context").fetchone()[0]
            self.assertEqual(json.loads(context), ["watch", "KL1_002", "edited"])
            self.assertEqual(len(db.execute("PRAGMA table_info(jobs)").fetchall()), 15)
        self.client.close()
        self.client = Client(self.root / "client", store=self.client_store,
                             upload_transport=self.transport, log=lambda message: None)
        retried = self._record_upload_payloads()
        self.scan()
        self.assertEqual(first, retried)
        self.assertEqual(len(self.drive.started), 1)
        self.assertEqual(source.read_bytes(), b"nested-restart")

    def test_nested_claim_intent_recovers_after_rename_before_journal_commit(self):
        source = self.watch / "KL1_001" / "photo.jpg"
        source.parent.mkdir()
        source.write_bytes(b"claim-journal-interruption")
        update = self.client._update
        def fail_claim_commit(request_id, **values):
            if values.get("status") == "claimed":
                raise OSError("Simulated interrupted claim commit")
            return update(request_id, **values)
        with patch.object(self.client, "_update", side_effect=fail_claim_commit), \
                patch.object(self.client, "_process", side_effect=OSError("Stopped before upload")):
            self.assertEqual(self.client.run_once()["failed"], 1)
        self.assertFalse(source.exists())
        self.assertEqual(self.drive.started, [])
        self.client.close()
        self.client = Client(self.root / "client", store=self.client_store,
                             upload_transport=self.transport, log=lambda message: None)
        payloads = self._record_upload_payloads()
        self.scan()
        self.assertEqual(payloads[0]["source_folders"], ["watch", "KL1_001"])
        self.assertEqual(source.read_bytes(), b"claim-journal-interruption")

    def test_legacy_job_without_context_omits_source_folders(self):
        source = self.watch / "photo.jpg"
        source.write_bytes(b"legacy-flat-pending-job")
        original = self.client._api
        def unavailable(method, path, payload=None):
            if path == "/uploads":
                raise OSError("Broker offline before first upload request")
            return original(method, path, payload)
        with patch.object(self.client, "_api", side_effect=unavailable):
            self.assertEqual(self.client.run_once()["failed"], 1)
        with self.client._db() as db:
            db.execute("DELETE FROM job_context")
        payloads = self._record_upload_payloads()
        self.scan()
        self.assertNotIn("source_folders", payloads[0])
        self.assertEqual(source.read_bytes(), b"legacy-flat-pending-job")

    def test_hidden_and_pending_descendants_are_not_claimed(self):
        ignored = [self.watch / ".hidden" / "photo.jpg",
                   self.watch / PENDING / "orphan" / "photo.jpg",
                   self.watch / "KL1_001" / PENDING / "photo.jpg",
                   self.watch / "KL1_001" / ".hidden.jpg"]
        for path in ignored:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"leave-hidden-media-alone")
        source = self.watch / "KL1_001" / "visible.jpg"
        source.write_bytes(b"visible")
        self.scan()
        self.assertEqual(len(self.drive.started), 1)
        self.assertEqual(source.read_bytes(), b"visible")
        for path in ignored:
            self.assertEqual(path.read_bytes(), b"leave-hidden-media-alone")

    def test_linked_directory_outside_root_is_not_traversed(self):
        outside = self.root / "outside"
        outside.mkdir()
        source = outside / "private.jpg"
        source.write_bytes(b"outside-watch-root")
        link = self.watch / "KL1_001"
        try:
            if sys.platform == "win32":
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            else:
                os.symlink(outside, link, target_is_directory=True)
        except (OSError, AttributeError):
            self.skipTest("Current OS account cannot create a test directory link")
        try:
            self.scan()
            self.assertEqual(self.drive.started, [])
            self.assertEqual(source.read_bytes(), b"outside-watch-root")
        finally:
            if sys.platform == "win32":
                link.rmdir()
            else:
                link.unlink()

    def test_nested_wrong_hash_does_not_delete_local_bytes(self):
        source = self.watch / "KL1_001" / "edited" / "photo.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"keep-nested-bad-receipt")
        self.transport.corrupt_metadata = True
        self.client.configure(str(self.watch), delete_after_verify=True, stable_seconds=0)
        self.scan()
        self.assertEqual(len(self.drive.files), 1)
        self.assertTrue(source.parent.is_dir())
        self.assertTrue(any(path.read_bytes() == b"keep-nested-bad-receipt" for path in self.media()))

    def test_old_broker_blocks_before_claim_and_does_not_touch_source(self):
        source = self.watch / "KL1_001" / "photo.jpg"
        source.parent.mkdir()
        source.write_bytes(b"must-not-claim-on-old-boss")
        original = self.client._api
        def old_health(method, path, payload=None):
            response = original(method, path, payload)
            if path == "/health":
                response.pop("album_routing", None)
            return response
        with patch.object(self.client, "_api", side_effect=old_health), self.assertRaises(ClientError):
            self.client.run_once()
        self.assertEqual(source.read_bytes(), b"must-not-claim-on-old-boss")
        self.assertFalse((self.watch / PENDING).exists())
        self.assertEqual(self.drive.started, [])
        with self.client._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_old_broker_also_blocks_existing_pending_upload(self):
        (self.watch / "photo.jpg").write_bytes(b"keep-existing-pending")
        self.transport.fail_after_receiving_once = True
        self.assertEqual(self.client.run_once()["failed"], 1)
        calls = list(self.transport.calls)
        original = self.client._api
        def old_health(method, path, payload=None):
            if path != "/health":
                self.fail("An old broker must not process an existing pending job")
            response = original(method, path, payload)
            response["album_routing"] = False
            return response
        with patch.object(self.client, "_api", side_effect=old_health), self.assertRaises(ClientError):
            self.client.run_once()
        self.assertEqual(self.transport.calls, calls)
        self.assertTrue(any(path.read_bytes() == b"keep-existing-pending" for path in self.media()))

    def _restart_after_final_journal_failure(self, delete):
        self.client.configure(str(self.watch), delete_after_verify=delete, stable_seconds=0)
        data = b"durable-final-operation"
        source = self.watch / "photo.jpg"
        source.write_bytes(data)
        update = self.client._update
        final_status = "deleted" if delete else "preserved"
        def fail_final_commit(request_id, **values):
            if values.get("status") == final_status:
                raise OSError("Simulated journal write failure after filesystem operation")
            return update(request_id, **values)
        with patch.object(self.client, "_update", side_effect=fail_final_commit):
            result = self.client.run_once()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(self.drive.files), 1)
        self.assertEqual(source.exists(), not delete)
        self.client.close()
        self.client = Client(self.root / "client", store=self.client_store,
                             upload_transport=self.transport, log=lambda message: None)
        self.scan()
        self.assertEqual(len(self.drive.started), 1)
        self.assertEqual(source.exists(), not delete)
        if not delete:
            self.assertEqual(source.read_bytes(), data)
        with self.client._db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()["status"], final_status)

    def test_restart_recovers_delete_after_file_removed_before_journal_commit(self):
        self._restart_after_final_journal_failure(delete=True)

    def test_restart_recovers_preserve_after_rename_before_journal_commit(self):
        self._restart_after_final_journal_failure(delete=False)

    def test_two_instances_cannot_process_same_journal_concurrently(self):
        second = Client(self.root / "client", store=self.client_store,
                        upload_transport=self.transport, log=lambda message: None)
        try:
            with self.client._process_lock(), self.assertRaises(ClientError):
                second.run_once()
        finally:
            second.close()
        self.assertEqual(self.drive.started, [])

    @unittest.skipUnless(sys.platform == "win32", "Windows writer-sharing guarantee")
    def test_open_writer_handle_blocks_deletion_until_closed(self):
        self.client.configure(str(self.watch), delete_after_verify=True, stable_seconds=0)
        data = b"writer-handle-must-block-delete"
        (self.watch / "photo.jpg").write_bytes(data)
        handles = []
        def hold_writer_open():
            handles.append(self.media()[0].open("r+b"))
            self.transport.on_data = None
        self.transport.on_data = hold_writer_open
        try:
            self.client.run_once()
            self.assertEqual(len(handles), 1)
            self.assertEqual(len(self.drive.files), 1)
            self.assertTrue(any(path.read_bytes() == data for path in self.media()))
        finally:
            for handle in handles:
                handle.close()
        self.scan()
        self.assertEqual(self.media(), [])


class GoogleTransportGuardTests(unittest.TestCase):
    def test_context_bounds_include_utf8_and_path_separators(self):
        self.assertEqual(_validate_source_folders(["KL1_001", "ảnh"]), ["KL1_001", "ảnh"])
        for context in (["a"] * 33, ["a" * 201], ["x" * 200] * 30,
                        ["ảnh" * 66] * 24, [".."], ["a/b"], ["a\\b"], ["x\x00y"]):
            with self.subTest(context=context), self.assertRaises(ClientError):
                _validate_source_folders(context)

    def test_non_google_session_urls_are_rejected_before_network(self):
        transport = GoogleUploadTransport()
        for url in (
            "http://www.googleapis.com/upload/drive/v3/files?upload_id=x",
            "https://evil.example/upload/drive/v3/files?upload_id=x",
            "https://www.googleapis.com.evil.example/upload/drive/v3/files?upload_id=x",
            "https://www.googleapis.com:8443/upload/drive/v3/files?upload_id=x",
            "https://user:password@www.googleapis.com/upload/drive/v3/files?upload_id=x",
            "https://www.googleapis.com/other-endpoint?upload_id=x",
        ):
            with self.subTest(url=url), self.assertRaises(ClientError):
                transport.put(url, b"", "bytes */1")


if __name__ == "__main__":
    unittest.main()
