# 第四阶段：多租户、安全和迁移验收报告

日期：2026-07-18  
分支：`optimize/stage4-tenant-security-migration`  
迁移版本：`20260718_0006`

## 结论

第四阶段功能验收通过。多租户碰撞、安全入口和迁移回滚路径已经补齐：

- 同一 `source_space_id` 在两个 tenant 下会映射为不同内部 `space_id`；
- 同一 `source_message_id` 在两个 tenant 下可以各自创建 Inbox/Task，不再被全局幂等误判；
- Redis 业务 key 已按 tenant 隔离；
- `/v1/commands` 默认关闭，启用后需要 Bearer token，并忽略请求体 tenant；
- Memory/Decision/Trace/Delivery 等核心时间字段迁移为 `TIMESTAMPTZ`；
- foundation migration 增加 frozen schema fingerprint，避免 baseline 随运行时 schema 静默漂移；
- Compose 和分布式启动脚本改为本机绑定，Compose 本地基础设施密码从 `.env` 获取；
- 删除测试 tenant 后，Inbox/Task/Space 业务行不留孤儿。

## 指标

| 指标 | 第三阶段 | 第四阶段 | 变化 |
|---|---:|---:|---:|
| 全量测试通过数 | 249 | 253 | +4 个回归测试 |
| 跨 tenant message/space 碰撞 | 可能 | 0 | 已阻断 |
| 未鉴权测试 API 访问 | 可进入处理 | 401/404 | 已阻断 |
| Alembic head | `20260718_0005` | `20260718_0006` | 新增迁移 |

本阶段不重新声明吞吐容量提升；Stage 4 的目标是隔离、安全和迁移正确性。

## 验证

- `PYTHONPATH=. conda run -n zcj_hello python -m pytest -q`
  - `253 passed, 5 warnings in 300.23s`
- `PYTHONPATH=. conda run -n zcj_hello ruff check .`
  - passed
- `alembic downgrade 20260718_0005 && alembic upgrade head`
  - passed
- `alembic current`
  - `20260718_0006 (head)`

## 约束

测试过程未执行 Docker、未使用 `DOCKER_HOST`，只通过 `.env` 中的 `DATABASE_URL` 和 `REDIS_URL` 访问已有 PostgreSQL/Redis。测试只删除隔离 tenant 的业务数据，不停止容器、不删除数据卷。

## 说明

`20260717_0001` 当前采用 fingerprint guard 固定 baseline 内容：未来只要运行时 metadata 变化，fresh upgrade 会直接失败，避免静默漂移。更彻底的手写 `op.create_table()` baseline 可以后续单独做，但本阶段已经满足“baseline 不随代码变化而无声改变”的核心风险控制。
