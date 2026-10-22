# 随心记 Agent

运行在飞书和 Web 控制台中的个人记忆助手：保存原始记录，从中提取任务、偏好、事实和经历，并结合原文回答历史问题。记忆的来源、修改版本和模型调用可以追溯。

## 能做什么

| 功能 | 技术实现 | 直白解释 |
| --- | --- | --- |
| 记录 | Note 原文持久化，Inbox / WAL 与后台任务处理 | 先保存你说的话，再整理 |
| 四类记忆 | task、preference、semantic、episodic | 分别记住要做的事、偏好、事实和经历 |
| 并行抽取 | 启用事实记忆后，任务/偏好与事实/经历使用两路独立结构化调用 | 同时处理不同类型的信息，分别校验结果 |
| 记忆整理 | 来源校验、版本化修改、整理工具与审批 | 合并重复、更新变化，也能查看修改依据 |
| 历史问答 | Memory 与 Note 混合检索、证据选择、来源引用 | 结合记忆和原话回答 |
| Web 控制台 | 消息运行、召回候选、工具参数/返回、模型上下文与记忆历史 | 能看到每一步实际做了什么 |

功能受配置开关控制。默认配置保留规则抽取；卡片与事实记忆需显式启用，不能把“代码已支持”理解成“所有部署均已开启”。

## 记忆如何工作

```text
消息 → 保存 Note 原文 → 抽取候选 → 校验和写入 → 带来源的记忆
                         ├─ 任务 / 偏好
                         └─ 语义事实 / 情景经历（启用事实记忆时并行）

整理 → 读取相关记忆与原文 → 提出修改 → 校验 / 审批 → 保存版本
查询 → 检索 Memory + Note → 选择证据 → 回答并附来源
```

- **任务卡片**只使用 `todo`（未结束）和 `done`（已结束）；完成、取消、放弃由结束原因区分，阻碍和进展保存为细节。
- **偏好卡片**保存偏好对象、适用条件、细节和证据，支持后续维护与整理。
- **语义与情景记忆**保留原始事实及派生整理卡，区分陈述和推断，并保存来源版本、指代依据和待确认问题。
- 自动整理可以关闭；关闭后继续正常抽取和查询，只在明确手动请求时整理。

实现说明：[任务卡片](docs/task-cards-design.md)、[偏好卡片](docs/preference-cards-implementation.md)、[事实与情景记忆](docs/fact-memory-consolidation.md)。

## 快速开始

需要 Python 环境；CI 配置使用 Python 3.10 / 3.11。先安装依赖并配置 `.env`：

```bash
git clone https://github.com/9leaa/suixinji.git
cd suixinji
python3 -m venv .venv
source .venv/bin/activate
make install-dev
cp .env.example .env
```

按 `.env.example` 填写飞书应用、模型和 Embedding 配置。启动前的 `check_config.py` 会检查这些必填项以及所选存储后端。

### 本地模式

适合单机开发与基础功能调试：

```dotenv
STORAGE_BACKEND=local
COORDINATION_BACKEND=local
TASK_QUEUE_BACKEND=local
```

```bash
python scripts/check_config.py
make start
make status
```

### PostgreSQL / Redis 与 Web 控制台

控制台依赖 PostgreSQL 和后台分布式处理角色。先准备 PostgreSQL（含 pgvector）及 Redis，并填写实际连接信息：

```dotenv
STORAGE_BACKEND=postgres
COORDINATION_BACKEND=redis
TASK_QUEUE_BACKEND=redis_streams
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE
REDIS_URL=redis://HOST:6379/0
```

```bash
make db-upgrade
python scripts/check_config.py
make distributed-start
make distributed-status
python scripts/run_console.py
```

打开 `http://127.0.0.1:18080/console`，在连接框填写本机 `data/console.token` 中的令牌。控制台启动脚本不会代替 Outbox relay 和 Worker；完整操作见[控制台说明](docs/message-console/README.md)与[分布式部署手册](docs/distributed_cutover_runbook.md)。

### 启用卡片和事实记忆

完成数据库迁移并配置模型后，在 `.env` 中启用：

```dotenv
SUIXINJI_TASK_CARDS_ENABLED=true
SUIXINJI_PREFERENCE_CARDS_ENABLED=true
SUIXINJI_FACT_MEMORY_ENABLED=true
SUIXINJI_MEMORY_EXTRACTOR_MODE=llm
# 只允许手动整理；不影响新消息抽取和查询。
SUIXINJI_MEMORY_AUTO_CONSOLIDATION_ENABLED=false
```

前三个功能开关代码默认值为 `false`，自动整理开关默认值为 `true`。启用事实记忆后使用两路模型抽取，即使抽取模式配置为 `rules` 也会调用模型；任一路失败不会当作成功的空结果。修改配置后需重启对应进程。

旧任务卡数据迁移工具为 `scripts/migrate_task_cards_v2.py`，要求指定目标数据库和备份路径；仅显式传入 `--apply` 才修改数据。不要把数据迁移当成普通启动步骤。

## 常用操作

| 目的 | 命令 |
| --- | --- |
| 记录 | 直接发送文本 |
| 历史问答 | `/ask 上次我提到这个项目时有什么进展？` |
| 查看处理过程 | `/trace latest`、`/trace <trace_id>` |
| 查看 / 搜索记忆 | `/memory profile`、`/memory search <关键词>` |
| 审阅变更 | `/memory pending`、`/memory approve <id>` |
| 修正 / 遗忘 | `/memory correct <id> <内容>`、`/memory forget <id>` |
| 查看事实与经历 | `/memory facts list`（需启用事实记忆） |
| 手动整理事实 | `/memory facts consolidate`（需启用事实记忆） |
| 回顾 | `/summary 今天`、`/summary 一周` |

控制台还提供记忆整理、审批和历史查看入口。不同记忆类型的管理协议见相应功能文档。

## 飞书交互示例

以下为历史版本的真实交互截图，用于展示记录和问答方式；当前字段、状态和页面以代码及上述说明为准。

<details>
<summary>展开截图</summary>

![记录消息](docs/images/feishu/01-record-note.png)
![历史问答](docs/images/feishu/02-ask-simple.png)
![混合检索问答](docs/images/feishu/03-ask-complex.png)

</details>

## 开发与验证

```bash
make lint
make test
make eval-dry-run
```

离线 dry-run 验证固定样例与流程，不代表真实模型质量或线上性能。CI 还执行数据库迁移及覆盖率检查。手动诊断脚本位于 `scripts/`：`P1_test.py` 会写入本地测试消息并触发处理；`connect_test.py` 会调用配置的模型服务，均不属于自动单元测试。

## 目录与文档

| 路径 | 内容 |
| --- | --- |
| `agent/`、`memory/` | 查询 Agent、记忆抽取、检索与整理 |
| `apps/`、`bot/` | API、控制台、后台进程与飞书接入 |
| `core/`、`runtime/` | 配置、模型客户端、任务调度与可靠投递 |
| `repositories/`、`storage/`、`infrastructure/` | 数据访问、Note 存储、数据库与 Redis |
| `summary/` | 总结与周期回顾 |
| `alembic/` | 数据库迁移 |
| `tests/`、`eval/` | 自动测试与评测 |
| `scripts/` | 启停、迁移、验证和手动诊断 |
| `docs/` | 功能说明与部署手册，入口见[文档索引](docs/README.md) |
| `docs/design/` | 设计资料，历史约束不代表当前实现 |
| `docs/archive/` | 历史计划、验证和其他参考材料 |

## 当前边界

目前主要处理文本。记忆抽取与整理仍依赖模型质量，需要结合真实样例校准；历史 Trace 无法补造未记录的上下文。本地文件存储适合单机开发，多进程使用应配置 PostgreSQL。飞书外部发送通过幂等和重试降低重复风险，不承诺严格 exactly-once。
