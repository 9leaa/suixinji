# 随心记 Agent · 设计方案

## 系统目标

项目目标是把飞书里的零散文本沉淀为可检索、可总结、可恢复的个人记忆库。工程上优先保证：入口快、慢任务有界、写入可恢复、参数集中、评测可复现。

## 写入链路

```text
Feishu Receiver
  -> parse text / strip mention
  -> build space_id
  -> append WAL once by message_id
  -> submit ingest task
  -> Classifier
  -> Embedding
  -> related search
  -> save markdown + index.json
  -> save vector
  -> mark WAL processed
```

WAL 位于 `data/cache/{space_id}.jsonl`。普通消息必须先写 WAL，再提交后台任务。队列满时不会删除 WAL，后续 pending recovery 可以继续处理。

分类使用固定 taxonomy，由 `core/taxonomy.py` 校验和规范化，不允许模型自由生成无限标签。

## 查询链路

固定条件查询直接读取 `index.json`：

```text
/type
/tag
/filter
```

自然语言查询使用 `agent/query_agent.py` 的 ReAct 循环：

```text
/ask
  -> decide tool
  -> filter_notes / semantic_search / list_recent / get_note / follow_links
  -> synthesize answer
```

默认查询参数来自 `core/settings.py`：`QUERY_TOP_K=5`、`QUERY_MIN_SCORE=0.55`。

## 总结链路

手动总结由 `/summary` 提交 summary task。自动总结由 `summary/scheduler.py` 每分钟扫描订阅，到点后提交同一个有界执行器；任务成功发送后才更新 `last_sent_date`。

总结流程：

```text
load notes in range
  -> draft summary
  -> reflection review
  -> fallback summary on LLM failure
  -> save summaries/
  -> send Feishu reply
```

默认自动总结时间来自 `core/settings.py`：`SUMMARY_DEFAULT_TIME="22:00"`。

## 并发模型

`runtime/executor.py` 提供 `BoundedTaskExecutor`：

```text
MAX_WORKERS = 4
TASK_QUEUE_SIZE = 100
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
- 查询任务允许并行。
- 同一 `space_id` 的 summary task 通过 summary lock 串行执行。

## 存储模型

```text
data/cache/{space_id}.jsonl                 WAL
data/notes/{space_id}/{YYYY-MM-DD}.md       人类可读笔记
data/notes/{space_id}/index.json            机器可读索引
data/notes/{space_id}/vectors/index.json    本地向量索引
data/notes/{space_id}/summaries/            总结
data/summary_subscriptions.json             自动总结订阅
data/logs/app-YYYY-MM-DD.jsonl              结构化日志
```

`space_id` 由飞书单聊 open_id 或群聊 chat_id 派生，并通过 `safe_space_id()` 做路径安全化。

## 一致性与恢复

- WAL append 成功后，消息不会因为后台队列满而丢失。
- Worker 以 `message_id` 检查笔记是否已存在，避免重复写入。
- 如果笔记已存在但向量缺失，`backfill_vector_if_missing()` 会补写向量。
- 启动时 `recover_pending_records()` 扫描 WAL 文件，继续处理 pending 记录。
- 结构化日志记录成功、失败、拒绝和最近 LLM timeout，供 `/status` 展示。

## 评测体系

CI 执行：

```text
ruff check .
python -m pytest tests --cov=. --cov-report=term-missing
python eval/eval_classification.py --dry-run
python eval/eval_retrieval.py --dry-run
python eval/eval_summary.py --dry-run
python eval/eval_query_react.py --dry-run
```

评测样例位于 `eval/data/`，展示指标位于 `docs/metrics/latest.json`。Dry-run 只验证数据和流程，不调用真实 LLM 或 embedding API。

## 已知边界

- 进程内锁不能覆盖多进程部署。
- 本地 JSON 向量索引不适合大规模数据。
- Summary scheduler 仍是单进程后台线程，但实际 summary 生成统一进入 `BoundedTaskExecutor`。
- 语音、图片、文件尚未进入 WAL。
- LLM 输出质量需要通过真实 `/feedback` 样例持续评估。
