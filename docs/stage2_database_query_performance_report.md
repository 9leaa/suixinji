# 第二阶段：数据库与查询性能验收报告

## 结论

第二阶段通过功能验收，但整体容量目标尚未通过。

数据库微基准取得数量级提升：Top 100 Memory 从 101 次 SQL 降到 2 次，
延迟从 6.839 秒降到 0.658 秒；10,000 条 Note 下六条常用查询路径提升
84.64% 至 92.35%。100 用户干净容量测试的 p95 端到端延迟下降 14.26%，
但仍为 815.361 秒，距离 120 秒目标还有 6.79 倍。剩余主要瓶颈是同空间
任务按 sequence 全局串行，属于第三阶段双 watermark 和任务所有权范围。

## 测试约束

- 环境：`zcj_hello`
- PostgreSQL、Redis：只通过 `.env` 中的 `DATABASE_URL`、`REDIS_URL` 访问
- Docker / Docker Compose / `DOCKER_HOST`：均未使用
- 多服务器模拟：26 个独立 Python 进程
- 外部依赖：Fake LLM / Embedding，无真实 API 调用和费用
- 数据清理：测试租户、Outbox 和独立 Redis namespace 已删除
- 微基准重复数：1；用于前后同口径工程对比，不作统计显著性声明

## 微基准

| 查询 | 基线 | 第二阶段 | 提升 |
| --- | ---: | ---: | ---: |
| Memory Top 100 | 6838.977 ms / 101 SQL | 658.000 ms / 2 SQL | 90.38%，10.39x |
| filter type，10k Notes | 3793.649 ms | 464.932 ms | 87.74%，8.16x |
| filter tag，10k Notes | 6788.853 ms | 519.356 ms | 92.35%，13.07x |
| list recent，10k Notes | 3427.650 ms | 526.419 ms | 84.64%，6.51x |
| get note，10k Notes | 3497.842 ms | 378.090 ms | 89.19%，9.25x |
| follow links，10k Notes | 5291.506 ms | 633.662 ms | 88.02%，8.35x |
| provisional search，10k Notes | 4077.430 ms | 539.104 ms | 86.78%，7.56x |

数据从 1,000 增至 10,000 条时，常用查询的最大延迟倍率从 8.89x 收敛到
1.29x，达到“不线性增长 10 倍”的验收要求。`EXPLAIN ANALYZE` 原始计划
保存在两份微基准 JSON 中。

## 容量对比

| 指标 | 阶段 0 | 第二阶段 | 变化 |
| --- | ---: | ---: | ---: |
| 客户端确认 accepted | 844 | 935 | +10.78% |
| 最终 accepted/completed | 977 | 988 | +1.13% |
| p50 worker execution | 4199 ms | 3461 ms | -17.58% |
| p95 worker execution | 5702 ms | 4949 ms | -13.21% |
| p50 queue wait | 331731 ms | 303859 ms | -8.40% |
| p95 queue wait | 947989 ms | 813221 ms | -14.22% |
| p50 end-to-end | 334384 ms | 305292 ms | -8.70% |
| p95 end-to-end | 951013 ms | 815361 ms | -14.26% |
| p95 outbox publish | 3188 ms | 2802 ms | -12.11% |

最终 988 条已接收请求全部完成，pending、stream lag、memory gap、failure、
defer 均为 0。第一次 `r1` 启动尝试因工具会话结束导致测试 Python 进程退出，
1000 个请求全部在连接前被拒绝、服务端 accepted 为 0；该次没有业务写入，
已清理 namespace，并从容量指标中排除。有效结果来自 `r2`。

## 实现内容

- Memory sources 批量装载，Top 100 从 N+1 收敛为两次 SQL。
- Candidate adjudication 使用单次轻量查询，不加载 sources/versions。
- Note type/tag/recent/get/relations/provisional 改为专用 SQL，不再全空间扫描。
- 增加 Note、Tag、Relation、Memory 查询索引和 Alembic `0004`。
- Memory access count 改为 Redis 聚合、scheduler 单 SQL 批量回写。
- Embedding 按模型与规范化 query 缓存，搜索结果继续使用版本化工具缓存。
- 确定性 Fast Path 覆盖 90% 常见测试问题，ReAct 最大 4 个 decision steps。

## 模型与费用

| 任务 | 路由 | 默认模型 |
| --- | --- | --- |
| 结构化抽取、低风险校验 | fast | `gpt-5.4-mini` |
| 单次查询综合、普通 ReAct | balanced | `gpt-5.4` |
| 高风险关系审理 | strong | `gpt-5.5` |

明确 type/tag/recent 查询为 0 次 LLM；当前状态和普通单跳问题最多一次
`gpt-5.4` 综合；复杂多跳才进入 ReAct。Embedding 缓存测试中，两次等价
query 的外部调用从 2 次降到 1 次。容量和微基准使用 Fake External，实际
LLM token 和 API 费用均为 0，不能据此宣称真实生产费用下降比例。

## 正确性回归

- 全仓：238 passed。
- PostgreSQL repository：9 passed。
- Memory 360 条质量集：关系准确率 100%，破坏性误判 0%，Recall@20 100%。
- MRR：94.17% 降到 92.50%，Recall 不变；同分候选使用随机 ID 排序会产生波动。
- Ruff、`git diff --check`：通过。
- Alembic `0004 -> 0003 -> 0004`：通过。

## 产物

- `docs/metrics/stage2_query_baseline.json`
- `docs/metrics/stage2_query_optimized.json`
- `docs/metrics/stage2_clean_capacity_100.json`
- `docs/metrics/stage2_summary.json`
- `docs/memory_eval/stage2.json`

暂不运行 500/1000 用户容量档。100 用户 p95 仍超目标 6.79 倍，此时扩大
规模只会放大已知的因果串行瓶颈并增加 Mac PostgreSQL 写入。完成第三阶段
双 watermark、lease/fencing 和连接预算后，再恢复 500/1000 用户验收。
