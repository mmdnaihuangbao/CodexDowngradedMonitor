//! Compatible SQLite archive and atomic evidence checkpoints.
use crate::domain::{now, rank, text, truth};
use anyhow::{Result, ensure};
use rusqlite::{Connection, OptionalExtension, params, params_from_iter, types::Value as Sql};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const VERDICTS: [&str; 4] = ["normal", "subtask", "incomplete", "downgrade"];
pub const INDEX_CACHE_LIMIT: usize = 200000;
const INDEX_COLUMNS: &str =
    "key_kind,key_value,model,effort,turn_id,thread_id,source,observed_at,rejected";
fn index_entry(r: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(
        json!({"key_kind":r.get::<_,String>(0)?,"key_value":r.get::<_,String>(1)?,"model":r.get::<_,String>(2)?,"effort":r.get::<_,Option<String>>(3)?,"turn_id":r.get::<_,Option<String>>(4)?,"thread_id":r.get::<_,Option<String>>(5)?,"source":r.get::<_,String>(6)?,"observed_at":r.get::<_,Option<f64>>(7)?,"rejected":r.get::<_,i32>(8)?}),
    )
}
pub struct Store {
    pub db: Connection,
    pub path: String,
    pub data_version: String,
}
pub fn pack(v: &Value) -> String {
    serde_json::to_string(v).expect("JSON values serialize")
}
pub fn evidence_payload(request: &Value) -> Value {
    let mut v = json!({"prev":request["prev"],"model":request["model"],"source":request.get("source").cloned().unwrap_or(json!("memory"))});
    for k in [
        "thread_id",
        "session_id",
        "turn_id",
        "root_turn_id",
        "candidate_only",
    ] {
        if truth(&request[k]) {
            v[k] = request[k].clone();
        }
    }
    v
}
pub fn fingerprint(v: &Value) -> String {
    format!("{:x}", Sha256::digest(pack(v).as_bytes()))
}
fn optional(v: &Value, k: &str) -> Option<String> {
    v.get(k).filter(|v| !v.is_null()).map(|v| {
        v.as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| v.to_string())
    })
}
fn real(v: &Value, k: &str, default: f64) -> f64 {
    crate::config::number(&v[k])
        .filter(|n| *n != 0.)
        .unwrap_or(default)
}
fn rows(db: &Connection, sql: &str, args: impl rusqlite::Params) -> Result<Vec<Value>> {
    let mut statement = db.prepare(sql)?;
    let values = statement
        .query_map(args, |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    values
        .into_iter()
        .map(|s| Ok(serde_json::from_str(&s)?))
        .collect()
}
impl Store {
    pub fn open(path: &str) -> Result<Self> {
        let mut db = Connection::open(path)?;
        db.busy_timeout(std::time::Duration::from_millis(1000))?;
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA temp_store=MEMORY;")?;
        db.execute_batch(include_str!("storage/schema.sql"))?;
        db.execute(
            "INSERT OR IGNORE INTO data_version(id,version) VALUES(1,'0.3')",
            [],
        )?;
        db.execute(
            "UPDATE data_version SET version='0.3' WHERE id=1 AND version IN ('0.1','0.2')",
            [],
        )?;
        let has_observed = db
            .prepare("PRAGMA table_info(request_model_index)")?
            .query_map([], |r| r.get::<_, String>(1))?
            .collect::<rusqlite::Result<Vec<_>>>()?
            .iter()
            .any(|s| s == "observed_at");
        if !has_observed {
            db.execute(
                "ALTER TABLE request_model_index ADD COLUMN observed_at REAL",
                [],
            )?;
        }
        if db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))? == 0 {
            db.execute_batch("PRAGMA user_version=1")?;
        }
        let format: Option<String> = db
            .query_row(
                "SELECT value FROM evidence_state WHERE key='index_format'",
                [],
                |r| r.get(0),
            )
            .optional()?;
        let format = format
            .as_deref()
            .map(serde_json::from_str::<u64>)
            .transpose()?
            .unwrap_or(1);
        ensure!(format <= 3, "证据索引格式高于本版本支持的格式 3");
        if format < 3 {
            // Identical v0.4 migration rules, committed together with their cursors.
            let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
            tx.execute(
                "DELETE FROM request_model_index WHERE key_kind='prefix'",
                [],
            )?;
            for (key, value) in [("index_format", "3"), ("rollout_files", "{}")]
                .into_iter()
                .chain((format < 2).then_some(("codex_log_cursor", "{}")))
            {
                tx.execute("INSERT INTO evidence_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", params![key,value,now()])?;
            }
            tx.commit()?;
        }
        let data_version =
            db.query_row("SELECT version FROM data_version WHERE id=1", [], |r| {
                r.get(0)
            })?;
        Ok(Self {
            db,
            path: path.into(),
            data_version,
        })
    }
    pub fn get(&self, rid: &str) -> Result<Option<Value>> {
        let value: Option<String> = self
            .db
            .query_row("SELECT payload FROM responses WHERE rid=?", [rid], |r| {
                r.get(0)
            })
            .optional()?;
        value.map(|s| Ok(serde_json::from_str(&s)?)).transpose()
    }
    pub fn recent_logs(&self) -> Result<Vec<Value>> {
        let mut r = rows(
            &self.db,
            "SELECT payload FROM collector_logs ORDER BY id DESC LIMIT 400",
            [],
        )?;
        r.reverse();
        Ok(r)
    }
    pub fn log(&self, line: &Value) -> Result<()> {
        self.db.execute(
            "INSERT INTO collector_logs(observed_at,backend,payload) VALUES (?,'rust',?)",
            params![now(), pack(line)],
        )?;
        Ok(())
    }
    pub fn save_batch(&mut self, records: &[Value], requests: &[Value]) -> Result<Vec<Value>> {
        let stamp = now();
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        for request in requests {
            if !truth(&request["model"]) {
                continue;
            }
            let data = evidence_payload(request);
            let previous = if truth(&data["candidate_only"]) {
                None
            } else {
                optional(&data, "prev")
            };
            tx.execute(
                "INSERT OR IGNORE INTO request_evidence VALUES(?,?,?,?,?)",
                params![
                    fingerprint(&data),
                    previous,
                    text(&data, "model"),
                    stamp,
                    pack(&data)
                ],
            )?;
        }
        let mut saved = vec![];
        for incoming in records {
            let mut rec = incoming.clone();
            let rid = text(&rec, "response_id").to_owned();
            ensure!(!rid.is_empty(), "响应缺少 ID");
            let existing: Option<String> = tx
                .query_row("SELECT payload FROM responses WHERE rid=?", [&rid], |r| {
                    r.get(0)
                })
                .optional()?;
            if let Some(ref raw) = existing {
                let old: Value = serde_json::from_str(raw)?;
                let same_expect = old.get("_expect") == rec.get("_expect");
                if rank(text(&rec, "status")) < rank(text(&old, "status")) {
                    rec.as_object_mut()
                        .unwrap()
                        .extend(old.as_object().unwrap().clone());
                } else {
                    let mut merged = old.clone();
                    merged
                        .as_object_mut()
                        .unwrap()
                        .extend(rec.as_object().unwrap().clone());
                    rec = merged;
                }
                if let Some(v) = old.get("_expect") {
                    rec["_expect"] = v.clone();
                } else {
                    rec.as_object_mut().unwrap().remove("_expect");
                }
                if !same_expect {
                    rec["_verdict"] = old.get("_verdict").cloned().unwrap_or(json!("incomplete"));
                }
                rec["_first_seen"] =
                    json!(real(&old, "_first_seen", stamp).min(real(&rec, "_first_seen", stamp)));
                rec["_last_seen"] =
                    json!(real(&old, "_last_seen", stamp).max(real(&rec, "_last_seen", stamp)));
                rec["_updates"] = json!(
                    old["_updates"]
                        .as_u64()
                        .unwrap_or(0)
                        .max(rec["_updates"].as_u64().unwrap_or(0))
                );
            }
            let packed = pack(&rec);
            if existing.as_deref() == Some(&packed) {
                continue;
            }
            let first = real(&rec, "_first_seen", stamp);
            let last = real(&rec, "_last_seen", first);
            let event = crate::config::number(&rec["created_at"])
                .filter(|v| *v >= 946684800.)
                .unwrap_or(first);
            let search = [
                "response_id",
                "model",
                "_req_model",
                "effort",
                "status",
                "text_format",
                "prev",
            ]
            .map(|k| optional(&rec, k).unwrap_or_default())
            .join(" ")
            .to_lowercase();
            tx.execute("INSERT INTO responses VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rid) DO UPDATE SET model=excluded.model,req_model=excluded.req_model,verdict=excluded.verdict,status=excluded.status,event_time=excluded.event_time,first_seen=excluded.first_seen,last_seen=excluded.last_seen,suspect=excluded.suspect,search_text=excluded.search_text,payload=excluded.payload",params![rid,optional(&rec,"model"),optional(&rec,"_req_model"),rec["_verdict"].as_str().unwrap_or("incomplete"),optional(&rec,"status"),event,first,last,i32::from(truth(&rec["_suspect_reason"])),search,packed])?;
            tx.execute("INSERT INTO response_events(rid,observed_at,backend,payload) VALUES (?,?,'rust',?)",params![rid,stamp,packed])?;
            saved.push(rec);
        }
        tx.commit()?;
        Ok(saved)
    }
    pub fn lookup_request(&self, previous: &str) -> Result<Option<Option<String>>> {
        let(n,model):(i64,Option<String>)=self.db.query_row("SELECT COUNT(DISTINCT model),MIN(model) FROM request_evidence WHERE previous_id=? AND json_extract(payload,'$.candidate_only') IS NULL",[previous],|r|Ok((r.get(0)?,r.get(1)?)))?;
        Ok(if n == 0 {
            None
        } else if n == 1 {
            Some(model)
        } else {
            Some(None)
        })
    }
    pub fn for_previous(&self, previous: &str) -> Result<Vec<Value>> {
        rows(
            &self.db,
            "SELECT payload FROM responses WHERE json_extract(payload,'$.prev')=?",
            [previous],
        )
    }
    pub fn all_for_backfill(&self, after: &str, limit: usize) -> Result<Vec<Value>> {
        rows(
            &self.db,
            "SELECT payload FROM responses WHERE rid>? ORDER BY rid LIMIT ?",
            params![after, limit as i64],
        )
    }
    pub fn unfinished(&self) -> Result<Vec<Value>> {
        rows(
            &self.db,
            "SELECT payload FROM responses WHERE status IN ('queued','in_progress') ORDER BY last_seen DESC LIMIT 1000",
            [],
        )
    }
    pub fn state(&self, key: &str, default: Value) -> Result<Value> {
        let raw: Option<String> = self
            .db
            .query_row("SELECT value FROM evidence_state WHERE key=?", [key], |r| {
                r.get(0)
            })
            .optional()?;
        Ok(raw
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or(default))
    }
    /// Entries and their read offsets share one transaction. Failed writes cannot skip evidence on restart.
    pub fn checkpoint(&mut self, entries: &[Value], states: &[(String, Value)]) -> Result<usize> {
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let stamp = now();
        let mut conflicts = 0;
        for e in entries {
            let kind = text(e, "key_kind");
            let key = text(e, "key_value");
            let model = text(e, "model");
            ensure!(
                !kind.is_empty() && !key.is_empty() && !model.is_empty(),
                "证据键无效"
            );
            let old:Option<(String,bool)>=tx.query_row("SELECT model,rejected FROM request_model_index WHERE key_kind=? AND key_value=?",params![kind,key],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
            let rejected = truth(&e["rejected"]);
            match old {
                None => {
                    tx.execute("INSERT INTO request_model_index(key_kind,key_value,model,effort,turn_id,thread_id,source,first_seen,last_seen,observed_at,rejected) VALUES(?,?,?,?,?,?,?,?,?,?,?)",params![kind,key,model,optional(e,"effort"),optional(e,"turn_id"),optional(e,"thread_id"),text(e,"source"),stamp,stamp,crate::config::number(&e["observed_at"]),rejected])?;
                    conflicts += usize::from(rejected);
                }
                Some((old, was_rejected)) if rejected || old != model => {
                    if !was_rejected {
                        tx.execute("UPDATE request_model_index SET rejected=1,last_seen=? WHERE key_kind=? AND key_value=?",params![stamp,kind,key])?;
                        conflicts += 1;
                    }
                }
                _ => {
                    tx.execute("UPDATE request_model_index SET last_seen=?,effort=COALESCE(?,effort),turn_id=COALESCE(?,turn_id),thread_id=COALESCE(?,thread_id),observed_at=COALESCE(?,observed_at) WHERE key_kind=? AND key_value=?",params![stamp,optional(e,"effort"),optional(e,"turn_id"),optional(e,"thread_id"),crate::config::number(&e["observed_at"]),kind,key])?;
                }
            }
        }
        for (key, value) in states {
            tx.execute("INSERT INTO evidence_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",params![key,pack(value),stamp])?;
        }
        tx.commit()?;
        Ok(conflicts)
    }
    pub fn index_load(&self) -> Result<Vec<Value>> {
        let mut stmt = self.db.prepare(&format!(
            "SELECT {INDEX_COLUMNS} FROM request_model_index ORDER BY last_seen DESC LIMIT ?"
        ))?;
        Ok(stmt
            .query_map([INDEX_CACHE_LIMIT as i64], index_entry)?
            .collect::<rusqlite::Result<Vec<_>>>()?)
    }
    pub fn index_response(&self, rid: &str) -> Result<Option<Value>> {
        Ok(self.db.prepare_cached(&format!("SELECT {INDEX_COLUMNS} FROM request_model_index WHERE key_kind='resp_id' AND key_value=?"))?
            .query_row([rid], index_entry).optional()?)
    }
    pub fn index_items_for_prefix(&self, prefix: &str) -> Result<Vec<Value>> {
        ensure!(
            !prefix.is_empty() && prefix.is_ascii(),
            "索引前缀必须是非空 ASCII"
        );
        // Use the existing (key_kind,key_value) primary key, without changing the schema.
        let mut end = prefix[..prefix.len() - 1].to_owned();
        end.push(char::from(prefix.as_bytes()[prefix.len() - 1] + 1));
        let mut stmt = self.db.prepare_cached(&format!("SELECT {INDEX_COLUMNS} FROM request_model_index WHERE key_kind='item' AND key_value>=? AND key_value<?"))?;
        Ok(stmt
            .query_map(params![prefix, end], index_entry)?
            .collect::<rusqlite::Result<Vec<_>>>()?)
    }
    pub fn totals(&self) -> Result<Value> {
        let mut counts = json!({"normal":0,"subtask":0,"incomplete":0,"downgrade":0});
        let mut statement = self
            .db
            .prepare("SELECT verdict,COUNT(*) FROM responses WHERE suspect=0 GROUP BY verdict")?;
        for r in statement.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))? {
            let (k, n) = r?;
            counts[k] = json!(n);
        }
        counts["error"] = json!(self.db.query_row(
            "SELECT COUNT(*) FROM responses WHERE suspect=0 AND status='failed'",
            [],
            |r| r.get::<_, i64>(0)
        )?);
        let(total,suspects,paired):(i64,i64,i64)=self.db.query_row("SELECT COUNT(*),COALESCE(SUM(suspect),0),COALESCE(SUM(CASE WHEN suspect=0 AND req_model IS NOT NULL THEN 1 ELSE 0 END),0) FROM responses",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        Ok(
            json!({"counts":counts,"captured":total-suspects,"suspect_count":suspects,"stored_total":total,"paired":paired}),
        )
    }
    pub fn query(&self, q: &Query) -> Result<Value> {
        ensure!(
            q.page >= 1 && (1..=200).contains(&q.page_size),
            "页码或每页数量无效"
        );
        ensure!(
            ["all", "suspect", "error"].contains(&q.filter.as_str())
                || VERDICTS.contains(&q.filter.as_str()),
            "筛选条件无效"
        );
        ensure!(q.q.chars().count() <= 500, "搜索内容过长");
        ensure!(
            q.start.is_none_or(f64::is_finite) && q.end.is_none_or(f64::is_finite),
            "时间无效"
        );
        if let (Some(a), Some(b)) = (q.start, q.end) {
            ensure!(a <= b, "开始时间不能晚于结束时间");
        }
        let mut clauses = vec!["suspect=?"];
        let mut args = vec![Sql::Integer(i64::from(q.filter == "suspect"))];
        if VERDICTS.contains(&q.filter.as_str()) {
            clauses.push("verdict=?");
            args.push(Sql::Text(q.filter.clone()));
        } else if q.filter == "error" {
            clauses.push("status='failed'");
        }
        if !q.q.is_empty() {
            clauses.push("search_text LIKE ? ESCAPE '\\'");
            args.push(Sql::Text(format!(
                "%{}%",
                q.q.to_lowercase()
                    .replace('\\', "\\\\")
                    .replace('%', "\\%")
                    .replace('_', "\\_")
            )));
        }
        if let Some(v) = q.start {
            clauses.push("event_time>=?");
            args.push(Sql::Real(v));
        }
        if let Some(v) = q.end {
            clauses.push("event_time<=?");
            args.push(Sql::Real(v));
        }
        let clause = clauses.join(" AND ");
        let tx = self.db.unchecked_transaction()?;
        let total: i64 = tx.query_row(
            &format!("SELECT COUNT(*) FROM responses WHERE {clause}"),
            params_from_iter(args.iter()),
            |r| r.get(0),
        )?;
        let pages = ((total + q.page_size - 1) / q.page_size).max(1);
        let page = q.page.min(pages);
        args.extend([
            Sql::Integer(q.page_size),
            Sql::Integer((page - 1) * q.page_size),
        ]);
        let records = rows(
            &tx,
            &format!(
                "SELECT payload FROM responses WHERE {clause} ORDER BY event_time DESC,rid DESC LIMIT ? OFFSET ?"
            ),
            params_from_iter(args.iter()),
        )?;
        tx.commit()?;
        Ok(
            json!({"responses":records,"total":total,"page":page,"page_size":q.page_size,"pages":pages}),
        )
    }
}
#[derive(serde::Deserialize, Debug)]
#[serde(default)]
pub struct Query {
    pub page: i64,
    pub page_size: i64,
    pub filter: String,
    pub q: String,
    pub start: Option<f64>,
    pub end: Option<f64>,
}
impl Default for Query {
    fn default() -> Self {
        Self {
            page: 1,
            page_size: 50,
            filter: "all".into(),
            q: String::new(),
            start: None,
            end: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn record(i: i64) -> Value {
        json!({"response_id":format!("resp_history_{i:08}"),"model":"test","status":"completed","created_at":1700000000+i,"_verdict":"normal","_first_seen":1700000000+i,"_last_seen":1700000000+i,"_updates":0})
    }
    #[test]
    fn pagination_literal_search_and_terminal_state() {
        let mut s = Store::open(":memory:").unwrap();
        s.save_batch(&(0..135).map(record).collect::<Vec<_>>(), &[])
            .unwrap();
        assert_eq!(s.query(&Query::default()).unwrap()["total"], 135);
        assert_eq!(
            s.query(&Query {
                page: 999,
                ..Default::default()
            })
            .unwrap()["page"],
            3
        );
        for (q, n) in [("%", 0), ("history_00000020", 1)] {
            assert_eq!(
                s.query(&Query {
                    q: q.into(),
                    ..Default::default()
                })
                .unwrap()["total"],
                n
            );
        }
        assert_eq!(
            s.query(&Query {
                start: Some(1700000020.),
                end: Some(1700000025.),
                ..Default::default()
            })
            .unwrap()["total"],
            6
        );
        let mut r = record(1);
        r["status"] = json!("in_progress");
        r["_expect"] = json!("new-config");
        s.save_batch(&[r], &[]).unwrap();
        let r = s.get("resp_history_00000001").unwrap().unwrap();
        assert_eq!(r["status"], "completed");
        assert!(r.get("_expect").is_none());
    }
    #[test]
    fn request_ambiguity_candidates_and_atomic_cursor() {
        let mut s = Store::open(":memory:").unwrap();
        s.save_batch(
            &[],
            &[json!({"prev":"resp_p","model":"a","candidate_only":"1"})],
        )
        .unwrap();
        assert_eq!(s.lookup_request("resp_p").unwrap(), None);
        s.save_batch(
            &[],
            &[
                json!({"prev":"resp_p","model":"a"}),
                json!({"prev":"resp_p","model":"b"}),
            ],
        )
        .unwrap();
        assert_eq!(s.lookup_request("resp_p").unwrap(), Some(None));
        let e = json!({"key_kind":"resp_id","key_value":"resp_x","model":"a","source":"rollout_token_usage"});
        s.checkpoint(std::slice::from_ref(&e), &[("cursor".into(), json!(1))])
            .unwrap();
        s.db.execute_batch("CREATE TRIGGER fail_cursor BEFORE UPDATE ON evidence_state BEGIN SELECT RAISE(ABORT,'disk failure'); END;").unwrap();
        let mut conflict = e;
        conflict["model"] = json!("b");
        assert!(
            s.checkpoint(&[conflict.clone()], &[("cursor".into(), json!(2))])
                .is_err()
        );
        assert_eq!(s.index_load().unwrap()[0]["rejected"], 0);
        assert_eq!(s.state("cursor", Value::Null).unwrap(), 1);
        s.db.execute_batch("DROP TRIGGER fail_cursor").unwrap();
        assert_eq!(
            s.checkpoint(&[conflict], &[("cursor".into(), json!(2))])
                .unwrap(),
            1
        );
        assert_eq!(s.index_load().unwrap()[0]["rejected"], 1);
    }
}
