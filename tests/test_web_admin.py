"""Real HTTP/TLS integration tests with explicit offline Drive and secret-store fakes."""
import hashlib
import http.client
import json
from pathlib import Path
import ssl
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from drivedrop.admin import WebAdmin
from drivedrop.broker import Broker
from drivedrop.client import Client, ClientError
from drivedrop.common import ApiError, canonical_json, pinned_request
from tests.fakes import MemoryStore, FakeDrive, FakeUploadTransport


class WebTests(unittest.TestCase):
    def test_profile_endpoint_requires_admin_and_csrf(self):
        self.broker.db.execute('INSERT INTO devices VALUES(?,?,?,0)',('mac','Mac01',time.time()));self.broker.db.commit()
        payload=dict(id='mac',machine_name='Mac01',employee_code='NV01',employee_name='Lan')
        self.assertEqual(self.req('/api/devices/profile',payload)[0],401)
        self.login()
        self.assertEqual(self.req('/api/devices/profile',payload,headers={'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(self.req('/api/devices/profile',payload)[0],200)
        status,dashboard,_=self.req('/api/dashboard')
        self.assertEqual(status,200);self.assertEqual(dashboard['report']['summary']['employees'],1)
        self.assertEqual(dashboard['devices'][0]['employee_name'],'Lan')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.broker = Broker(self.root / 'boss', store=MemoryStore(), drive=FakeDrive())
        self.web = WebAdmin(self.broker)
        self.web.start(host='127.0.0.1', tls_port=0, local_port=0)
        self.local = f'http://127.0.0.1:{self.web.local_port}'
        self.port = self.web.servers[1].server_port
        self.web.public_url = f'https://localhost:{self.port}'
        self.web.public_host = f'localhost:{self.port}'
        self.cookie = ''; self.csrf = ''
        self.context = ssl.create_default_context(cafile=str(self.broker.cert_path))
        self.creds = {'username': 'test-admin', 'password': 'Test-only-long-password!'}

    def tearDown(self):
        self.web.close(); self.broker.close(); self.temp.cleanup()

    def req(self, path, payload=None, public=False, headers=None):
        origin = self.web.public_url if public else self.local
        if public:
            c = http.client.HTTPSConnection('localhost', self.port, context=self.context, timeout=5)
        else:
            c = http.client.HTTPConnection('127.0.0.1', self.web.local_port, timeout=5)
        h = {'Origin': origin, 'Content-Type': 'application/json', 'Cookie': self.cookie, 'X-CSRF-Token': self.csrf}
        h.update(headers or {})
        body = None if payload is None else canonical_json(payload)
        c.request('GET' if payload is None else 'POST', path, body, h)
        r = c.getresponse(); raw = r.read(); status = r.status; response_headers = dict(r.getheaders()); c.close()
        try: value = json.loads(raw)
        except ValueError: value = raw
        return status, value, response_headers

    def setup_account(self):
        self.assertEqual(self.req('/api/setup', self.creds, headers={'X-Setup-Token': self.web.setup_token})[0], 200)

    def login(self, public=False):
        self.setup_account()
        status, result, headers = self.req('/api/login', self.creds, public=public)
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]; self.csrf = result['csrf']
        return headers

    def test_public_dashboard_requires_login_no_secrets_in_auth(self):
        self.assertEqual(self.req('/api/dashboard', public=True)[0], 401)
        _, info, _ = self.req('/api/auth', public=True)
        self.assertIsNone(info['csrf']); self.assertIsNone(info['username'])
        self.assertTrue(self.req('/health', public=True)[1]['google_connected'])

    def test_setup_requires_local_listener_valid_token_origin(self):
        self.assertEqual(self.req('/api/setup', self.creds, public=True, headers={'X-Setup-Token': self.web.setup_token})[0], 403)
        self.assertEqual(self.req('/api/setup', self.creds)[0], 403)
        self.assertEqual(self.req('/api/setup', self.creds, headers={'X-Setup-Token': self.web.setup_token, 'Origin': 'https://evil.example'})[0], 403)
        self.setup_account()
        self.assertEqual(self.req('/api/setup', self.creds, headers={'X-Setup-Token': self.web.setup_token})[0], 403)
        self.assertNotIn(self.creds['password'], self.web.config_file.read_text())

    def test_setup_expiry_and_dns_rebinding(self):
        self.web.setup_deadline = 0
        self.assertEqual(self.req('/api/setup', self.creds, headers={'X-Setup-Token': self.web.setup_token})[0], 403)
        self.assertEqual(self.req('/api/auth', headers={'Host': 'attacker.example'})[0], 421)
        self.assertEqual(self.req('/api/auth', public=True, headers={'Host': '192.168.1.105'})[0], 421)

    def test_login_rate_limit(self):
        self.setup_account()
        for _ in range(8):
            self.assertEqual(self.req('/api/login', dict(self.creds, password='Wrong-password-123'))[0], 401)
        self.assertEqual(self.req('/api/login', self.creds)[0], 429)

    def test_secure_cookie_local_session_not_reused_public(self):
        headers = self.login()
        self.assertIn('HttpOnly', headers['Set-Cookie']); self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.req('/api/dashboard')[0], 200)
        self.assertEqual(self.req('/api/dashboard', public=True)[0], 401)
        status, _, headers = self.req('/api/login', self.creds, public=True)
        self.assertEqual(status, 200); self.assertIn('; Secure', headers['Set-Cookie'])

    def test_csrf_logout_and_session_expiry(self):
        self.login(public=True)
        self.assertEqual(self.req('/api/enroll', {'name': 'Mac'}, public=True, headers={'X-CSRF-Token': 'wrong'})[0], 403)
        self.assertEqual(self.req('/api/enroll', {'name': 'Mac'}, public=True, headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.req('/api/logout', {}, public=True)[0], 200)
        self.assertEqual(self.req('/api/dashboard', public=True)[0], 401)
        _, result, headers = self.req('/api/login', self.creds, public=True)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        for session in self.web.sessions.values(): session['seen'] = time.monotonic() - 1801
        self.assertEqual(self.req('/api/dashboard', public=True)[0], 401)

    def test_enrollment_device_heartbeat_revoke_through_public_tls(self):
        self.login(public=True)
        status, enrollment, _ = self.req('/api/enroll', {'name': 'Mac test'}, public=True)
        self.assertEqual(status, 200); self.assertEqual(enrollment['tls_mode'], 'public_ca')
        client = Client(self.root / 'client', store=MemoryStore(), upload_transport=FakeUploadTransport(self.broker.drive))
        # Explicit test CA: production uses the operating-system/public CA trust store.
        with patch('drivedrop.common.ssl.create_default_context', return_value=self.context):
            device = client.enroll(enrollment)
            self.assertTrue(client.health()['ok'])
            watch = self.root / 'media'; watch.mkdir()
            sample = b'generated-test-media'
            (watch / 'test.jpg').write_bytes(sample)
            client.configure(watch, stable_seconds=0)
            client.run_once(); client.run_once()
            self.assertEqual((watch / 'test.jpg').read_bytes(), sample)
            self.assertEqual(self.broker.list_uploads()[0]['state'], 'verified')
            client.heartbeat()
            self.assertEqual(self.req('/api/dashboard', public=True)[1]['devices'][0]['id'], device['device_id'])
            self.assertEqual(self.req('/api/revoke', {'id': device['device_id']}, public=True)[0], 200)
            with self.assertRaises(ApiError): client.heartbeat()
            with self.assertRaises(ApiError):
                Client(self.root / 'second', store=MemoryStore()).enroll(enrollment)
        client.close()

    def test_public_ca_rejects_untrusted_self_signed_cert(self):
        with self.assertRaises(ApiError):
            pinned_request(self.web.public_url, self.broker.pin, 'GET', '/health', tls_mode='public_ca')
        with self.assertRaises(ApiError):
            pinned_request(self.web.public_url, self.broker.pin, 'GET', '/health', tls_mode='insecure')

    def test_channel_edit_and_no_google_enrollment(self):
        self.login()
        status, result, _ = self.req('/api/channels/save', {'name': 'Kênh kiểm thử', 'code': 'TT99'})
        self.assertEqual(status, 200)
        channel = next(c for c in self.broker.channels.list_channels() if c['code'] == 'TT99')
        self.assertEqual(self.req('/api/channels/enable', {'id': channel['id'], 'enabled': False})[0], 200)
        self.assertEqual(self.req('/api/channels/enable', {'id': channel['id'], 'enabled': 'false'})[0], 400)
        self.broker.drive.available = False
        self.assertEqual(self.req('/api/enroll', {'name': 'Mac'})[0], 409)

    def test_static_allowlist_and_csp(self):
        for path in ('/', '/app.js', '/style.css'):
            status, raw, headers = self.req(path)
            self.assertEqual(status, 200); self.assertIsInstance(raw, bytes)
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(self.req('/../web-admin.json')[0], 401)

    def test_admin_job_does_not_overlap_desktop_operation(self):
        self.login()
        self.broker.operation_lock.acquire()
        try:
            self.assertEqual(self.req('/api/job', {'name':'sync'})[0], 409)
        finally:
            self.broker.operation_lock.release()
        self.assertEqual(self.req('/api/job', {'name':'sync'})[0], 200)
        deadline = time.monotonic() + 5
        while self.web.job['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.web.job['state'], 'done')
        self.assertTrue(self.broker.operation_lock.acquire(blocking=False))
        self.broker.operation_lock.release()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows exclusive bind')
    def test_windows_cannot_reuse_web_port(self):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError): sock.bind(('127.0.0.1', self.port))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = MemoryStore(); self.calls = []
        def request(*args, **kwargs):
            self.calls.append((args, kwargs))
            return {'device_id': 'new-device', 'secret': 'new-protected-secret'}
        self.client = Client(self.root / 'client', store=self.store, request=request)
        self.client.config = {'device_id': 'old-device', 'server_url': 'https://old.example', 'certificate_sha256': '00'*32, 'delete_after_verify': True}
        self.client._save_config(); self.store.set('device_secret', 'old-secret')
        self.enrollment = {'server_url': 'https://new.example', 'certificate_sha256': '11'*32, 'code': 'one-time', 'tls_mode': 'public_ca'}

    def tearDown(self):
        self.client.close(); self.temp.cleanup()

    def test_migrate_preserves_old_key_and_changes_config_atomically(self):
        self.client.migrate_server(self.enrollment)
        self.assertEqual(self.client.config['device_id'], 'new-device')
        self.assertFalse(self.client.config['delete_after_verify'])
        self.assertEqual(self.store.get('device_secret'), 'old-secret')
        self.assertEqual(self.store.get(self.client.config['device_secret_key']), 'new-protected-secret')
        self.assertEqual(self.calls[0][1]['tls_mode'], 'public_ca')
        self.assertEqual(len(list(self.client.data_dir.glob('server-history-*.json'))), 1)

    def test_migrate_blocks_pending_media_and_active_worker(self):
        watch = self.root / 'watch'; pending = watch / '.drivedrop-pending'; pending.mkdir(parents=True)
        (pending / 'preserve.jpg').write_bytes(b'do not delete')
        self.client.config['watch_folder'] = str(watch)
        with self.assertRaises(ClientError): self.client.migrate_server(self.enrollment)
        self.assertEqual((pending / 'preserve.jpg').read_bytes(), b'do not delete')
        self.assertEqual(self.calls, [])
        self.client.lock.acquire()
        try:
            with self.assertRaises(ClientError): self.client.migrate_server(self.enrollment)
        finally: self.client.lock.release()

    def test_migrate_blocks_unsettled_journal(self):
        with self.client._db() as db:
            db.execute("INSERT INTO jobs(request_id,root,name,source_path,claimed_path,status,identity,created) VALUES('id','root','x.jpg','source','claimed','uploading','{}',0)")
        with self.assertRaises(ClientError): self.client.migrate_server(self.enrollment)
        self.assertEqual(self.client.config['device_id'], 'old-device'); self.assertEqual(self.calls, [])

    def test_migration_write_failure_retains_old_identity(self):
        original = self.client.config.copy()
        with patch('drivedrop.client.atomic_json', side_effect=[None, OSError('disk full')]):
            with self.assertRaises(OSError): self.client.migrate_server(self.enrollment)
        self.assertEqual(self.client.config, original)
        self.assertEqual(self.store.get('device_secret'), 'old-secret')


if __name__ == '__main__': unittest.main()
