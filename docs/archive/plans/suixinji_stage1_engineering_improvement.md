# 随心记 Agent 第一阶段工程改造方案

## 1. 阶段目标

在增加高级记忆系统前，先解决以下问题：

1. 设计文档与代码实现不一致。
2. 每个请求创建独立线程，缺少并发控制。
3. 缺少 CI 和标准化复现流程。
4. 项目功能较多，但缺少直观展示和量化证据。

本阶段不新增业务功能，目标是把项目从“能运行的个人 Demo”改造成“架构清晰、运行可靠、可以复现和评测的 Agent 工程项目”。

---

# 一、统一设计文档与代码实现

## 1.1 当前问题

重点检查并修正：

- `DESIGN.md` 中仍描述 `asyncio.create_task`，实际代码使用 `threading.Thread`。
- 文档描述标签自由生成，实际代码使用固定 taxonomy。
- `main.py` 中残留 Telegram 描述。
- README 使用本机 Conda 绝对路径。
- 已解决的问题仍保留旧 TODO。
- README、DESIGN、代码中的默认参数可能不一致，例如检索阈值、top-k、自动总结时间。

## 1.2 改造方案

新增统一配置文件：

```text
core/settings.py
```

集中管理：

```python
MAX_WORKERS = 4
TASK_QUEUE_SIZE = 100
LLM_TIMEOUT_SECONDS = 30
LLM_MAX_RETRIES = 2
EMBEDDING_TIMEOUT_SECONDS = 20

RELATED_TOP_K = 3
RELATED_MIN_SCORE = 0.5
QUERY_TOP_K = 5
QUERY_MIN_SCORE = 0.55

SUMMARY_DEFAULT_TIME = "22:00"
```

其他模块不得重复硬编码这些配置。

重写文档结构：

```text
README.md
├── 项目简介
├── 核心功能
├── 架构图
├── 快速启动
├── 使用示例
├── 测试与评测
├── 当前边界
└── Roadmap

DESIGN.md
├── 系统目标
├── 写入链路
├── 查询链路
├── 总结链路
├── 并发模型
├── 存储模型
├── 一致性与恢复
├── 评测体系
└── 已知边界
```

## 1.3 验收标准

- 文档中的每个模块都能在代码中找到对应实现。
- 所有关键参数只在统一配置层定义。
- README 不包含个人机器路径。
- 删除过期 TODO、Telegram 等错误描述。
- 新开发者只阅读 README 即可完成本地运行。

---

# 二、建立有界任务执行系统

## 2.1 当前问题

当前飞书消息、查询和总结会分别创建新的 daemon thread。

风险：

- 高频消息可能创建大量线程。
- 无法限制并发 LLM 请求。
- 无法观察任务排队数量。
- 程序退出时任务可能直接丢失。
- 查询、总结、写入任务缺少统一调度。

## 2.2 总体结构

```text
Feishu Receiver
      │
      ├── 写入类消息 → 先写 WAL
      │
      └── 查询/总结命令 → 创建 Task
                       │
                       ▼
               BoundedTaskExecutor
                       │
             ThreadPoolExecutor(4)
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       Ingest        Query        Summary
```

## 2.3 新增模块

```text
runtime/
├── task.py
├── executor.py
├── retry.py
└── task_registry.py
```

### Task 数据结构

```python
class Task:
    id: str
    task_type: str
    space_id: str
    message_id: str | None
    payload: dict
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
```

状态：

```text
queued
running
success
failed
rejected
```

### Executor 接口

```python
submit_ingest(record)
submit_query(space_id, question, chat_id)
submit_summary(space_id, range_key, chat_id)
get_stats()
shutdown()
```

## 2.4 关键规则

### 写入任务

- 收到消息后必须先写 WAL。
- WAL 写入成功后再提交后台任务。
- 队列已满时，消息仍保留在 WAL。
- 后续由 pending recovery 重新处理。

### 查询和总结任务

- 队列满时直接返回“当前任务较多，请稍后重试”。
- 不写入 WAL，但要记录任务拒绝日志。

### 并发限制

```text
全局最大 worker：4
同一 space_id 写入任务：串行执行
查询任务：允许并行
总结任务：同一 space_id 同时最多一个
```

### 超时与重试

只对以下错误重试：

- API timeout
- HTTP 429
- HTTP 5xx
- 临时网络异常

不重试：

- 配置错误
- Prompt 输出格式错误超过校验次数
- 参数错误
- 文件结构损坏

建议：

```text
最大重试：2 次
退避时间：1 秒、3 秒
单次 LLM 超时：30 秒
Embedding 超时：20 秒
```

## 2.5 `/status` 扩展

增加：

```text
- 当前运行任务数
- 当前排队任务数
- 成功任务数
- 失败任务数
- 被拒绝任务数
- 最老排队任务等待时间
- 最近一次 LLM 超时
```

## 2.6 验收标准

- 连续提交 200 条模拟消息时，线程数量保持稳定。
- 队列满后不会丢失已经写入 WAL 的消息。
- 同一 `space_id` 不会同时写 `index.json`。
- LLM 超时不会永久卡住 worker。
- `/status` 可以显示任务执行状态。
- 程序重启后能继续处理 pending WAL。

---

# 三、建立 CI 与可复现运行流程

## 3.1 新增文件

```text
.github/workflows/ci.yml
requirements.txt
requirements-dev.txt
pyproject.toml
Dockerfile
docker-compose.yml
Makefile
```

Docker 可以作为第二优先级，但 CI 必须完成。

## 3.2 CI 流程

每次 push 和 pull request 执行：

```text
1. 安装 Python
2. 安装依赖
3. Ruff 静态检查
4. Pytest 单元测试
5. Dry-run 评测
6. 检查测试覆盖率
```

命令：

```bash
ruff check .
python -m pytest tests --cov=. --cov-report=term-missing

python eval/eval_classification.py --dry-run
python eval/eval_retrieval.py --dry-run
python eval/eval_summary.py --dry-run
python eval/eval_query_react.py --dry-run
```

## 3.3 依赖管理

生产依赖：

```text
requirements.txt
```

开发依赖：

```text
requirements-dev.txt
├── pytest
├── pytest-cov
├── ruff
└── mypy
```

所有依赖锁定主版本或完整版本，避免未来安装结果不一致。

## 3.4 标准命令

通过 `Makefile` 统一：

```bash
make install
make test
make eval-dry-run
make lint
make start
make stop
make backup
```

## 3.5 验收标准

- 新环境 clone 后，不需要修改代码即可运行测试。
- GitHub 首页显示 CI 通过。
- 不依赖个人 Conda 环境。
- Dry-run 不调用真实 LLM 和 Embedding API。
- PR 出现测试失败时无法显示为绿色通过。

---

# 四、完善项目展示与量化证据

## 4.1 README 首页结构

```text
项目一句话定位
架构图
核心能力
运行演示
可靠性设计
评测结果
快速启动
项目边界
Roadmap
```

## 4.2 需要制作的展示材料

### 架构图

展示：

```text
Feishu
→ WAL
→ Bounded Executor
→ Classifier / Embedding / Related Search
→ Markdown / Index / Vector Store
→ ReAct Query
→ Reflection Summary
→ Observability / Evaluation
```

### 使用演示

至少包含：

1. 普通消息自动归档。
2. `/ask` 查询历史内容。
3. 修改偏好后检索最新结果。
4. `/summary 一周`。
5. `/status` 查看系统状态。
6. 重复消息被幂等跳过。
7. 进程退出后 pending 恢复。

### 量化指标

统一生成：

```text
docs/metrics/latest.json
```

记录：

```json
{
  "classification_accuracy": 0.8889,
  "retrieval_pass_rate": 0.95,
  "query_pass_rate": 0.90,
  "summary_pass_rate": 1.0,
  "p50_ingest_latency_ms": 0,
  "p95_ingest_latency_ms": 0,
  "p50_query_latency_ms": 0,
  "pending_recovery_rate": 1.0,
  "duplicate_prevention_rate": 1.0
}
```

不要只展示通过率，也要展示失败案例和当前限制。

## 4.3 Trace 示例

README 放一条完整执行链：

```text
message_received
→ wal_appended
→ task_queued
→ classify_success
→ embedding_success
→ related_search
→ note_saved
→ vector_saved
→ wal_processed
```

每一步显示：

- duration
- status
- input/output 摘要
- error
- fallback

## 4.4 验收标准

- GitHub 首页 30 秒内能看懂项目做什么。
- 能看到真实交互截图或 GIF。
- 能看到真实评测数字。
- 能展示一次故障恢复。
- 能展示一次完整 Trace。
- README 明确区分“已完成”和“未来计划”。

---

# 五、实施顺序

```text
P0：清理文档与配置
P1：实现 BoundedTaskExecutor
P2：增加超时、重试和状态统计
P3：建立 CI
P4：补充架构图、演示和指标
```

完成本阶段后，再进入 Memory V2。
