# 消息实验室

入口：http://127.0.0.1:18080/console 。本页说明管理员可观测界面；独立账号登录、用户隔离及本地验收见[多用户控制台](../console-multiuser.md)。

顶部采用紧凑状态栏，画布占据主要屏幕空间。Agent 节点按父子关系缩进并标记层级和所属父节点；展开/收起保持当前节点的屏幕位置，新出现的子节点短暂高亮。

节点流程分成“后端处理”和“Agent 执行”两条泳道，同层调用用实线、跨层调用用虚线箭头连接；分组属于展示层，不改变实际业务流程。

节点流程直接显示计划摘要、工具名称和参数；“展开内容”在节点内展示输出证据，“展开底层调用”显示模型与检索等子节点。Ask V1 的 V2 影子计划及其子调用不展示，也不计入页面节点/模型数量；端到端耗时仍为真实总耗时，原始 JSON 导出保留完整记录。

页面提供消息发送、命令菜单、最近运行、节点调用图、耗时瀑布、后台任务状态、节点输入输出、候选排名、JSON 导出。窄屏点击节点会打开详情抽屉。输入普通文本即保存笔记；输入 `/命令` 自动路由。

## 命令覆盖

- `/ask`、`/type`、`/tag`、`/filter`
- `/summary 今天|昨天|一周|一个月|半年|一年`
- `/summary_auto on|off|status|time HH:MM`（定时结果回到页面历史）
- `/memory list|show|search|profile|pending|approve|reject|edit|resolve|decisions|forget|purge|correct|conflicts|stats|consolidate`
- `/trace latest|<id>|memory <id>`、`/status`、`/feedback`，另提供 `/help`

命令复用原有业务服务。页面入口统一经过 Inbox/Outbox/Redis Worker；不会发送飞书回复。查询、管理、总结各自展示实际调用的节点；未请求模型的命令没有虚构模型节点。ID 操作与 Trace 查询限定到当前空间。

## 启动

在后端根目录运行：

```bash
.venv/bin/python -m alembic upgrade head
.venv/bin/python scripts/run_console.py
```

现有 `.env` 中的 PostgreSQL、Redis、模型配置继续使用。还需要运行当前分支的 `apps.outbox_relay` 和对应 `apps.worker`；现有 `scripts/start_distributed.sh` 维护这些后台角色，**已运行的旧进程需要重启才能加载埋点**。自动总结使用 `apps.scheduler`。

启动脚本默认只监听 `127.0.0.1:18080`，默认使用账号登录。仅在显式设置 `SUIXINJI_CONSOLE_AUTH_MODE=token` 的旧管理员模式下，令牌保存在权限 0600 的 `data/console.token`，也可设置 `SUIXINJI_CONSOLE_TOKEN`；该模式中 `SUIXINJI_CONSOLE_TENANT` 指定租户。账号模式使用 HttpOnly 会话 Cookie，空间和用户身份由后端确定。可用 `SUIXINJI_CONSOLE_PORT` 改端口。

新建表：`lifecycle_runs` / `lifecycle_spans`，迁移 `20261018_0015`。仍在同一个数据库；追踪使用独立的 `lifecycle` 连接池，避免业务事务占满单连接 Worker 时阻塞追踪。

## 追踪语义

- 连接线表示调用或后续任务派生关系，父节点耗时包含子节点；不要相加计算总耗时。
- Outbox/Redis 等待时间由跨进程时间戳计算，多主机运行需要校时；执行耗时使用单调时钟。
- 每次供应商调用单独记录实际模型、角色、任务、路由原因、重试次数、请求消息/参数、原始响应和 token 用量；密钥与认证字段脱敏。
- RRF 节点记录每路输入、k、权重和输出名次/分数；记忆 RRF 包含每路分数贡献。规则重排与 Cross Encoder 重排按实际调用分别记录。
- “Observation”是工具返回、证据与模型可见上下文，以及模型实际返回；不是模型未提供的内部思维。
- 记忆更新节点记录更新前后的状态和版本；领域事件补充冲突、裁决原因。事件没有独立耗时测量时显示“未计时”。
- 无追踪上下文时埋点为空操作；后台任务重试、水位线释放及 Ask V2 线程池均传递上下文。
- 历史最近展示 50 条；完整单次执行可以导出。只对接入后的运行采集，不能补造过去的模型输入输出。

## 验证

见 [validation.md](validation.md)。测试消息位于 `console-e2e-1018`，保留在页面历史供查看。验证用自动总结订阅已关闭。
