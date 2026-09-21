"""Isolated browser-test server; no real process scanning and no production database."""
import sys,pathlib
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import collector as C
from history_store import HistoryStore,validation_reason
path=sys.argv[1];port=sys.argv[2]
s=HistoryStore(path)
rows=[]
for i in range(135):
    status='in_progress' if i in (134,133,132) else 'completed'
    verdict={134:'normal',133:'incomplete',132:'downgrade'}.get(i,'normal')
    r={'response_id':f'resp_browser_{i:08d}','model':'gpt-6-astra','created_at':1700000000+i*60,
       'status':status,'_verdict':verdict,'_first_seen':1700000000+i*60,'_last_seen':1700000000+i*60,
       'effort':'high','_updates':0,'_pairing_status':'request_not_captured'}
    if status=='completed':r['completed_at']=r['created_at']+10
    if verdict=='downgrade':r['_req_model']='another-model'
    r['_suspect_reason']=validation_reason(r);rows.append(r)
r={'response_id':'resp_invalid_browser','model':'sample-model','completed_at':100,'_verdict':'incomplete','_first_seen':1700000000,'_last_seen':1700000000}
r['_suspect_reason']=validation_reason(r);rows.append(r)
s.save_batch(rows,backend='cpp');s.close()
C.enumerate_codex=lambda:[]
sys.argv=['collector.py','--port',port,'--db',path,'--no-open','--log',str(ROOT/'_verify/history-browser.log')]
C.main()
