use crate::{config, control, http, monitor::Monitor, platform};
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::watch;

#[derive(Default, Clone)]
pub struct Args {
    pub mode: String,
    pub no_open: bool,
    pub updates: Value,
    pub target: Option<u32>,
    pub no_evidence: bool,
}
impl Args {
    pub fn parse() -> Result<Self> {
        let mut args = Self {
            mode: "--start".into(),
            updates: json!({}),
            ..Default::default()
        };
        let mut iter = std::env::args().skip(1);
        while let Some(a) = iter.next() {
            match a.as_str() {
                "--start" | "--stop" | "--status" | "--serve" | "--foreground" | "--worker"
                | "--version" | "--help" => args.mode = a,
                "--no-open" => args.no_open = true,
                "--no-evidence" => args.no_evidence = true,
                "--pid" => args.target = Some(iter.next().context("--pid 缺少值")?.parse()?),
                "--host" | "--port" | "--expect" | "--workers" | "--min-interval-ms" | "--idle"
                | "--interval" => {
                    let value = iter.next().with_context(|| format!("{a} 缺少值"))?;
                    let key = match a.as_str() {
                        "--port" => "cpp_port",
                        "--idle" | "--interval" => "min_interval_ms",
                        _ => a.trim_start_matches("--"),
                    };
                    let key = if key == "min-interval-ms" {
                        "min_interval_ms"
                    } else {
                        key
                    };
                    args.updates[key] = if key == "host" || key == "expect" {
                        json!(value)
                    } else {
                        let mut number: f64 = value.parse()?;
                        if a == "--idle" || a == "--interval" {
                            number *= 1000.;
                        }
                        json!(number)
                    };
                }
                _ => bail!("未知参数：{a}"),
            }
        }
        Ok(args)
    }
}
pub async fn command(args: Args) -> Result<i32> {
    if args.mode == "--version" {
        println!("0.5.0");
        return Ok(0);
    }
    if args.mode == "--help" {
        println!(
            "Codex 降智雷达 0.5.0\n--start [--no-open]  --stop  --status  --foreground\n--host HOST --port PORT --expect MODEL --workers 1..16 --min-interval-ms 0..60000\n配置和数据只保存在 EXE 目录；升级复制 config.json 和 data/。"
        );
        return Ok(0);
    }
    if args.mode == "--serve" || args.mode == "--foreground" {
        return serve(args).await;
    }
    let sid = platform::user_sid()?;
    if let Ok((status, process)) = control::request(
        &sid,
        if args.mode == "--stop" {
            "stop"
        } else {
            "status"
        },
    )
    .await
    {
        if args.mode == "--stop" {
            println!("正在关闭采集器……");
            wait_exit(process, Duration::from_secs(10)).await?;
            println!("已关闭。");
            return Ok(0);
        }
        if args.mode == "--status" {
            println!("{status}");
            return Ok(0);
        }
        return wait_ready(&sid, args.no_open, None).await;
    }
    let Some(guard) = platform::Singleton::acquire(&sid)? else {
        // A concurrent new host may not have created its pipe yet. Give only this handshake a bounded grace period.
        for _ in 0..10 {
            tokio::time::sleep(Duration::from_millis(30)).await;
            if control::request(&sid, "status").await.is_ok() {
                if args.mode == "--stop" {
                    let (_, process) = control::request(&sid, "stop").await?;
                    wait_exit(process, Duration::from_secs(10)).await?;
                    println!("已关闭。");
                    return Ok(0);
                }
                if args.mode == "--status" {
                    let (status, _) = control::request(&sid, "status").await?;
                    println!("{status}");
                    return Ok(0);
                }
                return wait_ready(&sid, args.no_open, None).await;
            }
        }
        bail!(
            "用户级单例仍被占用，但控制管道不可用。可能是 v0.4 仍在运行，请先用旧版停止脚本关闭；不会另起采集器。"
        );
    };
    drop(guard);
    if args.mode == "--stop" {
        println!("采集器未运行。");
        return Ok(0);
    }
    if args.mode == "--status" {
        println!("{{\"running\":false}}");
        return Ok(1);
    }
    println!("正在启动采集器……");
    let mut child = vec!["--serve".to_owned(), "--no-open".to_owned()];
    for (k, v) in args.updates.as_object().unwrap() {
        let key = match k.as_str() {
            "cpp_port" => "port",
            "min_interval_ms" => "min-interval-ms",
            _ => k,
        };
        child.push(format!("--{key}"));
        child.push(
            v.as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| v.to_string()),
        );
    }
    if let Some(pid) = args.target {
        child.extend(["--pid".into(), pid.to_string()]);
    }
    if args.no_evidence {
        child.push("--no-evidence".into());
    }
    let proc = platform::spawn_host(&child)?;
    wait_ready(&sid, args.no_open, Some(proc)).await
}
async fn wait_ready(sid: &str, no_open: bool, child: Option<platform::Handle>) -> Result<i32> {
    let started = Instant::now();
    let mut prior = String::new();
    loop {
        if let Ok((status, _)) = control::request(sid, "status").await {
            let stage = crate::domain::text(&status, "stage");
            if stage == "ready" {
                let url = crate::domain::text(&status, "url");
                println!("就绪：{url}（{:.3} 秒）", started.elapsed().as_secs_f64());
                if !no_open {
                    platform::open_browser(url)?;
                }
                return Ok(0);
            }
            if stage == "failed" {
                bail!("启动失败：{}", status["error"]);
            }
            if stage != prior {
                println!("启动阶段：{stage}");
                prior = stage.to_owned();
            }
        }
        if let Some(ref proc) = child {
            let mut code = 0;
            platform::check(unsafe {
                windows_sys::Win32::System::Threading::GetExitCodeProcess(proc.0, &mut code)
            })?;
            if code != 259 && code != 5 {
                bail!("服务启动失败（{code}），请查看程序目录 data/logs/collector.log");
            }
        }
        ensure!(
            started.elapsed() < Duration::from_secs(10),
            "等待服务就绪超时，可运行 --status 查看阶段或 --stop 关闭；不会另起扫描器"
        );
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}
async fn wait_exit(process: platform::Handle, timeout: Duration) -> Result<()> {
    let started = Instant::now();
    loop {
        let code =
            unsafe { windows_sys::Win32::System::Threading::WaitForSingleObject(process.0, 0) };
        if code == windows_sys::Win32::Foundation::WAIT_OBJECT_0 {
            let mut exit = 0;
            platform::check(unsafe {
                windows_sys::Win32::System::Threading::GetExitCodeProcess(process.0, &mut exit)
            })?;
            ensure!(
                exit == 0,
                "服务已退出但关闭失败（退出码 {exit}），请查看 data/logs/collector.log"
            );
            return Ok(());
        }
        ensure!(
            started.elapsed() < timeout,
            "服务尚未退出；请查看 data/logs/collector.log，不会误报关闭成功"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}
fn trace(root: &std::path::Path, start: Instant, message: &str) {
    use std::io::Write;
    if let Ok(path) = config::local_path(root, "data/logs/collector.log")
        && let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
    {
        let _ = writeln!(
            f,
            "{} +{:.3}s {}",
            chrono::Local::now().to_rfc3339(),
            start.elapsed().as_secs_f64(),
            message
        );
    }
}
pub async fn serve(args: Args) -> Result<i32> {
    let start = Instant::now();
    let sid = platform::user_sid()?;
    let Some(_guard) = platform::Singleton::acquire(&sid)? else {
        return Ok(5);
    };
    let root = platform::executable_root()?;
    let (stop, mut stopped) = watch::channel(false);
    let (control_exit, control_done) = watch::channel(false);
    let status = Arc::new(Mutex::new(
        json!({"protocol":1,"service":"codex-model-monitor","pid":std::process::id(),"instance_id":format!("{}-{}",std::process::id(),crate::domain::now()),"version":"0.5.0","stage":"initializing","root":root,"data":root.join("data"),"backend":"rust"}),
    ));
    let control_task = tokio::spawn(control::serve(
        control::create(&sid, true)?,
        sid,
        status.clone(),
        stop.clone(),
        control_done,
    ));
    let outcome=async{
        config::prepare(&root)?;trace(&root,start,"control ready");
        let mut saved=config::load(&root)?;
        if !root.join("config.json").exists(){config::save(&root,&saved)?;}
        saved.as_object_mut().unwrap().extend(args.updates.as_object().unwrap().clone());let settings=config::validate(&saved)?;
        let tmp=std::ffi::CString::new(root.join("data/tmp").to_string_lossy().as_bytes())?;
        // Called before any SQLite connection/thread exists. All SQLite temp I/O stays portable.
        ensure!(unsafe{rusqlite::ffi::sqlite3_win32_set_directory8(2,tmp.as_ptr())}==0,"SQLite 临时目录初始化失败");
        ensure!(root.join("Web/index.html").is_file(),"缺少 Web/index.html");
        let(broker,_)=tokio::sync::broadcast::channel(2000);let init_root=root.clone();let init_broker=broker.clone();
        let settings_copy=settings.clone();let init=tokio::task::spawn_blocking(move||Monitor::open(&init_root,settings_copy,init_broker));
        let monitor=tokio::select!{r=init=>r??,_=stopped.changed()=>return Ok::<(),anyhow::Error>(())};trace(&root,start,"database ready");
        let monitor=Arc::new(Mutex::new(monitor));let host=crate::domain::text(&settings,"host");let port=settings["cpp_port"].as_u64().unwrap() as u16;
        let mut listener=None;let mut last=None;
        for p in port..=port.saturating_add(11){match tokio::net::TcpListener::bind((host,p)).await{Ok(l)=>{listener=Some(l);break;},Err(e)=>last=Some(e)}}
        let listener=listener.ok_or_else(||anyhow::anyhow!("HTTP 绑定失败：{:?}",last))?;let actual=listener.local_addr()?.port();
        let url=format!("http://{}:{actual}/",if host=="0.0.0.0"{"localhost"}else{host});
        {let mut s=status.lock().unwrap();s["url"]=json!(url);s["port"]=json!(actual);s["stage"]=json!("ready");}
        let app=http::App{monitor:monitor.clone(),root:root.clone(),stop:stop.clone(),status:status.clone(),broker};
        let mut http_stop=stop.subscribe();let http_task=tokio::spawn(async move{axum::serve(listener,http::router(app)).with_graceful_shutdown(async move{if !*http_stop.borrow(){let _=http_stop.changed().await;}}).await});
        trace(&root,start,"HTTP ready");
        let scan_monitor=monitor.clone();let scan_stop=stop.subscribe();let scan=std::thread::Builder::new().name("scanner-supervisor".into()).spawn(move||scan_loop(scan_monitor,scan_stop,args.target))?;
        let evidence=if !args.no_evidence{let m=monitor.clone();let s=stop.subscribe();Some(std::thread::Builder::new().name("evidence".into()).spawn(move||crate::evidence::run(m,s))?)}else{monitor.lock().unwrap().evidence_stats["enabled"]=json!(false);None};
        if !*stopped.borrow(){tokio::select!{r=stopped.changed()=>r?,r=tokio::signal::ctrl_c(),if args.mode=="--foreground"=>{r?;let _=stop.send(true);}}}
        status.lock().unwrap()["stage"]=json!("stopping");trace(&root,start,"stop accepted");
        tokio::task::spawn_blocking(move||scan.join().map_err(|_|anyhow::anyhow!("扫描线程异常退出"))).await???;
        if let Some(evidence)=evidence{tokio::task::spawn_blocking(move||evidence.join().map_err(|_|anyhow::anyhow!("证据线程异常退出"))).await???;}
        let _=tokio::time::timeout(Duration::from_secs(2),http_task).await;
        monitor.lock().unwrap().store.db.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)")?;
        trace(&root,start,"worker exited; database flushed; shutdown complete");Ok(())
    }.await;
    if let Err(ref e) = outcome {
        status.lock().unwrap()["stage"] = json!("failed");
        status.lock().unwrap()["error"] = json!(format!("{e:#}"));
        trace(&root, start, &format!("ERROR {e:#}"));
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
    let _ = stop.send(true);
    let _ = control_exit.send(true);
    let _ = control_task.await;
    outcome?;
    Ok(0)
}
fn scan_loop(
    monitor: Arc<Mutex<Monitor>>,
    stop: watch::Receiver<bool>,
    target: Option<u32>,
) -> Result<()> {
    let stopped = || *stop.borrow();
    let mut worker: Option<crate::worker::Worker> = None;
    let mut pending: Option<(Value, String)> = None;
    let mut backoff = Instant::now();
    while !stopped() {
        let start = Instant::now();
        let config = monitor.lock().unwrap().config.clone();
        let workers = config["workers"].as_u64().unwrap_or(4) as usize;
        if worker.as_ref().is_some_and(|w| w.workers != workers) {
            worker = None;
        }
        if let Some((ref batch, ref expect)) = pending {
            let saved = monitor.lock().unwrap().ingest(batch, expect);
            match saved {
                Ok(()) => pending = None,
                Err(e) => {
                    monitor
                        .lock()
                        .unwrap()
                        .log("error", format!("持久化失败，保留批次重试：{e}"));
                    interruptible_sleep(&stop, Duration::from_millis(300));
                    continue;
                }
            }
        }
        if worker.is_none() {
            if Instant::now() < backoff {
                interruptible_sleep(&stop, Duration::from_millis(100));
                continue;
            }
            let candidates = platform::enumerate().unwrap_or_default();
            let pid = target.or_else(|| platform::pick_pid(&candidates));
            monitor.lock().unwrap().candidates = candidates;
            if let Some(pid) = pid {
                match crate::worker::Worker::spawn(pid, workers) {
                    Ok(w) => {
                        let mut m = monitor.lock().unwrap();
                        m.pid = Some(pid);
                        m.status = "running".into();
                        m.log(
                            "info",
                            format!("已连接 pid={pid}，Rust worker={}，线程={workers}", w.pid),
                        );
                        worker = Some(w);
                    }
                    Err(e) => {
                        let mut m = monitor.lock().unwrap();
                        m.status = "denied".into();
                        m.log("error", format!("采集器启动失败：{e}"));
                        backoff = Instant::now() + Duration::from_secs(15);
                        continue;
                    }
                }
            } else {
                let mut m = monitor.lock().unwrap();
                m.status = "no_process".into();
                m.pid = None;
                backoff = Instant::now() + Duration::from_secs(2);
                continue;
            }
        }
        match worker.as_mut().unwrap().sweep(&stopped) {
            Ok(batch) if batch["alive"] == true => {
                pending = Some((batch, crate::domain::text(&config, "expect").into()))
            }
            Ok(_) => {
                worker = None;
                backoff = Instant::now() + Duration::from_secs(2);
            }
            Err(e) => {
                if !stopped() {
                    monitor
                        .lock()
                        .unwrap()
                        .log("error", format!("扫描失败：{e}"));
                }
                worker = None;
                backoff = Instant::now() + Duration::from_secs(2);
            }
        }
        let delay = Duration::from_secs_f64(
            crate::config::number(&config["min_interval_ms"]).unwrap_or(0.) / 1000.,
        );
        interruptible_sleep(&stop, delay.saturating_sub(start.elapsed()));
    }
    drop(worker);
    if let Some((batch, expect)) = pending {
        monitor
            .lock()
            .unwrap()
            .ingest(&batch, &expect)
            .context("关闭时批次写入失败")?;
    }
    monitor.lock().unwrap().status = "stopped".into();
    Ok(())
}
pub fn interruptible_sleep(stop: &watch::Receiver<bool>, duration: Duration) {
    let start = Instant::now();
    while !*stop.borrow() && start.elapsed() < duration {
        std::thread::sleep(Duration::from_millis(20).min(duration.saturating_sub(start.elapsed())));
    }
}
