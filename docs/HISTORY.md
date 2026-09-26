# 历史记录、分页与时间筛选

v0.5 的 Rust 服务直接使用 EXE 旁的 `data/monitor.sqlite`，SQLite 使用 WAL。从 v0.4 升级时先正常关闭旧实例，再将 `config.json` 和整个 `data/` 复制到新发布包，无需导出、转换或导入。

## 保存与恢复

- 每个完整响应 ID 保留一条最新记录，变化另存 `response_events`。重复扫描相同状态不产生新事件。
- 重启恢复数据库、证据索引、游标和最近 400 条采集日志。历史数量没有 3000 条上限，不自动删除旧记录。
- 所有归档查询直接访问 SQLite。后到证据也能修复很早的记录；完整归档按每轮 500 条遍历，证据冲突可撤销先前的配对和告警。
- `/api/clear` 只清当前日志显示，不删响应或数据库；重启可重新读取数据库中的日志。
- 数据库只保存提取的元数据，不保存原始进程内存或完整对话正文。运行中的数据库应通过 SQLite backup 接口备份，或正常关闭后复制整个目录，不能遗漏未合并的 WAL。
- 不支持将输出切换到用户目录或任意 `--db` 路径。启动、停止分别使用发布包的 `start.bat`、`stop.bat`。

## 格式兼容

| 表 | 内容 |
|---|---|
| `responses` | 最新状态、时间索引、判定和原格式 payload |
| `response_events` | 状态或证据变化历史 |
| `request_evidence` | 请求模型与前序响应证据；SHA-256 指纹规则不变 |
| `request_model_index` | `resp_id` / `item` 键对应请求模型、effort、turn、来源和冲突标记 |
| `evidence_state` | 日志库文件名与行号、rollout 字节偏移、已见回合及待配对项 |
| `collector_logs` | 采集运行日志 |
| `data_version` | 文本版本 `0.3` |

配置版本仍为 `0.1`，证据索引格式仍为 `3`，`PRAGMA user_version` 仍为 `1`。现有格式 3 的索引和游标不会因 Rust 重构重置。更早格式沿用 v0.4 的升级规则：移除旧 `prefix` 键，按原版本重置相关源游标以重新读取证据；该过程使用事务。

## 配对、重判与完成态

请求模型按以下顺序解析：内存 `previous_response_id` 配对、rollout 响应号精确匹配、日志 output item 最长共享前缀。来源分别为 `memory_websocket`、`rollout_token_usage`、`codex_log_prefix`。

前缀不能固定截断后直接认领：相邻请求可能只差末尾字符。共享长度相同却给出不同模型时保留歧义；精确键出现模型冲突后持久标记作废，不能用最后一条覆盖。

新记录保存首次采集时的 `_expect`，修改当前配置不重解释历史。旧记录缺少 `_expect` 时，不补写当前配置；是否能依据新硬证据修改原判定继续遵循旧版规则。

证据线程每 2 秒对最多 1000 条未完成记录对账：rollout 记账证明请求已结束时，可采用记账时间补齐 `completed_at`；存在后继响应引用时只补完成状态，不伪造完成时间。没有这些证据时保留原状态，已完成状态不倒退。

缺少有效时间或状态的疑似样本仍保存，但默认不进入常规列表、统计和告警。“疑似样本”筛选用于检查原因。内存里的仿真文本可能通过启发式，模型字段也不能直接证明模型实际能力。

## 分页与界面

```text
GET /api/responses?page=1&page_size=50&filter=all&q=&start=1700000000&end=1800000000
```

默认每页 50 条，上限 200；`filter` 支持 all、normal、subtask、incomplete、downgrade、suspect。搜索、判定和时间筛选叠加使用，时间上下界均包含，开始晚于结束时返回错误。排序使用有效创建时间，否则采用首次观测时间，同秒按完整 ID 稳定排序。

SSE 首帧是 snapshot，之后发送 patch；更新不会把第二页强行跳回第一页。进行中和排队中的普通记录显示蓝色，确认模型不匹配的降级记录显示红色，疑似样本使用灰色虚线。

## 验证

```powershell
./src/tools/build.ps1 -Tests
./src/tools/test.ps1 -PerformanceRuns 20
```

早期 C++ 内存字段观测属于历史调查，见 [字段调查](FIELD_SURVEY.md)。
