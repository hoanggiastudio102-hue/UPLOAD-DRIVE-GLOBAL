import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from drivedrop.broker import Broker
from drivedrop.common import ApiError
from drivedrop.inventory import scan_inventory, validate_inventory
from drivedrop.monitoring import list_status, save_status, validate_status
from drivedrop.reporting import add_reports, save_profile
from tests.fakes import FakeDrive, MemoryStore

class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.watch=self.root/'watch';self.watch.mkdir()
        self.broker=Broker(self.root/'boss',store=MemoryStore(),drive=FakeDrive())
    def tearDown(self):
        self.broker.close();self.temp.cleanup()
    def device(self, id, revoked=0):
        self.broker.db.execute('INSERT INTO devices VALUES(?,?,?,?)',(id,id,time.time(),revoked));self.broker.db.commit()
    def profile(self, device, code='NV01', name='Minh Anh'):
        return save_profile(self.broker,dict(id=device,machine_name=device,employee_code=code,employee_name=name))
    def test_nested_empty_and_non_media_are_counted_without_double_count(self):
        (self.watch/'Bài 01'/'Ảnh').mkdir(parents=True);(self.watch/'Trống').mkdir()
        (self.watch/'Bài 01'/'clip.MOV').write_bytes(b'abcd')
        (self.watch/'Bài 01'/'Ảnh'/'1.PNG').write_bytes(b'ab')
        (self.watch/'note.txt').write_text('abc');(self.watch/'.hidden.jpg').write_bytes(b'x')
        inv=validate_inventory(scan_inventory(self.watch));rows={r['path']:r for r in inv['folders']}
        self.assertEqual(len(rows),4);self.assertTrue(inv['complete'])
        self.assertEqual(rows['Bài 01']['videos'],1);self.assertEqual(rows['Bài 01']['images'],0)
        self.assertEqual(rows['Bài 01/Ảnh']['images'],1);self.assertEqual(rows['Trống']['bytes'],0)
        self.assertEqual(sum(r['bytes'] for r in rows.values()),9)
    def test_claimed_file_keeps_original_folder_and_queue_count(self):
        folder=self.watch/'Bài';folder.mkdir();pending=self.watch/'.drivedrop-pending'/'job';pending.mkdir(parents=True)
        (pending/'a.mp4').write_bytes(b'12345')
        jobs=[dict(source_path=str(folder/'a.mp4'),claimed_path=str(pending/'a.mp4'),name='a.mp4',status='uploading')]
        inv=scan_inventory(self.watch,jobs);row=next(r for r in inv['folders'] if r['path']=='Bài')
        self.assertEqual((row['videos'],row['queued'],row['bytes']),(1,1,5));validate_inventory(inv)
    def test_scan_limits_are_explicit(self):
        for i in range(5):(self.watch/f'{i}.jpg').write_bytes(b'x')
        inv=scan_inventory(self.watch,max_entries=2)
        self.assertFalse(inv['complete']);self.assertEqual(inv['folders'][0]['images'],2)
        with patch('drivedrop.inventory.MAX_FOLDERS',2):
            for i in range(4):(self.watch/f'dir{i}').mkdir()
            inv=scan_inventory(self.watch)
        self.assertEqual(len(inv['folders']),2);self.assertFalse(inv['complete'])
    def test_file_claimed_during_scan_is_not_counted_twice(self):
        import os
        source=self.watch/'a.jpg';source.write_bytes(b'abc')
        pending=self.watch/'.drivedrop-pending'/'job';pending.mkdir(parents=True);target=pending/'a.jpg'
        real_scandir=os.scandir
        class Entry:
            def __init__(self,entry):self.entry=entry;self.name=entry.name;self.path=entry.path
            def is_symlink(self):return self.entry.is_symlink()
            def stat(self,**kwargs):
                info=self.entry.stat(**kwargs)
                if self.name=='a.jpg' and source.exists():source.rename(target)
                return info
        class Entries:
            def __init__(self,path):self.entries=real_scandir(path)
            def __enter__(self):return (Entry(e) for e in self.entries)
            def __exit__(self,*args):self.entries.close()
        jobs=[dict(source_path=str(source),claimed_path=str(target),name='a.jpg',status='claiming')]
        with patch('drivedrop.inventory.os.scandir',side_effect=Entries):inv=scan_inventory(self.watch,jobs)
        self.assertEqual(inv['folders'][0]['images'],1);self.assertEqual(inv['folders'][0]['queued'],1)
    def test_link_targets_not_enumerated(self):
        outside=self.root/'outside';outside.mkdir();(outside/'private.jpg').write_bytes(b'x')
        try:(self.watch/'link').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('Symlink creation unavailable')
        inv=scan_inventory(self.watch);self.assertEqual(len(inv['folders']),1);self.assertEqual(inv['skipped'],1)
    def test_inventory_rejects_invalid_tree_numbers_and_timestamps(self):
        good=scan_inventory(self.watch)
        for mutate in [lambda x:x['folders'][0].update(path='../x'),lambda x:x['folders'][0].update(images=True),lambda x:x.update(captured=float('nan')),lambda x:x.update(captured=time.time()+99999),lambda x:x['folders'].append(dict(x['folders'][0])),lambda x:x.update(secret='x')]:
            bad=copy.deepcopy(good);mutate(bad)
            with self.assertRaises(ApiError):validate_status(dict(state='waiting',inventory=bad))
    def test_legacy_worker_status_is_accepted_and_missing_inventory_is_unknown(self):
        self.device('Mac01');save_status(self.broker,'Mac01',{'state':'waiting','version':'0.4.0'})
        devices=list_status(self.broker);report=add_reports(self.broker,devices)
        self.assertIsNone(devices[0]['inventory_totals']);self.assertEqual(report['summary']['inventory_machines'],0)
    def test_employee_count_is_distinct_and_unassigned_not_people(self):
        for id in ('Mac01','Mac02','Win01'):self.device(id)
        self.device('SELFTEST',1);self.profile('Mac01');self.profile('Mac02')
        report=add_reports(self.broker,list_status(self.broker))['summary']
        self.assertEqual((report['employees'],report['devices'],report['unassigned']),(1,3,1))
        self.profile('Mac02','NV02','Lan');self.assertEqual(add_reports(self.broker,list_status(self.broker))['summary']['employees'],2)
        self.profile('Mac01','','');self.assertEqual(add_reports(self.broker,list_status(self.broker))['summary']['unassigned'],2)
    def test_profile_validation_and_shared_name_update(self):
        self.device('Mac01');self.device('Mac02');self.profile('Mac01');self.profile('Mac02',name='Anh mới')
        devices=list_status(self.broker);add_reports(self.broker,devices)
        self.assertTrue(all(d['employee_name']=='Anh mới' for d in devices))
        with self.assertRaises(ApiError):self.profile('missing')
        with self.assertRaises(ApiError):self.profile('Mac01','bad/code')
        with self.assertRaises(ApiError):self.profile('Mac01','','name')
    def test_stale_and_partial_snapshots_are_not_marked_fresh(self):
        self.device('Mac01');inv=scan_inventory(self.watch);inv['captured']-=3600
        save_status(self.broker,'Mac01',dict(state='waiting',inventory=inv))
        devices=list_status(self.broker);r=add_reports(self.broker,devices)
        self.assertFalse(devices[0]['inventory_fresh']);self.assertEqual(r['summary']['inventory_machines'],1)
    def test_verified_day_and_history_do_not_guess_legacy_timestamps(self):
        self.device('Mac01');self.device('SELFTEST',1)
        for id,dev,at in [('new','Mac01',time.time()),('old','Mac01',None),('test','SELFTEST',time.time())]:
            self.broker.db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,?)',(id,dev,id,id,'x.jpg',10,'sha','md5','p',time.time()-86400,'verified'))
            self.broker.db.execute('INSERT INTO upload_routes VALUES(?,?,?,?,?,?,?)',(id,'ch','ch','ch','ANH','x.jpg','ch/x.jpg'))
            if at:self.broker.db.execute('INSERT INTO upload_verified VALUES(?,?)',(id,at))
        self.broker.db.commit();devices=list_status(self.broker);r=add_reports(self.broker,devices)
        self.assertEqual(r['summary']['today_files'],1);self.assertEqual(r['summary']['today_images'],1)
        self.assertEqual(sum(d['files'] for d in r['trend']),1)
        self.assertEqual(next(d for d in devices if d['id']=='Mac01')['confirmed'],2)
    def test_additive_tables_survive_reopen_without_changing_existing_records(self):
        self.device('Mac01');self.profile('Mac01');self.broker.close()
        self.broker=Broker(self.root/'boss',store=MemoryStore(),drive=FakeDrive())
        devices=list_status(self.broker);r=add_reports(self.broker,devices)
        self.assertEqual(r['summary']['employees'],1);self.assertEqual(devices[0]['name'],'Mac01')
