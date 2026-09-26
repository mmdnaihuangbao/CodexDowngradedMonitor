use crate::{control, monitor::Monitor, storage::Query};
use anyhow::Result;
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Query as HttpQuery, State},
    http::{StatusCode, header},
    response::{
        IntoResponse, Response,
        sse::{Event, KeepAlive, Sse},
    },
    routing::{get, post},
};
use serde_json::{Value, json};
use std::{
    convert::Infallible,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::{broadcast, watch};
use tokio_stream::{StreamExt, wrappers::ReceiverStream};

#[derive(Clone)]
pub struct App {
    pub monitor: Arc<Mutex<Monitor>>,
    pub root: PathBuf,
    pub stop: watch::Sender<bool>,
    pub status: control::Status,
    pub broker: broadcast::Sender<Value>,
}
type ApiResult = std::result::Result<Json<Value>, (StatusCode, Json<Value>)>;
fn error(code: StatusCode, e: impl std::fmt::Display) -> (StatusCode, Json<Value>) {
    (code, Json(json!({"ok":false,"reason":e.to_string()})))
}
pub async fn access<T: Send + 'static>(
    app: &App,
    f: impl FnOnce(&mut Monitor) -> Result<T> + Send + 'static,
) -> Result<T> {
    let m = app.monitor.clone();
    tokio::task::spawn_blocking(move || {
        let mut locked = m.lock().map_err(|_| anyhow::anyhow!("监控状态锁异常"))?;
        f(&mut locked)
    })
    .await?
}
async fn snapshot(State(app): State<App>) -> ApiResult {
    access(&app, |m| m.snapshot())
        .await
        .map(Json)
        .map_err(|e| error(StatusCode::INTERNAL_SERVER_ERROR, e))
}
async fn responses(State(app): State<App>, HttpQuery(q): HttpQuery<Query>) -> ApiResult {
    access(&app, move |m| m.query(&q))
        .await
        .map(Json)
        .map_err(|e| error(StatusCode::BAD_REQUEST, e))
}
async fn diagnose(State(app): State<App>) -> ApiResult {
    access(&app, |m| m.diagnose())
        .await
        .map(Json)
        .map_err(|e| error(StatusCode::INTERNAL_SERVER_ERROR, e))
}
async fn health(State(app): State<App>) -> Json<Value> {
    let mut s = app.status.lock().unwrap().clone();
    s["ok"] = json!(true);
    s["backend"] = json!("rust");
    Json(s)
}
async fn config(State(app): State<App>, Json(body): Json<Value>) -> ApiResult {
    if !body.is_object() {
        return Err(error(StatusCode::BAD_REQUEST, "配置必须是 JSON 对象"));
    }
    let root = app.root.clone();
    access(&app, move |m| {
        let mut updates = json!({});
        for k in ["expect", "workers"] {
            if let Some(v) = body.get(k) {
                updates[k] = v.clone();
            }
        }
        if let Some(v) = body.get("min_interval_ms") {
            updates["min_interval_ms"] = v.clone();
        } else if let Some(v) = body.get("idle").or_else(|| body.get("interval")) {
            updates["min_interval_ms"] =
                json!(crate::config::number(v).ok_or_else(|| anyhow::anyhow!("间隔无效"))? * 1000.);
        }
        m.config = crate::config::save_updates(&root, &m.config, &updates)?;
        m.log("info", "配置已更新");
        let stats = m.stats()?;
        let _ = m.broker.send(json!({"type":"patch","stats":stats}));
        Ok(json!({"ok":true,"stats":stats}))
    })
    .await
    .map(Json)
    .map_err(|e| {
        let status = if e
            .chain()
            .any(|cause| cause.is::<std::io::Error>() || cause.is::<rusqlite::Error>())
        {
            StatusCode::INTERNAL_SERVER_ERROR
        } else {
            StatusCode::BAD_REQUEST
        };
        error(status, e)
    })
}
async fn clear(State(app): State<App>) -> ApiResult {
    access(&app, |m| {
        m.logs.clear();
        let _ = m.broker.send(json!({"type":"patch","logs_reset":true}));
        Ok(json!({"ok":true,"history_preserved":true}))
    })
    .await
    .map(Json)
    .map_err(|e| error(StatusCode::INTERNAL_SERVER_ERROR, e))
}
async fn shutdown(State(app): State<App>) -> Json<Value> {
    let _ = app.stop.send(true);
    Json(json!({"ok":true}))
}
async fn index(State(app): State<App>) -> Response {
    static_file(app, "index.html".into()).await
}
async fn asset(State(app): State<App>, Path(path): Path<String>) -> Response {
    static_file(app, path).await
}
async fn static_file(app: App, mut path: String) -> Response {
    if path == "favicon.ico" {
        path = "icon.png".into();
    }
    if !std::path::Path::new(&path)
        .components()
        .all(|p| matches!(p, std::path::Component::Normal(_)))
    {
        return StatusCode::NOT_FOUND.into_response();
    }
    let web = app.root.join("Web");
    let file = web.join(&path);
    let Some(valid) = file
        .canonicalize()
        .ok()
        .zip(web.canonicalize().ok())
        .filter(|(p, root)| p.starts_with(root))
    else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let body = tokio::task::spawn_blocking(move || std::fs::read(valid.0)).await;
    match body {
        Ok(Ok(body)) => {
            let mime = match std::path::Path::new(&path)
                .extension()
                .and_then(|x| x.to_str())
            {
                Some("html") => "text/html; charset=utf-8",
                Some("js") => "application/javascript; charset=utf-8",
                Some("css") => "text/css; charset=utf-8",
                Some("png") => "image/png",
                Some("svg") => "image/svg+xml",
                _ => "application/octet-stream",
            };
            (
                [
                    (header::CONTENT_TYPE, mime),
                    (header::CACHE_CONTROL, "no-store"),
                ],
                body,
            )
                .into_response()
        }
        _ => StatusCode::NOT_FOUND.into_response(),
    }
}
async fn stream(State(app): State<App>) -> Response {
    let mut subscription = app.broker.subscribe();
    let mut stop = app.stop.subscribe();
    let (tx, rx) = tokio::sync::mpsc::channel::<Value>(16);
    tokio::spawn(async move {
        let initial = tokio::select! {_=stop.changed()=>return,r=access(&app,|m|m.snapshot())=>r};
        let Ok(initial) = initial else {
            return;
        };
        if tx
            .send(json!({"type":"snapshot","data":initial}))
            .await
            .is_err()
        {
            return;
        }
        let mut interval = tokio::time::interval(Duration::from_secs(2));
        interval.tick().await;
        loop {
            let event = tokio::select! {
                _=stop.changed()=>break,
                _=tx.closed()=>break,
                received=subscription.recv()=>match received{Ok(v)=>v,Err(broadcast::error::RecvError::Lagged(_))=>match access(&app,|m|m.snapshot()).await{Ok(s)=>json!({"type":"snapshot","data":s}),Err(_)=>break},Err(_)=>break},
                _=interval.tick()=>match access(&app,|m|m.stats()).await{Ok(s)=>json!({"type":"patch","stats":s}),Err(_)=>break}
            };
            tokio::select! {_=stop.changed()=>break,_=tx.closed()=>break,r=tx.send(event)=>if r.is_err(){break;}}
        }
    });
    Sse::new(
        ReceiverStream::new(rx).map(|v| Ok::<_, Infallible>(Event::default().data(v.to_string()))),
    )
    .keep_alive(
        KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("ping"),
    )
    .into_response()
}
pub fn router(app: App) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/api/health", get(health))
        .route("/api/snapshot", get(snapshot))
        .route("/api/responses", get(responses))
        .route("/api/diagnose", get(diagnose))
        .route("/api/stream", get(stream))
        .route("/api/config", post(config))
        .route("/api/clear", post(clear))
        .route("/api/shutdown", post(shutdown))
        .route("/{*path}", get(asset))
        .layer(DefaultBodyLimit::max(64 * 1024))
        .with_state(app)
}
