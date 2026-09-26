//! Same-image worker launched suspended with an explicit inherited-handle list and a kill-on-close job.
use crate::platform::{Handle, check, wide};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    fs::File,
    io::{Read, Write},
    mem::size_of,
    os::windows::io::FromRawHandle,
    ptr,
    sync::mpsc,
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::*,
    Security::SECURITY_ATTRIBUTES,
    System::{JobObjects::*, Pipes::*, Threading::*},
};
pub struct Worker {
    job: Handle,
    process: Handle,
    input: File,
    output: mpsc::Receiver<Result<Value>>,
    reader: Option<std::thread::JoinHandle<()>>,
    pub workers: usize,
    pub pid: u32,
}
fn read_sync(reader: &mut impl Read) -> Result<Value> {
    let mut header = [0u8; 4];
    reader.read_exact(&mut header)?;
    let n = u32::from_le_bytes(header) as usize;
    ensure!(n <= 64 * 1024 * 1024, "worker 消息超出限制");
    let mut data = vec![0; n];
    reader.read_exact(&mut data)?;
    Ok(serde_json::from_slice(&data)?)
}
fn write_sync(writer: &mut impl Write, v: &Value) -> Result<()> {
    let started = Instant::now();
    let mut bytes = serde_json::to_vec(v)?;
    if v.get("responses").is_some_and(Value::is_array) {
        let elapsed = started.elapsed().as_secs_f64();
        // Append the encoding measurement to the trusted sweep object without encoding it twice.
        ensure!(bytes.pop() == Some(b'}'), "扫描结果必须是 JSON 对象");
        write!(&mut bytes, ",\"serialize_cost\":{elapsed}}}")?;
    }
    ensure!(bytes.len() <= 64 * 1024 * 1024, "worker 消息超出限制");
    writer.write_all(&(bytes.len() as u32).to_le_bytes())?;
    writer.write_all(&bytes)?;
    writer.flush()?;
    Ok(())
}
fn pipe() -> Result<(Handle, Handle)> {
    unsafe {
        let mut a = ptr::null_mut();
        let mut b = ptr::null_mut();
        let sa = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: ptr::null_mut(),
            bInheritHandle: 1,
        };
        check(CreatePipe(&mut a, &mut b, &sa, 65536))?;
        Ok((Handle::new(a)?, Handle::new(b)?))
    }
}
impl Worker {
    pub fn spawn(pid: u32, workers: usize) -> Result<Self> {
        unsafe {
            let job = Handle::new(CreateJobObjectW(ptr::null(), ptr::null()))?;
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            check(SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of_val(&limits) as u32,
            ))?;
            let (child_in, parent_in) = pipe()?;
            let (parent_out, child_out) = pipe()?;
            check(SetHandleInformation(parent_in.0, HANDLE_FLAG_INHERIT, 0))?;
            check(SetHandleInformation(parent_out.0, HANDLE_FLAG_INHERIT, 0))?;
            let handles = [child_in.0, child_out.0];
            let mut bytes = 0;
            InitializeProcThreadAttributeList(ptr::null_mut(), 1, 0, &mut bytes);
            let mut attrs = vec![0usize; bytes.div_ceil(size_of::<usize>())];
            let list = attrs.as_mut_ptr().cast();
            check(InitializeProcThreadAttributeList(list, 1, 0, &mut bytes))?;
            struct Attributes(LPPROC_THREAD_ATTRIBUTE_LIST);
            impl Drop for Attributes {
                fn drop(&mut self) {
                    unsafe {
                        DeleteProcThreadAttributeList(self.0);
                    }
                }
            }
            let _attrs = Attributes(list);
            check(UpdateProcThreadAttribute(
                list,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_ptr().cast(),
                size_of_val(&handles),
                ptr::null_mut(),
                ptr::null(),
            ))?;
            let mut startup: STARTUPINFOEXW = std::mem::zeroed();
            startup.StartupInfo.cb = size_of_val(&startup) as u32;
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
            startup.StartupInfo.hStdInput = child_in.0;
            startup.StartupInfo.hStdOutput = child_out.0;
            startup.StartupInfo.hStdError = child_out.0;
            startup.lpAttributeList = list;
            let executable = std::env::current_exe()?;
            let app = wide(&executable);
            let mut command = wide(format!("\"{}\" --worker", executable.display()));
            let mut info: PROCESS_INFORMATION = std::mem::zeroed();
            check(CreateProcessW(
                app.as_ptr(),
                command.as_mut_ptr(),
                ptr::null(),
                ptr::null(),
                1,
                CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
                ptr::null(),
                ptr::null(),
                &startup.StartupInfo,
                &mut info,
            ))?;
            let process = Handle::new(info.hProcess)?;
            let thread = Handle::new(info.hThread)?;
            if let Err(e) = check(AssignProcessToJobObject(job.0, process.0)) {
                TerminateProcess(process.0, 1);
                WaitForSingleObject(process.0, 3000);
                return Err(e).context("worker Job Object 绑定失败");
            }
            if ResumeThread(thread.0) == u32::MAX {
                TerminateProcess(process.0, 1);
                return Err(std::io::Error::last_os_error().into());
            }
            drop(child_in);
            drop(child_out);
            let input = File::from_raw_handle(parent_in.0);
            std::mem::forget(parent_in);
            let mut output = File::from_raw_handle(parent_out.0);
            std::mem::forget(parent_out);
            let (tx, rx) = mpsc::channel();
            let reader = std::thread::Builder::new()
                .name("worker-ipc".into())
                .spawn(move || {
                    loop {
                        let value = read_sync(&mut output);
                        let failed = value.is_err();
                        if tx.send(value).is_err() || failed {
                            break;
                        }
                    }
                })?;
            let mut worker = Self {
                job,
                process,
                input,
                output: rx,
                reader: Some(reader),
                workers,
                pid: info.dwProcessId,
            };
            write_sync(
                &mut worker.input,
                &json!({"protocol":1,"parent":std::process::id(),"pid":pid,"workers":workers}),
            )?;
            let ready = worker.receive(&|| false, Duration::from_secs(5))?;
            ensure!(ready["ready"] == true, "worker 启动失败: {ready}");
            Ok(worker)
        }
    }
    fn receive(&self, stopped: &impl Fn() -> bool, timeout: Duration) -> Result<Value> {
        let deadline = Instant::now() + timeout;
        loop {
            ensure!(!stopped(), "采集已取消");
            ensure!(Instant::now() < deadline, "worker 响应超时");
            match self.output.recv_timeout(Duration::from_millis(30)) {
                Ok(v) => return v,
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(e) => return Err(e.into()),
            }
        }
    }
    pub fn sweep(&mut self, stopped: &impl Fn() -> bool) -> Result<Value> {
        write_sync(&mut self.input, &json!({"command":"scan"}))?;
        self.receive(stopped, Duration::from_secs(30))
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        unsafe {
            TerminateJobObject(self.job.0, 0);
            WaitForSingleObject(self.process.0, 3000);
        }
        if let Some(thread) = self.reader.take() {
            let _ = thread.join();
        }
    }
}
pub fn run() -> Result<()> {
    let actual_parent = crate::platform::parent_pid()?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let (status, _) = runtime.block_on(crate::control::request(
        &crate::platform::user_sid()?,
        "status",
    ))?;
    ensure!(
        status["pid"] == actual_parent,
        "worker 只能由当前用户的主服务创建"
    );
    drop(runtime);
    // Unauthenticated manual --worker has no inherited pipe and must never scan.
    unsafe {
        ensure!(
            windows_sys::Win32::Storage::FileSystem::GetFileType(GetStdHandle(STD_INPUT_HANDLE))
                == windows_sys::Win32::Storage::FileSystem::FILE_TYPE_PIPE,
            "worker 需要主服务私有管道"
        );
    }
    let mut input = std::io::stdin().lock();
    let mut output = std::io::stdout().lock();
    let hello = read_sync(&mut input)?;
    ensure!(hello["protocol"] == 1, "worker 协议不匹配");
    let parent = hello["parent"].as_u64().context("缺少主进程")? as u32;
    ensure!(actual_parent == parent, "worker 父进程握手不匹配");
    let _owner = crate::platform::check_pid_user(parent, &crate::platform::user_sid()?)?;
    let mut in_job = 0;
    unsafe {
        check(IsProcessInJob(
            GetCurrentProcess(),
            ptr::null_mut(),
            &mut in_job,
        ))?;
    }
    ensure!(in_job != 0, "worker 未受 Job Object 管理");
    let pid = hello["pid"].as_u64().context("缺少目标 PID")? as u32;
    let workers = hello["workers"].as_u64().context("缺少线程数")? as usize;
    let mut scanner = match crate::scanner::Scanner::open(pid, workers) {
        Ok(s) => s,
        Err(e) => {
            write_sync(&mut output, &json!({"ready":false,"reason":e.to_string()}))?;
            return Err(e);
        }
    };
    write_sync(&mut output, &json!({"ready":true}))?;
    while let Ok(request) = read_sync(&mut input) {
        match request["command"].as_str() {
            Some("scan") => write_sync(&mut output, &scanner.sweep()?)?,
            Some("stop") => break,
            _ => write_sync(&mut output, &json!({"alive":scanner.alive()}))?,
        }
    }
    Ok(())
}
use windows_sys::Win32::System::Console::{GetStdHandle, STD_INPUT_HANDLE};
