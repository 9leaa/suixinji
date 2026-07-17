# 随心记 Agent · 设计方案

## 系统目标

项目目标是把飞书里的零散文本沉淀为可检索、可总结、可恢复的个人记忆库。工程上优先保证：入口快、慢任务有界、写入可恢复、参数集中、评测可复现。

## 写入链路

```text
Feishu Receiver
  -> parse text / strip mention
  -> build space_id
  -> local sensitive-value gate
  -> append WAL once by message_id
  -> submit ingest task
  -> PendingDrainer resubmits pending when needed
  -> deterministic provisional classifier
  -> save markdown + index.json
  -> extract / adjudicate / evolve long-term memory
  -> mark WAL processed
  -> send archived acknowledgement
  -> separate enrichment pool: LLM classifier -> embedding -> related search -> vector
```

WAL 位于 `data/cache/{space_id}.jsonl`。普通消息必须先写 WAL，再提交后台任务。队列满时不会删除 WAL，`runtime/pending_drainer.py` 会定期扫描 pending 记录并重新提交到有界执行器。高置信敏感值是唯一例外：入口只写入不含原文的 `blocked_sensitive` 幂等记录，原文不会进入 WAL、Note、向量或外部模型。

正式飞书启动流程：

```text
create BoundedTaskExecutor
  -> PendingDrainer.drain_once()
  -> PendingDrainer.start()
  -> EnrichmentDrainer.drain_once()
  -> EnrichmentDrainer.start()
  -> start summary scheduler
  -> start Feishu long connection
```

`process_pending()` 保留给 CLI 和测试使用，不再作为飞书启动主路径。

分类使用固定 taxonomy，由 `core/taxonomy.py` 校验和规范化，不允许模型自由生成无限标签。

## 查询链路

固定条件查询直接读取 `index.json`：

```text
/type
/tag
/filter
```

自然语言查询先执行 read-after-write 屏障，再按证据状态路由：

```text
/ask
  -> flush same-space pending through fast local archival
  -> provisional_search (when enrichment is provisional/enriching/failed; no LLM wait)
  -> decide tool
  -> filter_notes / semantic_search / list_recent / get_note / follow_links
  -> synthesize answer
```

默认查询参数来自 `core/settings.py`：`QUERY_TOP_K=5`、`QUERY_MIN_SCORE=0.55`。

长期记忆查询通过 `memory_search` 工具和 `/memory search` 命令读取 `data/memory/memory.db` 中最新 `active` 记忆。`superseded`、`deleted` 和 `expired` 默认不会作为当前事实返回。

## 核心记忆系统

核心记忆系统将原始 Note 证据和长期认知分离：

```text
原始笔记
  -> memory_extraction_states = processing
  -> extract_candidates（rules / llm / hybrid）
  -> validate_candidates（敏感信息、置信度、结构校验）
  -> retrieve active same-type memories
  -> adjudicate_memory（new / same / merge / update_task / supersede / conflict）
  -> evolve_memory（确定性原子事务）
  -> memory_extraction_states = completed / empty / partial / failed
  -> write memory trace
```

存储位于 `data/memory/memory.db`，核心表：

```text
memories          当前记忆内容、类型、状态、重要性、置信度、访问次数
memory_sources    记忆与原始 note_id 的来源关系
memory_versions   每次创建、修正、删除、supersede 的版本记录
memory_decisions  每次审理的关系、置信度、理由、证据引用、动作和执行结果
memory_relations  supersedes/conflicts_with/summarized_from 等跨记忆关系
memory_extraction_states  每条 note 的记忆提取状态、候选数、成功数和错误摘要
memory_consolidation_runs daily/weekly/monthly consolidation 的持久化幂等记录
memory_traces     完整 Trace JSON；同时保留 JSONL 运维副本
```

短期支持四类记忆：

```text
episodic    具体事件
semantic    稳定事实
preference  偏好和约束
task        待办和进度，含 todo/in_progress/blocked/done/cancelled
```

用户命令：

```text
/memory list
/memory show <id>
/memory search <内容>
/memory profile
/memory pending
/memory approve <id>
/memory decisions
/memory forget <id>
/memory purge <id>
/memory correct <id> <新内容>
/memory conflicts
/memory stats
/memory consolidate daily|weekly|monthly
```

候选抽取支持 `SUIXINJI_MEMORY_EXTRACTOR_MODE=rules|llm|hybrid`。默认 `rules` 保持成本和延迟可控；`llm`/`hybrid` 的模型只输出候选，异常自动回退规则。所有候选都必须经过本地校验；中等置信度的破坏性动作会进入 `pending_review`，不能直接覆盖旧认知。重复记忆只追加来源并提高确认度；merge 必须生成新版本；supersede/conflict 会在单个 SQLite `BEGIN IMMEDIATE` 事务内同时写入新旧状态、版本、来源、关系和 Decision，避免半更新。

`/memory approve <id>` 会读取待审 Decision，并在一个事务中执行原始 merge/update_task/supersede/conflict；用于承载候选的 pending Memory 会按动作转为 active/conflicted/archived，不会与旧记忆无关系地同时激活。

每条 note 的提取结果独立记录为 `processing`、`completed`、`empty`、`partial` 或 `failed`；terminal 状态重复调用会幂等跳过。daily consolidation 只重试缺失状态、`failed`、`partial` 和超时的 `processing`。单条 note 提取失败会被隔离，外层 consolidation run 保持可重试。

Consolidation 入口：

```text
daily    process_unextracted_notes：处理尚未提取记忆的历史笔记
weekly   merge_duplicate_episodic：合并重复情景记忆并保留来源
monthly  generate_stable_semantic：由多条 episodic 生成稳定 semantic
```

`memory/scheduler.py` 在启动时创建后台线程，每小时检查一次：每天触发 daily，每周一触发 weekly，每月 1 日触发 monthly。每次执行先按 `space_id + cadence + period_key` reserve `memory_consolidation_runs`，reserve 成功才运行，完成后写 `completed`，失败写 `failed` 并允许后续重试。只有当本 cadence 的 report 不含 failed space 时，scheduler 才记录当天已完成；否则同一进程当天后续 tick 会再次进入该 cadence，并由数据库跳过已 completed 的 space。`/memory consolidate daily|weekly|monthly` 保留为手动运维入口，但同样调用幂等 scheduler 入口，避免手动重复执行 monthly 生成重复稳定记忆。

SQLite Memory DB 开启 WAL、`busy_timeout` 和针对 `database is locked` 的有限重试；consolidation reserve 使用 `BEGIN IMMEDIATE` 原子事务，保证同 key 并发 reserve 只有一个成功。候选旧记忆检索组合类型/状态过滤、结构化 subject/predicate、实体和词法相似度，Top K 由 `SUIXINJI_MEMORY_ADJUDICATION_TOP_K` 控制。`memory_vectors` 表保留为可选 embedding 扩展位。长期记忆查询通过 `SUIXINJI_MEMORY_QUERY_MIN_SCORE` 过滤低相关结果。

## Memory Trace

Memory trace 写入 SQLite `memory_traces`，并追加到 `data/memory/traces.jsonl` 作为运维副本。默认只记录 ID、长度、类型、分数、状态和原因摘要；错误中的输入/输出 preview 与凭据字段会被脱敏。

写入 trace 关键步骤：

```text
note_saved
memory_extraction_started
candidate_extracted
retrieval_started
candidate_memories_found
relation_decided
evolution_started
memory_inserted / memory_merged / memory_updated / memory_superseded / memory_conflicted / memory_pending_review / memory_discarded
trace_finished
```

查询 trace 关键步骤：

```text
query_received
query_routed
memory_search
note_search
rerank
evidence_selected
answer_generated
answer_returned
```

`/ask` 默认先执行 active memory prefetch；如果命中长期记忆，最终回答会附带来源。

查看方式：

```text
/trace latest
/trace <trace_id>
/trace memory <memory_id>
python scripts/show_trace.py --trace-id <trace_id>
```

## 总结链路

手动总结由 `/summary` 提交 summary task。自动总结由 `summary/scheduler.py` 每分钟扫描订阅，到点后提交同一个有界执行器；delivery 标记为 sent 后才更新 `last_sent_date`。

总结流程：

```text
load notes in range
  -> draft summary
  -> reflection review
  -> fallback summary on LLM failure
  -> save summaries/
  -> reserve delivery
  -> send Feishu reply
  -> mark delivery sent
```

默认自动总结时间来自 `core/settings.py`：`SUMMARY_DEFAULT_TIME="22:00"`。

## 并发模型

`runtime/executor.py` 提供 `BoundedTaskExecutor`：

```text
MAX_WORKERS = 4
TASK_QUEUE_SIZE = 100
PENDING_DRAIN_INTERVAL_SECONDS = 15
PENDING_DRAIN_BATCH_SIZE = 20
DELIVERY_RESERVATION_TTL_SECONDS = 600
DELIVERY_MAX_ATTEMPTS = 3
TASK_HISTORY_LIMIT = 1000
TASK_HISTORY_TTL_HOURS = 24
ENRICHMENT_MAX_WORKERS = 2
ENRICHMENT_QUEUE_SIZE = 50
ENRICHMENT_DRAIN_INTERVAL_SECONDS = 30
ENRICHMENT_MAX_ATTEMPTS = 3
```

任务类型：

```text
ingest
query
summary
```

任务状态：

```text
queued -> running -> success|failed
rejected
```

并发规则：

- 全局最大 worker 数由 `MAX_WORKERS` 控制。
- 队列容量由 `TASK_QUEUE_SIZE` 控制。
- 同一 `space_id` 写入任务通过进程内锁串行执行。
- 同一 `(space_id, message_id)` 的 ingest 任务通过 inflight set 防止重复提交。
- 查询任务允许并行。
- LLM/embedding 增强使用独立线程池，不占用 ingest/query/summary 的 worker；同一 note_id 通过 inflight set 去重。
- 同一 `space_id` 的 summary task 通过 summary lock 串行执行。
- 执行器不重试完整业务 runner；LLM SDK 默认 15 秒超时且不做内层重试，provisional/failed 增强由 EnrichmentDrainer 最多尝试 3 次。
- Summary scheduler 对每个订阅进行异常隔离，并通过 `run_scheduler_tick_safely()` 保护后台循环；单个订阅或单次 tick 失败不会让调度线程永久退出。

任务完成日志包含：

```text
queue_wait_ms
execution_ms
total_duration_ms
```

`TaskRegistry` 会保留 queued/running 任务、最近任务和近期失败任务，累计成功/失败/拒绝计数独立保留，避免长期运行时 `_tasks` 无限增长。

## 存储模型

```text
data/cache/{space_id}.jsonl                 WAL
data/notes/{space_id}/{YYYY-MM-DD}.md       人类可读笔记
data/notes/{space_id}/index.json            机器可读索引
data/notes/{space_id}/vectors/index.json    本地向量索引
data/notes/{space_id}/summaries/            总结
data/summary_subscriptions.json             自动总结订阅
data/deliveries/index.json                  发送幂等记录
data/logs/app-YYYY-MM-DD.jsonl              结构化日志
```

`space_id` 由飞书单聊 open_id 或群聊 chat_id 派生，并通过 `safe_space_id()` 做路径安全化。

## 一致性与恢复

- WAL append 成功后，消息不会因为后台队列满而丢失。
- PendingDrainer 在队列恢复后自动重新提交 pending；恢复任务不重复发送归档提示。
- Worker 以 `message_id` 检查笔记是否已存在，避免重复写入。
- provisional、enriching 或可重试 failed 笔记由 EnrichmentDrainer 补写分类与向量；交互归档和查询不等待该过程。
- DeliveryStore 以 delivery key 避免重复发送查询回答、归档成功提示、手动总结和自动总结。
- 自动总结 scheduler 在提交任务前会调用 `summary/reconciliation.py` 对账：如果当天 auto summary delivery 已经是 `sent`，但订阅 `last_sent_date` 未更新，会直接补写订阅状态并跳过本轮生成。
- 启动时会调用 `recover_stale_reserved_deliveries()`，把过期 reserved delivery 标记为 failed，允许后续按最大尝试次数重新 reserve。
- 结构化日志记录成功、失败、拒绝和最近 LLM timeout，供 `/status` 展示。

Delivery key 规则：

```text
ingest:{space_id}:{message_id}:archived
query:{space_id}:{message_id}
manual_summary:{space_id}:{message_id}
auto_summary:{space_id}:{range_key}:{date}
```

发送状态包括 `reserved`、`sent`、`failed` 和 `unknown`。`reserved` 是带 TTL 的租约，不是永久锁；`failed` 可有限重试；`sent` 永不自动重复发送；`unknown` 不会立即自动重发，避免远端已成功但本地无法确认时重复发送。

## 评测体系

CI 执行：

```text
ruff check .
python -m pytest tests --cov=. --cov-report=term-missing
python eval/eval_classification.py --dry-run
python eval/eval_retrieval.py --dry-run
python eval/eval_summary.py --dry-run
python eval/eval_query_react.py --dry-run
python eval/eval_memory.py --dry-run
```

CI 在 Python 3.10 和 3.11 上运行，设置 `timeout-minutes: 10`。评测样例位于 `eval/data/` 和 `eval/memory/`，展示指标位于 `docs/metrics/latest.json`。Dry-run 只验证数据和流程，不调用真实 LLM 或 embedding API。

Memory Evaluation 覆盖：

```text
extraction_cases.jsonl
filtering_cases.jsonl
relation_cases.jsonl
conflict_cases.jsonl
lifecycle_cases.jsonl
retrieval_cases.jsonl
end_to_end_cases.jsonl
```

未真实测量的延迟字段使用 `null` 和 `measurement_status=not_measured`。后续可用 `scripts/build_metrics.py` 从结构化日志生成真实运行时指标。

## 已知边界

- 进程内锁不能覆盖多进程部署。
- 本地 JSON 向量索引不适合大规模数据。
- Summary scheduler 仍是单进程后台线程，但实际 summary 生成统一进入 `BoundedTaskExecutor`。
- 飞书发送没有严格 exactly-once 保证；当前实现提供本地幂等和 unknown 状态保护。
- 语音、图片、文件尚未进入 WAL。
- LLM 输出质量需要通过真实 `/feedback` 样例持续评估。
- 记忆抽取默认规则模式，启用 `llm`/`hybrid` 前仍需要扩大真实反馈评测集；模型只能生成候选，不能绕过确定性演化事务。
