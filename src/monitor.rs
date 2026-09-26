//! Domain state is published only after SQLite commits; failed batches are safe to retry.
use crate::{
    domain::{self, now, rank, text, truth},
    storage::{Query, Store},
};
use anyhow::Result;
use serde_json::{Value, json};
use std::{
    borrow::Cow,
    collections::{HashMap, VecDeque},
    path::Path,
};

pub struct Monitor {
    pub store: Store,
    pub config: Value,
    pub status: String,
    pub pid: Option<u32>,
    pub candidates: Vec<Value>,
    pub metrics: Value,
    pub rounds: u64,
    pub last_scan: Option<f64>,
    pub last_sweep: Option<Value>,
    pub logs: VecDeque<Value>,
    pub alerts: Vec<Value>,
    pub evidence_stats: Value,
    pub index_rid: HashMap<String, Value>,
    pub index_items: HashMap<String, Vec<Value>>,
    index_complete: bool,
    started: f64,
    times: VecDeque<f64>,
    alert_seq: u64,
    backfill_cursor: String,
    pub broker: tokio::sync::broadcast::Sender<Value>,
}
impl Monitor {
    pub fn open(
        root: &Path,
        config: Value,
        broker: tokio::sync::broadcast::Sender<Value>,
    ) -> Result<Self> {
        let store = Store::open(
            root.join("data/monitor.sqlite")
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("路径编码无效"))?,
        )?;
        let logs = store.recent_logs()?.into();
        let mut m = Self {
            store,
            config,
            status: "starting".into(),
            pid: None,
            candidates: vec![],
            metrics: json!({}),
            rounds: 0,
            last_scan: None,
            last_sweep: None,
            logs,
            alerts: vec![],
            evidence_stats: json!({"enabled":true,"poll_seconds":2,"polls":0,"backfilled":0,"settled":0,"conflicts":0}),
            index_rid: HashMap::new(),
            index_items: HashMap::new(),
            index_complete: false,
            started: now(),
            times: VecDeque::new(),
            alert_seq: 0,
            backfill_cursor: String::new(),
            broker,
        };
        m.reload_index()?;
        Ok(m)
    }
    pub fn reload_index(&mut self) -> Result<()> {
        let entries = self.store.index_load()?;
        self.index_complete = entries.len() < crate::storage::INDEX_CACHE_LIMIT;
        self.index_rid.clear();
        self.index_items.clear();
        let mut keys = json!({});
        let mut sources = json!({});
        let mut rejected = 0;
        for entry in entries {
            let kind = text(&entry, "key_kind").to_owned();
            let source = text(&entry, "source").to_owned();
            if truth(&entry["rejected"]) {
                rejected += 1;
            } else {
                keys[&kind] = json!(keys[&kind].as_u64().unwrap_or(0) + 1);
                sources[&source] = json!(sources[&source].as_u64().unwrap_or(0) + 1);
            }
            let key = text(&entry, "key_value").to_owned();
            if kind == "resp_id" {
                self.index_rid.insert(key, entry);
            } else if kind == "item" && key.is_ascii() && key.len() >= 20 {
                self.index_items
                    .entry(key[..20].to_owned())
                    .or_default()
                    .push(entry);
            }
        }
        self.evidence_stats["index_keys"] = keys;
        self.evidence_stats["index_sources"] = sources;
        self.evidence_stats["index_rejected"] = json!(rejected);
        Ok(())
    }
    fn response_evidence(&self, rid: &str) -> Result<Option<Value>> {
        if let Some(entry) = self.index_rid.get(rid) {
            return Ok(Some(entry.clone()));
        }
        if self.index_complete {
            Ok(None)
        } else {
            self.store.index_response(rid)
        }
    }
    pub fn resolve(&self, rec: &Value) -> Result<(Option<String>, Option<String>, String)> {
        let prev = text(rec, "prev");
        if !prev.is_empty()
            && let Some(model) = self.store.lookup_request(prev)?
        {
            return Ok(match model {
                Some(m) => (
                    Some(m),
                    Some("memory_websocket".into()),
                    "matched_previous_response_id".into(),
                ),
                None => (None, None, "ambiguous_previous_response_id".into()),
            });
        }
        let rid = text(rec, "response_id");
        if let Some(e) = self.response_evidence(rid)? {
            return Ok(if truth(&e["rejected"]) {
                (None, None, "rejected_response_id".into())
            } else {
                (
                    Some(text(&e, "model").into()),
                    Some(text(&e, "source").into()),
                    "matched_response_id".into(),
                )
            });
        }
        if let Some(body) = rid
            .strip_prefix("resp_")
            .filter(|v| v.is_ascii() && v.len() >= 21)
        {
            // A cached bucket can be only partially loaded: a cache hit is not proof
            // that its longest match or all conflicting candidates are present.
            let bucket: Cow<'_, [Value]> = if self.index_complete {
                Cow::Borrowed(
                    self.index_items
                        .get(&body[..20])
                        .map(Vec::as_slice)
                        .unwrap_or(&[]),
                )
            } else {
                Cow::Owned(self.store.index_items_for_prefix(&body[..20])?)
            };
            let mut best = 0;
            let mut models = std::collections::BTreeSet::new();
            for e in bucket.iter() {
                if truth(&e["rejected"]) {
                    continue;
                }
                let length = body
                    .bytes()
                    .zip(text(e, "key_value").bytes())
                    .take_while(|(a, b)| a == b)
                    .count();
                if length > best {
                    best = length;
                    models.clear();
                    models.insert(text(e, "model"));
                } else if length == best {
                    models.insert(text(e, "model"));
                }
            }
            if best >= 21 {
                return Ok(if models.len() == 1 {
                    (
                        Some((*models.first().unwrap()).into()),
                        Some("codex_log_prefix".into()),
                        "matched_response_id_prefix".into(),
                    )
                } else {
                    (None, None, "ambiguous_response_id_prefix".into())
                });
            }
        }
        Ok((
            None,
            None,
            if prev.is_empty() {
                "no_previous_response_id"
            } else {
                "request_not_captured"
            }
            .into(),
        ))
    }
    fn classify(&self, rec: &mut Value) -> Result<()> {
        let (model, source, pairing) = self.resolve(rec)?;
        rec["_verdict"] = json!(domain::verdict(
            rec,
            model.as_deref(),
            text(&self.config, "expect")
        ));
        rec["_req_model"] = json!(model);
        rec["_evidence_source"] = json!(source);
        rec["_pairing_status"] = json!(pairing);
        rec["_suspect_reason"] = json!(domain::validation_reason(rec, now()));
        Ok(())
    }
    fn alert(&mut self, r: &Value) -> Option<Value> {
        let rid = text(r, "response_id");
        if r["_verdict"] != "downgrade" || truth(&r["_suspect_reason"]) {
            self.alerts.retain(|a| a["rid"] != rid);
            return None;
        }
        if self.alerts.iter().any(|a| a["rid"] == rid) {
            return None;
        }
        self.alert_seq += 1;
        let a = json!({"seq":self.alert_seq,"rid":rid,"model":r["model"],"req_model":r["_req_model"],"expect":r["_expect"],"effort":r["effort"],"status":r["status"],"prev":r["prev"],"text_format":r["text_format"],"safety_id":r["safety_id"],"paired":truth(&r["_req_model"]),"ts":chrono::Local::now().format("%H:%M:%S").to_string(),"wall":now()});
        self.alerts.insert(0, a.clone());
        self.alerts.truncate(200);
        Some(a)
    }
    pub fn log(&mut self, level: &str, msg: impl Into<String>) {
        let mut line = json!({"ts":chrono::Local::now().format("%H:%M:%S").to_string(),"level":level,"msg":msg.into()});
        if let Err(e) = self.store.log(&line) {
            line["msg"] = json!(format!("{}（日志写库失败：{e}）", text(&line, "msg")));
        }
        self.logs.push_back(line.clone());
        while self.logs.len() > 400 {
            self.logs.pop_front();
        }
        let _ = self.broker.send(json!({"type":"patch","logs":[line]}));
    }
    pub fn ingest(&mut self, batch: &Value, capture_expect: &str) -> Result<()> {
        let requests = batch["requests"].as_array().cloned().unwrap_or_default();
        // Persist request evidence before resolving it, so all paths use the same conflict semantics.
        self.store.save_batch(&[], &requests)?;
        let mut changed = HashMap::new();
        let stamp = now();
        for incoming in batch["responses"].as_array().into_iter().flatten() {
            let id = text(incoming, "response_id");
            if id.is_empty() {
                continue;
            }
            let prior = changed.get(id).cloned().or(self.store.get(id)?);
            let mut r = match prior {
                None => {
                    let mut r = incoming.clone();
                    r["_expect"] = json!(capture_expect);
                    r["_process_pid"] = json!(self.pid);
                    r["_first_seen"] = json!(stamp);
                    r["_last_seen"] = json!(stamp);
                    r["_updates"] = json!(0);
                    r
                }
                Some(old) => {
                    let gained = ["completed_at", "safety_id", "created_at", "prev"]
                        .iter()
                        .any(|k| !truth(&old[*k]) && truth(&incoming[*k]));
                    if rank(text(incoming, "status")) <= rank(text(&old, "status")) && !gained {
                        continue;
                    }
                    let mut r = old.clone();
                    r.as_object_mut()
                        .unwrap()
                        .extend(incoming.as_object().unwrap().clone());
                    if rank(text(incoming, "status")) < rank(text(&old, "status")) {
                        r["status"] = old["status"].clone();
                    }
                    r["_last_seen"] = json!(stamp);
                    r["_updates"] = json!(old["_updates"].as_u64().unwrap_or(0) + 1);
                    r
                }
            };
            self.classify(&mut r)?;
            changed.insert(id.to_owned(), r);
        }
        for request in &requests {
            if truth(&request["candidate_only"]) || text(request, "prev").is_empty() {
                continue;
            }
            for mut r in self.store.for_previous(text(request, "prev"))? {
                let id = text(&r, "response_id").to_owned();
                if changed.contains_key(&id) {
                    continue;
                }
                let old = r.clone();
                self.classify(&mut r)?;
                if r != old {
                    changed.insert(id, r);
                }
            }
        }
        let saved = self
            .store
            .save_batch(&changed.into_values().collect::<Vec<_>>(), &[])?;
        self.rounds += 1;
        self.last_scan = Some(stamp);
        self.metrics = batch.clone();
        self.metrics.as_object_mut().unwrap().remove("responses");
        self.metrics.as_object_mut().unwrap().remove("requests");
        self.times.push_back(stamp);
        while self.times.len() > 40 {
            self.times.pop_front();
        }
        self.last_sweep = Some(
            json!({"ts":chrono::Local::now().format("%H:%M:%S").to_string(),"wall":stamp,"cost":batch["scan_cost"],"bytes":batch["bytes"],"regions":batch["regions"],"workers":batch["workers"],"region_cost":batch["region_cost"],"hit_blocks":batch["hit_blocks"],"raw_responses":batch["responses"].as_array().map_or(0,Vec::len),"raw_requests":requests.len(),"unkeyed_requests":requests.iter().filter(|r|!truth(&r["prev"])).count(),"request_candidates":requests.iter().take(50).collect::<Vec<_>>(),"backend":"rust","native":self.metrics,"upserts":saved.len(),"items":saved.iter().take(200).map(domain::serialize).collect::<Vec<_>>() }),
        );
        self.publish(saved)?;
        Ok(())
    }
    pub fn publish(&mut self, records: Vec<Value>) -> Result<()> {
        let alerts: Vec<_> = records.iter().filter_map(|r| self.alert(r)).collect();
        if !records.is_empty() {
            let _=self.broker.send(json!({"type":"patch","upserts":records.iter().map(domain::serialize).collect::<Vec<_>>(),"alerts":alerts,"stats":self.stats()?}));
        }
        Ok(())
    }
    pub fn backfill(&mut self) -> Result<()> {
        // Walk the whole archive in bounded pages, including evicted and previously paired rows.
        // This also withdraws evidence and alerts after an index key becomes conflicted.
        let rows = self.store.all_for_backfill(&self.backfill_cursor, 500)?;
        let next = rows
            .last()
            .map(|r| text(r, "response_id").to_owned())
            .unwrap_or_default();
        let mut changed = vec![];
        for mut r in rows {
            let old = r.clone();
            self.classify(&mut r)?;
            if r != old {
                changed.push(r);
            }
        }
        let saved = self.store.save_batch(&changed, &[])?;
        self.backfill_cursor = next;
        self.evidence_stats["backfilled"] =
            json!(self.evidence_stats["backfilled"].as_u64().unwrap_or(0) + saved.len() as u64);
        self.publish(saved)?;
        let mut settled = vec![];
        for mut r in self.store.unfinished()? {
            let id = text(&r, "response_id");
            let entry = self
                .response_evidence(id)?
                .filter(|e| !truth(&e["rejected"]));
            if entry.is_none() && self.store.for_previous(id)?.is_empty() {
                continue;
            }
            r["status"] = json!("completed");
            if let Some(time) = entry
                .as_ref()
                .and_then(|e| crate::config::number(&e["observed_at"]))
                .filter(|v| *v > 0.)
            {
                r["completed_at"] = json!((time as i64).to_string());
            }
            r["_last_seen"] = json!(now());
            r["_updates"] = json!(r["_updates"].as_u64().unwrap_or(0) + 1);
            self.classify(&mut r)?;
            settled.push(r);
        }
        let saved = self.store.save_batch(&settled, &[])?;
        self.evidence_stats["settled"] =
            json!(self.evidence_stats["settled"].as_u64().unwrap_or(0) + saved.len() as u64);
        self.publish(saved)?;
        Ok(())
    }
    pub fn stats(&self) -> Result<Value> {
        let mut out = self.store.totals()?;
        let hz = if self.times.len() > 1 && now() - self.times.back().unwrap() < 3. {
            (self.times.len() - 1) as f64
                / (self.times.back().unwrap() - self.times.front().unwrap()).max(0.000001)
        } else {
            0.
        };
        let(request_keys,ambiguous):(i64,i64)=self.store.db.query_row("SELECT COUNT(*),COALESCE(SUM(n>1),0) FROM (SELECT COUNT(DISTINCT model) n FROM request_evidence WHERE previous_id IS NOT NULL AND json_extract(payload,'$.candidate_only') IS NULL GROUP BY previous_id)",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
        let extra = json!({"status":self.status,"pid":self.pid,"expect":self.config["expect"],"min_interval_ms":self.config["min_interval_ms"],"idle":crate::config::number(&self.config["min_interval_ms"]).unwrap_or(0.)/1000.,"workers":self.config["workers"],"active_workers":self.metrics["workers"].as_u64().unwrap_or(0),"regions":self.metrics["regions"].as_u64().unwrap_or(0),"region_cost":self.metrics["region_cost"].as_f64().unwrap_or(0.),"hz":hz,"rounds":self.rounds,"scan_cost":self.metrics["scan_cost"].as_f64().unwrap_or(0.),"scan_bytes":self.metrics["bytes"].as_u64().unwrap_or(0),"last_scan":self.last_scan,"database":self.store.path,"request_keys":request_keys,"ambiguous_pairings":ambiguous,"backend":"rust","config_version":"0.1","data_version":self.store.data_version,"clients":self.broker.receiver_count(),"up":now()-self.started,"server_time":now(),"evidence":self.evidence_stats});
        out.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        Ok(out)
    }
    pub fn query(&self, q: &Query) -> Result<Value> {
        let mut page = self.store.query(q)?;
        page["responses"] = json!(
            page["responses"]
                .as_array()
                .unwrap()
                .iter()
                .map(domain::serialize)
                .collect::<Vec<_>>()
        );
        Ok(page)
    }
    pub fn snapshot(&self) -> Result<Value> {
        let mut page = self.query(&Query::default())?;
        page["stats"] = self.stats()?;
        page["alerts"] = json!(self.alerts);
        page["logs"] = json!(self.logs);
        page["candidates"] = json!(self.candidates);
        Ok(page)
    }
    pub fn diagnose(&self) -> Result<Value> {
        let mut out = json!({"ok":self.status=="running"&&self.last_sweep.is_some(),"status":self.status,"pid":self.pid,"clients":self.broker.receiver_count(),"rounds":self.rounds,"hz":self.stats()?["hz"],"candidates":self.candidates,"evidence":self.evidence_stats});
        if let Some(s) = &self.last_sweep {
            out["sweep"] = s.clone();
            out["blocks"] = s["hit_blocks"].clone();
            out["raw_responses"] = s["raw_responses"].clone();
            out["unique"] = json!(s["items"].as_array().map_or(0, Vec::len));
            out["pairings"] = s["raw_requests"].clone();
            out["cost"] = s["cost"].clone();
            out["items"] = s["items"].clone();
        } else {
            out["reason"] = json!("尚无扫描结果");
        }
        Ok(out)
    }
}
