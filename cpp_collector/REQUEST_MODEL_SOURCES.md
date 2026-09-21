# 请求模型来源调查（2026-09-20）

目标：在维持进程内存只读、不注入、不改写 Codex 流量的前提下，提高请求模型证据的完整性。

## 本地已验证的事实

- 原采集器只在同时包含 `"resp_` 与 `"model"` 的 1 MiB 块中寻找请求；首个没有前序 ID 的请求可能被整块过滤。
- `extract_requests()` 只看 `response.create` 后 512 字节，模型/前序 ID 在长 input 后时无法正确提取。
- 请求索引以 `previous_response_id` 为键，空键不参与配对；同一前序号的并发分支会覆盖旧值。
- `Monitor.tick()` 旧逻辑只在响应新增或状态前进时应用请求映射；稍后才采到的请求不能修复已稳定的响应卡片。
- 只读检查本机 `logs_2.sqlite` 最近 20,000 条记录：在 HTTP 客户端、WebSocket endpoint、核心 client/retry 这些特定目标中能找到 `model` 和部分 `request_id`，但样本中没有同事件的 `resp_…` 响应号。没有输出或持久化日志原文。
- 当天最近会话文件的 `turn_context` 有 `model`、`turn_id`、`root_turn_id`，缺少逐 Responses 请求的响应号。一次 turn 可包含多个模型请求及工具循环，因此不能直接一对一配对。

## 方案比较

| 来源 | 请求模型证据 | 对既有只读方式的影响 | 当前结论 |
|---|---|---|---|
| 改善内存扫描与结构解析 | 实际序列化请求字段；仍有短生命周期窗口 | 保留原机制 | 本次已实现 C++ 路径，并修复延迟/冲突配对 |
| 本地 SQLite 日志 | 存在模型、线程/请求上下文；当前样本缺少与 `resp_…` 的连接键 | 可只读打开数据库 | 适合作为附加上下文；暂不能填入精确配对栏 |
| 会话 JSONL `turn_context` | 当前回合选择的模型 | 只读文件 | 可解释回合设置，不能当作每个实际请求的证据 |
| Codex 官方 OpenTelemetry | 官方文档提供 model 元数据、API/SSE/WebSocket 事件及耗时 | 需要启用导出并接收本地 OTLP，不再是纯被动扫描 | 值得下一阶段做 localhost 验证；须先确认具体版本是否导出 response ID、关联 span 和实际请求模型 |
| 自有网关/服务端访问日志 | 若网关记录请求体 model 且记录下游响应 ID，可直接关联 | 需要访问或修改用户控制的服务端，不修改目标内存 | 若已拥有网关日志，这是更稳妥的候选；本次未访问或修改网关 |
| 本地代理/改 base_url | 可在发送时获取请求模型；能否关联还取决于响应处理 | 改变网络路径、配置或 TLS 信任 | 不属于本次“机制一致”的实现范围 |
| 普通抓包/ETW 网络事件 | 可获得连接与时序；TLS 下不能直接当作明文 JSON 请求证据 | 可能需额外系统权限/捕获设施 | 不能仅靠网络事件替代请求体提取，未实施 |
| 注入、API Hook、调试断点 | 理论上可捕获短暂的明文参数 | 超出内存只读边界 | 不采用 |

## 推荐顺序

先使用 C++ 后端消除现有漏采点和进程重连开销。若仍需要完整逐请求覆盖，优先确认是否有自有网关的请求/响应关联日志；其次在明确允许改 Codex 配置后，验证本地 OTel 导出的字段。仅当确实拿到可关联的请求证据时才提升配对等级；不按时间最近或全局模型设置硬填请求模型。

当前没有改动 `config.toml`，没有启用日志/提示文本导出，没有部署代理。官方文档只证明 OTel 支持相关事件和模型元数据，不证明当前安装版本一定导出可与 `resp_…` 唯一关联的完整字段。

## 官方依据

- [Codex 高级配置：Observability and telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)：OTel 默认不导出，支持 model 元数据和 API/SSE/WebSocket 事件，启用需要配置 exporter。
- [Microsoft WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)：等待句柄需要 SYNCHRONIZE 权限。
- [Microsoft GetExitCodeProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getexitcodeprocess)：使用进程查询权限读取退出状态。
