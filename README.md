# 随心记 Agent

随心记 Agent 是一个飞书里的个人记忆助手：把随手发来的文本先写入 WAL，再由有界后台任务分类、生成 embedding、建立相关笔记链接，并支持 `/ask` 查询、固定条件筛选和周期总结。

```text
Feishu
  -> WAL
  -> BoundedTaskExecutor
  -> PendingDrainer
  -> Classifier / Embedding / Related Search
  -> Markdown / Index / Vector Store
  -> Memory V2 / Trace
  -> ReAct Query
  -> Reflection Summary
  -> DeliveryStore
  -> Observability / Evaluation
```

## 核心功能

- 普通文本自动归档：写入 `data/cache/{space_id}.jsonl`，后台生成标题、固定 taxonomy 的 type/tags、摘要和 related 链接。
- 查询历史笔记：`/ask` 走 ReAct 工具路由和语义检索；`/type`、`/tag`、`/filter` 直接筛选 `index.json`。
- 长期记忆：从原始笔记提取 episodic、semantic、preference、task 四类记忆，写入 SQLite，并保留来源、版本和状态。
- 记忆控制：`/memory list|show|search|forget|purge|correct|conflicts|stats|consolidate` 可查看、检索、修正、软删除和彻底删除长期记忆。
- 记忆 Trace：`/trace latest`、`/trace <id>`、`/trace memory <memory_id>` 可解释“为什么记住”和“为什么召回”。
- 手动和自动总结：`/summary 今天|昨天|一周|一个月|半年|一年`，以及 `/summary_auto on|off|status|time 22:00`。
- 可靠性设计：WAL 先写入、`message_id` 幂等、pending 后台自动 drain、有界任务队列、同一 `space_id` 写入串行。
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

## 使用示例

```text
今天看了一篇关于 RAG 语义分块的文章，感觉按标题层级切分不一定适合小说。
/ask 上次我说吃馅饼是什么时候？
/filter type=生活 tags=饮食,日常
/summary 一周
/summary_auto time 22:00
/memory search Python Agent
/memory consolidate daily
/trace latest
/status
```

重复消息会被 `message_id` 幂等跳过。队列满时消息会保持 pending，`PendingDrainer` 会在进程持续运行时自动重新提交；进程重启后也会先 drain 一轮 pending。

## 运行产物

```text
data/cache/{space_id}.jsonl
data/notes/{space_id}/{YYYY-MM-DD}.md
data/notes/{space_id}/index.json
data/notes/{space_id}/vectors/index.json
data/memory/memory.db
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

Memory V2 独立评测数据位于 `eval/memory/`，覆盖提取、过滤、关系判断、冲突更新、任务状态、过期隐藏、生命周期、检索、端到端场景和工程加固指标。当前展示指标记录在 `docs/metrics/latest.json`，包含分类、检索、查询、总结、pending 恢复、重复消息防护和 Memory Evaluation 状态。未真实测量的延迟字段使用 `null` 和 `measurement_status=not_measured`，不会把 0 包装成性能结论。

Memory V2 写入以 `memory_extraction_states` 记录每条 note 的提取状态：`completed` 和 `empty` 不会被 daily consolidation 重复处理，`failed` 和 `partial` 可恢复重试，超时的 `processing` 会被视为 stale 并恢复。SQLite 连接开启 WAL、`busy_timeout` 和有限 locked 重试；daily/weekly/monthly consolidation 通过 `memory_consolidation_runs` 按 `space_id + cadence + period_key` 持久化幂等，后台 scheduler 和手动 `/memory consolidate ...` 共用同一套幂等入口。daily 单条 note 失败会被隔离为 `failed` 列表，不阻塞后续 note；只要本 cadence 存在 failed space，scheduler 当天下一 tick 会重试，已 completed 的 space 由数据库跳过。长期记忆查询默认应用 `SUIXINJI_MEMORY_QUERY_MIN_SCORE`，低相关记忆不会进入 `/ask` 的 active memory prefetch。

## Trace 示例

```text
message_received
  -> wal_appended
  -> task_queued
  -> classify_success
  -> embedding_success
  -> related_search
  -> note_saved
  -> vector_saved
  -> memory_extraction_started
  -> extraction_state_processing
  -> candidate_extracted
  -> relation_classified
  -> memory_inserted / memory_merged / memory_superseded
  -> extraction_state_completed / extraction_state_empty / extraction_state_partial / extraction_state_failed
  -> wal_processed
```

查询 Trace 记录 `query_received`、`query_routed`、`memory_search`、`note_search`、`rerank`、`evidence_selected`、`answer_generated` 和 `answer_returned`。`/ask` 会先做一次 active memory prefetch；如果召回到长期记忆，回答末尾会附上来源。

每一步对应结构化日志字段：`duration_ms`、`status`、`space_id`、`message_id`、`record_id`、`error` 和 `extra`。默认不会记录完整消息正文，只记录长度和必要上下文。

任务日志还会记录 `queue_wait_ms`、`execution_ms` 和 `total_duration_ms`，后续可用 `python scripts/build_metrics.py` 从结构化日志生成真实运行时指标。

## 当前边界

- 只支持文本消息；语音、图片、文件仍是未来计划。
- 本地 JSON/Markdown 存储适合学习和小规模使用，不适合多进程高并发生产部署。
- 同一 `space_id` 写入在进程内串行；跨进程部署需要数据库锁或 OS 文件锁。
- 飞书接口没有本地 exactly-once 保证；当前通过 delivery key 避免正常重试和重复调度造成重复发送，网络超时等不确定状态会标记为 `unknown`。
- `reserved` delivery 使用 10 分钟租约，过期后可恢复为 failed 并有限重试；同一 delivery 默认最多尝试 3 次。
- LLM 质量依赖模型和 prompt，真实失败样例应通过 `/feedback` 持续沉淀到 `eval/data/`。
- Memory V2 当前提取和关系判断使用确定性规则，不调用 LLM；复杂自然语言合并会在后续接入可评测的 LLM 提取器。
- Memory consolidation 提供 daily/weekly/monthly 后台 scheduler，并保留 `/memory consolidate ...` 命令用于手动触发。

## Roadmap

- 已完成：WAL、固定 taxonomy 分类、embedding 相关笔记、ReAct 查询、手动/自动总结、有界任务执行器、pending drainer、发送幂等、Memory V2 第一版、CI 和 dry-run 评测。
- 下一阶段：LLM 记忆提取器、高级记忆合并、真实截图/GIF 展示、故障恢复演示材料、数据库或跨进程锁。
