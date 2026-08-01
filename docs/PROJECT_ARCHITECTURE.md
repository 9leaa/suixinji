# 随心记项目架构与文件索引

> 本文依据当前仓库受 Git 管理的文件编写，目标是帮助维护者快速定位功能边界、调用链和实现位置。它描述的是代码与受控文档，不把 `data/` 中的用户运行数据、缓存、日志、备份和未跟踪评测草稿当作源码。
>
> 入口说明见 [README](../README.md)，详细设计决策见根目录的 `DESIGN.md` 与 `SUIXINJI_*_DESIGN.md`。本文不包含环境变量的实际值、飞书凭据或用户数据。

## 阅读约定

本文的文件索引是“文件在整个项目中的职责”的权威入口：先按目录定位边界，再进入源码阅读模块顶部说明。Memory V3 和查询主链路的源码使用统一注释约定：

- **模块 docstring**：说明该文件处于哪一条调用链、上游输入和下游边界；
- **函数 docstring**：使用 `Args` 解释传参含义，使用 `Returns` 解释结果结构和副作用；可能向上传播的异常写在 `Raises`；
- **持久化/安全函数**：特别标注写入、幂等、敏感信息处理和回退边界；
- **实现注释**：只解释不能从代码直接读出的设计约束，不重复翻译变量名。

例如，排查“新消息已保存但长期记忆尚未可查询”时，应从 `agent/query_agent.py` 的因果屏障调用进入 `memory/consistency.py`，再检查 `memory/service.py` 的抽取状态和 `memory/consolidator.py` 的候选演化；不要直接修改展示层命令。

## 1. 系统目标和运行模式

随心记是一个以飞书消息为主要入口的个人记录与记忆 Agent。它将原始消息保存为可检索 Note，异步抽取可审计的长期 Memory，并支持问答、复杂查询、任务状态、用户画像、周期总结和人工记忆管理。

系统有两套可切换的执行/存储后端：

| 模式 | 关键开关 | 存储与调度 | 适用场景 |
| --- | --- | --- | --- |
| 本地模式 | `STORAGE_BACKEND=local`、`TASK_QUEUE_BACKEND=local` | Markdown/JSON、SQLite memory repository、WAL、进程内有界任务执行器 | 开发、单机使用、快速验收 |
| 分布式模式 | `STORAGE_BACKEND=postgres`、`TASK_QUEUE_BACKEND=redis_streams` | PostgreSQL 事务表、Outbox、Redis Streams、独立 receiver/worker/scheduler 进程 | 生产部署、可靠重试、多角色扩展 |

核心设计原则：原始 Note 是事实来源；Memory 是可演化的结构化派生信息；所有 LLM 写入经过候选、校验、裁决和 Trace；跨进程任务通过 Inbox/Task/Outbox 的事务边界实现幂等和可恢复。

## 2. 总体架构

### 2.1 写入链路

```text
飞书 IM 事件 / 本地 CLI
  -> bot/feishu_bot.py 或 main.py
  -> 敏感内容拦截、命令识别、幂等入口
  -> 本地：core/wal.py -> core/worker.py -> runtime/executor.py
  -> 分布式：apps/receiver.py -> PostgreSQL Inbox + Task + Outbox
       -> apps/outbox_relay.py -> Redis Stream -> apps/worker.py
  -> apps/handlers.py: ingest
  -> Note 持久化、分类、向量/富化
  -> memory/service.py
  -> extractor -> validator -> consolidator/adjudicator/relation guard
  -> evolution -> Memory/Version/Source/Relation
  -> trace.py / AgentRun 审计记录
```

写入时先确保 Note 可用，再将可能涉及长期记忆的消息送入 Memory 工作。分布式任务通过空间顺序水位线，防止后到消息绕过尚未完成的前序消息；Memory 写入使用按 memory key 的锁，避免并发合并相互覆盖。

### 2.2 查询链路

```text
飞书 /ask 或 API query 任务
  -> agent/query_agent.py
  -> hooks: 幂等、会话、限流、空间锁、缓存、可观测性、LLM 用量
  -> query_route_features.py: 规则特征与复杂度
  -> query_intent.py: 查询意图/路由（必要时 LLM）
  -> query_planner.py: 改写、子句分解、step-back，且限制预算
  -> Note 语义/关键词混合检索 + Memory 混合检索
  -> 来源去重、覆盖检查、ReAct/LLM 综合回答
  -> 引用最多 5 条 Note + 5 条 Memory，写入 Query Trace
```

复杂问题既会检索 Memory，也会回补 Note。前者提供稳定偏好、任务和事实状态；后者保留一次性事件和完整证据。回答来源按证据类型分别限量，不能为了凑数填充。

### 2.3 分布式角色

| 角色 | 进程入口 | 职责 |
| --- | --- | --- |
| Receiver | `bot.feishu_bot` / `apps.receiver` | 接收飞书事件或测试命令，原子创建 Inbox、Task、Outbox |
| Outbox relay | `apps.outbox_relay` | 认领数据库 Outbox，将任务发布至 Redis Stream |
| Worker | `apps.worker` + `runtime.streams.worker` | 按任务类型消费 Stream、租约续期、重试/死信和回收 |
| Scheduler | `apps.scheduler` | 触发自动总结、记忆合并、过期/向量生命周期维护 |
| API | `apps.api` | 提供受鉴权和限流保护的测试/接入命令端点 |

## 3. 关键数据与一致性边界

### 3.1 本地文件数据

| 位置 | 内容 | 写入方式 |
| --- | --- | --- |
| `data/wal/` | 接收但尚未处理或恢复中的消息 | `core/wal.py` 追加与去重，`core/worker.py` 消费 |
| `data/notes/<space>/` | Note Markdown、索引 JSON、摘要 | `storage/note_storage.py` 在 space 文件锁内原子更新 |
| `data/vectors/` | Note 向量及其索引 | `storage/vector_store.py` 序列化并按相似度检索 |
| `data/memory/` | SQLite 记忆库、Trace、调度状态 | `memory/repository.py` 和 `memory/trace.py` 管理 |
| `data/deliveries/` | 本地发送预约/发送状态 | `runtime/delivery_store.py` 管理，避免重复发飞书消息 |

当前版本还跟踪了两份早期运行产物：`data/summary_subscriptions.json` 是本地自动总结订阅状态的示例/遗留快照；`data/cache/改状态.py` 是历史人工调试脚本，名称虽在 cache 目录内，但不属于当前运行时导入链。两者都不应作为生产逻辑修改入口。

### 3.2 PostgreSQL 实体

`infrastructure/schema.py` 定义 ORM：`Tenant`、`User`、`Space`、`SpaceMember` 为租户边界；`InboxMessage`、`OutboxEvent`、`Task`、`TaskAttempt` 形成可靠任务链；`Note`、`NoteTag`、`NoteRelation`、`NoteEmbedding` 保存原始记录；`Memory`、`MemoryCandidateRow`、`MemorySource`、`MemoryVersion`、`MemoryVector`、`MemoryExtractionState`、`MemoryConsolidationRun`、`MemoryDecision`、`MemoryRelation`、`MemoryTrace` 保存长期记忆及审计；`Summary*`、`Delivery*` 管理发送；`AgentRun`、`AgentStep`、`LlmUsage` 用于查询和模型观测。

### 3.3 可靠性机制

- **接入幂等**：飞书事件 ID/消息 ID 是入口幂等键；本地 WAL 和数据库 Inbox 都拒绝重复。
- **事务投递**：分布式模式在同一数据库事务创建 Inbox、Task、Outbox，relay 失败可重放 Outbox。
- **任务租约**：Stream worker 认领任务、记录 attempt 和 lease；超时任务可以被 reclaim。
- **因果顺序**：`dispatch.py` 对同一 space 使用 sequence/watermark，只激活可安全执行的下一阶段。
- **外部发送**：Delivery reservation 防止重试造成重复飞书消息；不确定超时标记为 `unknown` 而不是盲目重发。
- **LLM 边界**：普通调用与 Memory extraction 各有超时；抽取默认 30 秒，仅 `APITimeoutError` 最多重试一次；失败写脱敏结构化日志并降级，不会伪造候选。

## 4. 根目录、构建和入口文件

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `main.py` | 本地命令行记录入口 | 用时间戳生成本地消息 ID，写 WAL 后调用 `process_pending`；不依赖飞书。 |
| `README.md` | 产品说明、飞书展示、启动与能力边界 | 面向使用者的主文档，引用 `docs/images/feishu/` 截图。 |
| `README_SUIXINJI_CLEAN.md` | 精简版/历史 README 文案 | 保留早期的项目介绍，主入口以 `README.md` 为准。 |
| `DESIGN.md` | 初始产品与工程设计 | 记录原始需求、阶段目标及本地实现约束。 |
| `SUIXINJI_COMPLEX_QUERY_ROUTING_AND_DECOMPOSITION_DESIGN.md` | 复杂查询设计 | 定义路由特征、子问题分解、改写和预算约束。 |
| `SUIXINJI_MEMORY_V3_REDESIGN_PLAN.md` | Memory V3 设计/迁移计划 | 说明候选 schema、canonical key、relation guard 与 shadow rollout。 |
| `SUIXINJI_NOTE_AND_LONG_TERM_MEMORY_RETRIEVAL_DESIGN.md` | Note/Memory 联合检索设计 | 规定双证据源、混合检索、RRF 和来源展示策略。 |
| `SUIXINJI_TASK_STATE_RECONCILIATION_DESIGN.md` | 任务状态对账设计 | 规定任务抽取、状态变更与冲突解决原则。 |
| `P1_test.py` | 早期 P1 手工/冒烟脚本 | 直接调用本地核心能力，属于开发辅助而非 pytest 主套件。 |
| `connect_test.py` | 外部连接连通性辅助脚本 | 用于检查模型/服务配置，不是运行时入口。 |
| `requirements.txt` | 生产 Python 依赖 | 固定飞书 SDK、OpenAI 兼容客户端、SQLAlchemy、Redis、FastAPI 等版本。 |
| `requirements-dev.txt` | 开发与 CI 依赖 | 在生产依赖上追加 pytest、coverage、ruff、mypy。 |
| `pyproject.toml` | 工具配置 | 设置 Ruff 规则、pytest 搜索范围、coverage 覆盖源码包。 |
| `.env.example` | 环境变量模板 | 记录飞书、模型、数据库、Redis、并发、重试、Memory/Query feature flag 默认值。 |
| `.gitignore` | Git 忽略规则 | 排除密钥、运行数据、缓存、虚拟环境和构建产物。 |
| `.github/workflows/ci.yml` | GitHub Actions 持续集成 | 在 Python 3.10/3.11 矩阵中启动 PostgreSQL/Redis，依次执行 Ruff、Alembic migration、带 63% 覆盖率阈值的 pytest 和评测 dry-run。 |
| `Dockerfile` | 应用容器镜像 | 基于 Python 3.11 slim 安装生产依赖并默认启动飞书 bot。 |
| `docker-compose.yml` | 本地基础设施与分布式编排 | 定义单体容器、PostgreSQL/Redis 及 receiver、relay、各类 worker、scheduler、API profile。 |
| `Makefile` | 常用运维命令 | 包装安装、测试、dry-run、迁移、启动/停止、分布式和 Stage 4 验证命令。 |

## 5. 飞书接入与应用层

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `bot/__init__.py` | 飞书包标记 | 仅使目录成为可导入 package。 |
| `bot/feishu_bot.py` | 飞书事件入口、命令 UI、文本发送 | 用 Lark SDK 校验配置、解析消息/mention/sender，构造 `space_id`；处理记录、`/ask`、总结、反馈及 memory/trace 命令；敏感消息先拦截；根据队列后端走 WAL/本地 executor 或 `apps.receiver`；`safe_send_text` 隔离发送失败。 |
| `apps/__init__.py` | 应用角色包标记 | 无业务逻辑。 |
| `apps/receiver.py` | 平台无关的接收适配层 | `InboxCommand` 将外部命令规范化，调用 PostgreSQL `receive_command`，在事务内投递。 |
| `apps/handlers.py` | 分布式 task handler 注册表 | 实现 ingest/query/summary/memory/enrichment/delivery：写 Note 后派生 memory/enrichment 任务，查询/总结后创建 delivery，向量任务处理 embedding，使用 lock 和 `TaskOutcome` 推进水位线。 |
| `apps/outbox_relay.py` | Outbox 到 Redis Stream 的 relay 进程 | 循环认领 outbox 事件、调用 Stream client 发布、成功/失败更新 Outbox 状态并按轮询间隔重试。 |
| `apps/worker.py` | 分布式 worker CLI 入口 | 按命令行 task type 取得 `HANDLERS` 并启动 `AdaptiveStreamWorker`。 |
| `apps/scheduler.py` | 分布式 scheduler 入口 | 启动 Memory 和 Summary 定时器，并进行恢复/协调。 |
| `apps/api.py` | FastAPI 接入/测试 API | 提供 `/health` 与 `/v1/commands`；通过 bearer token/测试上下文鉴权，按动作限流，再转换为 `InboxCommand`。 |

## 6. `core/`：通用领域服务

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `core/__init__.py` | 核心包标记 | 无业务逻辑。 |
| `core/settings.py` | 全局配置和 feature flags | 从环境读取后进行类型/范围归一化；统一暴露后端选择、队列、并发、LLM、检索、Memory V3 和阶段开关。 |
| `core/config.py` | LLM 与 embedding provider 配置 | 汇集 API key、base URL、模型、维度等连接配置。 |
| `core/model_policy.py` | 模型能力策略 | 定义 fast/balanced/strong 的任务角色与升级条件。 |
| `core/model_router.py` | 模型路由 | 根据 task 类型、复杂度和策略选择模型，记录路由原因，支持 feature flag 降级。 |
| `core/llm_client.py` | OpenAI-compatible LLM/embedding 客户端 | 提供文本/JSON completion 与 embedding；统一超时、模型路由、JSON 解析和 usage 记录；Memory extraction 使用独立 30s timeout 且只对 `APITimeoutError` 重试一次，失败结构化脱敏记录。 |
| `core/classifier.py` | Note 分类 | 用规则或 LLM 生成标题、类型、标签、摘要，提供不依赖 LLM 的回退值。 |
| `core/taxonomy.py` | 分类词表和规则 | 集中维护 Note 类型、标签映射和归一化辅助函数。 |
| `core/sensitive.py` | 敏感信息识别和日志脱敏 | 规则检测敏感内容，生成可安全展示的短预览，阻止不应持久化/发送给 LLM 的输入。 |
| `core/wal.py` | 本地写前日志 | 创建 pending/blocked record、按消息幂等追加、列举/加载/完成本地消息，支持进程重启恢复。 |
| `core/worker.py` | 本地记录处理流程 | 消费 WAL record，分类、保存 Note、入向量、触发/延后 Memory 和富化，并更新 pending 状态。 |
| `core/file_lock.py` | 本地 space 文件锁 | 用锁文件/上下文管理器串行化同一 space 的 JSON/Markdown 更新。 |
| `core/observability.py` | 本地结构化可观测性 | 写 JSONL 事件，保存最近成功、错误和处理阶段；供飞书命令查看。 |
| `core/feedback.py` | 用户反馈持久化 | 以按 space 的 JSONL 保存问答评价/纠正信息，供后续调优。 |

## 7. `storage/`：本地 Note 与向量存储

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `storage/__init__.py` | 本地存储包标记 | 无业务逻辑。 |
| `storage/note_storage.py` | 本地 Note 生命周期与检索 | 将原文写为 Markdown，维护 JSON 索引；提供按 ID、标签、类型、时间、关键词与可查询状态读取，并处理富化结果。 |
| `storage/vector_store.py` | 本地 Note 向量检索 | 生成/保存向量记录，使用 embedding 余弦相似度，并与词法结果做混合/RRF 排序。 |

## 8. `memory/`：长期记忆子系统

### 8.1 模型、抽取与校验

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `memory/__init__.py` | Memory 包标记 | 无业务逻辑。 |
| `memory/models.py` | Memory 领域模型与常量 | 定义 Memory、Candidate、Decision、Source、状态/类型集合、ID 生成、内容和 key 归一化。 |
| `memory/prompts.py` | Memory LLM prompt | 集中维护抽取、关系和裁决的结构化提示词。 |
| `memory/extraction_schema.py` | 抽取结果 schema | 用 Pydantic/显式校验约束 LLM JSON 的候选字段、类型、置信度和来源。 |
| `memory/clause_splitter.py` | 消息子句切分 | 按中文/英文标点和连接词切出候选子句，保留位置与原文证据。 |
| `memory/extractor.py` | Memory 候选抽取 | 组合规则、LLM、hybrid 模式；按句抽取偏好/任务/语义/事件候选，记录 extraction state，LLM 失败返回安全空/降级结果。 |
| `memory/candidate_validator.py` | 候选安全与质量验证 | 检查 schema、敏感信息、最小内容、置信度、类型合法性、来源对齐和无意义重复，输出接受项与拒绝原因。 |
| `memory/canonicalizer.py` | 稳定身份和 canonical key | 将同义、时态、任务主体等归一为稳定 key，减少相同任务/偏好的重复记忆。 |
| `memory/shadow.py` | V3 shadow 对比 | 在不改变正式写入的前提下运行/记录 V3 方案输出，用于渐进验证。 |

### 8.2 检索、关系与裁决

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `memory/retrieval_models.py` | Memory 检索 DTO | 定义检索证据、分数、来源与结果封装，避免 Agent 直接依赖数据库行。 |
| `memory/retriever.py` | 长期记忆检索 | 按 space、状态、类型检索并融合词法、向量、trigram/recency 信号；处理正负偏好查询和 source citations。 |
| `memory/candidate_retriever.py` | 候选相关记忆召回 | 在裁决前召回潜在相同/冲突记忆，并提供相似度信号。 |
| `memory/relation_classifier.py` | 候选与既有 Memory 的关系识别 | 判定 duplicate、support、contradict、update、unrelated 等关系，可走规则/LLM。 |
| `memory/relation_guard.py` | 关系安全门 | 将关系分类与类型、时间、任务状态约束结合，阻止不合理合并或错误 supersede。 |
| `memory/advisory.py` | 人工审查建议 | 生成需确认的原因/建议，供冲突和低置信候选进入 pending review。 |
| `memory/adjudicator.py` | 最终裁决 | 综合候选、相关记忆、关系、置信度和 policy，输出 insert/merge/update/supersede/conflict/discard/pending_review 决策。 |
| `memory/consolidator.py` | 单候选编排 | 召回相关项、运行关系检查和裁决、调用 evolution，并为 Trace 增加阶段。 |
| `memory/consistency.py` | Memory 读后写一致性屏障 | 等待对应 Inbox/Memory 水位线，确保查询不会在应当可见时读到未处理 Memory。 |

### 8.3 策略、演化与生命周期

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `memory/policies/__init__.py` | policy 分发与公共合并接口 | 根据 memory type 选择专用策略。 |
| `memory/policies/preference.py` | 偏好策略 | 处理正/负偏好、显式否定、冲突和最新有效偏好，避免“我不喜欢 X”回答为喜欢 X。 |
| `memory/policies/task.py` | 任务策略 | 使用 canonical task key 关联同一任务，按 todo/in_progress/done/cancelled 等状态进行状态机式更新。 |
| `memory/policies/semantic.py` | 稳定事实/语义策略 | 对可合并的背景事实处理去重、补充和 supersede。 |
| `memory/policies/episodic.py` | 事件策略 | 保留时间相关的一次性经历，限制过度合并。 |
| `memory/evolution.py` | 决策落库后的确定性演化 | 对 insert、add_source、merge、update_task、supersede、conflict 等动作调用 repository 原子写入，并写 Trace 时间与结果。 |
| `memory/lifecycle.py` | Memory 生命周期操作 | 管理 active、archived、deleted/purged、pending review 等状态转换。 |
| `memory/task_state.py` | 任务状态归一化与对账 | 将自然语言状态映射至统一状态集，并辅助处理完成、恢复和冲突。 |
| `memory/expiry.py` | 过期策略 | 计算短期/事件类记忆失效或清理候选，避免长期库无限膨胀。 |
| `memory/vector_lifecycle.py` | Memory 向量生命周期 | 发现待嵌入/过期 embedding，入队重建并校验 content hash/维度。 |
| `memory/scheduler.py` | Memory 定时作业 | 启动 consolidation、expiry、向量维护任务，带 lease 和重试控制。 |

### 8.4 仓库、服务与审计

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `memory/repository.py` | 本地 SQLite Memory repository | 创建表并提供记忆、候选、决策、来源、版本、状态、检索和原子 evolution CRUD；处理 SQLite busy 重试。 |
| `memory/trace.py` | Memory Trace 存储与隐私裁剪 | 创建 trace、追加阶段、计算耗时、持久化 latest/按 ID 读取；展示层只保留步骤名称、状态、耗时和候选摘要。 |
| `memory/service.py` | Memory 公共服务与飞书命令格式化 | 编排 Note -> extraction -> validation -> consolidation -> state；提供 list/search/profile/approve/reject/edit/forget/conflicts/trace 等命令的安全文本输出。 |

## 9. `agent/`：查询 Agent 与横切 Hook

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `agent/__init__.py` | Agent 包标记 | 无业务逻辑。 |
| `agent/query_route_features.py` | 查询路由特征 | 用问句词、时间范围、连接词、实体数、子句数等中英文规则生成复杂度、是否需分解/回补的可解释特征。 |
| `agent/query_intent.py` | 查询意图识别 | 先用结构化规则识别偏好、任务、时间、事实等意图；不确定或低召回时可调用 LLM 并回退到规则。 |
| `agent/query_planner.py` | 查询计划 | 在超时和次数预算内生成 rewrite、subquestion、step-back，去重并限制总查询数，避免复杂问题放大模型调用。 |
| `agent/query_agent.py` | 问答主编排 | 运行 Hook 生命周期、意图/计划、Note 与 Memory 检索、复杂查询证据覆盖、工具调用/综合回答、来源绑定和 Trace/指标记录。 |
| `agent/query_agent_flow.md` | 查询流程设计文档 | 以流程图和示例说明 `query_agent.py` 的路由、检索与回答过程。 |
| `agent/tools/__init__.py` | Agent 工具包占位 | 为后续可注册工具保留 package 边界。 |
| `agent/hooks/__init__.py` | Hook 公共导出 | 汇总 context、manager 和默认 Hook 创建器。 |
| `agent/hooks/base.py` | Hook 抽象接口 | 定义 before/after/error 生命周期契约，确保横切能力可组合。 |
| `agent/hooks/context.py` | 单次 Agent 运行上下文 | `AgentRunContext` 持有 tenant/space/user、请求、trace、缓存和统计。 |
| `agent/hooks/manager.py` | Hook 调度器 | 构建默认 Hook 顺序，依次调度前置、后置和异常回调。 |
| `agent/hooks/idempotency.py` | 查询幂等 Hook | 使用本地/Redis 幂等键缓存或拒绝重复请求。 |
| `agent/hooks/rate_limit.py` | 限流 Hook | 根据用户/space 和动作执行本地或 Redis 窗口限流。 |
| `agent/hooks/session.py` | 会话 Hook | 读取/更新短期会话上下文，供多轮问答补全。 |
| `agent/hooks/space_lock.py` | 空间锁 Hook | 在需串行的 Agent 操作上获取/释放 space lock。 |
| `agent/hooks/tool_cache.py` | 工具结果缓存 Hook | 对检索等可复用结果使用 TTL cache，减少重复模型/存储调用。 |
| `agent/hooks/task_dispatch.py` | 任务投递 Hook | 将需要异步处理的 Agent 副作用映射为 local/distributed task。 |
| `agent/hooks/observability.py` | Agent 观测 Hook | 创建/完成 AgentRun、步骤和异常审计。 |
| `agent/hooks/llm_usage.py` | LLM 用量 Hook | 聚合 token、模型、耗时和调用次数，写入运行记录。 |

## 10. `infrastructure/`：数据库与 Redis 实现

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `infrastructure/__init__.py` | 基础设施包标记 | 无业务逻辑。 |
| `infrastructure/database.py` | SQLAlchemy engine/session 生命周期 | 依据环境设置连接池大小、溢出、超时和 recycle；提供 session scope 与连接健康辅助。 |
| `infrastructure/schema.py` | PostgreSQL ORM schema | 声明所有表、外键、唯一约束、索引和 pgvector 字段，是迁移与 repository 的数据契约。 |
| `infrastructure/overload.py` | 数据库过载快照 | 采样连接池/数据库可用性，用于拒绝或降级高压请求。 |
| `infrastructure/redis_client.py` | Redis 客户端工厂 | 分离普通与 blocking 客户端，限制连接数、socket/connect timeout，并提供关闭逻辑。 |
| `infrastructure/redis_keys.py` | Redis key 命名空间 | 集中构造 tenant/space/功能维度 key，防止跨租户 key 冲突。 |
| `infrastructure/redis_cache.py` | Redis TTL 缓存 | JSON 序列化的 get/set/delete，故障时可安全降级。 |
| `infrastructure/redis_idempotency.py` | Redis 幂等锁/结果 | 原子占位、TTL 和结果读取，给 API/Agent 使用。 |
| `infrastructure/redis_rate_limit.py` | Redis 限流器 | 通过计数器/窗口原子操作实施每分钟额度。 |
| `infrastructure/redis_session.py` | Redis 会话存储 | 以 TTL 保存多轮查询上下文。 |
| `infrastructure/redis_lock.py` | Redis 分布式锁 | 使用 token、TTL、续期/释放和 `coordinated_lock` 统一 local/redis 锁语义。 |

## 11. `repositories/`：持久化访问层

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `repositories/__init__.py` | repository 包标记 | 无业务逻辑。 |
| `repositories/interfaces.py` | 存储接口协议 | 用 Protocol 定义 Note/Memory/任务等抽象依赖，降低业务层对具体后端耦合。 |
| `repositories/local/__init__.py` | 本地 repository 命名空间 | 当前本地实现主要复用 `storage/` 与 `memory/repository.py`。 |
| `repositories/postgres/__init__.py` | PostgreSQL repository 包标记 | 无业务逻辑。 |
| `repositories/postgres/common.py` | PostgreSQL 公共 helpers | 解析/创建 tenant、user、space，统一 session 和行转 dict 的基础操作。 |
| `repositories/postgres/dispatch.py` | 接收、投递和水位线事务 | 原子接收命令、创建 task/outbox、发布请求、激活可运行任务、推进 inbox 阶段和 space watermark；是分布式因果顺序核心。 |
| `repositories/postgres/inbox.py` | Inbox 数据访问 | 创建/查询/更新接收消息，处理幂等、状态与恢复。 |
| `repositories/postgres/tasks.py` | Task 与 attempt 数据访问 | 任务租约、状态转换、失败重试、attempt 记录、查询和过期回收。 |
| `repositories/postgres/outbox.py` | Outbox 数据访问 | 认领事件、记录发布成功/失败、lease 和重放。 |
| `repositories/postgres/notes.py` | Note 数据访问 | 写入原文、标题/摘要/类型、标签、关系和按 space 的内容读取/检索。 |
| `repositories/postgres/vectors.py` | Note 向量数据访问 | 管理 embedding 保存、版本/维度校验及 pgvector 相似检索。 |
| `repositories/postgres/memory.py` | Memory 数据访问 | 管理候选、决策、版本、来源、关系、抽取状态、向量和混合检索的数据库实现。 |
| `repositories/postgres/delivery.py` | Delivery 数据访问 | 预约、发送状态、attempt、过期预约恢复，保证外部消息近似一次发送。 |
| `repositories/postgres/summary.py` | Summary 数据访问 | 保存订阅、计划与发送记录，供 scheduler 和 reconciliation 查询。 |
| `repositories/postgres/agent_runs.py` | Agent 审计数据访问 | 保存 AgentRun、AgentStep 和 LLM usage，以便 trace 和指标审计。 |

## 12. `runtime/`：任务运行时

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `runtime/__init__.py` | 运行时包标记 | 无业务逻辑。 |
| `runtime/task.py` | 本地任务数据模型/状态常量 | 定义 queued/running/succeeded/failed/rejected 等任务状态和序列化字段。 |
| `runtime/task_registry.py` | 本地任务注册表 | 保存有限数量、有限 TTL 的任务历史，供查看和恢复。 |
| `runtime/executor.py` | 本地有界异步执行器 | 用线程池/队列执行 ingest、query、summary、memory、delivery；限制 worker/queue，维护任务状态、重试和本地送达。 |
| `runtime/pending_drainer.py` | 本地 WAL 定期 drain | 定时扫描 pending space，按批次调用本地处理，补偿飞书接入后中断。 |
| `runtime/enrichment_drainer.py` | 本地富化 drain | 有界并发处理需 embedding/富化的 Note，独立于关键写入路径。 |
| `runtime/delivery_store.py` | 本地 delivery 去重存储 | 预约、发送、失败、unknown、过期恢复和为 query/summary 构造稳定 key。 |
| `runtime/retry.py` | 重试策略 | 计算指数退避/可重试边界，避免对不可恢复错误无限重试。 |
| `runtime/consistency.py` | 运行时一致性工具 | 统一本地/数据库 read-after-write 等待和状态判断。 |
| `runtime/stream_dispatcher.py` | Stream 发布适配 | 将持久化 task 映射到 Redis task stream，并支持按类型 routing。 |
| `runtime/distributed_metrics.py` | 分布式指标采集 | 聚合任务、队列、outbox、延迟和失败率，输出可比较快照。 |
| `runtime/query_metrics.py` | 查询指标 | 记录路由、检索、LLM、回答和来源覆盖等查询性能指标。 |
| `runtime/load_testing.py` | 负载测试运行器 | 构造多 space/用户压测任务、统计吞吐/延迟/错误。 |
| `runtime/streams/__init__.py` | Redis Streams 子包标记 | 无业务逻辑。 |
| `runtime/streams/client.py` | Redis Streams 客户端 | 创建 stream/group、发布、读组消息、ack、claim 和长度控制。 |
| `runtime/streams/worker.py` | Redis Stream 消费器 | `StreamWorker` 执行 handler、管理租约/attempt/outcome/retry/ack；`AdaptiveStreamWorker` 根据负载调整并发和 reclaim。 |

## 13. `summary/`：周期总结

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `summary/__init__.py` | 总结包标记 | 无业务逻辑。 |
| `summary/daily_summary.py` | 时间范围总结生成 | 解析中文/英文时间范围，读取可查询 Note 和 Memory 变化，构造 LLM JSON prompt 并审阅；LLM 不可用时使用可解释的确定性 Markdown fallback，保存 summary 文件。 |
| `summary/subscription.py` | 自动总结订阅 | 为 space 保存启停、发送时间、最后发送日期，并提供更新/查询。 |
| `summary/scheduler.py` | 总结调度 | 扫描到期订阅，避免重复创建任务，在 local/distributed 后端投递 summary/delivery。 |
| `summary/reconciliation.py` | 总结发送对账 | 检测已生成未送达、预约过期或重复状态，并进行安全修复。 |

## 14. 数据库迁移

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `alembic.ini` | Alembic 配置 | 指向 `alembic/` 脚本目录并定义日志格式。 |
| `alembic/env.py` | Alembic 运行环境 | 导入 ORM metadata、读取 `DATABASE_URL`，支持 online/offline migration。 |
| `alembic/script.py.mako` | 迁移文件模板 | 生成 revision 的标准头部和 upgrade/downgrade 函数骨架。 |
| `alembic/versions/20260717_0001_postgres_foundation.py` | 初始 PostgreSQL 基础表 | 建立 tenant、space、inbox、task、note、memory 等第一版结构。 |
| `alembic/versions/20260718_0002_causal_space_dispatch.py` | 因果调度迁移 | 增加 space 顺序、watermark、任务依赖/投递所需字段和索引。 |
| `alembic/versions/20260718_0003_memory_correctness.py` | Memory 正确性迁移 | 添加候选、来源、版本、决策/状态等审计字段与约束。 |
| `alembic/versions/20260718_0004_query_performance.py` | 查询性能迁移 | 增加检索路径和常用过滤/排序索引。 |
| `alembic/versions/20260718_0005_concurrency_ownership.py` | 并发归属迁移 | 增加租约、attempt、owner/锁相关字段以支持 worker 抢占恢复。 |
| `alembic/versions/20260718_0006_tenant_security_migration.py` | 租户安全迁移 | 强化 tenant/user/space 隔离、唯一键和查询过滤边界。 |
| `alembic/versions/20260718_0007_memory_hybrid_retrieval.py` | Memory 混合检索迁移 | 引入向量/词法检索所需表、索引或字段。 |
| `alembic/versions/20260723_0008_memory_vector_lifecycle.py` | Memory 向量生命周期迁移 | 保存 embedding model、hash、版本、状态和重建控制字段。 |
| `alembic/versions/20260723_0009_memory_search_document_trgm.py` | Memory trigram 搜索迁移 | 建立搜索文档/pg_trgm 相关索引以改善中文/局部词匹配。 |

## 15. `scripts/`：运维、迁移和验证脚本

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `scripts/start.sh` | 单机服务启动 | 读取环境、启动 bot/本地运行组件并写 PID/日志。 |
| `scripts/stop.sh` | 单机服务停止 | 根据 PID/进程标识终止单机进程。 |
| `scripts/status.sh` | 单机状态查看 | 检查服务进程和关键运行状态。 |
| `scripts/logs.sh` | 单机日志查看 | 统一 tail/定位本地日志文件。 |
| `scripts/start_distributed.sh` | 分布式启动 | 按角色启动 receiver、relay、workers、scheduler/API，并做基础检查。 |
| `scripts/stop_distributed.sh` | 分布式停止 | 关闭由分布式脚本启动的所有角色。 |
| `scripts/status_distributed.sh` | 分布式状态查看 | 汇总各进程、依赖服务和 PID 状态。 |
| `scripts/stage4_processes.sh` | Stage 4 进程编排 | 为租户安全/可靠性验证启动、停止和查看隔离进程组。 |
| `scripts/stage5_processes.sh` | Stage 5 进程编排 | 为 dispatch roundtrip 验证管理对应角色。 |
| `scripts/run_stage4_validation.sh` | Stage 4 自动验证 | 执行 basic/chaos 等预设验证步骤并收集结果。 |
| `scripts/check_config.py` | 配置检查 | 校验后端组合、必填变量、连接配置和不兼容 feature flag。 |
| `scripts/check_database.py` | 数据库健康检查 | 连接数据库、确认 schema/基础查询，供部署前执行。 |
| `scripts/check_distributed_cutover.py` | 分布式切换检查 | 验证数据库、Redis、进程配置和关键开关是否满足 cutover 条件。 |
| `scripts/migrate_local_to_postgres.py` | 本地数据迁移 | 读取本地 Note/Memory 并写入 PostgreSQL；支持 `--dry-run` 预览，不删除源数据。 |
| `scripts/verify_migration.py` | 迁移校验 | 对比本地与 PostgreSQL 的记录数量、关键字段和关联完整性。 |
| `scripts/backfill_memory_vectors.py` | Memory 向量回填 | 查找缺失/过期 Memory embedding 并按批次生成或入队。 |
| `scripts/reconcile_task_lifecycle.py` | 任务生命周期对账 | 扫描不一致的 Inbox/Task/Outbox/Delivery 状态，报告或修复可确定项。 |
| `scripts/audit_memory_false_merges.py` | 记忆错误合并审计 | 分析 Memory 关系/版本/来源，找出可疑 merge 供人工复核。 |
| `scripts/show_trace.py` | Trace 查看 | 根据 trace ID 或 latest 读取并格式化 Memory/Query 审计。 |
| `scripts/collect_distributed_metrics.py` | 分布式指标采集 CLI | 调用 runtime metrics 并写出 JSON 快照。 |
| `scripts/build_metrics.py` | 指标聚合 | 将压测、验证和运行日志整理为 docs 可展示的指标 JSON。 |
| `scripts/load_test_multi_users.py` | 多用户压测 | 生成多租户/space 并发请求并报告延迟、吞吐和失败。 |
| `scripts/chaos_test_distributed.py` | 分布式混沌测试 | 注入 worker/relay/外部依赖异常，验证重试和恢复。 |
| `scripts/smoke_distributed_hooks.py` | 分布式 Hook 冒烟 | 检查幂等、限流、session、锁、观测等横切能力的接线。 |
| `scripts/wait_distributed_run.py` | 等待异步任务完成 | 轮询 task/inbox 状态直到成功、失败或超时。 |
| `scripts/cleanup_stage4_run.py` | Stage 4 测试清理 | 清理特定验证 run 的测试数据/进程痕迹，仅用于测试环境。 |
| `scripts/repair_20260715_validation_artifacts.py` | 历史验证产物修复 | 修复指定日期测试产物的格式/索引一致性。 |
| `scripts/backup_data.sh` | 本地数据备份 | 打包运行数据到带时间戳的归档。 |

## 16. 评测与数据集

`eval/` 的脚本默认支持 `--dry-run`，用于验证流程和指标逻辑而不调用外部 LLM 或写入生产数据。真实评测应使用隔离 space/数据库，并固定模型、配置和数据集版本。

| 文件 | 负责功能 | 实现方式/关键点 |
| --- | --- | --- |
| `eval/__init__.py` | 评测包标记 | 无业务逻辑。 |
| `eval/README.md` | 评测说明 | 记录数据集格式、运行方式和指标解释。 |
| `eval/common.py` | 评测公共工具 | 加载 JSONL、计分、报告、dry-run 和隔离环境辅助。 |
| `eval/eval_classification.py` | Note 分类评测 | 运行分类案例并比较 type/tag/title/summary 预期。 |
| `eval/eval_retrieval.py` | Note 检索评测 | 计算召回、排序和来源相关指标。 |
| `eval/eval_summary.py` | 总结评测 | 验证时间范围、证据覆盖、非编造与格式。 |
| `eval/eval_query_react.py` | Agent 问答评测 | 测量工具选择、回答、证据引用和复杂查询行为。 |
| `eval/eval_memory.py` | Memory 基础评测 | 覆盖抽取、过滤、冲突、生命周期、检索和端到端流程。 |
| `eval/eval_memory_quality.py` | Memory 质量评测 | 对候选质量、裁决、错误合并、任务状态等计算细分指标。 |
| `eval/build_memory_quality_dataset.py` | 质量集构建 | 从标准案例/人工标注整理可复现的 Memory 质量数据集。 |
| `eval/benchmark_stage2_queries.py` | Stage 2 查询基准 | 执行查询性能/容量场景并输出延迟和命中结果。 |
| `eval/live_retrieval_eval.py` | 小规模真实检索评测 | 在隔离数据上调用实际检索路径，检查 Recall/Precision/来源。 |
| `eval/large_live_retrieval_eval.py` | 大规模真实检索评测 | 扩展真实检索的规模、并发和聚合统计。 |
| `eval/p4_query_routing_eval.py` | P4 查询路由评测 | 对复杂度、分解、Memory/Note route 与覆盖率评分。 |
| `eval/data/classification_cases.jsonl` | 分类案例 | 输入文本及分类预期。 |
| `eval/data/query_cases.jsonl` | 普通问答案例 | 问题、证据和答案/行为预期。 |
| `eval/data/retrieval_cases.jsonl` | 检索案例 | 查询与应该命中的 Note/Memory 标识。 |
| `eval/data/summary_cases.jsonl` | 总结案例 | 时间范围、输入 Note 和总结断言。 |
| `eval/data/holdout_200_topics_v1.json` | 200 主题留出集 | 用于未见主题泛化评测。 |
| `eval/data/live_retrieval_cases.json` | 真实检索小集 | live retrieval 脚本的输入场景。 |
| `eval/data/live_retrieval_1000.json` | 1000 条真实检索集 | 大规模 live retrieval 输入。 |
| `eval/data/p1_task_retrieval_dev_v1.json` | P1 任务检索开发集 | 任务状态和相关任务召回案例。 |
| `eval/data/p2_memory_state_evolution_v1.json` | P2 Memory 演化集 | insert/update/merge/supersede 状态转换案例。 |
| `eval/data/p3_note_multi_evidence_v1.json` | P3 多证据 Note 集 | 复杂问题需多个 Note 支持的案例。 |
| `eval/data/p4_query_routing_v1.json` | P4 路由集 | 意图、复杂度、子句和路由预期。 |
| `eval/memory/extraction_cases.jsonl` | Memory 抽取案例 | 句子到候选类型/内容/置信度的断言。 |
| `eval/memory/filtering_cases.jsonl` | Memory 过滤案例 | 不应写入长期记忆的噪声、临时或敏感输入。 |
| `eval/memory/conflict_cases.jsonl` | Memory 冲突案例 | 相反偏好、矛盾事实和人工审核预期。 |
| `eval/memory/lifecycle_cases.jsonl` | 生命周期案例 | 任务状态、过期、归档和删除预期。 |
| `eval/memory/relation_cases.jsonl` | 关系分类案例 | duplicate/support/contradict/update 等关系断言。 |
| `eval/memory/retrieval_cases.jsonl` | Memory 检索案例 | 查询到 active/negative/任务类记忆的排序预期。 |
| `eval/memory/quality_cases.jsonl` | Memory 综合质量案例 | 候选、裁决、演化及人工复核的综合标注。 |
| `eval/memory/end_to_end_cases.jsonl` | Memory 端到端案例 | 从消息到候选、落库、查询证据的全链路断言。 |

## 17. 测试套件

所有 pytest 测试由 `tests/conftest.py` 提供隔离目录、环境变量和 fake 外部依赖。CI 运行 `ruff check .`、Alembic upgrade、`pytest tests --cov`（阈值 63%）和五个 dry-run 评测。

| 文件 | 覆盖功能 |
| --- | --- |
| `tests/README.md` | 主测试套件的运行约定与范围。 |
| `tests/conftest.py` | pytest fixture、临时存储和环境隔离。 |
| `tests/test_agent_hooks.py` | Hook 生命周期、顺序、幂等/缓存/观测接线。 |
| `tests/test_api_bind_config.py` | API host/bind 与配置安全约束。 |
| `tests/test_bounded_task_executor.py` | 本地有界队列、拒绝和任务状态。 |
| `tests/test_executor_concurrency.py` | 本地执行器并发、空间隔离和重试。 |
| `tests/test_pending_drainer.py` | WAL pending 定时补偿。 |
| `tests/test_delivery_store.py` | 本地 delivery reservation、重复和过期恢复。 |
| `tests/test_feishu_duplicate_silence.py` | 重复飞书事件不重复回复。 |
| `tests/test_llm_client_memory_extraction_retry.py` | 抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。 |
| `tests/test_sensitive_and_read_after_write.py` | 敏感拦截与读后写一致性。 |
| `tests/test_retry_boundaries.py` | 可重试/不可重试错误边界。 |
| `tests/test_redis_blocking_client.py` | Redis blocking 客户端连接预算。 |
| `tests/test_redis_coordination.py` | Redis cache/idempotency/session/rate limit 协作。 |
| `tests/test_redis_lock_boundaries.py` | 分布式锁归属、TTL 和释放边界。 |
| `tests/test_postgres_repositories.py` | PostgreSQL repositories、租户隔离及 schema CRUD。 |
| `tests/test_streams_outbox.py` | Inbox/Task/Outbox/Stream roundtrip、lease/reclaim。 |
| `tests/test_worker_database_paths.py` | 分布式 handler 的数据库读写路径。 |
| `tests/test_task_registry_retention.py` | 本地任务历史 TTL/容量清理。 |
| `tests/test_task_state_reconciliation.py` | Inbox/Task 状态对账。 |
| `tests/test_query_intent_v3.py` | 查询意图规则/LLM 回退。 |
| `tests/test_query_route_features.py` | 复杂度和子句特征提取。 |
| `tests/test_query_trace.py` | 查询 Trace、来源及展示裁剪。 |
| `tests/test_query_metrics.py` | 查询指标采集与聚合。 |
| `tests/test_retrieval_design.py` | Note 与 Memory 混合检索、引用和覆盖。 |
| `tests/test_memory_extractor.py` | rules/LLM/hybrid 抽取与回退。 |
| `tests/test_memory_candidate_validator.py` | 候选 schema、安全、质量、拒绝原因。 |
| `tests/test_memory_adjudication_evolution.py` | 裁决到确定性演化动作。 |
| `tests/test_memory_consolidation.py` | 相关召回、合并和冲突处理。 |
| `tests/test_memory_consolidation_concurrency.py` | 同 key 并发合并互斥。 |
| `tests/test_memory_consolidation_runs.py` | consolidation scheduler run/lease。 |
| `tests/test_memory_consolidator_resilience.py` | 裁决/合并异常的安全失败。 |
| `tests/test_memory_extraction_state.py` | extraction state、partial/failed/empty。 |
| `tests/test_memory_repository.py` | SQLite repository CRUD、版本和来源。 |
| `tests/test_memory_sqlite_concurrency.py` | SQLite busy retry/多线程写入。 |
| `tests/test_memory_service.py` | 服务编排及命令输出。 |
| `tests/test_memory_commands.py` | 飞书 memory 管理命令文本与操作。 |
| `tests/test_memory_consistency_v3.py` | 查询 memory barrier 与 V3 一致性。 |
| `tests/test_memory_hybrid_retrieval_stage6.py` | Stage 6 混合检索和性能。 |
| `tests/test_memory_scheduler.py` | 过期/合并/向量定时任务。 |
| `tests/test_memory_scheduler_retry.py` | memory scheduler 失败重试和 lease。 |
| `tests/test_memory_stage1_correctness.py` | 偏好否定、冲突和基础正确性回归。 |
| `tests/test_memory_trace.py` | Trace 写入、隐私裁剪、latest 展示。 |
| `tests/test_memory_v3_redesign.py` | V3 schema/key/relation guard/shadow 行为。 |
| `tests/test_memory_eval.py` | Memory eval 读取、计分和报告。 |
| `tests/test_summary_delivery.py` | 总结生成后 delivery 预约/发送。 |
| `tests/test_summary_reconciliation.py` | 总结发送状态对账。 |
| `tests/test_summary_scheduler_resilience.py` | scheduler 异常隔离和恢复。 |
| `tests/test_stage2_query_performance.py` | Stage 2 查询性能指标。 |
| `tests/test_stage3_concurrency_ownership.py` | Stage 3 并发所有权/租约。 |
| `tests/test_stage4_load_testing.py` | Stage 4 负载测试场景。 |
| `tests/test_stage4_metrics.py` | Stage 4 指标生成与阈值。 |
| `tests/test_stage4_resilience.py` | Stage 4 失效/恢复路径。 |
| `tests/test_stage5_dispatch_performance.py` | Stage 5 dispatch 吞吐/延迟。 |
| `tests/test_stage7_model_routing_and_clause_extraction.py` | 模型路由和 clause extraction。 |
| `tests/1阶段测试/README.md` | 阶段 1 遗留测试说明。 |
| `tests/1阶段测试/test_feedback.py` | 用户反馈存储。 |
| `tests/1阶段测试/test_query_filter.py` | Note 标签/类型过滤。 |
| `tests/1阶段测试/test_summary_range.py` | 总结时间范围解析。 |
| `tests/1阶段测试/test_summary_scheduler.py` | 自动总结调度。 |
| `tests/1阶段测试/test_summary_subscription.py` | 总结订阅增删改查。 |
| `tests/1阶段测试/test_taxonomy.py` | 分类词表。 |
| `tests/2阶段测试/README.md` | 阶段 2 遗留测试说明。 |
| `tests/2阶段测试/test_daily_summary_flow.py` | 总结端到端流程。 |
| `tests/2阶段测试/test_query_agent_react.py` | 早期 ReAct 查询流程。 |
| `tests/2阶段测试/test_worker_flow.py` | 早期本地 worker 写入流程。 |
| `tests/3阶段测试/README.md` | 阶段 3 遗留测试说明。 |
| `tests/3阶段测试/test_eval_common.py` | 评测公共工具。 |

## 18. 文档、报告和静态资产

| 文件/目录 | 负责功能 | 内容说明 |
| --- | --- | --- |
| `docs/distributed_cutover_runbook.md` | 分布式上线 runbook | 部署前检查、切换、回滚和验证步骤。 |
| `docs/stage1_memory_correctness_report.md` | Stage 1 报告 | Memory 正确性验证结果。 |
| `docs/stage2_database_query_performance_report.md` | Stage 2 报告 | 数据库查询性能结论。 |
| `docs/stage3_concurrency_ownership_report.md` | Stage 3 报告 | 并发所有权和租约验证。 |
| `docs/stage4_tenant_security_migration_report.md` | Stage 4 报告 | 租户安全迁移结果。 |
| `docs/stage4_validation_report.md` | Stage 4 报告 | 分布式验证与韧性结论。 |
| `docs/stage5_dispatch_roundtrip_report.md` | Stage 5 报告 | Inbox/Outbox/Stream roundtrip 验证。 |
| `docs/stage6_memory_hybrid_retrieval_report.md` | Stage 6 报告 | Memory 混合检索结果。 |
| `docs/stage7_memory_hybrid_model_routing_report.md` | Stage 7 报告 | 混合检索和模型路由结果。 |
| `docs/traces/example-memory-write.md` | Trace 样例 | 展示一次消息到 Memory 候选/裁决/演化的审计结构。 |
| `docs/traces/example-memory-query.md` | Trace 样例 | 展示查询路由、来源和回答的审计结构。 |
| `docs/memory_eval/README.md` | Memory 评测报告说明 | 解释目录内基线/阶段 JSON 的字段和比较方式。 |
| `docs/memory_eval/baseline.json` | Memory 评测基线 | 初始版本质量快照。 |
| `docs/memory_eval/baseline_v2.json` | V2 基线 | V2 方案的质量快照。 |
| `docs/memory_eval/stage1.json` | Memory Stage 1 指标 | 基础正确性结果。 |
| `docs/memory_eval/stage2.json` | Memory Stage 2 指标 | 进阶质量/检索结果。 |
| `docs/memory_eval/stage3.json` | Memory Stage 3 指标 | 更完整的端到端结果。 |
| `docs/metrics/baseline_clean.json` | 指标基线 | 清洁基线性能快照。 |
| `docs/metrics/baseline_clean_100.json` | 100 条基线 | 固定容量下的基线快照。 |
| `docs/metrics/latest.json` | 最新指标 | 最近汇总的运行指标。 |
| `docs/metrics/stage1_memory_correctness.json` | Stage 1 指标 | 记忆正确性数据。 |
| `docs/metrics/stage2_clean_capacity_100.json` | Stage 2 指标 | 100 容量测试。 |
| `docs/metrics/stage2_query_baseline.json` | Stage 2 指标 | 查询优化前基线。 |
| `docs/metrics/stage2_query_optimized.json` | Stage 2 指标 | 查询优化后结果。 |
| `docs/metrics/stage2_summary.json` | Stage 2 汇总 | Stage 2 结果汇总。 |
| `docs/metrics/stage3_clean_capacity_100.json` | Stage 3 指标 | 清洁容量测试。 |
| `docs/metrics/stage3_cross_space_1000.json` | Stage 3 指标 | 1000 space 隔离/容量测试。 |
| `docs/metrics/stage3_smoke_10.json` | Stage 3 指标 | 小规模冒烟测试。 |
| `docs/metrics/stage3_summary.json` | Stage 3 汇总 | Stage 3 结果汇总。 |
| `docs/metrics/stage4_latest.json` | Stage 4 指标 | 最新分布式验证快照。 |
| `docs/metrics/stage4_summary.json` | Stage 4 汇总 | Stage 4 指标汇总。 |
| `docs/metrics/stage5_clean_capacity_100.json` | Stage 5 指标 | dispatch 容量测试。 |
| `docs/metrics/stage5_cross_space_100.json` | Stage 5 指标 | 多 space 结果。 |
| `docs/metrics/stage5_cross_space_100_final.json` | Stage 5 指标 | 最终多 space 结果。 |
| `docs/metrics/stage5_smoke_20.json` | Stage 5 指标 | 20 条冒烟结果。 |
| `docs/metrics/stage5_summary.json` | Stage 5 汇总 | Stage 5 指标汇总。 |
| `docs/metrics/stage6_memory_hybrid_retrieval.json` | Stage 6 指标 | Memory 混合检索指标。 |
| `docs/metrics/stage7_memory_hybrid_model_routing.json` | Stage 7 指标 | 模型路由指标。 |
| `docs/images/feishu/01-record-note.png` | 飞书截图 | 普通消息记录为 Note。 |
| `docs/images/feishu/02-ask-simple.png` | 飞书截图 | 简单问答和引用。 |
| `docs/images/feishu/03-ask-complex.png` | 飞书截图 | 复杂查询、子问题和多源证据。 |
| `docs/images/feishu/04-memory-candidates.png` | 飞书截图 | 普通输入生成的 Memory 候选。 |
| `docs/images/feishu/05-task-lifecycle.png` | 飞书截图 | 任务状态更新。 |
| `docs/images/feishu/06-memory-audit-trace.png` | 飞书截图 | 可审计的 Memory Trace。 |
| `docs/images/feishu/07-memory-profile.png` | 飞书截图 | 动态用户画像。 |
| `docs/archive/plans/README.md` | 历史计划索引 | 说明 archive 内容只供追溯，不代表当前实现。 |
| `docs/archive/plans/suixinji_ci_time_dependent_tests_fix.md` | 历史计划 | CI 时间相关测试修复方案。 |
| `docs/archive/plans/suixinji_memory_v2_final_hardening_plan.md` | 历史计划 | Memory V2 最终加固记录。 |
| `docs/archive/plans/suixinji_memory_v2_second_hardening_plan.md` | 历史计划 | Memory V2 第二轮加固记录。 |
| `docs/archive/plans/suixinji_stage1_engineering_improvement.md` | 历史计划 | Stage 1 工程改进计划。 |
| `docs/archive/plans/suixinji_stage1_scheduler_final_fix.md` | 历史计划 | Stage 1 调度修复记录。 |
| `docs/archive/plans/suixinji_stage2_memory_v2_trace_evaluation.md` | 历史计划 | Stage 2 Trace 评测计划。 |
| `docs/archive/plans/随心记 Agent 第一阶段收尾修正方案.docx` | 历史计划附件 | 中文阶段收尾方案的 Office 文档。 |
| `docs/archive/plans/随心记第一阶段必修问题修正方案.docx` | 历史计划附件 | 中文问题修正方案的 Office 文档。 |

## 19. 修改导航

| 想修改的行为 | 首先查看 | 通常还会涉及 |
| --- | --- | --- |
| 飞书命令、回复格式、消息解析 | `bot/feishu_bot.py` | `memory/service.py`、`summary/*`、`apps/receiver.py` |
| Note 分类、保存、富化 | `core/worker.py`、`core/classifier.py` | `storage/*`、`repositories/postgres/notes.py` |
| Memory 抽取规则/LLM schema | `memory/extractor.py`、`memory/extraction_schema.py` | `core/llm_client.py`、`memory/candidate_validator.py`、对应测试 |
| Memory 合并、冲突、任务状态 | `memory/adjudicator.py`、`memory/relation_guard.py`、`memory/policies/task.py` | `memory/evolution.py`、`memory/repository.py` |
| 记忆检索或正负偏好 | `memory/retriever.py`、`memory/policies/preference.py` | `repositories/postgres/memory.py`、`tests/test_memory_stage1_correctness.py` |
| 复杂问答/子问题/来源展示 | `agent/query_agent.py`、`agent/query_planner.py` | `agent/query_route_features.py`、`memory/retriever.py`、`storage/vector_store.py` |
| 本地异步处理 | `runtime/executor.py` | `core/wal.py`、`runtime/*_drainer.py` |
| 分布式任务可靠性 | `repositories/postgres/dispatch.py`、`apps/handlers.py` | `apps/outbox_relay.py`、`runtime/streams/worker.py`、`infrastructure/schema.py` |
| 数据库结构 | `infrastructure/schema.py` | 新增对应 `alembic/versions/*` 迁移和 repository 测试 |
| 新增/调整配置 | `core/settings.py`、`.env.example` | `scripts/check_config.py`、README/部署文档 |

## 20. 不在本文逐项展开的运行时产物

- `data/`：真实或测试的 Note、WAL、缓存、日志、delivery、summary、load-test 输出；其格式由上述 storage/repository/runtime 模块决定。
- `backups/`：`backup_data.sh` 生成的数据归档，不参与运行时导入。
- `.env`、`.coverage`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`：本机环境与工具缓存，不能作为设计依据或提交。
- 新增但未跟踪的 benchmark 文件、密钥文件和二进制模型目录：应在审查内容、来源和敏感性后单独决定是否纳入版本控制。

## 21. 推荐阅读顺序

1. `README.md`、本文件第 2 节，理解用户功能与两种运行模式。
2. `bot/feishu_bot.py`、`core/worker.py`、`memory/service.py`，理解记录到长期记忆的单机链路。
3. `agent/query_agent.py`、`query_planner.py`、`memory/retriever.py`，理解问答和复杂检索。
4. `infrastructure/schema.py`、`repositories/postgres/dispatch.py`、`runtime/streams/worker.py`，理解生产可靠性边界。
5. 对应的 `tests/test_*.py`、`eval/` 和 `docs/stage*.md`，在修改前后确认回归和指标。
