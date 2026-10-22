# 随心记 `/ask` 统一查询链路改造计划

> 目标项目：`/home/zcj/suixinji`
> 计划版本：v1.0
> 设计目标：将当前“确定性快速路由 + 复杂查询扩展 + ReAct”逐步改造成统一的 **Plan → Execute → Evidence Repair → Answer** 工作流。
> 实施状态更新：2026-08-16。以下状态以远程 `/home/zcj/suixinji` 当前工作区为准。

## 当前实施状态

| 阶段 | 状态 | 当前边界 |
|---|---|---|
| P0 契约与校验 | 已完成 | AskPlan、QueryUnit、Evidence Bundle、schema/依赖/重复 Unit 校验与回归测试已落地。 |
| P1 Planner Shadow | 已完成 | `SUIXINJI_ASK_V2_SHADOW=true` 时只生成 Plan Trace，不改变用户收到的旧 ReAct 答案。 |
| P2 领域工具适配 | 已完成第一版 | Executor 已确定性映射 Task、Preference、Semantic、Episodic、Timeline、Note；尚未拆成独立 `agent/retrieval/*` 文件，避免无收益的迁移。 |
| P3 执行与证据层 | 已完成第一版 | Unit DAG、并发、单 Unit 错误隔离、一轮预算化补证、已选 Note 的全文受控定位/裁剪，以及“原文引文绑定的原子事实”已落地。事实层不能改任务状态、偏好极性或冲突结论；真实网络超时取消仍依赖底层 LLM Client。 |
| P4 回答器 | 已完成第一版 | Answer 只读取 Unit Bundle、逐 Unit 校验 Evidence ID、输出解析失败则使用确定性模板。 |
| P5 切流与旧路径退役 | 保持 Shadow，禁止切流 | 2026-08-16 的真实 LongMemEval-S 检索和全链路验证已完成，但暴露 task 状态演进、semantic 最新值解析和 Top-1 排序缺口；禁止开启 `SUIXINJI_ASK_V2_ENABLED` 或删除旧链路。 |

本轮新增的验证包括：后段答案片段定位、Note 自身展开、数字/日期线索、单 Unit 故障隔离、补证轮次预算，以及“事实引文必须可在证据中逐字定位”的校验。LongMemEval Oracle 的同一 30 条诊断样本中，事实层版本经项目 fast LLM Judge 得到 9/28，上一版为 5/28；每轮均有 2 条 Judge 网络失败，且存在模型随机性，不能作为正式因果结论或切流依据。Oracle 仅用于“证据使用”诊断。2026-08-16 已补完真实验证：LongMemEval-S 60/60 真实 Hybrid 检索的 Recall@1/@10 为 40.00%/90.00%，而隔离的 receiver→Redis→worker→Ask V2 全链路严格通过 2/4；完整证据和失败样本见 `SUIXINJI_ASK_REDESIGN_VERIFICATION_20260816.md`。因此 P5 继续保持 Shadow。

---

## 1. 结论

新的 `/ask` 不再通过 `simple / complex` 决定进入两套不同流程，也不再让回答模型自由选择多个检索工具。

统一链路如下：

```text
User Ask
   ↓
安全检查 + 会话上下文 + Memory Watermark
   ↓
Ask Planner LLM
   ↓
AskPlan Validator
   ↓
Unit Executor
   ↓
领域检索工具
   ↓
Evidence Resolver
   ↓
不足时执行一轮确定性 Evidence Repair
   ↓
Answer Synthesizer
   ↓
Answer + Evidence IDs + Trace
```

核心边界：

1. Planner LLM 只负责理解问题并生成 Query Unit，不直接选择函数名。
2. Executor 根据 `intent` 确定性映射领域工具。
3. Task、Preference、Semantic、Episodic 保留各自不同的读取语义。
4. 底层 Exact、Structured、FTS、Dense、Trigram、RRF 不暴露给 LLM。
5. 排序分数和回答模型不能修改任务状态、偏好极性或事实冲突结论。
6. Memory 命中后优先沿 `MemorySource → Note` 回溯证据，不重新做无约束 Note 全局检索。
7. 不足时最多执行一轮受控补证，不进入开放式 ReAct 循环。

---

## 2. 当前实现与主要问题

当前 `/ask` 主体位于 `agent/query_agent.py`，同时存在：

```text
确定性 fast route
+ structural simple/complex/uncertain
+ Query Intent LLM
+ query rewrite / decomposition / step-back
+ Memory prefetch
+ ReAct 多步工具调用
```

当前基础能力可以复用：

- `agent/query_intent.py`：结构化 Query Intent LLM。
- `agent/query_planner.py`：查询改写、分解和有限变体预算。
- `memory.service.memory_search`：长期记忆检索。
- `memory.service.task_status_search`：Task + 相关 Episodic 查询。
- `memory.semantic_profile_projection`：Semantic Projection + Live Delta。
- `memory.repository.get_memory_timeline`：Memory Version/Source 时间线。
- `memory.consistency.wait_for_memory_barrier`：读写一致性屏障。
- Memory Trace、Hook、LLM Usage、访问控制和敏感信息检查。

当前需要解决的问题：

1. 句子数被过度用于推断问题数，单句多问题和多句少问题容易误判。
2. `simple / complex` 同时承担问题数量、检索策略和推理难度，语义混杂。
3. fast path、复杂变体和 ReAct 都能决定检索行为，路径不统一。
4. ReAct Prompt 暴露多个工具，模型可能选择错误工具或产生不稳定调用次数。
5. 当前工具执行器实现的 action 多于 Prompt 声明，缺少严格的 Tool Allowlist 边界。
6. Evidence Quality 主要依赖命中和 score，缺少逐 Query Unit 的覆盖、冲突和直接证据判断。
7. Semantic 当前事实查询尚未形成正式的 Resolver 契约。
8. 复杂问题的子问题虽然有 `depends_on` 设计需求，但当前执行流程没有正式的依赖图。

---

## 3. 统一 AskPlan

### 3.1 不再使用 `simple / complex` 作为流程分支

问题数量和推理难度分开表达：

```text
units 数量
→ 用户有几个可独立回答的目标

answer_mode / evidence_mode
→ 每个目标需要直接事实、列表、时间线、比较、因果还是总结
```

示例：

| Query | Unit 数量 | answer_mode |
|---|---:|---|
| 我现在住哪？ | 1 | direct |
| 我住哪，又喜欢喝什么？ | 2 | direct |
| 我的研究方向为什么发生变化？ | 1 | causal |
| RAG 学习和简历有什么关系？ | 3 | compare / relationship |

### 3.2 建议数据模型

新增 `agent/ask_models.py`：

```python
class AskContextItem(BaseModel):
    text: str
    source: Literal["current_message", "session_context"]


class QueryUnit(BaseModel):
    id: str
    question: str
    source_spans: list[str]
    intent: Literal[
        "task_state",
        "preference_current",
        "semantic_current",
        "semantic_history",
        "episodic_history",
        "note_lookup",
        "memory_history",
    ]
    memory_type: Literal[
        "task",
        "preference",
        "semantic",
        "episodic",
    ] | None
    facet: Literal[
        "identity",
        "location",
        "education",
        "career",
        "project",
        "learning",
        "capability",
        "device",
        "other",
    ] | None
    topic: str | None
    time_mode: Literal["current", "recent", "history", "all"]
    evidence_mode: Literal[
        "current_state",
        "inventory",
        "timeline",
        "source_quote",
        "aggregate",
    ]
    need_source_evidence: bool
    depends_on: list[str]
    priority: int


class AskPlan(BaseModel):
    original_query: str
    context: list[AskContextItem]
    units: list[QueryUnit]
    answer_mode: Literal[
        "direct",
        "list",
        "timeline",
        "compare",
        "causal",
        "summary",
    ]
```

### 3.3 Planner 输入

Planner 输入只包括：

- 当前用户问题。
- 当前时间和时区。
- 有限的会话上下文，不传无限历史。
- 允许的 intent、facet、time mode、answer mode 定义。
- Unit 数量和依赖规则。

Planner 不读取数据库，不查看候选 Memory，也不输出：

- Tool 名称。
- `space_id`、tenant 或访问权限。
- 检索阈值、limit、RRF 权重。
- Memory ID、Note ID。
- 任务最终状态、偏好最终极性、事实最终结论。

### 3.4 Planner 输出原则

1. 多句不等于多问题，背景句进入 `context`。
2. 单句可以有多个 Query Unit。
3. 每个 Unit 必须能独立检索或依赖已有 Unit。
4. `source_spans` 必须来自当前问题；使用会话指代时必须标记 `session_context`。
5. 不能为了达到目标数量强行拆分。
6. 相同目标的不同表述应合并。
7. 分析型 Unit 可以依赖事实型 Unit，不必重复检索相同证据。

---

## 4. Plan Validator

新增 `agent/ask_plan_validator.py`，在任何检索发生前执行。

校验内容：

1. `units` 数量在 `[1, ASK_MAX_UNITS]` 内。
2. `source_spans` 能在原问题中定位；会话来源必须能在允许的 session context 中定位。
3. intent、memory type、facet、time mode 组合合法。
4. `depends_on` 引用存在，并且依赖图无环。
5. Unit 不得包含写操作、状态修改或偏好极性修改指令。
6. 根据 `subject + topic + time_mode + evidence_mode` 合并重复 Unit。
7. Planner 输出非法、为空或模型不可用时，生成一个安全降级 Unit：

```text
question = 原始问题
intent = note_lookup
time_mode = all
need_source_evidence = true
```

降级结果只能回答“笔记中是否提到”，不能声称当前任务状态、当前偏好或当前事实。

---

## 5. 工具分层

### 5.1 LLM 可见层

Planner 和 Answer LLM 都不直接调用检索工具：

```text
Planner LLM → AskPlan JSON
Answer LLM  → AnswerDecision JSON
```

### 5.2 编排层

新增 `agent/ask_executor.py`：

```text
execute_ask_plan(space_id, plan, access_context)
```

它负责：

- 根据 Unit intent 选择领域工具。
- 无依赖 Unit 并发执行。
- 有依赖 Unit 按拓扑顺序执行。
- 统一 Tool Budget、超时和错误隔离。
- 输出分组 Evidence Bundle。

### 5.3 领域工具层

领域工具保留独立契约，方便单测和独立评估，但只能由 Executor 调用。

#### `search_task_state`

```python
search_task_state(
    query: str,
    status: Literal["todo", "done", "all"] = "all",
    include_lifecycle: bool = True,
    include_events: bool = True,
) -> TaskEvidenceResult
```

内部复用并扩展现有 `task_status_search`：

```text
Task exact / structured / family / sparse / dense
→ RRF
→ Task policy rerank
→ 当前 Task State
→ 最多补充少量相关 Episodic
```

约束：

- `task_family_key` 仅授权召回，不授权同一任务实例。
- 当前状态仅来自 Task Memory/Version。
- Episodic 只能解释历史事件，不能充当当前任务状态。
- 任务持久化状态继续只允许 `todo / done`。

#### `search_preferences`

```python
search_preferences(
    query: str,
    scope: str | None = None,
    polarity: Literal["positive", "negative", "any"] = "any",
) -> PreferenceEvidenceResult
```

内部使用：

```text
assertion key
+ structured topic/scope/qualifiers
+ preference family
+ FTS
+ Dense
→ Weighted RRF
→ Preference policy rerank
```

约束：

- family 仅用于召回。
- identity 由 topic、scope、qualifiers 等结构化字段决定。
- polarity 不参与 identity，但参与当前偏好裁决。
- 排序模型不能反转偏好极性。

#### `resolve_semantic_facts`

```python
resolve_semantic_facts(
    query: str,
    facet: str = "auto",
    mode: Literal["current", "history", "all"] = "current",
) -> SemanticEvidenceResult
```

`current` 模式：

```text
Semantic Hybrid Retrieval
+ Facet Projection
+ Projection 尚未处理的 Live Delta
→ 当前候选 / uncertain / conflict
```

`history` 模式：

```text
同 facet/topic 的多条 Semantic Fact
→ 按事实时间排列
→ 构造事实时间线
```

约束：

- Resolver 只读，不修改 Semantic Memory。
- Semantic History 是多条事实，不是单条 Memory 的 Version History。
- Projection 是可重建派生层，失败时必须回退到 Raw Facts + Live Delta。
- 多个不冲突事实可以同时有效。
- 没有明确替代证据时返回 conflict/uncertain，不强行选新值。

#### `search_episodes`

```python
search_episodes(
    query: str,
    start_time: str | None = None,
    end_time: str | None = None,
) -> EpisodicEvidenceResult
```

内部使用 topic/entity、时间过滤、FTS、Dense 和 recency。Recency 只影响排序，不删除历史事件。

#### `search_notes`

```python
search_notes(
    query: str,
    note_type: str | None = None,
    tags: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> NoteEvidenceResult
```

该名称替代当前容易与 Semantic Memory 混淆的 `semantic_search`。内部继续封装 Metadata、FTS、Lexical、Trigram、Dense 和 Weighted RRF。

### 5.4 证据展开层

#### `expand_memory_evidence`

```python
expand_memory_evidence(
    memory_ids: list[str],
    include_versions: bool = False,
    include_source_notes: bool = True,
) -> list[ExpandedEvidence]
```

内部统一：

```text
Memory
→ MemorySource
→ Source Note
→ evidence_span / original text
```

只允许展开本轮已召回且通过访问控制的 ID。

#### `get_memory_timeline`

主要用于 Task、Preference、Episodic 的 Version/Source 历史；Semantic 历史不走该工具。

#### `get_note`

只允许读取已召回 Note 或已由 MemorySource 关联出的 Note。默认返回受字符预算限制的原文和 evidence span，不向回答模型无限加载全文。

---

## 6. Evidence Bundle

新增 `agent/evidence_models.py`：

```python
class EvidenceItem(BaseModel):
    evidence_id: str
    source_kind: Literal["memory", "memory_version", "note"]
    memory_type: str | None
    content: str
    evidence_span: str | None
    memory_id: str | None
    note_id: str | None
    source_note_ids: list[str]
    observed_at: str | None
    event_time: str | None
    recorded_at: str | None
    retrieval_channels: list[str]
    evidence_role: Literal[
        "current_candidate",
        "historical",
        "supporting",
        "conflicting",
        "raw_note",
    ]


class UnitResolution(BaseModel):
    status: Literal["resolved", "partial", "conflict", "not_found"]
    value: str | None
    reason_code: str
    selected_evidence_ids: list[str]
    conflicting_evidence_ids: list[str]


class UnitEvidenceBundle(BaseModel):
    unit_id: str
    evidence: list[EvidenceItem]
    resolution: UnitResolution
```

注意：

- `retrieval score` 可以用于排序和 Trace，但不能单独决定 Evidence 是否足够。
- Task、Preference 的状态/极性由确定性规则裁决。
- Semantic 无法确认新旧时必须返回 `conflict`，由回答层如实说明。
- Answer LLM 不得引用 Bundle 之外的 ID。

---

## 7. 时间语义

Ask 重构必须统一三个时间字段：

```text
event_time
用户明确描述事情发生或事实生效的时间

observed_at
系统收到原始 Note 的时间

recorded_at
Memory/Version/Source 写入数据库的时间
```

排序优先级：

```text
明确 event_time
→ observed_at
→ recorded_at
```

禁止直接把 `memory.updated_at` 当成事实发生时间。例如用户今天说“我去年搬到上海”，`recorded_at` 是今天，`event_time` 是去年。

第一版如果缺少可靠 `event_time`，Evidence Bundle 必须显示它为空，不允许静默伪造。

---

## 8. Evidence Quality 与受控补证

每个 Unit 独立判断：

```text
resolved
有足够直接证据，可以回答

partial
命中了相关内容，但没有覆盖完整问题

conflict
存在互相冲突且无法确定替代关系的证据

not_found
没有直接证据
```

禁止使用统一规则：

```python
score >= threshold
```

不同领域执行固定 Fallback：

```text
Task
→ Task 未命中
→ 相关 Episodic
→ Source Note

Preference
→ 当前 assertion 未命中
→ Source Note / Note

Semantic
→ Projection 缺失或 stale
→ Raw Facts + Live Delta
→ Source Note

Episodic
→ Episodic 未命中
→ Note

Note
→ 搜索未命中
→ not_found
```

第一版不保留自由 ReAct。只允许：

1. 一轮确定性 Fallback。
2. 一次已选 Evidence 的 Source/Version 展开。
3. 不允许回答模型重新指定任意工具或任意 ID。

后续只有在评测证明“Planner 错路由”是主要失败原因时，才考虑增加一次受限 Replan；该功能默认关闭。

---

## 9. Answer Synthesizer

Answer LLM 输入：

- 原始问题。
- 经过校验的 AskPlan。
- 各 Unit 的 Evidence Bundle。
- Unit 之间的依赖关系。
- 回答风格和字符预算。

Answer LLM 输出：

```json
{
  "unit_answers": [
    {
      "unit_id": "u1",
      "answer": "你目前住在上海。",
      "evidence_ids": ["mem_xxx"]
    }
  ],
  "final_answer": "你目前住在上海。",
  "unresolved_units": []
}
```

校验规则：

1. 每个回答引用的 ID 必须来自对应 Unit Bundle。
2. `not_found` 不得生成肯定答案。
3. `conflict` 必须明确说明存在冲突，不得暗自选边。
4. Task Answer 不得把 Episodic 当作当前 Task 状态。
5. Preference Answer 不得反转 polarity。
6. 引用失败时使用确定性模板返回，不丢弃已验证证据。

---

## 10. 并发与预算

建议配置：

```env
SUIXINJI_ASK_V2_ENABLED=false
SUIXINJI_ASK_V2_SHADOW=true

SUIXINJI_ASK_MAX_UNITS=4
SUIXINJI_ASK_HARD_MAX_UNITS=6
SUIXINJI_ASK_MAX_RETRIEVAL_ROUNDS=2
SUIXINJI_ASK_MAX_FALLBACK_UNITS=2
SUIXINJI_ASK_MAX_HYDRATE_IDS=5
SUIXINJI_ASK_EVIDENCE_PER_UNIT=5
SUIXINJI_ASK_PLANNER_TIMEOUT_SECONDS=12
SUIXINJI_ASK_EXECUTOR_TIMEOUT_SECONDS=15
SUIXINJI_ASK_ANSWER_TIMEOUT_SECONDS=20
```

说明：

- 不设置 `TARGET_SUBQUESTIONS`，避免模型为了达到目标而过度拆分。
- 正常最多 4 个 Unit；6 只是异常输入的硬上限。
- 无依赖 Unit 可以并发执行，但同一 Unit 内的状态/时间裁决必须保持确定性顺序。
- 并发不能绕过 PostgreSQL space 隔离、Hook、Rate Limit 和 AccessContext。

---

## 11. Trace 与可观测性

建议新增步骤：

```text
ask_received
ask_plan_generated
ask_plan_validated
ask_unit_started
ask_unit_retrieved
ask_unit_resolved
ask_fallback_executed
ask_evidence_expanded
ask_answer_generated
ask_answer_validated
ask_finished
```

Trace 至少记录：

- Planner 模型、耗时、token usage、key pool 选择结果。
- Unit 数量、intent、time/evidence mode、依赖关系。
- 每个 Unit 调用了哪个领域工具。
- 每个通道候选数、融合后候选数、最终 Evidence IDs。
- Fallback 原因和次数。
- `resolved / partial / conflict / not_found`。
- Answer 引用的 Evidence IDs。
- 总耗时、Planner/Executor/Answer 分段耗时。

Trace 不记录敏感原文、API Key 或完整 Prompt。

---

## 12. 实施阶段

### P0：契约和测试先行

新增：

```text
agent/ask_models.py
agent/evidence_models.py
agent/ask_plan_validator.py
tests/test_ask_plan_models.py
tests/test_ask_plan_validator.py
```

完成：

- AskPlan / QueryUnit / Evidence Bundle Schema。
- Unit 数量、source span、去重和依赖校验。
- 不涉及生产 `/ask` 路径。

### P1：Planner Shadow

新增或改造：

```text
agent/ask_planner.py
memory/prompts.py
core/model_policy.py
core/settings.py
```

完成：

- 每次真实 Ask 同步或异步生成 V2 AskPlan。
- `SUIXINJI_ASK_V2_SHADOW=true` 时只写 Trace，不影响用户答案。
- 建立单句多问题、多句少问题、多句多问题数据集。

### P2：领域工具标准化

新增建议：

```text
agent/retrieval/task_tool.py
agent/retrieval/preference_tool.py
agent/retrieval/semantic_tool.py
agent/retrieval/episodic_tool.py
agent/retrieval/note_tool.py
agent/retrieval/evidence_tool.py
```

复用：

- `memory_search`
- `task_status_search`
- `get_memory_timeline`
- Semantic Projection
- PostgreSQL Note Hybrid Search

这一阶段先保持旧 `/ask` 可调用现有实现，新工具通过适配器逐步替换，避免一次重写所有 Retriever。

### P3：Executor 与 Evidence Resolver

新增：

```text
agent/ask_executor.py
agent/evidence_resolver.py
agent/evidence_quality.py
```

完成：

- intent → domain tool 的确定性 Dispatch。
- Unit DAG 执行。
- 并发、预算、超时和单 Unit 错误隔离。
- 类型化 Resolution 和一轮确定性 Fallback。

### P4：Answer Synthesizer

新增或改造：

```text
agent/ask_answer.py
agent/answer_models.py
agent/query_agent.py
```

完成：

- 以 Unit Bundle 为输入生成答案。
- 严格 Evidence ID 校验。
- unresolved/conflict 的确定性降级模板。
- 新旧答案双跑对比。

### P5：灰度切换与旧路径退役

顺序：

```text
Shadow
→ 内部测试 space
→ 单用户灰度
→ 全量 Ask V2
→ 删除旧 fast/simple/complex/ReAct 主编排
```

旧功能在 V2 指标稳定前保留：

- `/trace latest`
- Memory watermark。
- Sensitive query block。
- provisional read-after-write。
- AccessContext。
- LLM Key Pool、重试和 Hook。

---

## 13. 测试设计

### 13.1 Planner 数据集

至少覆盖：

1. 单句单问题。
2. 单句多问题。
3. 多句单问题，包含背景句。
4. 多句多问题。
5. 多问题共享同一 topic，需要去重。
6. 比较/关系 Unit 依赖其他 Unit。
7. 指代依赖会话上下文。
8. 当前/历史/最近/all 时间范围。
9. Task、Preference、Semantic、Episodic、Note 混合问题。
10. 否定、反问、中英文混合、噪声和超长输入。

指标：

```text
Plan Schema Valid Rate
Unit Count Accuracy
Unit Precision / Recall / F1
Intent Accuracy / Macro-F1
Memory Type Accuracy
Facet Accuracy
Time Mode Accuracy
Dependency Edge P/R/F1
Unsupported Unit Rate
Duplicate Unit Rate
```

### 13.2 Tool 与 Resolver 测试

每个领域工具独立测试：

```text
Candidate Recall@K
Evidence Precision / Recall / F1
Current State Accuracy
Timeline Complete@K
Conflict Detection P/R/F1
Source Link P/R/F1
Cross-space Violation Count
```

### 13.3 端到端 Ask 测试

复用现有 Layer3 数据，同时增加多 Unit Ask 数据集。

核心指标：

```text
Per-Unit Answer Coverage
All-Units Complete Rate
Claim Precision / Recall / F1
Citation Precision / Recall / F1
No-answer Precision / Recall / F1
Current Fact Accuracy
Task State Accuracy
Timeline Exact / Complete@K
Conflict Disclosure Accuracy
Unsupported Claim Rate
```

工程指标：

```text
Planner p50 / p95
Executor p50 / p95
Answer p50 / p95
End-to-End p50 / p95
LLM Calls per Ask
Tool Calls per Ask
Fallback Rate
Planner / Executor / Answer Failure Rate
```

### 13.4 回归红线

以下指标不得退化：

- Cross-space access violation 必须继续为 0。
- 敏感信息检索泄漏必须继续为 0。
- Task 状态仍只有 `todo / done`。
- Episodic 不得成为当前 Task 状态。
- 排序模型不得修改状态、极性和冲突结论。
- Citation 中不得出现未选 Evidence ID。
- 当前 Layer3 current-state、timeline、citation/no-answer 指标不得出现显著回退。

---

## 14. 灰度、回滚与数据边界

V2 Ask 只读现有 Memory、Version、Source、Projection 和 Note，不修改写入端数据模型。

Feature Flag：

```text
ASK_V2_ENABLED=false
ASK_V2_SHADOW=true
```

回滚只需要关闭 V2 Flag，恢复旧 `answer_question_result` 路径；不需要回滚 Memory 数据。

Shadow 期间保存：

- V1/V2 Plan。
- V1/V2 selected Evidence IDs。
- V1/V2 Answer。
- 两者延迟、LLM 调用数和差异原因。

禁止 Shadow 请求污染：

- 用户长期记忆。
- Note。
- Semantic Projection。
- 用户画像。
- Task/Preference 状态。

---

## 15. 建议的第一轮实现范围

第一轮只完成：

1. AskPlan Schema。
2. Planner LLM。
3. Plan Validator。
4. Planner Shadow Trace。
5. Planner 专项测试集与指标。

先回答三个问题：

```text
LLM 能否正确区分背景和真正问题？
LLM 能否正确处理单句多问题、多句少问题、多句多问题？
LLM 是否会编造原问题不存在的 Query Unit？
```

这些指标稳定后，再开始替换 Retriever 和 ReAct。不要在 Planner 尚未验证时同时重写所有检索工具，否则失败时无法区分是 Plan 错、Retrieval 错还是 Answer 错。

---

## 16. 最终目标

```text
一条 Ask Workflow
+ 一个统一 AskPlan
+ 多个受控 Query Unit
+ 五个类型化领域 Retriever
+ 一套 Evidence Bundle
+ 一轮确定性补证
+ 一个带严格引用约束的 Answer Synthesizer
```

面试概括：

> 随心记的 `/ask` 从开放式 ReAct 重构为 Plan-Execute 工作流。Planner 负责从自然语言中抽取背景和 1～N 个 Query Unit，Executor 根据 Unit intent 确定性调用 Task、Preference、Semantic、Episodic 或 Note 领域 Retriever。每个 Unit 独立召回、融合和状态/时间裁决，证据不足时只执行一轮受控补证，最后由 Answer 模型基于分组 Evidence Bundle 生成带引用答案。该设计能统一处理单句多问题、多句少问题和多句多问题，同时保留 Memory 状态语义、来源追溯、延迟预算和可评测性。
