#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Codex 模型降级监控 · 后端采集器 + 本地 HTTP 服务
================================================

原理完全继承 codex_model_watch2.py
----------------------------------
Codex (codex.exe) 收到服务端 WebSocket 帧后会把响应对象反序列化到堆内存，
该对象含服务端权威字段 `model`：

    {"id":"resp_...","object":"response","created_at":...,"status":"...",
     ...,"max_tool_calls":null,"model":"gpt-6-astra","moderation":null,...,
     "safety_identifier":"user-...","reasoning":{"effort":"xhigh"}}

判据链：
  1. 只读扫描（VirtualQueryEx + ReadProcessMemory），不注入、不提权
  2. 只认「服务端下发」的对象（命中 >=2 个服务端独占字段，且不含客户端请求特征）
  3. 降级 = 请求模型 ≠ 响应模型；配对依据 响应.prev == 请求.previous_response_id
  4. luna / low 是正常子任务（标题生成、命令审查），不报警

四级分类：
  normal      请求模型 == 响应模型 == 预期模型                  → 绿
  subtask     请求模型 == 响应模型 != 预期模型（luna/low）      → 黄，不报警
  downgrade   请求模型 ≠ 响应模型                              → 红，报警
  incomplete  **没抓到请求模型**，无法断言降级                   → 黄，不报警
              （响应模型 != 预期模型 且 effort != low 时，证据不足，
                单列一类；绝不因为「采集缺口」而误报成降级）

与命令行版的差别
----------------
* **不向控制台输出任何内容**（stdout/stderr 一律丢弃），日志走 HTTP 接口给前端
* **采集与 HTTP 服务完全解耦**：
    - 采集线程常驻，进程起来就一直连续扫描，跟有没有人开网页无关
    - HTTP 层只做两件事：托管前端静态页 + 读状态库（snapshot / SSE）
    - HTTP 层不发起扫描；连「诊断」也只是回放采集线程最近一轮的观测
* 支持多句柄并行扫描（`--workers`）+ 连续扫描（`--idle 0`），提高采样频率，
  减少「响应对象存在窗口太短而采空」

HTTP 接口
---------
    GET  /                 前端页面
    GET  /style.css        样式
    GET  /app.js           脚本
    GET  /api/snapshot     全量状态快照（JSON）—— 读状态库
    GET  /api/stream       SSE 增量推送：snapshot 首帧 + patch 后续
    GET  /api/diagnose     只读回放最近一轮扫描观测（不触发扫描）
    POST /api/config       修改 {expect, idle}
    POST /api/clear        清空已捕获数据
    POST /api/shutdown     停止服务

用法
----
    python collector.py                     # 默认 127.0.0.1:48766，自动打开浏览器
    python collector.py --port 9000
    python collector.py --workers 6         # 并行扫描线程数
    python collector.py --idle 0.1          # 轮间歇 0.1s（默认 0 = 连续扫）
    python collector.py --expect gpt-6-astra
    python collector.py --no-open           # 不自动开浏览器
    python collector.py --log run.log
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from history_store import HistoryStore, DEFAULT_DB, validation_reason

# ==================================================================
# 0. 静默化 —— 本进程绝不向控制台写任何东西
# ==================================================================

def go_silent():
    """把 stdout/stderr 接到空设备，并吞掉线程未捕获异常的回溯。

    这样用 pythonw 启动时不会有控制台窗口，用 python 启动时也不会有任何输出。

    注意：只在 __main__ 里调用，不在 import 时调用 ——
    否则作为模块被引用（例如跑自检脚本）时会连带把调用方的输出也吞掉。
    """
    try:
        null = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        return
    try:
        sys.stdout = null
        sys.stderr = null
    except Exception:
        pass

    try:
        threading.excepthook = lambda args: None
    except Exception:
        pass
    try:
        sys.excepthook = lambda *a, **k: None
    except Exception:
        pass


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def fmt_epoch(v):
    """把 unix 秒转成本地时间字符串；非法值返回 None"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


# ==================================================================
# 1. 内存读取（与原脚本逐字一致）
# ==================================================================

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
MAX_REGION = 256 << 20


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


class ProcessScanner:
    """对目标进程做只读内存扫描"""

    # PAGE_READONLY / READWRITE / WRITECOPY / EXECUTE_READ / EXECUTE_READWRITE / EXECUTE_WRITECOPY
    READABLE_PROTECT = 0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80
    MEM_PRIVATE = 0x10000
    MEM_MAPPED = 0x20000
    SCAN_TYPES = MEM_PRIVATE | MEM_MAPPED
    CHUNK = 1 << 20

    def __init__(self, pid):
        self.pid = pid
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        self.k32.OpenProcess.restype = wt.HANDLE
        self.k32.CloseHandle.argtypes = [wt.HANDLE]
        self.k32.CloseHandle.restype = wt.BOOL
        self.k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        self.k32.GetExitCodeProcess.restype = wt.BOOL
        self.k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                            ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
        self.k32.VirtualQueryEx.restype = ctypes.c_size_t
        self.k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.k32.ReadProcessMemory.restype = wt.BOOL
        self.buf = ctypes.create_string_buffer(self.CHUNK)
        self.h = None

    def open(self):
        self.h = self.k32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, self.pid)
        return bool(self.h)

    def close(self):
        if self.h:
            try:
                self.k32.CloseHandle(self.h)
            except Exception:
                pass
            self.h = None

    def is_alive(self):
        """只查询退出码；不对缺少 SYNCHRONIZE 权限的句柄执行等待。"""
        if not self.h:
            return False
        try:
            code = wt.DWORD()
            return bool(self.k32.GetExitCodeProcess(self.h, ctypes.byref(code))) and code.value == 259
        except Exception:
            return False

    def iter_readable(self):
        addr = 0
        while addr < 0x7FFFFFFFFFFF:
            mbi = MEMORY_BASIC_INFORMATION()
            if self.k32.VirtualQueryEx(
                    self.h, ctypes.c_void_p(addr),
                    ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if size == 0:
                break
            if (mbi.State == MEM_COMMIT
                    and (mbi.Type & self.SCAN_TYPES)
                    and (mbi.Protect & self.READABLE_PROTECT)
                    and not (mbi.Protect & PAGE_NOACCESS)
                    and not (mbi.Protect & PAGE_GUARD)
                    and size <= MAX_REGION):
                yield base, size
            addr = base + size

    def read(self, addr, size):
        size = min(size, self.CHUNK)
        rd = ctypes.c_size_t(0)
        ok = self.k32.ReadProcessMemory(
            self.h, ctypes.c_void_p(addr), self.buf, size, ctypes.byref(rd))
        if ok and rd.value:
            return self.buf.raw[:rd.value]
        return b""

    def sweep(self, regions, consumer):
        """扫描指定区域列表，把「疑似含响应对象」的块立刻交给 consumer。

        与旧版 scan_hits 的差别：不再把命中块攒进 list 再返回，
        而是边扫边消费 —— 1MB/块的命中数据不会在内存里堆积。
        返回本轮实际读取的字节数。
        """
        nbytes = 0
        for base, size in regions:
            off = 0
            while off < size:
                n = min(self.CHUNK, size - off)
                data = self.read(base + off, n)
                if data:
                    nbytes += len(data)
                    # 预筛：同时出现 "resp_ 和 "model" 才送出去解析
                    if b'"resp_' in data and b'"model"' in data:
                        consumer(data)
                    off += len(data)
                else:
                    off += n
        return nbytes


def _balance_regions(regions, k):
    """把区域按字节量贪心均衡地切成 k 份（LPT 装箱，大块优先）"""
    slices = [[] for _ in range(k)]
    loads = [0] * k
    for base, size in sorted(regions, key=lambda r: r[1], reverse=True):
        i = loads.index(min(loads))
        slices[i].append((base, size))
        loads[i] += size
    return [s for s in slices if s]


class ParallelScanner:
    """多句柄并行内存扫描器。

    并行是安全的、也是有效的：
      * ReadProcessMemory 走内核态拷贝，ctypes 调用期间释放 GIL，真并行
      * 同一个进程句柄可以被多个线程并发 ReadProcessMemory / VirtualQueryEx

    因此起 N 个工作线程 + N 个独立句柄，把可读区域按字节量均分后同时扫，
    单轮扫描耗时近似降到 1/N，采样频率随之提高，减少「对象存在窗口太短而采空」。
    """

    REGION_TTL = 0.5      # 可读区域列表的缓存时长（秒）
    QUEUE_MAX = 32        # 命中块的流式队列上限，限制峰值内存

    def __init__(self, pid, workers=4):
        self.pid = pid
        self.workers = max(1, int(workers))
        self.pool = [ProcessScanner(pid) for _ in range(self.workers)]
        self._regions = []
        self._regions_at = 0.0
        # 指标
        self.last_bytes = 0
        self.last_regions = 0
        self.last_workers = 0
        self.region_cost = 0.0

    def open(self):
        opened = []
        for sc in self.pool:
            if not sc.open():
                for o in opened:
                    o.close()
                return False
            opened.append(sc)
        return True

    def close(self):
        for sc in self.pool:
            sc.close()

    def is_alive(self):
        return self.pool[0].is_alive() if self.pool else False

    def _refresh_regions(self):
        t0 = time.time()
        try:
            self._regions = list(self.pool[0].iter_readable())
        except Exception:
            self._regions = []
        self._regions_at = time.time()
        self.region_cost = self._regions_at - t0

    def sweep(self, consumer):
        """一轮并行扫描；consumer(chunk_bytes) 在主线程被调用"""
        now = time.time()
        if not self._regions or now - self._regions_at > self.REGION_TTL:
            self._refresh_regions()

        regions = self._regions
        self.last_regions = len(regions)
        if not regions:
            self.last_bytes = 0
            self.last_workers = 0
            return 0

        slices = _balance_regions(regions, self.workers)
        self.last_workers = len(slices)

        q = queue.Queue(maxsize=self.QUEUE_MAX)
        pending = [len(slices)]
        lock = threading.Lock()
        totals = [0] * len(slices)

        def worker(i, sc, regs):
            got = 0
            try:
                got = sc.sweep(regs, q.put)
            except Exception:
                pass
            totals[i] = got
            with lock:
                pending[0] -= 1
                if pending[0] == 0:
                    q.put(None)        # 结束哨兵

        threads = []
        for i, regs in enumerate(slices):
            t = threading.Thread(target=worker, args=(i, self.pool[i], regs),
                                 name=f"sweep-{i}", daemon=True)
            t.start()
            threads.append(t)

        try:
            while True:
                item = q.get()
                if item is None:
                    break
                consumer(item)
        except BaseException:
            # 主线程消费出错时，别把工作线程堵死在 q.put 上
            t0 = time.time()
            while pending[0] > 0 and time.time() - t0 < 5:
                try:
                    if q.get(timeout=0.3) is None:
                        break
                except queue.Empty:
                    continue
            raise
        finally:
            for t in threads:
                t.join(timeout=2.0)

        self.last_bytes = sum(totals)
        return self.last_bytes


# ==================================================================
# 2. 字段抽取（与原脚本逐字一致）
# ==================================================================

RE_OBJ_START = re.compile(rb'\{"id"\s*:\s*"resp_')
RE_FIELDS = {
    "response_id": re.compile(rb'"id"\s*:\s*"(resp_[0-9a-zA-Z_\-]+)"'),
    # 注意：原始脚本这里是 rb'"model"\s*:\s*"(gpt-[0-9a-zA-Z._\-]+)"'，
    # 硬编码了 gpt- 前缀 —— 只要响应模型不是 gpt- 开头（例如 luna-mini），
    # 整个响应对象就会被当作"缺 model"丢弃，直接漏报。
    # 请求侧用的是宽松的 ([^"]+)，两侧不一致本身就是 bug，这里统一放宽。
    # 放宽不会引入误报：对象仍须以 {"id":"resp_ 开头且命中 >=2 个服务端独占字段。
    "model":       re.compile(rb'"model"\s*:\s*"([^"\\]+)"'),
    "effort":      re.compile(rb'"effort"\s*:\s*"([a-z_]+)"'),
    "status":      re.compile(rb'"status"\s*:\s*"(in_progress|completed|failed|incomplete|cancelled|queued)"'),
    "prev":        re.compile(rb'"previous_response_id"\s*:\s*"(resp_[0-9a-zA-Z_\-]+)"'),
    "created_at":  re.compile(rb'"created_at"\s*:\s*(\d+)'),
    "completed_at": re.compile(rb'"completed_at"\s*:\s*(\d+)'),
    "safety_id":   re.compile(rb'"safety_identifier"\s*:\s*"([^"]+)"'),
    "text_format": re.compile(rb'"format"\s*:\s*\{\s*"type"\s*:\s*"([a-z_]+)"'),
}

# 服务端帧独占字段（用于确认 model 来源可信）
SERVER_MARKERS = [b'"safety_identifier"', b'"frequency_penalty"',
                  b'"presence_penalty"', b'"object":"response"',
                  b'"completed_at"', b'"max_output_tokens"']
SERVER_MIN_MARKERS = 2

# 客户端请求特征（出现即排除）
CLIENT_MARKERS = [b'"type":"response.create"', b'"client_metadata"']

# 客户端请求侧
RE_REQ_ANCHOR = re.compile(rb'"type"\s*:\s*"response\.create"')
RE_REQ_MODEL = re.compile(rb'"model"\s*:\s*"([^"]+)"')
RE_REQ_PREV = re.compile(rb'"previous_response_id"\s*:\s*"(resp_[0-9a-zA-Z_\-]+)"')

SUBTASK_EFFORT = "low"

STATUS_RANK = {"queued": 1, "in_progress": 2, "failed": 3, "incomplete": 3,
               "cancelled": 3, "completed": 4}

VERDICT_TAG = {"normal": "正常", "subtask": "子任务",
               "downgrade": "降级", "incomplete": "不完整"}


def extract_requests(chunk):
    """抽出客户端请求侧映射 {previous_response_id: 请求模型}"""
    out = {}
    for m in RE_REQ_ANCHOR.finditer(chunk):
        blk = chunk[m.start():m.start() + 512]
        mm = RE_REQ_MODEL.search(blk)
        if not mm:
            continue
        pp = RE_REQ_PREV.search(blk)
        key = pp.group(1).decode() if pp else None
        out[key] = mm.group(1).decode("utf-8", "replace")
    return out


def extract_responses(chunk):
    """从内存块里抽出所有「服务端下发」的 response 对象关键字段"""
    out = []
    for m in RE_OBJ_START.finditer(chunk):
        blk = chunk[m.start():m.start() + 4096]
        n_marks = sum(1 for mk in SERVER_MARKERS if mk in blk)
        if n_marks < SERVER_MIN_MARKERS:
            continue
        if any(mk in blk for mk in CLIENT_MARKERS):
            continue
        f = {}
        for key, rx in RE_FIELDS.items():
            mm = rx.search(blk)
            if mm:
                f[key] = mm.group(1).decode("utf-8", "replace")
        if f.get("response_id") and f.get("model"):
            f["_marks"] = n_marks
            out.append(f)
    return out


# ==================================================================
# 3. 进程枚举
# ==================================================================

def enumerate_codex():
    """Toolhelp32 / QueryFullProcessImageName：不启动 PowerShell 子进程。"""
    from process_discovery import enumerate_codex as discover
    return discover()


def pick_pid(cands):
    """优先 app-server 模式，其次内存占用最大的那个"""
    if not cands:
        return None
    for c in cands:
        if "app-server" in (c.get("cmd") or ""):
            return c["pid"]
    engines = [c for c in cands if c.get("name") == "codex.exe"]
    return max(engines or cands, key=lambda c: c.get("mb") or 0)["pid"]


# ==================================================================
# 4. 订阅广播（SSE）
# ==================================================================

class Broker:
    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, msg):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def count(self):
        with self._lock:
            return len(self._subs)


# ==================================================================
# 5. 监控主体
# ==================================================================

MAX_RESPONSES = 3000      # 上限，超出淘汰最旧的
MAX_ALERTS = 200
PROBE_BACKOFF = 2.0       # 没找到进程时，多久才重新枚举一次
DENIED_BACKOFF = 15.0     # 打开进程失败时，多久才重试
NO_PROCESS_WAIT = 0.5     # 没有可扫进程时，采集线程每轮歇多久


class Monitor:
    def __init__(self, expect, interval, logfile=None, workers=4, backend="python", db_path=":memory:"):
        self.lock = threading.RLock()
        self.broker = Broker()
        self.expect = expect
        self.idle = max(0.0, float(interval))   # 两轮扫描之间的空闲秒数（0 = 连续扫描）
        self.workers = max(1, int(workers))
        self.backend = backend
        self.logfile = logfile
        self.store = HistoryStore(db_path)
        self._pending_archive = None

        self.scanner = None
        self.seen = {}
        self.req_models = {}
        self.rid_kind = {}
        self.count = {"normal": 0, "subtask": 0,
                      "downgrade": 0, "incomplete": 0}

        self.rounds = 0
        self.scan_cost = 0.0
        self.scan_bytes = 0
        self.region_cost = 0.0
        self.active_workers = 0
        self.regions = 0
        self.last_scan = None
        self.last_sweep = None       # 最近一轮的原始观测（供诊断只读回放）
        self._round_ts = deque(maxlen=40)
        self.status = "starting"     # starting | running | no_process | denied | stopped
        self.pid = None
        self.candidates = []

        self.alerts = []
        self.logs = deque(maxlen=400)
        self.started_at = time.time()
        self._alert_seq = 0
        self._wait_log_at = 0.0
        self._probe_after = 0.0      # 下次允许枚举进程的最早时刻（退避）
        self._last_pub_at = 0.0      # 上次心跳广播时刻
        self._last_pub_status = None
        self._stop = threading.Event()
        self._scan_lock = threading.Lock()
        self.seen = {r["response_id"]: r for r in self.store.recent(MAX_RESPONSES)}
        self.rid_kind = {rid: r.get("_verdict", "incomplete") for rid, r in self.seen.items()}
        self.req_models = self.store.request_models()
        self.count = dict(self.store.totals()["counts"])
        self.logs.extend(self.store.recent_logs())

    # ---------------- 日志（不落控制台，只落内存 + 文件） ----------------
    def log(self, level, msg):
        line = {"ts": now_str(), "level": level, "msg": msg}
        with self.lock:
            self.logs.append(line)
        if self.logfile:
            try:
                with open(self.logfile, "a", encoding="utf-8") as fh:
                    fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                             f"{level.upper():<5} {msg}\n")
            except Exception:
                pass
        try:
            self.store.log(line, self.backend)
        except Exception as exc:
            # A storage failure must not also kill the collector's error-reporting path.
            line["msg"] += f"（日志写库失败：{exc}）"
        self.broker.publish({"type": "patch", "logs": [line]})

    # ---------------- 分类（判据与原脚本一致，另加「不完整」一级） ----------------
    #
    #   配对成功（拿到了请求模型）—— 这才是硬证据：
    #     normal     请求模型 == 响应模型 == 预期模型
    #     subtask    请求模型 == 响应模型 != 预期模型（luna/low 等子任务）
    #     downgrade  请求模型 != 响应模型          → 真降级，标红 + 告警
    #
    #   配对失败（没抓到请求模型）—— 证据不足，不下降级结论：
    #     normal     响应模型 == 预期模型（本来就是对的，无所谓配对）
    #     subtask    effort == low（启发式：主对话不用 low）
    #     incomplete 其余情况 → **不完整**，标黄、不报警
    #                （原先这里直接判 downgrade，只要请求侧没采到就报红，
    #                  属于把「采集缺口」误报成「模型降级」）
    def classify(self, rec):
        model = rec.get("model")
        effort = rec.get("effort")
        prev = rec.get("prev")
        if prev and prev not in self.req_models:
            found, model_evidence = self.store.lookup_request(prev)
            if found: self.req_models[prev] = model_evidence
        req_model = self.req_models.get(prev) if prev else None

        if req_model is not None:
            if req_model != model:
                return "downgrade", req_model
            if req_model == self.expect:
                return "normal", req_model
            return "subtask", req_model

        # 配对失败：回退启发式
        if model == self.expect:
            return "normal", None
        if effort == SUBTASK_EFFORT:
            return "subtask", None
        return "incomplete", None

    def pairing_status(self, rec):
        prev = rec.get("prev")
        if not prev:
            return "no_previous_response_id"
        if prev not in self.req_models:
            return "request_not_captured"
        if self.req_models[prev] is None:
            return "ambiguous_previous_response_id"
        return "matched_previous_response_id"

    # ---------------- 去重 ----------------
    def handle(self, rec):
        rid = rec["response_id"]
        now = time.time()
        old = self.seen.get(rid)
        if old is None:
            old = self.store.get(rid)
            if old is not None:
                self.seen[rid] = old
                self.rid_kind[rid] = old.get("_verdict", "incomplete")

        if old is None:
            rec["_process_pid"] = self.pid
            rec["_first_seen"] = now
            rec["_last_seen"] = now
            rec["_updates"] = 0
            self.seen[rid] = rec
            self._evict()
            return "new", rec

        new_rank = STATUS_RANK.get(rec.get("status", ""), 0)
        old_rank = STATUS_RANK.get(old.get("status", ""), 0)
        gained = [k for k in ("completed_at", "safety_id", "created_at", "prev")
                  if not old.get(k) and rec.get(k)]

        if new_rank > old_rank or gained:
            merged = dict(old)
            merged.update(rec)
            merged["_first_seen"] = old.get("_first_seen", now)
            merged["_last_seen"] = now
            merged["_updates"] = old.get("_updates", 0) + 1
            self.seen[rid] = merged
            return "update", merged

        return None, old

    def _evict(self):
        if len(self.seen) <= MAX_RESPONSES:
            return
        order = sorted(self.seen.items(), key=lambda kv: kv[1].get("_first_seen", 0))
        for rid, _ in order[:len(self.seen) - MAX_RESPONSES]:
            self.seen.pop(rid, None)
            self.rid_kind.pop(rid, None)

    # ---------------- 判定 + 计数 + 告警 ----------------
    def apply_verdict(self, rec, kind):
        rid = rec["response_id"]
        verdict, req_model = self.classify(rec)
        rec["_suspect_reason"] = validation_reason(rec)
        rec["_verdict"] = verdict
        rec["_req_model"] = req_model
        rec["_pairing_status"] = self.pairing_status(rec)

        prev_verdict = self.rid_kind.get(rid)
        if kind == "new":
            self.count[verdict] += 1
            self.rid_kind[rid] = verdict
        elif prev_verdict != verdict:
            if prev_verdict is not None:
                self.count[prev_verdict] = max(0, self.count[prev_verdict] - 1)
            self.count[verdict] += 1
            self.rid_kind[rid] = verdict

        new_alert = None
        if verdict != "downgrade" or rec.get("_suspect_reason"):
            # New contradictory evidence invalidates the earlier alert in snapshots.
            self.alerts[:] = [a for a in self.alerts if a["rid"] != rid]
        if verdict == "downgrade" and prev_verdict != "downgrade" and not rec.get("_suspect_reason"):
            self._alert_seq += 1
            new_alert = {
                "seq": self._alert_seq,
                "rid": rid,
                "model": rec.get("model"),
                "req_model": req_model,
                "expect": self.expect,
                "effort": rec.get("effort"),
                "status": rec.get("status"),
                "prev": rec.get("prev"),
                "text_format": rec.get("text_format"),
                "safety_id": rec.get("safety_id"),
                "paired": bool(req_model),
                "ts": now_str(),
                "wall": time.time(),
            }
            self.alerts.insert(0, new_alert)
            del self.alerts[MAX_ALERTS:]
        return new_alert

    # ---------------- 序列化 ----------------
    @staticmethod
    def ser(rec):
        created = rec.get("created_at")
        completed = rec.get("completed_at")
        try:
            duration = int(completed) - int(created)
            if int(created) <= 0 or duration < 0:
                duration = None
        except (TypeError, ValueError, OverflowError):
            duration = None
        return {
            "rid": rec.get("response_id"),
            "model": rec.get("model"),
            "req_model": rec.get("_req_model"),
            "pairing_status": rec.get("_pairing_status"),
            "suspect_reason": rec.get("_suspect_reason"),
            "verdict": rec.get("_verdict", "normal"),
            "effort": rec.get("effort"),
            "status": rec.get("status"),
            "prev": rec.get("prev"),
            "text_format": rec.get("text_format"),
            "safety_id": rec.get("safety_id"),
            "created_at": fmt_epoch(rec.get("created_at")),
            "completed_at": fmt_epoch(rec.get("completed_at")),
            "duration_seconds": duration,
            "marks": rec.get("_marks"),
            "first_seen": rec.get("_first_seen"),
            "last_seen": rec.get("_last_seen"),
            "updates": rec.get("_updates", 0),
        }

    def stats(self):
        totals = self.store.totals()
        return {
            "status": self.status,
            "pid": self.pid,
            "expect": self.expect,
            "idle": self.idle,
            "workers": self.workers,
            "active_workers": self.active_workers,
            "regions": self.regions,
            "region_cost": round(self.region_cost, 4),
            "hz": self.hz(),
            "rounds": self.rounds,
            "scan_cost": round(self.scan_cost, 4),
            "scan_bytes": self.scan_bytes,
            "last_scan": self.last_scan,
            **totals,
            "database": self.store.path,
            "request_keys": len(self.req_models),
            "ambiguous_pairings": sum(v is None for v in self.req_models.values()),
            "backend": self.backend,
            "clients": self.broker.count(),
            "up": round(time.time() - self.started_at, 1),
            "server_time": time.time(),
        }

    def snapshot(self):
        with self.lock:
            page = self.query_responses()
            return {
                "stats": self.stats(),
                **page,
                "alerts": list(self.alerts),
                "logs": list(self.logs),
                "candidates": list(self.candidates),
            }

    def query_responses(self, **kwargs):
        page = self.store.query(**kwargs)
        page["responses"] = [self.ser(r) for r in page["responses"]]
        return page

    def patch(self, upserts, new_alerts):
        return {
            "type": "patch",
            "upserts": upserts,
            "alerts": new_alerts,
            "stats": self.stats(),
        }

    # ---------------- 进程连接 ----------------
    def _ensure_process(self):
        if self.scanner is not None and self.scanner.is_alive():
            return True

        if self.scanner is not None:
            self.scanner.close()
            self.scanner = None
            self.pid = None
            self.status = "no_process"
            self._probe_after = 0.0
            self.log("warn", "codex.exe 已退出，等待重新出现…")

        # 退避：没有目标时降低 Windows API 枚举频率。
        now = time.time()
        if now < self._probe_after:
            return False
        self._probe_after = now + PROBE_BACKOFF

        cands = enumerate_codex()
        pid = pick_pid(cands)
        with self.lock:
            self.candidates = cands

        if pid is None:
            self.status = "no_process"
            if time.time() - self._wait_log_at > 30:
                self._wait_log_at = time.time()
                self.log("info", "未发现运行中的 codex.exe，持续等待中…")
            return False

        if self.backend == "cpp":
            from native_scanner import NativeScanner
            sc = NativeScanner(pid, self.workers)
        else:
            sc = ParallelScanner(pid, self.workers)
        if not sc.open():
            self.status = "denied"
            self._probe_after = now + DENIED_BACKOFF
            self.log("error", f"打开进程失败 pid={pid}（可能需要以管理员身份运行）")
            return False

        self.req_models = self.store.request_models()
        self.scanner = sc
        self.pid = pid
        self.status = "running"
        cmd = next((c["cmd"] for c in cands if c["pid"] == pid), "")
        self.log("info", f"已连接 codex.exe  pid={pid}  "
                         f"后端={self.backend} · 扫描线程={self.workers}  "
                         f"{'(app-server)' if 'app-server' in cmd else ''}")
        return True

    # ---------------- 单轮扫描 ----------------
    def tick(self):
        """做一轮内存扫描并把结果并入状态库。

        返回 True 表示真的扫了一轮；False 表示当前没有可扫的进程。
        注意：这是「采集」这一步，只由采集线程调用；
        HTTP 层永远不会调用它 —— 服务层只读状态库。
        """
        # Retry an uncommitted batch before reading more memory. Cached response state
        # has already advanced, so discarding this batch would silently lose history.
        if self._pending_archive is not None:
            self.store.save_batch(*self._pending_archive)
            self._pending_archive = None
        if not self._ensure_process():
            self._publish_heartbeat()
            return False

        t0 = time.time()
        resps, reqs = [], []
        native_metrics = {}
        hits = [0]

        def consume(data):
            hits[0] += 1
            resps.extend(extract_responses(data))
            reqs.extend({"prev": prev, "model": model} for prev, model in extract_requests(data).items())

        try:
            if self.backend == "cpp":
                batch = self.scanner.sweep_records()
                resps, reqs = batch["responses"], batch["requests"]
                hits[0] = batch["hit_blocks"]
                nbytes = batch["bytes"]
                native_metrics = dict(self.scanner.metrics)
            else:
                nbytes = self.scanner.sweep(consume)
        except Exception as e:
            self.log("error", f"扫描异常: {e}")
            try:
                self.scanner.close()
            except Exception:
                pass
            self.scanner = None
            return

        upserts, new_alerts, changed_records = [], [], []
        now = time.time()
        with self.lock:
            # A predecessor is not a unique request ID (forks/retries can reuse it).
            # Conflicting models remain ambiguous rather than last-writer-wins.
            evidence_changed = set()
            for request in reqs:
                prev, model = request.get("prev"), request.get("model")
                if not prev or not model:
                    continue
                if prev not in self.req_models:
                    found, stored_model = self.store.lookup_request(prev)
                    if found: self.req_models[prev] = stored_model
                if prev not in self.req_models or (self.req_models[prev] is not None and self.req_models[prev] != model):
                    evidence_changed.add(prev)
                if prev in self.req_models and self.req_models[prev] != model:
                    self.req_models[prev] = None
                else:
                    self.req_models[prev] = model
            while len(self.req_models) > MAX_RESPONSES * 2:
                self.req_models.pop(next(iter(self.req_models)))

            # 本轮原始观测留一份，供「诊断」只读回放（服务层不触发扫描）
            items = []
            seen_rids = set()
            for rec in resps:
                kind, r = self.handle(rec)
                if not kind:
                    continue
                alert = self.apply_verdict(r, kind)
                if alert:
                    new_alerts.append(alert)
                changed_records.append(dict(r))
                s = self.ser(r)
                upserts.append(s)
                if s["rid"] not in seen_rids and len(items) < 200:
                    seen_rids.add(s["rid"])
                    items.append({
                        "rid": s["rid"], "model": s["model"],
                        "req_model": s["req_model"], "effort": s["effort"],
                        "status": s["status"], "marks": s["marks"],
                    })

            # Late request evidence must update cards even if response status is unchanged.
            changed_rids = {row["rid"] for row in upserts}
            late_records = self.store.for_previous(evidence_changed)
            late_records.update({rid:r for rid,r in self.seen.items() if r.get("prev") in evidence_changed})
            for r in late_records.values():
                if r["response_id"] in changed_rids:
                    continue
                verdict, model = self.classify(r)
                pairing = self.pairing_status(r)
                if (verdict, model, pairing) != (r.get("_verdict"), r.get("_req_model"), r.get("_pairing_status")):
                    self.rid_kind.setdefault(r["response_id"], r.get("_verdict", "incomplete"))
                    alert = self.apply_verdict(r, "update")
                    if alert: new_alerts.append(alert)
                    changed_records.append(dict(r))
                    upserts.append(self.ser(r))

            self._pending_archive = (changed_records, reqs, self.backend)
            self.store.save_batch(*self._pending_archive)
            self._pending_archive = None
            self.count = dict(self.store.totals()["counts"])
            self._evict()
            self.last_sweep = {
                "ts": now_str(), "wall": now,
                "cost": round(now - t0, 4),
                "bytes": nbytes,
                "regions": self.scanner.last_regions,
                "workers": self.scanner.last_workers,
                "region_cost": round(self.scanner.region_cost, 4),
                "hit_blocks": hits[0],
                "raw_responses": len(resps),
                "raw_requests": len(reqs),
                "unkeyed_requests": sum(not r.get("prev") for r in reqs),
                "ambiguous_pairings": sum(v is None for v in self.req_models.values()),
                "backend": self.backend,
                "native": native_metrics,
                "upserts": len(upserts),
                "items": items,
            }

            self.rounds += 1
            self.scan_cost = now - t0
            self.scan_bytes = nbytes
            self.active_workers = self.scanner.last_workers
            self.regions = self.scanner.last_regions
            self.region_cost = self.scanner.region_cost
            self.last_scan = now
            self._round_ts.append(now)
            stats = self.stats()

        if upserts or new_alerts:
            self.broker.publish({"type": "patch", "upserts": upserts,
                                 "alerts": new_alerts, "stats": stats})
        return True

    def _publish_heartbeat(self):
        """没有可扫进程时，低频广播一次状态（状态变化时立即广播）。

        不能每轮都广播：没有 codex.exe 时循环会以每秒数百次的速度空转，
        那样会把 SSE 队列刷爆、也把 CPU 吃满。
        """
        now = time.time()
        with self.lock:
            changed = self.status != self._last_pub_status
            if not (changed or now - self._last_pub_at > 2.0):
                return
            self._last_pub_status = self.status
            self._last_pub_at = now
            stats = self.stats()
        self.broker.publish({"type": "patch", "upserts": [],
                             "alerts": [], "stats": stats})

    # ---------------- 采集线程（常驻，与 HTTP 服务解耦） ----------------
    def run(self):
        self.status = "starting"
        self.log("info", f"采集线程启动 · 预期模型={self.expect} · "
                         f"扫描线程={self.workers} · 轮间空闲={self.idle}s"
                         + ("（连续扫描）" if self.idle <= 0 else ""))
        while not self._stop.is_set():
            scanned = False
            try:
                with self._scan_lock:
                    scanned = self.tick()
            except Exception as e:
                self.log("error", f"采样循环异常: {e}")
            # 真扫过 → 按 idle 歇；没有可扫的进程 → 按 NO_PROCESS_WAIT 歇，
            # 否则 idle=0 时会变成 100% 占核的空转。
            wait = self.idle if scanned else NO_PROCESS_WAIT
            if wait > 0:
                self._stop.wait(wait)

    def stop(self):
        self._stop.set()
        with self._scan_lock:
            if self.scanner:
                self.scanner.close()
                self.scanner = None
        with self.lock:
            self.status = "stopped"

    def hz(self):
        """实测采样频率（最近 40 轮）；超过 3 秒没扫过就归零"""
        ts = self._round_ts
        if len(ts) < 2:
            return 0.0
        if time.time() - ts[-1] > 3.0:
            return 0.0
        span = ts[-1] - ts[0]
        return round((len(ts) - 1) / span, 2) if span > 0 else 0.0

    # ---------------- 诊断（纯只读，回放采集线程的观测） ----------------
    def diagnose(self):
        """排障视图：回放采集线程最近一轮的原始观测。

        刻意不在这里发起扫描 —— 服务层与采集器解耦，
        扫描永远只由采集线程驱动，避免两处同时读同一进程内存。
        """
        with self.lock:
            sweep = dict(self.last_sweep) if self.last_sweep else None
            out = {
                "ok": self.status == "running" and sweep is not None,
                "status": self.status,
                "pid": self.pid,
                "clients": self.broker.count(),
                "rounds": self.rounds,
                "hz": self.hz(),
                "candidates": list(self.candidates),
            }
            if sweep is None:
                out["reason"] = {
                    "no_process": "未找到运行中的 codex.exe",
                    "denied": "打开进程失败（可能需要管理员权限）",
                    "starting": "采集线程刚启动，还没完成第一轮扫描",
                    "stopped": "采集已停止",
                }.get(out["status"], "尚无扫描结果")
                return out
            out.update({
                "sweep": sweep,
                "blocks": sweep["hit_blocks"],
                "raw_responses": sweep["raw_responses"],
                "unique": len(sweep["items"]),
                "pairings": sweep["raw_requests"],
                "cost": sweep["cost"],
                "items": sweep["items"],
            })
            return out


# ==================================================================
# 6. HTTP 服务
# ==================================================================

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexWatch/1.0"
    protocol_version = "HTTP/1.1"
    monitor = None

    # 静默：不打印访问日志
    def log_message(self, fmt, *args):
        return

    # ---------------- 基础工具 ----------------
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _static(self, relpath):
        path = os.path.join(WEB_DIR, relpath)
        path = os.path.normpath(path)
        if not path.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(path):
            self._send(404, b"not found")
            return
        ext = os.path.splitext(path)[1].lower()
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except Exception:
            self._send(500, b"read error")
            return
        self._send(200, body, MIME.get(ext, "application/octet-stream"))

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------------- GET ----------------
    def do_GET(self):
        path = urlparse(self.path).path
        m = self.monitor

        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path == "/api/snapshot":
            self._json(m.snapshot())
        elif path == "/api/stream":
            self._sse()
        elif path == "/api/responses":
            try:
                query = parse_qs(urlparse(self.path).query)
                def arg(key, default=None): return query.get(key, [default])[0]
                data = m.query_responses(page=int(arg("page", "1")),
                    page_size=int(arg("page_size", "50")), filter=arg("filter", "all"),
                    q=arg("q", ""), start=float(arg("start")) if arg("start") else None,
                    end=float(arg("end")) if arg("end") else None)
                self._json(data)
            except (ValueError, OverflowError) as exc:
                self._json({"ok": False, "reason": str(exc)}, 400)
        elif path == "/api/diagnose":
            self._json(m.diagnose())
        elif path == "/api/health":
            self._json({"ok": True, "status": m.status, "backend": m.backend,
                        "service": "codex-model-monitor"})
        elif path == "/favicon.ico":
            self._send(204)
        else:
            self._static(path.lstrip("/"))

    # ---------------- POST ----------------
    def do_POST(self):
        path = urlparse(self.path).path
        m = self.monitor

        if path == "/api/config":
            body = self._body_json()
            with m.lock:
                if isinstance(body.get("expect"), str) and body["expect"].strip():
                    m.expect = body["expect"].strip()
                raw = body.get("idle", body.get("interval"))
                try:
                    iv = float(raw)
                    if 0.0 <= iv <= 60:
                        m.idle = iv
                except (TypeError, ValueError):
                    pass
                stats = m.stats()
            m.log("info", f"配置已更新 · 预期模型={m.expect} · 轮间空闲={m.idle}s")
            self._json({"ok": True, "stats": stats})

        elif path == "/api/clear":
            # Keep the legacy route safe: clear only the live log view, never history.
            with m.lock:
                m.logs.clear()
            m.broker.publish({"type": "patch", "logs_reset": True})
            self._json({"ok": True, "history_preserved": True})

        elif path == "/api/shutdown":
            self._json({"ok": True})
            m.log("info", "收到停止指令，服务即将退出")
            threading.Thread(target=self._shutdown, daemon=True).start()

        else:
            self._send(404, b"not found")

    def _shutdown(self):
        time.sleep(0.3)
        self.monitor.stop()
        try:
            self.server.shutdown()
        except Exception:
            pass

    # ---------------- SSE ----------------
    def _sse(self):
        m = self.monitor
        q = m.broker.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except Exception:
            m.broker.unsubscribe(q)
            return

        def frame(obj):
            data = json.dumps(obj, ensure_ascii=False)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        last_ping = time.time()
        try:
            frame({"type": "snapshot", "data": m.snapshot()})
            while not m._stop.is_set():
                try:
                    msg = q.get(timeout=1.0)
                    frame(msg)
                except queue.Empty:
                    pass
                if time.time() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            m.broker.unsubscribe(q)
            self.close_connection = True


# ==================================================================
# 7. 入口
# ==================================================================

class LocalHTTPServer(ThreadingHTTPServer):
    """本地专用 HTTP 服务。

    allow_reuse_address 必须关掉：Windows 上它等价于 SO_REUSEADDR，
    会让第二个实例"成功"绑到已被占用的端口上（而不是报错），
    端口顺延逻辑就永远不触发，还会出现请求落到随机实例的诡异现象。
    """
    allow_reuse_address = False
    daemon_threads = True


def serve(host, port, handler_cls, max_tries=12):
    last = None
    for p in range(port, port + max_tries):
        try:
            return LocalHTTPServer((host, p), handler_cls), p
        except OSError as e:
            last = e
    raise last


def main():
    ap = argparse.ArgumentParser(
        description="Codex 模型降级监控 · 常驻采集器 + 本地 HTTP 视图服务（静默，无控制台输出）")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite 历史库路径；两种后端默认共用 data/monitor.sqlite")
    ap.add_argument("--backend", choices=("python", "cpp"), default="python")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--expect", default="gpt-6-astra",
                    help="预期模型（默认 gpt-6-astra）")
    ap.add_argument("--workers", type=int, default=4,
                    help="并行扫描线程数（默认 4，每个线程一个独立进程句柄）")
    ap.add_argument("--idle", type=float, default=0.0,
                    help="两轮扫描之间的空闲秒数（默认 0 = 连续扫描，不歇）")
    ap.add_argument("--interval", type=float, default=None,
                    help="--idle 的别名（兼容旧命令）")
    ap.add_argument("--log", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "collector.log"),
        help="日志文件（控制台不输出，日志只落文件 + 前端）")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()
    if args.port is None: args.port = 48778 if args.backend == "cpp" else 48766

    idle = args.idle if args.interval is None else max(0.0, args.interval)
    workers = max(1, min(16, args.workers))

    # ---- 采集器：先于 HTTP 服务构造并启动，与 HTTP 完全解耦 ----
    if args.backend == "cpp":
        from native_scanner import EXECUTABLE
        if not os.path.isfile(EXECUTABLE):
            return 4
    monitor = Monitor(args.expect, idle, args.log, workers=workers, backend=args.backend, db_path=args.db)
    Handler.monitor = monitor

    worker = threading.Thread(target=monitor.run, name="collector", daemon=True)
    worker.start()

    # ---- HTTP：只做静态托管 + 状态读取，不参与扫描 ----
    try:
        httpd, real_port = serve(args.host, args.port, Handler)
    except OSError:
        # 端口全被占用：静默退出（日志无从记录）
        monitor.stop()
        return 3

    url = f"http://{args.host}:{real_port}/"
    monitor.log("info", f"HTTP 视图服务监听 {url}"
                        + ("" if real_port == args.port
                           else f"（端口 {args.port} 被占用，已改用 {real_port}）"))
    if real_port != args.port:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(args.log)),
                                   "port.txt"), "w", encoding="utf-8") as fh:
                fh.write(str(real_port))
        except Exception:
            pass

    if not args.no_open:
        try:
            import webbrowser
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        try:
            httpd.server_close()
            worker.join(timeout=35)
            monitor.store.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    go_silent()          # 只在真正启动服务时静默，import 时不动调用方的 stdout
    sys.exit(main())
