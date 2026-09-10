import unittest
from tests import test_web_admin as fixture


class UploadPageTests(unittest.TestCase):
    setUp = fixture.WebTests.setUp
    tearDown = fixture.WebTests.tearDown
    req = fixture.WebTests.req
    login = fixture.WebTests.login
    setup_account = fixture.WebTests.setup_account

    def seed(self):
        self.broker.db.execute('INSERT INTO devices VALUES(?,?,?,?)', ('mac', 'Mac01', 1, 0))
        self.broker.db.executemany('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            [(f'u{i}', 'mac', f'r{i}', f'f{i}', f'ẢNH {i:04}.jpg', 10, 'sha', 'md5', 'folder', 10, 'verified') for i in range(1203)])
        self.broker.db.commit()

    def test_all_rows_reachable_with_stable_ties(self):
        self.login(); self.seed()
        status, result, _ = self.req('/api/uploads/page', {'page':1,'page_size':100})
        self.assertEqual(status, 200)
        self.assertEqual((result['total'], result['pages']), (1203,13))
        found = []
        for page in range(1,14):
            result = self.req('/api/uploads/page', {'page':page,'page_size':100,'snapshot':result['snapshot']})[1]
            found.extend(row['id'] for row in result['items'])
        self.assertEqual(len(found),1203)
        self.assertEqual(len(set(found)),1203)
        self.assertEqual(found[-1],'u0')

    def test_search_beyond_first_100_and_page_clamping(self):
        self.login(); self.seed()
        r = self.req('/api/uploads/page', {'query':'ảnh 0000','page':99})[1]
        self.assertEqual((r['page'],r['total']), (1,1))
        self.assertEqual(r['items'][0]['id'],'u0')
        r = self.req('/api/uploads/page', {'query':"%' OR 1=1 --"})[1]
        self.assertEqual(r['total'],0)

    def test_new_upload_does_not_shift_current_snapshot(self):
        self.login(); self.seed()
        r = self.req('/api/uploads/page', {'page':2})[1]
        self.broker.db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('new','mac','new','new','new.jpg',10,'sha','md5','folder',99,'issued'))
        self.broker.db.commit()
        again = self.req('/api/uploads/page', {'page':2,'snapshot':r['snapshot']})[1]
        self.assertEqual(again['items'],r['items'])
        self.assertEqual(again['total'],1203)
        self.assertEqual(self.req('/api/uploads/page', {})[1]['total'],1204)

    def test_auth_csrf_and_bounds(self):
        self.assertEqual(self.req('/api/uploads/page', {})[0],401)
        self.login()
        self.assertEqual(self.req('/api/uploads/page', {},headers={'X-CSRF-Token':'bad'})[0],403)
        for payload in ({'page':0},{'page':True},{'page_size':10000},{'snapshot':-1},{'snapshot':True},{'query':'x'*201}):
            self.assertEqual(self.req('/api/uploads/page', payload)[0],400)

    def test_combined_filters_vietnam_day_boundary(self):
        from datetime import datetime, timezone
        self.login(); self.seed()
        self.broker.db.execute("INSERT INTO device_profiles VALUES('mac','NV01')")
        for uid,stamp in [('u0','2026-09-09T17:00:00+00:00'),('u1','2026-09-10T16:59:59+00:00'),('u2','2026-09-10T17:00:00+00:00')]:
            self.broker.db.execute('UPDATE uploads SET created=? WHERE id=?',(datetime.fromisoformat(stamp).timestamp(),uid))
            self.broker.db.execute('INSERT INTO upload_routes VALUES(?,?,?,?,?,?,?)',(uid,'ch','TH9','Channel','VIDEO','clip.mp4','Channel/clip.mp4'))
        self.broker.db.commit()
        filters=dict(device='mac',employee='NV01',media='VIDEO',state='verified',date_from='2026-09-10',date_to='2026-09-10')
        status,r,_=self.req('/api/uploads/page',filters)
        self.assertEqual(status,200)
        self.assertEqual({x['id'] for x in r['items']},{'u0','u1'})
        self.assertEqual(self.req('/api/uploads/page',dict(filters,media='ANH'))[1]['total'],0)
        self.assertEqual(self.req('/api/uploads/page',dict(filters,device='other'))[1]['total'],0)
        for invalid in ({'date_from':'2026-99-01'},{'date_from':'2026-09-11','date_to':'2026-09-10'},{'media':'all'},{'device':12}):
            self.assertEqual(self.req('/api/uploads/page',invalid)[0],400)
