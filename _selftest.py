# -*- coding: utf-8 -*-
"""自包含端到端测试：启动 collector → 探测全部接口 → 关闭"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
PORT = 48766
BASE = f"http://127.0.0.1:{PORT}"

# 绕过沙箱代理
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path, timeout=10):
    with opener.open(BASE + path, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def post(path, obj=None, timeout=10):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read()


env = dict(os.environ)
env.pop("HTTP_PROXY", None)
env.pop("HTTPS_PROXY", None)
env.pop("http_proxy", None)
env.pop("https_proxy", None)

# 本脚本会 POST /api/config，而配置路径固定在仓库里：先备份，跑完还原，别改掉本机设置。
CONFIG_PATH = os.path.join(ROOT, "config.json")
SAVED_CONFIG = open(CONFIG_PATH, "rb").read() if os.path.exists(CONFIG_PATH) else None

proc = subprocess.Popen(
    [PY, "collector.py", "--port", str(PORT), "--no-open", "--workers", "4", "--idle", "0"],
    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

ok = True
try:
    # 等服务起来
    for _ in range(40):
        try:
            get("/api/health", timeout=2)
            break
        except Exception:
            time.sleep(0.25)
    else:
        print("!! 服务未在 10s 内就绪")
        ok = False

    if ok:
        print("== 静态资源 ==")
        for p in ["/", "/style.css", "/app.js", "/favicon.ico"]:
            s, ct, b = get(p)
            print(f"  {p:<14} {s}  {ct:<34} {len(b)}B")

        time.sleep(3)   # 让采集线程跑几轮
        print("\n== /api/snapshot ==")
        s, ct, b = get("/api/snapshot")
        d = json.loads(b)
        print("  keys:", list(d.keys()))
        print("  stats:", json.dumps(d["stats"], ensure_ascii=False))
        for l in d["logs"]:
            print("  log :", l)

        print("\n== /api/diagnose（只读回放，不触发扫描）==")
        s, ct, b = get("/api/diagnose", timeout=30)
        print("  ", json.dumps(json.loads(b), ensure_ascii=False)[:500])

        print("\n== /api/config ==")
        s, b = post("/api/config", {"expect": "gpt-6-astra", "idle": 0.1})
        print(f"   {s} {b.decode()[:200]}")

        print("\n== /api/stream (SSE 首帧) ==")
        try:
            with opener.open(BASE + "/api/stream", timeout=6) as r:
                raw = r.readline().decode().strip()
                print("  ", raw[:200])
        except Exception as e:
            print("   SSE 读取异常:", e)
            ok = False

        print("\n== /api/clear ==")
        s, b = post("/api/clear")
        print(f"   {s} {b.decode()[:120]}")

        print("\n== 单例保护测试（相同及不同端口）==")
        from instance_guard import ALREADY_RUNNING
        for duplicate_port in (PORT, PORT + 1):
            result = subprocess.run(
                [PY, "collector.py", "--port", str(duplicate_port), "--no-open"],
                cwd=ROOT, capture_output=True, env=env, timeout=10)
            health = json.loads(get("/api/health")[2])
            passed = (result.returncode == ALREADY_RUNNING and health['pid'] == proc.pid
                      and not result.stdout and not result.stderr)
            print("   端口", duplicate_port, "重复启动被拒绝:", passed)
            ok = ok and passed

    print("\n== 停止接口 ==")
    s, b = post("/api/shutdown")
    print("  ", s, b.decode()[:80])

finally:
    try:
        out, err = proc.communicate(timeout=12)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    if SAVED_CONFIG is not None:
        with open(CONFIG_PATH, "wb") as stream:
            stream.write(SAVED_CONFIG)

print("\n== 控制台输出检查 ==")
print("  stdout bytes =", len(out or b""), "| stderr bytes =", len(err or b""))
if out:
    print("  stdout 内容:", out[:400])
if err:
    print("  stderr 内容:", err[:400])

print("\n结果:", "全部通过" if ok and not out and not err else "有失败项")
sys.exit(0 if (ok and not out and not err) else 1)
