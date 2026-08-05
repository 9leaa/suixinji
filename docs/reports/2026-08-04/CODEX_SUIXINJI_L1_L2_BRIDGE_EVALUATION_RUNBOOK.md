# 随心记 L1→L2 真实衔接评测说明书（Codex版）

> 数据集：`suixinji_l1_l2_bridge_v1.zip`  
> 主文件：`l1_to_l2_bridge.jsonl`  
> Schema：`suixinji.bridge.l1_l2.v1`  
> 当前规模：以 `manifest.json` 和 JSONL 实际行数为准；当前版本为 96 Cases、12 个场景族。  
> 项目路径：`/home/zcj/suixinji`

---

# 1. 评测目标

本数据集不是重新做一次独立 Layer 1 或独立 Layer 2，而是测试真实衔接：

```text
原始 Note
→ Layer 1 实际抽取 MemoryCandidate
→ Layer 2 实际消费这些 Candidate
→ Relation / Action
→ Memory / Version / Source / PendingReview
```

它重点回答：

1. Layer 1 抽出的 Candidate 能否被 Layer 2 正确消费；
2. Layer 1 的漏抽、错误类型、错误 Key、错误 Evidence 会怎样传播；
3. Layer 2 能否修复部分 Layer 1 字段误差；
4. 最终数据库快照是否仍然正确、唯一、可追踪；
5. 错误究竟来自 L1，还是 L2。

---

# 2. 评测边界

## 2.1 主运行必须调用真实生产链路

必须实际调用：

```text
Note处理入口
→ process_note_memory / extractor
→ validate_candidates
→ consolidate_candidate
→ adjudicator / relation guard
→ PostgreSQL repository evolve
```

禁止：

```text
直接把 expected_l1.candidates 当作主运行输入
直接把 expected_l2.final_snapshot 写入数据库
根据 Gold relation/action 修改生产结果
通过 case_id、m1、v1、s1 做业务判断
```

## 2.2 使用 PostgreSQL 隔离空间

每个 Case 必须使用独立：

```text
tenant_id
space_id
user_id
```

不得写入用户原有 Space。

Case 完成后：

1. 保存预测和快照；
2. 清理该 Case 的测试 Space；
3. 验证没有跨 Case、跨 Space 污染。

## 2.3 主模式与诊断模式

至少运行两种模式。

### Mode A：Actual Bridge，正式主指标

```text
原始 Note
→ 实际 L1
→ 实际 L2
```

所有端到端指标以 Mode A 为主。

### Mode B：Oracle-L1 Bridge，错误归因

```text
expected_l1.candidates
→ 实际 L2
```

Mode B 只用于判断：

> 如果 L1 给出标准 Candidate，L2 是否能正确完成归并。

不得把 Mode B 的高分冒充完整衔接结果。

可选增加：

### Mode C：Rules Baseline

使用纯 Rules L1，和生产 Hybrid 结果对比。不是硬性要求。

---

# 3. 数据结构

每个 Case 主要包含：

```text
case_id
scenario_family
difficulty
input.space_id
input.user_id
input.turns[]
expected_l2
coverage_tags
```

每轮 Turn 包含：

```text
turn_id
idempotency_key
occurred_at
note_text
delivery_replay
expected_l1.should_store
expected_l1.candidates
```

`expected_l2` 包含：

```text
decisions
final_snapshot.memories
final_snapshot.versions
final_snapshot.sources
final_snapshot.pending_reviews
invariants
```

数据集逻辑 Ref：

```text
Candidate：c1、c2
Memory：m1、m2
Version：v1、v2
Source：s1、s2
PendingReview：pr1
```

这些 Ref 只用于评测对齐，生产逻辑不得读取。

---

# 4. 执行流程

## 4.1 启动前检查

Codex先检查：

```text
数据集ZIP可解压
manifest.json存在
JSONL可逐行解析
case_id唯一
Task状态只包含 todo/blocked/done/cancelled
Relation只包含 new/same/merge/update/supersede/conflict
Action只包含 insert/add_source/update/pending_review
```

必须输出数据集审计：

```text
case_count
scenario_family_count
每个场景数量
字段缺失数
非法枚举数
重复case_id数
引用断裂数
```

数据集有结构错误时先停止，不得静默修正 Gold。

---

## 4.2 每个 Case 的运行顺序

严格按 `input.turns` 顺序执行。

对每个 Turn：

1. 建立真实 Note；
2. 使用真实 Layer 1 入口抽取；
3. 保存以下阶段输出：

```text
raw_llm
schema_validated
normalized
final_candidate
```

4. 记录：
   - LLM是否调用；
   - 模型、Prompt、Schema版本；
   - 是否超时；
   - 是否重试；
   - 是否规则降级；
5. 将 `final_candidate` 交给真实 Layer 2；
6. 保存每条实际 Decision；
7. 保存本轮执行前后数据库 Delta：
   - Memory；
   - Version；
   - Source；
   - Pending Review；
8. 若 `delivery_replay=true`：
   - 使用相同 `idempotency_key` 再投递一次；
   - 验证无额外业务写入。

全部 Turn 完成后，读取最终数据库快照。

---

## 4.3 时间比较

不得直接比较：

```text
2026-08-04T10:00:00+08:00
2026-08-04T02:00:00+00:00
```

这两个字符串表示同一时刻。

比较前必须：

1. 解析为带时区时间；
2. 统一为 UTC；
3. 再比较时间点。

`created_at` 与 `valid_from` 不得混为同一个字段。

---

# 5. Candidate匹配规则

Actual L1 预测没有 Gold 的 `c1/c2`，需要一对一匹配。

推荐顺序：

1. `evidence_text/evidence_span`规范化后完全一致；
2. `memory_type + canonical_topic`一致；
3. `memory_type + memory_key`一致；
4. `entity + attribute + operation`一致；
5. 相同 `memory_type` 下按原文位置稳定配对。

匹配必须一对一：

```text
一个预测Candidate不能匹配多个Gold
一个Gold Candidate不能匹配多个预测
```

未匹配 Gold：

```text
FN
```

未匹配预测：

```text
FP
```

必须同时保留两套 Candidate 指标：

### 官方宽松 Candidate匹配

与独立 Layer 1 保持可比，主要按 `memory_type` 配对。

### 严格 Candidate匹配

要求：

```text
类型
主题/身份
证据
核心字段
```

至少达到可被 Layer 2 正确消费的程度。

不能只报告宽松指标。

---

# 6. L1检查点指标

这些指标用于判断进入 L2 之前发生了什么。

## 6.1 Should-store Precision / Recall / F1

```text
TP：Gold应存，系统也进入记忆流程
FP：Gold不应存，系统却进入
FN：Gold应存，系统没有进入
```

```text
P = TP / (TP + FP)
R = TP / (TP + FN)
F1 = 2PR / (P + R)
```

## 6.2 Candidate Precision / Recall / F1

分别输出：

```text
candidate_official
candidate_strict
```

严格指标中，Evidence、Topic、Key不能全部错误后仍算正确。

## 6.3 Memory Type Macro-F1

类别：

```text
preference
task
semantic
episodic
```

分别算 F1 后平均。

## 6.4 Evidence Span F1

建议同时输出：

### Exact Span F1

配对后Evidence完全一致才算TP。

### Token/Character Overlap F1

```text
span_precision = overlap / predicted_span_length
span_recall = overlap / gold_span_length
span_f1 = 2PR/(P+R)
```

不得用整条原文替代精确证据而获得满分。

## 6.5 Task Status Accuracy

只在 Gold Task Candidate 上统计：

```text
todo
blocked
done
cancelled
```

不使用 `in_progress`。

## 6.6 Memory Key Accuracy

预测Key规范化后与Gold一致的比例。

另输出：

```text
same_identity_key_consistency
```

即同一 Case 内同一记忆身份的多轮Candidate是否产生稳定Key。

## 6.7 Polarity Accuracy

适用于 Preference及存在正负语义的Candidate。

## 6.8 Multi-candidate Recall

只统计含两个及以上Gold Candidate的Turn：

```text
命中的Gold Candidate数 / Gold Candidate总数
```

## 6.9 Candidate Count Exact

预测Candidate数量与Gold完全相同的Turn比例。

---

# 7. L2检查点指标

## 7.1 Task Identity Precision / Recall / F1

判断实际Candidate是否关联到正确Memory身份。

不能只比较预测Key；最终绑定的Memory Identity才是主值。

## 7.2 Relation Macro-F1

类别：

```text
new
same
merge
update
supersede
conflict
```

分别计算F1后宏平均。

必须输出两版：

### Relation F1 — Overall

所有Gold Decision都进入分母。L1漏掉Candidate时，相应Gold Decision记FN。

### Relation F1 — Conditional

只在L1已正确抽到并匹配Candidate的样本上计算。

两者区别：

```text
Overall：真实衔接效果
Conditional：L2收到可用Candidate后自身判断能力
```

## 7.3 Action Accuracy

类别：

```text
insert
add_source
update
pending_review
```

同样输出：

```text
overall
conditional_on_matched_candidate
```

## 7.4 Current State Accuracy

所有Turn结束后比较最终Active Memory：

```text
memory_type
task_status
current_value
polarity
status
canonical_topic
```

建议输出：

```text
field_accuracy
strict_current_state_exact
```

## 7.5 Task Transition Accuracy

对状态转移序列比较：

```text
todo → blocked
blocked → done
done → todo
cancelled → todo
```

同时检查中间Version，不只看最终状态。

## 7.6 Version Sequence Accuracy

检查：

```text
版本数量
sequence连续
顺序正确
每个版本内容/状态正确
Source绑定正确
```

## 7.7 Version Creation Accuracy

应该创建Version时是否创建，不该创建时是否未创建。

典型：

```text
same + add_source → 不创建Version
update/supersede → 创建Version
```

## 7.8 Source Link Precision / Recall / F1

以Source逻辑集合计算。

还要输出：

```text
source_exact_set_accuracy
```

## 7.9 Pending-review Precision / Recall / F1

以“应进入Pending Review”为正类，输出：

```text
TP / FP / FN / TN
```

不能用大量负样本的普通Accuracy冒充Pending能力。

## 7.10 Duplicate Active Rate

```text
出现同一Identity多个Active的Case数 / 全部Case数
```

目标为0。

## 7.11 Stale Active Rate

过期、被替代或旧状态仍为Active的Case比例。

目标为0。

## 7.12 Orphan Done Task Rate

没有合法历史Task却留下Active Task(done)的比例。

目标为0。

## 7.13 Idempotence Accuracy

重复投递后满足：

```text
Memory不增加
Version不增加
Source不重复
PendingReview不重复
Decision结果稳定
```

的Replay Case比例。

---

# 8. L1→L2衔接核心指标

以下是本数据集最重要的指标，不能只报告独立层指标。

## 8.1 Candidate Propagation Recall

定义：

> L1成功抽出的正确Candidate中，有多少被L2产生了正确Decision。

```text
分子：L1匹配正确，且L2 relation/action/target正确的Candidate
分母：L1匹配正确的Candidate
```

它不惩罚L1漏抽，专门看L2消费能力。

---

## 8.2 Gold Decision Coverage

定义：

> 原始消息中所有应该发生的L2 Decision，实际链路完成了多少。

```text
正确完成的Gold Decision数 / 全部Gold Decision数
```

L1漏抽会直接使该指标下降。

---

## 8.3 Memory Formation Precision / Recall / F1

比较最终实际Memory与Gold最终Memory。

匹配身份建议使用：

```text
memory_type
canonical_topic
entity
attribute
memory_key
```

Recall回答：

> 原始消息应该形成的Memory，最终形成了多少。

Precision回答：

> 实际形成的Memory中，有多少不是重复或错误记忆。

---

## 8.4 Identity Survival Accuracy

定义：

> 从L1 Candidate到L2最终Memory，逻辑身份是否保持正确。

即使L1 Key文本不完全一致，只要L2通过Identity Guard正确归并，也可算成功。

该指标可反映Layer 2对L1错误Key的恢复能力。

---

## 8.5 Status Propagation Accuracy

定义：

> 原始消息表达的Task状态，经过L1字段和L2演化后，最终状态是否正确。

按Decision和Case分别报告：

```text
decision_level
final_case_level
```

---

## 8.6 Evidence-to-Source Continuity P/R/F1

追踪：

```text
Note原文Evidence
→ Candidate Evidence
→ Memory Source
→ Version Source
```

正确要求：

1. Gold Evidence对应的Note成为正确Source；
2. Source绑定到正确Memory/Version；
3. 没有把无关Turn绑定进去。

---

## 8.7 Multi-candidate Completion Rate

只统计一个Turn含多个Gold Candidate的场景。

Case通过要求：

```text
所有Candidate均被抽取
所有Candidate均被L2处理
所有预期Memory均落库
无多余Memory
```

---

## 8.8 L2 Recovery Rate

定义：

> L1部分结构字段错误，但L2仍恢复出正确最终Memory的比例。

适用的可恢复错误：

```text
memory_key轻微不同
canonical_topic轻微变体
content措辞不同
Evidence范围偏长但仍包含事实
```

不可恢复错误：

```text
Candidate完全漏抽
正负极性反转
把不同实体合并
```

报告必须列出恢复依据，不能通过Gold特判。

---

## 8.9 Bridge Case Exact

一个Case只有以下全部正确才算Exact：

```text
L1 Candidate集合
L2 Decision集合
最终Memory
Version
Source
PendingReview
所有Invariant
```

它是严格指标，不能替代分项指标。

---

## 8.10 Oracle Gap

分别比较Mode A和Mode B。

```text
L1 Impact = Oracle-L1结果 - Actual Bridge结果
```

至少计算：

```text
Memory Formation Recall差值
Current State Accuracy差值
Source Link F1差值
Bridge Case Exact差值
```

差值越大，说明端到端损失主要来自L1。

---

# 9. 错误归因

每个失败Case必须标注一个主原因和可选次原因。

主原因枚举：

```text
L1_SHOULD_STORE_MISS
L1_EXTRACTION_MISS
L1_EXTRA_CANDIDATE
L1_TYPE_ERROR
L1_KEY_IDENTITY_ERROR
L1_STATUS_ERROR
L1_POLARITY_ERROR
L1_EVIDENCE_ERROR
L2_IDENTITY_ERROR
L2_RELATION_ERROR
L2_ACTION_ERROR
L2_STATE_ERROR
L2_VERSION_ERROR
L2_SOURCE_ERROR
L2_PENDING_ERROR
IDEMPOTENCY_ERROR
INVARIANT_ERROR
SYSTEM_ERROR
```

归因规则：

1. Candidate未进入L2：L1；
2. Candidate存在但错误绑定Identity：L2；
3. Identity正确但Relation/Action错误：L2；
4. Decision正确但数据库快照错误：Repository/L2；
5. Replay后重复写入：幂等；
6. 多层同时失败，选择最早发生的层作为主原因。

---

# 10. 输出文件

结果目录建议：

```text
eval/results/l1_l2_bridge_<run_id>/
```

必须包含：

```text
run_manifest.json
dataset_audit.json
predictions_actual.jsonl
predictions_oracle_l1.jsonl
metrics_actual.json
metrics_oracle_l1.json
bridge_metrics.json
summary.md
failed_cases.jsonl
error_attribution.json
l1_candidate_confusion.json
l1_type_confusion.json
l2_relation_confusion.json
l2_action_confusion.json
task_status_confusion.json
field_metrics.json
invariant_report.json
latency_report.json
```

每条Prediction至少包含：

```text
case_id
turn_id
gold_l1
predicted_l1
candidate_mapping
gold_l2_decision
predicted_l2_decision
db_before
db_after
final_snapshot
error_stage
latency
llm_diagnostic
```

---

# 11. 运行记录

`run_manifest.json`必须记录：

```text
Git Commit SHA
数据集文件SHA256
运行时间
Python版本
PostgreSQL版本
环境名
L1模式
模型与Provider
Prompt版本
Schema版本
Rule版本
Embedding无关，但若被调用必须记录
超时
重试
并发数
Feature Flags
```

---

# 12. 验收边界

本数据集第一次运行的主要目标是建立真实衔接基线，不应为了达到独立Layer 2的100%而修改Gold或绕过L1。

## 硬安全门槛

```text
execution_error = 0
cross_space_contamination = 0
duplicate_active_rate = 0
stale_active_rate = 0
orphan_done_task_rate = 0
idempotence = 100%
```

## L2条件能力目标

在L1 Candidate已正确匹配的子集上：

```text
Task Identity F1 ≥ 0.95
Relation Macro-F1 ≥ 0.95
Action Accuracy ≥ 0.95
Source Link F1 ≥ 0.95
Pending-review F1 ≥ 0.95
```

## 衔接健康目标

```text
Candidate Propagation Recall ≥ 0.95
Memory Formation Recall ≥ L1严格Candidate Recall - 0.03
Status Propagation Accuracy不得比L1 Task Status Accuracy再下降超过3个百分点
Evidence-to-Source Continuity F1 ≥ 0.90
```

若不达标，先按错误归因判断是否是L1输入损失，不能直接修改L2规则。

---

# 13. Codex最终回复要求

运行完成后只提交：

1. 结果目录；
2. Actual与Oracle-L1的核心指标对比；
3. 最大的五类失败；
4. 失败归因到L1还是L2；
5. 是否触发安全硬门槛；
6. 所有运行命令；
7. Commit SHA；
8. 没有修改Gold的声明。

不得只回复“测试通过”。
