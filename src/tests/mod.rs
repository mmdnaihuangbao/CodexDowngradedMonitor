//! Behavior tests live under src together with the implementation.
use crate::*;
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::atomic::{AtomicUsize, Ordering},
};
static SEQUENCE: AtomicUsize = AtomicUsize::new(0);
pub struct Sandbox(pub PathBuf);
impl Sandbox {
    pub fn new() -> Self {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("out/test-results")
            .join(format!(
                "unit-{}-{}",
                std::process::id(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
        std::fs::create_dir_all(&path).unwrap();
        config::prepare(&path).unwrap();
        Self(path)
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
fn monitor(dir: &Sandbox) -> monitor::Monitor {
    monitor::Monitor::open(
        &dir.0,
        config::validate(&json!({"expect":"expected"})).unwrap(),
        tokio::sync::broadcast::channel(2000).0,
    )
    .unwrap()
}
fn response(i: usize) -> Value {
    json!({"response_id":format!("resp_test_{i:08}"),"model":"different","status":"completed","created_at":"1700000000","completed_at":"1700000010","prev":"resp_parent"})
}
#[test]
fn late_evidence_conflict_restart_and_evicted_history() {
    let dir = Sandbox::new();
    let mut m = monitor(&dir);
    m.ingest(
        &json!({"responses":(0..3105).map(response).collect::<Vec<_>>(),"requests":[]}),
        "expected",
    )
    .unwrap();
    assert_eq!(m.store.totals().unwrap()["stored_total"], 3105);
    m.ingest(
        &json!({"responses":[],"requests":[{"prev":"resp_parent","model":"expected"}]}),
        "expected",
    )
    .unwrap();
    assert_eq!(
        m.store.get("resp_test_00000000").unwrap().unwrap()["_verdict"],
        "downgrade"
    );
    assert_eq!(m.alerts.len(), 200);
    m.ingest(
        &json!({"responses":[],"requests":[{"prev":"resp_parent","model":"other"}]}),
        "expected",
    )
    .unwrap();
    assert_eq!(m.store.totals().unwrap()["paired"], 0);
    assert!(m.alerts.is_empty());
    drop(m);
    let m = monitor(&dir);
    let r = m.store.get("resp_test_00000000").unwrap().unwrap();
    assert_eq!(r["_pairing_status"], "ambiguous_previous_response_id");
    assert_eq!(r["_expect"], "expected");
}
#[test]
fn failed_batch_retry_does_not_advance_domain() {
    let dir = Sandbox::new();
    let mut m = monitor(&dir);
    let batch = json!({"responses":[response(1)],"requests":[]});
    m.store.db.execute_batch("CREATE TRIGGER disk_full BEFORE INSERT ON responses BEGIN SELECT RAISE(ABORT,'disk full'); END;").unwrap();
    assert!(m.ingest(&batch, "expected").is_err());
    assert_eq!(m.rounds, 0);
    assert_eq!(m.store.totals().unwrap()["stored_total"], 0);
    m.store.db.execute_batch("DROP TRIGGER disk_full").unwrap();
    m.ingest(&batch, "expected").unwrap();
    assert_eq!(m.store.totals().unwrap()["stored_total"], 1);
}
#[test]
fn prefix_longest_ties_priority_and_rejection() {
    let dir = Sandbox::new();
    let mut m = monitor(&dir);
    let body = "123456789012345678901234567890123456789012345678";
    let entries = [
        json!({"key_kind":"item","key_value":"12345678901234567890123000","model":"neighbor","source":"codex_log_prefix"}),
        json!({"key_kind":"item","key_value":"12345678901234567890123400","model":"right","source":"codex_log_prefix"}),
    ];
    m.store.checkpoint(&entries, &[]).unwrap();
    m.reload_index().unwrap();
    let r = json!({"response_id":format!("resp_{body}"),"prev":"resp_parent"});
    assert_eq!(m.resolve(&r).unwrap().0.as_deref(), Some("right"));
    m.store.checkpoint(&[json!({"key_kind":"resp_id","key_value":r["response_id"],"model":"exact","source":"rollout_token_usage"})],&[]).unwrap();
    m.reload_index().unwrap();
    assert_eq!(m.resolve(&r).unwrap().0.as_deref(), Some("exact"));
    m.store.checkpoint(&[json!({"key_kind":"resp_id","key_value":r["response_id"],"model":"conflict","source":"rollout_token_usage"})],&[]).unwrap();
    m.reload_index().unwrap();
    assert_eq!(m.resolve(&r).unwrap().2, "rejected_response_id");
    m.store
        .save_batch(&[], &[json!({"prev":"resp_parent","model":"memory"})])
        .unwrap();
    assert_eq!(m.resolve(&r).unwrap().0.as_deref(), Some("memory"));
}
#[test]
fn rollout_utf8_crlf_partial_pending_and_truncation() {
    use std::io::Write;
    let dir = Sandbox::new();
    let path = dir.0.join("rollout.jsonl");
    let first = "{\"type\":\"event_msg\",\"timestamp\":\"2026-01-01T08:00:00+08:00\",\"payload\":{\"response_id\":\"resp_x\",\"turn_id\":\"turn\",\"note\":\"中文\"}}\r\n";
    std::fs::write(&path,format!("{first}{{\"type\":\"turn_context\",\"payload\":{{\"turn_id\":\"turn\",\"model\":\"expected\"}}}}")).unwrap();
    let (entries, state, _) =
        evidence::read_rollout(&path, &json!({}), usize::MAX, &|| false).unwrap();
    assert!(entries.is_empty());
    assert_eq!(state["offset"], first.len());
    assert_eq!(state["pending"].as_array().unwrap().len(), 1);
    let mut file = std::fs::OpenOptions::new()
        .append(true)
        .open(&path)
        .unwrap();
    file.write_all(b"\r\n").unwrap();
    drop(file);
    let (entries, next, _) = evidence::read_rollout(&path, &state, usize::MAX, &|| false).unwrap();
    assert_eq!(entries[0]["model"], "expected");
    assert_eq!(entries[0]["observed_at"], 1767225600.0);
    assert!(next["pending"].as_array().unwrap().is_empty());
    std::fs::write(&path, b"{}\n").unwrap();
    let (_, reset, _) = evidence::read_rollout(&path, &next, usize::MAX, &|| false).unwrap();
    assert_eq!(reset["offset"], 3);
    assert_eq!(reset["turns"], json!({}));
}
#[test]
fn large_index_preserves_archive_pairing_prefix_conflicts_and_completion() {
    let dir = Sandbox::new();
    let mut m = monitor(&dir);
    let longest = "aaaaaaaaaaaaaaaaaaaa1111111111";
    let tied = "bbbbbbbbbbbbbbbbbbbb1234567890";
    let rejected = "cccccccccccccccccccc1234567890";
    let make = |kind: &str, key: &str, model: &str| json!({"key_kind":kind,"key_value":key,"model":model,"source":if kind=="item" {"codex_log_prefix"}else{"rollout_token_usage"},"observed_at":1700000100.0});
    let mut bad = make("resp_id", &format!("resp_{rejected}"), "expected");
    bad["rejected"] = json!(true);
    m.store
        .checkpoint(
            &[
                make("resp_id", "resp_old_exact", "expected"),
                make("resp_id", "resp_old_unfinished", "expected"),
                make("item", "aaaaaaaaaaaaaaaaaaaa11111", "expected"),
                make("item", "bbbbbbbbbbbbbbbbbbbb12X", "expected"),
                bad,
            ],
            &[],
        )
        .unwrap();
    m.store
        .db
        .execute("UPDATE request_model_index SET last_seen=1", [])
        .unwrap();
    m.store
        .checkpoint(
            &[
                make("item", "aaaaaaaaaaaaaaaaaaaa12", "wrong-shorter"),
                make("item", "bbbbbbbbbbbbbbbbbbbb12Y", "conflicting"),
                make("item", &rejected[..26], "must-not-override-rejection"),
            ],
            &[],
        )
        .unwrap();
    m.reload_index().unwrap();
    let mut exact = response(1);
    exact["response_id"] = json!("resp_old_exact");
    let mut prefix = response(2);
    prefix["response_id"] = json!(format!("resp_{longest}"));
    m.ingest(
        &json!({"responses":[exact,prefix],"requests":[]}),
        "expected",
    )
    .unwrap();
    assert_eq!(
        m.store.get("resp_old_exact").unwrap().unwrap()["_verdict"],
        "downgrade"
    );
    // Ensure a real archive larger than the cache; five old entries are left out,
    // while recent, misleading candidates for the SAME buckets are still cached.
    m.store.db.execute_batch("WITH RECURSIVE seq(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM seq WHERE n<200000) INSERT INTO request_model_index(key_kind,key_value,model,source,first_seen,last_seen,rejected) SELECT 'resp_id','resp_filler_'||n,'expected','rollout_token_usage',2,2,0 FROM seq;").unwrap();
    m.reload_index().unwrap();
    assert!(!m.index_rid.contains_key("resp_old_exact"));
    assert_eq!(m.index_items[&longest[..20]].len(), 1);
    m.backfill().unwrap();
    assert_eq!(
        m.store.get("resp_old_exact").unwrap().unwrap()["_req_model"],
        "expected"
    );
    assert_eq!(
        m.store.get(&format!("resp_{longest}")).unwrap().unwrap()["_req_model"],
        "expected"
    );
    assert_eq!(
        m.resolve(&json!({"response_id":format!("resp_{tied}")}))
            .unwrap()
            .2,
        "ambiguous_response_id_prefix"
    );
    assert_eq!(
        m.resolve(&json!({"response_id":format!("resp_{rejected}")}))
            .unwrap()
            .2,
        "rejected_response_id"
    );
    let mut unfinished = response(3);
    unfinished["response_id"] = json!("resp_old_unfinished");
    unfinished["status"] = json!("in_progress");
    unfinished.as_object_mut().unwrap().remove("completed_at");
    m.ingest(&json!({"responses":[unfinished],"requests":[]}), "expected")
        .unwrap();
    m.backfill().unwrap();
    let completed = m.store.get("resp_old_unfinished").unwrap().unwrap();
    assert_eq!(completed["status"], "completed");
    assert_eq!(completed["completed_at"], "1700000100");
    drop(m);
    let mut m = monitor(&dir);
    m.backfill().unwrap();
    assert_eq!(
        m.store.get("resp_old_exact").unwrap().unwrap()["_verdict"],
        "downgrade"
    );
    assert_eq!(
        m.resolve(&json!({"response_id":format!("resp_{longest}")}))
            .unwrap()
            .0
            .as_deref(),
        Some("expected")
    );
}

#[test]
fn panel_updates_do_not_persist_runtime_overrides() {
    let dir = Sandbox::new();
    let stored = config::validate(&json!({"host":"localhost","cpp_port":48778,"workers":2,"min_interval_ms":250,"expect":"disk","custom":"preserved"})).unwrap();
    config::save(&dir.0, &stored).unwrap();
    let mut runtime = stored.clone();
    runtime["host"] = json!("0.0.0.0");
    runtime["cpp_port"] = json!(50000);
    runtime["expect"] = json!("temporary");
    runtime["min_interval_ms"] = json!(100);
    let effective = config::save_updates(&dir.0, &runtime, &json!({"workers":1})).unwrap();
    let mut expected_disk = stored.clone();
    expected_disk["workers"] = json!(1);
    assert_eq!(config::load(&dir.0).unwrap(), expected_disk);
    runtime["workers"] = json!(1);
    assert_eq!(effective, runtime);
    // Preserve unrelated edits made on disk since startup too.
    expected_disk["custom"] = json!("external-edit");
    config::save(&dir.0, &expected_disk).unwrap();
    let effective = config::save_updates(&dir.0, &effective, &json!({"expect":"panel"})).unwrap();
    expected_disk["expect"] = json!("panel");
    assert_eq!(config::load(&dir.0).unwrap(), expected_disk);
    assert_eq!(effective["expect"], "panel");
    assert_eq!(effective["cpp_port"], 50000);
    let before = std::fs::read(dir.0.join("config.json")).unwrap();
    assert!(config::save_updates(&dir.0, &effective, &json!({"workers":true})).is_err());
    assert_eq!(std::fs::read(dir.0.join("config.json")).unwrap(), before);
}

#[test]
fn config_atomic_and_no_startup_rewrite() {
    let dir = Sandbox::new();
    let raw = b"\xef\xbb\xbf{\"version\":\"0.1\",\"workers\":3,\"custom\":\"keep\"}";
    std::fs::write(dir.0.join("config.json"), raw).unwrap();
    let c = config::load(&dir.0).unwrap();
    assert_eq!(c["workers"], 3);
    assert_eq!(std::fs::read(dir.0.join("config.json")).unwrap(), raw);
    config::save(&dir.0, &c).unwrap();
    assert_eq!(config::load(&dir.0).unwrap()["custom"], "keep");
    assert!(!dir.0.join("data/tmp/config.next").exists());
}

#[test]
fn golden_v040_verdicts_and_fingerprints() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/verdict-v040.json")).unwrap();
    for c in cases {
        assert_eq!(
            domain::verdict(
                &c["record"],
                c["request"].as_str(),
                domain::text(&c, "current_expect")
            ),
            c["verdict"],
            "{c}"
        );
    }
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/fingerprints-v040.json")).unwrap();
    for c in cases {
        assert_eq!(storage::pack(&c["payload"]), c["packed"]);
        assert_eq!(storage::fingerprint(&c["payload"]), c["sha256"]);
    }
}
#[test]
fn fresh_and_older_index_formats_follow_legacy_migration() {
    let dir = Sandbox::new();
    let path = dir.0.join("data/monitor.sqlite");
    let s = storage::Store::open(path.to_str().unwrap()).unwrap();
    assert_eq!(s.state("index_format", Value::Null).unwrap(), 3);
    drop(s);
    for format in [1, 2] {
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute(
            "UPDATE evidence_state SET value=? WHERE key='index_format'",
            [format.to_string()],
        )
        .unwrap();
        db.execute_batch("UPDATE evidence_state SET value='{\"last_id\":123}' WHERE key='codex_log_cursor'; UPDATE evidence_state SET value='{\"old\":99}' WHERE key='rollout_files'; INSERT INTO request_model_index VALUES('prefix','old','m',NULL,NULL,NULL,'codex_log_prefix',1,1,NULL,0);").unwrap();
        drop(db);
        let s = storage::Store::open(path.to_str().unwrap()).unwrap();
        assert_eq!(s.state("index_format", Value::Null).unwrap(), 3);
        assert_eq!(s.state("rollout_files", Value::Null).unwrap(), json!({}));
        assert_eq!(
            s.state("codex_log_cursor", Value::Null).unwrap(),
            if format == 1 {
                json!({})
            } else {
                json!({"last_id":123})
            }
        );
        assert_eq!(
            s.db.query_row(
                "SELECT COUNT(*) FROM request_model_index WHERE key_kind='prefix'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            0
        );
    }
}
#[test]
fn legacy_schema_data_and_markers_unchanged() {
    let dir = Sandbox::new();
    let path = dir.0.join("data/monitor.sqlite");
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch(include_str!("fixtures/legacy-v040.sql"))
        .unwrap();
    db.execute_batch("PRAGMA user_version=1").unwrap();
    let schema = |c: &rusqlite::Connection| {
        c.prepare("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")
            .unwrap()
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap()
    };
    let before = schema(&db);
    drop(db);
    let mut s = storage::Store::open(path.to_str().unwrap()).unwrap();
    assert_eq!(schema(&s.db), before);
    assert_eq!(s.data_version, "0.3");
    assert_eq!(s.state("index_format", Value::Null).unwrap(), 3);
    assert_eq!(
        s.state("codex_log_cursor", Value::Null).unwrap()["last_id"],
        123
    );
    let r = s.get("resp_migration").unwrap().unwrap();
    assert!(r.get("_expect").is_none());
    assert_eq!(r["_verdict"], "subtask");
    s.save_batch(&[response(99)], &[]).unwrap();
    assert_eq!(
        s.db.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "ok"
    );
    assert_eq!(
        s.db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
}
