# 请求模型来源调查（2026-09-20）

> **2026-09-22 补充：本文第 11、12 条结论已被推翻并修正。** 新增两条**精确**请求模型通道（codex 自有日志的响应号前缀、rollout 的 `token_usage_record`），二者与内存 `previous_response_id` 配对交叉验证 157/157 与 140/140 **零冲突**，并在 20 条真实降级样本上 20/20 给出**请求侧**模型。详见文末「[2026-09-22 补充：两条精确通道](#2026-09-22-补充两条精确通道已验证)」。

目标：在维持进程内存只读、不注入、不改写 Codex 流量的前提下，提高请求模型证据的完整性。

## 本地已验证的事实

- 原采集器只在同时包含 `"resp_` 与 `"model"` 的 1 MiB 块中寻找请求；首个没有前序 ID 的请求可能被整块过滤。
- `extract_requests()` 只看 `response.create` 后 512 字节，模型/前序 ID 在长 input 后时无法正确提取。
- 请求索引以 `previous_response_id` 为键，空键不参与配对；同一前序号的并发分支会覆盖旧值。
- `Monitor.tick()` 旧逻辑只在响应新增或状态前进时应用请求映射；稍后才采到的请求不能修复已稳定的响应卡片。
- ~~只读检查本机 `logs_2.sqlite` 最近 20,000 条记录：……样本中没有同事件的 `resp_…` 响应号。~~ **取样口径不足，已修正**：`logs` 表确实不写完整 `resp_…`，但它写**每一条 output item 的 ID**，而 item ID 与响应 ID 共享同一段请求前缀；按前缀关联即可逐请求还原模型。见补充第 2 节。
- ~~当天最近会话文件的 `turn_context` 有 `model`、`turn_id`、`root_turn_id`，缺少逐 Responses 请求的响应号，因此不能直接一对一配对。~~ **结论已修正**：漏看了同一文件里的 `token_usage_record`，它带 `response_id`（= `resp_…`）与 `turn_id`，因此 `response_id → turn_id → turn_context.model` 可以做到**逐请求一对一**。见补充第 1 节。

## 方案比较

| 来源 | 请求模型证据 | 对既有只读方式的影响 | 当前结论 |
|---|---|---|---|
| 改善内存扫描与结构解析 | 实际序列化请求字段；仍有短生命周期窗口 | 保留原机制 | 本次已实现 C++ 路径，并修复延迟/冲突配对 |
| 本地 SQLite 日志 | **可给出逐请求的请求模型**：`logs` 的 output item ID 与响应 ID 共享 23～25 位十六进制请求前缀，span 上带 `model=` / `turn.id=` | 可只读打开数据库（已验证 codex 运行时可读） | **纳入正式配对证据**，来源标记 `codex_log_prefix` |
| 会话 JSONL `turn_context` / `token_usage_record` | **可给出逐请求的请求模型**：`token_usage_record.response_id → turn_id → turn_context.model` | 只读文件（追加写，可尾随） | **纳入正式配对证据**，来源标记 `rollout_token_usage` |
| Codex 官方 OpenTelemetry | 官方文档提供 model 元数据、API/SSE/WebSocket 事件及耗时 | 需要启用导出并接收本地 OTLP，不再是纯被动扫描 | 值得下一阶段做 localhost 验证；须先确认具体版本是否导出 response ID、关联 span 和实际请求模型 |
| 自有网关/服务端访问日志 | 若网关记录请求体 model 且记录下游响应 ID，可直接关联 | 需要访问或修改用户控制的服务端，不修改目标内存 | 若已拥有网关日志，这是更稳妥的候选；本次未访问或修改网关 |
| 本地代理/改 base_url | 可在发送时获取请求模型；能否关联还取决于响应处理 | 改变网络路径、配置或 TLS 信任 | 不属于本次“机制一致”的实现范围 |
| 普通抓包/ETW 网络事件 | 可获得连接与时序；TLS 下不能直接当作明文 JSON 请求证据 | 可能需额外系统权限/捕获设施 | 不能仅靠网络事件替代请求体提取，未实施 |
| 注入、API Hook、调试断点 | 理论上可捕获短暂的明文参数 | 超出内存只读边界 | 不采用 |

## 推荐顺序

先使用 C++ 后端消除现有漏采点和进程重连开销。逐请求覆盖由**服务侧只读旁路索引**补足（2026-09-22 起实现，见补充章节）：codex 自有日志的响应号前缀索引为主，rollout 的 `token_usage_record` 索引为辅，两者都只提取 id/model/effort/turn 字段，不落日志正文，且不进 C++ 扫描器。若仍需要完整逐请求覆盖，再考虑自有网关的请求/响应关联日志；OTel 导出已非必要（其收益被上述两条通道覆盖，且需要改 Codex 配置）。

当前没有改动 `config.toml`，没有启用日志/提示文本导出，没有部署代理。官方文档只证明 OTel 支持相关事件和模型元数据，不证明当前安装版本一定导出可与 `resp_…` 唯一关联的完整字段。

## 官方依据

- [Codex 高级配置：Observability and telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)：OTel 默认不导出，支持 model 元数据和 API/SSE/WebSocket 事件，启用需要配置 exporter。
- [Microsoft WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)：等待句柄需要 SYNCHRONIZE 权限。
- [Microsoft GetExitCodeProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getexitcodeprocess)：使用进程查询权限读取退出状态。

---

## 2026-09-22 补充：两条精确通道（已验证）

调查范围：本机 `data/monitor.sqlite`（253 条响应）、`~/.codex/logs_2.sqlite`（52,437 行，09-14 起）、`~/.codex/sessions` + `archived_sessions`（325 个 rollout，1.88 GB）、`Desktop/Codex400/captures`（7 个会话、5.4 GB 抓包）。全程只读，未修改被观测进程、未改 Codex 配置、未代理流量。

### 1. 通道 A：rollout `token_usage_record`（来源标记 `rollout_token_usage`）

```
token_usage_record.payload = { thread_id, turn_id, session_id, root_turn_id, response_id, usage, … }
                                                              ↑ resp_…
turn_context.payload       = { turn_id, root_turn_id, model, effort, … }
```

- 键：`response_id`（响应号，与内存侧同一字符串）；值：`turn_context.model` / `effort`。
- 实测：6203 个 `response_id`，6192 个能取到 turn 模型。
- 交叉验证：与内存 `previous_response_id` 配对**同时有答案的 157 条，157 一致，0 冲突**。
- 局限：只为**写 rollout 的会话**服务。`cwd=~/.codex/memories` 的后台记忆线程不写 rollout，落在通道 B。

### 2. 通道 B：codex 自有日志的响应号前缀（来源标记 `codex_log_prefix`）

响应 ID 与它产出的 output item ID 共享同一段请求前缀：

```
响应: resp_ <请求前缀> <响应段>
item: rs_ | msg_ | fc_ | ctc_ | ctco_ | at_  <同一段请求前缀> <item 段>
```

抓包实测（469 个响应、2114 个本响应产出的 item）：共享 **25 位 1291 个、24 位 706 个、23 位 103 个** —— 前缀长度不固定。

**2026-09-22 运行期修正（重要）**：该前缀不是随机值，而是**递增计数器** —— 同一回合内相邻请求
常常只差最后 1–2 个 hex（实测 `…b1f7a` / `…b1f7f` / `…b1f80` / `…b1f81` / `…b1f82`）。
因此**不能按固定长度（25→24→23）截断取键**：那等于拿邻居请求的模型。实现已改为保存
item ID 前 26 位，查询时在**同线程桶**（ID 前 20 位）内比共享前缀长度、取最长的一条；
并列却给出不同模型时不给答案（`ambiguous_response_id_prefix`）。混合模型回合只会丢覆盖，不会给错模型。
另实测 item ID 的十六进制部分已到 **50 位**（响应 ID 仍是 48 位），抽取上限已放宽到 64 位。

`logs` 表把每次采样请求的每个 output item 都写下来，且同一行的 tracing span 带模型与回合：

```
…turn{thread.id=… turn.id=… model=gpt-5.6-terra codex.turn.reasoning_effort=medium}:
   try_run_sampling_request{turn_id=… model=gpt-5.6-terra}:
   Output item item_type="reasoning" item_id="rs_0c77857d95e57910016ab086cee5fc…"
```

- 键：响应 ID 的十六进制前缀；值：同行 span 的 `model=` / `turn.id=` / `reasoning_effort=`。
- 实测：出现 1590 个前缀；**同一前缀映射到多个模型的 0 个、映射到多个 `turn_id` 的 0 个**。
- 交叉验证：与内存配对 **agree 140 / disagree 0**。
- 只读可用性：`file:…logs_2.sqlite?mode=ro` 在 codex 运行时读到实时数据（已实测）。若直接读**拷贝**，必须先 `PRAGMA wal_checkpoint(TRUNCATE)`，否则表结构不可见。

### 3. 判定性验证：通道给出的是「请求模型」而不是「服务模型」

抓包会话 `2026-09-18_14-08-32_378734` 中 20 条真实降级（请求 `gpt-6-astra`、实际下发 `gpt-5.6-luna`）：

| 请求模型（抓包请求体） | 服务模型（抓包响应体） | 通道 A | 通道 B |
|---|---|---|---|
| `gpt-6-astra` ×20 | `gpt-5.6-luna` ×20 | `gpt-6-astra` **20/20** | `gpt-6-astra` **20/20** |

因此两条通道都能支撑「请求模型 ≠ 响应模型 → 降级」的硬判定与告警，不是循环论证（若给的是服务模型，本行结果应为 luna）。

### 4. 覆盖与本机收益（253 条基线，实现后实测）

| 项 | 数量 |
|---|---|
| 无请求证据的响应（实现前） | 47（incomplete 23 / normal 11 / subtask 13） |
| 只取 25 位前缀时 incomplete 可解 | 14/23 |
| **改为「共享前缀最长」匹配后** | item 通道命中 **12 条**（旧的固定长度口径 9 条，且存在取到邻居请求模型的风险） |
| 实现后经旁路通道拿到请求模型 | **21 条**（会话记录 12 + 日志前缀 9） |
| 实现后 incomplete（非疑似） | 23 → **6**（清理掉的 `req == resp`，即采集缺口而非降级） |
| 实现后请求配对 | 206 → **227** |
| 与内存配对交叉验证（两边都有答案） | **188 条全部一致，0 冲突** |
| 抓包真实降级样本哨兵 | **21/21 给出请求侧模型** |

### 5. 未确认与边界（不得当作结论）

- **残余 incomplete 已定性（2026-09-22 现场实例）**：`resp_0c783d043ef225a4016ab1f3fcc59c87d0844…`（11:20:30，`gpt-5.6-sol`，`completed`，`previous_response_id` 为空）。它在 `logs` 里只命中 1 行、共 80 字符：
  `last_model_response_id="resp_0c783d…"`（target `feedback_tags`，module `codex_core::client`）。
  同批次的 span 是 `app_server.request{…thread/start…}:thread_spawn:session_init:startup_prewarm{thread.id=01a0c721-…}` ——
  即**新线程预热请求**。该批次所有行内**没有 `model=` token、没有 `item_id`**；该响应号在 rollout 命中 0 行、在 `thread_items` 命中 0 行。
  结论：这类请求（`startup_prewarm`、`cwd=~/.codex/memories` 的后台记忆生成线程等非持久化旁路调用）
  **请求侧模型在本地数据里不存在**，属数据边界而非解析缺陷；面板上会长期显示为「不完整」。
  要覆盖它只能引入"按同线程 span 或先后顺序推断"的弱来源，属于猜测，按既定边界不实现。
  更早那批 6 条残余（`0c7785…` 之外的 `0679f064…`/`00ee5cdd…`/`06ea4a05…`/`0e99973c…`/`032a2ce2…`/`094c6227…`）
  前缀在 `logs`、`thread_history_1.sqlite` 与 `.codex` 下 25,471 个其他文件中零命中，与本例同属这一类。
- `thread_history_1.sqlite` 的 `thread_items.item_id` **不是**协议 item ID（`like 'resp_%'` 0 行、前缀查询 0 命中），**不可用作索引源**。
- `logs` 表的 `feedback_tags.last_model_response_id` 含 144 个完整响应号，但样本中未找到同事件的可用模型字段，未纳入方案。
- `logs_2.sqlite` 名称中的 `_2` 表明日志会轮转（当前仅覆盖 09-14 起），因此解析出的 `req_model` **必须落库**，否则历史证据随轮转丢失。
- 前缀长度与共享关系是**对服务端 ID 生成规则的逆向观察**，不是官方契约：实现必须带唯一性熔断（同一前缀出现第二个模型即整键作废，不猜）。
- 隐私边界：`feedback_log_body` 含 `world_state` 等正文，实现只提取 id/model/effort/turn/thread 五个字段，正文不落库、不入日志。
- 抽取细节的两条已知边界（实测确认，无实际影响）：① 模型名 token 短于 3 字符不会被抽取（真实模型名都远长于此）；
  ② 同一行里出现两个不同模型 token 时整行跳过，不做取舍。两者都偏保守：宁可少一条证据，不给错一条。
- 日志库轮转已跟踪：每轮按修改时间取最新的 `logs*.sqlite`，文件名变化时游标归零重扫。
  若轮转发生时本进程不在运行，那段窗口的日志不回扫（缺口由 rollout 通道兜）。
