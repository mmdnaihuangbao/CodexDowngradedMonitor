//! Per-user control channel. No filesystem discovery and no dependency on HTTP/SQLite locks.
use crate::platform;
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    os::windows::io::AsRawHandle,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::windows::named_pipe::{ClientOptions, NamedPipeServer, ServerOptions},
    sync::watch,
};

pub type Status = Arc<Mutex<Value>>;
pub fn pipe_name(sid: &str) -> String {
    format!(r"\\.\pipe\CodexDowngradedMonitor-{sid}-control")
}
pub fn create(sid: &str, first: bool) -> Result<NamedPipeServer> {
    let mut security = platform::Security::user(sid)?;
    // SAFETY: SECURITY_ATTRIBUTES and its descriptor live until CreateNamedPipe returns.
    Ok(unsafe {
        ServerOptions::new()
            .first_pipe_instance(first)
            .reject_remote_clients(true)
            .create_with_security_attributes_raw(
                pipe_name(sid),
                (&mut security.attrs as *mut windows_sys::Win32::Security::SECURITY_ATTRIBUTES)
                    .cast(),
            )
    }?)
}
pub async fn read_frame<R: tokio::io::AsyncRead + Unpin>(
    reader: &mut R,
    limit: usize,
) -> Result<Value> {
    let length = reader.read_u32_le().await? as usize;
    ensure!(length <= limit, "IPC 消息超出限制");
    let mut bytes = vec![0; length];
    reader.read_exact(&mut bytes).await?;
    Ok(serde_json::from_slice(&bytes)?)
}
pub async fn write_frame<W: tokio::io::AsyncWrite + Unpin>(
    writer: &mut W,
    value: &Value,
) -> Result<()> {
    let data = serde_json::to_vec(value)?;
    writer.write_u32_le(data.len() as u32).await?;
    writer.write_all(&data).await?;
    Ok(())
}
pub async fn request(sid: &str, command: &str) -> Result<(Value, platform::Handle)> {
    let mut client = ClientOptions::new().open(pipe_name(sid))?;
    let mut pid = 0;
    platform::check(unsafe {
        windows_sys::Win32::System::Pipes::GetNamedPipeServerProcessId(
            client.as_raw_handle(),
            &mut pid,
        )
    })?;
    let process = platform::check_pid_user(pid, sid)?;
    let value = tokio::time::timeout(Duration::from_millis(750), async {
        write_frame(
            &mut client,
            &json!({"version":1,"request_id":1,"command":command}),
        )
        .await?;
        read_frame(&mut client, 64 * 1024).await
    })
    .await
    .context("控制通道响应超时")??;
    ensure!(
        value["pid"] == pid && value["service"] == "codex-model-monitor" && value["protocol"] == 1,
        "控制通道身份或协议不匹配"
    );
    Ok((value, process))
}
pub async fn serve(
    mut listener: NamedPipeServer,
    sid: String,
    status: Status,
    stop: watch::Sender<bool>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<()> {
    let slots = Arc::new(tokio::sync::Semaphore::new(16));
    loop {
        tokio::select! {r=listener.connect()=>r?,_=shutdown.changed()=>break}
        let next = create(&sid, false)?;
        let mut client = std::mem::replace(&mut listener, next);
        let Ok(permit) = slots.clone().try_acquire_owned() else {
            drop(client);
            continue;
        };
        let status = status.clone();
        let stop = stop.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let _ = tokio::time::timeout(Duration::from_secs(1), async {
                let req = read_frame(&mut client, 4096).await?;
                let stopping = req["version"] == 1 && req["command"] == "stop";
                let mut value = status.lock().unwrap().clone();
                value["request_id"] = req["request_id"].clone();
                if stopping {
                    value["stage"] = json!("stopping");
                }
                value["ok"] = json!(
                    req["version"] == 1
                        && matches!(req["command"].as_str(), Some("stop" | "status"))
                );
                write_frame(&mut client, &value).await?;
                if stopping {
                    let _ = stop.send(true);
                }
                Ok::<_, anyhow::Error>(())
            })
            .await;
        });
    }
    Ok(())
}
