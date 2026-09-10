import unittest
from drivedrop.alerts import acknowledge
from drivedrop.monitoring import save_status, list_status
from drivedrop.common import ApiError
from tests import test_web_admin as fixture


class AlertTests(unittest.TestCase):
    setUp = fixture.WebTests.setUp
    tearDown = fixture.WebTests.tearDown
    req = fixture.WebTests.req
    login = fixture.WebTests.login
    setup_account = fixture.WebTests.setup_account

    def seed(self):
        self.broker.db.execute('INSERT INTO devices VALUES(?,?,?,?)', ('mac','Mac01',1,0))
        self.broker.db.commit()
        self.status = dict(state='error',error='Missing path',queued=11)
        save_status(self.broker,'mac',self.status)

    def alert(self):
        return list_status(self.broker)[0]['alert']

    def hide(self):
        a=self.alert()
        acknowledge(self.broker,dict(id='mac',token=a['token'],hidden=True))
        return a

    def test_group_ack_retry_recovery_and_new_occurrence(self):
        self.seed();a=self.hide()
        for _ in range(3):save_status(self.broker,'mac',self.status)
        self.assertEqual(self.alert()['token'],a['token'])
        self.assertTrue(self.alert()['acknowledged'])
        save_status(self.broker,'mac',dict(state='scanning',queued=11))
        self.assertTrue(self.alert()['acknowledged'])
        save_status(self.broker,'mac',self.status)
        self.assertTrue(self.alert()['acknowledged'])
        self.assertEqual(self.broker.db.execute('SELECT count(*) FROM device_alerts').fetchone()[0],1)
        self.assertEqual(list_status(self.broker)[0]['status']['queued'],11)
        save_status(self.broker,'mac',dict(state='waiting'))
        self.assertIsNone(self.alert())
        save_status(self.broker,'mac',self.status)
        self.assertNotEqual(self.alert()['token'],a['token'])
        self.assertFalse(self.alert()['acknowledged'])

    def test_new_error_escalation_stale_ack_and_restore(self):
        self.seed();old=self.hide()
        save_status(self.broker,'mac',dict(self.status,queued=12))
        self.assertFalse(self.alert()['acknowledged'])
        with self.assertRaises(ApiError):acknowledge(self.broker,dict(id='mac',token=old['token'],hidden=True))
        self.hide()
        save_status(self.broker,'mac',dict(self.status,error='Different error'))
        self.assertFalse(self.alert()['acknowledged'])
        self.hide();token=self.alert()['token']
        acknowledge(self.broker,dict(id='mac',token=token,hidden=False))
        self.assertFalse(self.alert()['acknowledged'])

    def test_offline_and_restart_persistence(self):
        self.seed();self.hide()
        # Persistence is in SQLite, not a process-local acknowledgement cache.
        import sqlite3
        path=self.broker.db.execute('PRAGMA database_list').fetchone()[2]
        with sqlite3.connect(path) as db:self.assertEqual(db.execute('SELECT acknowledged FROM device_alerts').fetchone()[0],1)
        db.close()
        self.broker.db.execute('UPDATE device_status SET received=1');self.broker.db.commit()
        self.assertEqual(self.alert()['kind'],'offline')
        self.assertFalse(self.alert()['acknowledged'])
        self.hide()
        save_status(self.broker,'mac',dict(state='waiting'))
        self.assertIsNone(self.alert())

    def test_auth_csrf_and_report_counts(self):
        self.seed();payload=dict(id='mac',token=self.alert()['token'],hidden=True)
        self.assertEqual(self.req('/api/alerts/acknowledge',payload)[0],401)
        self.login()
        self.assertEqual(self.req('/api/alerts/acknowledge',payload,headers={'X-CSRF-Token':'bad'})[0],403)
        self.assertEqual(self.req('/api/alerts/acknowledge',payload)[0],200)
        s=self.req('/api/dashboard')[1]['report']['summary']
        self.assertEqual((s['attention'],s['acknowledged']),(0,1))
        self.assertEqual(list_status(self.broker)[0]['status']['queued'],11)
