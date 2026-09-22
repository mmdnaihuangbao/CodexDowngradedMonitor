"""SQLite archive for response state, revisions, request evidence and collector logs."""
import hashlib
import json
import math
import os
import sqlite3
import threading
import time

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'monitor.sqlite')
DATA_VERSION = '0.3'
VERDICTS = ('normal', 'subtask', 'incomplete', 'downgrade')
INDEX_KEY_KINDS = ('resp_id', 'prefix')

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
            -- 旁路证据索引：键来自 codex 自有日志/会话记录，不是内存观测。
            CREATE TABLE IF NOT EXISTS request_model_index (
                key_kind TEXT NOT NULL, key_value TEXT NOT NULL, model TEXT NOT NULL,
                effort TEXT, turn_id TEXT, thread_id TEXT, source TEXT NOT NULL,
                first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                rejected INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (key_kind, key_value)
            );
            CREATE INDEX IF NOT EXISTS model_index_source ON request_model_index(source, rejected);
            -- 旁路索引的增量游标（日志行号、rollout 文件偏移），必须落库才能跨重启续读。
            CREATE TABLE IF NOT EXISTS evidence_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
            );
        ''')
        self.db.execute('INSERT OR IGNORE INTO data_version(id,version) VALUES (1,?)', (DATA_VERSION,))
        # 0.2 只扩展证据 JSON；0.3 只新增旁路证据索引表。两者都不重写历史响应与判定。
        self.db.execute("UPDATE data_version SET version=? WHERE id=1 AND version IN ('0.1','0.2')", (DATA_VERSION,))
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
            for key in ('thread_id', 'session_id', 'turn_id', 'root_turn_id', 'candidate_only'):
                if request.get(key): data[key] = request[key]
            packed = self.pack(data)
            fingerprint = hashlib.sha256(packed.encode()).hexdigest()
            if fingerprint not in self._request_seen:
                evidence.append((fingerprint,data,packed))
        if not records and not evidence: return
        now = time.time()
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for fingerprint, data, packed in evidence:
                # Keep unverified candidates out of the legacy lookup column too: an older
                # concurrently running backend does not understand candidate_only.
                previous = None if data.get('candidate_only') else data['prev']
                self.db.execute('INSERT OR IGNORE INTO request_evidence VALUES (?,?,?,?,?)',
                                (fingerprint, previous, data['model'], now, packed))
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
                FROM request_evidence WHERE previous_id IS NOT NULL AND json_extract(payload,'$.candidate_only') IS NULL
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
            row = self.db.execute("SELECT COUNT(DISTINCT model),MIN(model) FROM request_evidence WHERE previous_id=? AND json_extract(payload,'$.candidate_only') IS NULL",(previous,)).fetchone()
        return row[0] > 0, row[1] if row[0] == 1 else None

    def request_details(self, previous):
        if not previous: return []
        with self.lock:
            rows = self.db.execute('SELECT payload,first_seen FROM request_evidence WHERE previous_id=? ORDER BY first_seen DESC LIMIT 50', (previous,)).fetchall()
            return [{**json.loads(row[0]), 'first_seen': row[1]} for row in rows]

    # ---------------- 旁路证据索引（codex 日志前缀 / rollout 响应号） ----------------
    def index_save(self, entries):
        """写入旁路证据索引，返回被判定为冲突的键。

        同一个键（响应号或其前缀）出现第二个不同模型时，整个键作废并保留首见模型：
        前缀是对服务端 ID 生成规则的逆向观察，冲突意味着这次观察失效，
        此时宁可没有证据，也不能按"最后一次写入"猜一个模型出来。
        """
        conflicts = []
        entries = list(entries)
        if not entries: return conflicts
        now = time.time()
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for entry in entries:
                kind, key, model = entry['key_kind'], entry['key_value'], entry['model']
                # 索引层可能已经把"同一轮内撞键"的条目标成作废，这里同样要认。
                incoming = bool(entry.get('rejected'))
                row = self.db.execute('SELECT model,rejected FROM request_model_index '
                                      'WHERE key_kind=? AND key_value=?', (kind, key)).fetchone()
                if row is None:
                    self.db.execute('INSERT INTO request_model_index VALUES (?,?,?,?,?,?,?,?,?,?)',
                                    (kind, key, model, entry.get('effort'), entry.get('turn_id'),
                                     entry.get('thread_id'), entry['source'], now, now,
                                     1 if incoming else 0))
                    if incoming:
                        conflicts.append({'key_kind': kind, 'key_value': key, 'kept': model,
                                          'conflict': entry.get('conflict_model'),
                                          'source': entry['source']})
                elif incoming or row['model'] != model:
                    if not row['rejected']:
                        self.db.execute('UPDATE request_model_index SET rejected=1,last_seen=? '
                                        'WHERE key_kind=? AND key_value=?', (now, kind, key))
                        conflicts.append({'key_kind': kind, 'key_value': key,
                                          'kept': row['model'],
                                          'conflict': entry.get('conflict_model') or model,
                                          'source': entry['source']})
                else:
                    self.db.execute('UPDATE request_model_index SET last_seen=?, effort=COALESCE(?,effort), '
                                    'turn_id=COALESCE(?,turn_id), thread_id=COALESCE(?,thread_id) '
                                    'WHERE key_kind=? AND key_value=?',
                                    (now, entry.get('effort'), entry.get('turn_id'),
                                     entry.get('thread_id'), kind, key))
        return conflicts

    def index_drop_kinds(self, kinds):
        """删除指定类型的索引键（索引格式升级时用），返回删除条数。"""
        kinds = tuple(kinds)
        if not kinds: return 0
        with self.lock, self.db:
            marks = ','.join('?' * len(kinds))
            cursor = self.db.execute(f'DELETE FROM request_model_index WHERE key_kind IN ({marks})', kinds)
            return cursor.rowcount

    def index_load(self, limit=200000):
        with self.lock:
            rows = self.db.execute('SELECT key_kind,key_value,model,effort,turn_id,thread_id,source,rejected '
                                   'FROM request_model_index ORDER BY last_seen DESC LIMIT ?', (limit,)).fetchall()
        return [dict(row) for row in rows]

    def index_stats(self):
        counts, rejected, sources = {}, 0, {}
        with self.lock:
            for row in self.db.execute('SELECT key_kind,source,rejected,COUNT(*) FROM request_model_index '
                                       'GROUP BY key_kind,source,rejected'):
                if row[2]:
                    rejected += row[3]
                    continue
                counts[row[0]] = counts.get(row[0], 0) + row[3]
                sources[row[1]] = sources.get(row[1], 0) + row[3]
        return {'keys': counts, 'rejected': rejected, 'sources': sources}

    def state_get(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM evidence_state WHERE key=?', (key,)).fetchone()
        if row is None: return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def state_set(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT INTO evidence_state(key,value,updated_at) VALUES (?,?,?) '
                            'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                            (key, json.dumps(value, ensure_ascii=False), time.time()))

    def responses_missing_request_model(self, limit=500):
        with self.lock:
            rows = self.db.execute('SELECT payload FROM responses WHERE req_model IS NULL '
                                   'ORDER BY last_seen DESC LIMIT ?', (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

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
