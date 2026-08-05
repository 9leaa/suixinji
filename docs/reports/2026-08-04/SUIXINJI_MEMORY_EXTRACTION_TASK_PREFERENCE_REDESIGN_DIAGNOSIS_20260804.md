# 随心记 Memory 抽取、Task/Preference 身份与状态模型诊断及推荐方案

> 日期：2026-08-04  
> 范围：多 Candidate 抽取、多个偏好完整抽取、近三轮指代、Task 二态状态、Task done/episodic、Preference 身份，以及上一轮 Task 身份放宽方案。  
> 本文先作为诊断和实施基线；2026-08-04 已按本文实施代码，未修改 PostgreSQL 用户历史数据。

## 1. 结论摘要

当前最重要的问题不是某一句 Prompt 写得不够好，而是下面四个机制叠加：

1. LLM 虽被告知“一条笔记可以产生多条候选”，但没有“逐原子事实覆盖”的可验证输出契约。
2. hybrid 模式只要 LLM 返回至少一条候选，就完全放弃 rules 发现但 LLM 遗漏的候选。
3. LLM candidates、rule hints 均存在最多 5 条的截断，Schema 校验失败的行会静默丢失。
4. Task/Preference 的“宽松召回身份”和“允许修改状态的严格身份”没有分层，导致召回过严与误合并风险只能二选一。

推荐的统一方向是：

```text
原子信息覆盖式抽取
→ 仅对近三条消息做受控指代消解
→ Task/Preference 宽松 Family 召回
→ LLM 做 same_instance / same_family / different / uncertain 裁决
→ 本地规则负责最终状态、Version、Source 和数据库写入
→ 查询与画像阶段再做更宽的语义聚合
```

Task 状态按用户的新要求收敛为：

```text
todo / done
```

这是硬契约：`blocked` 不再是 Task 状态。任务只要没有结束，无论是正在处理、卡住、等待权限、暂停还是暂时无法继续，`task_status` 一律为 `todo`。如果保留阻塞原因，它只能是普通说明字段或原始证据，绝不能参与状态枚举、状态过滤或状态流转。

但取消和正常完成不能在语义上完全丢失区别，建议同时保留非状态字段：

```text
closure_reason = completed | cancelled | abandoned | unknown
blocker / progress_note = 原始说明
```

即：取消任务时 `task_status=done`，同时 `closure_reason=cancelled`，避免查询时错误回答成“任务已经完成”。

## 2. 本次诊断依据

代码主要检查了：

- `memory/prompts.py`
- `memory/extractor.py`
- `memory/clause_splitter.py`
- `memory/extraction_schema.py`
- `memory/candidate_validator.py`
- `memory/field_contracts.py`
- `memory/canonicalizer.py`
- `memory/candidate_retriever.py`
- `memory/adjudicator.py`
- `memory/relation_guard.py`
- `memory/consolidator.py`
- `memory/policies/task.py`
- `memory/policies/preference.py`
- `memory/task_state.py`
- `memory/service.py`
- `apps/handlers.py`
- `repositories/postgres/notes.py`
- `repositories/postgres/inbox.py`

同时使用了 L1→L2 Bridge 真实链路运行结果：

- `eval/results/l1_l2_bridge_20260804_1310/predictions_actual.jsonl`
- `eval/results/l1_l2_bridge_20260804_1310/metrics_actual.json`

关键现象如下：

| 场景 | Gold Candidate | 实际预测 | 官方匹配 Recall | Candidate 数量完全一致 |
|---|---:|---:|---:|---:|
| multi_candidate_mixed | 24 | 17 | 66.67% | 12.50% |
| preference_same_add_source | 16 | 17 | 100% | 93.75% |
| preference_supersede | 24 | 18 | 66.67% | 62.50% |
| orphan_done_resolution | 8 | 13 | 62.50% | 37.50% |

`multi_candidate_mixed` 的 8 个 Turn 全部出现漏抽。典型原文：

```text
我喜欢咖啡，主要使用 MacBook Pro，这周要完成随心记评测。
```

Gold 是 preference、semantic、task 三条；实际经常只有 preference 和 task，并且 evidence/content 覆盖整句，semantic 被漏掉。

`preference_supersede` 的典型原文：

```text
我现在不喜欢咖啡了，更偏向绿茶。
```

Gold 是两条独立 preference；实际通常只生成一条整句 preference。

因此以下方案不是凭 Prompt 直觉提出，而是针对已复现的链路失效。

## 3. 问题一：多 Candidate 句子漏抽

### 3.1 当前问题在哪里

#### A. Prompt 只表达“允许多条”，没有表达“必须覆盖全部原子信息”

当前 Prompt 已包含：

```text
一条笔记可以产生多条候选
同一句中独立信息必须分别判断
```

所以不能简单归因为“Prompt 完全没写”。真正缺少的是可验证的覆盖协议：

- 没有给每个 clause/atom 编号；
- 没有要求返回 `covered_clause_ids`；
- 没有要求说明哪些 clause 被忽略以及原因；
- 没有 Candidate 数量与可存储原子事实数量的一致性检查；
- 没有针对漏抽 clause 的第二次补抽。

LLM 生成一条覆盖整句的 preference 后，在当前 Schema 看来仍是合法结果，因此系统无法知道 semantic/task 子句被吞掉了。

#### B. hybrid 的“非空 LLM 全权覆盖”会放大漏抽

`memory/extractor.py::extract_candidates()` 的当前行为是：

```text
LLM 返回非空 candidates
→ 只使用 LLM candidates
→ rules 发现的其他候选全部舍弃
```

这项设计能避免 rules/LLM 简单并集造成重复候选，但副作用是：LLM 只抽出 1 条时，rules 已发现的第 2、3 条也没有补救机会。

因此问题不是改成“无脑 rules + LLM 并集”即可解决。正确方向应是“按 clause 覆盖缺口补齐”。

#### C. 存在硬截断

当前链路存在：

- Prompt 定义最多 5 条 Candidate；
- LLM 输出只处理 `rows[:5]`；
- `_rule_hints()` 也只传前 5 条；
- clause splitter 最多 8 个 clause。

一条包含 6 个以上独立偏好/任务/事实的笔记，不可能完整通过当前单次输出契约。

#### D. clause splitter 覆盖不完整

当前切分主要识别句号、分号，以及后面带特定起始词的逗号。它不能稳定覆盖：

```text
喜欢咖啡和绿茶
不喜欢咖啡，更偏向绿茶
要修 API、补测试、写报告
```

特别是并列宾语和省略主语的后半句，可能仍作为一个大 clause 进入 LLM/rules。

#### E. Schema 失败会静默变成“少一条”

`parse_extracted_candidate()` 对以下情况直接返回 `None`：

- Pydantic 校验失败；
- task 缺 entity/attribute/operation/topic/status；
- evidence 不在当前原文中；
- 字段类型或枚举非法。

上层只看到有效 Candidate 变少，却不知道是模型没抽，还是抽了但 Schema 拒绝。

### 3.2 推荐处理方法

采用“Atomic Coverage Extraction”而不是只改 Prompt。

#### 第一步：先生成原子 clause/atom 清单

确定性切分和轻量规则只负责找覆盖边界，不负责决定最终类型：

```json
{
  "atoms": [
    {"atom_id":"a1", "text":"我喜欢咖啡"},
    {"atom_id":"a2", "text":"主要使用 MacBook Pro"},
    {"atom_id":"a3", "text":"这周要完成随心记评测"}
  ]
}
```

并列宾语应进一步拆成可独立更新的 atom：

```text
我喜欢咖啡和绿茶
→ 喜欢咖啡
→ 喜欢绿茶
```

但专有名称、固定短语和复合名词不能机械按“和”切开，切分结果仍需 LLM 校验。

#### 第二步：Prompt 改成覆盖协议

要求 LLM 对每个 atom 输出：

```json
{
  "atom_id":"a1",
  "decision":"candidate|ignore|uncertain",
  "ignore_reason":null,
  "candidates":[...]
}
```

硬约束：

- 每个 atom 必须出现一次；
- 一个 atom 可产生多条 Candidate；
- 一条 Candidate 必须只覆盖一个可独立更新的事实；
- `ignore` 必须给固定 reason_code；
- 不允许用覆盖整句的大 Candidate 代替多个独立事实。

#### 第三步：覆盖检查和定向补抽

首次输出后检查：

```text
eligible atoms - covered atoms = uncovered atoms
```

若存在遗漏，只把遗漏 atom 送入一次 targeted repair。不要重跑整个原文，也不要把所有 rules Candidate 无脑并入。

#### 第四步：取消“5 条即截断”的信息损失

建议：

- 单批 Candidate 上限提高到 10～12；
- 超出上限时按 atoms 分批，不直接截断；
- 每个 batch 都保留相同 note_id 和 atom_id；
- 最终按 `note_id + atom_id + type + evidence` 去重。

#### 第五步：保留拒绝诊断

Schema 解析应返回：

```text
valid_candidates
schema_rejections
coverage_gaps
repair_attempts
```

使 evaluator 能区分：

```text
模型漏抽 / 模型输出非法 / evidence 不落地 / validator 拒绝 / 去重删除
```

### 3.3 验收指标

建议至少设置：

- Multi-candidate Recall ≥ 95%；
- Preference multi-candidate Recall ≥ 98%；
- Candidate Count Exact ≥ 95%；
- Eligible Atom Coverage = 100%；
- Schema rejection 必须 100% 可审计；
- 单 Candidate Precision 不得因补抽下降超过 2 个百分点。

## 4. 问题二：一句话多个偏好必须完整提取

### 4.1 当前问题

这不是独立于问题一的另一个 Prompt 小问题，而是问题一在 Preference 上最明显的表现。

当前 `preference_signature()` 的 `_extract_topic()` 会：

1. 找到第一个偏好标记；
2. 按逗号/分号等切分；
3. 把第一部分作为 main topic；
4. 把其余部分作为 qualifiers。

因此：

```text
我不喜欢咖啡，更偏向绿茶
```

容易被压成“关于咖啡/绿茶的一条偏好”，而不是两个可独立更新的 Preference Candidate。

### 4.2 偏好的原子化原则

应按“可独立改变”来判断是否拆分：

```text
喜欢咖啡，也喜欢绿茶
→ 两条 preference

工作日早上喜欢喝淡咖啡
→ 一条 preference，scope=工作日早上，topic=淡咖啡

更喜欢绿茶而不是咖啡
→ 至少表达绿茶 positive 与咖啡 negative/comparative relation
```

不能把 scope、qualifier 或比较对象全部拆成新偏好；否则会过抽。

### 4.3 推荐 Schema

Preference Candidate 建议保留：

```text
topic/object
polarity
scope
qualifiers
comparison_target（可选）
intensity（可选）
evidence_span
atom_id
```

比较句可以产生两条原子 Preference，并用同一 `comparison_group_id` 关联。这样未来更新“咖啡”或“绿茶”时不必覆盖整句偏好。

### 4.4 Prompt 需要补的示例

Prompt 应明确给出正例：

```text
“我不喜欢咖啡，更偏向绿茶”
必须产生：
1. 咖啡 / negative
2. 绿茶 / positive
```

以及反例：

```text
“我喜欢工作日早上的淡咖啡”
不能拆成“工作日”“早上”“淡”“咖啡”四条。
```

但 Prompt 只是其中一环，仍必须配合 atom coverage、补抽和 Schema rejection 审计。

## 5. 问题三：跨轮指代只支持向前 3 条

### 5.1 当前状态

当前 `process_note_memory()` 传给 extractor 的主要是：

```text
note_id
current text
classification
rules hints
```

没有把当前消息之前的 Note/Inbox 消息作为抽取上下文。因此当前写入链路实际上没有可靠的跨轮指代消解；查询阶段虽能处理部分模糊提问，但那不是 ingest 时的 Candidate 抽取能力。

### 5.2 推荐边界

按用户要求冻结为：

```text
最多向前 3 条用户消息
```

超过 3 条不在 ingest 阶段自动解析，保留原 Note，交给最终查询 LLM 通过检索处理。

### 5.3 上下文选择必须基于消息顺序

不能简单按数据库 `created_at` 取最近三条，因为 Worker 可能并发或延迟执行。推荐基于：

```text
同 tenant
同 space
同 user
Inbox sequence_no < current sequence_no
按 sequence_no 倒序取 3 条
```

排除：

- 当前消息；
- bot/system 消息；
- 敏感或已删除 Note；
- 其他用户或其他 Space；
- 顺序在当前消息之后但先完成处理的消息。

### 5.4 仅在存在指代信号时启用

例如：

```text
这个、那个、它、这件事、上面那个、继续做、也完成了、取消它
```

普通完整句不应自动拼接前三条，避免旧信息污染当前 Candidate。

### 5.5 Prompt 和输出契约

输入必须明确分区：

```json
{
  "current_text":"这个也做完了",
  "previous_messages":[
    {"offset":-1,"note_id":"真实内部ID","text":"记得完成数据库迁移"}
  ]
}
```

LLM 只能从 `current_text` 抽取当前新增断言，previous_messages 只能用于解指代，不能重复抽取旧事实。

输出补充：

```text
reference_status = resolved | unresolved | not_applicable
antecedent_note_id
antecedent_offset = -1 | -2 | -3
antecedent_evidence_span
resolution_confidence
```

如果没有在三条内找到唯一 antecedent：

- 不自动修改 Task/Preference；
- 记录 `reference_unresolved`；
- 当前 Note 正常保存并可被查询检索；
- 不创建伪造的 canonical identity。

### 5.6 Source 契约

跨轮解析成功后，当前消息和 antecedent 都应进入可审计来源链：

```text
current note：状态/偏好变化的直接证据
antecedent note：被指代对象身份的证据
```

不能把上一轮文本复制进当前 `evidence_span`，否则证据看起来像出现在当前原文中。

## 6. 问题四：Task 状态只保留 todo/done

### 6.1 推荐的新状态契约

新写入只允许：

```text
todo
done
```

明确禁止：

```text
blocked
cancelled
in_progress
```

这些值均不得再作为新 Task Candidate、Memory、Version 或查询结果中的 `task_status`。其中 `blocked/in_progress` 统一归一化为 `todo`，`cancelled` 统一归一化为 `done`。

映射规则：

| 原表达/旧状态 | 新 task_status | 附加信息 |
|---|---|---|
| 计划、待办、正在、继续 | todo | progress_note 可选 |
| 阻塞、卡住、等待权限 | todo | 可选保留阻塞说明，但它不是状态 |
| 完成、做完、搞定 | done | closure_reason=completed |
| 取消、不做了、不用做 | done | closure_reason=cancelled |
| 放弃 | done | closure_reason=abandoned |

这里必须强调：`done` 只表示“该任务不再是当前待办”，不能自动等同于“成功完成”。用户问“完成了吗”时，Answer 层要结合 `closure_reason` 回答。相应地，任务只要尚未结束，即使当前完全无法推进，也仍然是 `todo`。

### 6.2 状态转移

推荐只保留：

```text
todo → todo：补充进展、阻塞原因、Source
todo → done：完成/取消/放弃
done → done：重复完成/取消证据，只加 Source
done → todo：明确重新打开或重新创建
```

`done → todo` 必须区分：

- reopen 同一 Task Instance；
- 新一代同族任务（new generation）。

由 Task Identity LLM + 本地规则共同判断；不应只看状态。

### 6.3 为什么必须保留 closure_reason

如果取消直接写成：

```text
task_status=done
```

但不保留原因，查询“这个任务完成了吗”时系统可能错误回答“已完成”。因此二态化可以简化当前/非当前过滤，但不能丢掉业务结局。

### 6.4 需要修改的范围（后续实施时）

生产契约涉及：

- `memory/models.py::TASK_STATUSES`
- `memory/field_contracts.py::TASK_STATUSES/TASK_STATUS_ALIASES/normalize_task_status`
- `memory/extraction_schema.py` 的 Literal 和 validator
- `memory/prompts.py` 的输出枚举与说明
- `memory/task_state.py`
- `memory/policies/task.py::ALLOWED_TRANSITIONS/is_terminal`
- `memory/relation_guard.py`
- `memory/consolidator.py::consolidate_done_task`
- `memory/repository.py` 与 PostgreSQL repository 的兼容判断
- `memory/service.py` 的画像/当前任务过滤
- Layer 1/2/3 evaluator、数据集、混淆矩阵和历史 timeline claim

注意：Worker/Outbox/Delivery Task 自身的 `blocked/cancelled` 是任务调度状态，不属于 Memory Task 状态，不能一起删除。

### 6.5 旧 PostgreSQL 数据兼容

按用户之前的要求，实施时不应自动迁移现有用户数据。推荐先做读取兼容：

```text
legacy in_progress → todo
legacy blocked     → todo + legacy blocker
legacy cancelled   → done + closure_reason=cancelled
```

新写入只产出 todo/done；旧记录等待用户后续手工迁移。

## 7. 问题五：Task done 自动转成 episodic

### 7.1 当前真实流程

`memory/consolidator.py::consolidate_candidate()` 对所有 `task_status=done` 的 Candidate 立即进入 `consolidate_done_task()`，不会先走普通的 `retrieve_candidates()`。

`consolidate_done_task()`：

1. 只读取 active task；
2. 使用 exact key、structured identity、`task_identity_compatible()`、fuzzy topic 匹配；
3. 多个匹配进入 pending-review；
4. 无匹配且是 strong task completion 时进入 pending-review；
5. 无匹配且不是 strong completion 时调用 `convert_orphan_done_task_to_episodic()`；
6. fuzzy 匹配即使只有一个，也进入 pending-review。

### 7.2 存在的问题

#### A. 严格 Task Identity 会造成错误的“无历史任务”

前一轮 Task 与本轮完成句换了说法时，严格匹配可能失败。系统随后把完成句当成 orphan，进一步转换为 episodic。

这不是事实类型真的变了，而是任务身份召回失败产生的级联错误。

#### B. done 分支绕过普通候选召回

普通 Task 会经过 hybrid candidate retrieval；done 分支直接扫描 active tasks 并采用自己的匹配规则。两条 Task 身份路径不统一，容易出现 todo 能召回、done 反而召回不到的情况。

#### C. Task 与 Episodic 被当成互斥类型

```text
昨天提交了论文初稿
```

既可能表示某 Task 已结束，也可能是有时间的事件。当前实现用“转换”把 Task 身份丢掉，而不是保留任务状态证据并按需增加 episodic 表达。

#### D. Bridge 已出现直接类型偏移

例如 Gold 期望 task(done)：

```text
提交论文初稿已经完成
```

实际抽取为：

```text
episodic：昨天已经提交论文初稿了
```

说明问题既可能发生在 L1 类型判断，也可能发生在 L2 orphan conversion；两个阶段都需独立审计。

### 7.3 推荐新流程

取消“匹配失败即把 task 破坏性转换为 episodic”的默认行为。

```text
task(done) Candidate
→ 近三轮指代解析
→ Task Family 宽召回（含 active 和必要的近期 terminal history）
→ LLM Task Identity 裁决
→ 本地状态规则
```

具体分支：

| 情况 | 推荐行为 |
|---|---|
| 唯一 same_task，旧状态 todo | 更新该 Task 为 done |
| 唯一 same_task，旧状态 done | add_source，不建重复 Task |
| same_family 但不是 same_task | 新建 done Task Instance 或 pending-review，不改旧任务 |
| 多个可能实例 | pending-review / uncertain，不强制合并 |
| 无历史但当前明确描述完整任务目标 | 允许新建 task(done)，保留 closure_reason |
| 当前只有“这个做完了”，三轮内无 antecedent | 不创建伪造 Task；记录 unresolved，交查询 LLM |
| 明确时间事件且有独立历史价值 | L1 可另产出 episodic Candidate；不要把 task 转掉 |

### 7.4 是否允许 Task + Episodic 双 Candidate

建议允许，但有条件：

- Task Candidate 表达任务当前/终止状态；
- Episodic Candidate 表达有独立价值的事件时间、地点或经历；
- 两者 Source 可来自同一 Note；
- 二者建立 derived/related 关系，不相互覆盖；
- 普通“任务完成了”不必自动再生成 episodic，避免重复记忆。

## 8. 上一轮结论：Task 身份放宽

### 8.1 当前过严点

当前 Task key 主要由：

```text
entity + attribute + operation + scope
```

构成。`task_identity_compatible()` 除精确外主要接受相同 topic 或较长字符串以短字符串结尾；candidate retriever 对 V3 Task 也只保留很窄的 suffix refinement；Relation Guard 不兼容时直接按新任务插入。

因此同一任务不同表述容易得到不同 Key；但若直接降低字符串阈值，又会把同项目下不同任务误合并。

### 8.2 推荐拆成 Task Family 与 Task Instance

#### Task Family：宽松召回身份

用于：

- 画像聚合；
- 用户提问时召回相关任务；
- L2 给 LLM 提供候选集合；
- 发现同一项目/目标的多种表达。

建议特征：

```text
tenant/space/user
project/entity
goal/material
主要对象
外部编号/专有名词
时间范围
alias
```

状态词、时态词和“完成/处理/优化”等宽泛动作不应成为硬身份。

#### Task Instance：严格状态身份

用于：

- 修改 todo/done；
- 创建 Version；
- Source 绑定；
- reopen/new generation；
- 防止同族不同任务误合并。

### 8.3 LLM 裁决契约

宽召回后让 LLM 输出：

```json
{
  "identity_relation":"same_instance|same_family|different|uncertain",
  "confidence":0.0,
  "reason_code":"...",
  "supporting_fields":[],
  "conflicting_fields":[]
}
```

本地策略：

| LLM 结果 | 行为 |
|---|---|
| same_instance + 高置信 | 允许进入本地状态流转 |
| same_family | 保留独立 Instance，建立 family/related 关系 |
| uncertain | pending-review 或独立保存，不更新已有状态 |
| different | 新建 Task Instance |

LLM 不得直接执行数据库写入，不得绕过状态机，不得覆盖 Source/Version。

## 9. 问题六：Preference 身份是否也过严

### 9.1 结论

是的。Preference 当前也存在“召回可以有点宽，但最终裁决仍要求精确 key”的不一致。

当前 Preference：

- key 包含 `entity + normalized topic + scope`；
- retriever 可以用 `topic_compatibility >= 0.75` 找到近似主题；
- named anchors 必须完整集合相同，防止 A1/A10、iPhone 15/iPhone 16 被误合并；
- Relation Guard 对正常 same/update 仍要求 exact key、相同 subject 和 scope；
- V3 Adjudicator 没有 exact identity 时通常走 new/insert；
- 当前 LLM advisory 只提供建议，不能真正执行 Preference 身份合并。

这会导致同一偏好的不同说法形成多条：

```text
喜欢喝燕麦拿铁
早上通常选燕麦咖啡
更偏爱口味淡一点的燕麦拿铁
```

但 Preference 比 Task 更不能简单模糊合并，因为偏好允许同时共存，且 polarity/scope 很重要。

### 9.2 推荐拆成 Preference Family 与 Preference Assertion

#### Preference Family：宽召回

用于画像与查询聚合：

```text
饮品偏好
咖啡偏好
工作方式偏好
界面主题偏好
```

#### Preference Assertion：可持久化的原子断言

至少保留：

```text
topic/object
polarity
scope
qualifiers
intensity
comparison_target
evidence
```

只有确定为同一 Assertion，才允许 add_source/update/supersede。

### 9.3 LLM Preference 裁决

推荐输出：

```json
{
  "identity_relation":"same_assertion|same_family|different|uncertain",
  "polarity_relation":"same|changed|not_comparable|uncertain",
  "scope_relation":"same|overlap|disjoint|uncertain",
  "confidence":0.0,
  "reason_code":"..."
}
```

规则边界：

- 同 Family 不代表旧偏好被替代；
- 不同 scope 可以同时成立，例如早上喜欢咖啡、晚上不喜欢咖啡；
- 明确型号、版本、专有名词不同，不能因 embedding 相似就合并；
- polarity 相反但没有“现在/不再/改为”等更新证据时，优先 conflict/共存，不自动 supersede；
- 查询/画像可让 LLM 聚合展示，但不能反向覆盖原始 Preference Assertion。

### 9.4 放宽的位置

应该放宽：

- Preference Family 召回；
- lexical/vector/alias Top-K；
- 用户画像聚类；
- 查询时 LLM rerank 和综合回答。

不应该放宽：

- polarity 更新；
- scope 覆盖；
- named anchor 冲突；
- 自动 supersede；
- Source/Version 归属。

## 10. 统一架构建议

### 10.1 Layer 1：完整抽取，不做最终身份合并

职责：

```text
atom coverage
type
topic/goal
polarity/status
scope
evidence
近三轮 antecedent
```

不负责决定已有 Memory 是否就是同一实例。

### 10.2 Layer 2A：宽松候选召回

分别维护：

```text
Task Family retrieval
Preference Family retrieval
```

使用确定性字段、lexical、embedding、alias 和时间联合召回，目标是高 Recall@K。

### 10.3 Layer 2B：LLM Identity Adjudication

只比较当前 Candidate 与已召回的少量真实 Memory。输入不包含 Gold 或 evaluator 逻辑 Ref。

LLM 可判断：

```text
same instance/assertion
same family
different
uncertain
```

### 10.4 Layer 2C：确定性写入策略

规则继续掌管：

- todo/done 状态流转；
- closure_reason；
- Version；
- Source；
- ACL/sensitivity；
- stale evidence；
- 幂等；
- 并发 snapshot/version 检查；
- pending-review。

### 10.5 Layer 3：查询与画像宽聚合

查询阶段可以比写入阶段更宽：

1. 召回 Family 下的多个 Task Instance/Preference Assertion；
2. 召回相关 Version 与 Source；
3. 由最终 LLM 结合问题意图聚合；
4. 明确区分“当前状态”和“历史/相关任务”；
5. 不把查询时的临时聚合结果反写成永久合并。

## 11. 推荐新增字段

初期可以先放在结构化 scope/audit 中验证，稳定后再决定是否正式迁移 Schema：

### 通用 Candidate

```text
atom_id
coverage_status
schema_validation_status
reference_status
antecedent_note_id
antecedent_offset
resolution_confidence
identity_family_hint
identity_confidence
```

### Task

```text
task_family_key
task_instance_key
aliases
closure_reason
blocker
progress_note
generation
```

### Preference

```text
preference_family_key
preference_assertion_key
aliases
comparison_group_id
comparison_target
intensity
```

内部可以使用真实数据库 ID，但生产逻辑不得依赖 m1/c1/v1/s1 等 evaluator 逻辑 Ref。

## 12. 建议实施顺序

### Stage 0：冻结新契约和重算数据集

- Task 状态固定 todo/done；
- blocked/in_progress→todo，cancelled→done；新写入严禁出现 blocked；
- closure_reason/blocker 契约；
- 更新 Layer 1/2/3 Gold 与 evaluator 映射，但不得为了结果篡改语义；
- 保留现有 PostgreSQL 数据不动。

### Stage 1：先修多 Candidate/多 Preference 完整率

- atom splitter；
- coverage Prompt；
- clause/atom IDs；
- targeted repair；
- 去掉静默截断；
- Schema rejection 可审计。

这是最高优先级，因为 L1 漏抽之后，L2/Layer 3 无法补回不存在的 Candidate。

### Stage 2：加入近三轮指代解析

- 基于 Inbox sequence；
- 同 tenant/space/user；
- 仅在指代信号触发；
- antecedent Source 审计；
- 三轮外 unresolved。

### Stage 3：Task Family / Instance

- 宽召回；
- LLM identity adjudication；
- todo/done 本地状态机；
- false merge hard negatives。

### Stage 4：重做 done/orphan/episodic

- done 统一走 Task Family 检索；
- 移除默认破坏性 conversion；
- 支持 task + episodic 独立 Candidate；
- 保证明确完成任务不会因措辞变化丢失 Task 身份。

### Stage 5：Preference Family / Assertion

- 宽 Preference 召回；
- polarity/scope/named-anchor 作为硬保护；
- LLM 做 same assertion/same family 裁决；
- 多偏好和比较偏好专项。

### Stage 6：查询和画像聚合

- Family 级 recall；
- LLM query-time grouping；
- 画像去重；
- 保留原始 Memory/Version/Source，不反写临时合并。

## 13. 建议专项数据集与验收门槛

### 多 Candidate

- 2/3/5/8 个原子事实；
- 同类型多个 Candidate；
- 混合 task/preference/semantic/episodic；
- 中文连接词、省略主语、顿号、换行；
- 1 个合法 + 1 个低价值 + 1 个敏感内容。

门槛：

```text
Eligible Atom Coverage = 100%
Multi-candidate Recall ≥ 95%
Candidate Count Exact ≥ 95%
```

### 多 Preference

- 同极性多个对象；
- 正负混合；
- comparative；
- 相同 topic 不同 scope；
- 型号/版本 hard negatives。

门槛：

```text
Preference Candidate Recall ≥ 98%
Polarity Accuracy ≥ 98%
Scope Accuracy ≥ 95%
```

### 近三轮指代

- antecedent 在 -1/-2/-3；
- -4 必须 unresolved；
- 三轮内有多个可能 antecedent；
- 跨用户、跨 Space 污染；
- Worker 乱序完成但 sequence 正确。

门槛：

```text
三轮内唯一指代解析 Accuracy ≥ 95%
跨用户/跨 Space 污染 = 0
三轮外自动解析率 = 0
```

### Task Identity

```text
Task Family Recall@10 ≥ 98%
same_instance 自动合并 Precision ≥ 99%
Hard-negative False Merge Rate ≤ 1%
Task 状态流转 Accuracy ≥ 98%
```

### done / episodic

```text
已有 Task 完成身份 Recall ≥ 98%
错误 Task→Episodic Conversion Rate = 0
无 antecedent 的模糊完成句错误更新率 = 0
```

### Preference Identity

```text
Preference Family Recall@10 ≥ 98%
same_assertion 自动合并 Precision ≥ 99%
Polarity/Scope 错误覆盖率 = 0
```

## 14. 不推荐的快捷修改

以下修改看似能快速提分，但会产生系统性问题：

1. 只在 Prompt 再加一句“不要漏抽”；
2. 将 rules 和 LLM candidates 无脑并集；
3. 直接把 5 改成更大的数字但不做 atom coverage；
4. 仅降低 Task/Preference 字符串相似度阈值；
5. 仅凭 embedding 最近邻自动 merge；
6. 让 LLM 直接更新状态、Version 或数据库；
7. 把取消映射 done 后丢掉 cancellation reason；
8. 把无法匹配的所有 done Task 都转成 episodic；
9. 将前三条旧消息直接拼进当前原文，并允许它们产生新 Candidate；
10. 为了通过 evaluator 使用 Gold 或逻辑 Ref 做判断。

## 15. 最终推荐决策

推荐采用一条统一路线，而不是六个独立补丁：

```text
覆盖式原子抽取
  解决多 Candidate 和多个偏好漏抽

近三轮受控指代
  解决“这个/它/也完成了”等局部语境

Task/Preference 两层身份
  Family 用于宽召回和画像
  Instance/Assertion 用于安全写入

LLM 身份裁决 + 本地确定性状态机
  LLM 承担语义比较
  规则保护状态、证据、并发和安全

todo/done 二态 + closure_reason/blocker
  简化当前任务判断
  不丢失取消和阻塞的真实语义

取消默认 Task→Episodic 破坏性转换
  先恢复任务身份
  Episodic 只作为独立、有证据的事件 Candidate
```

其中第一优先级必须是多 Candidate/多 Preference 完整率；因为 Candidate 一旦在 L1 漏掉，后续的 L2 身份裁决、状态流转、画像和查询都没有机会恢复。

## 16. 本次实施结果（2026-08-04）

已完成：

- Task 新写入固定为 `todo/done`；旧 `in_progress/blocked/cancelled` 只读归一化，未回写 PostgreSQL；`closure_reason`、`blocker`、`progress_note` 保存在结构化 scope 中。
- 原子覆盖抽取：扩大 clause 边界、取消 5 条硬截断、按未覆盖 atom/type 补规则候选、支持多偏好对象拆分、记录 Schema 拒绝事件。
- 近三轮指代：按 tenant/space/user + Inbox `sequence_no` 取前 3 条，仅在指代信号出现时传入 LLM；保留 antecedent 元数据并补来源审计。
- Task/Preference Family：加入宽召回分数和 family/assertion 元数据；高置信身份 LLM 只提供裁决，状态、极性、scope、Source 和写库仍由本地规则保护。
- 所有 Task Candidate（包括 done）统一经过召回/裁决路径；取消默认的 Task→Episodic 破坏性转换；无历史但身份完整的完成任务保存为 `task(done)`。
- Layer 2/桥接评测适配器按二态契约归一化旧数据；Version 和查询投影不再泄漏旧 Task 状态。
- 持久化入口再次统一归一化 Task 状态，移除 `cancelled` 作为 Memory Task 演化分支；调度系统自身的 `blocked/cancelled` 仍保留，和 Memory Task 明确隔离。

验证：

- Memory 相关回归：`144 passed`。
- Worker/Outbox/抽取专项回归：`56 passed`。
- 全部 `tests/`：`429 passed, 1 failed`。唯一失败是既有 `tests/2阶段测试/test_query_agent_react.py::test_answer_question_defaults_to_semantic_search_when_llm_returns_no_action`，单独运行同样失败，未涉及本次改动文件。

未完成或需后续专项：

- 需要在开启 `SUIXINJI_STRONG_ESCALATION_ENABLED` 后，用真实供应商跑 Task/Preference 身份 LLM 的专项评测；默认关闭以保持安全和成本可控。
- PostgreSQL 旧历史数据尚未迁移，符合“先不要动用户数据”的要求；后续可按二态映射手工调整。
- 查询 ReAct 的既有失败需要单独诊断，不属于本次 Task/Preference 重构范围。
- 另外，当前环境 `STORAGE_BACKEND=postgres` 下存在历史 `metric-*` 评测空间；Stage 2 脚本现已加保护，拒绝在 PostgreSQL 后端运行。此次未删除或迁移这些评测空间，避免扩大数据操作范围。
