# 实际内存字段调查（2026-09-21）

> 本文保留当时的调查证据与旧 Python/C++ 实现对照，不是 v0.5 操作说明。当前实现已经迁入 `src/` 并改用 Rust，使用与测试以 README 为准。

本次按用户要求进行了只读全量扫描。只读取 Codex 引擎的已提交、可读私有/映射内存，不修改目标进程，不启动监控服务，不运行测试。只保存字段路径、类型、覆盖计数，没有保存字段值或原始内存。动态 ID、工作区路径和自定义 Schema 名称归一化为 `*`。

## 观测范围与限制

共在同一个引擎进程上完成 4 次全量遍历；每次读取都包含跨块重叠。仅统计成功解析的完整 JSON 对象，每轮内部按完整对象去重，各轮之间可能是同一对象，不能把计数相加当作唯一请求数。完整响应符合现有服务端标记与时间/状态校验，但内存启发式不能绝对排除仿真样本。

这份清单证明字段在本次运行中实际出现，不承诺未来每条请求都有这些字段。对象最大解析窗口为 1 MiB，生命周期过短、内存变化、对象不完整或超过窗口都会漏采。`json_decode_failures` 还包括误命中的普通嵌套片段，不能解释为漏掉的真实响应数。未做网络拦截，也未从 API 文档假定某个字段一定存在。

| 轮次 | 时间 | 可读区域 | 读取量（含重叠，MiB） | 完整响应对象 | WebSocket 请求对象 |
|---|---|---:|---:|---:|---:|
| 1 | 2026-09-21 09:43:26 | 238 | 224.32 | 0 | 2 |
| 2 | 2026-09-21 09:49:35 | 284 | 196.84 | 1 | 5 |
| 3 | 2026-09-21 09:50:44 | 271 | 205.73 | 1 | 5 |
| 4 | 2026-09-21 09:53:55 | 271 | 221.21 | 1 | 4 |

## 最值得补充展示的数据

| 类别 | 本次实际出现的字段 | 可用于展示 | 当前主采集器 |
|---|---|---|---|
| Token 用量 | `usage.input_tokens`、`output_tokens`、`total_tokens` | 单次请求用量 | 未抽取保存 |
| 缓存与推理 Token | `usage.input_tokens_details.cached_tokens`、`cache_write_tokens`；`usage.output_tokens_details.reasoning_tokens` | 缓存命中/写入、推理用量；有分母时计算命中率 | 未抽取保存 |
| 内容用量分摊 | `usage.attribution.items.*` 及 `content[]` 下的 Token 数 | 分项用量详情；不能未经确认重复累加到总量 | 未抽取保存 |
| 服务与推理设置 | `service_tier`、`reasoning.mode`、`context`、`summary`、`text.verbosity` | 服务等级、推理/输出设置 | 目前仅提取 effort 与格式 |
| 缓存策略 | `prompt_cache_retention`、`prompt_cache_options.mode/ttl/comparison_response_id`、`prompt_cache_diagnostics.type`、`prompt_cache_key` | 缓存配置与诊断 | 未抽取保存 |
| 采样参数 | `temperature`、`top_p`、`top_logprobs`、`frequency_penalty`、`presence_penalty` | 请求参数详情 | 部分只作识别标记 |
| 工具配置及用量 | `parallel_tool_calls`、`tool_choice`、`tools[]`、`tool_usage.web_search.num_requests`、`tool_usage.image_gen.*` | 工具声明数量、工具用量 | 未抽取保存；出现字段不等于该工具实际被调用，零也是有效值 |
| 请求上下文 | `client_metadata.thread_id/session_id/turn_id/root_turn_id` | 任务、回合归属和筛选 | 目前请求证据仅保留 model、prev、source |
| 回合详细设置 | `client_metadata.x-codex-turn-metadata` 内嵌 JSON 的 `agent_name`、`request_kind`、`thread_source`、`turn_trigger`、`model`、`reasoning_effort`、`turn_started_at_unix_ms` | 解释回合来源、所选模型和触发方式 | 未抽取保存；回合设置不能代替逐请求模型证据 |
| 工作区上下文 | 内嵌 JSON 中 `workspace_kind`、`workspaces.*.latest_git_commit_hash/has_changes` | 工作区类型与提交状态 | 未抽取保存 |
| 输入与工具消息结构 | `input[].type/role/call_id`、工具调用名称/参数与回传内容结构 | 消息/工具调用计数、工具时间线候选 | 未抽取保存；正文与参数应单独确定保存范围 |
| 状态和标识 | `background`、`store`、`truncation`、`safety_identifier` | 响应属性与关联详情 | safety_identifier 只有 Python 路径提取，C++ 未提取 |

`error`、`incomplete_details`、`instructions`、`max_output_tokens`、`max_tool_calls`、`moderation`、`user` 本次完整响应样本均为空；`output` 和 `metadata` 是空容器。因此目前只能证明这些字段存在，无法从本次响应样本证明错误原因、截断原因或输出子字段的结构。

输入工具回传里的文本确实存在，但本次没有保存正文、命令、参数值、缓存键、用户/任务/安装标识原值，也没有把上表候选字段自动加入正式数据库。

## 已有数据可直接展示

- 响应 API 已有 `prev`、`first_seen`；当前卡片没有展示。
- 数据库 payload 已有 `_process_pid`，可增加到响应详情。
- `response_events` 已有状态变化时间和采集后端，可做记录时间线。
- `request_evidence` 已有 `source` 与 `first_seen`，可说明请求模型证据来源。
- 统计/诊断接口已有 `database`、`up`、`request_keys`、`ambiguous_pairings`、`unkeyed_requests`、C++ 的 `read_errors/read_worker_seconds/parse_worker_seconds`。累计线程耗时不等于墙钟耗时。
- 本次实现新增每条记录的采集时预期模型 `expect`，非空时卡片显示；旧记录没有该字段时不猜测补齐。

## 全部观测字段

下表保留全部归一化字段路径，包括容器。出现/有值是该轮含此字段、含非空值的对象数量；数值 0 和布尔 false 都算有值。各字段采用最近一次出现该字段的样本轮次，覆盖率不能推断为总体请求覆盖率。`[]` 表示数组元素，`*` 表示动态键，`.$json` 表示协议字符串中的 JSON 结构。

### 响应对象：126 个字段路径

| 字段路径 | 类型 | 轮次 | 出现/有值/该轮对象数 |
|---|---|---:|---|
| `background` | bool | 4 | 1/1/1 |
| `completed_at` | int | 4 | 1/1/1 |
| `created_at` | int | 4 | 1/1/1 |
| `error` | null | 4 | 1/0/1 |
| `frequency_penalty` | float | 4 | 1/1/1 |
| `id` | str | 4 | 1/1/1 |
| `incomplete_details` | null | 4 | 1/0/1 |
| `instructions` | null | 4 | 1/0/1 |
| `max_output_tokens` | null | 4 | 1/0/1 |
| `max_tool_calls` | null | 4 | 1/0/1 |
| `metadata` | dict | 4 | 1/0/1 |
| `model` | str | 4 | 1/1/1 |
| `moderation` | null | 4 | 1/0/1 |
| `object` | str | 4 | 1/1/1 |
| `output` | list | 4 | 1/0/1 |
| `parallel_tool_calls` | bool | 4 | 1/1/1 |
| `presence_penalty` | float | 4 | 1/1/1 |
| `previous_response_id` | str | 4 | 1/1/1 |
| `prompt_cache_diagnostics` | dict | 4 | 1/1/1 |
| `prompt_cache_diagnostics.type` | str | 4 | 1/1/1 |
| `prompt_cache_key` | str | 4 | 1/1/1 |
| `prompt_cache_options` | dict | 4 | 1/1/1 |
| `prompt_cache_options.comparison_response_id` | str | 4 | 1/1/1 |
| `prompt_cache_options.mode` | str | 4 | 1/1/1 |
| `prompt_cache_options.ttl` | str | 4 | 1/1/1 |
| `prompt_cache_retention` | str | 4 | 1/1/1 |
| `reasoning` | dict | 4 | 1/1/1 |
| `reasoning.context` | str | 4 | 1/1/1 |
| `reasoning.effort` | str | 4 | 1/1/1 |
| `reasoning.mode` | str | 4 | 1/1/1 |
| `reasoning.summary` | str | 4 | 1/1/1 |
| `safety_identifier` | str | 4 | 1/1/1 |
| `service_tier` | str | 4 | 1/1/1 |
| `status` | str | 4 | 1/1/1 |
| `store` | bool | 4 | 1/1/1 |
| `temperature` | float | 4 | 1/1/1 |
| `text` | dict | 4 | 1/1/1 |
| `text.format` | dict | 4 | 1/1/1 |
| `text.format.type` | str | 4 | 1/1/1 |
| `text.verbosity` | str | 4 | 1/1/1 |
| `tool_choice` | str | 4 | 1/1/1 |
| `tool_usage` | dict | 4 | 1/1/1 |
| `tool_usage.image_gen` | dict | 4 | 1/1/1 |
| `tool_usage.image_gen.input_tokens` | int | 4 | 1/1/1 |
| `tool_usage.image_gen.input_tokens_details` | dict | 4 | 1/1/1 |
| `tool_usage.image_gen.input_tokens_details.image_tokens` | int | 4 | 1/1/1 |
| `tool_usage.image_gen.input_tokens_details.text_tokens` | int | 4 | 1/1/1 |
| `tool_usage.image_gen.output_tokens` | int | 4 | 1/1/1 |
| `tool_usage.image_gen.output_tokens_details` | dict | 4 | 1/1/1 |
| `tool_usage.image_gen.output_tokens_details.image_tokens` | int | 4 | 1/1/1 |
| `tool_usage.image_gen.output_tokens_details.text_tokens` | int | 4 | 1/1/1 |
| `tool_usage.image_gen.total_tokens` | int | 4 | 1/1/1 |
| `tool_usage.web_search` | dict | 4 | 1/1/1 |
| `tool_usage.web_search.num_requests` | int | 4 | 1/1/1 |
| `tools` | list | 4 | 1/1/1 |
| `tools[]` | dict | 4 | 1/1/1 |
| `tools[].description` | str | 4 | 1/1/1 |
| `tools[].name` | str | 4 | 1/1/1 |
| `tools[].tools` | list | 4 | 1/1/1 |
| `tools[].tools[]` | dict | 4 | 1/1/1 |
| `tools[].tools[].description` | str | 4 | 1/1/1 |
| `tools[].tools[].format` | dict | 4 | 1/1/1 |
| `tools[].tools[].format.definition` | str | 4 | 1/1/1 |
| `tools[].tools[].format.syntax` | str | 4 | 1/1/1 |
| `tools[].tools[].format.type` | str | 4 | 1/1/1 |
| `tools[].tools[].name` | str | 4 | 1/1/1 |
| `tools[].tools[].output_schema` | null | 4 | 1/0/1 |
| `tools[].tools[].parameters` | dict | 4 | 1/1/1 |
| `tools[].tools[].parameters.additionalProperties` | bool | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties` | dict | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*` | dict | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.description` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.encrypted` | bool | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items` | dict | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.additionalProperties` | bool | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties` | dict | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*` | dict | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.description` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.items` | dict | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.items.additionalProperties` | bool | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.items.properties` | dict | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.items.properties.*` | dict | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.items.properties.*.description` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.items.properties.*.type` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.items.required` | list | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.items.required[]` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.items.type` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.minItems` | int | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.properties.*.type` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.required` | list | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.required[]` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.items.type` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.minItems` | int | 4 | 1/1/1 |
| `tools[].tools[].parameters.properties.*.type` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.required` | list | 4 | 1/1/1 |
| `tools[].tools[].parameters.required[]` | str | 4 | 1/1/1 |
| `tools[].tools[].parameters.type` | str | 4 | 1/1/1 |
| `tools[].tools[].strict` | bool | 4 | 1/1/1 |
| `tools[].tools[].type` | str | 4 | 1/1/1 |
| `tools[].type` | str | 4 | 1/1/1 |
| `top_logprobs` | int | 4 | 1/1/1 |
| `top_p` | float | 4 | 1/1/1 |
| `truncation` | str | 4 | 1/1/1 |
| `usage` | dict | 4 | 1/1/1 |
| `usage.attribution` | dict | 4 | 1/1/1 |
| `usage.attribution.items` | dict | 4 | 1/1/1 |
| `usage.attribution.items.*` | dict | 4 | 1/1/1 |
| `usage.attribution.items.*.cache_write_tokens` | int | 4 | 1/1/1 |
| `usage.attribution.items.*.cached_tokens` | int | 4 | 1/1/1 |
| `usage.attribution.items.*.content` | list | 4 | 1/1/1 |
| `usage.attribution.items.*.content[]` | dict | 4 | 1/1/1 |
| `usage.attribution.items.*.content[].cache_write_tokens` | int | 4 | 1/1/1 |
| `usage.attribution.items.*.content[].cached_tokens` | int | 4 | 1/1/1 |
| `usage.attribution.items.*.content[].input_tokens` | int | 4 | 1/1/1 |
| `usage.attribution.items.*.content[].output_tokens` | int | 4 | 1/1/1 |
| `usage.attribution.items.*.input_tokens` | int | 4 | 1/1/1 |
| `usage.attribution.items.*.output_tokens` | int | 4 | 1/1/1 |
| `usage.input_tokens` | int | 4 | 1/1/1 |
| `usage.input_tokens_details` | dict | 4 | 1/1/1 |
| `usage.input_tokens_details.cache_write_tokens` | int | 4 | 1/1/1 |
| `usage.input_tokens_details.cached_tokens` | int | 4 | 1/1/1 |
| `usage.output_tokens` | int | 4 | 1/1/1 |
| `usage.output_tokens_details` | dict | 4 | 1/1/1 |
| `usage.output_tokens_details.reasoning_tokens` | int | 4 | 1/1/1 |
| `usage.total_tokens` | int | 4 | 1/1/1 |
| `user` | null | 4 | 1/0/1 |

### WebSocket 请求对象：87 个字段路径

| 字段路径 | 类型 | 轮次 | 出现/有值/该轮对象数 |
|---|---|---:|---|
| `client_metadata` | dict | 4 | 4/4/4 |
| `client_metadata.guardian_credits_requested` | str | 4 | 4/4/4 |
| `client_metadata.root_turn_id` | str | 4 | 4/4/4 |
| `client_metadata.session_id` | str | 4 | 4/4/4 |
| `client_metadata.thread_id` | str | 4 | 4/4/4 |
| `client_metadata.turn_id` | str | 4 | 4/4/4 |
| `client_metadata.ws_request_header_x_openai_internal_codex_responses_lite` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-installation-id` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json` | dict | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.agent_name` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.analytics_enabled` | bool | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.auto_review_enabled` | bool | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.context_window_id` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.installation_id` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.model` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.node_repl_auto_review_required` | bool | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.node_repl_disabled` | bool | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.reasoning_effort` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.request_kind` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.root_turn_id` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.sandbox` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.sandbox_mode` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.session_id` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.thread_id` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.thread_source` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.turn_id` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.turn_started_at_unix_ms` | int | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.turn_trigger` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.window_id` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.window_number` | int | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.workspace_kind` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.workspaces` | dict | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.workspaces.*` | dict | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.workspaces.*.has_changes` | bool | 4 | 4/4/4 |
| `client_metadata.x-codex-turn-metadata.$json.workspaces.*.latest_git_commit_hash` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-window-id` | str | 4 | 4/4/4 |
| `client_metadata.x-codex-ws-stream-request-start-ms` | str | 4 | 4/4/4 |
| `include` | list | 4 | 4/4/4 |
| `include[]` | str | 4 | 4/4/4 |
| `input` | list | 4 | 4/4/4 |
| `input[]` | dict | 4 | 4/4/4 |
| `input[].call_id` | str | 4 | 4/4/4 |
| `input[].content` | list | 2 | 2/2/5 |
| `input[].content[]` | dict | 2 | 2/2/5 |
| `input[].content[].text` | str | 2 | 2/2/5 |
| `input[].content[].type` | str | 2 | 2/2/5 |
| `input[].id` | str | 4 | 4/4/4 |
| `input[].internal_chat_message_metadata_passthrough` | dict | 4 | 4/4/4 |
| `input[].internal_chat_message_metadata_passthrough.cell_id` | str | 4 | 4/4/4 |
| `input[].internal_chat_message_metadata_passthrough.content_item_kinds` | list | 2 | 2/2/5 |
| `input[].internal_chat_message_metadata_passthrough.content_item_kinds[]` | str | 2 | 2/2/5 |
| `input[].internal_chat_message_metadata_passthrough.create_time` | float | 4 | 4/4/4 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls` | list | 4 | 4/4/4 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls[]` | dict | 4 | 4/4/4 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls[].arguments` | dict, str | 4 | 4/4/4 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls[].arguments._codex_executed_tool_call_truncated` | dict | 3 | 1/1/5 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls[].arguments._codex_executed_tool_call_truncated.max_bytes` | int | 3 | 1/1/5 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls[].arguments._codex_executed_tool_call_truncated.original_bytes` | int | 3 | 1/1/5 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls[].arguments.cmd` | str | 4 | 2/2/4 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls[].arguments.max_output_tokens` | int | 4 | 2/2/4 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls[].arguments.yield_time_ms` | int | 3 | 1/1/5 |
| `input[].internal_chat_message_metadata_passthrough.executed_tool_calls[].name` | str | 4 | 4/4/4 |
| `input[].internal_chat_message_metadata_passthrough.tool_calls_complete` | bool | 4 | 4/4/4 |
| `input[].internal_chat_message_metadata_passthrough.turn_id` | str | 4 | 4/4/4 |
| `input[].output` | list | 4 | 4/4/4 |
| `input[].output[]` | dict | 4 | 4/4/4 |
| `input[].output[].text` | str | 4 | 4/4/4 |
| `input[].output[].type` | str | 4 | 4/4/4 |
| `input[].role` | str | 2 | 2/2/5 |
| `input[].type` | str | 4 | 4/4/4 |
| `model` | str | 4 | 4/4/4 |
| `parallel_tool_calls` | bool | 4 | 4/4/4 |
| `previous_response_id` | str | 4 | 4/4/4 |
| `prompt_cache_key` | str | 4 | 4/4/4 |
| `reasoning` | dict | 4 | 4/4/4 |
| `reasoning.context` | str | 4 | 4/4/4 |
| `reasoning.effort` | str | 4 | 4/4/4 |
| `reasoning.summary` | str | 4 | 4/4/4 |
| `store` | bool | 4 | 4/4/4 |
| `stream` | bool | 4 | 4/4/4 |
| `stream_options` | dict | 4 | 4/4/4 |
| `stream_options.reasoning_summary_delivery` | str | 4 | 4/4/4 |
| `text` | dict | 4 | 4/4/4 |
| `text.verbosity` | str | 4 | 4/4/4 |
| `tool_choice` | str | 4 | 4/4/4 |
| `type` | str | 4 | 4/4/4 |

## 可复用的只读调查脚本（已移除）

> **2026-09-22：`tools/survey_fields.py` 已随 Python 采集器一起移除。** 它建立在 Python 只读内存读取器
> （`ProcessScanner`）之上，而该读取器与整个 Python 采集后端已下线；本文件上方的字段清单与样例保留为
> **当时的调查记录**，不再有对应工具可以复跑。

当时的用法（仅作记录，命令已不可运行）：`python -B tools/survey_fields.py --output _verify/field-survey.json`
每次只遍历一次当前发现的小写 `codex.exe` 引擎进程并输出聚合清单，不启动采集服务、不改配置或业务数据库。
`--examples` 模式只允许写入被 Git 忽略且未被跟踪的 `_verify` 路径，样例原值不截断；私有对照页面为
`_verify/FIELD_EXAMPLES.html`，完整值与来源为 `_verify/field-examples.json`，本文件保持不含真实样例。
