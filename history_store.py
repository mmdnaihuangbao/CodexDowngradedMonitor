"""SQLite archive for response state, revisions, request evidence and collector logs."""
import hashlib
import json
import math
import os
import sqlite3
import threading
import time

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'monitor.sqlite')
DATA_VERSION = '0.1'
VERDICTS = ('normal', 'subtask', 'incomplete', 'downgrade')

def validation_reason(rec):
    reasons = []
    try:
        created = float(rec.get('created_at'))
        if not math.isfinite(created) or not 946684800 <= created <= time.time() + 86400:
            reasons.append('创建时间异常')
    except (TypeError, ValueError):
        reasons.append('缺少创建时间')
    completed = rec.get('completed_at')
    if completed is not None:
        try:
            completed = float(completed)
            if not math.isfinite(completed) or completed < 946684800 or completed > time.time() + 86400:
                reasons.append('完成时间异常')
        except (ValueError, TypeError):
            reasons.append('完成时间异常')
    if rec.get('status') not in ('queued', 'in_progress', 'completed', 'incomplete', 'failed', 'cancelled'):
        reasons.append('缺少有效请求状态')
    return '；'.join(reasons)

class HistoryStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = path
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS responses (
                rid TEXT PRIMARY KEY, model TEXT, req_model TEXT, verdict TEXT NOT NULL,
                status TEXT, event_time REAL NOT NULL, first_seen REAL NOT NULL,
                last_seen REAL NOT NULL, suspect INTEGER NOT NULL DEFAULT 0,
                search_text TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS responses_page ON responses(suspect,event_time DESC,rid DESC);
            CREATE INDEX IF NOT EXISTS responses_filter ON responses(suspect,verdict,event_time DESC,rid DESC);
            CREATE INDEX IF NOT EXISTS responses_previous ON responses(json_extract(payload,'$.prev'));
            CREATE TABLE IF NOT EXISTS response_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, rid TEXT NOT NULL,
                observed_at REAL NOT NULL, backend TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_rid ON response_events(rid,id);
            CREATE TABLE IF NOT EXISTS request_evidence (
                fingerprint TEXT PRIMARY KEY, previous_id TEXT, model TEXT NOT NULL,
                first_seen REAL NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evidence_previous ON request_evidence(previous_id);
            CREATE TABLE IF NOT EXISTS collector_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at REAL NOT NULL,
                backend TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS data_version (
                id INTEGER PRIMARY KEY CHECK (id=1), version TEXT NOT NULL
            );
        ''')
        self.db.execute('INSERT OR IGNORE INTO data_version(id,version) VALUES (1,?)', (DATA_VERSION,))
        if self.db.execute('PRAGMA user_version').fetchone()[0] == 0:
            self.db.execute('PRAGMA user_version=1')
        self.data_version = self.db.execute('SELECT version FROM data_version WHERE id=1').fetchone()[0]
        self.db.commit()
        self._version = -1
        self._totals = None
        self._request_seen = {r[0] for r in self.db.execute('SELECT fingerprint FROM request_evidence ORDER BY first_seen DESC LIMIT 20000')}

    @staticmethod
    def pack(rec):
        return json.dumps(rec, ensure_ascii=False, separators=(',', ':'), sort_keys=True)

    def save_batch(self, records, requests=(), backend='python'):
        records = list(records)
        evidence = []
        for request in requests:
            if not request.get('model'): continue
            data = {'prev': request.get('prev'), 'model': request['model'], 'source': request.get('source', 'memory')}
            packed = self.pack(data)
            fingerprint = hashlib.sha256(packed.encode()).hexdigest()
            if fingerprint not in self._request_seen:
                evidence.append((fingerprint,data,packed))
        if not records and not evidence: return
        now = time.time()
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for fingerprint, data, packed in evidence:
                self.db.execute('INSERT OR IGNORE INTO request_evidence VALUES (?,?,?,?,?)',
                                (fingerprint, data['prev'], data['model'], now, packed))
            for rec in records:
                rid = rec['response_id']
                existing = self.db.execute('SELECT payload FROM responses WHERE rid=?', (rid,)).fetchone()
                if existing:
                    old = json.loads(existing['payload'])
                    same_expect = old.get('_expect') == rec.get('_expect') and ('_expect' in old) == ('_expect' in rec)
                    rank = {'queued':1,'in_progress':2,'failed':3,'incomplete':3,'cancelled':3,'completed':4}
                    if rank.get(rec.get('status'),0) < rank.get(old.get('status'),0):
                        # A second backend must not regress a persisted terminal state.
                        rec = {**rec, **old}
                    else:
                        rec = {**old, **rec}
                    # 两个采集后端竞争同一响应时，以已入库的首次采集配置为准。
                    # 旧历史缺少目标模型时保持缺失，不能拿当前配置回填。
                    if '_expect' in old:
                        rec['_expect'] = old['_expect']
                    else:
                        rec.pop('_expect', None)
                    if not same_expect:
                        rec['_verdict'] = old.get('_verdict', 'incomplete')
                    rec['_first_seen'] = min(float(old.get('_first_seen') or now), float(rec.get('_first_seen') or now))
                    rec['_last_seen'] = max(float(old.get('_last_seen') or now), float(rec.get('_last_seen') or now))
                    rec['_updates'] = max(old.get('_updates',0),rec.get('_updates',0))
                packed = self.pack(rec)
                if existing and existing['payload'] == packed: continue
                first = float(rec.get('_first_seen') or now)
                last = float(rec.get('_last_seen') or first)
                try: event_time = float(rec.get('created_at'))
                except (ValueError, TypeError): event_time = first
                if not math.isfinite(event_time) or event_time < 946684800: event_time = first
                search = ' '.join(str(rec.get(k) or '') for k in
                                  ('response_id','model','_req_model','effort','status','text_format','prev')).lower()
                self.db.execute('''INSERT INTO responses VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(rid) DO UPDATE SET model=excluded.model,req_model=excluded.req_model,
                    verdict=excluded.verdict,status=excluded.status,event_time=excluded.event_time,
                    first_seen=excluded.first_seen,last_seen=excluded.last_seen,suspect=excluded.suspect,
                    search_text=excluded.search_text,payload=excluded.payload''',
                    (rid,rec.get('model'),rec.get('_req_model'),rec.get('_verdict','incomplete'),
                     rec.get('status'),event_time,first,last,int(bool(rec.get('_suspect_reason'))),search,packed))
                self.db.execute('INSERT INTO response_events(rid,observed_at,backend,payload) VALUES (?,?,?,?)',
                                (rid, now, backend, packed))
            if records: self._totals = None
        self._request_seen.update(row[0] for row in evidence)
        if len(self._request_seen) > 20000:
            self._request_seen = {r[0] for r in self.db.execute('SELECT fingerprint FROM request_evidence ORDER BY first_seen DESC LIMIT 20000')}

    def log(self, line, backend):
        with self.lock, self.db:
            self.db.execute('INSERT INTO collector_logs(observed_at,backend,payload) VALUES (?,?,?)',
                            (time.time(),backend,self.pack(line)))

    def get(self, rid):
        with self.lock:
            row = self.db.execute('SELECT payload FROM responses WHERE rid=?',(rid,)).fetchone()
            return json.loads(row['payload']) if row else None

    def recent(self, limit=3000):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(
                'SELECT payload FROM responses ORDER BY last_seen DESC,rid DESC LIMIT ?', (limit,))]

    def recent_logs(self, limit=400):
        with self.lock:
            rows = self.db.execute('SELECT payload FROM collector_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
            return [json.loads(r[0]) for r in reversed(rows)]

    def request_models(self, limit=6000):
        with self.lock:
            rows = self.db.execute('''SELECT previous_id,CASE WHEN COUNT(DISTINCT model)=1 THEN MIN(model) END
                FROM request_evidence WHERE previous_id IS NOT NULL
                GROUP BY previous_id ORDER BY MAX(first_seen) DESC LIMIT ?''', (limit,)).fetchall()
            return dict(rows)

    def for_previous(self, keys):
        rows = {}
        with self.lock:
            for key in keys:
                for row in self.db.execute("SELECT rid,payload FROM responses WHERE json_extract(payload,'$.prev')=?",(key,)):
                    rows[row['rid']] = json.loads(row['payload'])
        return rows

    def lookup_request(self, previous):
        with self.lock:
            row = self.db.execute('SELECT COUNT(DISTINCT model),MIN(model) FROM request_evidence WHERE previous_id=?',(previous,)).fetchone()
        return row[0] > 0, row[1] if row[0] == 1 else None

    def totals(self):
        with self.lock:
            version = self.db.execute('PRAGMA data_version').fetchone()[0]
            if self._totals is not None and version == self._version:
                return self._totals
            counts = {v: 0 for v in VERDICTS}
            for row in self.db.execute('SELECT verdict,COUNT(*) FROM responses WHERE suspect=0 GROUP BY verdict'):
                counts[row[0]] = row[1]
            total, suspects, paired = self.db.execute('''SELECT COUNT(*),COALESCE(SUM(suspect),0),
                COALESCE(SUM(CASE WHEN suspect=0 AND req_model IS NOT NULL THEN 1 ELSE 0 END),0) FROM responses''').fetchone()
            self._totals = {'counts':counts,'captured':total-suspects,'suspect_count':suspects,'stored_total':total,'paired':paired}
            self._version = version
            return self._totals

    def query(self, page=1, page_size=50, filter='all', q='', start=None, end=None):
        if page < 1 or not 1 <= page_size <= 200: raise ValueError('页码或每页数量无效')
        if filter not in ('all','suspect',*VERDICTS): raise ValueError('筛选条件无效')
        if len(q) > 500: raise ValueError('搜索内容过长')
        for t in (start,end):
            if t is not None and not math.isfinite(t): raise ValueError('时间无效')
        if start is not None and end is not None and start > end: raise ValueError('开始时间不能晚于结束时间')
        clauses=['suspect=?'];params=[int(filter=='suspect')]
        if filter in VERDICTS:clauses.append('verdict=?');params.append(filter)
        if q:
            literal=q.lower().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            clauses.append("search_text LIKE ? ESCAPE '\\'");params.append('%'+literal+'%')
        if start is not None:clauses.append('event_time>=?');params.append(start)
        if end is not None:clauses.append('event_time<=?');params.append(end)
        where=' AND '.join(clauses)
        with self.lock:
            # A read transaction keeps total and page rows consistent across two backends.
            self.db.execute('BEGIN')
            try:
                total=self.db.execute('SELECT COUNT(*) FROM responses WHERE '+where,params).fetchone()[0]
                pages=max(1,math.ceil(total/page_size));page=min(page,pages)
                rows=self.db.execute('SELECT payload FROM responses WHERE '+where+
                    ' ORDER BY event_time DESC,rid DESC LIMIT ? OFFSET ?',params+[page_size,(page-1)*page_size]).fetchall()
            finally:self.db.commit()
        return {'responses':[json.loads(row[0]) for row in rows], 'total':total,'page':page,'page_size':page_size,'pages':pages}

    def close(self):
        with self.lock:self.db.close()
