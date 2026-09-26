
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
                observed_at REAL,
                rejected INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (key_kind, key_value)
            );
            CREATE INDEX IF NOT EXISTS model_index_source ON request_model_index(source, rejected);
            -- 旁路索引的增量游标（日志行号、rollout 文件偏移），必须落库才能跨重启续读。
            CREATE TABLE IF NOT EXISTS evidence_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
            );
