BEGIN TRANSACTION;
CREATE TABLE collector_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at REAL NOT NULL,
                backend TEXT NOT NULL, payload TEXT NOT NULL
            );
CREATE TABLE data_version (
                id INTEGER PRIMARY KEY CHECK (id=1), version TEXT NOT NULL
            );
INSERT INTO "data_version" VALUES(1,'0.3');
CREATE TABLE evidence_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
            );
INSERT INTO "evidence_state" VALUES('index_format','3',1.790358849011280995e+09);
INSERT INTO "evidence_state" VALUES('codex_log_cursor','{"path": "logs_2.sqlite", "last_id": 123}',1.790358849011280995e+09);
INSERT INTO "evidence_state" VALUES('rollout_files','{}',1.790358849011280995e+09);
CREATE TABLE request_evidence (
                fingerprint TEXT PRIMARY KEY, previous_id TEXT, model TEXT NOT NULL,
                first_seen REAL NOT NULL, payload TEXT NOT NULL
            );
INSERT INTO "request_evidence" VALUES('fe60fbf8742a646eba2195fcc07ce2ad659d96add9baac390391b4b824e6d4af',NULL,'gpt-test',1.79035884901075093011e+09,'{"model":"gpt-test","prev":null,"source":"memory"}');
INSERT INTO "request_evidence" VALUES('7f74740b4ee712c1983d1a23b91cdb91f0295a0d4f22116462365b63b9e48294','resp_中文','test',1.79035884901075093011e+09,'{"model":"test","prev":"resp_中文","source":"memory_websocket","turn_id":"turn\\\"中"}');
INSERT INTO "request_evidence" VALUES('2d0e0f6427f05ae7a41f62cc9a2b0e075777824add935b2d54449ddc762de496',NULL,'candidate',1.79035884901075093011e+09,'{"candidate_only":"1","model":"candidate","prev":"resp_p","source":"memory_http_candidate"}');
CREATE TABLE request_model_index (
                key_kind TEXT NOT NULL, key_value TEXT NOT NULL, model TEXT NOT NULL,
                effort TEXT, turn_id TEXT, thread_id TEXT, source TEXT NOT NULL,
                first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                observed_at REAL,
                rejected INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (key_kind, key_value)
            );
CREATE TABLE response_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, rid TEXT NOT NULL,
                observed_at REAL NOT NULL, backend TEXT NOT NULL, payload TEXT NOT NULL
            );
INSERT INTO "response_events" VALUES(1,'resp_migration',1.79035884901075093011e+09,'cpp','{"_first_seen":1700000000,"_last_seen":1700000010,"_updates":1,"_verdict":"subtask","completed_at":"1700000010","created_at":"1700000000","model":"legacy","response_id":"resp_migration","status":"completed"}');
CREATE TABLE responses (
                rid TEXT PRIMARY KEY, model TEXT, req_model TEXT, verdict TEXT NOT NULL,
                status TEXT, event_time REAL NOT NULL, first_seen REAL NOT NULL,
                last_seen REAL NOT NULL, suspect INTEGER NOT NULL DEFAULT 0,
                search_text TEXT NOT NULL, payload TEXT NOT NULL
            );
INSERT INTO "responses" VALUES('resp_migration','legacy',NULL,'subtask','completed',1700000000.0,1700000000.0,1700000010.0,0,'resp_migration legacy   completed  ','{"_first_seen":1700000000,"_last_seen":1700000010,"_updates":1,"_verdict":"subtask","completed_at":"1700000010","created_at":"1700000000","model":"legacy","response_id":"resp_migration","status":"completed"}');
CREATE INDEX responses_page ON responses(suspect,event_time DESC,rid DESC);
CREATE INDEX responses_filter ON responses(suspect,verdict,event_time DESC,rid DESC);
CREATE INDEX responses_previous ON responses(json_extract(payload,'$.prev'));
CREATE INDEX events_rid ON response_events(rid,id);
CREATE INDEX evidence_previous ON request_evidence(previous_id);
CREATE INDEX model_index_source ON request_model_index(source, rejected);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('response_events',1);
COMMIT;