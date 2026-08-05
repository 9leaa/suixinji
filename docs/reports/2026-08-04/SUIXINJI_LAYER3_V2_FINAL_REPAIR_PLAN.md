# 随心记 Layer 3 v2 最终修复计划书

> 日期：2026-08-04  
> 适用项目：`/home/zcj/suixinji`  
> 当前基线 Run：`stage8_full_20260804`  
> 当前状态：Layer 3 尚未通过最终门槛，暂停 Layer 1 / Layer 2 / Worker 跨层回归  
> 本计划目标：冻结 Layer 3 v2 契约，完成剩余五项修复，小集通过后重跑 520 Cases，最终再进行跨层回归

---

# 1. 当前基线

## 1.1 已通过部分

| 能力 | 当前结果 |
|---|---:|
| Cases | 520 |
| Answer Error | 0 |
| Execution Error | 0 |
| Access Violation | 0 |
| Business State Mutation | 0 |
| Stale Answer Usage | 0 |
| Forbidden Claim | 0 |
| Restricted识别 | 15 / 15 |
| 当前状态检索 | 1.000 |
| 当前状态Claim | 1.000 |
| 当前状态Citation | 1.000 |

已经可以认为稳定的能力：

```text
ACL前置过滤
多API Key与429重试
当前状态Memory检索
查询只读
敏感信息拒绝
历史Version读取
结构化AnswerResult基础
```

本轮修复不得破坏这些已通过能力。

## 1.2 未通过部分

| 指标 | 当前 | 最终门槛 |
|---|---:|---:|
| Claim F1 | 0.547850 | ≥ 0.95 |
| Citation F1 | 0.962229 | ≥ 0.98 |

主要剩余问题：

1. 历史时间线的生产Claim与旧Gold粒度不一致；
2. No-answer缺少主题、属性和槽位相关性判断；
3. Mixed-language Query Rewrite / Vector / Hybrid链路仍有真实召回缺口；
4. Profile Summary遗漏第三条事实对应的Source；
5. 部分路由没有完整暴露真实Selected Evidence和Tool Evidence；
6. v1数据集中的Restricted和Conflict预期已经落后于当前生产契约。

---

# 2. 本轮总体执行顺序

```text
Stage 0  冻结Layer 3 v2契约
→ Stage 1  修历史Claim评分
→ Stage 2  修No-answer主题相关性
→ Stage 3  修Mixed-language Semantic
→ Stage 4  修Profile Source完整性
→ Stage 5  补Evidence暴露
→ Stage 6  分项小集
→ Stage 7  重跑520
→ Stage 8  Layer1 / Layer2 / Worker回归
```

强制规则：

- 每个Stage独立修改；
- 每个Stage独立Commit；
- 每个Stage只跑对应小集；
- 当前Stage未通过，不得进入下一Stage；
- 不修改Layer 3 v1原始数据；
- 不通过Gold、case_id或固定对象名控制生产结果；
- 不为了指标降低安全语义；
- 不允许Evaluator根据答案文本倒推生产实际使用的Evidence。

---

# 3. Stage 0：冻结 Layer 3 v2 契约

## 3.1 目标

在继续修改生产逻辑之前，先明确：

```text
什么情况应该回答
什么情况应该拒答
什么情况属于冲突
什么情况属于权限不足
历史Claim如何表示和评分
原始召回、最终Context和最终Answer分别如何验收
```

避免生产行为已经升级，但评测仍按照旧语义扣分。

## 3.2 Answer Type v2

固定枚举：

```text
answered
no_answer
qualified_history_only
conflict
clarification
restricted
system_error
```

不得继续增加：

```text
history_answered
episodic_answered
list_answered
```

使用辅助字段区分回答模式：

```text
answer_type
reason_code
evidence_mode
```

推荐：

```text
evidence_mode = current | history | mixed | none
```

## 3.3 Answer Type定义

### answered

适用：

```text
直接询问当前，且存在可靠Current Evidence
直接询问历史，且存在可靠Version/History Evidence
正常列表和画像查询
```

示例：

```text
问：这个任务完成前是什么状态？
答：完成前是blocked。
answer_type=answered
evidence_mode=history
reason_code=history_query
```

### no_answer

适用：

```text
没有与Query主题、属性或槽位匹配的可靠证据
```

示例：

```text
问：我最喜欢的电影是什么？
只有：用户喜欢咖啡
```

正确结果：

```text
answer_type=no_answer
selected_evidence=[]
claims=[]
citations=[]
```

### qualified_history_only

仅适用：

```text
用户询问当前状态
但系统只有历史、superseded或expired Evidence
```

示例：

```text
问：我现在住在哪里？
只有：以前住在上海
```

答案：

```text
只能确认你以前住在上海，无法确认当前居住地。
```

### conflict

适用：

```text
同一Identity存在未解决的正反状态
Pending Review尚未完成
Active与Pending Candidate在值或polarity上冲突
```

示例：

```text
喜欢咖啡
不喜欢咖啡
当前Pending Review
```

不能退化为：

```text
no_answer
qualified_history_only
```

必须输出：

```text
answer_type=conflict
```

### clarification

适用：

```text
Query使用单数模糊指代
存在多个同类、同分或近似候选
用户没有要求全部列出
```

示例：

```text
那个评测现在怎么样了？
```

应询问：

```text
你指的是第一阶段评测还是第二阶段评测？
```

### restricted

适用：

```text
相关记录存在，但当前AccessContext无权访问
```

安全契约必须冻结为唯一方案。

推荐：

```text
Repository返回不含敏感内容的access_denied marker
```

Marker只能包含：

```text
kind=access_denied
reason=insufficient_permission
resource_type=memory
```

禁止包含：

```text
敏感content
current_value
source内容
真实敏感Memory ID
```

如果产品最终决定“记录存在本身也不可暴露”，则Repository完全隐藏记录，并使用统一模糊拒绝。Repository、AnswerDecision、Evaluator必须使用同一方案。

### system_error

适用：

```text
工具失败
数据库失败
模型调用最终失败
不可恢复的执行异常
```

不得计入业务No-answer。

## 3.4 Claim v2契约

### 原子Claim

内部继续保持：

```text
一个可独立验证的事实 = 一条Claim
```

示例：

```text
c1：任务最初为todo
c2：任务随后变为blocked
c3：任务最终变为done
```

每条Claim必须绑定：

```text
memory_ids
version_ids
source_ids
support_role
```

### Claim Group / Summary Claim

为解决历史时间线Gold是一个复合Claim的问题，新增：

```text
ClaimGroup
TimelineSummaryClaim
```

建议结构：

```json
{
  "group_type": "timeline",
  "summary_text": "任务依次经历了todo、blocked、done。",
  "ordered_member_claim_ids": ["c1", "c2", "c3"],
  "memory_ids": ["memory-id"],
  "version_ids": ["version-1", "version-2", "version-3"],
  "source_ids": ["source-1", "source-2", "source-3"],
  "support_role": "history"
}
```

原则：

- 生产内部保留原子Claim；
- 时间线回答额外产生Summary Claim；
- Evaluator允许Gold复合Claim匹配Summary Claim；
- 不得删除原子Claim来迁就旧Gold；
- 标题、版本编号和格式文本不算事实Claim。

## 3.5 Retrieval指标v2分层

### Raw Candidate层

仅用于诊断：

```text
Raw Candidate Recall
Raw Candidate Precision
Raw Irrelevant Rate
Channel Noise
```

原始通道出现干扰项，不直接判最终答案失败。

### Selected Context层

核心检索验收：

```text
Selected Context Recall
Selected Context Precision
Selected Must-not Violation
Selected Stale Violation
Selected Access Violation
Selected Tool Coverage
```

### Final Answer层

最终业务验收：

```text
Claim F1
Citation F1
Forbidden Claim
Stale Answer Usage
No-answer F1
Conflict Accuracy
Clarification Accuracy
Restricted Accuracy
```

## 3.6 数据集版本

保留：

```text
suixinji.layer3.retrieval_answer.v1
```

新增：

```text
suixinji.layer3.retrieval_answer.v2
```

禁止直接覆盖v1。

v2至少修改：

### Sensitive 15 Cases

```text
旧：no_answer
新：restricted
```

### Pending Review Conflict 20 Cases

```text
旧：qualified_history_only或旧冲突语义
新：conflict
```

### History Timeline Cases

新增：

```text
expected_claim_groups
ordered timeline members
summary claim
```

保留旧字段用于兼容，但v2评分优先使用新契约。

## 3.7 契约交付文件

Codex需先生成：

```text
docs/adr/ADR_LAYER3_ANSWER_CONTRACT_V2.md
eval/layer3/contracts/v2.py
eval/layer3/contract_migrations/v1_to_v2.py
eval/layer3/data_v2/contract_change_manifest.json
```

`contract_change_manifest.json`必须记录：

```text
case_id
v1字段
v2字段
修改原因
是否属于安全契约升级
是否属于Claim粒度升级
```

## 3.8 Stage 0验收

- v1文件未被修改；
- v2 Schema可独立校验；
- Restricted、Conflict、Qualified History语义不重叠；
- 原子Claim与Summary Claim可同时存在；
- Raw、Selected、Answer三层指标明确定义；
- Contract Change Manifest完整；
- 不运行520全量。

---

# 4. Stage 1：修历史 Claim 评分

## 4.1 当前问题

生产已经正确获取：

```text
v1 todo
v2 blocked
v3 done
```

且：

```text
Citation F1 = 1.0
```

但生产产生多个原子Claim，旧Gold只有一个复合时间线Claim，导致：

```text
FP = 267
FN = 100
History Claim F1 = 0
```

这主要是契约和评分粒度问题，不是历史事实选择错误。

## 4.2 修改方向

### 生产侧

修改：

```text
agent/answer_models.py
agent/query_agent.py
```

历史回答生成：

```text
原子Claim c1/c2/c3
+
Timeline Summary Claim
```

必须保证：

```text
顺序正确
Version绑定正确
Source绑定正确
support_role=history
```

### Evaluator侧

修改：

```text
eval/layer3/run_layer3_eval.py
或新增 eval/layer3/metrics_claims.py
```

匹配顺序：

1. 优先匹配结构化Claim Group；
2. 检查member顺序；
3. 检查状态值；
4. 检查Version集合；
5. 检查Source集合；
6. Summary Text只做辅助语义匹配；
7. 不把标题、版本编号、说明文字算成FP。

## 4.3 不允许的做法

- 不把所有时间线重新压成一个不可拆的大Claim；
- 不删除原子Claim；
- 不通过字符串完全相等判断时间线；
- 不让Evaluator从答案文本猜Version；
- 不为了指标忽略错误顺序。

## 4.4 小集

优先：

```text
history_and_temporal
history_synthesis
task_transition
qualified_history_only
```

代表Case：

```text
l3_history_001
l3_multianswer_003
l3_noanswer_041
```

## 4.5 验收

| 指标 | 门槛 |
|---|---:|
| History Claim Group F1 | ≥ 0.95 |
| Timeline Order Accuracy | ≥ 0.98 |
| History Citation F1 | ≥ 0.98 |
| Version Source Exact | ≥ 0.98 |
| Stale Answer Usage | 0 |

---

# 5. Stage 2：修 No-answer 主题相关性

## 5.1 当前问题

35个缺失电影偏好Case中，有16个使用：

```text
用户喜欢咖啡
```

回答电影问题。

当前No-answer Recall：

```text
0.316667
```

根因：

```text
Memory Type匹配
但Query主题/属性/槽位不匹配
```

## 5.2 修改方向

新增或完善：

```text
QuerySlot
CandidateSlot
EvidenceCompatibility
```

Query解析至少输出：

```text
intent
entity
attribute
topic
memory_type
requested_time_mode
polarity_question
```

候选Evidence至少输出：

```text
entity
attribute
canonical_topic
memory_type
predicate
current_value
```

## 5.3 Evidence Compatibility Gate

建议规则：

```text
Type匹配只是必要条件，不是充分条件。
```

候选必须满足至少一种：

1. `attribute`精确或规范化匹配；
2. `canonical_topic`高置信度匹配；
3. `predicate + object`匹配；
4. Query Rewrite后主题匹配；
5. Cross-encoder/Rerank明确支持。

示例：

```text
Query：最喜欢的电影
Candidate：喜欢咖啡
```

结果：

```text
memory_type=preference：匹配
attribute/topic：不匹配
最终：不可答
```

## 5.4 No-answer行为

没有兼容Evidence时：

```text
answer_type=no_answer
selected_evidence=[]
claims=[]
citations=[]
```

禁止：

```text
我不知道电影，但我知道你喜欢咖啡。
```

## 5.5 阈值原则

不得使用一个全局相似度阈值解决所有问题。

推荐组合：

```text
结构化槽位匹配
+
语义分数
+
Top1/Top2分差
+
Memory Type
```

阈值必须通过开发小集确定，不能读取Gold动态控制。

## 5.6 小集

```text
35个absent cases
电影 vs 咖啡
不存在的身份信息
不存在的学校/宿舍信息
存在同类型但不同属性的Preference/Semantic
```

增加反例：

```text
问咖啡偏好，存在咖啡Preference → answered
问电影偏好，只存在咖啡Preference → no_answer
```

## 5.7 验收

| 指标 | 门槛 |
|---|---:|
| Absent No-answer Recall | ≥ 0.95 |
| Absent No-answer Precision | ≥ 0.95 |
| Absent No-answer F1 | ≥ 0.95 |
| Irrelevant Selected Context | 0 |
| Irrelevant Citation | 0 |
| Related Preference正常回答回归 | 100% |

---

# 6. Stage 3：修 Mixed-language Semantic

## 6.1 当前问题

20个应回答的：

```text
mixed-language + hybrid
```

Case返回`no_answer`，缺少`s1` Citation。

可能失败点：

```text
Rewrite未执行
Rewrite结果未进入Hybrid
Query Embedding失败
Embedding Contract不一致
正确Vector结果低于阈值
Fusion后被压低
AnswerDecision没有使用已命中Evidence
```

## 6.2 诊断字段

每个Mixed-language Case必须保存：

```text
original_query
rewritten_queries
rewrite_reason
query_embedding_success
embedding_model
embedding_dimension
embedding_version
vector_raw_hits
vector_scores
fts_hits
trigram_hits
structured_hits
rrf_hits
selected_evidence
answer_decision
```

## 6.3 Query Rewrite

针对：

```text
focus
working on
mainly working
current project
最近在忙
主要在做
```

做通用语言规范化，不写死测试对象。

示例：

```text
What am I mainly working on now?
→ 我现在主要在做什么？
```

```text
我现在的focus是什么？
→ 我现在的工作重点是什么？
```

保留：

```text
original query
rewritten query
```

两路共同召回，不能只使用Rewrite覆盖原Query。

## 6.4 Embedding Contract

必须确认：

```text
Seed Memory Vector
Query Vector
```

使用相同：

```text
provider
model
dimension
normalize
distance metric
embedding version
```

不匹配时应明确报诊断错误，不得静默退化后仍标记Vector已执行。

## 6.5 Fusion

确认：

```text
original query vector
rewrite query vector
FTS
trigram
structured
```

都能进入统一RRF或Fusion。

保存每个Channel贡献。

## 6.6 阈值

输出正负样本分数分布：

```text
Gold target score
Top distractor score
Top1/Top2 margin
```

只有在分布证明合理时才调整：

```text
min_score
rerank threshold
confidence gate
```

禁止为了20条Case直接降低全局阈值。

## 6.7 小集

```text
mixed_language 20 Cases
paraphrase
typo
noise
indirect_reference
纯英文Query
中英混合Query
```

## 6.8 出口门槛

| 指标 | 门槛 |
|---|---:|
| Vector Seed完整率 | 100% |
| Embedding Contract匹配率 | 100% |
| Query Embedding成功率 | 100% |
| Mixed-language Recall@3 | ≥ 0.90 |
| Mixed-language Citation F1 | ≥ 0.98 |
| Paraphrase Recall@3 | ≥ 0.95 |
| Typo/Noise Recall@3 | ≥ 0.95 |
| Top1命中后错误No-answer | 0 |

未达到时，不进入Stage 4。

---

# 7. Stage 4：修 Profile Source 完整性

## 7.1 当前问题

Profile Summary需要三条事实：

```text
饮食偏好
主要设备
常用语言
```

25个Case中通常只绑定：

```text
s1
s2
```

遗漏：

```text
s3
```

导致分桶Citation F1为：

```text
0.952381
```

## 7.2 修改方向

Profile Summary改为Slot驱动：

```text
food_preference
primary_device
preferred_language
```

每个Slot必须生成独立：

```text
Selected Evidence
Supported Claim
Source Set
```

示例：

```text
Claim 1：你喜欢咖啡
→ m1
→ s1

Claim 2：你主要使用MacBook
→ m2
→ s2

Claim 3：你常用Python
→ m3
→ s3
```

## 7.3 完整性检查

在Answer生成前执行：

```text
每条将被回答的Claim是否至少有一个Source
```

缺Source时：

- 不得凭空回答该Claim；
- 可降级为只回答有Source支持的事实；
- 输出诊断`missing_claim_source`；
- 不得事后把所有Source无差别拼进Citation。

## 7.4 小集

```text
profile_summary 25 Cases
cross_type profile
三Slot全部存在
缺一个Slot
一个Slot有多个Source
```

## 7.5 验收

| 指标 | 门槛 |
|---|---:|
| Profile Claim Recall | ≥ 0.95 |
| Profile Citation Precision | ≥ 0.98 |
| Profile Citation Recall | ≥ 0.98 |
| Profile Citation F1 | ≥ 0.98 |
| Claim无Source比例 | 0 |

---

# 8. Stage 5：补 Evidence 暴露

## 8.1 当前问题

```text
selected_context_unavailable_rate = 0.075
selected_tool_refs_unavailable_rate = 0.276923
```

部分路由无法确认：

```text
实际执行了什么工具
工具返回了什么对象
最终选择了什么Evidence
哪些Source真正支持答案
```

## 8.2 AnswerResult v2

必须直接暴露：

```text
executed_tools
executed_channels
retrieved_evidence
selected_evidence
selected_memory_ids
selected_version_ids
selected_source_ids
claims
claim_groups
answer_decision
```

## 8.3 RetrievalEvidence字段

```text
kind
id
memory_id
version_id
source_ids
memory_type
status
task_status
score
rank
channel
tool
role
selected
filter_reason
```

生产逻辑只使用真实ID。

评测逻辑Ref：

```text
m1
v1
s1
pr1
```

只能由Evaluator映射，生产不得读取。

## 8.4 工具覆盖

必须覆盖：

```text
memory_search
memory_history
list_tasks
list_recent_episodes
profile_summary
conflict_lookup
access_denied
```

不能只有普通`memory_search`进入Evaluator。

## 8.5 禁止事项

Evaluator不得：

```text
从答案文本猜selected Evidence
额外重新查询数据库代替Answer Path
根据Gold补selected refs
把raw channel hits当最终selected context
```

生产没有暴露时只能记录：

```text
null
unavailable
```

## 8.6 小集

```text
history
history_synthesis
task_list
episodic_list
profile_summary
restricted
conflict
```

## 8.7 验收

| 指标 | 门槛 |
|---|---:|
| selected_context_unavailable_rate | 0 |
| selected_tool_refs_unavailable_rate | 0 |
| History Version Ref Coverage | 100% |
| Profile Source Ref Coverage | 100% |
| Task/Episodic Tool Evidence Coverage | 100% |
| Sensitive Content in Prediction | 0 |

---

# 9. Stage 6：分项小集回归

所有专项修复完成后，先跑小集，不直接跑520。

## 9.1 契约小集

```text
restricted 15
conflict 20
qualified_history_only 20
clarification 10
```

检查：

```text
Answer Type
Reason Code
Evidence Mode
Claims
Citations
Forbidden Claim
```

## 9.2 History小集

```text
history_and_temporal
history_synthesis
task_transition
```

检查：

```text
原子Claim
Summary Claim
顺序
Version
Source
```

## 9.3 No-answer小集

```text
absent 35
```

检查：

```text
主题不匹配
Selected Context为空
Claims为空
Citations为空
```

## 9.4 Semantic小集

```text
mixed_language 20
paraphrase
typo
noise
indirect_reference
```

## 9.5 Profile小集

```text
profile_summary 25
```

## 9.6 Evidence小集

检查所有工具：

```text
executed_tools
retrieved_evidence
selected_evidence
selected refs
```

## 9.7 小集总门槛

| 能力 | 门槛 |
|---|---:|
| Restricted Accuracy | 100% |
| Conflict Accuracy | ≥ 95% |
| Qualified History Accuracy | ≥ 95% |
| Clarification Accuracy | ≥ 95% |
| History Claim F1 | ≥ 95% |
| Absent No-answer F1 | ≥ 95% |
| Mixed-language Recall@3 | ≥ 90% |
| Profile Citation F1 | ≥ 98% |
| Evidence unavailable rate | 0 |
| Access / Stale Answer / Forbidden Claim | 0 |

任意一项未通过：

```text
停止
修复该专项
重新运行对应小集
```

---

# 10. Stage 7：重跑 520 Cases

## 10.1 运行要求

使用：

```text
Layer 3 v2数据集
真实PostgreSQL
真实生产Query Path
真实ACL
统一EvidenceBundle
结构化AnswerResult
```

保存到项目持久目录，不再只放`/tmp`：

```text
eval/results/layer3_v2_stage7_<timestamp>/
```

必须包含：

```text
layer3_run_manifest.json
layer3_summary.md
layer3_metrics.json
layer3_predictions.jsonl
layer3_failed_cases.jsonl
layer3_contract_report.json
layer3_claim_report.json
layer3_no_answer_report.json
layer3_semantic_report.json
layer3_profile_report.json
layer3_evidence_coverage_report.json
layer3_access_control_report.json
layer3_latency_report.json
```

## 10.2 520全量门槛

### 安全与稳定性

| 指标 | 门槛 |
|---|---:|
| Answer Error | 0 |
| Execution Error | 0 |
| Access Violation | 0 |
| Cross-space Violation | 0 |
| Business State Mutation | 0 |
| Stale Answer Usage | 0 |
| Forbidden Claim | 0 |

### 检索与回答

| 指标 | 门槛 |
|---|---:|
| Current State Recall@1 | ≥ 0.98 |
| History Hit@3 | ≥ 0.95 |
| Claim F1 | ≥ 0.95 |
| Citation F1 | ≥ 0.98 |
| No-answer F1 | ≥ 0.95 |
| Conflict Accuracy | ≥ 0.95 |
| Clarification Accuracy | ≥ 0.95 |
| Restricted Accuracy | 1.00 |
| Mixed-language Recall@3 | ≥ 0.90 |
| Evidence unavailable rate | 0 |

### 诊断指标

以下不作为直接失败门槛，但必须报告：

```text
Raw Irrelevant Rate
Raw Must-not Hit Rate
各Channel Noise
Selected Context Precision
Selected Context Violation
```

如果Raw候选噪声高但最终Selected/Answer正确：

```text
记录为后续检索优化项
不判安全或答案失败
```

---

# 11. Stage 8：跨层回归

只有520全量通过后执行。

## 11.1 Layer 1回归

检查：

```text
Should-store
Candidate抽取
Memory Type
Key字段
Task Status
Evidence Span
多Candidate
LLM/Rules降级
```

要求：

```text
不因Query层结构改动造成抽取回归
```

## 11.2 Layer 2回归

检查：

```text
Task Identity
Relation
Action
Current State
Task Transition
Version Sequence
Source Link
Pending Review
Orphan Done
Idempotence
Concurrency
```

特别检查：

```text
v2 Conflict契约与真实Pending Review持久化一致
```

## 11.3 Redis Worker Smoke

检查：

```text
receiver
outbox relay
ingest worker
enrichment worker
memory worker
delivery worker
retry
dead letter
```

确保新增结构化Answer/Evidence没有影响Worker链路。

## 11.4 真实 `/ask` Smoke

至少测试：

```text
当前状态
历史状态
任务时间线
Task列表
最近经历
Profile Summary
无答案
Stale-only
Conflict
Clarification
Restricted
Mixed-language
```

飞书仍调用：

```text
answer_question -> str
```

结构化评估/API使用：

```text
answer_question_result
```

旧消息格式不变。

---

# 12. 文件级修改范围

| 文件/目录 | 主要修改 |
|---|---|
| `docs/adr/ADR_LAYER3_ANSWER_CONTRACT_V2.md` | 冻结Answer Type、Restricted、Conflict、Claim、指标契约 |
| `eval/layer3/contracts/v2.py` | v2 Schema与评分契约 |
| `eval/layer3/contract_migrations/v1_to_v2.py` | v1→v2数据迁移 |
| `eval/layer3/data_v2/` | v2数据集与变更清单 |
| `agent/answer_models.py` | ClaimGroup、Summary Claim、Evidence v2 |
| `agent/query_agent.py` | No-answer Gate、Mixed-language Rewrite、Profile Source、AnswerDecision |
| `memory/service.py` | 统一Evidence输出与Source hydration |
| `repositories/postgres/memory.py` | 必要的Evidence/Source/Version查询支持 |
| `eval/layer3/run_layer3_eval.py` | v2评分、三层指标、Evidence覆盖 |
| `eval/layer3/metrics_claims.py` | Claim与Timeline Group评分 |
| `tests/` | Contract、Claim、No-answer、Semantic、Profile、Evidence测试 |

---

# 13. Feature Flags与回滚

建议保留：

```text
SUIXINJI_LAYER3_CONTRACT_V2_ENABLED
SUIXINJI_QUERY_TIMELINE_CLAIM_GROUP_ENABLED
SUIXINJI_QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED
SUIXINJI_QUERY_MIXED_LANGUAGE_REWRITE_ENABLED
SUIXINJI_QUERY_PROFILE_SOURCE_STRICT_ENABLED
SUIXINJI_QUERY_EVIDENCE_V2_ENABLED
```

回滚原则：

- 每个Stage独立Flag；
- 关闭生产Flag不删除新数据结构；
- v1/v2数据集同时保留；
- 旧`answer_question -> str`继续兼容；
- 新字段只增不删；
- 不回滚用户Memory数据；
- 跨层回归失败时优先关闭Query层Flag。

---

# 14. Codex每个Stage交付要求

每个Stage完成后必须提交：

```text
1. 修改文件清单
2. 根因说明
3. 实际修改内容
4. 小集运行命令
5. 修复前后指标
6. 未通过Case
7. Commit SHA
8. Feature Flag
9. 回滚方法
10. 是否修改Gold或数据集契约
```

禁止只回答：

```text
已修复
全部通过
指标提升明显
```

---

# 15. Codex执行顺序

## Commit 1：Layer 3 v2契约

```text
ADR
Schema
v1→v2迁移
Contract Change Manifest
契约单测
```

## Commit 2：History Claim

```text
原子Claim
Timeline Summary Claim
Claim Group评分
History小集
```

## Commit 3：No-answer Gate

```text
Query Slot
Candidate Slot
Compatibility Gate
Absent小集
```

## Commit 4：Mixed-language Semantic

```text
Rewrite
Embedding诊断
Fusion
Mixed-language小集
```

## Commit 5：Profile Source

```text
Slot Evidence
Per-claim Source
Profile小集
```

## Commit 6：Evidence Exposure

```text
AnswerResult v2
Tool Evidence
Selected Evidence
Coverage小集
```

## Commit 7：Layer 3 v2全量

```text
520 Cases
完整报告
失败分析
```

## Commit 8：跨层回归

```text
Layer1
Layer2
Redis Worker
真实/ask
```

---

# 16. 完成定义

只有同时满足以下条件，Layer 3才算完成：

- Layer 3 v2契约已经冻结；
- v1数据集保持不变；
- Restricted、Conflict、Qualified History语义一致；
- 历史原子Claim和Summary Claim均可追踪；
- No-answer不再使用同类型但不同主题的Memory；
- Mixed-language召回达到门槛；
- Profile每条Claim均绑定完整Source；
- 所有真实Tool和Selected Evidence均可被Evaluator看到；
- 520全量Claim F1 ≥ 0.95；
- 520全量Citation F1 ≥ 0.98；
- 所有安全硬门槛为0；
- Layer 1和Layer 2回归通过；
- Redis Worker与真实`/ask` Smoke通过；
- 运行结果、Commit和配置可复现。

---

# 17. 最终核心原则

本轮不是为了把一个总分“调到95%”，而是完成以下能力闭环：

```text
稳定契约
→ 正确检索
→ 正确选证据
→ 正确决定能否回答
→ 每条事实有来源
→ 评测器看到真实生产行为
→ 跨层无回归
```

最终系统应做到：

```text
没有答案时不拿无关记忆凑答案；
只有历史时不冒充当前；
有冲突时不武断选一边；
无权限时不泄露内容；
中英混合问题也能召回正确记忆；
多事实答案中的每条事实都能追溯到自己的Source。
```
