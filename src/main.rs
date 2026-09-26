#![cfg_attr(not(test), windows_subsystem = "windows")]
mod app;
mod config;
mod control;
mod domain;
mod evidence;
mod extract;
mod http;
mod monitor;
mod platform;
mod scanner;
mod storage;
#[cfg(test)]
mod tests;
mod worker;

fn main() {
    let result = (|| -> anyhow::Result<i32> {
        let args = app::Args::parse()?;
        if args.mode == "--worker" {
            worker::run()?;
            return Ok(0);
        }
        if args.mode != "--serve" {
            unsafe {
                windows_sys::Win32::System::Console::AttachConsole(
                    windows_sys::Win32::System::Console::ATTACH_PARENT_PROCESS,
                );
            }
        }
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()?;
        runtime.block_on(app::command(args))
    })();
    match result {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            eprintln!("{e:#}");
            std::process::exit(2);
        }
    }
}
