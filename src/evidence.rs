//! Read-only external inputs. Durable offsets commit with evidence, never before it.
use crate::{domain::text, monitor::Monitor};
use anyhow::{Context, Result, ensure};
use regex::Regex;
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::{self, File},
    io::{BufRead, BufReader, Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::{Arc, LazyLock, Mutex},
    time::Duration,
};
static ITEM: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?:rs|msg|fc|ctc|ctco|at)_([0-9a-f]{40,64})").unwrap());
static MODEL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"[ ="':]model[ ="':]+([A-Za-z0-9][A-Za-z0-9._\-]{2,40})"#).unwrap()
});
static TURN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"turn(?:\.id=|_id=)([0-9a-f\-]{36})").unwrap());
static THREAD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"thread(?:\.id=|_id=)([0-9a-f\-]{36})").unwrap());
static EFFORT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"reasoning_effort[ ="':]+([a-z_]+)"#).unwrap());
pub fn home() -> PathBuf {
    std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("USERPROFILE").unwrap_or_default()).join(".codex")
        })
}
fn capture(regex: &Regex, s: &str) -> Value {
    regex
        .captures(s)
        .map(|c| json!(&c[1]))
        .unwrap_or(Value::Null)
}
pub fn parse_log(body: &str) -> Vec<Value> {
    if !body.contains("item_id") {
        return vec![];
    }
    let models: BTreeSet<_> = MODEL
        .captures_iter(body)
        .map(|m| m[1].to_owned())
        .filter(|m| !m.starts_with("resp_"))
        .collect();
    if models.len() != 1 {
        return vec![];
    }
    ITEM.captures_iter(body).map(|c|json!({"key_kind":"item","key_value":&c[1][..26],"model":models.first().unwrap(),"effort":capture(&EFFORT,body),"turn_id":capture(&TURN,body),"thread_id":capture(&THREAD,body),"source":"codex_log_prefix"})).collect()
}
fn uri(path: &Path) -> String {
    let path = path.to_string_lossy().replace('\\', "/");
    let path = path.strip_prefix("//?/").unwrap_or(&path);
    let mut out = String::from("file:");
    for b in path.bytes() {
        if b.is_ascii_alphanumeric() || b"/:._-".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out.push_str("?mode=ro&readonly_shm=1");
    out
}
/// Pin the existing SHM against deletion. SQLite's Win32 readonly_shm mode still
/// uses OPEN_ALWAYS: an existence check alone would leave a create-file race.
fn readonly_database(path: &Path) -> Result<(rusqlite::Connection, Vec<File>)> {
    use std::os::windows::fs::OpenOptionsExt;
    let pin =
        |p: &Path| -> Result<File> { Ok(fs::OpenOptions::new().read(true).share_mode(3).open(p)?) };
    let mut db_file = pin(path)?;
    let mut header = [0u8; 100];
    db_file.read_exact(&mut header)?;
    ensure!(&header[..16] == b"SQLite format 3\0", "外部数据库头无效");
    let mut handles = vec![db_file];
    if header[18] == 2 || header[19] == 2 {
        for suffix in ["-wal", "-shm"] {
            handles.push(
                pin(&PathBuf::from(format!("{}{suffix}", path.display())))
                    .context("外部 WAL/SHM 不满足只读条件，本轮暂缓")?,
            );
        }
    }
    let db = rusqlite::Connection::open_with_flags(
        uri(path),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
            | rusqlite::OpenFlags::SQLITE_OPEN_URI
            | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    db.busy_timeout(Duration::from_millis(200))?;
    db.execute_batch("PRAGMA query_only=ON; PRAGMA temp_store=MEMORY;")?;
    Ok((db, handles))
}

fn log_poll(
    home: &Path,
    state: &Value,
    stop: &impl Fn() -> bool,
) -> Result<(Vec<Value>, Value, Value)> {
    let mut paths = fs::read_dir(home)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.file_name()
                .and_then(|s| s.to_str())
                .is_some_and(|s| s.starts_with("logs") && s.ends_with(".sqlite"))
        })
        .collect::<Vec<_>>();
    paths.sort_by_key(|p| (fs::metadata(p).and_then(|m| m.modified()).ok(), p.clone()));
    let path = paths.last().context("未找到 logs*.sqlite")?;
    let name = path.file_name().unwrap().to_string_lossy().to_string();
    let last = if state["path"] == name {
        state["last_id"].as_i64().unwrap_or(0)
    } else {
        0
    };
    let (mut newest, mut count) = (last, 0);
    let mut entries = vec![];
    let (db, _pins) = readonly_database(path)?;
    let mut stmt =
        db.prepare("SELECT id,feedback_log_body FROM logs WHERE id>? ORDER BY id LIMIT 20000")?;
    let mut rows = stmt.query([last])?;
    while !stop() {
        let Some(row) = rows.next()? else {
            break;
        };
        newest = row.get(0)?;
        count += 1;
        let body: Option<String> = row.get(1)?;
        if let Some(body) = body {
            entries.extend(parse_log(&body));
        }
    }
    Ok((
        entries,
        json!({"path":name,"last_id":newest}),
        json!({"rows":count,"last_id":newest,"db":name}),
    ))
}
fn paths(home: &Path) -> Vec<PathBuf> {
    fn visit(path: &Path, depth: usize, out: &mut Vec<PathBuf>) {
        let Ok(dir) = fs::read_dir(path) else {
            return;
        };
        for item in dir.flatten() {
            let p = item.path();
            if p.is_dir() && depth > 0 {
                visit(&p, depth - 1, out);
            } else if p.is_file()
                && p.file_name()
                    .and_then(|s| s.to_str())
                    .is_some_and(|s| s.starts_with("rollout-") && s.ends_with(".jsonl"))
            {
                out.push(p);
            }
        }
    }
    let mut files = vec![];
    visit(&home.join("sessions"), 3, &mut files);
    visit(&home.join("archived_sessions"), 0, &mut files);
    files.sort_by_key(|p| std::cmp::Reverse(fs::metadata(p).and_then(|m| m.modified()).ok()));
    files.truncate(400);
    files
}
pub fn read_rollout(
    path: &Path,
    prev: &Value,
    budget: usize,
    stop: &impl Fn() -> bool,
) -> Result<(Vec<Value>, Value, usize)> {
    let mut offset = prev["offset"].as_u64().unwrap_or(0);
    let mut turns = prev
        .get("turns")
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or(json!({}));
    let mut pending = prev["pending"].as_array().cloned().unwrap_or_default();
    for p in &mut pending {
        if let Some(a) = p.as_array_mut() {
            a.resize(4, Value::Null);
            a.truncate(4);
        }
    }
    let size = fs::metadata(path)?.len();
    if offset > size {
        offset = 0;
        turns = json!({});
        pending.clear();
    }
    let mut reader = BufReader::new(File::open(path)?);
    reader.seek(SeekFrom::Start(offset))?;
    let mut used = 0usize;
    let mut session = Value::Null;
    let mut line = vec![];
    while !stop() && used < budget {
        line.clear();
        let count = reader.read_until(b'\n', &mut line)?;
        if count == 0 || line.last() != Some(&b'\n') {
            break;
        }
        offset += count as u64;
        used += count;
        let Ok(obj) = serde_json::from_slice::<Value>(&line) else {
            continue;
        };
        let payload = &obj["payload"];
        if obj["type"] == "turn_context" {
            let id = text(payload, "turn_id");
            if !id.is_empty() {
                turns[id] = json!({"model":payload["model"],"effort":payload["effort"]});
            }
        } else if obj["type"] == "session_meta" {
            session = payload["session_id"].clone();
        } else if payload["response_id"].is_string() {
            let timestamp = obj["timestamp"]
                .as_str()
                .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
                .map(|d| d.timestamp_millis() as f64 / 1000.);
            pending.push(json!([
                payload["response_id"],
                payload["turn_id"],
                payload
                    .get("thread_id")
                    .filter(|v| !v.is_null())
                    .unwrap_or(&session),
                timestamp
            ]));
        }
    }
    let mut waiting = vec![];
    let mut entries = vec![];
    for p in pending {
        let Some(a) = p.as_array().filter(|a| a.len() >= 4) else {
            continue;
        };
        let turn = &turns[a[1].as_str().unwrap_or("")];
        if turn["model"].is_string() {
            entries.push(json!({"key_kind":"resp_id","key_value":a[0],"model":turn["model"],"effort":turn["effort"],"turn_id":a[1],"thread_id":a[2],"observed_at":a[3],"source":"rollout_token_usage"}));
        } else if waiting.len() < 2000 {
            waiting.push(p);
        }
    }
    Ok((
        entries,
        json!({"offset":offset,"turns":turns,"pending":waiting}),
        used,
    ))
}
fn rollout_poll(
    home: &Path,
    old: &Value,
    stop: &impl Fn() -> bool,
) -> Result<(Vec<Value>, Value, Value)> {
    let mut state = json!({});
    let mut entries = vec![];
    let mut budget = 32 * 1024 * 1024usize;
    let mut touched = 0;
    for path in paths(home) {
        let key = path.to_string_lossy().to_string();
        let prev = old.get(&key).cloned().unwrap_or(json!({}));
        if stop() || budget == 0 {
            state[&key] = prev;
            continue;
        }
        match read_rollout(&path, &prev, budget, &stop) {
            Ok((found, next, used)) => {
                budget = budget.saturating_sub(used);
                touched += usize::from(used > 0);
                state[&key] = next;
                entries.extend(found);
            }
            Err(e) => {
                let mut next = prev;
                next["error"] = json!(e.to_string());
                state[&key] = next;
            }
        }
    }
    let note = json!({"files_read":touched,"files_tracked":state.as_object().unwrap().len(),"new":entries.len()});
    Ok((entries, state, note))
}
pub fn run(monitor: Arc<Mutex<Monitor>>, stop: tokio::sync::watch::Receiver<bool>) -> Result<()> {
    let home = home();
    let stopped = || *stop.borrow();
    while !stopped() {
        let (old_log, old_rollout) = {
            let m = monitor.lock().unwrap();
            (
                m.store.state("codex_log_cursor", json!({}))?,
                m.store.state("rollout_files", json!({}))?,
            )
        };
        let mut entries = vec![];
        let mut states = vec![];
        let mut notes = json!({});
        for (name, result, old, key) in [
            (
                "codex_log_prefix",
                log_poll(&home, &old_log, &stopped),
                &old_log,
                "codex_log_cursor",
            ),
            (
                "rollout_token_usage",
                rollout_poll(&home, &old_rollout, &stopped),
                &old_rollout,
                "rollout_files",
            ),
        ] {
            match result {
                Ok((found, next, note)) => {
                    entries.extend(found);
                    if &next != old {
                        states.push((key.to_owned(), next));
                    }
                    notes[name] = note;
                }
                Err(e) => notes[name] = json!({"error":e.to_string()}),
            }
        }
        let mut m = monitor.lock().unwrap();
        let persist = (|| -> Result<()> {
            if !entries.is_empty() || !states.is_empty() {
                let conflicts = m.store.checkpoint(&entries, &states)?;
                m.evidence_stats["conflicts"] =
                    json!(m.evidence_stats["conflicts"].as_u64().unwrap_or(0) + conflicts as u64);
                if !entries.is_empty() {
                    m.reload_index()?;
                }
            }
            m.evidence_stats["polls"] = json!(m.evidence_stats["polls"].as_u64().unwrap_or(0) + 1);
            m.evidence_stats["last_poll"] = json!(crate::domain::now());
            m.evidence_stats["codex_home"] = json!(home);
            m.evidence_stats["log_db"] = notes["codex_log_prefix"]["db"].clone();
            m.evidence_stats["log_last_id"] = notes["codex_log_prefix"]["last_id"].clone();
            m.evidence_stats["log_rows_indexed"] = json!(
                m.evidence_stats["log_rows_indexed"].as_u64().unwrap_or(0)
                    + notes["codex_log_prefix"]["rows"].as_u64().unwrap_or(0)
            );
            m.evidence_stats["rollout_files"] =
                notes["rollout_token_usage"]["files_tracked"].clone();
            m.evidence_stats["sources"] = notes;
            m.backfill()?;
            Ok(())
        })();
        if let Err(e) = persist {
            m.evidence_stats["last_error"] = json!(e.to_string());
            m.log("error", format!("证据事务失败，保留原游标重试：{e}"));
        }
        drop(m);
        crate::app::interruptible_sleep(&stop, Duration::from_secs(2));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wal_reads_do_not_modify_or_create_external_files() {
        let dir = crate::tests::Sandbox::new();
        let path = dir.0.join("external.sqlite");
        let writer = rusqlite::Connection::open(&path).unwrap();
        writer.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE logs(id INTEGER PRIMARY KEY,feedback_log_body TEXT); INSERT INTO logs VALUES(1,'test');").unwrap();
        let files = [
            path.clone(),
            PathBuf::from(format!("{}-wal", path.display())),
            PathBuf::from(format!("{}-shm", path.display())),
        ];
        let before = files
            .iter()
            .map(fs::read)
            .collect::<std::io::Result<Vec<_>>>()
            .unwrap();
        // A separate process models the real external writer. SQLite reuses a writable
        // SHM mapping for connections inside the SAME process, regardless of URI flags.
        use std::os::windows::process::CommandExt;
        let child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "evidence::tests::readonly_child",
                "--ignored",
                "--nocapture",
            ])
            .env("MONITOR_TEST_READ_DB", &path)
            .creation_flags(0x08000000)
            .output()
            .unwrap();
        assert!(
            child.status.success(),
            "{}",
            String::from_utf8_lossy(&child.stdout)
        );
        let after = files
            .iter()
            .map(fs::read)
            .collect::<std::io::Result<Vec<_>>>()
            .unwrap();
        for (i, (a, b)) in before.iter().zip(&after).enumerate() {
            assert!(
                a == b,
                "external file {i} changed at {:?}",
                a.iter().zip(b).position(|(a, b)| a != b)
            );
        }
        drop(writer);
        assert!(!files[1].exists());
        assert!(!files[2].exists());
        assert!(readonly_database(&path).is_err());
        assert!(!files[1].exists());
        assert!(!files[2].exists());
    }
    #[test]
    fn log_model_conflicts_are_not_guessed() {
        let body = " item_id=msg_1234567890123456789012345678901234567890 model=expected reasoning_effort=high";
        assert_eq!(parse_log(body)[0]["model"], "expected");
        assert_eq!(
            parse_log(body)[0]["key_value"],
            "1234567890123456789012345678"[..26]
        );
        assert!(parse_log(&format!("{body} model=other")).is_empty());
    }
    #[test]
    #[ignore = "subprocess helper invoked by wal_reads_do_not_modify_or_create_external_files"]
    fn readonly_child() {
        let path = PathBuf::from(std::env::var_os("MONITOR_TEST_READ_DB").unwrap());
        let (reader, _pins) = readonly_database(&path).unwrap();
        assert_eq!(
            reader
                .query_row("SELECT COUNT(*) FROM logs", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
    }
}
