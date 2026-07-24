# 随心记 Stage 6 分布式运行问题排查说明

## 1. 目的

请在仓库 `9leaa/suixinji` 中独立排查并修复当前 Stage 6 分布式运行中的稳定性问题。

当前系统已经能够完成：

```text
飞书消息
→ Inbox
→ Note
→ Memory Candidate
→ Memory
→ 飞书反馈
```

但目前存在若干已经暴露的代码缺陷和功能缺口。请不要只扩大连接池或调整环境变量来掩盖问题，需要定位根因、给出可靠修复，并补充测试。

---

## 2. 当前代码版本与部署环境

### 仓库

```text
Repository: 9leaa/suixinji
Main commit inspected: e04da2276b2d5315ec9613e9778fee20289194c3
Stage 6 merge commit: 92f35b3ed7bb0f76b45b2249f71061bf4de97eee
Alembic head: 20260718_0007
```

### 部署拓扑

```text
Ubuntu 服务器
├─ receiver
├─ api
├─ outbox-relay
├─ worker-ingest
├─ worker-query
├─ worker-summary
├─ worker-memory
├─ worker-enrichment
├─ worker-delivery
└─ scheduler

Mac Docker
├─ PostgreSQL 16 + pgvector
└─ Redis 7

Ubuntu 通过 SSH 隧道访问 Mac 上的 PostgreSQL 和 Redis
```

### 当前关键配置

```dotenv
STORAGE_BACKEND=postgres
COORDINATION_BACKEND=redis
TASK_QUEUE_BACKEND=redis_streams
SUIXINJI_ENV=stage6fresh
SUIXINJI_MEMORY_RETRIEVAL_MODE=hybrid
SUIXINJI_MEMORY_HYBRID_VECTOR_ENABLED=true
EMBEDDING_DIMENSION=1024
```

### 数据库与 Redis

使用全新数据库：

```text
suixinji_v2
```

不迁移旧 Note、Memory、Candidate 或向量数据。

Redis 使用独立命名空间：

```text
sxj:stage6fresh:*
```

用于避免旧 Stream、Lock、Cache、Consumer Group 干扰。

---

## 3. 当前运行状态

目前所有分布式角色均能启动：

```text
outbox-relay: running
worker-ingest: running
worker-query: running
worker-summary: running
worker-memory: running
worker-enrichment: running
worker-delivery: running
scheduler: running
api: running
receiver: running
```

API 健康检查：

```text
GET http://127.0.0.1:18000/health
{"status":"ok"}
```

注意：服务器端口 `8000` 已被其他未知服务占用，因此本地将 `scripts/start_distributed.sh` 中 API 端口临时改为了 `18000`。这个修改尚未形成正式代码方案。

---

# 4. 已确认的问题

## P0：`worker-ingest` PostgreSQL 连接池耗尽

### 现象

飞书消息能成功接收并写入 Inbox，Note 也能部分生成，但任务曾停在：

```text
status=pending
note_status=pending
memory_status=pending
```

日志出现：

```text
sqlalchemy.exc.TimeoutError:
QueuePool limit of size 1 overflow 0 reached,
connection timed out, timeout 10.00
```

主要调用链：

```text
apps.handlers.handle_ingest
→ process_note_memory
→ memory.service
→ repositories.postgres.memory.get_extraction_state
→ session.get(...)
→ engine.pool.connect()
→ QueuePool timeout
```

随后任务失败处理本身也无法取得数据库连接：

```text
runtime.streams.worker._handle
→ fail_task
→ repositories.postgres.tasks._owned_running_task
→ session.execute
→ QueuePool timeout
```

### 当前代码相关位置

```text
core/settings.py
infrastructure/database.py
apps/handlers.py
memory/service.py
repositories/postgres/memory.py
repositories/postgres/tasks.py
```

当前 `database_pool_budget()` 对普通 Worker 返回：

```python
if resolved.startswith("worker-") or resolved == "scheduler":
    return 1, 0
```

### 临时处理

服务器本地临时改为：

```python
if resolved == "worker-ingest":
    return 3, 1
if resolved.startswith("worker-") or resolved == "scheduler":
    return 1, 0
```

然后只重启了 `worker-ingest`。

修改后，旧 pending 任务被自动恢复并完成。

最终结果：

```text
Inbox: processed
note_status: completed
memory_status: completed
Note: ready
Memory Candidate: applied
Memory: active
```

示例：

```text
输入：我喜欢喝大窑嘉宾
Memory：用户喜欢喝大窑嘉宾
```

### 需要排查的根因

不要默认最终解决方案就是扩大连接池。请重点检查：

1. `handle_ingest()` 是否在一个仍持有数据库连接或事务的调用中，又进入了新的 `session_scope()`。
2. `process_record()`、`process_note_memory()`、repository 层是否存在嵌套 Session。
3. 是否有 Session/Connection 跨越 LLM 调用、embedding 调用、Redis 锁、文件操作或较长计算过程。
4. 同一业务操作是否应该共享一个 Session，而不是反复申请独立连接。
5. Worker 的任务 lease heartbeat 线程是否也会在同一进程中并发申请数据库连接。
6. `fail_task()` 是否必须保证拥有独立的故障处理连接预算。
7. 当前每个进程的连接预算是否符合数据库全局预算。
8. 是否存在连接泄漏或事务未及时结束。

### 期望修复

应满足以下之一：

- 缩短事务范围，避免长事务持有连接；
- 将嵌套 repository 调用改为复用已有 Session；
- 明确拆分数据库事务和外部调用；
- 为 Worker 正确配置经过测算的最小连接池；
- 或采用组合方案。

需要解释为什么修复后不会在并发或异常路径下再次耗尽。

---

## P0：`coordinated_lock()` 会错误捕获业务异常

### 现象

数据库连接超时后，又出现：

```text
RuntimeError: generator didn't stop after throw()
```

调用链：

```text
with coordinated_lock(...):
    process_note_memory(...)
```

### 当前逻辑问题

`infrastructure/redis_lock.py` 中的逻辑大致为：

```python
if COORDINATION_BACKEND == "redis":
    lock = RedisDistributedLock(key)
    try:
        if lock.acquire(wait_seconds):
            ...
            try:
                yield "redis"
            finally:
                ...
            return
    except Exception:
        ...
    with postgres_advisory_lock(key):
        yield "postgres"
```

问题是：

1. Redis 锁已经成功取得；
2. contextmanager 已经执行到 `yield "redis"`；
3. `with` 块内部业务代码抛出异常；
4. 该异常被外层 `except Exception` 捕获；
5. 函数继续执行 PostgreSQL fallback；
6. contextmanager 第二次 `yield`；
7. 最终触发 `generator didn't stop after throw()`。

这会掩盖原始业务异常。

### 正确行为

仅以下情况可以触发 Redis → PostgreSQL fallback：

- Redis 连接失败；
- Redis 锁获取操作失败；
- Redis 锁等待超时，并且当前任务允许 fallback。

业务代码在获得锁后抛出的异常必须原样向上传播，绝不能触发第二种锁后端。

### 期望修复与测试

请重新设计异常边界，将锁获取、锁内业务执行、锁释放拆开处理。

至少补充测试：

- Redis 获取失败时 fallback；
- Redis 获取成功且业务成功；
- Redis 获取成功但业务抛异常；
- Redis release 失败；
- 非 critical 锁获取失败；
- 任意一次 contextmanager 调用最多只 `yield` 一次。

---

## P1：Redis Stream 阻塞读取与 socket timeout 冲突

### 现象

`worker-enrichment` 日志反复出现：

```text
redis.exceptions.TimeoutError: Timeout reading from socket
```

调用位置：

```text
runtime.streams.client.StreamClient.read
→ xreadgroup(..., block=...)
```

### 当前默认参数

```text
SUIXINJI_REDIS_SOCKET_TIMEOUT_SECONDS = 2.0
SUIXINJI_STREAM_BLOCK_MS = 5000
```

也就是：

```text
socket timeout = 2 秒
XREADGROUP block = 5 秒
```

当 Stream 没有新消息时，Redis 客户端会在正常阻塞结束前先触发 socket timeout。

### 当前影响

Worker 外层会捕获异常并继续循环，因此不会永久退出，但会导致：

- 大量错误日志；
- Redis 连接反复断开重建；
- 无任务时也被记录成错误；
- 增加 Redis 和 SSH 隧道负担；
- 降低 Worker 稳定性和可观测性。

### 期望修复

请评估合理方案：

1. 阻塞读取使用独立 Redis Client/ConnectionPool，并设置更长 socket timeout；
2. socket timeout 至少大于 `STREAM_BLOCK_MS / 1000`，并留安全余量；
3. 或禁用阻塞连接的 socket read timeout；
4. 非阻塞命令仍保留较短 timeout；
5. 正常的空闲阻塞读取不能记录为 ERROR。

建议增加配置校验：

```text
redis_socket_timeout > stream_block_seconds + safety_margin
```

---

## P1：重试、租约回收和日志可观测性需要核验

### 已观察行为

旧 `worker-ingest` 失败后被停止，新 Worker 启动后，旧任务最终恢复。

相关机制可能包括：

```text
Redis XAUTOCLAIM
PostgreSQL task lease expiration
scheduler.enqueue_due_retries()
Outbox 重发
```

### 需要确认

1. 失败任务实际通过哪条路径恢复。
2. `retry` 状态是否一定能被 Scheduler 再次写入 Outbox。
3. Redis pending entry 和 PostgreSQL Task 状态是否可能不一致。
4. Worker 进程被 kill 时，lease、pending entry、重复执行和幂等保护如何工作。
5. 任务达到 `dead_letter` 后 Inbox 状态是否正确结束。
6. 恢复过程中是否可能同时被旧 Worker 和新 Worker 执行。
7. 日志是否能明确显示 task_id、message_id、attempt、lease、reclaim、retry、completed。

### 日志问题

重启 Worker 后执行：

```bash
tail -n 40 data/logs/worker-ingest.log
```

看到的是旧进程遗留错误，容易被误认为新 Worker 仍在报错。

建议考虑：

- 每次进程启动输出启动时间、PID、role、pool budget；
- 日志按进程启动或日期轮转；
- 状态脚本显示日志最后更新时间；
- 错误日志包含当前 PID。

---

## P2：飞书重复事件会产生多余用户回复

### 现象

同一个飞书消息事件被重复投递时，数据库幂等保护有效，不会重复写入。

但机器人会依次回复：

```text
已收到，正在整理到随心记。
```

以及：

```text
这条消息已经收到过了，已跳过重复处理。
```

### 问题与建议

数据层面没有重复，但用户会看到额外回复。重复事件应默认静默 ACK，不再发送用户可见消息。

请检查：

```text
apps/feishu_bot.py
```

并补充测试：

- 首次事件正常回复；
- 同 message_id 重复事件不重复写入；
- 重复事件不重复回复；
- 必要时仅记录结构化日志。

---

## P2：Memory Vector 生命周期尚未确认完整

### 当前已经确认

数据库中存在：

```text
memory_vectors
```

并已启用：

```dotenv
SUIXINJI_MEMORY_RETRIEVAL_MODE=hybrid
SUIXINJI_MEMORY_HYBRID_VECTOR_ENABLED=true
EMBEDDING_DIMENSION=1024
```

数据库也有 pgvector 与 HNSW 索引。

### 尚未确认

目前没有确认以下完整生命周期真实存在并可工作：

```text
Memory 创建
→ 自动创建 vector job
→ 生成 embedding
→ memory_vectors.status=ready
→ Memory 内容变化时刷新向量
→ 失败重试
→ 旧向量失效或替换
→ hybrid retrieval 实际使用 ready 向量
```

目前成功生成了 Memory，但不能据此证明 `memory_vectors` 已自动写入。

### 需要排查

1. Memory 创建后是否自动 enqueue embedding。
2. 谁负责写 `memory_vectors`。
3. embedding model、dimension、content hash 是否持久化。
4. Memory update、merge、supersede 后如何处理旧向量。
5. embedding 失败是否重试。
6. hybrid retrieval 在无向量时是否安全降级。
7. 是否存在 backfill 命令。
8. 是否有 vector freshness 检查。

需要明确结论：已实现但有 bug、仅部分实现，或尚未实现。不要仅根据表和索引存在就判断功能完整。

---

## P2：本地部署修改尚未固化

当前服务器存在未正式固化的本地修改。

### API 端口

```text
scripts/start_distributed.sh
8000 → 18000
```

原因：服务器端口 `8000` 已被其他进程占用，且当前用户无 sudo 权限确认来源。

建议改成环境变量：

```dotenv
SUIXINJI_API_HOST=127.0.0.1
SUIXINJI_API_PORT=18000
```

### Ingest 连接池

```text
core/settings.py
worker-ingest: (1, 0) → (3, 1)
```

这是临时补丁，尚未经过根因验证、测试或提交。

---

# 5. 已通过的端到端验证

## 测试一

```text
我喜欢喝大窑嘉宾
```

结果：

```text
Inbox:
  status=processed
  note_status=completed
  memory_status=completed

Note:
  status=ready

Memory Candidate:
  content=用户喜欢喝大窑嘉宾
  status=applied

Memory:
  content=用户喜欢喝大窑嘉宾
  status=active
```

## 测试二

```text
我喝牛奶
```

结果：

```text
Inbox:
  status=processed
  note_status=completed
  memory_status=completed

Note:
  status=ready
```

没有生成 Memory，但任务已完成。这可能是提取规则判断结果，不是卡死。

## 测试三

```text
啊啊啊你好了吗？
```

结果：

```text
Inbox:
  status=processed
  note_status=completed
  memory_status=completed

Note:
  status=ready
```

没有生成 Memory，符合预期。

---

# 6. 请重点检查的文件

```text
core/settings.py
infrastructure/database.py
infrastructure/redis_client.py
infrastructure/redis_lock.py

apps/handlers.py
apps/worker.py
apps/scheduler.py
apps/outbox_relay.py
apps/feishu_bot.py

runtime/streams/client.py
runtime/streams/worker.py
runtime/stream_dispatcher.py

repositories/postgres/tasks.py
repositories/postgres/dispatch.py
repositories/postgres/memory.py
repositories/postgres/notes.py

memory/service.py
memory/extractor.py
memory/retrieval.py

scripts/start_distributed.sh
scripts/status_distributed.sh
```

---

# 7. Codex 排查要求

## 第一阶段：只读排查

先不要修改代码，输出：

1. 连接池耗尽的准确根因；
2. 所有同时申请 PostgreSQL 连接的执行路径；
3. `coordinated_lock()` 异常传播问题的确认；
4. Redis timeout 与 Stream block 的配置冲突确认；
5. 任务失败后恢复路径；
6. Memory Vector 生命周期的现状；
7. 每个问题的严重级别；
8. 推荐修复顺序。

## 第二阶段：修复方案

给出最小且长期可靠的修复方案，说明：

- 修改哪些文件；
- 为什么这样改；
- 是否改变事务语义；
- 是否影响幂等性；
- 是否影响任务租约；
- 是否影响数据库全局连接预算；
- 是否兼容单进程模式；
- 是否兼容分布式模式。

## 第三阶段：实现与测试

至少需要包含以下测试。

### PostgreSQL

- 单连接预算下不发生无意嵌套耗尽；
- Ingest 正常路径；
- Ingest Memory 路径；
- 业务异常路径；
- `fail_task()` 能可靠持久化失败状态；
- Worker kill 后任务可恢复；
- 多个任务并发时无连接泄漏。

### Redis Lock

- Redis 获取成功；
- Redis 获取失败后 fallback；
- 锁内业务异常原样传播；
- 只 yield 一次；
- release 异常行为明确。

### Redis Streams

- 无消息时正常阻塞；
- 不产生 socket timeout 错误；
- pending message reclaim；
- retry task 再发布；
- consumer 重启恢复。

### 飞书幂等

- 重复事件不重复写入；
- 重复事件不产生第二条用户回复。

### Memory Vector

- 新 Memory 自动生成向量；
- 更新 Memory 后向量刷新；
- embedding 失败重试；
- 无 ready 向量时 hybrid 安全降级；
- 维度不匹配时明确失败。

---

# 8. 验收标准

修复后应满足：

1. 不依赖无限增大数据库连接池才能完成 Ingest。
2. 连续发送多条飞书消息不会出现 QueuePool timeout。
3. 业务异常不会触发 `generator didn't stop after throw()`。
4. Worker 空闲时不再出现 Redis `Timeout reading from socket`。
5. Worker 重启后 pending/retry 任务能自动恢复。
6. Task、Inbox、Outbox、Redis Stream 状态最终一致。
7. 重复飞书事件不产生额外用户回复。
8. Memory Vector 生命周期有明确实现和测试，或者明确标记为未完成并给出实施计划。
9. API 端口可通过环境变量配置。
10. 所有修复有自动化测试，不只依赖手工验证。

---

# 9. 安全约束

排查和修复时请遵守：

- 不删除旧数据库；
- 不重置或清空 PostgreSQL；
- 不执行 Redis `FLUSHALL` 或 `FLUSHDB`；
- 不删除未知 tmux、session 或 process；
- 不停止与本项目无关的服务；
- 不重写已经成功处理的业务数据；
- 不直接修改生产任务状态，除非先给出可回滚方案；
- 对所有配置和代码修改保留备份或使用 Git 分支。

---

# 10. 最终需要 Codex 输出

请最终生成：

1. 根因分析；
2. 修复设计；
3. 代码 diff；
4. 测试结果；
5. 数据库连接预算说明；
6. 任务恢复机制说明；
7. Memory Vector 实现状态说明；
8. 部署与回滚步骤；
9. 仍未解决的风险列表。
