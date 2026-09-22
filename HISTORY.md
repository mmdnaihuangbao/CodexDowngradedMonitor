# 历史记录、分页与时间筛选

两个后端默认共用项目 `data/monitor.sqlite`。SQLite 使用 WAL；新增响应、状态变化、请求模型证据和采集日志均自动保存。每个完整响应 ID 对应一条最新记录，变化另存 `response_events`。重复扫描到相同状态不会生成一条新历史记录。

- 重启自动恢复历史、最近的活动缓存和最近 400 条日志。
- 内存最多缓存 3000 条响应；数据库不受这个上限影响，不自动删除旧记录。
- 浏览器调用 `GET /api/responses` 做服务端分页，默认 50 条，可切换 25/50/100 条、上一页/下一页、输入页码跳转。
- 开始/结束时间使用浏览器原生日历与时间控件，按本地时间输入，向后端传 Unix 秒。两端均包含在范围内；开始时间不能晚于结束时间。
- 筛选依据请求的 `created_at`；缺失或明显异常时使用首次采集时间。排序同样按此时间从新到旧，同秒以完整 ID 稳定排序。
- 搜索、判定筛选与时间范围一起应用；改变筛选回到第一页。SSE 更新不会主动把第二页等跳回第一页。
- 原“清空”改为“日志清屏”。`POST /api/clear` 也只清空当前日志显示，不删除请求或数据库；重启仍能恢复数据库中的日志。
- 进程停止前已提交的历史可以恢复，但不可能补回引入持久化之前已经丢失的内存数据。

## 数据库与启动

```powershell
python start.py
# 指定独立数据库（如需隔离某次运行）
python start.py --db D:\MonitorData\history.sqlite
```

指定 `--db` 不会改变正在运行实例的数据库，请先停止该实例后重新启动。

`data/` 已加入 Git 忽略。数据库包括模型、请求号、状态和时间等已提取元数据，不保存整个进程的原始内存或完整对话正文。备份运行中的 SQLite 请使用 SQLite backup 接口，或先正常停止服务后复制文件，不能仅复制运行中的主文件而忽略尚未合并的 WAL。

主要表：

| 表 | 内容 |
|---|---|
| `responses` | 每个响应的最新状态、时间索引和判定 |
| `response_events` | 捕获到的状态/证据变化历史 |
| `request_evidence` | 去重后的请求模型与前序响应证据（内存来源） |
| `request_model_index` | 旁路证据索引：`resp_id` / `prefix` 两类键 → 请求模型、effort、turn、来源；同一个键撞到第二个模型时置 `rejected` 并停止参与配对 |
| `evidence_state` | 旁路索引的增量游标（日志库文件名 + 行号；rollout 各文件的字节偏移与已见回合） |
| `collector_logs` | 采集运行日志 |
| `data_version` | 单行数据版本声明，当前文本值 `0.3`；启动时兼容升级 `0.1`/`0.2`，只新增表与版本声明、不重写历史分类，不等同于整数 `PRAGMA user_version` |

`request_model_index` 与 `evidence_state` 是 2026-09-22 引入的只读旁路证据索引（codex 自有日志 + 会话 rollout）；
它们必须落库，因为日志库会轮转，索引结果不能只留在内存里。

## 请求模型证据来源与延迟重判

`responses.req_model` 现在可能来自三条通道，来源存在 payload 的 `_evidence_source`，接口输出为 `evidence_source`，卡片显示成「证据来源」：

| `evidence_source` | 含义 |
|---|---|
| `memory_websocket` | 内存里 `响应.prev == 请求.previous_response_id` 配对（原有通道） |
| `codex_log_prefix` | codex 自有日志的响应号前缀索引（只读旁路） |
| `rollout_token_usage` | 会话 rollout 的 `token_usage_record.response_id`（只读旁路） |

配对状态（接口 `pairing_status`）在原有 `matched_previous_response_id` / `request_not_captured` /
`no_previous_response_id` / `ambiguous_previous_response_id` 之外新增三个：
`matched_response_id`（按响应号精确命中 rollout 记录）、`matched_response_id_prefix`
（与某个 output item 共享最长前缀而认领）、`ambiguous_response_id_prefix`
（共享最长的若干条目给出不同模型，因此不给请求模型）、`rejected_response_id`
（该响应号键曾撞到两个模型，已整键作废）。

索引键分两类：`resp_id`（rollout 的响应号）与 `item`（output item ID 前 26 位）。
item 键**不做固定长度截断**：那段请求前缀是递增计数器，同回合相邻请求常常只差最后 1–2 个 hex，
按固定长度取键会取到邻居请求的模型。查询时在同线程桶内比共享前缀长度、取最长的一条，
并列且模型不一致就不给答案。格式升级（`evidence_state.index_format`）会清掉旧键并重扫一次日志库。

**延迟重判**：证据晚到时，证据线程会把「还没有请求模型」的记录重新判一次（每轮上限 2000 条），
判出降级照常告警并写入 `response_events`；已经有请求模型的记录不在证据线程里改写。
旧记录缺少 `_expect` 时，只有两种情况允许被硬证据改写：原判 `incomplete` 且请求模型 == 响应模型 → `normal`；
请求模型 ≠ 响应模型 → `downgrade`。`normal`/`subtask` 的区分依赖采集时的预期模型，
历史记录缺这个字段时保持原判，不拿今天的配置重新解释。

新记录在 payload 中保存 `_expect`（首次采集时的预期模型，允许空字符串），接口输出为 `expect`，非空时卡片显示“采集时预期”。修改配置不会重新分类历史；后续补齐状态或请求证据仍使用该记录自己的预期模型。旧记录缺少 `_expect` 时保留原分类，不用当前配置补写未知的历史设置。版本表为增量创建，不重写已有响应和事件。

## 截图中的 actual / cross / 1970 是什么

它对应 `cpp_collector/tests/extract_tests.cpp` 的跨块测试对象 `resp_cross_boundary`：模型为 `actual`、`completed_at=100`、没有真实创建时间与状态。Unix 第 100 秒转换到 UTC+8 就是 `1970-01-01 08:01:40`。

本机旧 C++ 实例的快照中已确认存在同一个 ID。此前测试代码文字进入 Codex 进程内存后，内存解析启发式可能把它当成协议响应；这不是一次真实模型降级的证据。

新增“疑似样本”筛选：缺少有效创建时间/状态或时间明显异常的对象保留入库，但默认不在请求列表、正常/降级统计及告警中显示。打开“疑似样本”可检查原因。真实但字段不全的片段也可能被隔离，后续补齐字段后可回到正常列表；这不是绝对的真假识别，也无法阻止内容完全仿真的样本通过启发式检查。

## 未完成请求的颜色

`in_progress` 和 `queued` 为未完成状态：

- 正常-未完成、不完整-未完成、子任务-未完成：蓝色边线和标签。
- 已确认模型不匹配的降级-未完成：红色。
- 完成后恢复其正常/子任务/不完整/降级颜色。
- 疑似样本使用灰色虚线，与真实状态分开。

## 接口与验证

`GET /api/snapshot` 现在返回第一页及 `total/page/page_size/pages`，不再返回全库。完整历史通过分页 API 遍历。

```text
GET /api/responses?page=1&page_size=50&filter=all&q=&start=1700000000&end=1800000000
```

`filter` 可取 all、normal、subtask、incomplete、downgrade、suspect。服务器每页上限 200 条。

```powershell
python tests/test_history.py
python cpp_collector/tests/test_collectors.py
# 从仍在运行的旧版无持久化实例导入已有快照（不会删除数据库）
python tools/import_legacy_snapshot.py --port 48778
```

数据库测试覆盖 3105 条记录、缓存淘汰后的归档/补配、分页和时间边界、重启恢复、变化去重、样本隔离和双连接终态不回退。浏览器测试覆盖分页跳转、每页数量、时间范围、错误范围、搜索、复制、蓝/红状态及宽窄屏布局。
