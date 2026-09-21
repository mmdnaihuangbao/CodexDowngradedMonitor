import json
import pathlib
import sys
import tempfile
import time
import unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from history_store import HistoryStore, validation_reason
import collector as C

BASE=1700000000

def rec(i,status='completed',created=None):
    r={'response_id':f'resp_history_{i:08d}','model':'test-model','status':status,
       'created_at':created if created is not None else BASE+i,'_verdict':'normal',
       '_first_seen':BASE+i,'_last_seen':BASE+i,'_updates':0}
    if status=='completed':r['completed_at']=r['created_at']+10
    r['_suspect_reason']=validation_reason(r)
    return r

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(pathlib.Path(self.tmp.name)/'history.sqlite')
        self.store=HistoryStore(self.path)
    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def test_pagination_dates_and_literal_search(self):
        self.store.save_batch([rec(i) for i in range(135)])
        p1=self.store.query();p2=self.store.query(page=2);p3=self.store.query(page=3)
        self.assertEqual((p1['total'],p1['pages']), (135,3))
        self.assertEqual([len(p['responses']) for p in (p1,p2,p3)],[50,50,35])
        self.assertEqual(len({r['response_id'] for p in (p1,p2,p3) for r in p['responses']}),135)
        result=self.store.query(start=BASE+20,end=BASE+25)
        self.assertEqual(result['total'],6)
        self.assertEqual(result['responses'][0]['created_at'],BASE+25)
        self.assertEqual(self.store.query(q='%')['total'],0)
        self.assertEqual(self.store.query(q='history_00000020')['total'],1)
        self.assertEqual(self.store.query(page=999)['page'],3)
        with self.assertRaises(ValueError):self.store.query(start=5,end=4)
        with self.assertRaises(ValueError):self.store.query(start=float('nan'))

    def test_revisions_restart_evidence_and_log(self):
        r=rec(1,'in_progress');self.store.save_batch([r],[{'prev':'resp_parent','model':'test-model'}],'cpp')
        r=rec(1);r['_updates']=1;self.store.save_batch([r],backend='cpp')
        self.store.save_batch([r],backend='cpp')
        self.store.log({'ts':'10:00:00','level':'info','msg':'archived'},'cpp')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM response_events').fetchone()[0],2)
        self.store.close();self.store=HistoryStore(self.path)
        self.assertEqual(self.store.get(r['response_id'])['status'],'completed')
        self.assertEqual(self.store.request_models()['resp_parent'],'test-model')
        self.assertEqual(self.store.recent_logs()[0]['msg'],'archived')
        m=C.Monitor('test-model',0,db_path=self.path)
        try:
            self.assertEqual(m.snapshot()['total'],1)
            self.assertEqual(m.snapshot()['responses'][0]['status'],'completed')
            self.assertEqual(m.stats()['stored_total'],1)
        finally:m.store.close()

    def test_suspect_is_kept_but_not_normal_stats(self):
        bad={'response_id':'resp_sample_probe','model':'placeholder','completed_at':'100','_verdict':'incomplete','_first_seen':BASE,'_last_seen':BASE}
        bad['_suspect_reason']=validation_reason(bad)
        self.store.save_batch([rec(1),bad])
        self.assertEqual(self.store.query()['total'],1)
        self.assertEqual(self.store.query(filter='suspect')['total'],1)
        self.assertEqual(self.store.totals()['stored_total'],2)
        self.assertEqual(self.store.totals()['captured'],1)
        self.assertEqual(self.store.query(filter='suspect',start=BASE,end=BASE)['total'],1)

    def test_two_connections_do_not_regress_completed(self):
        other=HistoryStore(self.path)
        try:
            self.store.save_batch([rec(1,'in_progress')]);self.assertEqual(other.totals()['captured'],1)
            other.save_batch([rec(1)]);self.store.save_batch([rec(1,'in_progress')])
            self.assertEqual(self.store.get(rec(1)['response_id'])['status'],'completed')
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM response_events').fetchone()[0],2)
        finally:other.close()

    def test_failed_write_is_retried_without_losing_cached_state(self):
        class Scanner:
            last_regions=1;last_workers=1;region_cost=0;metrics={}
            def is_alive(self):return True
            def sweep_records(self):return {'responses':[rec(1)],'requests':[],'hit_blocks':1,'bytes':1}
            def close(self):pass
        m=C.Monitor('test-model',0,backend='cpp',db_path=self.path);m.scanner=Scanner();m.pid=1
        save=m.store.save_batch
        def fail(*args):raise OSError('simulated disk write failure')
        m.store.save_batch=fail
        try:
            with self.assertRaises(OSError):m.tick()
            self.assertIsNotNone(m._pending_archive)
            m.store.save_batch=save;m.tick()
            self.assertEqual(m.stats()['stored_total'],1)
            self.assertIsNone(m._pending_archive)
        finally:m.stop();m.store.close()

    def test_eviction_keeps_archive_and_late_evidence(self):
        class Scanner:
            last_regions=1;last_workers=1;region_cost=0;metrics={}
            def is_alive(self):return True
            def sweep_records(self):return self.batch
            def close(self):pass
        m=C.Monitor('test-model',0,backend='cpp',db_path=self.path);sc=Scanner();m.scanner=sc;m.pid=1
        rows=[rec(i) for i in range(3105)]
        rows[0]['prev']='resp_late_parent';rows[0]['model']='different'
        sc.batch={'responses':rows,'requests':[],'hit_blocks':1,'bytes':1}
        try:
            m.tick();self.assertEqual(m.stats()['stored_total'],3105)
            self.assertLessEqual(len(m.seen),3000)
            rid=rows[0]['response_id'];self.assertNotIn(rid,m.seen)
            sc.batch={'responses':[],'requests':[{'prev':'resp_late_parent','model':'test-model'}],'hit_blocks':1,'bytes':1}
            m.tick();self.assertEqual(m.store.get(rid)['_req_model'],'test-model')
            self.assertEqual(m.query_responses(page=63)['total'],3105)
        finally:m.stop();m.store.close()

if __name__=='__main__':unittest.main(verbosity=2)
