# 随心记 Memory V2 第二轮加固方案

## 一、本次要做什么

本次不继续增加新的记忆类型，也不接入 LLM 记忆提取器。

本轮只处理 Memory V2 当前最关键的工程问题：

```text
1. 一条笔记的记忆提取是否真正完成，目前没有独立状态
2. SQLite 多线程并发写入可能出现 database is locked
3. daily / weekly / monthly consolidation 重启后可能重复执行
4. Memory 检索没有最低分数，低相关记忆可能进入回答上下文
5. README 和测试文档已经落后于当前代码
```

本次修改完成后，Memory V2 应具备：

```text
可恢复
可重试
不重复
可观测
并发更稳定
```

---

# 二、本次修改范围

## 必修项

```text
P1：增加 memory_extraction_states
P2：SQLite WAL、busy_timeout 和有限重试
P3：增加 memory_consolidation_runs
```

## 顺手完成

```text
P4：增加 MEMORY_QUERY_MIN_SCORE
P5：同步 README、DESIGN、tests/README
P6：补充对应测试和评测
```

本轮暂时不做：

```text
LLM 记忆提取器
Memory Embedding 全量接入
多进程部署
分布式锁
新的飞书 UI
截图和 GIF
```

---

# 三、问题一：没有笔记级记忆提取状态

## 3.1 当前问题

当前 Worker 保存笔记后执行：

```text
save note
→ save vector
→ process_note_memory
→ mark WAL processed
```

如果记忆处理失败：

```text
笔记已经保存
→ Memory 提取失败
→ WAL 仍然被标记 processed
```

这本身可以接受，因为不能让 Memory V2 阻塞基础归档。

问题在于系统没有独立记录：

```text
这条笔记到底有没有完成记忆提取
```

目前 daily consolidation 主要通过：

```python
note_has_memory(note_id)
```

判断是否处理过。

这个判断不够准确。

---

## 3.2 两种错误情况

### 情况一：部分成功

一条笔记提取出两个候选：

```text
candidate A 保存成功
candidate B 保存失败
```

此时已经存在一条 `memory_sources`，所以：

```python
note_has_memory(note_id) == True
```

后续 daily consolidation 会跳过这条笔记。

结果：

```text
candidate B 永远不会恢复
```

### 情况二：正常零候选

例如：

```text
你好
收到
好的
```

提取器正常返回：

```text
0 个候选
```

因为没有写入 `memory_sources`，系统会认为从未处理。

结果：

```text
每天重复提取
```

以后接入 LLM 后，会产生重复费用。

---

# 四、P1：增加 memory_extraction_states

## 4.1 新增数据库表

在 `memory/repository.py` 的 `init_db()` 中增加：

```sql
CREATE TABLE IF NOT EXISTS memory_extraction_states (
    note_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_extraction_status
ON memory_extraction_states(space_id, status, updated_at);
```

---

## 4.2 状态定义

```text
pending
processing
completed
empty
partial
failed
```

含义：

| 状态 | 含义 |
|---|---|
| pending | 已登记，但尚未开始 |
| processing | 正在提取或合并 |
| completed | 所有候选都成功处理 |
| empty | 正常完成，但没有需要保存的候选 |
| partial | 部分候选成功，部分失败 |
| failed | 整体失败，没有可靠完成 |

---

## 4.3 增加模型

建议在 `memory/models.py` 中增加：

```python
MEMORY_EXTRACTION_STATUSES = {
    "pending",
    "processing",
    "completed",
    "empty",
    "partial",
    "failed",
}
```

可增加：

```python
@dataclass(frozen=True)
class MemoryExtractionState:
    note_id: str
    space_id: str
    status: str
    candidate_count: int
    processed_count: int
    attempt_count: int
    last_error: str | None
    started_at: str | None
    completed_at: str | None
    updated_at: str
```

---

## 4.4 增加 Repository API

在 `memory/repository.py` 增加：

```python
def get_extraction_state(
    note_id: str,
    db_path=None,
) -> MemoryExtractionState | None:
    ...
```

```python
def mark_extraction_processing(
    note_id: str,
    space_id: str,
    db_path=None,
) -> MemoryExtractionState:
    ...
```

```python
def mark_extraction_completed(
    note_id: str,
    space_id: str,
    *,
    candidate_count: int,
    processed_count: int,
    db_path=None,
) -> MemoryExtractionState:
    ...
```

```python
def mark_extraction_empty(
    note_id: str,
    space_id: str,
    db_path=None,
) -> MemoryExtractionState:
    ...
```

```python
def mark_extraction_partial(
    note_id: str,
    space_id: str,
    *,
    candidate_count: int,
    processed_count: int,
    error: str,
    db_path=None,
) -> MemoryExtractionState:
    ...
```

```python
def mark_extraction_failed(
    note_id: str,
    space_id: str,
    *,
    error: str,
    db_path=None,
) -> MemoryExtractionState:
    ...
```

```python
def list_retryable_extraction_states(
    space_id: str,
    *,
    limit: int = 100,
    db_path=None,
) -> list[MemoryExtractionState]:
    ...
```

重试状态：

```text
pending
failed
partial
```

---

# 五、修改 process_note_memory

文件：

```text
memory/service.py
```

## 5.1 推荐流程

```text
开始
→ mark processing
→ extract candidates
→ 逐个 consolidate
→ 根据结果写 completed / empty / partial
→ trace finished
```

推荐伪代码：

```python
def process_note_memory(
    note,
    classification=None,
) -> dict[str, Any]:
    note_id = ...
    space_id = ...
    text = ...

    mark_extraction_processing(
        note_id,
        space_id,
    )

    trace = start_trace(
        "memory_write",
        space_id,
        note_id=note_id,
    )

    try:
        candidates = extract_candidates(
            note_id,
            text,
            classification=classification,
        )

        if not candidates:
            mark_extraction_empty(
                note_id,
                space_id,
            )
            finish_trace(trace)

            return {
                "note_id": note_id,
                "space_id": space_id,
                "candidates": 0,
                "results": [],
                "trace_id": trace["trace_id"],
                "extraction_status": "empty",
            }

        results = []
        errors = []

        for candidate in candidates:
            try:
                result = consolidate_candidate(
                    space_id,
                    note_id,
                    candidate,
                    trace=trace,
                )
                results.append(result)
            except Exception as exc:
                errors.append(
                    f"{type(exc).__name__}: {exc}"
                )

        if errors and results:
            mark_extraction_partial(
                note_id,
                space_id,
                candidate_count=len(candidates),
                processed_count=len(results),
                error="; ".join(errors),
            )
            finish_trace(
                trace,
                status="partial",
            )

            return {
                "note_id": note_id,
                "space_id": space_id,
                "candidates": len(candidates),
                "results": results,
                "errors": errors,
                "trace_id": trace["trace_id"],
                "extraction_status": "partial",
            }

        if errors:
            error = "; ".join(errors)

            mark_extraction_failed(
                note_id,
                space_id,
                error=error,
            )

            finish_trace(
                trace,
                status="failed",
            )

            raise RuntimeError(error)

        mark_extraction_completed(
            note_id,
            space_id,
            candidate_count=len(candidates),
            processed_count=len(results),
        )

        finish_trace(trace)

        return {
            "note_id": note_id,
            "space_id": space_id,
            "candidates": len(candidates),
            "results": results,
            "trace_id": trace["trace_id"],
            "extraction_status": "completed",
        }

    except Exception as exc:
        current = get_extraction_state(note_id)

        if current is None or current.status == "processing":
            mark_extraction_failed(
                note_id,
                space_id,
                error=f"{type(exc).__name__}: {exc}",
            )

        add_step(
            trace,
            "memory_write_failed",
            status="failed",
            error=str(exc),
        )
        finish_trace(
            trace,
            status="failed",
        )
        raise
```

---

## 5.2 注意事项

不要因为单个候选失败，就丢掉已经成功处理的候选。

不要在 partial 后直接标记 completed。

不要通过 `memory_sources` 推断处理状态。

---

# 六、修改 Worker 恢复逻辑

文件：

```text
core/worker.py
```

## 6.1 当前问题

当前发现笔记已经存在时，只补向量：

```text
note exists
→ backfill vector
→ mark processed
```

应增加 Memory 状态检查。

---

## 6.2 推荐流程

```python
if note_exists(space_id, message_id):
    note = _find_note_by_message_id(
        space_id,
        message_id,
    )

    added_vector = backfill_vector_if_missing(
        space_id,
        message_id,
    )

    if note is not None:
        state = get_extraction_state(
            str(note.get("id") or ""),
        )

        if state is None or state.status in {
            "pending",
            "failed",
            "partial",
        }:
            try:
                process_note_memory(note)
            except Exception:
                LOGGER.exception(
                    "Memory recovery failed"
                )

    mark_processed(
        space_id,
        record_id,
    )
    return
```

但注意：

```text
Worker 重跑时不应该无限同步重试
```

建议只做一次恢复尝试。

---

# 七、修改 daily consolidation

文件：

```text
memory/consolidator.py
```

## 7.1 删除旧判断

不要继续使用：

```python
if note_has_memory(note_id):
    skip
```

改为读取：

```python
state = get_extraction_state(note_id)
```

处理规则：

```text
completed → skip
empty → skip
processing 且未超时 → skip
pending → process
failed → process
partial → process
没有状态 → process
```

---

## 7.2 processing 超时恢复

增加配置：

```text
SUIXINJI_MEMORY_EXTRACTION_LEASE_SECONDS=600
```

如果：

```text
status = processing
updated_at 已超过 10 分钟
```

视为 stale processing：

```text
processing → failed
→ 允许 daily 重试
```

避免程序崩溃后永远卡在 processing。

---

# 八、P2：SQLite 并发加固

## 8.1 当前风险

不同 `space_id` 的 Worker 可以并发执行。

Memory V2 共用：

```text
data/memory/memory.db
```

多个线程同时写入时可能出现：

```text
sqlite3.OperationalError: database is locked
```

---

## 8.2 修改连接配置

文件：

```text
memory/repository.py
```

当前：

```python
conn = sqlite3.connect(path)
```

建议改为：

```python
conn = sqlite3.connect(
    path,
    timeout=10,
)

conn.row_factory = sqlite3.Row

conn.execute(
    "PRAGMA foreign_keys = ON"
)
conn.execute(
    "PRAGMA journal_mode = WAL"
)
conn.execute(
    "PRAGMA busy_timeout = 10000"
)
conn.execute(
    "PRAGMA synchronous = NORMAL"
)
```

---

## 8.3 增加统一事务执行器

建议增加：

```python
def _is_locked_error(exc: Exception) -> bool:
    return (
        isinstance(exc, sqlite3.OperationalError)
        and "locked" in str(exc).casefold()
    )
```

```python
def _run_write(
    operation,
    *,
    max_attempts: int = 3,
):
    delay = 0.05

    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if (
                not _is_locked_error(exc)
                or attempt >= max_attempts
            ):
                raise

            time.sleep(delay)
            delay *= 2
```

重试范围只针对：

```text
database is locked
database table is locked
```

不要对所有异常重试。

---

## 8.4 哪些函数需要接入

至少包括：

```text
insert_memory
add_source
update_memory
mark_accessed
purge_memory
写 extraction state
写 consolidation run
```

---

## 8.5 配置项

在 `core/settings.py` 增加：

```python
MEMORY_DB_BUSY_TIMEOUT_MS = _int_env(
    "SUIXINJI_MEMORY_DB_BUSY_TIMEOUT_MS",
    10000,
)

MEMORY_DB_WRITE_MAX_ATTEMPTS = _int_env(
    "SUIXINJI_MEMORY_DB_WRITE_MAX_ATTEMPTS",
    3,
)
```

在 `.env.example` 增加：

```env
SUIXINJI_MEMORY_DB_BUSY_TIMEOUT_MS=10000
SUIXINJI_MEMORY_DB_WRITE_MAX_ATTEMPTS=3
```

---

# 九、P3：Consolidation 持久化幂等

## 9.1 当前问题

Memory Scheduler 的运行状态只保存在内存：

```python
last_run_dates = {}
```

程序重启后状态清空。

可能出现：

```text
daily 已执行
→ 程序重启
→ daily 再执行

monthly 已执行
→ 每月 1 日程序重启
→ monthly 再创建稳定语义记忆
```

---

## 9.2 新增数据库表

```sql
CREATE TABLE IF NOT EXISTS memory_consolidation_runs (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    cadence TEXT NOT NULL,
    period_key TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    result_json TEXT,
    UNIQUE(space_id, cadence, period_key)
);

CREATE INDEX IF NOT EXISTS idx_memory_consolidation_status
ON memory_consolidation_runs(
    cadence,
    period_key,
    status
);
```

---

## 9.3 period_key 规则

```text
daily   → 2026-07-14
weekly  → 2026-W29
monthly → 2026-07
```

增加：

```python
def consolidation_period_key(
    cadence: str,
    day: date,
) -> str:
    ...
```

---

## 9.4 状态

```text
running
completed
failed
```

---

## 9.5 Repository API

增加：

```python
def reserve_consolidation_run(
    space_id: str,
    cadence: str,
    period_key: str,
    db_path=None,
) -> ConsolidationRun | None:
    ...
```

规则：

```text
不存在 → 创建 running，返回记录
completed → 返回 None
running 且未过期 → 返回 None
failed → 允许重新 reserve
running 已过期 → 改 failed，再重新 reserve
```

增加：

```python
def mark_consolidation_completed(
    run_id: str,
    result: dict[str, Any],
    db_path=None,
) -> None:
    ...
```

```python
def mark_consolidation_failed(
    run_id: str,
    error: str,
    db_path=None,
) -> None:
    ...
```

---

## 9.6 修改 Scheduler

文件：

```text
memory/scheduler.py
```

不要再只依赖：

```python
last_run_dates
```

新的流程：

```text
列出 space_ids
→ 为每个 space 计算 period_key
→ reserve consolidation run
→ reserve 失败则跳过
→ 执行 consolidation
→ completed / failed
```

推荐：

```python
def run_memory_consolidation_once(
    cadence: str,
    *,
    space_ids: list[str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    current_day = today or date.today()
    period_key = consolidation_period_key(
        cadence,
        current_day,
    )

    targets = space_ids or list_memory_space_ids()
    results = []

    for space_id in targets:
        run = reserve_consolidation_run(
            space_id,
            cadence,
            period_key,
        )

        if run is None:
            results.append({
                "space_id": space_id,
                "cadence": cadence,
                "period_key": period_key,
                "status": "skipped",
                "reason": "already_reserved_or_completed",
            })
            continue

        try:
            result = run_memory_consolidation(
                space_id,
                cadence,
            )

            mark_consolidation_completed(
                run.id,
                result,
            )

            results.append(result)

        except Exception as exc:
            mark_consolidation_failed(
                run.id,
                f"{type(exc).__name__}: {exc}",
            )

            results.append({
                "space_id": space_id,
                "cadence": cadence,
                "period_key": period_key,
                "status": "failed",
                "error": str(exc),
            })
```

---

# 十、P4：增加 Memory 检索最低分数

## 10.1 当前问题

当前 Memory 检索只要：

```text
score > 0
```

就可能返回。

字符重叠很少的低相关记忆，也可能进入 `/ask` 上下文。

---

## 10.2 增加配置

在 `core/settings.py` 增加：

```python
MEMORY_QUERY_MIN_SCORE = _float_env(
    "SUIXINJI_MEMORY_QUERY_MIN_SCORE",
    0.45,
)
```

`.env.example`：

```env
SUIXINJI_MEMORY_QUERY_MIN_SCORE=0.45
```

---

## 10.3 修改 repository.search_memories

当前：

```python
scored = [
    (memory, score)
    for memory, score in scored
    if score > 0
]
```

建议：

```python
def search_memories(
    space_id: str,
    query: str,
    *,
    memory_type: str | None = None,
    include_inactive: bool = False,
    min_score: float = MEMORY_QUERY_MIN_SCORE,
    limit: int = 10,
    mark_access: bool = True,
    db_path=None,
):
    ...
```

筛选：

```python
scored = [
    (memory, score)
    for memory, score in scored
    if score >= min_score
]
```

---

## 10.4 修改 memory_search

文件：

```text
memory/service.py
```

增加：

```python
def memory_search(
    space_id: str,
    query: str,
    *,
    memory_type: str | None = None,
    min_score: float = MEMORY_QUERY_MIN_SCORE,
    limit: int = 8,
):
    ...
```

ReAct 工具参数也允许传：

```text
min_score
```

---

# 十一、Trace 和可观测性

## 11.1 Extraction Trace 增加

增加步骤：

```text
extraction_state_processing
extraction_state_completed
extraction_state_empty
extraction_state_partial
extraction_state_failed
```

不要记录完整原文。

只记录：

```text
note_id
candidate_count
processed_count
attempt_count
error_type
```

---

## 11.2 Consolidation Trace / 日志

增加结构化日志：

```text
memory.consolidation.reserve
memory.consolidation.run
memory.consolidation.completed
memory.consolidation.failed
memory.consolidation.skipped
```

字段：

```text
space_id
cadence
period_key
run_id
status
duration_ms
error
```

---

## 11.3 `/memory stats`

建议增加：

```text
extraction_by_status
consolidation_last_runs
retryable_extraction_count
```

示例：

```text
记忆统计：
total=24
active=18
conflicted=2
extraction={completed: 20, empty: 8, failed: 1, partial: 1}
retryable=2
```

---

# 十二、测试方案

## 12.1 Extraction State 测试

新增：

```text
tests/test_memory_extraction_state.py
```

必须覆盖：

```text
正常有候选 → completed
正常零候选 → empty
整体失败 → failed
部分候选失败 → partial
attempt_count 会增长
completed 不会被 daily 重复处理
empty 不会被 daily 重复处理
failed 会被 daily 重试
partial 会被 daily 重试
stale processing 会恢复为 failed
```

---

## 12.2 Worker 恢复测试

新增或扩展：

```text
tests/2阶段测试/test_worker_flow.py
```

场景：

```text
笔记已经存在
向量已经存在
extraction state = failed
```

断言：

```text
Worker 会再次调用 process_note_memory
基础笔记不会重复保存
WAL 最终标记 processed
```

---

## 12.3 SQLite 并发测试

新增：

```text
tests/test_memory_sqlite_concurrency.py
```

建议使用：

```python
ThreadPoolExecutor(max_workers=8)
```

同时执行：

```text
不同 space_id 插入记忆
同一 space_id 添加来源
更新 task 状态
```

断言：

```text
没有 database is locked
记录数量正确
sources 不丢失
versions 正确
```

---

## 12.4 SQLite 锁重试测试

通过 monkeypatch 模拟：

```text
第一次、第二次抛 database is locked
第三次成功
```

断言：

```text
执行 3 次
最终成功
```

再测试：

```text
普通 ValueError
```

断言：

```text
只执行 1 次
不重试
```

---

## 12.5 Consolidation 幂等测试

新增：

```text
tests/test_memory_consolidation_runs.py
```

覆盖：

```text
同一 daily period 只执行一次
同一 weekly period 只执行一次
同一 monthly period 只执行一次
程序重启后仍然跳过 completed
failed 可以重试
running 未过期不能重复执行
running 过期后可以恢复
不同 space_id 独立执行
不同 period_key 可以执行
```

---

## 12.6 Memory 检索阈值测试

覆盖：

```text
低于 min_score 不返回
达到 min_score 返回
自定义 min_score 生效
低相关记忆不进入 /ask observations
```

---

# 十三、评测更新

## 13.1 Memory Evaluation 增加指标

在 `eval/eval_memory.py` 增加：

```text
extraction_recovery_rate
empty_reprocessing_rate
partial_recovery_rate
consolidation_duplicate_rate
sqlite_write_success_rate
low_relevance_filter_rate
```

---

## 13.2 目标

```text
extraction_recovery_rate = 1.0
empty_reprocessing_rate = 0.0
partial_recovery_rate = 1.0
consolidation_duplicate_rate = 0.0
sqlite_write_success_rate = 1.0
low_relevance_filter_rate >= 0.9
```

---

# 十四、文档修改

## README.md

修正：

```text
“四条 dry-run”
```

为：

```text
“五条 dry-run”
```

增加：

```text
Memory extraction state
SQLite WAL 并发策略
Consolidation 持久化幂等
Memory 检索阈值
```

---

## DESIGN.md

增加完整流程：

```text
note saved
→ extraction state = processing
→ extract candidates
→ consolidate
→ completed / empty / partial / failed
```

增加：

```text
consolidation reserve
→ run
→ completed / failed
```

---

## tests/README.md

更新：

```text
当前测试数量
Memory V2 测试清单
eval_memory.py
并发测试
恢复测试
幂等测试
```

不要继续写死旧的：

```text
83 passed
```

可以改成：

```text
最近一次结果以 CI 为准。
```

---

# 十五、建议修改文件

```text
core/settings.py
.env.example

memory/models.py
memory/repository.py
memory/service.py
memory/consolidator.py
memory/scheduler.py
memory/retriever.py
memory/trace.py

core/worker.py
agent/query_agent.py

eval/eval_memory.py
eval/memory/*.jsonl

tests/test_memory_extraction_state.py
tests/test_memory_sqlite_concurrency.py
tests/test_memory_consolidation_runs.py
tests/test_memory_service.py
tests/test_memory_scheduler.py
tests/2阶段测试/test_worker_flow.py
tests/2阶段测试/test_query_agent_react.py

README.md
DESIGN.md
tests/README.md
```

---

# 十六、实施顺序

## 第一步：Extraction State

```text
S1：建 memory_extraction_states 表
S2：增加状态 Repository API
S3：修改 process_note_memory
S4：修改 Worker 已存在笔记恢复逻辑
S5：修改 daily consolidation
S6：补状态测试
```

## 第二步：SQLite 并发

```text
S7：连接开启 WAL、busy_timeout
S8：增加 database locked 有限重试
S9：接入所有写操作
S10：补并发与重试测试
```

## 第三步：Consolidation 幂等

```text
S11：建 memory_consolidation_runs 表
S12：增加 reserve / completed / failed API
S13：修改 Memory Scheduler
S14：补重启和重复执行测试
```

## 第四步：检索与文档

```text
S15：增加 MEMORY_QUERY_MIN_SCORE
S16：接入 memory_search 和 ReAct
S17：更新评测
S18：同步 README、DESIGN、tests/README
```

---

# 十七、验收命令

## Ruff

```bash
python -m ruff check .
```

## 完整测试

```bash
python -m pytest tests \
  --cov=. \
  --cov-report=term-missing \
  --cov-fail-under=63
```

## Dry-run

```bash
python eval/eval_classification.py --dry-run
python eval/eval_retrieval.py --dry-run
python eval/eval_summary.py --dry-run
python eval/eval_query_react.py --dry-run
python eval/eval_memory.py --dry-run
```

## Memory 全量离线评测

```bash
python eval/eval_memory.py
```

---

# 十八、最终验收标准

## Extraction

```text
[ ] 每条 note 都有独立 extraction state
[ ] 正常零候选会记录 empty
[ ] 部分成功会记录 partial
[ ] failed 和 partial 可以恢复
[ ] completed 和 empty 不会重复处理
[ ] stale processing 可以恢复
```

## SQLite

```text
[ ] 开启 WAL
[ ] 设置 busy_timeout
[ ] locked 错误有限重试
[ ] 普通异常不重试
[ ] 并发测试不出现 database is locked
```

## Consolidation

```text
[ ] daily 同一 period 不重复
[ ] weekly 同一 period 不重复
[ ] monthly 同一 period 不重复
[ ] 重启后仍保持幂等
[ ] failed 可以重试
[ ] stale running 可以恢复
```

## Retrieval

```text
[ ] Memory 搜索有最低分数
[ ] 低相关记忆不会进入回答上下文
[ ] min_score 可以通过环境变量配置
```

## CI

```text
[ ] Python 3.10 通过
[ ] Python 3.11 通过
[ ] Ruff 通过
[ ] 全部 pytest 通过
[ ] 覆盖率高于 63%
[ ] 五条 dry-run 通过
```

---

# 十九、本轮完成后的项目状态

完成本轮后，Memory V2 将从：

```text
功能已经能跑
```

提升为：

```text
有明确状态
失败能恢复
并发更稳定
重启不重复
检索有门槛
```

之后才适合进入下一阶段：

```text
LLM Memory Extractor
Memory Embedding
更复杂的语义合并
更强的冲突判断
```

---

# 二十、一句话总结

本次不是继续“增加记忆功能”，而是把 Memory V2 的执行底座补完整：

> 明确每条笔记是否处理完成，解决 SQLite 并发锁问题，并保证 consolidation 在程序重启后也不会重复执行。
