# 第三阶段：高并发与任务所有权验收报告

## 结论

第三阶段通过功能验收，但容量目标仍未通过。

任务 lease/fencing、Outbox 三阶段 relay、周期 reclaim、双 Watermark、
按意图一致性、Memory key 锁和全局连接预算均已实现。1000 个不同 space
最终接收并完成 1000 条命令和 2692 个任务，守恒差为 0，失败、defer、
dead letter、Memory gap、Stream lag/pending 均为 0，数据库连接峰值为 27/40。

入口 64 路突发只首次接收 676 条：313 条因 Receiver 连接池 10 秒超时返回
500，11 条返回过载 503。相同 message ID 以并发 8 重试后，补接收 324 条、
识别重复 676 条、失败 0。最终代码已把数据库池超时改成带 `Retry-After`
的 503，但没有掩盖容量不足。1000-space p95 端到端仍为 796.390 秒，
是 120 秒目标的 6.64 倍。

## 测试约束

- 环境：`zcj_hello`
- PostgreSQL、Redis：仅通过 `.env` 的 `DATABASE_URL`、`REDIS_URL` 访问
- Docker / Docker Compose / `DOCKER_HOST`：均未使用
- 多服务器模拟：26 个独立 Python 进程
- 外部依赖：Fake LLM / Embedding，真实 API 调用和费用均为 0
- 数据清理：两个有效测试 tenant、Outbox 和独立 Redis namespace 已删除
- 无效启动 `r1`：环境未导出导致 1000 次连接拒绝，无服务端写入，不计指标

## 实现内容

- Task 增加 `lease_token`、`claim_version`、`claimed_by`、`lease_expires_at`；
  complete/fail/defer/heartbeat 全部使用 fencing。
- Outbox 使用短事务 claim、事务外 Redis publish、token-fenced 完成/失败，
  并支持退避、重试和 poison event。
- Redis Streams 主循环优先读取新消息，周期 reclaim 使用持久游标并记录指标。
- Note 与关键 Memory 分别使用 `note_watermark`、`memory_watermark`；历史/摘要
  等待 Note，当前状态查询等待 Memory，弱一致性查询不设屏障。
- Memory 演化按 `(space_id, memory_key)` 加锁，避免同一事实并发覆盖。
- Redis 故障时启用本地限流和数据库 backpressure；正常 Redis 不再被局部
  Receiver 池瞬时占满误判，数据库池超时统一返回可重试 503。
- PostgreSQL 按进程角色分配连接池，26 进程理论峰值 36，预算 40。

## 验收结果

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 旧 Worker 迟到结果不能改任务 | 通过 | lease token / claim version 并发测试 |
| Redis/Worker 抖动无重复副作用 | 通过 | relay/reclaim 测试；676 条幂等重试 |
| 1000 跨空间最终守恒 | 通过 | 1000 Inbox、2692 tasks 全完成 |
| 单空间必要顺序 | 通过 | 双 Watermark 测试与 100 用户连续消息轮次 |
| 无关 space 不被 Memory 全局阻塞 | 通过 | 1000-space 全程 `blocked=0` |
| 数据库连接不超过预算 | 通过 | 峰值观测 27，预算 40 |

## 容量指标

100 用户、每用户 10 条的连续消息轮次用于观察因果调度；该轮在过载误判
修复前运行，因此作为诊断数据，不代表最终入口代码。

| 指标 | 第二阶段 | 第三阶段 100 用户 | 变化 |
| --- | ---: | ---: | ---: |
| p95 Receiver acceptance | 10009 ms | 9207 ms | 改善 8.01% |
| p95 worker execution | 4949 ms | 6077 ms | 回退 22.79% |
| p95 queue wait | 813221 ms | 997662 ms | 回退 22.68% |
| p95 end-to-end | 815361 ms | 1003616 ms | 回退 23.09% |

最终 1000-space 轮次在过载判断修复后运行：

| 指标 | 结果 |
| --- | ---: |
| 首次 64 路突发接收 | 676 / 1000 |
| 幂等重试补接收 / 重复 / 失败 | 324 / 676 / 0 |
| 最终完成 | 1000 Inbox / 2692 tasks |
| p95 queue wait | 790596 ms |
| p95 worker execution | 6135 ms |
| p95 end-to-end | 796390 ms |
| p95 lock wait | 223 ms |
| PostgreSQL 连接 | 27 / 40 |

跨空间 p95 queue wait 比第二阶段 100 用户混合轮次低 2.78%，端到端低 2.33%，
但工作负载空间分布不同，只能说明移除全局 Memory 串行后略有改善，不能作为
严格同口径加速结论。当前主要瓶颈是 Receiver 仅 6 个理论连接，以及每个任务
在 SSH 反向端口上进行多次 PostgreSQL 事务往返。

## 模型与费用

第三阶段不改变阶段 2 的模型路由：结构化低风险任务默认 `gpt-5.4-mini`，
普通综合默认 `gpt-5.4`，高风险关系审理默认 `gpt-5.5`。本阶段容量与质量
验收全部使用规则模式和 Fake External，LLM token、Embedding 调用和 API
费用均为 0，不能据此声明真实模型成本下降。

## 正确性回归

- 全仓：249 passed；针对性并发测试：14 passed。
- Memory 360 条：关系准确率 100%，破坏性误判 0%，Recall@20 100%，
  MRR 94.17%，端到端准确率 83.33%。
- Ruff：通过。
- Alembic `0005 -> 0004 -> 0005`：通过，最终为 `0005 (head)`。

## 产物

- `docs/metrics/stage3_smoke_10.json`
- `docs/metrics/stage3_clean_capacity_100.json`
- `docs/metrics/stage3_cross_space_1000.json`
- `docs/metrics/stage3_summary.json`
- `docs/memory_eval/stage3.json`

阶段 4 可以开始多租户、安全和迁移加固；容量问题必须继续保留为未通过项，
后续优先减少 Receiver/Worker 的数据库事务往返，再重新跑同口径 100 用户轮次。
