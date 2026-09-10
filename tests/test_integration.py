"""Exercises a real localhost TLS broker, with Google replaced explicitly by FakeDrive."""
from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import time
import unittest
import uuid

from drivedrop.broker import Broker
from drivedrop.common import ApiError, canonical_json, pinned_request, sign_request
from tests.fakes import FakeDrive, MemoryStore


class BrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drivedrop-test-")
        self.state = Path(self.temp.name)
        self.drive = FakeDrive()
        self.store = MemoryStore()
        self.broker = Broker(self.state, store=self.store, drive=self.drive, route_channels=False)
        self.port = self.broker.start(host="127.0.0.1", port=0)
        self.base = f"https://127.0.0.1:{self.port}"
        self.enrollment = self.broker.create_enrollment(self.base, name="test")
        self.pin = self.enrollment["certificate_sha256"]
        self.device = self.enroll(self.enrollment)

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def enroll(self, config):
        return pinned_request(self.base, config["certificate_sha256"], "POST", "/enroll",
                              {"code": config["code"], "name": "Test employee"})

    def request(self, path, payload, device=None):
        device = device or self.device
        return pinned_request(self.base, self.pin, "POST", path, payload,
                              device_id=device["device_id"], secret=device["secret"])

    def upload_payload(self, data=b"sample-media", name="sample.jpg"):
        return {
            "request_id": str(uuid.uuid4()), "name": name, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "md5": hashlib.md5(data).hexdigest(),
        }

    def assert_api_rejected(self, callable_, allowed_statuses=(400, 401, 403, 409, 429)):
        with self.assertRaises(ApiError) as caught:
            callable_()
        self.assertIn(caught.exception.status, allowed_statuses)

    def test_health_uses_pinned_tls(self):
        result = pinned_request(self.base, self.pin, "GET", "/health")
        self.assertTrue(result["ok"])
        self.assertTrue(result["google_connected"])
        self.assert_api_rejected(
            lambda: pinned_request(self.base, "00" * 32, "GET", "/health"), (0, 401, 403))

    def test_stalled_tls_handshake_does_not_block_other_clients(self):
        # An accepted TCP peer sends no ClientHello. The accept loop must remain free.
        with socket.create_connection(("127.0.0.1", self.port), timeout=3):
            started = time.monotonic()
            result = pinned_request(self.base, self.pin, "GET", "/health")
            elapsed = time.monotonic() - started
        self.assertTrue(result["ok"])
        self.assertLess(elapsed, 3, "One stalled handshake blocked a healthy client")

    def test_existing_listener_collision_fails_without_replacing_listener(self):
        other_broker = Broker(self.state / "collision-broker", store=MemoryStore(), drive=FakeDrive(), route_channels=False)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as existing:
                existing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                existing.bind(("127.0.0.1", 0))
                existing.listen(1)
                occupied_port = existing.getsockname()[1]
                with self.assertRaises(OSError):
                    other_broker.start(host="127.0.0.1", port=occupied_port)
                self.assertIsNone(other_broker.server)
                with socket.create_connection(("127.0.0.1", occupied_port), timeout=3):
                    connection, _ = existing.accept()
                    connection.close()
        finally:
            other_broker.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows exclusive address binding")
    def test_windows_reuse_address_socket_cannot_take_broker_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conflicting:
            conflicting.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                conflicting.bind(("127.0.0.1", self.port))
        self.assertTrue(pinned_request(self.base, self.pin, "GET", "/health")["ok"])

    def test_enrollment_code_single_use(self):
        self.assert_api_rejected(lambda: self.enroll(self.enrollment))
        self.assertEqual(len(self.broker.list_devices()), 1)

    def test_unknown_credentials_and_revoked_device_cannot_upload(self):
        bad = dict(self.device, secret="not-the-device-secret")
        self.assert_api_rejected(lambda: self.request("/uploads", self.upload_payload(), bad))
        self.broker.revoke_device(self.device["device_id"])
        self.assert_api_rejected(lambda: self.request("/uploads", self.upload_payload()))
        self.assertEqual(self.drive.started, [])

    def test_request_replay_is_rejected_over_actual_https(self):
        payload = self.upload_payload()
        body = canonical_json(payload)
        timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
        headers = {
            "Content-Type": "application/json", "X-Device-Id": self.device["device_id"],
            "X-Timestamp": timestamp, "X-Nonce": nonce,
            "X-Signature": sign_request(self.device["secret"], "POST", "/uploads", timestamp, nonce, body),
        }
        statuses = []
        for _ in range(2):
            conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ssl._create_unverified_context(), timeout=5)
            try:
                conn.connect()
                self.assertEqual(hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest(), self.pin)
                conn.request("POST", "/uploads", body=body, headers=headers)
                response = conn.getresponse()
                statuses.append(response.status)
                response.read()
            finally:
                conn.close()
        self.assertIn(statuses[0], (200, 201))
        self.assertIn(statuses[1], (401, 403, 409))
        self.assertEqual(len(self.drive.started), 1)

    def test_same_request_is_idempotent_and_changed_metadata_rejected(self):
        payload = self.upload_payload()
        first = self.request("/uploads", payload)
        again = self.request("/uploads", payload)
        self.assertEqual(first["upload_id"], again["upload_id"])
        self.assertEqual(first["file_id"], again["file_id"])
        self.assertEqual(len(self.drive.started), 1)
        changed = dict(payload, name="changed.jpg")
        self.assert_api_rejected(lambda: self.request("/uploads", changed))

    def test_quota_reserves_pending_bytes(self):
        self.broker.daily_limit_bytes = 15
        self.request("/uploads", self.upload_payload(b"1234567890"))
        self.assert_api_rejected(lambda: self.request("/uploads", self.upload_payload(b"1234567890")))
        self.assertEqual(len(self.drive.started), 1)

    def test_non_media_and_traversal_are_rejected(self):
        for name in ("program.exe", "../escape.jpg", "..\\escape.jpg", "x.jpg/other", "x.tmp"):
            with self.subTest(name=name):
                self.assert_api_rejected(lambda: self.request("/uploads", self.upload_payload(name=name)))
        self.assertEqual(self.drive.started, [])

    def test_verify_requires_correct_server_known_file_metadata(self):
        data = b"independently-checked-video"
        upload = self.request("/uploads", self.upload_payload(data, "sample.mp4"))
        verify = lambda: self.request("/verify", {"upload_id": upload["upload_id"]})
        self.assert_api_rejected(verify, (409,))
        for overrides in ({"size": "1"}, {"parents": ["wrong-folder"]},
                          {"md5Checksum": "0" * 32}, {"sha256Checksum": "0" * 64},
                          {"trashed": True}):
            with self.subTest(overrides=overrides):
                self.drive.complete(upload["file_id"], data, **overrides)
                self.assert_api_rejected(verify, (409,))
        self.drive.complete(upload["file_id"], data)
        result = verify()
        self.assertTrue(result["verified"])
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())

    def test_device_cannot_verify_another_devices_upload(self):
        upload = self.request("/uploads", self.upload_payload())
        another = self.enroll(self.broker.create_enrollment(self.base))
        self.assert_api_rejected(lambda: self.request("/verify", {"upload_id": upload["upload_id"]}, another), (403, 404))

    def test_restart_reuses_file_id_without_duplicate_success(self):
        data = b"video-restart"
        upload = self.request("/uploads", self.upload_payload(data))
        restarted = self.request("/uploads/restart", {"upload_id": upload["upload_id"]})
        self.assertEqual(restarted["file_id"], upload["file_id"])
        self.assertEqual(self.drive.generated, 1)
        self.drive.complete(upload["file_id"], data)
        final = self.request("/uploads/restart", {"upload_id": upload["upload_id"]})
        self.assertEqual(final["state"], "verified")
        self.assertEqual(self.drive.generated, 1)

    def test_broker_restart_preserves_request_and_session(self):
        payload = self.upload_payload()
        first = self.request("/uploads", payload)
        original_pin = self.pin
        self.broker.close()
        self.broker = Broker(self.state, store=self.store, drive=self.drive, route_channels=False)
        self.port = self.broker.start(host="127.0.0.1", port=0)
        self.base = f"https://127.0.0.1:{self.port}"
        self.assertEqual(self.broker.pin, original_pin)
        again = self.request("/uploads", payload)
        self.assertEqual(again["file_id"], first["file_id"])
        self.assertEqual(again["session_url"], first["session_url"])
        self.assertEqual(len(self.drive.started), 1)


if __name__ == "__main__":
    unittest.main()
