"""只读旁路证据索引：codex 自有日志（响应号前缀）+ 会话 rollout（响应号）。

两条通道给出的都是**客户端请求侧**模型，与内存采集的 previous_response_id 配对互补：

* ``codex_log_prefix``   —— 响应 ID 与其 output item ID 共享前 23～25 位十六进制请求前缀，
  codex 把每个 output item 连同 tracing span 的 ``model=`` 一起写进 ``logs`` 表；
* ``rollout_token_usage`` —— rollout 的 ``token_usage_record.response_id`` → ``turn_id``
  → ``turn_context.model``，是逐请求的一对一映射。

边界（与 cpp_collector/REQUEST_MODEL_SOURCES.md 的 2026-09-22 补充一致）：

* 只读打开；不修改被观测进程、不改 Codex 配置、不代理流量、不进内存扫描器；
* 只提取 id / model / effort / turn / thread 五个字段，日志正文（feedback_log_body）绝不落库；
* 前缀是对服务端 ID 生成规则的逆向观察，不是官方契约：唯一性由 store.index_save 熔断兜底，
  同一前缀出现第二个模型即整键作废，此时宁可没有证据也不猜。
"""
import glob
import json
import os
import re
import sqlite3
import time

CODEX_HOME_ENV = "CODEX_HOME"
LOG_DB_NAME = "logs_2.sqlite"       # 找不到其它 logs*.sqlite 时的兜底文件名
LOG_DB_GLOB = "logs*.sqlite"        # 文件名里的 _2 说明会轮转，必须跟着最新的那个走
ROLLOUT_GLOBS = ("sessions/*/*/*/rollout-*.jsonl", "archived_sessions/*.jsonl")

# 与面板 SSE 心跳同频；轮询间隔是服务侧新增的等待策略，2 秒已与用户确认。
POLL_SECONDS = 2.0
# 索引格式版本：格式变了就得清掉旧键并整库重扫一次（见 collector.Monitor 里的迁移）。
INDEX_FORMAT = 2
# 响应 ID 与它自己产出的 item ID 共享一段"请求前缀"。实测该前缀长度不固定（22～26 位都有），
# 而且是递增计数器：同一回合内相邻请求常常只差最后 1～2 个 hex。因此不能按固定长度截断取键
# （那等于拿邻居请求的模型），改为保存 item ID 前 26 位，查询时在同一线程桶内比"共享前缀最长"。
ITEM_KEY_LEN = 26
ITEM_BUCKET_LEN = 20      # 同线程的 ID 前 20 位相同，用它分桶；跨线程共享 21 位在实际数据里不存在
MIN_ITEM_SHARE = 21       # 共享少于这个位数不足以认领，宁可没有证据
LOG_BATCH_ROWS = 20000          # 单轮最多处理的新日志行，避免首轮长时间占用写锁
ROLLOUT_TRACK_LIMIT = 400       # 只跟踪最近 N 个 rollout；更旧文件的条目早已落库
ROLLOUT_PENDING_LIMIT = 2000    # 文件内尚未见到对应 turn_context 的响应号暂存量上限

# item ID 的十六进制部分实测已到 50 位（响应 ID 仍是 48 位），放宽上限以免将来更长时被截断。
RE_ITEM_ID = re.compile(r"(?:rs|msg|fc|ctc|ctco|at)_([0-9a-f]{40,64})")
RE_MODEL = re.compile(r"[ =\"':]model[ =\"':]+([A-Za-z0-9][A-Za-z0-9._\-]{2,40})")
RE_TURN = re.compile(r"turn(?:\.id=|_id=)([0-9a-f\-]{36})")
RE_THREAD = re.compile(r"thread(?:\.id=|_id=)([0-9a-f\-]{36})")
RE_EFFORT = re.compile(r"reasoning_effort[ =\"':]+([a-z_]+)")


def codex_home():
    return os.environ.get(CODEX_HOME_ENV) or os.path.join(os.path.expanduser("~"), ".codex")


class CodexLogIndex:
    """从 codex 自有日志库建立「响应 ID 前缀 → 请求模型」索引。

    增量靠 ``logs.id`` 游标（落库），重启不重扫全表；日志会轮转，所以解析结果必须落库。
    轮转后 ``id`` 是新库自己的序列，因此文件名变化时游标要归零重扫。
    只跟踪最新修改的那个库：轮转发生时若本进程没在运行，那段窗口的日志不再回扫
    （此前扫到的条目已经落库，缺口由 rollout 通道兜）。
    """

    name = "codex_log_prefix"
    STATE_KEY = "codex_log_cursor"

    def __init__(self, home, store):
        self.home = home
        self.store = store
        # 兼容只有整型游标的旧状态（当时只认 logs_2.sqlite 一个固定文件名）。
        state = store.state_get(self.STATE_KEY, None)
        if state is None:
            legacy = store.state_get("codex_log_last_id", None)
            if isinstance(legacy, int):
                state = {"path": LOG_DB_NAME, "last_id": legacy}
        if isinstance(state, dict):
            self.db_name = state.get("path") or LOG_DB_NAME
            self.last_id = int(state.get("last_id") or 0)
        else:
            self.db_name = LOG_DB_NAME
            self.last_id = int(state or 0)
        self.rows_indexed = 0

    def _current_path(self):
        """返回当前正在写入的日志库：同名目录下最新修改的那个 logs*.sqlite。"""
        candidates = glob.glob(os.path.join(self.home, LOG_DB_GLOB))
        if not candidates:
            return None
        def mtime(path):
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0
        candidates.sort(key=lambda p: (mtime(p), p))
        return candidates[-1]

    def poll(self):
        """返回 (entries, note)。note 只含计数与错误，不含日志正文。"""
        path = self._current_path()
        if path is None:
            return [], {"missing": LOG_DB_GLOB}
        db_name = os.path.basename(path)
        cursor_before = (self.db_name, self.last_id)
        if db_name != self.db_name:
            # 日志轮转：新库的 id 从自己的 1 开始，沿用旧游标会漏掉开头一大段。
            self.db_name, self.last_id = db_name, 0
        try:
            # 每次轮询新建只读连接：短事务不占 WAL，也不影响 codex 自己的写入。
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as exc:
            return [], {"error": f"打开失败: {exc}"}
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT id,feedback_log_body FROM logs WHERE id>? "
                               "ORDER BY id LIMIT ?", (self.last_id, LOG_BATCH_ROWS)).fetchall()
        except sqlite3.Error as exc:
            return [], {"error": f"读取失败: {exc}"}
        finally:
            con.close()

        entries, newest = [], self.last_id
        for row in rows:
            newest = row["id"]
            body = row["feedback_log_body"]
            if not body or "item_id" not in body:
                continue
            items = RE_ITEM_ID.findall(body)
            if not items:
                continue
            models = {m for m in RE_MODEL.findall(body) if not m.startswith("resp_")}
            if len(models) != 1:
                # 一行里出现多个模型 token 说明这一行的抽取不可靠，整行跳过（不猜）。
                continue
            model = models.pop()
            turn = RE_TURN.search(body)
            thread = RE_THREAD.search(body)
            effort = RE_EFFORT.search(body)
            for item in items:
                # 一个 item 只留一条：完整的共享前缀在查询时才能算出来。
                entries.append({
                    "key_kind": "item", "key_value": item[:ITEM_KEY_LEN], "model": model,
                    "effort": effort.group(1) if effort else None,
                    "turn_id": turn.group(1) if turn else None,
                    "thread_id": thread.group(1) if thread else None,
                    "source": self.name,
                })
        self.rows_indexed += len(rows)
        if newest != self.last_id:
            self.last_id = newest
        # 只在游标真的变了才落库：轮询是常驻的，没变化也写一次等于白刷 WAL。
        if (self.db_name, self.last_id) != cursor_before:
            self.store.state_set(self.STATE_KEY, {"path": self.db_name, "last_id": self.last_id})
        return _dedupe(entries, "key_value"), {"rows": len(rows), "last_id": self.last_id,
                                               "db": self.db_name}


class RolloutIndex:
    """尾随 .codex/sessions/**/rollout-*.jsonl，建立「响应号 → 该回合请求模型」索引。

    rollout 是追加写文件：按（文件, 字节偏移）续读，偏移与已见回合一起落库，
    重启后既不重扫历史字节，也不会因为 turn_context 落在偏移之前而丢模型。
    """

    name = "rollout_token_usage"
    STATE_KEY = "rollout_files"

    def __init__(self, home, store):
        self.home = home
        self.store = store
        self.files = store.state_get(self.STATE_KEY, {}) or {}
        self.files_indexed = 0

    def _paths(self):
        paths = []
        for pattern in ROLLOUT_GLOBS:
            paths.extend(glob.glob(os.path.join(self.home, pattern)))
        def mtime(path):
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0
        paths.sort(key=mtime, reverse=True)
        return paths[:ROLLOUT_TRACK_LIMIT]

    def poll(self):
        entries, state, touched = [], {}, 0
        for path in self._paths():
            try:
                size = os.stat(path).st_size
            except OSError:
                continue
            prev = self.files.get(path) or {}
            offset = int(prev.get("offset", 0))
            turns = dict(prev.get("turns") or {})
            pending = [list(item) for item in (prev.get("pending") or [])]
            if offset > size:
                # 文件被重写或迁移过：从头重扫，避免偏移落在别的会话内容中间。
                offset, turns, pending = 0, {}, []
            if offset >= size and not pending:
                state[path] = {"offset": offset, "turns": turns}
                continue
            try:
                pending, found, offset = _read_rollout(path, offset, turns, pending)
            except OSError as exc:
                state[path] = {"offset": offset, "turns": turns, "pending": pending,
                               "error": str(exc)[:120]}
                continue
            entries.extend(found)
            touched += 1
            self.files_indexed += 1
            state[path] = {"offset": offset, "turns": turns,
                           "pending": pending[:ROLLOUT_PENDING_LIMIT]}
        if state != self.files:
            self.files = state
            self.store.state_set(self.STATE_KEY, state)
        return _dedupe(entries, "key_value"), {"files_read": touched,
                                               "files_tracked": len(state),
                                               "new": len(entries)}


def _read_rollout(path, offset, turns, pending):
    """从 offset 续读完整行；半行不消费（文件正被 codex 追加），留到下一轮。"""
    entries = []
    session_id = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line:
                break
            if not line.endswith("\n"):
                break
            offset = fh.tell()
            try:
                if '"turn_context"' in line:
                    payload = (json.loads(line).get("payload") or {})
                    turn_id = payload.get("turn_id")
                    if turn_id:
                        turns[turn_id] = {"model": payload.get("model"),
                                          "effort": payload.get("effort")}
                elif '"session_meta"' in line:
                    session_id = (json.loads(line).get("payload") or {}).get("session_id")
                elif '"response_id"' in line:
                    payload = (json.loads(line).get("payload") or {})
                    response_id = payload.get("response_id")
                    if response_id:
                        pending.append([response_id, payload.get("turn_id"),
                                        payload.get("thread_id") or session_id])
            except ValueError:
                # 单行 JSON 坏掉只跳过这一行；rollout 允许被并发追加。
                continue

    still_pending = []
    for response_id, turn_id, thread_id in pending:
        turn = turns.get(turn_id)
        if turn and turn.get("model"):
            entries.append({"key_kind": "resp_id", "key_value": response_id,
                            "model": turn["model"], "effort": turn.get("effort"),
                            "turn_id": turn_id, "thread_id": thread_id,
                            "source": RolloutIndex.name})
        else:
            still_pending.append([response_id, turn_id, thread_id])
    return still_pending, entries, offset


def _dedupe(entries, field):
    """同键合并。

    同一轮里同一个键出现第二个不同模型时按熔断处理：标成作废并记录冲突模型，
    不能让"后出现的那条"覆盖先出现的 —— 那等于把冲突悄悄变成"最后一次写入赢"。
    """
    unique = {}
    for entry in entries:
        key = (entry["key_kind"], entry[field])
        previous = unique.get(key)
        if previous is None:
            unique[key] = entry
        elif previous["model"] != entry["model"]:
            unique[key] = {**previous, "rejected": 1, "conflict_model": entry["model"]}
    return list(unique.values())


def common_prefix_length(left, right):
    """两个十六进制 ID 的共享前缀长度。"""
    length = 0
    for a, b in zip(left, right):
        if a != b:
            break
        length += 1
    return length


class EvidenceIndex:
    """两条只读通道的调度器：各自独立容错，一条挂了不影响另一条与主采集。"""

    def __init__(self, store, home=None, poll_seconds=POLL_SECONDS):
        self.store = store
        self.home = home or codex_home()
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.log = CodexLogIndex(self.home, store)
        self.rollout = RolloutIndex(self.home, store)
        self.sources = (self.log, self.rollout)
        self.polls = 0
        self.last_poll = None
        self.last_error = None
        self.last_counts = {}

    def poll(self):
        """返回 (entries, counts)。entries 由调用方落库并装入内存。"""
        entries, counts = [], {}
        for source in self.sources:
            try:
                found, note = source.poll()
            except Exception as exc:  # 单条通道失败不能影响主采集
                self.last_error = f"{source.name}: {exc}"
                counts[source.name] = {"error": str(exc)[:200]}
                continue
            entries.extend(found)
            counts[source.name] = note
        self.polls += 1
        self.last_poll = time.time()
        self.last_counts = counts
        return _dedupe(entries, "key_value"), counts

    def describe(self):
        return {
            "enabled": True,
            "codex_home": self.home,
            "poll_seconds": self.poll_seconds,
            "polls": self.polls,
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "log_rows_indexed": self.log.rows_indexed,
            "log_db": self.log.db_name,
            "log_last_id": self.log.last_id,
            "rollout_files_tracked": len(self.rollout.files),
            "sources": self.last_counts,
        }


if __name__ == "__main__":
    # 只读自检：用内存库跑一遍索引，打印计数，不写任何项目数据。
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from history_store import HistoryStore
    store = HistoryStore(":memory:")
    index = EvidenceIndex(store)
    started = time.time()
    found, counts = index.poll()
    conflict = store.index_save(found)
    stats = store.index_stats()
    print(json.dumps({
        "home": index.home,
        "seconds": round(time.time() - started, 2),
        "entries": len(found),
        "counts": counts,
        "conflicts": len(conflict),
        "stats": stats,
        "second_poll_entries": len(index.poll()[0]),
    }, ensure_ascii=False, indent=2))
    store.close()
