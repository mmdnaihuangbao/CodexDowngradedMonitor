use serde_json::{Value, json};

pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
pub fn rank(status: &str) -> u8 {
    match status {
        "queued" => 1,
        "in_progress" => 2,
        "failed" | "incomplete" | "cancelled" => 3,
        "completed" => 4,
        _ => 0,
    }
}
pub fn truth(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
pub fn verdict(rec: &Value, request: Option<&str>, expect: &str) -> String {
    let legacy = rec.get("_expect").is_none() && rec.get("_verdict").is_some();
    if let Some(req) = request {
        if req != text(rec, "model") {
            return "downgrade".into();
        }
        if legacy {
            return if rec["_verdict"] == "incomplete" {
                "normal"
            } else {
                text(rec, "_verdict")
            }
            .into();
        }
        let target = rec["_expect"].as_str().unwrap_or(expect);
        return if target.is_empty() || target == req {
            "normal"
        } else {
            "subtask"
        }
        .into();
    }
    if legacy {
        return text(rec, "_verdict").into();
    }
    let target = rec["_expect"].as_str().unwrap_or(expect);
    if !target.is_empty() && target == text(rec, "model") {
        "normal"
    } else if !target.is_empty() && rec["effort"] == "low" {
        "subtask"
    } else {
        "incomplete"
    }
    .into()
}
pub fn validation_reason(rec: &Value, time: f64) -> String {
    let mut reasons = Vec::new();
    match crate::config::number(&rec["created_at"]) {
        Some(t) if (946684800. ..=time + 86400.).contains(&t) => {}
        Some(_) => reasons.push("创建时间异常"),
        None => reasons.push("缺少创建时间"),
    }
    if !rec["completed_at"].is_null()
        && !crate::config::number(&rec["completed_at"])
            .is_some_and(|t| (946684800. ..=time + 86400.).contains(&t))
    {
        reasons.push("完成时间异常");
    }
    if rank(text(rec, "status")) == 0 {
        reasons.push("缺少有效请求状态");
    }
    reasons.join("；")
}
pub fn serialize(rec: &Value) -> Value {
    let mut out = json!({});
    for (to, from) in [
        ("rid", "response_id"),
        ("model", "model"),
        ("req_model", "_req_model"),
        ("expect", "_expect"),
        ("pairing_status", "_pairing_status"),
        ("evidence_source", "_evidence_source"),
        ("suspect_reason", "_suspect_reason"),
        ("effort", "effort"),
        ("status", "status"),
        ("prev", "prev"),
        ("text_format", "text_format"),
        ("safety_id", "safety_id"),
        ("marks", "_marks"),
        ("first_seen", "_first_seen"),
        ("last_seen", "_last_seen"),
    ] {
        out[to] = rec[from].clone();
    }
    out["verdict"] = rec.get("_verdict").cloned().unwrap_or(json!("normal"));
    out["updates"] = rec.get("_updates").cloned().unwrap_or(json!(0));
    let created = crate::config::number(&rec["created_at"]);
    let completed = crate::config::number(&rec["completed_at"]);
    out["duration_seconds"] = match (created, completed) {
        (Some(a), Some(b)) if a > 0. && b.trunc() >= a.trunc() => {
            json!((b.trunc() - a.trunc()) as i64)
        }
        _ => Value::Null,
    };
    for k in ["created_at", "completed_at"] {
        out[k] = crate::config::number(&rec[k])
            .filter(|t| *t > 0.)
            .and_then(|t| chrono::DateTime::from_timestamp(t as i64, 0))
            .map(|t| {
                json!(
                    t.with_timezone(&chrono::Local)
                        .format("%Y-%m-%d %H:%M:%S")
                        .to_string()
                )
            })
            .unwrap_or(Value::Null);
    }
    out
}
