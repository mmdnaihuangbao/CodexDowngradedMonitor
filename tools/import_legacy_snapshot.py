"""One-time import of an older collector snapshot; never clears existing history."""
import argparse
from datetime import datetime
import json
import pathlib
import sys
import time
import urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from history_store import HistoryStore,DEFAULT_DB,validation_reason

def epoch(value):
    if value is None or value=='':return None
    if isinstance(value,(int,float)):return value
    try:return datetime.fromisoformat(value).timestamp()
    except ValueError:return None

def import_snapshot(snapshot, store):
    records=[];requests=[];now=time.time()
    for item in snapshot.get('responses',[]):
        if not item.get('rid'):continue
        record={'response_id':item['rid'],'model':item.get('model'),
                'status':item.get('status'),'effort':item.get('effort'),'prev':item.get('prev'),
                'created_at':epoch(item.get('created_at')),'completed_at':epoch(item.get('completed_at')),
                'text_format':item.get('text_format'),'_marks':item.get('marks'),
                '_verdict':item.get('verdict','incomplete'),'_req_model':item.get('req_model'),
                '_pairing_status':item.get('pairing_status'),
                '_first_seen':item.get('first_seen') or now,'_last_seen':item.get('last_seen') or now,
                '_updates':item.get('updates',0),'_process_pid':None}
        record['_suspect_reason']=validation_reason(record)
        records.append(record)
        if record.get('prev') and record.get('_req_model'):
            requests.append({'prev':record['prev'],'model':record['_req_model'],'source':'legacy_snapshot'})
    backend=snapshot.get('stats',{}).get('backend','python')
    store.save_batch(records,requests,backend)
    for line in snapshot.get('logs',[]):store.log(line,backend)
    return len(records)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    source=ap.add_mutually_exclusive_group(required=True)
    source.add_argument('--port',type=int)
    source.add_argument('--snapshot',type=pathlib.Path)
    ap.add_argument('--db',default=DEFAULT_DB)
    args=ap.parse_args()
    if args.snapshot:
        data=json.loads(args.snapshot.read_text(encoding='utf-8-sig'))
    else:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{args.port}/api/snapshot',timeout=10) as response:data=json.load(response)
        # New snapshots are paginated; use the paginated API instead of importing an incomplete snapshot.
        if 'pages' in data and data['pages']>1:raise SystemExit('Use this importer only for a legacy, unpaginated snapshot.')
    store=HistoryStore(args.db)
    try:print(json.dumps({'imported':import_snapshot(data,store),'totals':store.totals()},ensure_ascii=False))
    finally:store.close()
if __name__=='__main__':main()
