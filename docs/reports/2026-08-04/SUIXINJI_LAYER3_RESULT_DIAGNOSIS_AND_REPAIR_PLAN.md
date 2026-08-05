# 随心记 Layer 3 检索与回答结果诊断及修复计划

> 文档性质：问题诊断、实施顺序与验收方案  
> 适用项目：`/home/zcj/suixinji`  
> 评测运行：`layer3_20260803_065152`  
> 测试规模：520 Cases  
> 后端：真实 PostgreSQL  
> 检索模式：Hybrid  
> 生产入口：`memory.service.memory_search` + `agent.query_agent.answer_question`

---

# 1. 总体结论

本次第三阶段已经证明：

```text
明确的当前状态查询
PostgreSQL检索延迟
跨Space隔离
查询只读性
```

表现较好。

但第三阶段整体尚未通过，当前问题由六部分组成：

1. **敏感信息权限过滤发生在错误层级**；
2. **Answer LLM大面积触发限流，导致回答指标失真**；
3. **历史Version没有进入正式检索能力**；
4. **列表型、多Memory和复杂查询仍被当成普通单跳语义搜索**；
5. **无答案、冲突、歧义和受限回答缺少统一结构化契约**；
6. **评测器将过期、无关、越权等不同违规混成同一个Stale指标**。

当前不建议先调RRF权重或Rerank参数。

正确修复顺序：

```text
评测口径修正
→ 权限控制与Answer可用性
→ 历史Version检索
→ 列表/多Memory查询
→ 语义检索链路
→ 无答案/冲突/澄清
→ Claim与Citation
→ 全量回归
```

---

# 2. 当前基线

## 2.1 运行完整性

| 项目 | 结果 |
|---|---:|
| Cases | 520 |
| 执行/Seed错误 | 0 |
| Answer错误 | 289 |
| Answer错误类型 | `RateLimitExceeded` |
| 跨Space违规 | 0 |
| 查询修改业务状态 | 0 |

说明：

- 测试框架、Seed、PostgreSQL查询和清理链路已跑通；
- 查询没有修改Memory、Version、Source或Pending Review业务状态；
- 但289/520的回答调用失败，使回答层结果不能直接作为纯质量结论。

## 2.2 总体检索结果

| 指标 | 当前 |
|---|---:|
| Recall@1 | 49.15% |
| Recall@3 | 54.13% |
| Recall@5 | 54.13% |
| MRR | 0.649 |
| nDCG@10 | 0.489 |

## 2.3 回答与引用结果

| 指标 | 当前 |
|---|---:|
| Claim F1 | 29.91% |
| Citation F1 | 43.14% |
| Citation Exact-set | 25.19% |
| No-answer F1 | 3.23% |
| Answer错误率 | 55.58% |

这些结果同时混入：

```text
检索失败
回答逻辑失败
限流失败
评测器识别失败
```

修复前不能将其解释为单纯的模型回答能力。

## 2.4 硬约束

| 项目 | 当前 |
|---|---:|
| Sensitive Access Violations | **15** |
| Cross-space Violations | 0 |
| Business State Mutations | 0 |
| Forbidden Claim Rate | 0 |

敏感权限违规是当前最高优先级问题。

---

# 3. 哪些能力已经通过

## 3.1 当前状态检索

`current_state_retrieval`：

| 指标 | 结果 |
|---|---:|
| Recall@1 | 100% |
| Current Hit@1 | 100% |
| MRR | 1.0 |
| nDCG@10 | 1.0 |

说明明确询问：

```text
当前偏好
当前Task状态
当前事实
明确Episodic事件
```

时，正确Memory能稳定排在第一位。

本轮修改不得破坏这条已通过链路。

## 3.2 PostgreSQL查询性能

| 指标 | 结果 |
|---|---:|
| Retrieval P50 | 274 ms |
| Retrieval P95 | 339 ms |
| Retrieval P99 | 476 ms |

检索性能已经满足“一秒内”的基础目标。

修复复杂查询时应避免让所有Query无条件执行昂贵多路查询。

## 3.3 数据隔离与只读性

```text
跨Space错误召回 = 0
查询修改业务状态 = 0
```

后续新增Version检索和权限过滤后必须继续保持。

---

# 4. P0：先修评测口径

## 4.1 问题：回答失败与回答错误混在一起

当前289个Case触发：

```text
RateLimitExceeded
```

但这些Case仍进入Claim、Citation和No-answer总指标。

这会把：

```text
服务不可用
```

错误解释成：

```text
答案内容不正确
```

## 修复方案

输出三组指标：

### A. Answer Availability

```text
answer_attempts
answer_success_count
answer_error_count
answer_success_rate
rate_limit_rate
timeout_rate
```

### B. Successful-call Quality

仅在Answer成功的Case中计算：

```text
Claim P/R/F1
Citation P/R/F1
No-answer F1
Answer Type Accuracy
```

### C. End-to-end Quality

将回答调用失败视为失败，计算线上端到端质量。

报告必须同时展示三组，不能只展示一种。

---

## 4.2 问题：No-answer从文本猜测，识别不可靠

系统已经生成过：

```text
没有找到足够相关的记录
无法根据现有记忆确认
```

但No-answer统计只有1个TP。

说明评测器或Answer返回结构无法稳定识别拒答。

## 修复方案

`answer_question`返回结构化结果：

```python
class AnswerResult:
    answer_type: str
    no_answer: bool
    reason_code: str | None
    answer: str
    claims: list[Claim]
    citations: list[Citation]
```

建议`answer_type`固定为：

```text
answered
no_answer
qualified_history_only
conflict
clarification
restricted
system_error
```

不得再只靠最终文本中的“没有”“无法”等关键词判断。

---

## 4.3 问题：Stale指标混入无关与越权结果

当前：

```text
stale_retrieval_rate = 22.12%
```

实际使用了`must_not_return_refs`，其中包含：

```text
过期Memory
普通干扰Memory
越权Memory
歧义候选
```

所以它不是纯粹的Stale Rate。

## 修复方案

拆成四个指标：

```text
Stale Retrieval Violation
Irrelevant Retrieval Rate
Access-control Retrieval Violation
Ambiguous-candidate Answer Usage
```

真正Stale只统计：

```text
status = superseded
valid_until < query_time
历史Version被当成当前状态
旧状态被用来回答当前问题
```

另外保留通用：

```text
Must-not-return Context Violation Rate
```

---

## 4.4 问题：Precision@K口径容易误导

单答案Case只返回一条正确结果时：

```text
P@10 = 1 / 10 = 10%
```

会让人误以为返回了9条错误结果。

## 修复方案

同时输出：

```text
Fixed-denominator Precision@K
Precision@Returned
Average Returned Count
Hit@K
```

单答案当前状态场景重点展示：

```text
Hit@1
Recall@1
MRR
```

---

## 4.5 问题：Route与真实检索通道混为一谈

当前只有：

```text
observed_route
```

但生产链路可能同时经过多个Channel。

## 修复方案

分开记录：

```text
planner_route
executed_channels
fused_channels
final_context_channels
```

Route Accuracy只做辅助诊断，不作为核心Case失败条件。

---

# 5. P0：敏感信息权限控制

## 5.1 问题

15个敏感权限Case全部出现违规：

```text
requester = external_app
allow_sensitive = false
memory.access_scope = owner_only
```

受限Memory仍进入了检索结果。

即使最终答案没有直接输出敏感值，也属于安全失败，因为敏感内容已经可能进入：

```text
Retriever结果
Rerank
Context Builder
Answer LLM
日志
```

## 5.2 正确安全边界

权限过滤必须发生在：

```text
Repository / Retrieval Channel
```

而不是Answer生成阶段。

正确链路：

```text
Query + AccessContext
→ 数据库/通道级权限过滤
→ Fusion
→ Rerank
→ Context
→ Answer
```

## 5.3 实施要求

为所有通道统一增加：

```text
space_id
requester
allow_sensitive
memory.sensitivity
memory.access_scope
```

必须覆盖：

```text
structured
exact
fulltext
trigram
vector
fallback
raw diagnostic channel
```

推荐统一方法：

```python
def build_access_predicate(access_context) -> AccessPredicate:
    ...
```

所有Repository查询必须接收同一个Predicate，禁止不同通道自行实现。

## 5.4 日志保护

敏感值不得进入普通日志。

日志仅允许：

```text
memory_id
sensitivity
access_scope
filter_reason
```

不得打印完整`content/current_value/evidence_text`。

## 5.5 验收

```text
Sensitive retrieval violations = 0
Sensitive context violations = 0
Sensitive answer violations = 0
Sensitive log violations = 0
```

这是硬门槛。

---

# 6. P0：Answer限流与可用性

## 6.1 问题

```text
289 / 520 = 55.58%
```

的Answer调用发生`RateLimitExceeded`。

这不仅影响性能，也使Claim、Citation和No-answer无法正确评估。

## 6.2 修复方案

### 方案A：遵守服务端Retry-After

捕获限流异常中的：

```text
retry_after_ms
```

使用：

```text
等待指定时间
+ 小幅Jitter
+ 有上限的重试
```

禁止立即快速重试。

### 方案B：全局限流器

多个评测Worker必须共享：

```text
RPM
TPM
最大并发请求数
```

不能每个Worker单独限流。

建议：

```python
class SharedLLMRateLimiter:
    async def acquire(request_tokens: int) -> None:
        ...
```

### 方案C：评测基线先使用并发1

先运行：

```text
concurrency = 1
```

获得无大面积限流干扰的质量基线。

之后再测试：

```text
1 → 2 → 3
```

用于测可用性与吞吐。

### 方案D：简单答案使用确定性渲染

以下场景不必调用通用Answer LLM：

```text
单一Task当前状态
单一Preference
单一Semantic当前值
明确无权限
明确无记录
明确Conflict
明确Clarification
```

由结构化Result通过模板生成。

只有：

```text
多Claim总结
复杂历史时间线
需要自然语言组织的综合回答
```

才调用Answer LLM。

### 方案E：系统错误与No-answer分开

限流失败返回：

```json
{
  "answer_type": "system_error",
  "retryable": true,
  "reason_code": "rate_limited"
}
```

不得返回空字符串，也不得计为业务No-answer。

## 6.3 验收

```text
Answer availability ≥ 99%
RateLimit error rate ≤ 1%
System error与No-answer零混淆
成功调用质量单独输出
```

---

# 7. P1：历史Version检索

## 7.1 问题

`history_and_temporal`：

```text
Current Hit = 100%
History Hit = 0%
nDCG@10 = 0
```

系统只找到当前Memory，没有把Version作为可检索历史证据。

因此无法可靠回答：

```text
完成前是什么状态
最初是什么状态
经历了哪些变化
什么时候发生转移
```

## 7.2 不建议的修法

不要简单地把所有Version都混入普通全局向量库。

风险：

```text
当前状态查询被旧Version污染
相同Memory多个版本占满Top-K
过期信息排到当前信息前面
```

## 7.3 推荐两阶段历史查询

```text
历史意图识别
→ 找到目标Memory Identity
→ 查询该Memory的Version链
→ 根据问题选择Version
→ 按sequence/valid time生成时间线
```

## 7.4 新增领域接口

建议增加：

```python
async def get_memory_timeline(
    space_id: str,
    memory_id: str,
) -> list[MemoryVersion]:
    ...

async def get_previous_version(
    memory_id: str,
    before_sequence: int | None = None,
) -> MemoryVersion | None:
    ...

async def get_state_at_time(
    memory_id: str,
    query_time: datetime,
) -> MemoryVersion | None:
    ...
```

如Query还未确定Memory身份，先使用当前Memory检索定位Identity。

## 7.5 历史意图

Query Planner至少识别：

```text
以前
之前
最初
后来
变化
经历
完成前
什么时候变成
从A到B
历史
过程
```

并输出：

```python
query_intent = "history"
history_operation = (
    "previous_state"
    | "initial_state"
    | "timeline"
    | "transition_time"
    | "state_at_time"
)
```

## 7.6 时间排序

优先级：

```text
valid_from / valid_until
→ Version sequence
→ observed_at
→ created_at
```

不要只按数据库插入时间生成业务时间线。

## 7.7 Version引用

每个Version必须能够回溯对应Source：

```text
Version
→ source_note_id / source relation
→ evidence text
```

历史答案的Citation必须引用支持该历史状态的Source，而不是只引用当前Memory。

## 7.8 验收

```text
History Hit@3 ≥ 95%
History Hit@5 ≥ 98%
Temporal Order Accuracy ≥ 98%
Previous-state Accuracy ≥ 95%
Timeline Claim F1 ≥ 95%
History Citation F1 ≥ 98%
```

---

# 8. P1：列表型与多Memory查询

## 8.1 问题

以下查询不能靠普通单跳Semantic Search解决：

```text
列出我当前三个项目的状态
列出最近两件经历
概括我的偏好、设备和语言
总结某任务从开始到完成的过程
```

当前多Memory数据集：

```text
Recall@3 = 31.25%
MRR = 0.375
nDCG@10 = 0.327
```

## 8.2 Query Planner增加意图

建议固定：

```text
single_lookup
list_tasks
list_recent_episodes
profile_summary
history_timeline
conflict_check
clarification_needed
```

## 8.3 确定性列表接口

### Task列表

```python
async def list_tasks(
    space_id: str,
    statuses: set[str] | None,
    limit: int,
    order_by: str,
) -> list[Memory]:
    ...
```

支持：

```text
当前任务
已完成任务
阻塞任务
最近任务
```

### Episodic列表

```python
async def list_recent_episodes(
    space_id: str,
    limit: int,
    before: datetime | None,
) -> list[Memory]:
    ...
```

必须明确“最近”使用：

```text
事件业务时间
而非数据库Seed时间
```

推荐优先：

```text
valid_from / event_time
→ observed_at
→ created_at
```

### Profile Summary

根据问题要求的槽位执行并行结构化查询：

```text
Preference
Primary Device
Preferred Language
Current Focus
Location
```

不要用一个向量Query期待自动召回所有槽位。

## 8.4 多Memory检索预算

为不同意图设置不同预算：

```text
single_lookup：Top 3
list_tasks：结构化Limit N
profile_summary：每槽位Top 1
history_timeline：目标Memory + 全Version链
```

避免所有查询统一`top_k=10`。

## 8.5 Context Builder

最终上下文需保留：

```text
logical group
memory type
current/history role
source support
```

不能将多个Memory拼成无结构文本后交给LLM自行猜测。

## 8.6 验收

```text
Task List Recall ≥ 98%
Recent Episodic Recall ≥ 98%
Profile Summary Claim F1 ≥ 95%
Multi-memory Citation F1 ≥ 98%
History Synthesis Claim Precision ≥ 95%
```

---

# 9. P1：语义改写与Vector链路

## 9.1 问题

`semantic_paraphrase_and_noise`：

```text
Recall@1 = 60%
MRR = 0.60
```

其中：

```text
错别字：较好
间接指代：较好
部分中英混合：可用
普通语义改写：明显失败
纯英文跨语言：不稳定
```

## 9.2 必须先排查，而不是直接调阈值

### 检查1：Seed是否生成向量

确认评测Seed后：

```text
memory_vectors中存在目标Memory
embedding维度正确
embedding模型版本正确
```

如果Seed只写Memory表而未写向量，Vector评测无效。

### 检查2：写入与查询模型是否一致

记录：

```text
embedding provider
model
dimension
normalization
distance metric
```

写入和查询必须一致。

### 检查3：向量通道是否真实执行

每个Case保存：

```text
query embedding success
vector candidate count
raw similarity
threshold before/after
```

不能只看到Planner说`semantic_search`，却不知道向量库是否返回候选。

### 检查4：阈值0.55

输出相关与无关结果的分数分布：

```text
positive score histogram
negative score histogram
```

再选择阈值。

禁止仅为了数据集通过直接把阈值降到很低。

### 检查5：跨语言Embedding

用单独小集验证：

```text
中文Memory ↔ 英文Query
中文Memory ↔ 中英混合Query
```

如果模型本身跨语言能力不足，应增加Query Rewrite或使用多语言Embedding。

## 9.3 Hybrid融合

推荐召回：

```text
Structured
Full-text
Trigram
Vector
```

后使用RRF或标准化融合。

但对于语义改写：

```text
Vector候选必须真实存在
```

不能依赖Structured通道碰巧命中。

## 9.4 Query Rewrite

只对低召回风险Query启用：

```text
英文
中英混合
间接指代
短Query
错别字
```

Rewrite必须保留原Query，并记录：

```text
original query
rewritten queries
每条Rewrite召回贡献
```

## 9.5 验收

按tag分别报告：

```text
paraphrase Recall@3 ≥ 95%
mixed_language Recall@3 ≥ 90%
typo Recall@3 ≥ 95%
indirect_reference Recall@3 ≥ 90%
overall nDCG@10 ≥ 0.90
```

---

# 10. P1：无答案、冲突、历史限定与歧义

## 10.1 统一Answer Decision

在调用自然语言生成前，先生成结构化决策：

```python
@dataclass
class AnswerDecision:
    answer_type: str
    reason_code: str
    selected_memories: list[str]
    selected_versions: list[str]
    conflicts: list[str]
    clarification_options: list[str]
    access_denied: bool
```

Answer LLM只负责表达，不负责决定是否有答案。

---

## 10.2 Absent：无相关记录

条件：

```text
没有达到最低相关阈值的结果
没有结构化槽位命中
```

输出：

```text
answer_type = no_answer
reason_code = insufficient_memory
```

无关Memory不得进入最终Context或Citation。

---

## 10.3 Stale-only：只有历史信息

例如只知道：

```text
以前住在上海
```

用户问：

```text
现在住在哪里
```

输出：

```text
answer_type = qualified_history_only
```

可以说历史事实，但必须明确：

```text
无法确认当前状态
```

禁止将历史值表达成当前值。

---

## 10.4 Conflict：存在冲突

当前查询不能只返回Active Memory而完全丢掉Pending冲突信息。

增加Conflict Context：

```python
@dataclass
class ConflictContext:
    active_memory_id: str | None
    conflicting_candidate_ids: list[str]
    pending_review_ids: list[str]
    conflict_reason: str
```

Answer Decision：

```text
answer_type = conflict
reason_code = unresolved_conflict
```

不得武断选择一侧。

---

## 10.5 Sensitive：权限不足

输出：

```text
answer_type = restricted
reason_code = access_denied
```

但前提是敏感Memory已在检索前过滤，Answer层不应看到敏感值。

---

## 10.6 Ambiguous：指代不明

当：

```text
多个候选分数接近
对象名称相似
Query缺少唯一标识
```

输出：

```text
answer_type = clarification
```

并给出选项：

```text
第一阶段评测
第二阶段评测
```

不能擅自选择。

## 10.7 验收

分别报告：

```text
Absent F1
Qualified-history Accuracy
Conflict Handling Accuracy
Restricted Accuracy
Clarification Accuracy
```

不要只用一个No-answer Boolean。

目标：

```text
各类 ≥ 95%
敏感泄露 = 0
武断冲突回答 = 0
```

---

# 11. P2：Claim与Citation链路

## 11.1 当前问题

总体：

```text
Claim Precision = 36.93%
Claim Recall = 25.13%
Citation Precision = 86.76%
Citation Recall = 28.71%
```

Citation Precision较高，说明给出的引用多数是正确的。

Citation Recall低，主要来自：

```text
Answer调用失败
历史Version无Source
多Memory漏召回
Claim没有逐条绑定Source
```

## 11.2 结构化Claim

Answer生成前先准备：

```python
@dataclass
class SupportedClaim:
    claim_type: str
    subject: str
    predicate: str
    value: str
    memory_ids: list[str]
    version_ids: list[str]
    source_ids: list[str]
```

最终自然语言答案由这些Supported Claim生成。

禁止Answer LLM创造不在Supported Claim中的事实。

## 11.3 Claim-Citation绑定

每条Claim必须有自己的Citation：

```json
{
  "text": "项目A当前为blocked",
  "citations": ["s2"]
}
```

不能只在整段答案末尾附一个Source集合。

## 11.4 Citation过滤

只允许引用：

```text
实际支持该Claim的Source
```

无关但被召回的Memory不能成为Citation。

## 11.5 模板优先

简单回答使用模板：

```text
Task：{topic}当前状态为{status}
Preference：你现在{polarity_text}{object}
Semantic：你当前的{attribute}是{value}
Restricted：该信息无权访问
Conflict：当前记录存在冲突，暂时无法确认
```

复杂多Claim才使用LLM润色。

## 11.6 验收

在Answer成功Case上：

```text
Claim Precision ≥ 95%
Claim Recall ≥ 95%
Citation Precision ≥ 98%
Citation Recall ≥ 98%
Claim-Citation Support ≥ 98%
Unsupported Claim Rate = 0
```

---

# 12. P2：检索结果过滤与Rerank

## 12.1 Final Context Guard

在答案前增加统一守卫：

```python
validate_final_context(
    query_intent,
    access_context,
    selected_results,
)
```

检查：

```text
权限
当前/历史角色
冲突
相关性阈值
重复Identity
must-not-return
```

## 12.2 当前状态优先规则

当前状态Query：

```text
active current Memory优先
superseded/expired不得进入最终Context
Version仅作为解释性证据，不能覆盖Current
```

## 12.3 历史Query规则

历史Query：

```text
Version可以进入Final Context
当前Memory只作为Identity定位或最终状态补充
```

## 12.4 去重

同一Memory的多个召回通道必须融合成一个对象。

历史模式下再按：

```text
memory_id + version_sequence
```

保留不同Version。

## 12.5 低相关结果门禁

若最高相关度不足：

```text
不应为了凑Context返回无关Memory
```

输出No-answer或Clarification。

阈值必须通过开发集分布确定，不能硬编码为数据集答案。

---

# 13. P2：性能与可观测性

## 13.1 当前性能

```text
Retrieval P95 = 339 ms
Total P95 = 26.23 s
```

检索较快，主要问题在Answer调用和限流等待。

## 13.2 分阶段延迟

必须记录：

```text
planner_ms
structured_ms
fulltext_ms
trigram_ms
embedding_ms
vector_ms
fusion_ms
rerank_ms
version_fetch_ms
context_build_ms
answer_queue_wait_ms
answer_llm_ms
citation_ms
total_ms
```

## 13.3 成功和失败分开

Answer延迟必须拆分：

```text
successful answer latency
rate-limit retry latency
system-error latency
deterministic-template latency
```

当前Answer P50=134ms主要受到快速限流失败影响，不能当作成功模型调用延迟。

## 13.4 运行日志

每个Case保留：

```text
planner decision
executed channels
raw scores
filter reasons
selected context
answer decision
claims
citations
rate-limit attempts
```

---

# 14. 预计涉及代码区域

以下路径以本次生产入口和现有项目结构为依据，实施时按仓库真实模块映射。

| 模块 | 修改方向 |
|---|---|
| `memory/service.py` | 查询意图、真实检索结果结构、权限上下文、History入口 |
| `agent/query_agent.py` | 结构化Answer Decision、模板回答、限流错误与No-answer分离 |
| `repositories/postgres/memory.py` | 权限Predicate、Task/Episodic列表、Version链查询 |
| `memory/retrieval/*` | Structured/FTS/Trigram/Vector融合、Final Context Guard |
| `memory/query_router*` | history/list/profile/conflict/clarification意图 |
| `memory/models.py` | AnswerResult、AnswerDecision、ConflictContext、SupportedClaim |
| `infrastructure/llm/*` | 全局Rate Limiter、Retry-After、Jitter、可用性指标 |
| `eval/layer3/*` | 指标拆分、结构化No-answer、真实Stale、成功调用质量 |
| `tests/*query*` | 单元测试、Repository契约、权限、历史、列表、引用 |

---

# 15. 实施顺序

## Phase 0：冻结基线

保存当前：

```text
run manifest
metrics
predictions
failed cases
commit SHA
```

后续每次修复都与此基线比较。

---

## Phase 1：修评测器与输出契约

完成：

```text
Answer Availability
Successful-call Quality
End-to-end Quality
结构化Answer Type
真实Stale指标
Must-not-return指标
Planner Route与执行Channel拆分
```

此阶段不得修改Gold数据来迎合当前实现。

---

## Phase 2：修权限与限流

优先完成：

```text
权限前置过滤
敏感日志脱敏
全局Rate Limiter
Retry-After
并发1质量基线
系统错误与业务No-answer分离
```

验收后再进入功能修复。

---

## Phase 3：实现历史Version查询

顺序：

```text
历史意图识别
→ Memory Identity定位
→ Version链读取
→ 时间操作
→ Source映射
→ 历史答案
```

先只跑`history_and_temporal`。

---

## Phase 4：实现列表与多Memory查询

依次实现：

```text
list_tasks
list_recent_episodes
profile_summary
history_timeline
```

先使用结构化结果，不急于让LLM自由规划。

---

## Phase 5：修Vector与改写

先做诊断：

```text
向量是否存在
模型是否一致
阈值分布
通道是否执行
```

确认链路后，再做Query Rewrite与融合。

---

## Phase 6：修No-answer、Conflict和Clarification

引入统一`AnswerDecision`，禁止通过自由文本猜状态。

---

## Phase 7：Claim与Citation

让最终答案只能基于`SupportedClaim`生成。

---

## Phase 8：全量回归

执行：

```text
单元测试
Repository contract tests
Layer 3单数据集
Layer 3全量并发1
Layer 3全量并发3
Layer 1回归
Layer 2回归
Redis Worker smoke
飞书 /ask smoke
```

---

# 16. 各阶段验收标准

## P0硬门槛

```text
Sensitive Access Violations = 0
Cross-space Violations = 0
Business State Mutations = 0
System Error与No-answer混淆 = 0
```

## Answer可用性

```text
Answer Success Rate ≥ 99%
RateLimit Error Rate ≤ 1%
```

## 当前状态

保持：

```text
Current Recall@1 = 100%
Current MRR = 1.0
```

允许最低门槛：

```text
Current Recall@1 ≥ 98%
```

## 历史

```text
History Hit@3 ≥ 95%
History Hit@5 ≥ 98%
Temporal Order Accuracy ≥ 98%
```

## 语义改写

```text
Overall Recall@3 ≥ 95%
MRR ≥ 0.90
nDCG@10 ≥ 0.90
```

## 多Memory

```text
Recall@5 ≥ 98%
Claim F1 ≥ 95%
Citation F1 ≥ 98%
```

## 无答案与安全

```text
Absent F1 ≥ 95%
Conflict Accuracy ≥ 95%
Qualified-history Accuracy ≥ 95%
Clarification Accuracy ≥ 95%
Restricted Accuracy = 100%
Stale Answer Usage = 0
```

## 性能

```text
Retrieval P95 ≤ 1 s
无LLM简单答案P95 ≤ 1 s
复杂Answer成功调用P95单独报告
```

---

# 17. 新增测试要求

## 17.1 权限测试

对每个通道分别测试：

```text
structured
fulltext
trigram
vector
hybrid
fallback
```

确认敏感Memory无法绕过。

## 17.2 Version测试

至少覆盖：

```text
previous state
initial state
full timeline
transition time
state at date
current vs history
```

## 17.3 列表测试

```text
列出全部todo
列出阻塞任务
列出最近N件经历
列出某时间段经历
跨类型Profile Summary
```

## 17.4 限流测试

模拟：

```text
429 + Retry-After
连续429
重试成功
重试耗尽
```

## 17.5 Answer Decision测试

每种类型都要有：

```text
answered
no_answer
qualified_history_only
conflict
clarification
restricted
system_error
```

## 17.6 Citation测试

```text
每Claim一个Source
多个Source支持一Claim
历史Version Source
无关Source不得引用
```

---

# 18. 必须生成的新报告

修复后输出：

```text
layer3_run_manifest.json
layer3_summary.md
layer3_metrics.json
layer3_answer_availability.json
layer3_successful_answer_metrics.json
layer3_e2e_answer_metrics.json
layer3_retrieval_metrics.json
layer3_history_report.json
layer3_list_query_report.json
layer3_semantic_report.json
layer3_no_answer_breakdown.json
layer3_access_control_report.json
layer3_stale_report.json
layer3_must_not_return_report.json
layer3_citation_report.json
layer3_route_and_channel_report.json
layer3_latency_report.json
layer3_predictions.jsonl
layer3_failed_cases.jsonl
```

---

# 19. Codex交付要求

Codex完成修复后必须给出：

1. 修改文件清单；
2. 每个问题的根因；
3. 修复前后指标；
4. 新增测试清单；
5. 完整运行命令；
6. Git Commit SHA；
7. 未通过指标及原因；
8. 是否修改了领域契约；
9. 是否修改了数据集或Gold；
10. 是否存在只在评测Adapter中生效、生产链路未生效的逻辑。

禁止只回答：

```text
已修复
全部测试通过
指标明显提升
```

---

# 20. 完成定义

第三阶段只有同时满足以下条件才算完成：

- 当前状态检索不回归；
- 历史Version能够被正式查询和引用；
- 列表型与多Memory查询使用正确工具，不再退化为单跳语义搜索；
- 敏感Memory在检索前被过滤；
- Answer限流错误降到可接受范围；
- No-answer、Conflict、Restricted、Clarification有结构化契约；
- Claim全部由可追溯Source支持；
- Stale、无关和权限违规分别统计；
- 查询仍保持业务只读；
- Layer 1和Layer 2回归无明显下降；
- 全量520 Cases重新运行并输出可复现报告。

---

# 21. 最终实施重点

本轮修复的核心不是“把Recall@K调高”，而是补齐不同查询类型真正需要的能力：

```text
当前状态查询 → Current Memory
历史问题 → Version Timeline
列表查询 → Structured List Tool
语义改写 → Vector / Rewrite
无答案 → Relevance Gate
冲突 → Pending Conflict Context
敏感信息 → Retrieval-time ACL
多Claim答案 → Supported Claims + Citations
```

只有这些能力边界明确后，Hybrid检索和Answer生成才不会继续承担它们本不应该承担的职责。
