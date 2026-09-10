"""OAuth account-binding regression tests. Browser and Google are explicitly simulated."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse
import urllib.request

from drivedrop.common import ApiError
from drivedrop.drive import ABOUT, TOKEN, GoogleDrive
from tests.fakes import MemoryStore


class GoogleAccountBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drivedrop-oauth-test-")
        self.root = Path(self.temp.name)
        self.store = MemoryStore()
        self.original_client = {"client_id": "original.apps.googleusercontent.com", "client_secret": "old-client-secret"}
        self.store.set("google-client", json.dumps(self.original_client))
        self.store.set("google-refresh", "old-refresh")
        self.store.set("google-account-id", "original-account")
        self.drive = GoogleDrive(self.root, self.store)
        self.drive.folder_id = "original-private-folder"
        self.drive.access = "old-access"
        self.candidate_user = {"permissionId": "original-account", "emailAddress": "owner@example.test"}
        self.browser_query = None
        self.callback_errors = []
        self.threads = []

    def tearDown(self):
        for thread in self.threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "OAuth loopback callback did not finish")
        self.assertEqual(self.callback_errors, [])
        self.temp.cleanup()

    def google_request(self, method, url, body=None, headers=None):
        if method == "POST" and url == TOKEN:
            return 200, {}, {"access_token": "candidate-access", "refresh_token": "candidate-refresh", "expires_in": 3600}
        if method == "GET" and url == ABOUT:
            self.assertEqual(headers["Authorization"], "Bearer candidate-access")
            return 200, {}, {"user": self.candidate_user}
        self.fail("Unexpected Google endpoint in offline test")

    def open_simulated_browser(self, url):
        self.browser_query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        redirect = self.browser_query["redirect_uri"][0]
        query = urllib.parse.urlencode({"code": "offline-authorization-code", "state": self.browser_query["state"][0]})
        def callback():
            try:
                with urllib.request.urlopen(redirect + "?" + query, timeout=3) as result:
                    self.assertEqual(result.status, 200)
                    result.read()
            except Exception as exc:
                self.callback_errors.append(type(exc).__name__)
        thread = threading.Thread(target=callback, daemon=True)
        self.threads.append(thread)
        thread.start()
        return True

    def authorize(self, expected_email=None):
        with patch("drivedrop.drive.webbrowser.open", side_effect=self.open_simulated_browser), \
                patch("drivedrop.drive.request", side_effect=self.google_request), \
                patch.object(self.drive, "ensure_folder", return_value="original-private-folder") as ensure:
            self.drive.authorize(timeout=4, expected_email=expected_email)
            return ensure.call_count

    def assert_original_state(self):
        self.assertEqual(self.store.get("google-refresh"), "old-refresh")
        self.assertEqual(self.store.get("google-account-id"), "original-account")
        self.assertEqual(self.drive.access, "old-access")
        self.assertEqual(self.drive.folder_id, "original-private-folder")

    def test_import_cannot_replace_oauth_client_after_account_linked(self):
        client_path = self.root / "different-client.json"
        client_path.write_text(json.dumps({"installed": {"client_id": "different.apps.googleusercontent.com", "client_secret": "different"}}), encoding="utf-8")
        with self.assertRaises(ApiError) as caught:
            self.drive.import_client(client_path)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(json.loads(self.store.get("google-client")), self.original_client)
        self.assert_original_state()

    def test_wrong_google_account_does_not_replace_existing_credentials(self):
        self.candidate_user["permissionId"] = "another-account"
        with self.assertRaises(ApiError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.status, 409)
        self.assert_original_state()

    def test_wrong_expected_email_does_not_replace_existing_credentials(self):
        with self.assertRaises(ApiError) as caught:
            self.authorize(expected_email="different-owner@example.test")
        self.assertEqual(caught.exception.status, 409)
        self.assert_original_state()
        self.assertEqual(self.browser_query["login_hint"], ["different-owner@example.test"])

    def test_missing_account_identifier_does_not_replace_credentials(self):
        self.candidate_user.pop("permissionId")
        with self.assertRaises(ApiError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.status, 502)
        self.assert_original_state()

    def test_same_account_reauthorization_retains_folder(self):
        ensure_count = self.authorize(expected_email=" OWNER@EXAMPLE.TEST ")
        self.assertEqual(ensure_count, 1)
        self.assertEqual(self.store.get("google-refresh"), "candidate-refresh")
        self.assertEqual(self.store.get("google-account-id"), "original-account")
        self.assertEqual(self.drive.folder_id, "original-private-folder")
        self.assertEqual(self.browser_query["scope"], ["https://www.googleapis.com/auth/drive.file"])
        self.assertEqual(self.browser_query["code_challenge_method"], ["S256"])


if __name__ == "__main__":
    unittest.main()
