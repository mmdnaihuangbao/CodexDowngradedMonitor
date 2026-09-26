//! Small audited Win32 ownership boundary. Observed processes are opened read-only.
use anyhow::{Context, Result, ensure};
use std::{mem::size_of, os::windows::ffi::OsStrExt, ptr};
use windows_sys::Win32::{
    Foundation::*, Security::Authorization::*, Security::*, System::Threading::*,
};
pub fn wide(s: impl AsRef<std::ffi::OsStr>) -> Vec<u16> {
    s.as_ref().encode_wide().chain(Some(0)).collect()
}
pub fn check(ok: i32) -> Result<()> {
    if ok == 0 {
        Err(std::io::Error::last_os_error().into())
    } else {
        Ok(())
    }
}
pub struct Handle(pub HANDLE);
// SAFETY: kernel handles may be used/closed on any thread. Mutex ownership is separately !Send.
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
impl Handle {
    pub fn new(h: HANDLE) -> Result<Self> {
        ensure!(
            !h.is_null() && h != INVALID_HANDLE_VALUE,
            "Win32: {}",
            std::io::Error::last_os_error()
        );
        Ok(Self(h))
    }
}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
pub fn process_sid(process: HANDLE) -> Result<String> {
    // SAFETY: buffers are aligned, sized from GetTokenInformation, and kept alive for conversion.
    unsafe {
        let mut h = ptr::null_mut();
        check(OpenProcessToken(process, TOKEN_QUERY, &mut h))?;
        let token = Handle::new(h)?;
        let mut size = 0;
        GetTokenInformation(token.0, TokenUser, ptr::null_mut(), 0, &mut size);
        let mut buffer = vec![0usize; (size as usize).div_ceil(size_of::<usize>())];
        check(GetTokenInformation(
            token.0,
            TokenUser,
            buffer.as_mut_ptr().cast(),
            size,
            &mut size,
        ))?;
        let info = &*buffer.as_ptr().cast::<TOKEN_USER>();
        let mut text = ptr::null_mut();
        check(ConvertSidToStringSidW(info.User.Sid, &mut text))?;
        let mut n = 0;
        while *text.add(n) != 0 {
            n += 1;
        }
        let sid = String::from_utf16_lossy(std::slice::from_raw_parts(text, n));
        LocalFree(text.cast());
        Ok(sid)
    }
}
pub fn user_sid() -> Result<String> {
    process_sid(unsafe { GetCurrentProcess() })
}
pub fn check_pid_user(pid: u32, sid: &str) -> Result<Handle> {
    let process = Handle::new(unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            0,
            pid,
        )
    })?;
    ensure!(process_sid(process.0)? == sid, "控制通道的进程用户不匹配");
    Ok(process)
}
pub struct Security {
    descriptor: PSECURITY_DESCRIPTOR,
    pub attrs: SECURITY_ATTRIBUTES,
}
impl Security {
    pub fn user(sid: &str) -> Result<Self> {
        unsafe {
            // Medium integrity label permits the same user's unelevated launcher to control an elevated host.
            let sddl = wide(format!("D:P(A;;GA;;;{sid})(A;;GA;;;SY)S:(ML;;NW;;;ME)"));
            let mut descriptor = ptr::null_mut();
            check(ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                1,
                &mut descriptor,
                ptr::null_mut(),
            ))?;
            Ok(Self {
                descriptor,
                attrs: SECURITY_ATTRIBUTES {
                    nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
                    lpSecurityDescriptor: descriptor,
                    bInheritHandle: 0,
                },
            })
        }
    }
}
impl Drop for Security {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.descriptor);
        }
    }
}
pub struct Singleton {
    handle: Handle,
    _thread: std::marker::PhantomData<std::rc::Rc<()>>,
}
impl Singleton {
    pub fn acquire(sid: &str) -> Result<Option<Self>> {
        unsafe {
            let sec = Security::user(sid)?;
            let name = wide(format!("Global\\CodexDowngradedMonitor-{sid}"));
            let h = Handle::new(CreateMutexW(&sec.attrs, 0, name.as_ptr()))?;
            match WaitForSingleObject(h.0, 0) {
                WAIT_OBJECT_0 | WAIT_ABANDONED => Ok(Some(Self {
                    handle: h,
                    _thread: Default::default(),
                })),
                WAIT_TIMEOUT => Ok(None),
                _ => Err(std::io::Error::last_os_error().into()),
            }
        }
    }
}
impl Drop for Singleton {
    fn drop(&mut self) {
        unsafe {
            ReleaseMutex(self.handle.0);
        }
    }
}
pub fn open_browser(url: &str) -> Result<()> {
    use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};
    let action = wide("open");
    let url = wide(url);
    let r = unsafe {
        ShellExecuteW(
            ptr::null_mut(),
            action.as_ptr(),
            url.as_ptr(),
            ptr::null(),
            ptr::null(),
            SW_SHOWNORMAL,
        )
    };
    ensure!(r as usize > 32, "无法打开浏览器");
    Ok(())
}
pub fn enumerate() -> Result<Vec<serde_json::Value>> {
    use windows_sys::Win32::System::{Diagnostics::ToolHelp::*, ProcessStatus::*};
    unsafe {
        let snapshot = Handle::new(CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0))?;
        let mut item: PROCESSENTRY32W = std::mem::zeroed();
        item.dwSize = size_of::<PROCESSENTRY32W>() as u32;
        let mut ok = Process32FirstW(snapshot.0, &mut item);
        let mut out = vec![];
        while ok != 0 {
            let name = String::from_utf16_lossy(
                &item.szExeFile[..item.szExeFile.iter().position(|v| *v == 0).unwrap_or(260)],
            );
            if name.eq_ignore_ascii_case("codex.exe") {
                let mut path = String::new();
                let mut mb = 0.;
                if let Ok(h) = Handle::new(OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION,
                    0,
                    item.th32ProcessID,
                )) {
                    let mut buffer = vec![0u16; 32768];
                    let mut n = buffer.len() as u32;
                    if QueryFullProcessImageNameW(h.0, 0, buffer.as_mut_ptr(), &mut n) != 0 {
                        path = String::from_utf16_lossy(&buffer[..n as usize]);
                    }
                    let mut counters: PROCESS_MEMORY_COUNTERS = std::mem::zeroed();
                    counters.cb = size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
                    if GetProcessMemoryInfo(
                        h.0,
                        &mut counters,
                        size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
                    ) != 0
                    {
                        mb = counters.WorkingSetSize as f64 / 1048576.;
                    }
                }
                out.push(serde_json::json!({"pid":item.th32ProcessID,"parent_pid":item.th32ParentProcessID,"name":name,"path":path,"cmd":"","mb":mb}));
            }
            ok = Process32NextW(snapshot.0, &mut item);
        }
        Ok(out)
    }
}
pub fn pick_pid(candidates: &[serde_json::Value]) -> Option<u32> {
    candidates
        .iter()
        .max_by(|a, b| {
            let engine = |v: &serde_json::Value| crate::domain::text(v, "name") == "codex.exe";
            engine(a).cmp(&engine(b)).then_with(|| {
                a["mb"]
                    .as_f64()
                    .unwrap_or(0.)
                    .total_cmp(&b["mb"].as_f64().unwrap_or(0.))
            })
        })
        .and_then(|v| v["pid"].as_u64())
        .map(|n| n as u32)
}
pub fn parent_pid() -> Result<u32> {
    use windows_sys::Win32::System::Diagnostics::ToolHelp::*;
    unsafe {
        let snapshot = Handle::new(CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0))?;
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = size_of::<PROCESSENTRY32W>() as u32;
        let mut ok = Process32FirstW(snapshot.0, &mut entry);
        while ok != 0 {
            if entry.th32ProcessID == std::process::id() {
                return Ok(entry.th32ParentProcessID);
            }
            ok = Process32NextW(snapshot.0, &mut entry);
        }
        anyhow::bail!("无法确定 worker 父进程")
    }
}
pub fn executable_root() -> Result<std::path::PathBuf> {
    Ok(std::env::current_exe()?
        .parent()
        .context("程序目录无效")?
        .canonicalize()?)
}

fn quote_arg(value: &str) -> String {
    let mut out = String::from("\"");
    let mut slashes = 0;
    for c in value.chars() {
        if c == '\\' {
            slashes += 1;
            continue;
        }
        if c == '"' {
            out.push_str(&"\\".repeat(slashes * 2 + 1));
        } else {
            out.push_str(&"\\".repeat(slashes));
        }
        slashes = 0;
        out.push(c);
    }
    out.push_str(&"\\".repeat(slashes * 2));
    out.push('"');
    out
}
/// A background host must not inherit ANY launcher handles. Inheriting a redirected
/// stdout handle keeps its reader waiting for EOF until the service exits.
pub fn spawn_host(arguments: &[String]) -> Result<Handle> {
    unsafe {
        let exe = std::env::current_exe()?;
        let app = wide(&exe);
        let mut parts = vec![quote_arg(&exe.to_string_lossy())];
        parts.extend(arguments.iter().map(|a| quote_arg(a)));
        let mut command = wide(parts.join(" "));
        let mut startup: STARTUPINFOW = std::mem::zeroed();
        startup.cb = size_of::<STARTUPINFOW>() as u32;
        let mut info: PROCESS_INFORMATION = std::mem::zeroed();
        check(CreateProcessW(
            app.as_ptr(),
            command.as_mut_ptr(),
            ptr::null(),
            ptr::null(),
            0,
            CREATE_NO_WINDOW,
            ptr::null(),
            ptr::null(),
            &startup,
            &mut info,
        ))?;
        let process = Handle::new(info.hProcess)?;
        drop(Handle::new(info.hThread)?);
        Ok(process)
    }
}
