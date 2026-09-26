//! Read-only scan pool. Workers own bounded buffers; enumeration is cached for 500 ms.
use crate::{extract, platform::Handle};
use anyhow::{Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::WAIT_TIMEOUT,
    System::{Diagnostics::Debug::ReadProcessMemory, Memory::*, Threading::*},
};
#[derive(Clone)]
struct Task {
    address: usize,
    size: usize,
    owned: usize,
    available: usize,
}
struct Work {
    tasks: Arc<Vec<Task>>,
    next: Arc<AtomicUsize>,
}
#[derive(Default)]
struct Part {
    batch: extract::Batch,
    bytes: u64,
    owned: u64,
    tasks: u64,
    hits: u64,
    errors: u64,
    supplemental: u64,
    read: f64,
    parse: f64,
    prefilter: f64,
}
pub struct Scanner {
    process: Arc<Handle>,
    senders: Vec<mpsc::Sender<Work>>,
    results: mpsc::Receiver<Part>,
    threads: Vec<std::thread::JoinHandle<()>>,
    cached: Arc<Vec<Task>>,
    enumerated: Instant,
    regions: usize,
    region_cost: f64,
}
fn read(process: &Handle, address: usize, buffer: &mut [u8]) -> (usize, bool) {
    let mut got = 0;
    let ok = unsafe {
        ReadProcessMemory(
            process.0,
            address as *const _,
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut got,
        )
    };
    (got.min(buffer.len()), ok != 0)
}
impl Scanner {
    pub fn open(pid: u32, workers: usize) -> Result<Self> {
        let process = Arc::new(Handle::new(unsafe {
            OpenProcess(
                PROCESS_VM_READ | PROCESS_QUERY_INFORMATION | PROCESS_SYNCHRONIZE,
                0,
                pid,
            )
        })?);
        let (result_tx, results) = mpsc::channel();
        let mut senders = vec![];
        let mut threads = vec![];
        for i in 0..workers.clamp(1, 16) {
            let (tx, rx) = mpsc::channel::<Work>();
            senders.push(tx);
            let output = result_tx.clone();
            let process = process.clone();
            threads.push(
                std::thread::Builder::new()
                    .name(format!("scan-{i}"))
                    .spawn(move || {
                        let mut buffer = vec![0; 2 * 1024 * 1024 + extract::WINDOW];
                        while let Ok(work) = rx.recv() {
                            let mut part = Part::default();
                            loop {
                                let n = work.next.fetch_add(1, Ordering::Relaxed);
                                let Some(task) = work.tasks.get(n) else {
                                    break;
                                };
                                part.tasks += 1;
                                part.owned += task.owned as u64;
                                let t = Instant::now();
                                let (got, ok) =
                                    read(&process, task.address, &mut buffer[..task.size]);
                                part.read += t.elapsed().as_secs_f64();
                                part.bytes += got as u64;
                                if !ok {
                                    part.errors += 1;
                                }
                                for block in (0..task.owned.min(got)).step_by(1024 * 1024) {
                                    let owned = (1024 * 1024).min(task.owned - block);
                                    let size = (owned + extract::WINDOW).min(got - block);
                                    let bytes = &buffer[block..block + size];
                                    let t = Instant::now();
                                    let candidate = extract::candidate(bytes);
                                    part.prefilter += t.elapsed().as_secs_f64();
                                    if !candidate {
                                        continue;
                                    }
                                    part.hits += 1;
                                    let t = Instant::now();
                                    let mut batch = extract::extract(bytes, owned);
                                    part.parse += t.elapsed().as_secs_f64();
                                    for offset in std::mem::take(&mut batch.retries) {
                                        let absolute = block + offset;
                                        let length =
                                            extract::MAX_WINDOW.min(task.available - absolute);
                                        if length <= got - absolute {
                                            continue;
                                        }
                                        let mut more = vec![0; length];
                                        let t = Instant::now();
                                        let (extra, ok) =
                                            read(&process, task.address + absolute, &mut more);
                                        part.read += t.elapsed().as_secs_f64();
                                        part.bytes += extra as u64;
                                        part.supplemental += 1;
                                        if !ok {
                                            part.errors += 1;
                                        }
                                        let t = Instant::now();
                                        let retry = extract::extract(&more[..extra], 1);
                                        part.parse += t.elapsed().as_secs_f64();
                                        batch.responses.extend(retry.responses);
                                        batch.requests.extend(retry.requests);
                                        batch.candidates += retry.candidates;
                                        batch.truncated += retry.truncated;
                                    }
                                    part.batch.responses.extend(batch.responses);
                                    part.batch.requests.extend(batch.requests);
                                    part.batch.candidates += batch.candidates;
                                    part.batch.truncated += batch.truncated;
                                }
                            }
                            if output.send(part).is_err() {
                                break;
                            }
                        }
                    })?,
            );
        }
        Ok(Self {
            process,
            senders,
            results,
            threads,
            cached: Arc::new(vec![]),
            enumerated: Instant::now() - Duration::from_secs(1),
            regions: 0,
            region_cost: 0.,
        })
    }
    pub fn alive(&self) -> bool {
        unsafe { WaitForSingleObject(self.process.0, 0) == WAIT_TIMEOUT }
    }
    fn enumerate(&mut self) {
        if !self.cached.is_empty() && self.enumerated.elapsed() < Duration::from_millis(500) {
            return;
        }
        let t = Instant::now();
        let mut tasks = vec![];
        let mut address = 0usize;
        self.regions = 0;
        unsafe {
            let mut info: MEMORY_BASIC_INFORMATION = std::mem::zeroed();
            while VirtualQueryEx(
                self.process.0,
                address as *const _,
                &mut info,
                std::mem::size_of_val(&info),
            ) == std::mem::size_of_val(&info)
            {
                let base = info.BaseAddress as usize;
                let size = info.RegionSize;
                let Some(end) = base.checked_add(size) else {
                    break;
                };
                if end <= address {
                    break;
                }
                if info.State == MEM_COMMIT
                    && (info.Type == MEM_PRIVATE || info.Type == MEM_MAPPED)
                    && info.Protect & (PAGE_GUARD | PAGE_NOACCESS) == 0
                    && info.Protect
                        & (PAGE_READONLY
                            | PAGE_READWRITE
                            | PAGE_WRITECOPY
                            | PAGE_EXECUTE_READ
                            | PAGE_EXECUTE_READWRITE
                            | PAGE_EXECUTE_WRITECOPY)
                        != 0
                {
                    self.regions += 1;
                    for offset in (0..size).step_by(2 * 1024 * 1024) {
                        let owned = (2 * 1024 * 1024).min(size - offset);
                        tasks.push(Task {
                            address: base + offset,
                            size: (owned + extract::WINDOW).min(size - offset),
                            owned,
                            available: size - offset,
                        });
                    }
                }
                address = end;
            }
        }
        self.cached = Arc::new(tasks);
        self.region_cost = t.elapsed().as_secs_f64();
        self.enumerated = Instant::now();
    }
    pub fn sweep(&mut self) -> Result<Value> {
        if !self.alive() {
            return Ok(json!({"alive":false}));
        }
        let t = Instant::now();
        self.enumerate();
        let next = Arc::new(AtomicUsize::new(0));
        for sender in &self.senders {
            sender.send(Work {
                tasks: self.cached.clone(),
                next: next.clone(),
            })?;
        }
        let mut total = Part::default();
        let mut responses = vec![];
        let mut requests = vec![];
        let mut seen_res = BTreeSet::new();
        let mut seen_req = BTreeSet::new();
        let mut read_max: f64 = 0.;
        let mut parse_max: f64 = 0.;
        let mut filter_max: f64 = 0.;
        let mut worker_max: f64 = 0.;
        let mut merge_cost = 0.;
        for _ in &self.senders {
            let r = self.results.recv()?;
            let merge_start = Instant::now();
            total.bytes += r.bytes;
            total.owned += r.owned;
            total.tasks += r.tasks;
            total.hits += r.hits;
            total.errors += r.errors;
            total.supplemental += r.supplemental;
            total.read += r.read;
            total.parse += r.parse;
            total.prefilter += r.prefilter;
            read_max = read_max.max(r.read);
            parse_max = parse_max.max(r.parse);
            filter_max = filter_max.max(r.prefilter);
            worker_max = worker_max.max(r.read + r.parse + r.prefilter);
            total.batch.candidates += r.batch.candidates;
            total.batch.truncated += r.batch.truncated;
            for v in r.batch.responses {
                if seen_res.insert(v.to_string()) {
                    responses.push(v);
                }
            }
            for v in r.batch.requests {
                if seen_req.insert(v.to_string()) {
                    requests.push(v);
                }
            }
            merge_cost += merge_start.elapsed().as_secs_f64();
        }
        ensure!(
            responses.len() + requests.len() <= 200000,
            "扫描结果超出安全上限"
        );
        Ok(
            json!({"alive":true,"bytes":total.bytes,"owned_bytes":total.owned,"tasks":total.tasks,"regions":self.regions,"workers":self.senders.len(),"region_cost":self.region_cost,"scan_cost":t.elapsed().as_secs_f64(),"hit_blocks":total.hits,"read_errors":total.errors,"read_worker_seconds":total.read,"parse_worker_seconds":total.parse,"prefilter_worker_seconds":total.prefilter,"read_worker_max_seconds":read_max,"parse_worker_max_seconds":parse_max,"prefilter_worker_max_seconds":filter_max,"worker_max_seconds":worker_max,"merge_cost":merge_cost,"parse_candidates":total.batch.candidates,"incomplete_candidates":total.batch.truncated,"supplemental_reads":total.supplemental,"responses":responses,"requests":requests}),
        )
    }
}
impl Drop for Scanner {
    fn drop(&mut self) {
        self.senders.clear();
        for thread in self.threads.drain(..) {
            let _ = thread.join();
        }
    }
}
