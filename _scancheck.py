# -*- coding: utf-8 -*-
"""扫描链路端到端验证。

本机没有 codex.exe，所以造一个「假 codex」子进程：
在它自己的堆内存里放四个真实形态的响应对象（正常 / 降级 / 子任务 / 不完整）
和三条客户端请求对象，然后让 collector 的采集线程去扫它。

验证点：
  1. 进程连接 / 区域枚举 / 并行切片 / 字节级预筛
  2. 服务端标识判据（SERVER_MARKERS >= 2）与客户端特征排除
  3. 请求侧配对（响应.prev == 请求.previous_response_id）
  4. 四级判定：normal / subtask / downgrade / incomplete
  5. 只有 downgrade 触发告警；incomplete 绝不报警
"""
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, r"C:\Users\naihuangbao\Desktop\CodeDowngradedMonitor")

import collector as C   # noqa: E402

PY = sys.executable
ROOT = r"C:\Users\naihuangbao\Desktop\CodeDowngradedMonitor"

CHILD_SRC = r'''
import time

def resp(rid, model, effort, prev, status="completed"):
    return ('{"id":"%s","object":"response","created_at":1700000000,'
            '"status":"%s","max_tool_calls":null,"model":"%s","moderation":null,'
            '"safety_identifier":"user-fake123","reasoning":{"effort":"%s"},'
            '"previous_response_id":"%s","completed_at":1700000001,'
            '"frequency_penalty":0.0,"presence_penalty":0.0,'
            '"max_output_tokens":8192,"format":{"type":"json_schema"}}'
            % (rid, status, model, effort, prev)).encode()

def req(model, prev, kind="response.create"):
    return ('{"type":"%s","model":"%s","previous_response_id":"%s",'
            '"input":[{"role":"user","content":"hi"}],"stream":true}'
            % (kind, model, prev)).encode()

PAD = b"P" * 6000     # 把各对象拉开，模拟它们落在不同分配块里

BLOBS = [
    req("gpt-6-astra", "resp_req001"),          # 主对话请求
    PAD,
    resp("resp_0001", "gpt-6-astra", "xhigh", "resp_req001"),   # 正常
    PAD,
    req("gpt-6-astra", "resp_req002"),
    PAD,
    resp("resp_0002", "luna-mini", "xhigh", "resp_req002"),     # 降级
    PAD,
    req("luna-mini", "resp_req003"),
    PAD,
    resp("resp_0003", "luna-mini", "low", "resp_req003"),       # 子任务
    PAD,
    # 请求侧故意不提供 resp_req404 对应的请求对象 -> 配对失败
    # 响应模型 != 预期 且 effort != low -> 应为「不完整」，不得报警
    resp("resp_0004", "gpt-5-mini", "xhigh", "resp_req404"),
]

HOLD = []
for b in BLOBS:
    HOLD.append(bytearray(b))

print("READY", flush=True)
time.sleep(60)
'''


def main():
    child = subprocess.Popen([PY, "-c", CHILD_SRC],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        line = child.stdout.readline().decode().strip()
        assert line == "READY", f"子进程未就绪: {line!r}"

        # 让采集器把假进程当成 codex.exe
        C.enumerate_codex = lambda: [{
            "pid": child.pid,
            "cmd": r"C:\fake\codex.exe --app-server",
            "mb": 20.0,
        }]

        mon = C.Monitor("gpt-6-astra", 0.0, None, workers=4)
        t = threading.Thread(target=mon.run, daemon=True)
        t.start()

        # 等它扫到
        for _ in range(60):
            time.sleep(0.2)
            if len(mon.seen) >= 4:
                break

        st = mon.stats()
        print("== 采集统计 ==")
        print("  status  :", st["status"], " pid:", st["pid"])
        print("  workers :", st["active_workers"], "/", st["workers"])
        print("  regions :", st["regions"], " 区域枚举耗时:", st["region_cost"], "s")
        print("  单轮耗时:", round(st["scan_cost"], 5), "s",
              "  单轮扫描量:", round(st["scan_bytes"] / 1048576, 3), "MB")
        print("  采样频率:", st["hz"], "轮/秒   轮数:", st["rounds"])
        print("  计数    :", st["counts"])

        print("\n== 捕获到的响应 ==")
        for rid, r in sorted(mon.seen.items()):
            print(f"  {rid}  model={r.get('model'):<12} effort={r.get('effort'):<6}"
                  f" status={r.get('status'):<10} verdict={r.get('_verdict'):<10}"
                  f" 请求={r.get('_req_model')}")

        print("\n== 告警 ==")
        for a in mon.alerts:
            print(f"  [{a['ts']}] {a['req_model']} -> {a['model']}"
                  f"  paired={a['paired']}  effort={a['effort']}")

        print("\n== 诊断接口（只读回放）==")
        d = mon.diagnose()
        print("  ok:", d["ok"], " blocks:", d.get("blocks"),
              " raw:", d.get("raw_responses"), " pairings:", d.get("pairings"))

        # ---- 断言 ----
        expect = {
            "resp_0001": ("normal", "gpt-6-astra"),
            "resp_0002": ("downgrade", "gpt-6-astra"),
            "resp_0003": ("subtask", "luna-mini"),
            "resp_0004": ("incomplete", None),
        }
        fail = []
        for rid, (v, rm) in expect.items():
            got = mon.seen.get(rid)
            if got is None:
                fail.append(f"{rid} 未捕获")
                continue
            if got.get("_verdict") != v:
                fail.append(f"{rid} 判定={got.get('_verdict')}，应为 {v}")
            if got.get("_req_model") != rm:
                fail.append(f"{rid} 配对请求模型={got.get('_req_model')}，应为 {rm}")

        # 只有真降级能产生告警；不完整绝不能报警
        if not mon.alerts:
            fail.append("真降级未产生告警")
        else:
            if mon.alerts[0]["rid"] != "resp_0002":
                fail.append(f"告警响应ID={mon.alerts[0]['rid']}，应为 resp_0002")
            bad = [a["rid"] for a in mon.alerts if a["rid"] == "resp_0004"]
            if bad:
                fail.append("「不完整」被误报成降级告警")
        if mon.count.get("incomplete") != 1:
            fail.append(f"incomplete 计数={mon.count.get('incomplete')}，应为 1")
        if mon.count.get("downgrade") != 1:
            fail.append(f"downgrade 计数={mon.count.get('downgrade')}，应为 1")

        mon.stop()
        print("\n结果:", "全部通过" if not fail else "失败 -> " + "; ".join(fail))
        return 1 if fail else 0
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except Exception:
            child.kill()


if __name__ == "__main__":
    sys.exit(main())
