# 随心记 Layer 3 修复前复核任务书（交给 Codex）

> 本轮任务性质：**只分析，不修改代码**  
> 目标：结合既有诊断书、最新测试结果和当前仓库真实实现，重新确认根因并提交修改计划。  
> 项目路径：`/home/zcj/suixinji`

---

# 1. 本轮需要读取的材料

请先完整读取：

1. 既有诊断书：

```text
SUIXINJI_LAYER3_RESULT_DIAGNOSIS_AND_REPAIR_PLAN.md
```

2. 最新测试结果：

```text
layer3_full_repair_20260803_1642.zip
```

3. 当前仓库真实代码：

```text
/home/zcj/suixinji
```

4. 第三阶段评测说明与数据集契约：

```text
suixinji_layer3_evaluation_runbook_for_codex.md
eval/layer3/
```

如果上述文件被放在其他目录，请先搜索定位，不要因为路径不同而跳过。

---

# 2. 重要原则

既有诊断书是根据上一轮结果写出的，其中部分问题在最新修复中已经解决。

因此：

```text
不要直接照诊断书实施
不要默认里面每一项仍然成立
不要因为最新总指标未达标，就认定所有修复都无效
```

正确做法是：

```text
旧诊断书中的问题假设
+
最新逐Case结果
+
当前生产代码调用链
=
新的根因分析和修改计划
```

每个结论都必须注明属于：

```text
A. 已经修复
B. 部分修复
C. 尚未修复
D. 评测器统计/接线错误
E. 无法从现有证据确认
```

---

# 3. 最新结果中已经确认的变化

以下内容是本轮分析的起点，必须重新从结果文件和代码中验证，不要只引用本任务书。

## 3.1 已明显修复

初步观察表明：

```text
Answer运行错误：289 → 0
RateLimit错误率：55.58% → 0
Sensitive Access Violations：15 → 0
Cross-space Violations：保持0
查询业务状态修改：保持0
```

当前状态检索仍保持：

```text
Recall@1 = 100%
Current Hit@1 = 100%
MRR = 1.0
nDCG@10 = 1.0
```

请确认这些结果是否由真实生产逻辑产生，而不是仅在评测Adapter中规避。

---

## 3.2 已部分实现，但评测可能没有正确统计

初步观察表明，以下能力可能已经存在：

```text
历史Version读取
Task列表查询
Episodic列表查询
Profile Summary
结构化Answer Type
权限前置过滤
```

但部分真实结果可能没有进入统一的：

```text
retrieved_refs
selected_context_refs
selected_version_refs
executed_channels
```

例如：

```text
Answer实际使用了v1/v2/v3
但History Hit仍然为0
```

或者：

```text
list_tasks实际返回了任务
但retrieval数组为空
```

请重点确认：

> 这是生产能力未实现，还是评测Adapter没有把结构化工具结果统一映射为可评分证据？

---

# 4. 本轮必须重点复核的问题

---

## 4.1 统一检索证据与逻辑Ref映射

请完整追踪以下调用链：

```text
Query Planner
→ memory_search / structured tools / history tools
→ raw results
→ fusion / filtering
→ selected context
→ answer decision
→ answer result
→ eval adapter
→ logical ref mapping
→ metrics
```

重点检查：

```text
普通Memory Search结果
History Version结果
list_tasks结果
list_recent_episodes结果
profile_summary结果
pending/conflict结果
```

是否都进入统一的数据结构。

需要回答：

1. 当前有哪些不同的结果对象？
2. 哪些对象只进入Answer，没有进入Evaluator？
3. `m1/v1/s1/pr1`映射在哪里完成？
4. Version真实ID为何有不同格式？
5. 结构化工具结果是否丢失rank、score、channel和logical_ref？
6. `retrieved_refs`、`selected_context_refs`和`selected_version_ids`是否来自同一事实来源？
7. 当前History Hit=0究竟是功能失败还是统计失败？

建议评估是否需要统一：

```python
RetrievalEvidence
```

但不要为了形式统一做无必要的大规模重构。先说明最小修改方案。

---

## 4.2 Episodic检索正确但回答为No-answer

最新结果中可能存在：

```text
Episodic Recall@1 = 100%
但Answer全部为no_answer
```

请定位：

1. Episodic Memory是否进入最终Context？
2. Answer Decision是否只认可Task/Preference/Semantic？
3. “何时发生”查询是否错误要求Version？
4. Episodic的事件时间存在哪个字段？
5. `content/current_value/valid_from/observed_at`如何被转成Supported Claim？
6. Relevance Gate为何拒绝已经命中的Episodic？
7. 是生产逻辑错误，还是Evaluator错误识别Answer Type？

必须给出具体模块、函数和条件分支。

---

## 4.3 Semantic检索命中但Answer拒答

最新结果中可能出现：

```text
typo / noise / indirect_reference检索已命中m1
但最终Answer仍为no_answer或qualified_history_only
```

请分析：

1. Top1是否确实为目标Memory？
2. Top1与Top2分差是多少？
3. 当前Relevance Gate使用什么条件？
4. 是否因为存在干扰项就整体拒答？
5. 是否误把`updated_at`、`status`或来源字段判成历史信息？
6. `qualified_history_only`是如何触发的？
7. 是否存在Planner Route与Answer Decision语义不一致？

请区分：

```text
Retriever没有召回
Retriever召回但Final Context丢失
Final Context正确但Answer Decision拒绝
Answer正确但Evaluator误判
```

---

## 4.4 History专用数据集与Complex History Synthesis未复用同一路径

可能存在：

```text
history_and_temporal能够读取Version
multi_memory中的history_synthesis却仍只搜索当前Memory
```

请检查：

1. History意图识别在哪一层？
2. `总结X从开始到完成的过程`为什么没有进入Timeline路径？
3. 专用History Query和Complex Query是否走两套不同实现？
4. 是否存在重复实现？
5. 应该把哪一个版本作为唯一生产入口？
6. History Timeline结果如何进入Claim和Citation？

修改计划应优先复用现有正确路径，而不是再新建第三套逻辑。

---

## 4.5 Task列表返回范围失控

可能存在：

```text
Gold要求3个项目
list_tasks返回6个，包括干扰任务
Claim Recall=100%
Claim Precision很低
```

请确认：

1. Query中的数量限制是否被解析？
2. `三个项目`是否进入limit？
3. list_tasks按什么排序？
4. 是否需要按当前状态、主题相关性或最近访问过滤？
5. 为什么干扰任务也被返回？
6. 是否存在同Topic重复Task？
7. 是否需要Identity去重或Active-only过滤？
8. 数据集中的干扰项在真实业务语义上应如何排除？

不要简单硬编码`limit=3`。需要建立通用规则：

```text
显式数量
状态过滤
时间范围
主题范围
去重
排序
```

---

## 4.6 Episodic列表只部分成功

请检查：

1. `list_recent_episodes`实际读取哪个表？
2. 是否只读取Memory而未读取事件时间？
3. “最近”按哪个时间字段排序？
4. `valid_from / observed_at / created_at / updated_at`优先级是什么？
5. 为什么只返回一部分Gold事件？
6. 返回的结构化结果是否进入统一Evidence？
7. Citation是否关联到对应事件Source？

请明确业务时间与数据库写入时间的区别。

---

## 4.7 Absent场景仍使用无关Memory

可能存在：

```text
Query询问喜欢的电影
系统回答不知道电影，但附带咖啡偏好
```

请分析：

1. 无关Memory如何进入Final Context？
2. Relevance最低门槛在哪里？
3. 为什么No-answer后仍允许输出额外事实？
4. Citation Builder为何引用无关Source？
5. Answer模板是否把所有Context内容都组织进答案？
6. 是否应该在Answer Decision之前清空低相关Context？

正确行为应是：

```text
没有相关证据
→ no_answer
→ selected context为空或仅保留诊断信息
→ 不输出无关事实
→ 不引用无关Source
```

---

## 4.8 Stale-only全部变成纯No-answer

场景：

```text
只有“以前住在上海”
Query问“现在住在哪里”
```

期望：

```text
qualified_history_only
只能确认历史值，不能确认当前值
```

请检查：

1. 过期Memory或Version是否仍可作为历史证据进入Context？
2. 当前过滤器是否把它完全删除？
3. `qualified_history_only`需要哪些输入条件？
4. `status=superseded`和Version的处理是否不同？
5. 如何保证历史证据可说、当前结论不可说？

修改方案必须保持：

```text
Stale Answer Usage = 0
```

但不能因此丢失所有历史可用信息。

---

## 4.9 Conflict仍武断选择Active一侧

场景：

```text
Active Memory：喜欢绿茶
Pending冲突：不喜欢绿茶
```

可能实际Raw结果能看到两侧，但Final Context只保留Active。

请定位：

1. PendingReview是否Seed成功？
2. Query时是否查询PendingReview？
3. PendingReview如何关联Memory Identity？
4. Conflict信息在哪一步被丢弃？
5. Active-only过滤是否误删冲突证据？
6. Answer Decision如何判断Conflict？
7. 为什么20条全部变成answered而不是conflict？

正确链路应是：

```text
查询当前身份
→ 发现未解决PendingReview/Conflict
→ 构造ConflictContext
→ answer_type=conflict
→ 不武断选择一边
```

不能把Pending对象当普通Memory直接展示，也不能彻底忽略。

---

## 4.10 Ambiguous查询未触发Clarification

场景：

```text
第一阶段评测
第二阶段评测
Query：那个评测怎么样了？
```

系统可能列出两个结果，但没有要求用户澄清。

请分析：

1. 是否有歧义检测逻辑？
2. 如何比较候选Identity和分数接近度？
3. 什么情况下允许“列出所有候选”代替Clarification？
4. 数据集契约为何要求Clarification？
5. Clarification Options从哪里生成？
6. 是否需要区分：
   - 用户要求“都列出”
   - 用户使用单数模糊指代

修改方案必须是通用意图规则，而不是针对“那个评测”写关键词特判。

---

## 4.11 Claim粒度与评分异常

可能存在：

```text
一条Claim文本包含6个任务
但只绑定2个memory_id
```

或者：

```text
历史答案内容正确
但Claim Precision极低
```

请检查：

1. Claim由生产Answer生成还是Evaluator重新切分？
2. 一个Claim允许包含几个事实？
3. Claim与Memory/Version/Source是否一一可追踪？
4. 历史列表标题、版本号是否被误判成事实Claim？
5. Claim Matcher是否把同义表达错误算FP？
6. 生产Claim结构和评测Gold Claim结构是否一致？

推荐原则：

```text
一个可独立验证的事实 = 一条Claim
```

但请先根据现有实现提出最小可行改动。

---

## 4.12 Stale指标仍可能统计错误

请检查当前指标实现是否仍然通过：

```text
must_not_return_refs
```

推导Stale。

真正Stale应仅由对象状态和业务时间判断：

```text
status == superseded
valid_until < query_time
历史Version被用作当前值
旧状态覆盖当前状态
```

需要分别输出：

```text
Stale Retrieval Violation
Stale Answer Usage
Irrelevant Retrieval
Access-control Violation
Ambiguous Candidate Usage
Must-not-return Violation
```

确认它们当前是否真实计算，还是仅字段存在但值一直为0。

---

# 5. 必须区分的两类问题

最终报告必须把问题拆成两大类。

## 5.1 生产逻辑问题

例如：

```text
Answer Decision错误
Conflict信息没有进入Context
list_tasks返回范围错误
History Synthesis没有调用Timeline
Episodic时间解析错误
```

## 5.2 评测器/Adapter问题

例如：

```text
Version已被使用但History Hit仍为0
结构化工具结果没有进入retrieved_refs
逻辑Ref映射不完整
Claim Matcher粒度错误
Stale指标口径错误
```

禁止用修改Evaluator掩盖真实生产错误。

同样，也禁止为了提升指标而在生产逻辑中读取Gold。

---

# 6. 需要检查的代码区域

请根据仓库真实结构定位，不要只机械使用以下路径。

重点检查：

```text
memory/service.py
agent/query_agent.py
repositories/postgres/memory.py
memory/retrieval/
memory/query_router*
memory/models.py
infrastructure/llm/
eval/layer3/
tests/
```

需要给出：

```text
文件路径
类/函数
调用关系
当前行为
根因
建议修改
相关测试
```

不要只写“修改检索器”“优化Answer Prompt”。

---

# 7. 本轮禁止事项

本轮只提交分析和计划，不允许：

```text
修改生产代码
修改Evaluator代码
修改数据集
修改Gold
重新运行全量评测
提交Commit
```

允许：

```text
读取代码
解压结果
运行只读分析脚本
统计JSONL
检查日志
运行不会修改仓库的只读命令
```

如果必须运行测试才能确认假设，只在计划中说明，不要在本轮实施。

---

# 8. Codex必须提交的分析报告

请生成：

```text
LAYER3_PRE_REPAIR_ROOT_CAUSE_ANALYSIS.md
```

结构必须包括：

## 8.1 执行摘要

用一页说明：

```text
哪些已经修好
哪些是评测统计错误
哪些是生产逻辑错误
哪些仍无法确认
```

## 8.2 修复前后对比

至少包括：

```text
Answer错误
敏感权限
当前状态
History
Semantic
Multi-memory
No-answer
Claim
Citation
Latency
```

## 8.3 调用链审计

画出或描述：

```text
Query
→ Planner
→ Tools/Channels
→ Evidence
→ Context
→ AnswerDecision
→ Answer
→ Evaluator
```

标出每个信息丢失点。

## 8.4 问题逐项根因

对本任务书第4节的每个问题给出：

```text
状态
证据
根因
影响范围
涉及代码
是否为生产问题
是否为评测问题
```

## 8.5 证据引用

必须引用：

```text
结果文件
case_id
关键字段
代码文件与行号/函数名
```

禁止只写推测。

无法确认时明确写：

```text
现有证据不足
需要通过什么最小测试确认
```

---

# 9. Codex必须提交的修改计划

请生成：

```text
LAYER3_NEXT_REPAIR_IMPLEMENTATION_PLAN.md
```

计划必须包括：

## 9.1 修改顺序

建议按依赖关系排序，而不是按指标高低排序。

重点考虑：

```text
统一Evidence与评测接线
→ Answer Decision
→ History/Complex复用
→ List范围
→ Conflict/Clarification
→ Claim/Citation
→ 指标修正
```

Codex可以调整顺序，但必须说明原因。

## 9.2 每项修改的最小范围

每一项写明：

```text
修改文件
新增/修改接口
不修改的模块
兼容性风险
回归风险
```

## 9.3 测试计划

每项修复必须有对应测试：

```text
单元测试
Repository Contract Test
Query Agent测试
Eval Adapter自测
单数据集回归
全量回归
```

## 9.4 验收指标

至少包括：

```text
当前状态不回归
History真实命中
Episodic回答
Semantic命中后可回答
Task列表范围
Stale-only
Conflict
Clarification
Absent
Claim/Citation
真实Stale指标
```

## 9.5 回滚点

对高风险修改说明：

```text
如何灰度
如何回滚
如何避免破坏Layer 1/Layer 2
```

---

# 10. 强制回答的问题

Codex最终必须明确回答以下问题：

1. History Hit=0是生产功能失败、Evaluator接线失败，还是两者都有？
2. Task List已经真实执行了吗？为什么检索指标仍为0？
3. Episodic检索命中后为什么Answer Decision仍拒答？
4. Semantic命中后为什么仍输出No-answer？
5. History Synthesis为什么没有复用Timeline能力？
6. Conflict信息具体在哪一步丢失？
7. Stale-only为什么没有进入qualified_history_only？
8. Ambiguous为什么没有触发Clarification？
9. Task列表为什么返回全部干扰项？
10. Claim Precision低主要来自生产Claim结构还是Matcher？
11. 当前Stale Rate是否仍然错误使用must_not_return_refs？
12. 哪些修复只需要改Evaluator，哪些必须改生产代码？
13. 是否存在为了本次数据集写死关键词、数量或对象名的实现？
14. 最新修复是否有逻辑只存在于Eval Adapter，没有进入生产入口？
15. 下一轮修复完成后，应先跑哪些小集，再跑520条全量？

---

# 11. 验收计划建议

修改计划应至少包含以下阶段：

## Stage A：评测接线自测

使用少量固定Case验证：

```text
History Version逻辑Ref
list_tasks逻辑Ref
Episodic List逻辑Ref
selected_context
Citation
```

## Stage B：Answer Decision专项

分别测试：

```text
answered
no_answer
qualified_history_only
conflict
clarification
restricted
```

## Stage C：功能小集

先分别运行：

```text
Episodic当前问答
Semantic命中后回答
History专用
History Synthesis
Task List
Episodic List
Absent
Conflict
Ambiguous
```

## Stage D：全量520 Cases

全部小集通过后再运行全量。

## Stage E：回归

至少运行：

```text
Layer 1核心回归
Layer 2 PostgreSQL回归
Redis Worker smoke
真实 /ask smoke
```

---

# 12. 最终要求

本轮Codex回复中必须包含两个Markdown文件：

```text
LAYER3_PRE_REPAIR_ROOT_CAUSE_ANALYSIS.md
LAYER3_NEXT_REPAIR_IMPLEMENTATION_PLAN.md
```

在用户确认前：

```text
不要修改代码
不要开始实施
不要提交Commit
不要宣称已经修复
```

最终回复只需要说明：

```text
分析文件位置
计划文件位置
最重要的3～5个结论
仍需用户确认的问题
```
