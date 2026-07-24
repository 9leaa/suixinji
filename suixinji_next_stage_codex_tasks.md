# 随心记下一阶段稳定性修复任务书

## 1. 目标

请在仓库 `9leaa/suixinji` 中完成以下五项修复：

1. 修复 Worker 数据库连接竞争与故障写入路径；
2. 修复 Redis Stream 阻塞客户端；
3. 补充任务恢复日志与可观测性；
4. 修复飞书重复事件产生额外用户回复的问题；
5. 参数化 API Host 与 Port。

本阶段不处理 Memory Vector 生命周期，也不修改记忆提取策略。

---

## 2. 当前基线

### 仓库

```text
Repository: 9leaa/suixinji
Current known main commit after lock fix:
c61a7c699a73207e571f95caebee74083db8176b
```

该提交已经修复：

```text
infrastructure/redis_lock.py
```

中的 `coordinated_lock()` 异常边界问题。

后续修改应基于该提交继续，不要回退此修复。

---

## 3. 当前运行架构

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
```

Ubuntu 通过 SSH 隧道连接 PostgreSQL 和 Redis。

当前关键配置：

```dotenv
STORAGE_BACKEND=postgres
COORDINATION_BACKEND=redis
TASK_QUEUE_BACKEND=redis_streams
SUIXINJI_ENV=stage6fresh
```

---

# 4. 任务一：修 Worker 数据库连接竞争与故障写入路径

## 4.1 已知问题

当前普通 Worker 的数据库连接预算原本为：

```python
if resolved.startswith("worker-") or resolved == "scheduler":
    return 1, 0
```

即：

```text
pool_size=1
max_overflow=0
```

但一个 Worker 进程内同时可能有以下数据库使用者：

```text
主线程 handler
任务 lease heartbeat 线程
异常后的 fail_task()
defer_task()
complete_task()
Redis reclaim 后重新 claim_task()
```

已实际出现：

```text
sqlalchemy.exc.TimeoutError:
QueuePool limit of size 1 overflow 0 reached,
connection timed out, timeout 10.00
```

且失败后：

```text
fail_task()
```

也无法取得连接，导致任务错误状态可能无法持久化。

当前本地临时补丁为：

```python
if resolved == "worker-ingest":
    return 3, 1
```

该补丁使任务恢复，但并不代表根因已经解决。

---

## 4.2 排查要求

请完整列出一个 Worker 处理单条任务时所有可能同时存在的数据库连接申请路径。

至少检查：

```text
runtime/streams/worker.py
repositories/postgres/tasks.py
repositories/postgres/dispatch.py
repositories/postgres/memory.py
repositories/postgres/notes.py
apps/handlers.py
memory/service.py
infrastructure/database.py
core/settings.py
```

重点回答：

1. heartbeat 线程在何时申请连接；
2. handler 主线程持有连接的最长区间；
3. 是否存在 Session 嵌套；
4. 是否存在事务跨越 Redis 锁、LLM、embedding 或较长业务计算；
5. handler 出错时，原 Session 是否已经释放；
6. `fail_task()` 是否可能与 heartbeat 同时抢连接；
7. `complete_task()` 是否可能与 heartbeat 同时抢连接；
8. Worker kill 或租约即将过期时，heartbeat 是否可能长期占用连接；
9. 是否存在连接泄漏；
10. 当前总进程数下，数据库全局最大连接占用是多少。

---

## 4.3 设计目标

修复后应满足：

```text
业务主路径
heartbeat
失败持久化
完成持久化
```

之间不会因为最小连接预算而互相饿死。

请优先考虑以下方向，并说明取舍：

### 方案 A：缩短事务范围

确保 repository 层事务只覆盖必要 SQL，不跨越：

```text
Redis 调用
LLM 调用
embedding 调用
锁等待
长时间计算
网络 I/O
```

### 方案 B：复用 Session

对于同一事务中的多个 repository 操作，允许显式传入已有 Session，避免重复申请连接。

### 方案 C：拆分 heartbeat 数据库引擎

heartbeat 使用独立的小型 Engine/Pool，避免与业务连接池互相争抢。

例如：

```text
business engine
heartbeat engine
```

但必须评估总连接预算。

### 方案 D：为故障路径预留连接

确保：

```text
fail_task()
defer_task()
complete_task()
```

在业务连接池压力下仍有可用连接。

可以使用独立 Engine，也可以通过连接池预算设计解决。

### 方案 E：调整进程角色连接预算

允许不同角色有不同预算，例如：

```text
worker-ingest
worker-memory
worker-enrichment
worker-query
worker-delivery
scheduler
```

但不允许简单地给所有进程无限放大连接池。

---

## 4.4 连接预算要求

请给出明确预算表：

| Role | pool_size | max_overflow | 理由 |
|---|---:|---:|---|
| receiver |  |  |  |
| outbox-relay |  |  |  |
| worker-ingest |  |  |  |
| worker-query |  |  |  |
| worker-summary |  |  |  |
| worker-memory |  |  |  |
| worker-enrichment |  |  |  |
| worker-delivery |  |  |  |
| scheduler |  |  |  |
| api |  |  |  |

并计算：

```text
所有进程最大理论连接数
```

该值必须受：

```dotenv
SUIXINJI_DATABASE_GLOBAL_BUDGET
```

约束。

若当前 `DATABASE_GLOBAL_BUDGET` 只是一项配置但没有运行时校验，请补充校验或启动时告警。

---

## 4.5 故障路径要求

必须保证：

```text
handler 抛异常
→ heartbeat 停止
→ 原业务资源释放
→ fail_task() 可取得连接
→ task 进入 retry 或 dead_letter
→ Redis message 被正确 ACK 或保留
```

重点检查当前顺序：

```python
except Exception:
    finish_heartbeat()
    fail_task(...)
```

是否足够。

请明确：

1. heartbeat 停止后是否保证已释放 DB 连接；
2. `heartbeat.join(timeout=1)` 是否足够；
3. join 超时后是否可能仍有数据库调用；
4. lease renewal 失败时是否需要结构化日志；
5. `fail_task()` 失败时如何避免原 Redis message 永久卡住；
6. 是否需要 fallback 故障日志或安全退出策略。

---

## 4.6 测试要求

至少补充以下测试：

### 单任务测试

- handler 正常完成；
- handler 抛异常；
- fail_task 成功；
- complete_task 成功；
- defer_task 成功；
- heartbeat 正常续租；
- heartbeat DB 调用变慢；
- heartbeat DB 调用失败；
- handler 与 heartbeat 同时访问数据库；
- 连接池最小预算下不死锁。

### 并发测试

- 同一 Worker 内连续处理多条任务；
- 多 Worker 并发处理；
- PostgreSQL 延迟注入；
- 连接池耗尽模拟；
- fail_task 在高压力下仍能写入；
- 无连接泄漏；
- 任务最终状态一致。

### 进程恢复测试

- kill Worker；
- 等待 lease 过期；
- 新 Worker 接管；
- 原 Worker 的 stale completion 不覆盖新 Worker；
- task_attempt 正确记录 `lease_expired`。

---

# 5. 任务二：修 Redis Stream 阻塞客户端

## 5.1 已知问题

当前默认配置：

```text
SUIXINJI_REDIS_SOCKET_TIMEOUT_SECONDS=2.0
SUIXINJI_STREAM_BLOCK_MS=5000
```

而 Stream 消费使用：

```python
xreadgroup(..., block=5000)
```

意味着：

```text
socket timeout = 2 秒
阻塞读取 = 5 秒
```

正常空闲等待会提前触发：

```text
redis.exceptions.TimeoutError:
Timeout reading from socket
```

这不是业务错误，却被 Worker 外层记录为 ERROR。

---

## 5.2 设计目标

阻塞消费连接必须满足：

```text
socket read timeout > stream block timeout + safety margin
```

或使用无 socket read timeout 的专用阻塞连接。

推荐优先考虑独立客户端：

```text
普通 Redis Client
├─ lock
├─ cache
├─ publish
├─ health check
└─ 非阻塞命令

Blocking Redis Client
└─ XREADGROUP
```

---

## 5.3 实现要求

请评估并实现以下一种可靠方案。

### 方案 A：独立 Blocking Client

新增：

```python
get_blocking_redis()
```

或类似接口。

要求：

- 独立 ConnectionPool；
- 独立 `socket_timeout`；
- 适配 `STREAM_BLOCK_MS`；
- 连接池数量受限；
- 不影响普通 Redis 命令的快速失败；
- shutdown 时正确 disconnect。

### 方案 B：动态超时

Stream Client 创建时设置：

```text
socket_timeout >= STREAM_BLOCK_MS / 1000 + margin
```

但不要把普通 Redis Client 全局 timeout 一并放大，除非有充分理由。

---

## 5.4 配置校验

请增加启动时校验：

```text
blocking_redis_socket_timeout
>
stream_block_ms / 1000
```

如果配置不合理，应：

- 启动失败；
- 或输出清晰 warning 并自动调整；

两者选其一并解释。

建议引入独立配置：

```dotenv
SUIXINJI_REDIS_BLOCKING_SOCKET_TIMEOUT_SECONDS=7
```

安全余量至少 1 秒。

---

## 5.5 异常语义

必须区分：

```text
正常空闲返回
Redis 连接断开
SSH 隧道断开
Redis 服务不可用
socket 真正超时
NOGROUP
BUSYGROUP
```

正常空闲不应记录 ERROR。

真正连接故障应保留：

```text
task_type
worker_id
stream
group
consumer
block_ms
exception type
retry delay
```

---

## 5.6 测试要求

- Stream 空闲超过多个 block 周期，不产生 TimeoutError；
- 有消息时正常返回；
- Redis 断开时能重连；
- NOGROUP 自动恢复；
- blocking client 与普通 client 使用不同 timeout；
- 多 Worker 各自阻塞读取；
- shutdown 时连接释放；
- SSH 隧道短暂中断后恢复；
- 不产生错误日志风暴。

---

# 6. 任务三：补任务恢复日志

## 6.1 当前问题

任务恢复机制存在，但日志无法清晰回答：

```text
这条任务为什么重新执行？
是谁接管的？
是 retry 重发还是 Redis reclaim？
旧 Worker 是否已经失去 ownership？
这次是第几次 attempt？
```

当前可能存在的恢复路径：

```text
running task lease 过期
retry task 由 scheduler enqueue_due_retries()
outbox-relay 重新发布
Redis XAUTOCLAIM 回收 pending entry
新 Worker claim_task()
旧 Worker stale completion
```

---

## 6.2 目标

对每条任务，日志应能还原完整生命周期：

```text
created
published
stream_received
claimed
started
lease_renewed
lease_renew_failed
handler_completed
handler_failed
retry_scheduled
retry_published
redis_reclaimed
lease_reclaimed
completed
dead_letter
stale_completion
```

---

## 6.3 建议事件名称

至少增加或统一以下结构化事件：

```text
runtime.worker_started
runtime.stream_message_received
runtime.task_claimed
runtime.task_lease_renewed
runtime.task_lease_renew_failed
runtime.task_lease_reclaimed
runtime.stream_pending_reclaimed
runtime.task_retry_scheduled
runtime.task_retry_published
runtime.task_completed
runtime.task_failed
runtime.task_dead_lettered
runtime.task_stale_completion
runtime.outbox_published
runtime.outbox_publish_failed
```

---

## 6.4 每条日志必备字段

```text
pid
hostname
process_role
worker_id
task_id
task_type
message_id
stream
consumer_group
consumer
redis_message_id
attempt_count
failure_count
defer_count
claim_version
lease_token_hash
lease_expires_at
previous_status
new_status
reclaim_reason
retry_at
duration_ms
error_type
```

不要直接记录完整 lease token，可记录哈希或前后几位。

---

## 6.5 进程启动日志

每个分布式进程启动时至少记录：

```text
pid
role
hostname
database pool_size
database max_overflow
redis namespace
stream block_ms
redis socket timeout
code revision
start time
```

这样重启后不会把旧日志误认为新进程日志。

---

## 6.6 状态脚本增强

建议增强：

```text
scripts/status_distributed.sh
```

显示：

```text
role
pid
start time
uptime
log file
log last modified
last structured event
```

但不要让状态脚本依赖数据库或 Redis 才能运行。

---

## 6.7 测试要求

- retry 重发日志；
- lease 过期接管日志；
- XAUTOCLAIM 日志；
- stale completion 日志；
- dead_letter 日志；
- outbox 发布失败和恢复日志；
- Worker kill 后可通过日志还原接管过程；
- 日志中不泄露敏感数据。

---

# 7. 任务四：修飞书重复事件回复

## 7.1 已知问题

飞书可能重复投递相同事件。

当前数据幂等有效，不会重复创建 Inbox 或 Task，但用户会收到额外回复：

```text
已收到，正在整理到随心记。
```

之后又收到：

```text
这条消息已经收到过了，已跳过重复处理。
```

对于 `/ask` 和 `/summary`，情况更明显：

```text
先发送“我去翻一下随心记”
或“我来整理...”
然后才做幂等判断
```

因此重复事件仍会产生用户可见消息。

---

## 7.2 目标行为

首次事件：

```text
正常处理
正常发送一次确认消息
```

重复事件：

```text
静默 ACK
不重复入库
不重复创建任务
不重复发送任何用户可见消息
记录结构化 duplicate 日志
```

---

## 7.3 实现要求

请检查：

```text
bot/feishu_bot.py
apps/feishu_bot.py
```

以实际仓库结构为准。

幂等判断必须发生在任何用户可见回复之前。

推荐流程：

```text
解析 event/message_id
→ receive()/idempotency check
→ duplicate?
   ├─ yes: log + return success
   └─ no: 创建任务 + 发送首次反馈
```

不要只删除“重复消息”那句回复，而保留前置的 `/ask` 或 `/summary` 提示。

---

## 7.4 返回语义

重复事件仍应向飞书平台返回成功 ACK，避免平台继续重试。

需要确认：

- 回调 HTTP 模式；
- 长连接模式；
- SDK handler 返回值；

都不会因静默 return 被视为失败。

---

## 7.5 测试要求

### 普通 ingest

- 首次消息创建一次 Inbox；
- 首次消息发送一次确认；
- 重复消息不新增 Inbox；
- 重复消息不新增 Task；
- 重复消息不产生任何可见回复。

### `/ask`

- 首次消息只发送一次处理中提示；
- 重复消息不再次发送提示；
- 最终 answer 只发送一次。

### `/summary`

- 首次消息只发送一次处理中提示；
- 重复消息不再次发送提示；
- 最终 summary 只发送一次。

### 并发重复事件

- 两个相同 message_id 几乎同时到达；
- 只有一个获得首次处理权；
- 另一个静默结束；
- 不出现竞态重复回复。

---

# 8. 任务五：参数化 API Host 与 Port

## 8.1 当前问题

当前服务器端口：

```text
8000
```

已被其他服务占用。

本地临时修改：

```text
scripts/start_distributed.sh
8000 → 18000
```

该方案不可移植，也不适合长期维护。

---

## 8.2 目标

新增环境变量：

```dotenv
SUIXINJI_API_HOST=127.0.0.1
SUIXINJI_API_PORT=18000
```

默认值建议：

```text
host=127.0.0.1
port=8000
```

部署环境可覆盖为：

```text
port=18000
```

---

## 8.3 实现要求

至少修改：

```text
core/settings.py
scripts/start_distributed.sh
scripts/status_distributed.sh
.env.example
README.md
```

如有测试启动脚本，也应同步。

启动脚本应使用：

```bash
uvicorn apps.api:app \
  --host "$SUIXINJI_API_HOST" \
  --port "$SUIXINJI_API_PORT"
```

并正确处理：

- 环境变量为空；
- 非整数端口；
- 端口范围非法；
- Host 非法；
- 端口已占用。

---

## 8.4 端口占用检查

启动前建议检查：

```text
host:port 是否已监听
```

如已占用：

```text
明确报错
不写入错误 PID 文件
不影响其他角色启动
```

不要尝试 kill 未知进程。

---

## 8.5 状态与健康检查

状态脚本和健康检查必须读取相同配置：

```text
SUIXINJI_API_HOST
SUIXINJI_API_PORT
```

避免：

```text
启动监听 18000
状态脚本仍检查 8000
```

---

## 8.6 测试要求

- 默认端口 8000；
- 自定义端口 18000；
- 自定义 Host；
- 非法端口配置；
- 端口占用；
- 启动失败不残留 PID 文件；
- status 脚本检查正确端口；
- `/health` 正常返回。

---

# 9. 修改边界

本阶段不要处理：

```text
Memory Vector 生命周期
Memory 提取规则
Memory Candidate 判定策略
hybrid retrieval 排序算法
数据库 schema 大改
旧数据迁移
Redis namespace 改动
```

除非上述五项修复确实需要最小兼容性调整。

---

# 10. 实施顺序

严格按以下顺序：

```text
A. Worker 数据库连接竞争与故障路径
B. Redis Stream 阻塞客户端
C. 任务恢复日志
D. 飞书重复事件
E. API Host/Port 参数化
```

每完成一项：

1. 单独提交；
2. 单独测试；
3. 输出 diff；
4. 输出风险；
5. 不要把五项合成一个巨大提交。

推荐提交信息：

```text
fix: harden worker database connection paths
fix: separate redis blocking stream client
feat: improve distributed task recovery logs
fix: silence duplicate feishu events
feat: parameterize api bind address
```

---

# 11. 验收场景

## 场景一：连续发送消息

连续发送至少 20 条飞书消息。

要求：

```text
无 QueuePool timeout
无 generator didn't stop after throw
无 Redis socket timeout
所有 Inbox 最终 processed
所有 note_status 最终 completed/failed
所有 memory_status 最终 completed/failed
```

---

## 场景二：Worker 重启

处理中 kill `worker-ingest`。

要求：

```text
新 Worker 自动接管
日志明确显示 lease/reclaim 路径
任务最终完成
旧 Worker stale completion 不覆盖新结果
```

---

## 场景三：Redis 短暂不可用

短暂断开 SSH Redis 隧道或停止测试 Redis。

要求：

```text
Worker 不退出
不会产生无边界错误日志风暴
恢复连接后继续消费
pending/retry 任务最终完成
```

仅可在测试环境执行，不要影响未知服务。

---

## 场景四：重复飞书事件

重复投递相同 message_id。

要求：

```text
只处理一次
只回复一次
重复事件静默 ACK
```

---

## 场景五：API 自定义端口

设置：

```dotenv
SUIXINJI_API_PORT=18000
```

要求：

```text
API 启动在 18000
status 检查 18000
health 检查 18000
无硬编码 8000
```

---

# 12. 回滚要求

每个提交必须可独立回滚。

特别注意：

- 不删除 PostgreSQL 数据；
- 不清空 Redis；
- 不重建 Consumer Group；
- 不修改现有任务状态；
- 不 kill 未知进程；
- 不覆盖本地 `.env`；
- 不提交 `.env`；
- 保留当前已生效的 `worker-ingest (3,1)` 临时保护，直到新的连接方案验证通过。

正式连接池方案验证通过后，才能决定是否移除临时补丁。

---

# 13. 最终输出要求

每项修复完成后，请输出：

## 13.1 根因

```text
问题为什么发生
```

## 13.2 设计

```text
为什么选择该方案
为什么不选其他方案
```

## 13.3 修改文件

```text
文件路径
关键函数
```

## 13.4 测试

```text
测试命令
测试结果
```

## 13.5 运行验证

```text
实际进程状态
数据库状态
Redis 状态
飞书表现
```

## 13.6 风险

```text
仍可能存在的边界条件
```

## 13.7 回滚

```text
对应提交
回滚命令
配置恢复方式
```

---

# 14. 最终阶段验收标准

全部完成后应满足：

1. Worker 不再因 heartbeat、handler、fail_task 竞争连接而卡死；
2. 故障状态能够可靠写入 PostgreSQL；
3. Redis Stream 空闲等待不再产生 socket timeout；
4. 任务恢复过程可通过结构化日志完整还原；
5. 重复飞书事件不产生额外用户回复；
6. API Host 和 Port 完全由环境变量控制；
7. 所有角色可正常启动、停止和查看状态；
8. 所有修改均有自动化测试；
9. 五项修改各自独立提交；
10. 不影响当前已经跑通的：

```text
飞书
→ Inbox
→ Note
→ Candidate
→ Memory
→ Delivery
```

链路。
