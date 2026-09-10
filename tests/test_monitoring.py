import json
import hashlib
import uuid
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from drivedrop.broker import Broker
from drivedrop.client import Client, ClientError
from drivedrop.common import ApiError, pinned_request
from drivedrop.monitoring import list_status, list_articles, validate_status
from tests.fakes import MemoryStore, FakeDrive

class MonitoringTests(unittest.TestCase):
    def test_new_client_reports_many_folders_over_authenticated_heartbeat(self):
        for i in range(160):
            folder=self.root/'watch'/f'Bài-{i:03}';folder.mkdir();(folder/'ảnh.jpg').write_bytes(b'abc')
        self.client.heartbeat()
        inv=list_status(self.broker)[0]['status']['inventory']
        self.assertEqual(len(inv['folders']),161)
        self.assertEqual(sum(f['images'] for f in inv['folders']),160)
        # Cached metadata is retained between 60-second scans, and refreshed afterwards.
        (self.root/'watch'/'new.mp4').write_bytes(b'123')
        self.client.heartbeat()
        self.assertEqual(list_status(self.broker)[0]['status']['inventory']['folders'][0]['videos'],0)
        self.client.inventory_next=0;self.client.heartbeat()
        self.assertEqual(list_status(self.broker)[0]['status']['inventory']['folders'][0]['videos'],1)

    def test_inventory_upgrade_falls_back_for_legacy_server(self):
        calls=[]
        def old_api(method,path,payload):
            calls.append(payload.copy())
            if 'inventory' in payload:raise ApiError('Old server',400)
            return {'ok':True}
        with patch.object(self.client,'_api',side_effect=old_api):
            self.assertTrue(self.client.heartbeat()['ok'])
        self.assertIn('inventory',calls[0]);self.assertNotIn('inventory',calls[1])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.broker = Broker(self.root / "boss", store=MemoryStore(), drive=FakeDrive())
        port = self.broker.start(host="127.0.0.1", port=0)
        self.base = f"https://127.0.0.1:{port}"
        self.client = Client(self.root / "client", store=MemoryStore())
        self.client.enroll(self.broker.create_enrollment(self.base), "MAC01")
        (self.root / "watch").mkdir()
        self.client.configure(self.root / "watch")
    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()
    def test_authenticated_progress_received_and_server_time_marks_stale(self):
        self.client.report_activity(state="uploading", file="TH9_002/clip.mp4", sent=5, size=10,
                                    destination="KÊNH 76 - TH9/VIDEO/TH9_002/clip.mp4")
        self.assertTrue(self.client.heartbeat()["ok"])
        row = list_status(self.broker)[0]
        self.assertEqual(row["status"]["sent"], 5)
        self.assertTrue(row["online"])
        with patch("drivedrop.monitoring.time.time", return_value=row["received"] + 61):
            self.assertFalse(list_status(self.broker)[0]["online"])
    def test_unknown_fields_and_invalid_progress_rejected(self):
        for values in ({"secret":"x"}, {"sent":11,"size":10}, {"queued":True}, {"error":"x"*301}, {"state":"fake"}):
            with self.subTest(values=values), self.assertRaises(ApiError):
                validate_status(dict({"state":"waiting"}, **values))

    def test_article_totals_come_from_verified_server_records(self):
        data = b"video"
        upload = self.client._api("POST","/uploads",{"request_id":str(uuid.uuid4()),"name":"clip.mp4",
            "source_folders":["TH9_002","VIDEO"],"size":len(data),
            "sha256":hashlib.sha256(data).hexdigest(),"md5":hashlib.md5(data).hexdigest()})
        row = list_articles(self.broker)[0]
        self.assertEqual(row["folder"], "KÊNH 76 - TH9/VIDEO/TH9_002")
        self.assertEqual((row["issued"], row["verified"]),(1,0))
        self.broker.drive.complete(upload["file_id"],data)
        self.client._api("POST","/verify",{"upload_id":upload["upload_id"]})
        self.assertEqual(list_articles(self.broker)[0]["verified"],1)
    def test_anonymous_and_revoked_heartbeat_cannot_update_status(self):
        with self.assertRaises(ApiError):
            pinned_request(self.base,self.broker.pin,"POST","/heartbeat",{"state":"waiting"})
        self.broker.revoke_device(self.client.config["device_id"])
        with self.assertRaises(ApiError):
            self.client.heartbeat()
        self.assertIsNone(list_status(self.broker)[0]["received"])
    def test_running_worker_holds_lock_during_idle_and_sends_stopped(self):
        stop = threading.Event()
        entered = threading.Event()
        other = Client(self.root / "client", store=self.client.store)
        with patch.object(self.client, "_run_once_locked", side_effect=lambda event: entered.set()):
            thread = threading.Thread(target=self.client.run,args=(stop,))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ClientError):
                    other.run_once()
            finally:
                stop.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(list_status(self.broker)[0]["status"]["state"], "stopped")
    def test_mac_agent_preserves_state_and_uses_stable_paths(self):
        from drivedrop import mac_background as bg
        original = self.client.config_path.read_bytes()
        (self.root / "Library").mkdir()
        with patch.object(bg.sys, "platform", "darwin"), patch.object(bg.os,"getuid",return_value=501,create=True), \
             patch.object(bg.Path,"home",return_value=self.root), patch.object(bg,"stop"), \
             patch.object(bg,"active",return_value=True), patch.object(bg.subprocess,"run") as run:
            path = bg.install(self.client)
            config = plistlib.loads(path.read_bytes())
            self.assertEqual(config["Label"], bg.LABEL)
            self.assertEqual(config["ProgramArguments"][-1], str(self.client.data_dir))
            self.assertTrue(config["RunAtLoad"])
            self.assertFalse(config["KeepAlive"]["SuccessfulExit"])
            self.assertEqual(config["Nice"], 10)
            entry = Path(config["ProgramArguments"][2])
            compile(entry.read_text(),str(entry),"exec")
            self.assertEqual(self.client.config_path.read_bytes(), original)
            self.assertEqual(run.call_args[0][0][:3],["/bin/launchctl","bootstrap","gui/501"])
    @unittest.skipUnless(sys.platform == "win32", "Windows kernel")
    def test_windows_second_instance_requests_existing_window(self):
        from drivedrop.desktop import Instance
        first = Instance(self.root)
        second = Instance(self.root)
        try:
            self.assertTrue(first.primary)
            self.assertFalse(second.primary)
            self.assertTrue(first.requested())
            self.assertFalse(first.requested())
        finally:
            second.close()
            first.close()

if __name__ == "__main__":
    unittest.main()
