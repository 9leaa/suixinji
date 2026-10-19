# 随心记后端架构全景

直接打开 `canvas.html` 使用交互画布，或打开 `index.html` 阅读完整说明页。画布支持拖拽模块、分组移动、平移缩放、关联高亮、连线详情、6 个场景逐步播放、模拟状态、31 张表浏览和携带布局的 HTML 导出。

原完整说明页：无需联网或安装前端依赖。七个章节涵盖实际服务架构、消息生命周期、31 张数据库表、8 类 Redis Streams、协调与缓存、故障恢复、部署参数及源码边界。所有演示只在浏览器中运行，不触发业务操作。

两个 HTML 页面均内嵌脱敏快照，可独立复制。`snapshot.json` 保留同一份可读采样数据。采样不导出数据库内容、密钥或连接密码。数据量与服务状态均为采样时刻的结果，刷新浏览器不会重新查询后端。

重新采样并更新 HTML：

```bash
cd /Users/zhangchengjie/Projects/suixinji/server
.venv/bin/python docs/backend-explorer/refresh_snapshot.py
```

需要项目 PostgreSQL、Redis 已可访问。脚本只读后端，写入本目录快照与 HTML。数据源：当前 `core.settings`、`infrastructure.schema`、PostgreSQL inspector、Redis PING / INFO / XINFO / XLEN 以及项目 PID 对应进程。

事实核对：当前任务队列为 `redis_streams`，主链路为事务 Inbox / Task / Outbox → Relay → Redis Stream → Worker 数据库认领与租约 → 处理器 → 数据库结果状态 → ACK；失败和延期同样可能在持久化状态后 ACK，再通过新的 Outbox 重试。当前为单机多进程部署，不等于已验证多机高可用。飞书真实收发未验收。
