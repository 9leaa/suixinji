# 随心记 Layer 3 修复前根因复核报告

> 本报告按 `CODEX_LAYER3_PRE_REPAIR_ANALYSIS_TASK.md` 生成。  
> 本轮只做分析，不修改生产代码、Evaluator、数据集或 PostgreSQL 用户数据。  
> 最新结果目录：`eval/results/layer3_full_repair_20260803_1642`

## 1. 执行摘要

结论：上一轮修复不是无效，P0 可用性和敏感权限已经明显修好；当前 Layer3 未通过的主因已经变成“生产 Evidence/AnswerDecision 未统一”和“评测 Adapter 未接入结构化工具与 Version 逻辑 ref”。两类问题同时存在，不能只改 Evaluator，也不能只调检索参数。

| 问题域 | 状态 | 结论 |
|---|---|---|
| Answer 运行错误 / RateLimit | A 已修复 | 最新 520 cases 中 `answer_error_count=0`、`answer_error_types={}`。 |
| Sensitive / Cross-space / 只读 | A 已修复 | 最新 `access_violation_count=0`、`business_state_mutation_count=0`；代码在 RRF 前做 ACL。 |
| 当前状态检索 | A/B | 检索 A：`current_state_retrieval Recall@1=1.0`；回答 B：30 个 current cases 被判 `no_answer`，6 个误判 `qualified_history_only`。 |
| History 专用查询 | B/D | 生产能读 timeline 并回答版本内容；但 Evaluator 只用 `retrieved_refs` 评分，未把 version id 映射成 `v1/v2/v3`，导致 History Hit=0。 |
| Complex History Synthesis | C | “总结 X 从开始到完成”没有复用 `memory_history`，走普通 complex 搜索，常召回当前/干扰 Memory。 |
| Task / Episodic / Profile 结构化工具 | B/C/D | 工具真实执行，但结果没有进入统一 `retrieved_refs/executed_channels`；生产侧列表范围、数量、排序和去重仍不正确。 |
| Episodic 单点问答 | C | Top1 命中 episodic 后 Answer 层仍拒答，说明不是召回失败，而是最终上下文/决策/生成链路失败。 |
| Semantic typo/noise/indirect | C | typo/noise Top1 命中仍 `no_answer`；indirect 因“之前”误走 history，输出 `qualified_history_only`。 |
| Absent / stale-only / conflict / ambiguous | C/D | no-answer 决策、历史证据回退、pending conflict、歧义澄清均未形成统一 AnswerDecision；stale 指标仍混用 `must_not_return_refs`。 |
| Claim / Citation | C/D | 生产只生成一条大 Claim，缺少 per-claim Memory/Version/Source 绑定；评测也按句子粗切，容易放大 FP。 |

## 2. 修复前后对比

旧结果来自 `SUIXINJI_LAYER3_RESULT_DIAGNOSIS_AND_REPAIR_PLAN.md` 的 `layer3_20260803_065152`；新结果来自 `eval/results/layer3_full_repair_20260803_1642/layer3_metrics.json` 与 `layer3_summary.md`。

| 指标/能力 | 旧结果 | 最新结果 | 状态 | 说明 |
|---|---:|---:|---|---|
| Cases | 520 | 520 | A | 规模一致。 |
| Answer 错误 | 289 | 0 | A | 旧诊断第 2.1 节记录 289 个 RateLimit；最新 summary 记录 Answer calls with errors=0。 |
| RateLimit 错误率 | 55.58% | 0 | A | 最新 `answer_error_types={}`。 |
| Sensitive Access Violations | 15 | 0 | A | 最新 access report 为 0；生产检索有 ACL。 |
| Cross-space Violations | 0 | 0 | A | 最新 access report 为 0。 |
| Business State Mutation | 0 | 0 | A | 最新 access report 为 0；评测 read-only 排除 access_count/last_accessed_at。 |
| Overall Recall@1 | 0.4915 | 0.4915 | E | 总体基本未变，因为列表/历史/无答案仍未统一评分。 |
| Current-state Recall@1 | 1.0 | 1.0 | A | 当前状态检索保持满分。 |
| History Hit@1 | 0 | 0 | B/D | 生产 timeline 已读出，但 `retrieved_refs` 没有 version logical refs。 |
| Semantic Recall@1 | 未单列 | 0.6 | C | typo/noise 命中后回答仍失败；paraphrase 召回也有问题。 |
| Multi-memory Recall@1 | 未单列 | 0.0833 | C/D | 结构化列表不进入 retrieval；复杂历史召回干扰项。 |
| No-answer F1 | 0.0323 | 0.2162 | B/C | 有进步，但 absent/conflict/stale/ambiguous 仍大量失败。 |
| Claim F1 | 0.2991 | 0.4636 | B/C/D | Answer 可用性修复后上升，但 Claim 结构仍错误。 |
| Citation F1 | 0.4314 | 0.7706 | B/C/D | 专用 history citation 很好；列表和 source/version 绑定仍不足。 |
| Latency | 旧 answer P50 受快速限流失败影响 | retrieval P50 359.745ms；answer P50 2308.823ms，P95 26221.477ms | E | 最新可用性提升后 answer 延迟更真实，但需分模板/LLM路径继续拆分。 |

## 3. 调用链审计

当前实际链路：

```text
Query
→ agent.query_agent._deterministic_route
→ _execute_tool / memory_search / memory_history / list_tasks / list_recent_episodes / profile_summary
→ _merge_evidence + _with_sources
→ answer_question 返回字符串
→ answer_question_result 重新做 memory_search / memory_history 诊断
→ AnswerResult(answer_type, selected_memory_ids, selected_version_ids, claims, citations)
→ eval/layer3/CaseRunner.run 再单独调用 memory_search 和 raw hybrid diagnostics
→ db_to_logical 只映射 Memory
→ score_case 只用 retrieved_refs 计算 history/current/stale
```

主要信息丢失点：

1. `answer_question_result` 与 `answer_question` 是两套检索来源。`answer_question_result` 在 `agent/query_agent.py:1935-1944` 先独立搜索 evidence/history_evidence，再调用 `answer_question`，因此 `selected_memory_ids/selected_version_ids` 不一定等于真实回答路径实际使用的工具结果。
2. `CaseRunner.run` 的 `retrieved_refs` 只来自评测前置的 `memory_search`，见 `eval/layer3/run_layer3_eval.py:312` 和 `375-379`；`list_tasks/list_recent_episodes/profile_summary/memory_history` 的结构化结果没有统一进入 `retrieved_refs`。
3. Version 没有完整逻辑映射。Memory 映射只在 `logical_to_db/db_to_logical` 中记录 m-ref，见 `run_layer3_eval.py:195-198`；version v1 使用 `_insert_memory` 自动创建的 `ver_*`，v2/v3 使用 `l3_<run>_<case>_<m>_v<seq>`，见 `run_layer3_eval.py:217-245`，没有 `version_db_to_logical`。
4. `selected_context_refs` 和 `selected_version_refs` 在 520 条 prediction 中全部缺失；评测无法知道最终 context 选择了哪些逻辑 ref。
5. `executed_channels` 是 raw diagnostic 的 exact/structured/FTS/trigram/vector 排名，不包含 answer path 中的 `list_tasks`、`memory_history` 等工具；见 `run_layer3_eval.py:317-331`。
6. Answer type 是后验文本/route heuristic，不是真正基于 EvidenceBundle 的前置决策。代码见 `agent/query_agent.py:1951-1972`。
7. Claim 结构是一条大文本绑定所有 selected memory，见 `agent/query_agent.py:1975-1984`；没有逐事实 source/version 绑定。

## 4. 问题逐项根因

### 4.1 统一检索证据与逻辑 Ref 映射

状态：B 部分修复 + D 评测接线错误。

证据：

- `l3_history_001` 的回答实际列出 v1/v2/v3，`answer_result.selected_version_ids` 有三个版本 id，但 `retrieved_refs=["m1"]`，`relevant_history_refs=["v1","v2","v3"]`，因此 History Hit=0。
- `l3_multianswer_001` 的 `list_tasks` 返回任务并生成答案，但 `retrieved_refs=[]`、`executed_channels=[]`。
- 全量统计：`selected_context_refs` 缺失 520/520，`selected_version_refs` 缺失 520/520。

代码依据：

- Memory logical ref 映射：`eval/layer3/run_layer3_eval.py:195-198`。
- Version seed id 格式不统一：`eval/layer3/run_layer3_eval.py:217-245`。
- `retrieved_refs` 只来自 `memory_search`：`eval/layer3/run_layer3_eval.py:312`、`375-379`。
- AnswerResult 只暴露 DB id，不暴露 logical ref：`agent/answer_models.py:39-62`。

根因：

- 当前有多种结果对象：`memory_search` dict、`MemoryRetrievalHit`、`memory_history` version dict、`list_tasks` memory dict、`list_recent_episodes` memory dict、`profile_summary` slots、`AnswerResult.selected_*`、answer 文本中的 `memory:` citation。
- 它们没有归并为同一事实来源，Evaluator 又只认 `retrieved_refs`，所以结构化工具和 version 被统计丢失。

影响范围：History Hit、Task List/Episodic List retrieval、Citation exact set、Claim source support、stale/irrelevant 指标。

生产问题：是，AnswerResult 不真实承载最终 evidence。  
评测问题：是，Adapter 缺少 version/source/tool 逻辑映射。

### 4.2 Episodic 检索正确但回答为 No-answer

状态：C 尚未修复。

证据：

- coverage tag `episodic` 共 30 条：Recall@1=1.0，但 answer type 全部 `no_answer`，Claim F1=0。
- `l3_current_004`：query “我何时提交论文初稿？”，`retrieved_refs=["m1"]`，Top1 content 为“用户在2026-07-30提交论文初稿”，score=0.9184；最终 answer 为“我没有在随心记里找到足够相关的记录。”，`answer_type=no_answer`。

代码依据：

- 路由未识别“何时 + 事件”为 episodic 快路径，落到 `semantic_search`：`agent/query_agent.py:300-308`。
- AnswerResult 先用 `memory_search(min_score=0.0)` 选到 evidence，再由 `answer_question` 自由生成；如果 answer 文本含拒答词，就覆盖成 `no_answer`：`agent/query_agent.py:1936-1952`。

根因：

- Episodic Memory 进入了检索结果，但最终回答生成链路没有把 episodic 的 `content/current_value/observed_at/valid_from` 转成可支持的 Claim。
- AnswerDecision 不是“证据先行”；它依赖最终文本 `_answer_is_no_answer`，导致“已命中证据但模型/模板拒答”被固化为 no_answer。

影响范围：recent_event、episodic 当前问答、时间问答。

生产问题：是。  
评测问题：否，case 证据显示检索命中但回答失败。

### 4.3 Semantic 检索命中但 Answer 拒答

状态：C 尚未修复。

证据：

- typo 20 条：Recall@1=1.0，但 answer type 全部 `no_answer`。
- noise 20 条：Recall@1=1.0，但 answer type 全部 `no_answer`。
- indirect_reference 20 条：Recall@1=1.0，但 answer type 全部 `qualified_history_only`。
- `l3_semantic_003`：Top1 m1，score=0.9108，最终 `no_answer`。
- `l3_semantic_005`：Top1 m1，score=0.48；Top2 m6，score=0.2494，分差 0.2306；仍 `no_answer`。
- `l3_semantic_004`：“之前说的那个当前重点是什么来着？” 因“之前”进入 `memory_history`，输出历史版本；期望是当前事实回答。

代码依据：

- `_CURRENT_FACT_MARKERS` 只有“住在哪里/住哪/现在住/目前住/正在学习/重点做什么/当前项目”，缺少“当前重点/主要在忙/focus”等表达：`agent/query_agent.py:95-100`、`289-299`。
- `_HISTORY_MARKERS` 包含“之前”，且在 list/current fact 前判断：`agent/query_agent.py:225-231`。
- AnswerResult 以文本拒答判断 answer_type：`agent/query_agent.py:1951-1952`。

根因：

- Planner route 与 AnswerDecision 的语义不一致：retriever 能命中，但 route 没有稳定把“当前重点/间接指代/focus”归入 current fact。
- Relevance gate 过度依赖阈值和最终文本，不基于 Top1 与 Top2 分差、memory_type、query intent 做确定性支持判断。

影响范围：semantic_paraphrase_and_noise、混合语言、错别字、间接指代。

生产问题：是。  
评测问题：否为主；Evaluator 能看到命中与拒答之间的断点。

### 4.4 History 专用数据集与 Complex History Synthesis 未复用同一路径

状态：B/D + C。

证据：

- `history_and_temporal` 100 条全部 `qualified_history_only`，Citation F1=1.0，说明专用 timeline 答案已能读版本并引用来源。
- 但 History Hit@1 仍 0，因为 `retrieved_refs` 只有 m1，没有 v1/v2/v3。
- `l3_multianswer_003`：“总结上下文工程实验从开始到完成的过程。” `observed_route_diagnostic=complex`，没有走 `memory_history`；retrieved_refs 为 `["m6","m1","m5","m4"]`，没有 selected versions，回答还说无法确认具体时间线。

代码依据：

- history route 条件排除了“总结/归纳/多次”等复杂词：`agent/query_agent.py:225`。
- `memory_history` 已有 timeline 能力，调用 `get_memory_timeline` 并展开 versions：`agent/query_agent.py:1066-1093`。
- fast path 中 `memory_history` 使用 `_history_fallback_answer`：`agent/query_agent.py:1613-1615`。

根因：

- 专用 history 和 complex synthesis 是两套路径。复杂历史问题被排除出 timeline，但后续 complex 路径没有补调用 `get_memory_timeline`。

影响范围：history_synthesis、timeline、多 Claim 历史总结。

生产问题：是。  
评测问题：也有，History Hit 对专用 history 统计失败。

### 4.5 Task 列表返回范围失控

状态：C + D。

证据：

- task_list 25 条：retrieved_nonempty=0，但 answer type 全部 `answered`，说明工具真实执行却未进入 retrieval。
- `l3_multianswer_001`：query 要“当前三个项目”，route `list_tasks(limit=30)`；答案返回 6 个任务，其中 m4/m5/m6 是 `must_not_return_refs`，且 `answer_result.selected_memory_ids` 只有 m1/m2，与答案 citation m6/m5/m4/m3/m2 不一致。

代码依据：

- route 固定 `limit=30`，未解析“三个”：`agent/query_agent.py:232-240`。
- `list_tasks` 只按 `status="active"` 和可选 task_status 过滤，再返回 repository 顺序；没有数量解析、主题过滤、identity 去重或业务时间排序：`agent/query_agent.py:1096-1106`。
- inventory 模板最多输出前 10 条：`agent/query_agent.py:1272-1278`。

根因：

- 生产列表能力存在，但缺少通用范围规则：显式数量、状态范围、主题范围、去重、排序。
- Evaluator 只记录独立 `memory_search`，没有把 `list_tasks` 结果映射到 `retrieved_refs`。

影响范围：task_list、profile summary、multi-memory claim precision、citation exact set。

生产问题：是。  
评测问题：是。

### 4.6 Episodic 列表只部分成功

状态：C + D。

证据：

- episodic_list 25 条：retrieved_nonempty=0；answer type 为 `answered` 14 条、`qualified_history_only` 11 条；Claim F1=0.4124，Citation F1=0.5556。
- `l3_multianswer_004`：“列出我最近记录的两件经历” 被 route 到 `memory_history`，返回 task m6 的版本，而不是 episodic m1/m2。

代码依据：

- `_HISTORY_MARKERS` 包含“经历”，且在 `_LIST_MARKERS` 前判断，导致“列出...经历”优先命中 history：`agent/query_agent.py:99`、`225-231`。
- `list_recent_episodes` 只调用 `list_memories(status="active", memory_type="episodic")`，没有业务事件时间字段优先级：`agent/query_agent.py:1109-1114`。

根因：

- 路由优先级错误：list episodic 应优先于 history marker。
- “最近”没有明确使用业务时间。数据集 source 有 `observed_at`，memory content 有事件日期，但当前 list_recent_episodes 不读 source observed_at，也未定义 `valid_from/observed_at/created_at/updated_at` 优先级。
- 结构化工具结果未进入统一 Evidence。

影响范围：recent episodic list、per-claim citation。

生产问题：是。  
评测问题：是。

### 4.7 Absent 场景仍使用无关 Memory

状态：C 尚未修复。

证据：

- absent 35 条：retrieved_nonempty=35；answer type 为 `answered` 14、`no_answer` 21；no-answer recall=0.6857。
- `l3_noanswer_001`：query “我最喜欢的电影是什么？” 返回 m1“用户喜欢咖啡”，答案为“我不知道，但我知道你喜欢咖啡。”，并引用 m1。expected `must_not_return_refs=["m1"]`。

代码依据：

- current preference route 只按 memory_type=preference 搜索，min_score=0.45：`agent/query_agent.py:261-270`。
- `score_case` 将 `must_not_return_refs` 当 stale，而 irrelevant 排除 must_not，导致 `irrelevant_retrieval_rate=0`：`eval/layer3/run_layer3_eval.py:450-451`。
- AnswerResult 如果最终文本未命中 `_answer_is_no_answer` 的短语，就会落为 `answered`：`agent/query_agent.py:1951-1972`。

根因：

- “无相关证据”之前没有 topic/predicate relevance gate。类型对了但属性错了（电影 vs 咖啡）仍进入 Final Context。
- no-answer 后仍允许输出“但我知道...”这种无关事实，并建立 citation。

影响范围：absent、低相关偏好/事实查询、forbidden/stale answer usage。

生产问题：是。  
评测问题：D 也存在，irrelevant 和 stale 口径混淆。

### 4.8 Stale-only 全部变成纯 No-answer

状态：C + D。

证据：

- stale_only 20 条：retrieved_nonempty=0，answer type 全部 `no_answer`，Claim/Citation F1=0。
- `l3_noanswer_041`：输入只有 superseded semantic m1 和 version v1“用户曾居住在新加坡”；期望 `qualified_history_only`，禁止回答“现在住在新加坡”；实际完全拒答。

代码依据：

- current fact route 使用 `memory_search(memory_type="semantic")`，默认不含 inactive/superseded：`agent/query_agent.py:289-299`，`memory_search` 未暴露 include_inactive。
- `search_memories` 默认 `include_inactive=False`，非 inactive 只查 active：`repositories/postgres/memory.py:2910-2921`、`2937-2944`。
- `qualified_history_only` 只在 route 为 `memory_history` 且有 history_evidence 时触发：`agent/query_agent.py:1953-1954`。

根因：

- 当前值查询没有 stale-aware fallback：active 没有命中时，应查询 superseded/version 作为历史证据，并生成 qualified_history_only。
- 为避免 stale answer，当前链路把历史证据整体过滤掉，导致“历史可说，当前不可说”的中间态丢失。

影响范围：stale-only、过期居住地/偏好/任务历史。

生产问题：是。  
评测问题：是，stale_retrieval 口径仍错误使用 must_not。

### 4.9 Conflict 仍武断选择 Active 一侧

状态：C 尚未修复。

证据：

- conflict 20 条：answer type 全部 `answered`，无 `conflict`；citation recall=0.5，只引用 active 一侧。
- `l3_noanswer_061`：输入 m1 active positive，m2 pending_review negative，pending_reviews 包含 pr1；实际 retrieved_refs 只有 m1，答案断言“你是喜欢咖啡的”。

代码依据：

- seed 只写 memories/versions/sources，没有写 `pending_reviews` 对象：`eval/layer3/run_layer3_eval.py:192-246`。
- 生产查询 `memory_search` 默认只查 active，不查 pending_review：`repositories/postgres/memory.py:2915-2921`。
- conflict 判断只看 `all_evidence` 里的 `status=="conflicted"` 或 polarity 多值；pending_review 未进入 evidence，自然无法触发：`agent/query_agent.py:1955-1965`。

根因：

- 评测输入的 pending_reviews 没有持久化/映射。
- 生产 query path 没有 pending/conflict 查询 primitive，Active-only 过滤把冲突另一侧排除。
- AnswerDecision 没有 ConflictContext，无法表达“未解决冲突，不选择任何一边”。

影响范围：pending_review、conflict、偏好/事实冲突。

生产问题：是。  
评测问题：可能有：pending_reviews seed 契约未落库。

### 4.10 Ambiguous 查询未触发 Clarification

状态：C 尚未修复。

证据：

- ambiguous_reference 10 条：answer type 全部 `answered`，no-answer FN=10。
- `l3_noanswer_091`：query “那个评测现在怎么样了？” 同时有 m1 第一阶段评测、m2 第二阶段评测，score 0.5843/0.5921 接近；系统直接列出两个结果，没有澄清。

代码依据：

- clarification 只在 `len(evidence)>=2` 且问题含“哪个/哪一个/哪条/哪种”时触发：`agent/query_agent.py:1966-1968`。
- `score_case` 的 `ambiguous_candidate` 只看 `expected.ambiguous_candidate/requires_clarification` 字段，但数据集此类 case 主要用 `answer_type="clarification"`、`no_answer=true`、`must_not_return_refs` 表达；导致 `ambiguous_candidate_rate=0`：`eval/layer3/run_layer3_eval.py:452`。

根因：

- 生产歧义检测过窄，只识别显式“哪个”，不识别单数模糊指代“那个/这个/它” + 多个同类候选。
- 没有候选 identity/score 接近度比较，也没有 clarification options。
- 评测字段接线对 ambiguous 也不完整。

影响范围：ambiguous_reference、承接问、多个相似任务/项目。

生产问题：是。  
评测问题：是。

### 4.11 Claim 粒度与评分异常

状态：C + D。

证据：

- Overall Claim Precision=0.3502，Recall=0.6855，F1=0.4636。
- `l3_multianswer_001` 的一条 `SupportedClaim.text` 包含 6 个任务，但 `memory_ids` 只绑定 m1/m2；答案 citation 又包含 m6/m5/m4/m3/m2。
- `l3_history_001` 一条 Claim 包含 v1/v2/v3 三个事实，`selected_version_ids` 有值，但 claim 自身 `version_ids=[]`。

代码依据：

- `SupportedClaim` 支持 memory/version/source 字段，但生产只填大段 `claim_text` 和 `memory_ids=selected`：`agent/answer_models.py:13-18`、`agent/query_agent.py:1975-1984`。
- Evaluator 按标点/换行切 `predicted_sentences` 计算 FP：`eval/layer3/run_layer3_eval.py:471-472`。

根因：

- 生产 Claim 没有遵守“一个可独立验证的事实 = 一条 Claim”。
- Claim 与 citation 没有逐条绑定，版本/source 关系缺失。
- Evaluator 的句子粗切会放大格式标题、版本列表、解释性文本的 FP，但主因仍是生产结构未提供可评分 Claim。

影响范围：Claim Precision、Citation per claim、History、多任务列表。

生产问题：是。  
评测问题：是。

### 4.12 Stale 指标仍可能统计错误

状态：D 评测统计错误 + C 生产 stale 处理不足。

证据：

- `score_case` 直接 `stale_retrieved = bool(set(retrieved) & must_not)`，并把同一个布尔值作为 `must_not_return_violation`：`eval/layer3/run_layer3_eval.py:450`、`498-499`。
- `irrelevant_retrieved = bool(set(retrieved) - relevant - must_not)`，因此只要数据集把无关项放进 `must_not_return_refs`，irrelevant rate 就会被压成 0：`run_layer3_eval.py:451`。
- 最新 overall `irrelevant_retrieval_rate=0.0`，但 `l3_noanswer_001` 明确返回无关咖啡 Memory；`l3_multianswer_001` 返回 m4/m5/m6 干扰任务。

根因：

- `must_not_return_refs` 同时承载 stale、irrelevant、sensitive、ambiguous candidate，当前指标把它们全叫 stale。
- 真实 stale 应基于 memory status、valid_until、query_time、version-as-current-use 判断，而不是 gold 的 must_not 集合。

影响范围：stale_retrieval_rate、irrelevant_retrieval_rate、ambiguous_candidate_rate、must_not_return_violation、failed_cases 输出。

生产问题：是，stale-only 业务处理缺失。  
评测问题：是，指标口径需要拆分。

## 5. 强制问题回答

1. History Hit=0 是生产功能失败、Evaluator 接线失败，还是两者都有？  
   两者都有。专用 history 查询里生产已能读 timeline，但 Evaluator 未把 version 映射为 v1/v2/v3；complex history synthesis 仍是生产功能失败。

2. Task List 已经真实执行了吗？为什么检索指标仍为0？  
   已执行。`l3_multianswer_001` 生成了列表答案；检索指标为 0 是因为 Evaluator 的 `retrieved_refs` 只来自独立 `memory_search`，没有接入 `list_tasks` 结果。

3. Episodic 检索命中后为什么 Answer Decision 仍拒答？  
   因为 AnswerDecision 不是基于已选 evidence 的前置判断，而是调用 `answer_question` 后用文本拒答词反推 `no_answer`；episodic 事实未被转换成 SupportedClaim。

4. Semantic 命中后为什么仍输出 No-answer？  
   route 对“当前重点/focus/间接指代”覆盖不足，且 relevance/decision 依赖阈值和最终文本；Top1 命中没有形成强制可答决策。

5. History Synthesis 为什么没有复用 Timeline 能力？  
   `_deterministic_route` 在 history marker 命中时排除了“总结/归纳/多次”等 complex marker，后续 complex path 没有补调 `memory_history`。

6. Conflict 信息具体在哪一步丢失？  
   至少两步：评测 seed 未落 `pending_reviews`；生产查询默认只查 active，pending_review 不进入 `all_evidence`，AnswerDecision 无法看到 polarity conflict。

7. Stale-only 为什么没有进入 qualified_history_only？  
   当前 fact route 只查 active memory，superseded/version 被过滤；`qualified_history_only` 只由 `memory_history` route 触发，current query 没有 stale-aware history fallback。

8. Ambiguous 为什么没有触发 Clarification？  
   clarification 仅识别“哪个/哪一个/哪条/哪种”；“那个评测”这类单数模糊指代 + 多候选未被识别。

9. Task 列表为什么返回全部干扰项？  
   `list_tasks` 只按 active task 查询，route 固定 limit=30；未解析“三个”，未按 topic/current_value/source authority 去重，也未过滤同 topic 老描述。

10. Claim Precision 低主要来自生产 Claim 结构还是 Matcher？  
    主要来自生产 Claim 结构：一条大 Claim 绑定多个事实且 source/version 缺失；Matcher 的粗切句会放大问题，是次因。

11. 当前 Stale Rate 是否仍然错误使用 `must_not_return_refs`？  
    是。`score_case` 直接用 `retrieved ∩ must_not` 作为 stale retrieval。

12. 哪些修复只需要改 Evaluator，哪些必须改生产代码？  
    只改 Evaluator：version/source/tool logical ref 映射、`selected_context_refs` 接线、stale/irrelevant/ambiguous 指标拆分。必须改生产：AnswerDecision、evidence bundle、episodic/semantic 可答、history synthesis、list 范围、stale-only、conflict、clarification、claim/citation 结构。

13. 是否存在为了本次数据集写死关键词、数量或对象名的实现？  
    未发现直接读取 gold 或 case_id 的生产逻辑；但当前 route 本身是关键词驱动且覆盖面窄。下一轮修复不能写死“随心记评测/上下文工程实验/三个”等对象名或固定数量。

14. 最新修复是否有逻辑只存在于 Eval Adapter，没有进入生产入口？  
    有 Adapter 侧规避/诊断逻辑：评测使用独立 user_id 避免全局 quota 干扰，raw channel diagnostics 不影响 answer path；但敏感 ACL 是生产仓储层已有，不只是 Adapter。

15. 下一轮修复完成后，应先跑哪些小集，再跑 520 条全量？  
    先跑接线 smoke：history version、list_tasks、episodic_list、selected_context、citation；再跑 AnswerDecision 小集：answered/no_answer/qualified_history_only/conflict/clarification/restricted；再分桶跑 episodic、semantic、history、history_synthesis、task_list、episodic_list、absent、stale、conflict、ambiguous；最后跑 520 全量和 Layer1/Layer2/Redis smoke。

## 6. 证据索引

结果文件：

- `eval/results/layer3_full_repair_20260803_1642/layer3_summary.md`
- `eval/results/layer3_full_repair_20260803_1642/layer3_metrics.json`
- `eval/results/layer3_full_repair_20260803_1642/layer3_predictions.jsonl`
- `eval/results/layer3_full_repair_20260803_1642/layer3_access_control_report.json`
- `eval/results/layer3_full_repair_20260803_1642/layer3_no_answer_report.json`
- `eval/results/layer3_full_repair_20260803_1642/layer3_latency_report.json`

代表 cases：

- `l3_current_004`：episodic Top1 命中但 no_answer。
- `l3_history_001`：timeline 答案正确但 History Hit=0。
- `l3_multianswer_001`：list_tasks 执行但 retrieved_refs 为空，且返回干扰任务。
- `l3_multianswer_003`：history synthesis 没有复用 timeline。
- `l3_multianswer_004`：episodic list 被 history marker 抢路由。
- `l3_noanswer_001`：absent 问题引用无关咖啡偏好。
- `l3_noanswer_041`：stale-only 被纯拒答。
- `l3_noanswer_061`：conflict 丢 pending_review 一侧。
- `l3_noanswer_091`：ambiguous 未 clarification。
- `l3_noanswer_081`：sensitive restricted 成功。
- `l3_semantic_003/004/005`：semantic 命中后拒答或误判 history。

代码文件：

- `agent/query_agent.py:_deterministic_route`，约 `184-308`。
- `agent/query_agent.py:memory_history/list_tasks/list_recent_episodes/profile_summary`，约 `1066-1123`。
- `agent/query_agent.py:_execute_tool/_inventory_fallback_answer`，约 `1137-1278`。
- `agent/query_agent.py:_answer_question_impl` fast path，约 `1550-1630`。
- `agent/query_agent.py:answer_question_result`，约 `1917-1989`。
- `agent/answer_models.py`，约 `7-62`。
- `memory/service.py:memory_search`，约 `380-412`。
- `repositories/postgres/memory.py:hybrid_search_memory_hits/search_memories/get_memory_timeline`，约 `2690-3045`。
- `memory/access.py`，约 `33-60`。
- `eval/layer3/run_layer3_eval.py:CaseRunner.seed/run/score_case`，约 `177-506`。

## 7. 仍需最小测试确认的点

- `profile_summary` 在真实 `/ask` 中是否会把 slots 可靠转成答案与 citation；当前证据显示工具存在，但未抽样足够 case。
- 生产数据库中 pending_review 冲突对象真实产生时，是否有 relation/decision 可查询；评测 seed 目前没有写 pending_reviews，所以只能确认 query path 没读到。
- 业务事件时间的真实来源字段：当前数据集有 source `observed_at`，MemoryRecord 是否在生产路径中长期保留该字段，需要下一轮 contract test 明确。
