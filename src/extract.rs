//! Bounded, byte-oriented metadata extraction. Never decode or retain prompt bodies.
use serde_json::{Value, json};
use std::collections::BTreeMap;
pub const WINDOW: usize = 256 * 1024;
pub const MAX_WINDOW: usize = 4 * 1024 * 1024;
const KEYS: &[&[u8]] = &[
    b"id",
    b"type",
    b"model",
    b"previous_response_id",
    b"object",
    b"client_metadata",
    b"safety_identifier",
    b"frequency_penalty",
    b"presence_penalty",
    b"completed_at",
    b"max_output_tokens",
    b"status",
    b"created_at",
    b"reasoning",
    b"text",
    b"format",
    b"input",
    b"effort",
    b"thread_id",
    b"session_id",
    b"turn_id",
    b"root_turn_id",
];
#[derive(Default, Debug)]
pub struct Batch {
    pub responses: Vec<Value>,
    pub requests: Vec<Value>,
    pub candidates: u64,
    pub truncated: u64,
    pub retries: Vec<usize>,
}
type Fields<'a> = BTreeMap<&'a [u8], &'a [u8]>;
fn ws(s: &[u8], p: &mut usize) {
    while s.get(*p).is_some_and(|c| b" \r\n\t".contains(c)) {
        *p += 1;
    }
}
fn string_end(s: &[u8], p: &mut usize) -> bool {
    if s.get(*p) != Some(&b'"') {
        return false;
    }
    *p += 1;
    while let Some(&c) = s.get(*p) {
        *p += 1;
        if c == b'"' {
            return true;
        }
        if c < 32 {
            return false;
        }
        if c == b'\\' {
            let Some(&e) = s.get(*p) else {
                return false;
            };
            *p += 1;
            if e == b'u' {
                for _ in 0..4 {
                    if !s.get(*p).is_some_and(u8::is_ascii_hexdigit) {
                        return false;
                    }
                    *p += 1;
                }
            }
        }
    }
    false
}
fn skip(s: &[u8], p: &mut usize, depth: u32) -> bool {
    ws(s, p);
    if *p >= s.len() || depth > 64 {
        return false;
    }
    if s[*p] == b'"' {
        return string_end(s, p);
    }
    if s[*p] == b'{' || s[*p] == b'[' {
        let close = if s[*p] == b'{' { b'}' } else { b']' };
        *p += 1;
        while *p < s.len() {
            let c = s[*p];
            if c == close {
                *p += 1;
                return true;
            }
            if b"\"{[".contains(&c) {
                if !skip(s, p, depth + 1) {
                    return false;
                }
            } else {
                if c == 0 || c == b'}' || c == b']' {
                    return false;
                }
                *p += 1;
            }
        }
        return false;
    }
    let start = *p;
    while *p < s.len() && !b",}] \r\n\t".contains(&s[*p]) {
        if s[*p] == 0 {
            return false;
        }
        *p += 1;
    }
    *p > start
}
fn fields(s: &[u8]) -> (Fields<'_>, usize) {
    let mut out = Fields::new();
    let mut p = 0;
    ws(s, &mut p);
    if s.get(p) != Some(&b'{') {
        return (out, 0);
    }
    p += 1;
    for _ in 0..128 {
        ws(s, &mut p);
        if p >= s.len() {
            break;
        }
        if s[p] == b'}' {
            return (out, p + 1);
        }
        let k = p;
        if !string_end(s, &mut p) {
            break;
        }
        let key = &s[k + 1..p - 1];
        if key.contains(&b'\\') {
            return (Fields::new(), 0);
        }
        ws(s, &mut p);
        if s.get(p) != Some(&b':') {
            break;
        }
        p += 1;
        ws(s, &mut p);
        let start = p;
        if !skip(s, &mut p, 0) {
            break;
        }
        if KEYS.contains(&key) && out.insert(key, &s[start..p]).is_some() {
            return (Fields::new(), 0);
        }
        ws(s, &mut p);
        if p >= s.len() {
            break;
        }
        if s[p] == b'}' {
            return (out, p + 1);
        }
        if s[p] != b',' {
            break;
        }
        p += 1;
    }
    (out, 0)
}
fn scalar(v: &[u8]) -> String {
    if v.is_empty() || v.len() > 256 || v == b"null" {
        return String::new();
    }
    let text = if v.first() == Some(&b'"') && v.last() == Some(&b'"') {
        let x = &v[1..v.len() - 1];
        if x.contains(&b'\\') {
            return String::new();
        }
        x
    } else if v.iter().all(u8::is_ascii_digit) {
        v
    } else {
        return String::new();
    };
    std::str::from_utf8(text).unwrap_or("").to_owned()
}
fn value(f: &Fields<'_>, key: &[u8]) -> String {
    scalar(f.get(key).copied().unwrap_or_default())
}
fn rid(s: &str) -> bool {
    s.len() > 5
        && s.starts_with("resp_")
        && s.as_bytes()[5..]
            .iter()
            .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(c))
}
pub fn candidate(data: &[u8]) -> bool {
    memchr::memmem::find(data, b"\"model\"").is_some()
        || memchr::memmem::find(data, b"\"response.create\"").is_some()
}
pub fn extract(data: &[u8], limit: usize) -> Batch {
    let mut out = Batch::default();
    let mut p = 0;
    while p < data.len().min(limit) {
        let Some(relative) = memchr::memchr(b'{', &data[p..data.len().min(limit)]) else {
            break;
        };
        p += relative;
        let start = p;
        p += 1;
        if out.responses.len() + out.requests.len() >= 10000 {
            break;
        }
        let s = &data[start..data.len().min(start + MAX_WINDOW)];
        let mut k = 1;
        ws(s, &mut k);
        let key_start = k;
        if !string_end(s, &mut k) {
            continue;
        }
        let first = &s[key_start + 1..k - 1];
        if first == b"type" || first == b"id" {
            ws(s, &mut k);
            if s.get(k) != Some(&b':') {
                continue;
            }
            k += 1;
            ws(s, &mut k);
            let vstart = k;
            if !string_end(s, &mut k) {
                continue;
            }
            let v = &s[vstart + 1..k - 1];
            if (first == b"type" && v != b"response.create")
                || (first == b"id" && !v.starts_with(b"resp_"))
            {
                continue;
            }
        }
        out.candidates += 1;
        let (f, complete) = fields(s);
        let model = value(&f, b"model");
        if complete == 0 {
            out.truncated += 1;
            if s.len() < MAX_WINDOW
                && (value(&f, b"type") == "response.create"
                    || f.contains_key(b"client_metadata".as_slice()))
            {
                out.retries.push(start);
            }
            continue;
        }
        if model.is_empty() {
            continue;
        }
        let kind = value(&f, b"type");
        let http = kind.is_empty()
            && f.contains_key(b"input".as_slice())
            && f.contains_key(b"client_metadata".as_slice())
            && !f.contains_key(b"id".as_slice())
            && !f.contains_key(b"object".as_slice());
        let previous = value(&f, b"previous_response_id");
        if kind == "response.create" || http {
            let mut r = json!({"model":model,"source":if http{"memory_http_candidate"}else{"memory_websocket"}});
            if rid(&previous) {
                r["prev"] = json!(previous);
            }
            if http {
                r["candidate_only"] = json!("1");
            }
            if let Some(meta) = f.get(b"client_metadata".as_slice()) {
                let context = fields(meta).0;
                for name in ["thread_id", "session_id", "turn_id", "root_turn_id"] {
                    let v = value(&context, name.as_bytes());
                    if !v.is_empty() {
                        r[name] = json!(v);
                    }
                }
            }
            out.requests.push(r);
            p = start + complete;
            continue;
        }
        let id = value(&f, b"id");
        if !rid(&id) || f.contains_key(b"client_metadata".as_slice()) {
            continue;
        }
        let marks = [
            b"safety_identifier".as_slice(),
            b"frequency_penalty",
            b"presence_penalty",
            b"completed_at",
            b"max_output_tokens",
        ]
        .iter()
        .filter(|k| f.contains_key(**k))
        .count()
            + usize::from(value(&f, b"object") == "response");
        if marks < 2 {
            continue;
        }
        let mut r = json!({"response_id":id,"model":model,"_marks":marks});
        for key in ["status", "created_at", "completed_at"] {
            let v = value(&f, key.as_bytes());
            if !v.is_empty() {
                r[key] = json!(v);
            }
        }
        if rid(&previous) {
            r["prev"] = json!(previous);
        }
        if let Some(v) = f.get(b"reasoning".as_slice()) {
            let effort = value(&fields(v).0, b"effort");
            if !effort.is_empty() {
                r["effort"] = json!(effort);
            }
        }
        let text_fields = f.get(b"text".as_slice()).map(|v| fields(v).0);
        let format = if let Some(ref t) = text_fields {
            t.get(b"format".as_slice())
        } else {
            f.get(b"format".as_slice())
        };
        if let Some(v) = format {
            r["text_format"] = json!(value(&fields(v).0, b"type"));
        }
        out.responses.push(r);
        p = start + complete;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nested_adjacent_duplicate_and_candidates() {
        let data=br#"{"type":"response.create","input":[{"model":"wrong"}],"model":"request","previous_response_id":"resp_parent"} {"id":"resp_real","object":"response","output":[{"model":"wrong"}],"model":"actual","completed_at":100}"#;
        let b = extract(data, usize::MAX);
        assert_eq!(b.requests[0]["model"], "request");
        assert_eq!(b.responses[0]["model"], "actual");
        for bad in [
            br#"{"id":"resp_bad","model":"wrong"}{"object":"response","completed_at":100}"#
                .as_slice(),
            br#"{"id":"resp_x","object":"response","model":"a","model":"b","completed_at":100}"#,
        ] {
            assert!(extract(bad, usize::MAX).responses.is_empty());
        }
        let b=extract(br#"{"input":[],"model":"a","client_metadata":{"turn_id":"turn"},"previous_response_id":"resp_p"}"#,usize::MAX);
        assert_eq!(b.requests[0]["candidate_only"], "1");
        assert_eq!(b.requests[0]["turn_id"], "turn");
    }
    #[test]
    fn overlap_and_large_input() {
        let object =
            br#"{"id":"resp_boundary","object":"response","model":"actual","completed_at":100}"#;
        let chunk = 1 << 20;
        let mut memory = vec![b'P'; chunk * 2];
        memory[chunk - 30..chunk - 30 + object.len()].copy_from_slice(object);
        assert!(extract(&memory[..chunk], chunk).responses.is_empty());
        assert_eq!(extract(&memory[..chunk + WINDOW], chunk).responses.len(), 1);
        let data = format!(
            r#"{{"type":"response.create","input":["{}"],"model":"actual","previous_response_id":"resp_p"}}"#,
            "x".repeat(600000)
        );
        assert_eq!(
            extract(data.as_bytes(), usize::MAX).requests[0]["prev"],
            "resp_p"
        );
        assert_eq!(
            extract(&data.as_bytes()[..WINDOW], usize::MAX).retries,
            vec![0]
        );
    }
}
