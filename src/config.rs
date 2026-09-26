//! Legacy config.json contract. Ordinary loads never rewrite a user's file.
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};

pub fn defaults() -> Value {
    json!({"version":"0.1","host":"localhost","cpp_port":48778,
        "expect":"","min_interval_ms":0,"workers":4})
}

pub fn number(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_str()?.parse().ok())
        .filter(|n| n.is_finite())
}

pub fn validate(input: &Value) -> Result<Value> {
    let mut out = defaults();
    let incoming = input.as_object().context("配置必须是 JSON 对象")?;
    out.as_object_mut().unwrap().extend(incoming.clone());
    out.as_object_mut().unwrap().remove("port");
    ensure!(out["version"] == "0.1", "不支持的配置版本");
    let expect = out["expect"]
        .as_str()
        .context("预期模型必须是字符串")?
        .trim()
        .to_owned();
    out["expect"] = json!(expect);
    let host = out["host"].as_str().context("监听地址须为字符串")?;
    ensure!(
        !host.is_empty()
            && host
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c)),
        "监听地址无效"
    );
    for (key, low, high, integer) in [
        ("cpp_port", 1., 65535., true),
        ("workers", 1., 16., true),
        ("min_interval_ms", 0., 60000., false),
    ] {
        let n = number(&out[key]).with_context(|| format!("{key} 必须是有效数字"))?;
        ensure!(
            (low..=high).contains(&n) && (!integer || n.fract() == 0.),
            "{key} 超出范围或不是整数"
        );
        out[key] = if n.fract() == 0. {
            json!(n as u64)
        } else {
            json!(n)
        };
    }
    Ok(out)
}

pub fn load(root: &Path) -> Result<Value> {
    let path = root.join("config.json");
    match fs::read_to_string(&path) {
        Ok(text) => validate(
            &serde_json::from_str::<Value>(text.trim_start_matches('\u{feff}'))
                .context("config.json 不是有效 JSON")?,
        ),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(defaults()),
        Err(e) => Err(e).context("读取 config.json 失败"),
    }
}

/// Reject reparse points at every output component, including an existing target.
/// Output paths are private, fixed names; external read-only evidence paths do not use this.
pub fn local_path(root: &Path, relative: &str) -> Result<PathBuf> {
    use std::os::windows::fs::MetadataExt;
    let mut path = root.to_path_buf();
    for part in Path::new(relative).components() {
        if let std::path::Component::Normal(name) = part {
            path.push(name);
        } else {
            bail!("输出路径必须位于程序目录内");
        }
        match fs::symlink_metadata(&path) {
            Ok(m) => ensure!(
                m.file_attributes() & 0x400 == 0,
                "输出路径不可包含链接或目录联接：{}",
                path.display()
            ),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(path)
}

pub fn prepare(root: &Path) -> Result<()> {
    for rel in ["data", "data/tmp", "data/logs", "data/backups"] {
        fs::create_dir_all(local_path(root, rel)?)
            .with_context(|| format!("无法写入程序目录 {rel}，不会回退用户目录"))?;
    }
    for rel in [
        "config.json",
        "data/monitor.sqlite",
        "data/monitor.sqlite-wal",
        "data/monitor.sqlite-shm",
        "data/monitor.sqlite-journal",
    ] {
        local_path(root, rel)?;
    }
    Ok(())
}

/// Persist only the submitted settings; command-line overrides remain runtime-only.
pub fn save_updates(root: &Path, runtime: &Value, updates: &Value) -> Result<Value> {
    let changes = updates.as_object().context("配置更新必须是 JSON 对象")?;
    let mut stored = load(root)?;
    stored.as_object_mut().unwrap().extend(changes.clone());
    let stored = validate(&stored)?;
    let mut effective = runtime.clone();
    effective.as_object_mut().unwrap().extend(changes.clone());
    let effective = validate(&effective)?;
    save(root, &stored)?;
    Ok(effective)
}

pub fn save(root: &Path, config: &Value) -> Result<Value> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
    };
    let checked = validate(config)?;
    let target = local_path(root, "config.json")?;
    let temp = local_path(root, "data/tmp/config.next")?;
    let result = (|| -> Result<()> {
        let mut file = fs::File::create(&temp)?;
        file.write_all(serde_json::to_string_pretty(&checked)?.as_bytes())?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        drop(file);
        let from: Vec<u16> = temp.as_os_str().encode_wide().chain(Some(0)).collect();
        let to: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
        // SAFETY: both paths are NUL-terminated and remain alive through the call.
        if unsafe {
            MoveFileExW(
                from.as_ptr(),
                to.as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        } == 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(temp);
    }
    result.context("保存配置失败")?;
    Ok(checked)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn legacy_config_validation() {
        let v = validate(
            &json!({"port":123,"workers":"4","min_interval_ms":0.5,"expect":" test ","custom":7}),
        )
        .unwrap();
        assert!(v.get("port").is_none());
        assert_eq!(v["expect"], "test");
        assert_eq!(v["custom"], 7);
        for v in [
            json!({"workers":true}),
            json!({"workers":1.5}),
            json!({"cpp_port":0}),
            json!({"version":"0.5"}),
            json!({"host":"../x"}),
            json!({"expect":null}),
        ] {
            assert!(validate(&v).is_err(), "{v}");
        }
    }
}
