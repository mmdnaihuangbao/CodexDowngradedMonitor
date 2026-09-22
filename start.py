#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Codex 模型降级监控 · 启动器

为什么启动逻辑不放 .bat
-----------------------
cmd.exe 按本地代码页解析 .bat（中文 Windows 是 GBK）。文件里只要出现非 ASCII
字符，就可能出现「尾字节吞掉后一个字符」的现象 —— 典型症状是
`where pythonw >nul 2>nul` 里的 `>` 被吃掉，于是报
`'nul' 不是内部或外部命令`。所以真正的启动逻辑放在这个 .py 里，
.bat 只做一件事：找到 python 然后调用本文件，且全文件只用 ASCII。

采集器本身是静默的（stdout/stderr 接到空设备），
所以启动进度、状态、停止都由这个启动器负责反馈。

用法
----
    python start.py                 启动（已在跑就只把浏览器指过去）
    python start.py --port 9000
    python start.py --workers 8     并行扫描线程数（默认 4）
    python start.py --min-interval-ms 100  最小采样间隔（默认 0ms = 连续扫）
    python start.py --expect gpt-6-astra
    python start.py --fg            前台运行，Ctrl+C 停止（排错用）
    python start.py --status        查看运行状态
    python start.py --stop          停止服务
    python start.py --no-open       启动但不自动开浏览器
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from history_store import DEFAULT_DB
from config_store import load_config, validate_config, validate_port, browser_host

ROOT = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(ROOT, "collector.py")
LOGFILE = os.path.join(ROOT, "collector.log")

DEFAULT_PORT = 48778
PORT_TRIES = 12

# subprocess 创建标志：无控制台窗口 + 与父进程解绑（父进程退出后继续跑）
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def say(*args):
    """pythonw 下 sys.stdout 可能是 None，print 会抛异常 —— 全部吞掉"""
    try:
        print(*args)
    except Exception:
        pass


# ---------------------------------------------------------------- HTTP 小工具

def _opener():
    # 必须绕过系统代理：某些环境会把 127.0.0.1 也丢给代理，导致 502
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(port, path, method="GET", body=None, timeout=2.0, host="localhost"):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(f"http://{browser_host(host)}:{port}{path}",
                                 data=data, headers=headers, method=method)
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_running(port, host="localhost"):
    """在 port..port+11 范围内找我们自己的服务；找不到返回 None。

    端口被占用时采集器会自动顺延，所以这里要扫一段而不是只看一个端口。
    连不上是立刻 refused（不是等超时），所以循环很快。
    """
    for p in range(port, min(65536, port + PORT_TRIES)):
        try:
            h = api(p, "/api/health", timeout=1.0, host=host)
            if isinstance(h, dict) and h.get("ok"):
                return p
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 动作

def open_browser(port, no_open, host="localhost"):
    if no_open:
        return
    try:
        import webbrowser
        webbrowser.open(f"http://{browser_host(host)}:{port}/")
    except Exception:
        pass


def cmd_status(port, host="localhost"):
    p = find_running(port, host)
    if p is None:
        say("状态：未在运行")
        return 1
    try:
        snap = api(p, "/api/snapshot", host=host)
    except Exception as e:
        say(f"状态：端口 {p} 有服务但读取失败：{e}")
        return 1

    s = snap["stats"]
    c = s.get("counts", {})
    status_cn = {
        "running": "采集中", "starting": "启动中", "no_process": "未发现 codex.exe",
        "denied": "进程无权限", "stopped": "已停止",
    }.get(s.get("status"), s.get("status"))

    say(f"状态       : {status_cn}")
    say(f"面板       : http://{browser_host(host)}:{p}/")
    say(f"目标进程   : pid={s.get('pid')}")
    say(f"预期模型   : {s.get('expect')}")
    say(f"采样频率   : {s.get('hz')} 轮/秒   轮数 {s.get('rounds')}")
    say(f"单轮耗时   : {s.get('scan_cost')} s   单轮扫描量 "
        f"{round((s.get('scan_bytes') or 0) / 1048576, 2)} MB")
    say(f"扫描线程   : {s.get('active_workers')}/{s.get('workers')}   "
        f"最小采样间隔 {s.get('min_interval_ms', (s.get('idle') or 0) * 1000)}ms")
    say(f"已捕获     : {s.get('captured')}   请求配对 {s.get('paired')}")
    say(f"判定       : 正常 {c.get('normal', 0)} ｜ 子任务 {c.get('subtask', 0)} ｜ "
        f"不完整 {c.get('incomplete', 0)} ｜ 降级 {c.get('downgrade', 0)}")
    for a in (snap.get("alerts") or [])[:5]:
        say(f"  降级告警  : [{a.get('ts')}] {a.get('req_model')} -> {a.get('model')}")
    return 0


def cmd_stop(port, host="localhost"):
    p = find_running(port, host)
    if p is None:
        say("状态：未在运行，无需停止")
        return 0
    try:
        api(p, "/api/shutdown", method="POST", body={}, host=host)
    except Exception:
        pass
    for _ in range(40):
        time.sleep(0.25)
        if find_running(p, host) is None:
            say(f"已停止（端口 {p} 已释放）")
            return 0
    say(f"已发送停止指令，但端口 {p} 仍被占用；可直接结束该 python 进程")
    return 1


def cmd_foreground(args):
    """前台运行：直接在当前进程里跑采集器，Ctrl+C 即停"""
    sys.path.insert(0, ROOT)
    argv = [COLLECTOR, "--host", args.host, "--port", str(args.port), "--workers", str(args.workers),
            "--min-interval-ms", str(args.min_interval_ms), "--expect", args.expect, "--db", args.db, "--no-open"]
    if args.log:
        argv += ["--log", args.log]
    sys.argv = argv
    import collector
    return collector.main()


def cmd_start(args):
    from native_scanner import EXECUTABLE
    if not os.path.isfile(EXECUTABLE):
        say("尚未编译 C++ 采集器，请运行 powershell -ExecutionPolicy Bypass -File cpp_collector/build.ps1")
        return 2
    running = find_running(args.port, args.host)
    if running is not None:
        say(f"服务已在运行（端口 {running}），直接打开面板。")
        say(f"前端地址：http://{browser_host(args.host)}:{running}/")
        open_browser(running, args.no_open, args.host)
        return 0

    if args.fg:
        say(f"前台运行中…… 面板 http://{browser_host(args.host)}:{args.port}/   按 Ctrl+C 停止")
        return cmd_foreground(args)

    if not os.path.isfile(COLLECTOR):
        say(f"找不到 {COLLECTOR}")
        return 2

    interp = sys.executable or "python"
    argv = [interp, COLLECTOR, "--host", args.host, "--port", str(args.port),
            "--workers", str(args.workers), "--min-interval-ms", str(args.min_interval_ms),
            "--expect", args.expect, "--db", args.db, "--no-open"]
    if args.log:
        argv += ["--log", args.log]

    try:
        subprocess.Popen(
            argv, cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS)
    except Exception as e:
        say(f"启动失败：{e}")
        return 2

    # 等它就绪（最多 ~15s；采集线程首次枚举进程可能要几秒）
    real = None
    for _ in range(60):
        time.sleep(0.25)
        real = find_running(args.port, args.host)
        if real is not None:
            break

    if real is None:
        say("启动超时：15s 内没等到 HTTP 服务就绪。")
        say(f"请查看日志：{args.log or LOGFILE}")
        return 1

    say("")
    say("  Codex 模型降级监控 已启动")
    say("  " + "-" * 46)
    say(f"  前端地址    http://{browser_host(args.host)}:{real}/")
    say(f"  HTTP 监听   {args.host}:{real}")
    say(f"  扫描线程    {args.workers}    最小采样间隔 {args.min_interval_ms}ms"
        + ("（连续扫描）" if args.min_interval_ms == 0 else ""))
    say(f"  预期模型    {args.expect or '未设置'}")
    if real != args.port:
        say(f"  注意        端口 {args.port} 被占用，已自动改用 {real}")
    say(f"  日志        {args.log or LOGFILE}")
    say("  " + "-" * 46)
    say(f"  查看状态    python start.py --status --host {args.host} --port {real}")
    say(f"  停止服务    python start.py --stop --host {args.host} --port {real}")
    say("")
    say("  采集器已在后台静默运行。未启动 Codex 时面板会显示"
        "「未发现 codex.exe」，属正常，启动 Codex 后会自动接入。")

    open_browser(real, args.no_open, args.host)
    return 0


# ---------------------------------------------------------------- 入口

def main():
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        say(f"读取 config.json 失败：{exc}")
        return 2
    ap = argparse.ArgumentParser(
        description="Codex 模型降级监控 · 启动器（启动 / 状态 / 停止）")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite 历史库文件")
    ap.add_argument("--host", default=config["host"], help="HTTP 监听地址，0.0.0.0 支持局域网访问")
    ap.add_argument("--port", type=int, default=None,
                    help=f"HTTP 监听端口（默认 {DEFAULT_PORT}，被占用自动顺延）")
    ap.add_argument("--workers", type=int, default=config["workers"],
                    help="C++ 采集器并行扫描线程数（1～16，默认读取配置）")
    ap.add_argument("--min-interval-ms", type=float, default=None, help="最小采样间隔，单位 ms")
    ap.add_argument("--idle", type=float, default=None, help="最小采样间隔的秒单位别名")
    ap.add_argument("--expect", default=config["expect"], help="预期模型，默认留空")
    ap.add_argument("--log", default=None, help="日志文件路径")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--fg", action="store_true", help="前台运行（Ctrl+C 停止）")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的服务")
    ap.add_argument("--status", action="store_true", help="查看运行状态")
    args = ap.parse_args()
    if args.port is None:
        args.port = config["cpp_port"]
    else:
        try:
            args.port = validate_port(args.port)
        except ValueError as exc:
            ap.error(str(exc))
    if args.min_interval_ms is None:
        args.min_interval_ms = config["min_interval_ms"] if args.idle is None else args.idle * 1000
    try:
        effective = validate_config({"expect": args.expect, "workers": args.workers,
                                     "min_interval_ms": args.min_interval_ms, "host": args.host})
    except ValueError as exc:
        ap.error(str(exc))
    args.expect = effective["expect"]

    if args.status:
        return cmd_status(args.port, args.host)
    if args.stop:
        return cmd_stop(args.port, args.host)
    return cmd_start(args)


if __name__ == "__main__":
    sys.exit(main())
