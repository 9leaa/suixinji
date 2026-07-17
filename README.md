# 随心记 Agent

随心记 Agent 是一个飞书里的个人记忆助手：普通文本先写入 Inbox/WAL 并用本地规则快速归档，LLM 分类、embedding 和相关链接在独立后台池补全；`/ask`、固定条件筛选和周期总结都可以读取已经落库的内容。存储可通过 `STORAGE_BACKEND=local|postgres` 切换。

```text
Feishu
  -> WAL
  -> BoundedTaskExecutor
  -> PendingDrainer
  -> Local Provisional Classification / Markdown / Index / Memory
  -> EnrichmentDrainer / LLM Classification / Embedding / Related Search / Vector Store
  -> Memory Extraction / Adjudication / Evolution / Trace
  -> ReAct Query
  -> Reflection Summary
  -> DeliveryStore
  -> Observability / Evaluation
```

## 核心功能

- 普通文本自动归档：写入 `data/cache/{space_id}.jsonl` 后，本地规则先生成 provisional 元数据并立即归档；LLM/embedding/related 在独立后台池补全，失败最多重试有限次数。
- 查询历史笔记：`/ask` 会先冲刷同空间 pending，并用本地词法检索读取尚在增强的最新笔记；其余情况走 ReAct 和语义检索。`/type`、`/tag`、`/filter` 直接筛选 `index.json`。
- 敏感内容保护：密码、密钥、Bearer/JWT、带凭据的连接串和高风险身份证件/银行卡值在入口处本地拦截，不写原文、不做 embedding、不发送给模型；遗留敏感笔记也从查询、链接、总结和记忆整理中统一过滤。
- 长期记忆：原始 Note 只作为证据；经过 Candidate 校验、旧记忆检索、六类关系审理和确定性演化后，形成当前有效认知。
- 可审计演化：`new/same/merge/update_task/supersede/conflict` 均写入 `memory_decisions`；跨记忆关系写入 `memory_relations`，合并、任务更新、替代和冲突使用版本记录。
- 保守审阅：置信度不足的破坏性动作先生成 `pending_review` 候选；`/memory approve <id>` 会原子执行原审理关系，而不是简单把候选改成 active。
- 记忆控制：`/memory list|show|search|profile|pending|approve|decisions|forget|purge|correct|conflicts|stats|consolidate` 可查看、审阅、修正和维护长期记忆。
- 记忆 Trace：`/trace latest`、`/trace <id>`、`/trace memory <memory_id>` 可解释“为什么记住”和“为什么召回”。
- 手动和自动总结：`/summary 今天|昨天|一周|一个月|半年|一年`，以及 `/summary_auto on|off|status|time 22:00`。
- 可靠性设计：WAL 先写入、`message_id` 幂等、pending 后台自动 drain、有界任务队列、同一 `space_id` 写入串行。
- 延迟隔离：交互任务池和 LLM 增强池分离；默认单次模型请求 15 秒、SDK 不做内层重试，失败由应用级 EnrichmentDrainer 间隔重试。
- 发送幂等：查询回答、归档成功提示、手动总结和自动总结都通过 `DeliveryStore` 生成 delivery key，避免重复发送。
- 状态对账：自动总结如果已经发送但订阅状态没更新，下一轮 scheduler 会按 delivery 记录补写 `last_sent_date`，不会重复生成或发送。
- 调度韧性：Scheduler 对每个订阅进行异常隔离，并在 tick 层提供总异常保护；单个订阅或单次 tick 失败不会导致后台线程退出。
- 可观测性：结构化日志写入 `data/logs/app-YYYY-MM-DD.jsonl`，`/status` 展示 pending、队列、容量、成功、失败、拒绝和最近错误。

## 快速启动

```bash
python3 -m venv .venv
source .venv/bin/activate
make install-dev
cp .env.example .env
python scripts/check_config.py
make test
make eval-dry-run
make start
```

`.env` 至少需要飞书应用配置；真实 LLM、embedding 调用还需要 OpenAI 或 OpenAI-compatible 服务配置。开发测试和 dry-run 评测不会调用真实 API。

PostgreSQL 模式需要设置 `STORAGE_BACKEND=postgres` 和 `DATABASE_URL`，然后运行 `make db-upgrade`。如果数据库由其他机器提供，不要启动 compose 的 `local-infra` profile。仓库中的 PostgreSQL 容器仅用于明确需要本机临时数据库时手动启动：`docker compose --profile local-infra up -d postgres`。

## Redis Hooks 与分布式角色

第二、第三阶段使用以下配置：

```text
STORAGE_BACKEND=postgres
COORDINATION_BACKEND=redis
TASK_QUEUE_BACKEND=redis_streams
REDIS_URL=redis://...
```

Query、Summary 和 Memory 流程经过同步 HookManager，提供共享限流、请求幂等、LLM 并发槽位、版本化查询缓存、临时 Session、短临界区分布式锁和 PostgreSQL Agent Run 审计。Redis 故障时，缓存和 Session 会跳过；普通 Inbox 仍以 PostgreSQL 唯一约束为最终保证；关键写锁会回退 PostgreSQL advisory lock。

分布式运行角色：

```text
Receiver -> PostgreSQL Inbox + Task + Outbox
Outbox Relay -> Redis Streams
Ingest / Query / Summary / Memory Worker
Delivery Worker -> Feishu
Leader-locked Scheduler
FastAPI test receiver :8000
```

在已激活 `zcj_hello` 环境中启动：

```bash
make distributed-start
make distributed-status
make distributed-stop
```

也可以使用 Docker：

```bash
make distributed-up
make distributed-down
```

本机测试用 Redis profile 明确关闭 AOF 和 RDB 快照，不做持久化写盘；使用外部 Redis 时无需启动该 profile。

## 使用示例

```text
今天看了一篇关于 RAG 语义分块的文章，感觉按标题层级切分不一定适合小说。
/ask 上次我说吃馅饼是什么时候？
/filter type=生活 tags=饮食,日常
/summary 一周
/summary_auto time 22:00
/memory search Python Agent
/memory decisions
/memory pending
/memory profile
/memory consolidate daily
/trace latest
/status
```

重复消息会被 `message_id` 幂等跳过。队列满时消息会保持 pending，`PendingDrainer` 会在进程持续运行时自动重新提交；进程重启后也会先 drain 一轮 pending。

## 运行产物

`STORAGE_BACKEND=postgres` 时，Inbox、Note、Embedding、Memory、Summary Subscription 和 Delivery 的正式读写都进入 PostgreSQL；下面的本地文件仅用于 `local` 兼容、导出、备份和日志。

```text
data/cache/{space_id}.jsonl
data/notes/{space_id}/{YYYY-MM-DD}.md
data/notes/{space_id}/index.json
data/notes/{space_id}/vectors/index.json
data/memory/memory.db              # memories/sources/versions/decisions/relations/traces
data/memory/traces.jsonl
data/notes/{space_id}/summaries/
data/summary_subscriptions.json
data/deliveries/index.json
data/logs/app-YYYY-MM-DD.jsonl
```

## 测试与评测

```bash
make lint
make test
make eval-dry-run
```

CI 会在 Python 3.10 和 3.11 上执行 Ruff、pytest coverage 和五条 dry-run 评测：

```text
eval/eval_classification.py --dry-run
eval/eval_retrieval.py --dry-run
eval/eval_summary.py --dry-run
eval/eval_query_react.py --dry-run
eval/eval_memory.py --dry-run
```

Memory 独立评测数据位于 `eval/memory/`，覆盖提取、过滤、六类关系审理、错误 merge/supersede、冲突更新、任务状态、生命周期、检索、端到端场景和工程加固指标。当前展示指标记录在 `docs/metrics/latest.json`。未真实测量的延迟字段使用 `null` 和 `measurement_status=not_measured`，不会把 0 包装成性能结论。

核心记忆写入以 `memory_extraction_states` 记录每条 note 的提取状态：`completed` 和 `empty` 不会被重复处理，`failed` 和 `partial` 可恢复重试，超时的 `processing` 会被视为 stale 并恢复。SQLite 连接开启 WAL、`busy_timeout` 和有限 locked 重试；daily/weekly/monthly consolidation 通过 `memory_consolidation_runs` 持久化幂等。长期记忆查询默认应用 `SUIXINJI_MEMORY_QUERY_MIN_SCORE`，低相关记忆不会进入 `/ask` 的 active memory prefetch。

## Trace 示例

```text
message_received
  -> wal_appended
  -> task_queued
  -> local_classify_success
  -> provisional_note_saved
  -> memory_extraction_started
  -> extraction_state_processing
  -> candidate_extracted
  -> candidate_memories_found
  -> relation_decided
  -> evolution_started
  -> memory_inserted / memory_merged / memory_updated / memory_superseded / memory_conflicted / memory_pending_review
  -> extraction_state_completed / extraction_state_empty / extraction_state_partial / extraction_state_failed
  -> wal_processed
  -> archived_reply
  -> background_llm_classify / embedding / related_search / vector_saved
```

查询 Trace 记录 `query_received`、`query_routed`、`memory_search`、`note_search`、`rerank`、`evidence_selected`、`answer_generated` 和 `answer_returned`。`/ask` 会先做一次 active memory prefetch；如果召回到长期记忆，回答末尾会附上来源。

每一步对应结构化日志字段：`duration_ms`、`status`、`space_id`、`message_id`、`record_id`、`error` 和 `extra`。默认不会记录完整消息正文，只记录长度和必要上下文。

任务日志还会记录 `queue_wait_ms`、`execution_ms` 和 `total_duration_ms`，后续可用 `python scripts/build_metrics.py` 从结构化日志生成真实运行时指标。

## 当前边界

- 只支持文本消息；语音、图片、文件仍是未来计划。
- `local` 后端适合学习和小规模使用；多实例部署应使用 PostgreSQL 后端。
- 同一 `space_id` 写入在进程内串行；跨进程部署需要数据库锁或 OS 文件锁。
- 飞书接口没有本地 exactly-once 保证；当前通过 delivery key 避免正常重试和重复调度造成重复发送，网络超时等不确定状态会标记为 `unknown`。
- `reserved` delivery 使用 10 分钟租约，过期后可恢复为 failed 并有限重试；同一 delivery 默认最多尝试 3 次。
- LLM 质量依赖模型和 prompt，真实失败样例应通过 `/feedback` 持续沉淀到 `eval/data/`。
- 记忆抽取支持 `rules`、`llm`、`hybrid` 三种模式，默认使用可复现的 `rules`；`llm`/`hybrid` 只生成候选，失败会回退规则，数据库更新始终由本地审理阈值和原子演化事务控制。
- 当前候选检索以类型、状态、结构化 subject/predicate、实体和词法相似度为稳定路径；`memory_vectors` 仍保留为可选 embedding 检索扩展位。
- Memory consolidation 提供 daily/weekly/monthly 后台 scheduler，并保留 `/memory consolidate ...` 命令用于手动触发。

## Roadmap

- 已完成：WAL、固定 taxonomy 分类、embedding 相关笔记、ReAct 查询、手动/自动总结、有界任务执行器、pending drainer、发送幂等、可审计核心记忆链路、CI 和 dry-run 评测。
- 下一阶段：在真实反馈集上评测后启用 `hybrid` 抽取、接入 memory embedding、补充冲突人工裁决界面、真实截图/GIF 展示和跨进程锁。
