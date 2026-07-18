# 随心记：Redis + PostgreSQL + Agent Hooks + 多用户/多服务器改造方案

> 基于仓库 `9leaa/suixinji` 最新提交 `05167ec` 的结构审查。
>
> 本方案目标不是简单“给项目加上 Redis 和 PostgreSQL”，而是把当前单进程、本地文件型 Agent，逐步改造成可以承载大量用户、多个服务实例和多个 Worker 的分布式 Agent 系统。

---

# 1. 当前项目状态审查

## 1.1 当前主链路

当前核心流程大致是：

```text
飞书长连接
  ↓
bot/feishu_bot.py
  ↓
本地 JSONL WAL
  ↓
BoundedTaskExecutor
  ↓
分类 + Embedding + Related Search
  ↓
Markdown / index.json / vectors/index.json
  ↓
SQLite Memory V2
  ↓
Query Agent / Summary / Delivery
```

当前工程已经具备一些很有价值的可靠性机制：

```text
- message_id 幂等
- WAL 先写后处理
- 有界线程池和队列拒绝
- PendingDrainer 恢复 pending
- Delivery 租约和发送幂等
- Memory extraction 状态
- Memory consolidation 幂等
- Trace、结构化日志和离线评测
```

这些机制不应该被推倒重写，而应该迁移到 PostgreSQL 和 Redis 架构中继续保留。

---

## 1.2 当前不适合多服务器的部分

### 本地 WAL

当前：

```text
data/cache/{space_id}.jsonl
```

问题：

```text
- 不同服务器拥有不同文件
- message_id 去重需要扫描本地文件
- 修改状态时需要重写整个 JSONL
- 多进程只能依赖本机文件锁
```

### 本地任务执行器

当前 `BoundedTaskExecutor` 中包含：

```text
- ThreadPoolExecutor
- BoundedSemaphore
- TaskRegistry
- inflight_ingest set
- summary threading.Lock
- space_id 进程内锁
```

这些状态只在当前 Python 进程中可见。

如果运行三个服务实例：

```text
实例 A 不知道实例 B 的 inflight
实例 B 不知道实例 C 的任务队列
三个实例可能同时处理同一 message_id
三个实例可能同时修改同一 space_id
```

### 笔记和向量存储

当前：

```text
data/notes/{space_id}/{date}.md
data/notes/{space_id}/index.json
data/notes/{space_id}/vectors/index.json
```

问题：

```text
- 每次查询需要读取本地 JSON
- 每次新增向量需要读出全部向量再覆盖写入
- 多台机器之间不共享
- 不适合大量用户和高并发写入
```

### Memory V2

当前使用：

```text
data/memory/memory.db
```

SQLite 已经做了 WAL、busy_timeout 和 locked retry，但它仍然是单机文件数据库，不适合作为多个应用服务器共享的核心数据源。

### Delivery 和 Summary Subscription

当前分别保存在：

```text
data/deliveries/index.json
data/summary_subscriptions.json
```

其中的锁也是 `threading.RLock`，只能保护一个进程。

### Query Agent

当前 `agent/query_agent.py` 的流程是直接调用：

```text
complete_json
→ _run_tool
→ _execute_tool
→ memory_search / semantic_search / get_note
→ complete_json
```

这些位置已经天然适合加入 Hook，但目前还没有统一的 HookManager 和 AgentRunContext。

---

# 2. 改造后的总体定位

## 2.1 PostgreSQL 与 Redis 的职责

### PostgreSQL：最终事实来源

PostgreSQL 保存必须长期可靠存在的数据：

```text
- 用户、租户和空间
- 原始消息
- Outbox 事件
- 任务和任务尝试记录
- 笔记、标签、关联关系
- Note Embedding 和 Memory Embedding
- 长期记忆、来源、版本和状态
- Summary Subscription 和 Summary Run
- Delivery 状态
- Agent Run、Agent Step 和 LLM Usage
```

### Redis：运行时协调层

Redis 实现六个场景：

```text
1. 请求限流
2. 消息幂等快速拦截
3. Redis Streams 分布式任务队列
4. 分布式锁
5. 热点缓存
6. 临时会话状态
```

Redis 中的数据原则上都应该能够过期、重建或从 PostgreSQL 恢复。

---

## 2.2 推荐总架构

```text
飞书 / Web / 压测客户端
            ↓
Receiver / API 实例（可多实例）
            ↓
Agent before_request hooks
├─ Redis 限流
├─ Redis 幂等快速检查
└─ Redis Session 读取
            ↓
PostgreSQL 事务
├─ 写 inbox_messages
└─ 写 outbox_events
            ↓
Outbox Relay
            ↓
Redis Streams
├─ ingest
├─ query
├─ summary
├─ memory
└─ delivery
            ↓
多个 Worker 实例
            ↓
Agent / LLM / Tool Hooks
├─ 配额与并发检查
├─ 查询缓存
├─ space_id 分布式锁
├─ 缓存失效
└─ 失败清理
            ↓
PostgreSQL + pgvector
            ↓
Delivery Worker
            ↓
飞书回复
```

---

# 3. 对现有图片流程的一项重要修正

用户图片中的流程是：

```text
Redis 去重
→ 数据库记录原始消息
→ Redis Streams
```

方向基本正确，但不能直接做两次独立写入：

```text
先写 PostgreSQL
再写 Redis Stream
```

否则会出现双写不一致。

## 3.1 双写问题

情况一：

```text
PostgreSQL 写成功
Redis Stream 写失败
→ 原始消息存在，但永远没有 Worker 处理
```

情况二：

```text
Redis Stream 写成功
PostgreSQL 事务失败
→ Worker 收到一个数据库中不存在的任务
```

## 3.2 推荐使用 Transactional Outbox

Receiver 在一个 PostgreSQL 事务中同时写：

```text
inbox_messages
outbox_events
```

示例：

```text
BEGIN
  INSERT inbox_messages(...)
  INSERT outbox_events(event_type='ingest.requested', ...)
COMMIT
```

独立的 Outbox Relay 再执行：

```text
读取未发布 outbox_events
→ XADD 到 Redis Stream
→ 标记 published_at
```

即使 Relay 崩溃，也可以继续扫描未发布事件。

Redis Stream 中只放：

```text
task_id
event_id
space_id
task_type
attempt
```

不要把完整消息正文和全部用户数据放进 Redis Stream。Worker 根据 ID 从 PostgreSQL 读取真实数据。

---

# 4. PostgreSQL 数据模型

## 4.1 用户与空间

```text
tenants
users
spaces
space_members
```

建议所有业务表至少包含：

```text
tenant_id
space_id
```

防止大量用户情况下数据串空间。

## 4.2 消息和任务

### inbox_messages

核心字段：

```text
id
source
source_message_id
source_event_id
tenant_id
space_id
chat_id
sender_json
text
received_at
status
sequence_no
```

关键约束：

```text
UNIQUE(source, source_message_id)
UNIQUE(space_id, sequence_no)
```

### outbox_events

```text
id
event_type
aggregate_type
aggregate_id
payload_json
created_at
published_at
publish_attempt_count
last_error
```

### tasks

```text
id
task_type
tenant_id
space_id
source_message_id
idempotency_key
status
priority
attempt_count
max_attempts
next_retry_at
created_at
started_at
completed_at
last_error
```

关键约束：

```text
UNIQUE(idempotency_key)
```

### task_attempts

记录每次 Worker 尝试：

```text
task_id
worker_id
attempt_no
status
started_at
finished_at
error_type
error_summary
```

---

## 4.3 笔记与向量

```text
notes
note_tags
note_relations
note_embeddings
```

### notes

替代：

```text
Markdown + index.json
```

保存：

```text
id
message_id
tenant_id
space_id
created_at
title
note_type
summary
text
metadata_json
```

关键约束：

```text
UNIQUE(space_id, message_id)
```

### note_embeddings

使用 PostgreSQL `pgvector`：

```text
note_id
model
dimensions
embedding vector(...)
created_at
```

当前 Markdown 可以保留，但定位改为：

```text
导出格式 / 备份格式 / 人类可读产物
```

而不是核心事实来源。

---

## 4.4 Memory V2

将现有 SQLite 表迁移到 PostgreSQL：

```text
memories
memory_sources
memory_versions
memory_vectors
memory_extraction_states
memory_consolidation_runs
```

继续保留现有语义：

```text
completed / empty / partial / failed
active / superseded / conflicted / deleted
space_id + cadence + period_key 幂等
```

不要因为迁移 PostgreSQL 而丢掉已经完成的 Memory V2 加固机制。

---

## 4.5 Summary 和 Delivery

```text
summary_subscriptions
summary_runs
deliveries
delivery_attempts
```

### deliveries

需要保留：

```text
reserved
sent
failed
unknown
lease_expires_at
attempt_count
```

关键约束：

```text
UNIQUE(delivery_key)
```

发送是否真正完成，最终以 PostgreSQL Delivery 状态为准，不能只依赖 Redis key。

---

## 4.6 Agent 运行记录

```text
agent_runs
agent_steps
llm_usage
```

### agent_runs

```text
run_id
tenant_id
space_id
user_id
message_id
run_type
status
started_at
finished_at
error_type
```

### agent_steps

```text
run_id
step_no
step_type
name
status
duration_ms
safe_input_json
safe_output_json
error_type
```

### llm_usage

```text
run_id
model
request_count
input_tokens
output_tokens
total_tokens
estimated_cost
```

---

# 5. Redis 六个场景的具体设计

## 5.1 场景一：限流

目标：防止某个用户、租户或接口耗尽系统和 LLM 额度。

Redis Key：

```text
sxj:{env}:rate:user:{user_id}:ask
sxj:{env}:rate:user:{user_id}:ingest
sxj:{env}:rate:tenant:{tenant_id}:llm
sxj:{env}:rate:global:embedding
```

建议至少支持：

```text
- 每用户每分钟请求数
- 每用户 LLM 并发数
- 每租户每分钟 Token 预算
- 系统全局模型并发上限
```

第一版可以使用 `INCR + EXPIRE` 固定窗口。

正式版建议使用 Lua 实现 Token Bucket 或 Sliding Window，保证多条 Redis 命令原子执行。

策略：

```text
普通消息写入：Redis 故障时可降级，仍写 PostgreSQL Inbox
高成本 /ask 和 /summary：Redis 故障时使用保守本地限制或暂时拒绝
```

---

## 5.2 场景二：消息幂等快速拦截

Redis Key：

```text
sxj:{env}:idem:message:{source}:{message_id}
```

状态：

```text
processing
completed
failed
```

处理方式：

```text
SET key processing NX EX 600
```

Redis 的作用是快速挡住重复请求，但 PostgreSQL 的：

```text
UNIQUE(source, source_message_id)
```

才是最终幂等保证。

Redis 不能作为唯一的幂等数据库。

---

## 5.3 场景三：Redis Streams 任务队列

建议 Stream：

```text
sxj:{env}:stream:ingest
sxj:{env}:stream:query
sxj:{env}:stream:summary
sxj:{env}:stream:memory
sxj:{env}:stream:delivery
sxj:{env}:stream:deadletter
```

Consumer Group：

```text
ingest-workers
query-workers
summary-workers
memory-workers
delivery-workers
```

处理规则：

```text
XREADGROUP 领取任务
→ PostgreSQL 将 task 标记 running
→ 执行业务
→ PostgreSQL 提交最终事务
→ XACK
```

Worker 崩溃：

```text
消息留在 Pending Entries List
→ 超出租约
→ XAUTOCLAIM
→ 其他 Worker 接管
```

重试状态应以 PostgreSQL `tasks` 和 `task_attempts` 为准。

延迟重试可以使用：

```text
PostgreSQL next_retry_at
+ Retry Scheduler 重新发布 Outbox
```

超过最大次数后：

```text
task.status = dead_letter
+ XADD stream:deadletter
```

---

## 5.4 场景四：分布式锁

Redis Key：

```text
sxj:{env}:lock:space:{space_id}
sxj:{env}:lock:memory:{memory_id}
sxj:{env}:lock:scheduler:{job_name}
```

获取锁：

```text
SET key random_token NX PX 30000
```

释放锁必须使用 Lua：

```text
只有 Redis 中的 token 仍等于自己的 token 时才删除
```

长任务需要续租机制。

Redis 锁适合协调多个 Worker，但关键数据正确性仍需要：

```text
PostgreSQL 事务
唯一约束
SELECT ... FOR UPDATE
必要时 PostgreSQL advisory lock
```

### 同一用户消息顺序

分布式锁只能保证“不同时写”，不能自动保证消息顺序。

因此 Inbox 需要：

```text
sequence_no
```

Worker 获取 space lock 后检查：

```text
当前 sequence_no 是否是该空间下一个待处理序号
```

如果前一条还没有完成，则当前任务延后重试。

后续规模继续扩大时，可以将 ingest Stream 按 `hash(space_id)` 分为固定分区。

---

## 5.5 场景五：缓存

建议缓存：

```text
- Memory Search 结果
- Note Semantic Search 结果
- 最近笔记列表
- 用户设置和权限
- Embedding 结果
```

不建议一开始缓存：

```text
- 每个 Agent 的完整最终回答
- 核心写入状态
- Delivery 最终状态
```

### 缓存 Key

```text
sxj:{env}:cache:memory-search:{space_id}:{version}:{query_hash}
sxj:{env}:cache:note-search:{space_id}:{version}:{query_hash}
sxj:{env}:cache:embedding:{model}:{text_hash}
```

### 版本化失效

Redis 维护：

```text
sxj:{env}:cachever:space:{space_id}
```

每次笔记或 Memory 发生变化：

```text
INCR cachever
```

查询缓存 Key 中带当前版本，因此不需要使用危险且低效的通配符扫描删除缓存。

建议 TTL：

```text
Memory/Note 搜索：1～5 分钟
用户设置：5～30 分钟
Embedding：较长 TTL，根据模型版本区分
```

缓存失效时，系统必须仍能从 PostgreSQL 正常查询。

---

## 5.6 场景六：临时会话状态

Redis Key：

```text
sxj:{env}:session:{tenant_id}:{user_id}
```

保存：

```text
current_intent
waiting_for
pending_operation
last_agent_run_id
conversation_summary
expires_at
```

场景：

```text
用户：帮我总结
Agent：总结今天还是一周？
用户：一周
```

Redis Session 让 Agent 知道“一周”是在回答上一步的问题。

建议 TTL：

```text
15～30 分钟
```

长期偏好和长期记忆仍然写入 PostgreSQL Memory，不放在 Session 中。

---

# 6. Agent Hook 设计

## 6.1 当前代码应采用同步 Hook

当前项目整体是同步函数和线程池结构。

本次不要同时进行：

```text
分布式改造
+
全项目 async 重写
```

第一版使用同步：

```text
redis-py sync client
SQLAlchemy sync session
同步 HookManager
```

后续有明确性能瓶颈时再考虑 async。

---

## 6.2 AgentRunContext

建议统一上下文：

```python
@dataclass
class AgentRunContext:
    run_id: str
    tenant_id: str
    user_id: str
    space_id: str
    message_id: str | None
    task_id: str | None
    trace_id: str | None
    run_type: str
    session: dict
    resources: dict
    metadata: dict
```

`resources` 用于暂存：

```text
已获取的锁 token
LLM 并发槽位
幂等 key
缓存信息
```

---

## 6.3 Hook 生命周期

```text
before_agent
before_llm
after_llm
before_tool
after_tool
after_agent
on_error
```

执行顺序：

```text
注册顺序执行 before_*
反向顺序执行 cleanup/on_error
```

---

## 6.4 Hook 实现

### RateLimitHook

位置：

```text
before_agent
before_llm
```

作用：

```text
用户接口限流
LLM 并发限制
租户 Token 预算
```

### IdempotencyHook

位置：

```text
before_agent
after_agent
on_error
```

作用：

```text
processing → completed / failed
```

### SessionHook

位置：

```text
before_agent
after_agent
```

作用：

```text
读取临时会话
更新等待状态
完成后删除无用状态
```

### LlmUsageHook

位置：

```text
before_llm
after_llm
on_error
```

作用：

```text
检查预算
占用并发槽位
记录 token
释放并发槽位
```

### ToolCacheHook

位置：

```text
before_tool
after_tool
```

只缓存明确的只读工具：

```text
memory_search
semantic_search
list_recent
get_note
follow_links
```

写工具完成后：

```text
INCR cachever:space:{space_id}
```

### SpaceLockHook

位置：

```text
before_tool
after_tool
on_error
```

写工具：

```text
save_note
update_memory
forget_memory
purge_memory
consolidate_memory
```

只在真正写数据库时持有锁，不要在整个 Agent 推理期间一直持锁。

### TaskDispatchHook

位置：

```text
before_tool
```

对于耗时工具：

```text
generate_summary
memory_consolidation
large_import
```

可以返回：

```text
queued + task_id
```

并通过 PostgreSQL Outbox 发布到 Redis Streams，而不是在当前请求里同步执行。

### ObservabilityHook

位置：所有阶段。

写入：

```text
agent_runs
agent_steps
llm_usage
结构化日志
```

禁止记录完整用户原文和敏感凭据。

---

## 6.5 Hook 接入当前 Query Agent

当前自然接入点：

```text
answer_question 开始
→ before_agent

complete_json 前
→ before_llm

complete_json 后
→ after_llm

_run_tool 前
→ before_tool

_run_tool 后
→ after_tool

answer_question 返回前
→ after_agent

异常分支
→ on_error
```

工具调用不应继续散落在 `_execute_tool()` 中直接执行。

建议封装为：

```python
hook_manager.run_tool(
    context,
    tool_name,
    args,
    tool_callable,
)
```

统一负责：

```text
缓存
锁
Trace
错误清理
```

---

# 7. 推荐代码结构

```text
apps/
├── receiver.py
├── outbox_relay.py
├── worker_ingest.py
├── worker_query.py
├── worker_summary.py
├── worker_memory.py
├── worker_delivery.py
└── scheduler.py

infrastructure/
├── database.py
├── redis_client.py
├── redis_keys.py
├── redis_lock.py
├── redis_rate_limit.py
└── stream_client.py

repositories/
├── interfaces.py
├── postgres/
│   ├── inbox.py
│   ├── task.py
│   ├── note.py
│   ├── memory.py
│   ├── summary.py
│   └── delivery.py
└── local/
    └── compatibility_backends.py

agent/hooks/
├── base.py
├── manager.py
├── context.py
├── rate_limit.py
├── idempotency.py
├── session.py
├── llm_usage.py
├── tool_cache.py
├── space_lock.py
└── observability.py

runtime/streams/
├── producer.py
├── consumer.py
├── retry.py
└── dead_letter.py

alembic/
scripts/
├── migrate_local_to_postgres.py
├── verify_migration.py
├── load_test_multi_users.py
├── publish_test_tasks.py
└── export_markdown.py
```

---

# 8. 四阶段实施方案

# 阶段一：建立 PostgreSQL 共享数据底座

## 阶段目标

先解决“多台服务器看不到同一份数据”的根本问题。

这一阶段暂时保留当前：

```text
BoundedTaskExecutor
PendingDrainer
单进程运行方式
```

只替换存储，不同时改任务队列，降低一次性风险。

## 主要工作

### 1. 增加基础设施

加入：

```text
PostgreSQL 16
pgvector
SQLAlchemy 2
psycopg 3
Alembic
Docker Compose
```

环境变量：

```text
DATABASE_URL
STORAGE_BACKEND=local|postgres
```

建立数据库连接池和健康检查。

### 2. 建立 Repository 接口

将当前文件函数背后的业务语义抽象出来：

```text
InboxRepository
TaskRepository
NoteRepository
VectorRepository
MemoryRepository
DeliveryRepository
SummaryRepository
```

业务层不再直接依赖：

```text
Path
json.load
sqlite3.connect
```

先实现：

```text
LocalRepository：兼容现有测试
PostgresRepository：新的正式实现
```

### 3. 创建 PostgreSQL 表

至少完成：

```text
users / spaces
inbox_messages / outbox_events
tasks / task_attempts
notes / note_tags / note_relations / note_embeddings
Memory V2 全部表
summary_subscriptions / summary_runs
deliveries / delivery_attempts
agent_runs / agent_steps / llm_usage
```

### 4. 迁移现有数据

脚本必须：

```text
支持 --dry-run
支持重复运行
按 message_id / id 幂等
输出迁移前后数量
输出失败清单
```

迁移来源：

```text
JSONL WAL
index.json
vectors/index.json
SQLite memory.db
Delivery JSON
Summary Subscription JSON
```

### 5. 调整 Markdown 定位

正式读取都从 PostgreSQL。

Markdown 变为：

```text
按需导出
备份
用户下载
演示材料
```

## 阶段测试

### Repository Contract Test

同一套测试分别跑：

```text
LocalRepository
PostgresRepository
```

确认行为一致。

### 迁移测试

构造一套本地数据，迁移后检查：

```text
消息数量一致
笔记数量一致
Memory 来源和版本一致
Delivery 状态一致
Embedding 维度一致
```

### 并发数据库测试

多个线程同时：

```text
写不同 space_id
写相同 message_id
更新同一 memory
reserve 同一 delivery_key
```

验证唯一约束和事务正确。

## 阶段验收

```text
[ ] STORAGE_BACKEND=local 原测试仍通过
[ ] STORAGE_BACKEND=postgres 全部 Repository 测试通过
[ ] 本地数据可幂等迁移
[ ] PostgreSQL 成为主要事实来源
[ ] Markdown/JSON 不再被运行主链路直接查询
```

---

# 阶段二：加入 Redis 协调层和 Agent Hooks

## 阶段目标

先让一个或多个 API 实例能够共享：

```text
限流
幂等
锁
缓存
Session
Agent Hook 状态
```

这一阶段仍可继续使用本地 Executor，暂不切 Redis Streams 主队列。

## 主要工作

### 1. Redis 基础设施

加入：

```text
redis-py
Redis 7
连接池
健康检查
统一 Key Builder
```

环境变量：

```text
REDIS_URL
COORDINATION_BACKEND=local|redis
CACHE_ENABLED=true|false
```

Redis Key 必须包含：

```text
env
tenant_id 或 space_id
业务类型
```

### 2. Agent HookManager

实现：

```text
AgentRunContext
HookManager
before_agent / after_agent / on_error
before_llm / after_llm
before_tool / after_tool
```

先接入 `/ask` Query Agent，再接入 Summary 和 Memory Agent 流程。

### 3. 实现五类 Redis 能力

本阶段实现：

```text
限流
幂等快速拦截
分布式锁
缓存
Session
```

Redis Streams 留到第三阶段。

### 4. 保留 PostgreSQL 最终保证

```text
Redis 幂等 + PostgreSQL UNIQUE
Redis 锁 + PostgreSQL 事务/行锁
Redis 缓存 + PostgreSQL 回源
Redis Session + PostgreSQL 长期记忆
```

### 5. Redis 故障降级

必须明确：

```text
缓存失败 → 跳过缓存
Session 失败 → 无会话模式继续
普通消息限流失败 → PostgreSQL 仍接收消息
关键写锁失败 → 使用 PostgreSQL 锁或拒绝写入
高成本 LLM 限流失败 → 保守拒绝或本地限制
```

## 阶段测试

### Hook 顺序测试

验证：

```text
before 按注册顺序
cleanup/on_error 反向执行
异常时锁和 LLM 并发槽位一定释放
```

### 限流测试

```text
单用户超限
多个用户互不影响
多个实例共享同一个计数
Key 到期后恢复
```

### 幂等测试

10 个并发线程提交相同 message_id：

```text
Redis 只有一个获得 processing
PostgreSQL 只有一条 inbox_message
```

### 分布式锁测试

多个进程同时修改同一 space_id：

```text
同一时刻只有一个进入写区
锁过期后可恢复
错误 token 无法释放别人的锁
```

### 缓存测试

```text
第一次查 PostgreSQL
第二次命中 Redis
写入新笔记后 cache version 增加
旧缓存不能继续命中
```

### Session 测试

```text
多轮对话能恢复意图
不同用户 Session 隔离
TTL 到期后自动清理
```

## 阶段验收

```text
[ ] Query Agent 已经过 HookManager
[ ] 六个 Hook 生命周期可观测
[ ] Redis 限流/幂等/锁/缓存/Session 可用
[ ] Redis 故障不会造成用户笔记丢失
[ ] PostgreSQL 仍是最终事实来源
```

---

# 阶段三：Redis Streams 和多服务器角色拆分

## 阶段目标

把当前“一个进程中同时接收、排队、执行、调度”的结构拆开，使 Worker 可以水平扩容。

## 主要工作

### 1. 拆分应用角色

```text
Receiver / API
Outbox Relay
Ingest Worker
Query Worker
Summary Worker
Memory Worker
Delivery Worker
Scheduler
```

Feishu Adapter 只负责把平台事件转成统一的 Inbox Command。

建议额外提供一个 FastAPI 测试入口，方便压测，不需要创建大量真实飞书账号。

### 2. Transactional Outbox

Receiver：

```text
PostgreSQL 事务写 Inbox + Outbox
```

Relay：

```text
SELECT ... FOR UPDATE SKIP LOCKED
→ XADD Redis Stream
→ published_at
```

多 Relay 实例也不会重复占用同一批 Outbox。

即使重复发布，Task 的 PostgreSQL idempotency_key 也必须挡住重复执行。

### 3. Redis Streams Consumer Group

Worker 需要实现：

```text
读取
标记 running
执行业务
提交 PostgreSQL
ACK
```

增加：

```text
Pending 扫描
XAUTOCLAIM
有限重试
指数退避
Dead Letter Stream
```

### 4. 同一 space_id 顺序和写入协调

初版采用：

```text
Redis space lock
+
PostgreSQL sequence_no
+
数据库事务
```

如果当前任务不是下一个序号：

```text
不执行写入
更新 next_retry_at
稍后重新发布
```

### 5. Scheduler 单实例语义

多个服务器不能各自无条件运行 Scheduler。

可选：

```text
Redis leader lock
```

但真正的每次调度幂等仍由 PostgreSQL：

```text
summary_runs
memory_consolidation_runs
UNIQUE(period_key...)
```

保证。

### 6. Delivery 独立 Worker

业务 Worker 只创建 Delivery Record 和 Delivery Task。

Delivery Worker：

```text
reserve delivery
→ 调用飞书
→ sent / failed / unknown
```

避免分类 Worker 因发送接口慢而被长期占用。

## 阶段测试

### 多 Worker 分发

向各个 Stream 发布 1000 个任务，启动 3～5 个 Worker：

```text
所有任务最终完成
没有重复业务结果
多个 Worker 都有消费记录
```

### Worker 崩溃测试

```text
Worker 已读取但未 ACK
→ kill Worker
→ Pending 留存
→ 其他 Worker XAUTOCLAIM
→ 最终完成
```

### Outbox Relay 崩溃测试

```text
Inbox/Outbox 已提交
Relay 未发布就崩溃
→ 重启后继续发布
```

以及：

```text
Redis 已发布
Relay 未标 published 就崩溃
→ 可能重复发布
→ Task 幂等挡住重复业务
```

### 顺序测试

同一用户快速发送：

```text
我喜欢咖啡
我不喝咖啡了
我更喜欢茶
```

让三条消息进入不同 Worker，最终必须按照 sequence_no 演化。

### Scheduler 多实例测试

同时启动三个 Scheduler：

```text
只有一个获得当前 Leader Lock
即使锁异常，同周期数据库 Run 仍只生成一次
```

## 阶段验收

```text
[ ] Receiver 和 Worker 已完全拆开
[ ] 本地 Executor 不再承担生产队列职责
[ ] Redis Streams 支持 ACK/Pending/Reclaim
[ ] PostgreSQL Outbox 能处理 Redis 短暂故障
[ ] Worker 可横向扩容
[ ] 同一用户写入顺序可验证
```

---

# 阶段四：多用户、多服务器模拟、故障测试和正式切换

## 阶段目标

证明系统不是“看起来支持分布式”，而是真的能够在大量用户、多实例和故障条件下保持正确。

## 主要工作

### 1. 多用户模拟器

新增：

```text
scripts/load_test_multi_users.py
```

使用虚拟：

```text
user_id
space_id
message_id
chat_id
```

不需要真实注册上千个飞书账号。

流量模型建议：

```text
70% 普通笔记写入
20% /ask
5% summary
5% memory 命令
```

用户模型：

```text
普通用户：低频
活跃用户：持续写入和查询
突发用户：短时间大量请求
恶意用户：重复 message_id 和高频 /ask
```

### 2. 本地模拟多服务器

Docker Compose：

```text
redis
postgres
receiver × 2
outbox-relay × 2
ingest-worker × 4
query-worker × 2
summary-worker × 2
memory-worker × 2
delivery-worker × 2
scheduler × 2
```

示例：

```bash
docker compose up --scale receiver=2 --scale ingest-worker=4 --scale query-worker=2
```

这些容器就可以近似模拟多台服务器。

### 3. 分级压力测试

#### 基础级

```text
100 用户
每人 10 条消息
总计 1000 请求
```

#### 中等级

```text
1000 用户
持续 10～30 分钟
```

#### 压力级

```text
5000～10000 虚拟用户
突发和持续流量混合
```

真实 LLM 压测要使用小规模。

大规模工程压测默认使用 Fake LLM 和 Fake Embedding，避免费用和第三方限流干扰。

### 4. Chaos Test

至少测试：

```text
处理过程中 kill Worker
Redis 重启
PostgreSQL 短暂断开
Outbox Relay 重启
Delivery Worker 发送超时
LLM 超时
锁持有者崩溃
重复发布 Stream 消息
Scheduler Leader 切换
```

### 5. 可观测指标

每次测试输出：

```text
submitted
accepted
rate_limited
duplicate
queued
running
completed
failed
pending
dead_letter
retry_count
stream_lag
stream_pending
lock_wait_ms
queue_wait_ms
execution_ms
p50 / p95 / p99 latency
LLM tokens
estimated_cost
```

核心守恒关系：

```text
submitted
=
completed
+ failed
+ pending
+ rate_limited
+ duplicate
```

### 6. 正式切换

建议按开关逐步切换：

```text
STORAGE_BACKEND=postgres
COORDINATION_BACKEND=redis
QUEUE_BACKEND=redis_streams
```

切换前：

```text
迁移全部本地数据
数量核对
抽样内容核对
停写窗口或增量同步
最终切换读取
```

切换后保留：

```text
Markdown 导出工具
本地数据只读备份
回滚说明
```

## 阶段验收

```text
[ ] 1000 用户压测无数据串空间
[ ] 重复消息不会生成重复笔记
[ ] Worker 崩溃任务可接管
[ ] Redis 短暂故障不会丢 Inbox
[ ] PostgreSQL 重连后任务可恢复
[ ] 同一用户记忆演化顺序正确
[ ] Delivery 不重复发送
[ ] p95、失败率和积压指标有真实记录
[ ] Docker Compose 可以一条命令复现多服务器环境
```

---

# 9. 测试体系建议

## 9.1 单元测试

```text
Redis Key 构造
Hook 顺序
限流算法
幂等状态机
锁 token 释放
缓存版本失效
Session TTL
Task 状态机
```

## 9.2 Repository Contract Test

每个 Repository 都有统一行为测试，PostgreSQL 实现必须全部通过。

## 9.3 集成测试

必须使用真实：

```text
Redis 容器
PostgreSQL + pgvector 容器
```

`fakeredis` 可以用于小型单测，但不能替代 Streams、Lua、锁和故障恢复的真实集成测试。

## 9.4 端到端测试

```text
模拟 Receiver 请求
→ Inbox + Outbox
→ Redis Stream
→ Worker
→ Note / Memory
→ Delivery
```

最终检查所有状态。

## 9.5 CI

CI 增加 Service Containers：

```text
postgres
redis
```

执行：

```text
Alembic upgrade head
Ruff
Mypy
Unit Tests
PostgreSQL Integration Tests
Redis Integration Tests
End-to-End Smoke Test
现有五条 Evaluation Dry-run
```

大型压力测试不放在每次 CI 中，可以按手动 Workflow 或 nightly 执行。

---

# 10. 关键安全和隔离要求

## 多租户隔离

所有查询必须带：

```text
tenant_id
space_id
```

Repository 层禁止提供没有空间条件的普通业务查询。

后期可以考虑 PostgreSQL Row Level Security，但第一版先保证应用层条件和测试覆盖。

## Redis Key 隔离

Key 必须包含环境：

```text
sxj:dev:...
sxj:test:...
sxj:prod:...
```

防止测试污染正式数据。

## 数据最小化

Redis 中尽量只保存：

```text
ID
哈希
计数器
状态
短期上下文摘要
```

不要长期保存完整原始消息正文。

## 日志

日志只记录：

```text
space_id
message_id
run_id
task_id
时延
状态
错误类型
```

不要记录用户完整正文、Token、密码和 Redis/PostgreSQL 连接串。

---

# 11. 明确不建议的做法

```text
1. 不要把 PostgreSQL、Redis、Streams、Hook 和 async 一次性全部重写。
2. 不要用 Redis 替代 PostgreSQL 长期保存用户笔记。
3. 不要直接进行 PostgreSQL + Redis 双写而不做 Outbox。
4. 不要把完整消息正文塞进 Redis Streams。
5. 不要只依赖 Redis 锁保证数据正确。
6. 不要让每个 Worker 都无条件启动 Scheduler。
7. 不要为了写简历而做一个没有失效策略的缓存。
8. 不要用真实 LLM 跑上万条压力请求。
9. 不要在没有迁移校验和回滚方案时删除本地数据。
```

---

# 12. 最终完成后的系统能力

完成四个阶段后，随心记应具备：

```text
- PostgreSQL 统一保存大量用户数据
- pgvector 进行 Note 和 Memory 语义检索
- Redis 限流、幂等、锁、缓存和 Session
- Redis Streams 分发多类型后台任务
- 多 Receiver、多 Worker 水平扩容
- Transactional Outbox 防止数据库与队列双写不一致
- Agent 生命周期 Hook
- LLM 并发、Token 和成本控制
- Worker 崩溃任务接管
- Scheduler 多实例幂等
- 多用户、多服务器压力与故障测试
- 可观测的 p50/p95/p99、积压、重试和成本指标
```

---

# 13. 一句话总结

本次改造不是简单把本地文件替换成两个中间件，而是：

> PostgreSQL 负责保存所有可靠事实，Redis 负责协调大量用户和多个服务器，Agent Hooks 把限流、幂等、锁、缓存、会话和用量控制统一插入 Agent 生命周期，Transactional Outbox 和 Redis Streams 负责让后台任务既能扩展又不会丢失。
