# 消息生命周期控制台

参考 [Langfuse Agent Graphs](https://langfuse.com/docs/observability/features/agent-graphs) 的执行图与 observation 详情、[Tracing](https://langfuse.com/docs/observability/overview) 的嵌套耗时与输入输出。采用三栏：消息与历史 / 执行图和瀑布 / 节点详情。前端由 FastAPI 同源提供，无 CDN 运行依赖。

真实执行链：FastAPI → Redis 幂等 → PostgreSQL Inbox/Task/Outbox → Relay XADD → Stream Worker 认领 → 分类/笔记 → Memory 候选/冲突裁决/版本更新/异步向量；Ask → 路由/计划 → 工具与每路召回 → RRF → 重排 → 观察/回答 → 页面结果。未走到的分支不伪造为执行节点。

Trace 使用 ContextVar 关联 span，消息 payload 携带上下文，事务内创建的后续任务继续传播；Outbox 传递到 Redis。线程池显式 copy_context。节点开始即持久化，完成后记录单调时钟耗时。父节点耗时包含子节点，不能相加。业务 trace event 无测量时间时为事件，不显示伪造耗时。

复用 DATABASE_URL，新增 lifecycle_runs / lifecycle_spans；原业务表不改结构。追踪仅页面消息开启；无上下文时 instrumentation 为空操作。脱敏认证字段；页面 API 使用独立环境 token（只在浏览器会话内存保存），不暴露数据库或供应商密钥。API 支持历史、详情、JSON 导出与轮询增量刷新。运行状态由真实 Task 状态和活动 span 共同计算，失败/重试/未完成不能显示完成。

普通消息和 Ask 均通过 receive() 和真实队列，Ask 结果存入 lifecycle_runs，页面入口不创建飞书投递任务。默认独立 console 空间，支持输入已有空间 ID。相同数据库不等于相同会话空间。

验证：Context 隔离/跨任务传播/敏感字段、API 鉴权/历史隔离/错误与重试、真实 PostgreSQL+Redis 普通消息→后续 Ask、浏览器发送与节点详情。不会把 mock 测试当成真实模型验收。

补充：入口统一自动识别现有全部命令族，`apps/console_commands.py` 的菜单与路由共用清单；命令业务逻辑复用原有 helper/service。定时总结由 StreamTaskDispatcher 建立稳定 trace ID，重复调度不产生重复业务任务。水位线释放的 SQL CTE 与重试入队也必须传播 `_trace`，不能只修改 receive/enqueue_task。追踪单独使用 lifecycle 连接池，但仍复用数据库。
