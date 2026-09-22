# C++ 只读内存采集器

本目录专门存放 **C++ 采集器**。`src/main.cpp` 和 `src/extract.hpp` 负责 Windows 内存读取、预筛、结构化字段提取和单轮内去重。网页、HTTP/SSE、状态缓存、分类与告警复用根目录 Python 服务。不是纯 C++ HTTP 服务。

## 编译与启动

需要 VS2022 的“使用 C++ 的桌面开发”组件及 Windows SDK，无第三方 C++ 库，无需下载依赖。本机已完成 x64 Release 编译。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File cpp_collector/build.ps1
python start.py
```

也可双击根目录 `start-cpp.bat`（等价于 `start.bat`）。

| 入口 | 采集实现 | 默认端口 |
|---|---|---|
| `start-cpp.bat` / `start.bat` | C++ 常驻辅助进程 | 48778 |

双击 `stop-cpp.bat` 可停止默认端口上的实例；附加参数会继续传给启动器，例如 `stop-cpp.bat --port 48878`。

端口占用时仍顺延最多 12 个端口。`--port` 可指定端口，只影响当次运行。

```powershell
python start.py --status
python start.py --stop
python start.py --workers 4 --min-interval-ms 20 --no-open
```

`--backend` 选项已随 Python 采集器一起移除：扫描实现只有 C++ 一个。历史仍存在 `data/monitor.sqlite`，重启会读取历史；旧版尚未入库的记录需先导入，见 [历史记录说明](../HISTORY.md)。修改代码不会自动更新已经运行的旧进程。前端“采集详情”可查看扫描器状态。

## 只读边界与进程开销

- 对目标只使用 `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ`，通过 `VirtualQueryEx` / `ReadProcessMemory` 读取已提交且可读的 `MEM_PRIVATE` / `MEM_MAPPED` 区域。
- 不请求写内存、远程线程或调试权限，不注入、不 Hook、不提权、不改 Codex 配置、不接管网络流量。
- 进程发现改为 `CreateToolhelp32Snapshot` / `Process32FirstW` / `Process32NextW`，查询路径使用 `QueryFullProcessImageNameW`，不再运行 PowerShell 枚举命令。
- 存活检查改用 `GetExitCodeProcess`。旧代码的 `WaitForSingleObject` 要求 `SYNCHRONIZE` 权限，原句柄没有该权限，失败被误当作退出，导致反复重连、反复启动 PowerShell。（这些修正最早落在 Python 采集器上，该采集器已移除。）
- C++ 模式为一个 Python 网页服务进程加一个常驻 C++ 辅助进程；没有每轮新建进程，也没有每轮新建 C++ 工作线程。辅助进程用隐藏窗口标志启动。VS 构建时运行开发环境批处理属于一次性构建，不在采集路径内。
- 同时存在多个引擎时优先匹配实际小写 `codex.exe` 引擎名，再按工作集选择；不再读取 PowerShell 命令行。当前版本只监控一个引擎进程。

## 性能实现

1. C++ 常驻线程池，动态分发 1 MiB 任务；单个大区域也能分摊到多个线程。
2. 重用每个工作线程的读取缓冲区；筛选、解析在工作线程内完成，向 Python 只传必要字段，不传原始内存和对话文本。
3. 每块额外向前读取最多 256 KiB，覆盖边界对象；只负责本块起始范围内的对象，避免重叠造成重复输出。
4. 区域列表缓存 0.5 秒；上一轮命中区域先扫，但仍完成全量可读区域扫描。
5. 逐对象、逐层提取直接字段，跳过嵌套输入、输出和 JSON 字符串，避免拿相邻对象或嵌套内容的 model 拼接结果。
6. 暴露读取错误计数、读取/解析工作线程累计耗时；累计线程耗时不能与单轮墙钟耗时直接相加比较。

本机 2026-09-20 测量，4 个线程、每种后端预热 2 轮后交替测 8 轮，计时包含解析和 C++ IPC。
当时 Python 采集器与 C++ 采集器并存，下表是那次对照的原始数据（Python 侧与对照脚本 `benchmark.py` 现已随该采集器移除，数字仅作历史记录）：

| 样本 | Python 单轮中位 | C++ 单轮中位 | 比值 |
|---|---:|---:|---:|
| 固定 64 MiB 测试进程，均捕获 6 个唯一响应 | 56.553 ms | 5.654 ms | 10.00× |
| 当时运行中的 Codex 引擎 | 340.474 ms | 24.474 ms | 13.91× |

这些是特定机器和负载下的单轮延迟，不是通用提速保证，也不是整机 CPU 节省比例。C++ 扫描量包含重叠，且不再排除超过 256 MiB 的区域，所以读取字节量与 Python 不相同。真实进程的对象生命周期不同，不能用不同轮的记录数证明召回率相同；固定样本与边界单测用于验证覆盖情况。`--min-interval-ms 0` 连续扫描可能持续占用 CPU/内存带宽；想限制频率可增大最小采样间隔，例如 `--min-interval-ms 100`。间隔从两轮开始时间计算，不是在扫描后固定休息。

## 请求模型的配对

保留原有核心机制：从内存里的 `response.create` 获取请求模型，按双方的 `previous_response_id` 关联；响应侧要求至少两个原有服务器标记，排除客户端请求。预期模型非空时沿用原分类规则；默认空值不主动标记子任务，历史记录固定使用首次采集时的预期模型。

新增改进：

- C++ 请求/响应解析窗口由原来 512 B / 4 KiB 提升到最多 256 KiB，支持跳过长输入后读取后续字段。
- 预筛允许单独的 `response.create`，不要求同一块同时存在 `resp_`；没有前序 ID 的首轮请求也能观测到，但不按时间猜测配对。
- 请求后到时，即使响应没有新增状态，也会重新判定并更新原卡片。
- 同一前序 ID 观察到不同请求模型后，记为歧义，不再“最后写入覆盖”。相应旧告警从快照移除；此前已经发出的瞬时通知无法撤回。
- 界面区分无前序 ID、未捕获请求和前序 ID 歧义。统计“请求配对”现在统计实际配对的响应数，`request_keys` 单独表示索引数量。

仍有明确边界：对象若超过窗口、字段使用未支持的排列/编码、位于不可读内存、在采样间隔内消失，仍可能漏采；当前不跨独立 VirtualQueryEx 区域拼接。`previous_response_id` 是前序响应号，不是唯一请求号，分支/重试即使模型一致也不能证明一一对应。无配对时不会仅凭不同于预期模型就判定降级。内存中的模型字段只是本工具所观测到的协议字段，不能独立证明服务器实际运行了哪套权重。

## 验证

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File cpp_collector/tests/run.ps1
```

测试覆盖：唯一对象跨 1 MiB 边界（无重叠确实漏采）、长输入后字段、相邻/嵌套对象隔离、首请求、模型冲突、实际只读内存读取、存活检查、辅助进程复用和退出、延迟配对，以及 HTTP/SSE/诊断/配置/状态与关闭。测试样本只分配并写入自己的测试进程内存，不向被观测应用写内存。

## 其他请求模型来源

详见 [请求模型来源调查](REQUEST_MODEL_SOURCES.md)。目前没有把配置模型、会话模型或日志模型冒充逐请求配对结果。
