//! Isolated read-only memory fixture; never included in the release archive.
use std::io::{BufRead, Write};
fn main() {
    let args: Vec<_> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--hold-mutex") {
        use windows_sys::Win32::{
            Foundation::CloseHandle,
            System::Threading::{CreateMutexW, ReleaseMutex},
        };
        let name: Vec<u16> = args
            .get(2)
            .expect("mutex name")
            .encode_utf16()
            .chain(Some(0))
            .collect();
        unsafe {
            let mutex = CreateMutexW(std::ptr::null(), 1, name.as_ptr());
            assert!(!mutex.is_null());
            println!("ready mutex");
            std::io::stdout().flush().unwrap();
            let _ = std::io::stdin().read_line(&mut String::new());
            assert_ne!(ReleaseMutex(mutex), 0);
            CloseHandle(mutex);
        }
        return;
    }
    if args.get(1).map(String::as_str) == Some("--hold-db") {
        let db = rusqlite::Connection::open(args.get(2).expect("database path")).unwrap();
        db.execute_batch("BEGIN IMMEDIATE").unwrap();
        println!("ready db");
        std::io::stdout().flush().unwrap();
        let _ = std::io::stdin().read_line(&mut String::new());
        db.execute_batch("ROLLBACK").unwrap();
        return;
    }
    if args.get(1).map(String::as_str) == Some("--inspect-db") {
        let db = rusqlite::Connection::open_with_flags(
            args.get(2).expect("database path"),
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .unwrap();
        let mut report = serde_json::json!({});
        for table in [
            "responses",
            "response_events",
            "request_evidence",
            "request_model_index",
            "evidence_state",
            "collector_logs",
        ] {
            report[table] = serde_json::json!(
                db.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                    .get::<_, i64>(0))
                    .unwrap()
            );
        }
        report["integrity"] = serde_json::json!(
            db.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
                .unwrap()
        );
        report["user_version"] = serde_json::json!(
            db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
                .unwrap()
        );
        report["data_version"] = serde_json::json!(
            db.query_row("SELECT version FROM data_version WHERE id=1", [], |r| {
                r.get::<_, String>(0)
            })
            .unwrap()
        );
        println!("{report}");
        return;
    }
    if args.get(1).map(String::as_str) == Some("--legacy-db") {
        let path = std::path::Path::new(args.get(2).expect("database path"));
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let db = rusqlite::Connection::open(path).unwrap();
        db.execute_batch(include_str!("fixtures/legacy-v040.sql"))
            .unwrap();
        db.execute_batch("PRAGMA user_version=1").unwrap();
        return;
    }
    let mut memory = vec![b'P'; 64 * 1024 * 1024];
    let mut objects = Vec::new();
    for (name, model, prev, effort) in [
        ("normal", "expected", "resp_p1", "high"),
        ("down", "different", "resp_p2", "high"),
        ("sub", "small", "resp_p3", "low"),
        ("unknown", "different", "resp_absent", "high"),
        ("boundary", "expected", "resp_p1", "high"),
    ] {
        objects.push(format!(r#"{{"id":"resp_{name}","object":"response","model":"{model}","status":"completed","created_at":1700000000,"completed_at":1700000010,"previous_response_id":"{prev}","reasoning":{{"effort":"{effort}"}}}}"#));
    }
    for (prev, model) in [
        ("resp_p1", "expected"),
        ("resp_p2", "expected"),
        ("resp_p3", "small"),
    ] {
        objects.push(format!(
            r#"{{"type":"response.create","model":"{model}","previous_response_id":"{prev}"}}"#
        ));
    }
    objects.push(format!(r#"{{"type":"response.create","input":["{}"],"model":"late-large","previous_response_id":"resp_large"}}"#,"x".repeat(600000)));
    for (i, obj) in objects.iter().enumerate() {
        let offset = if i == 4 {
            2 * 1024 * 1024 - 30
        } else {
            4 * 1024 * 1024 + i * 3 * 1024 * 1024
        };
        memory[offset..offset + obj.len()].copy_from_slice(obj.as_bytes());
    }
    let checksum = memory
        .iter()
        .fold(0u64, |sum, b| sum.wrapping_add(*b as u64));
    println!("ready {}", std::process::id());
    std::io::stdout().flush().unwrap();
    for line in std::io::stdin().lock().lines() {
        match line.unwrap_or_default().as_str() {
            "check" => {
                assert_eq!(
                    memory
                        .iter()
                        .fold(0u64, |sum, b| sum.wrapping_add(*b as u64)),
                    checksum
                );
                println!("unchanged");
                std::io::stdout().flush().unwrap();
            }
            _ => break,
        }
    }
    std::hint::black_box(memory);
}
