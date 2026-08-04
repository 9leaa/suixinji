# 随心记 Layer 3 下一轮修复实施计划（新版）

> 计划依据：`LAYER3_PRE_REPAIR_ROOT_CAUSE_ANALYSIS.md`、`CODEX_LAYER3_PRE_REPAIR_ANALYSIS_TASK.md`、最新结果 `eval/results/layer3_full_repair_20260803_1642`。  
> 本文件是实施计划，不代表本轮已修改生产代码。  
> 关键原则：每个 Stage 独立提交、独立运行小集；全部小集通过后，才允许跑 520 条全量。

## 1. 修改顺序

按依赖关系排序：

```text
Stage 0 指标拆分与 Evaluator 自测，冻结可信基线
→ Stage 1 统一 Evidence 数据结构与评测接线
→ Stage 2 Semantic Retrieval 专项
→ Stage 3 AnswerDecision 前置化
→ Stage 4 History / Complex / Stale-only 复用 Timeline
→ Stage 5 List / Profile 范围控制
→ Stage 6 Conflict / Clarification
→ Stage 7 Claim / Citation 结构化
→ Stage 8 分桶回归、全量 520、Layer1/Layer2/Worker 回归
```

原因：

- 先做 Stage 0，是为了把 `stale / irrelevant / access / ambiguous / must_not` 指标拆开，并用少量固定 case 证明 Evaluator 能正确看见 memory、version、source、structured tool evidence。否则后续生产修复无法判断是真修好，还是统计口径仍错。
- Stage 1 统一 Evidence，但生产逻辑只能依赖真实 DB id、业务字段、score、rank、status、source、version、access policy；`m1/v1/s1` 这类 `logical_ref` 只能作为可选评测元数据，不得进入生产决策。
- Semantic Retrieval 单独成 Stage 2，因为当前 semantic 既有“召回缺失”也有“命中后拒答”，必须先确认 vector seed、embedding、fusion 和阈值是否可信，再改 AnswerDecision。

## 2. 全局约束

- 不修改数据集 zip/jsonl。
- 不修改 gold expected 字段。
- 不碰用户 PostgreSQL 既有数据。
- 不为 Layer3 case_id、对象名、`m1/m2/m3`、`v1/v2/v3`、固定“三个项目”等写死逻辑。
- 不通过读取 gold、降低阈值、扩大 top_k 掩盖 AnswerDecision 或检索问题。
- 每个 Stage 独立提交；每个 Stage 只跑对应小集；全部小集通过后再跑 520 全量。
- 如果某个 Stage 小集失败，停在该 Stage 修复，不顺手扩大范围。

## 3. Stage 0：指标拆分与 Evaluator 自测，冻结可信基线

目标：先让评测结果可信。Stage 0 只改 Evaluator / 测试辅助，不改生产查询逻辑。

最小修改范围：

- 修改 `eval/layer3/run_layer3_eval.py:score_case`
  - 保留 `must_not_return_violation`：表示 gold 禁止集合命中。
  - 新增或修正：
    - `stale_retrieval_violation`：基于实际对象状态、`valid_until`、`query_time`、version role 判断。
    - `stale_answer_usage`：历史/过期 evidence 是否被当作当前值回答。
    - `irrelevant_retrieval`：普通无关干扰项命中，不再被 `must_not_return_refs` 吞掉。
    - `access_control_violation`：敏感/owner-only 越权。
    - `ambiguous_candidate_usage`：clarification case 中擅自使用候选直接回答。
    - `restricted_answer_rate`：restricted 是否被安全拒答或安全 marker 处理。
    - `tool_result_coverage`：结构化工具结果是否进入可评分 refs。
- 修改 `eval/layer3/run_layer3_eval.py:CaseRunner.seed/run`
  - 建立 Memory / Version / Source / PendingReview 的评测逻辑映射。
  - 明确输出：
    - `retrieved_refs`
    - `selected_context_refs`
    - `selected_version_refs`
    - `selected_source_refs`
    - `executed_tools`
    - `executed_channels`
    - `raw_channel_hits`
  - `logical_ref` 只写入 prediction/report，作为评测元数据；不得回传给生产代码。
- 添加 Evaluator 自测。
  - 自测输入应是手工构造的 prediction dict，不需要启动生产查询。
  - 覆盖：
    - history version ref 映射。
    - list tool ref 映射。
    - stale vs irrelevant 拆分。
    - restricted 安全统计。
    - ambiguous candidate 统计。

不修改：

- 不改 `agent/query_agent.py`。
- 不改 `memory/service.py`。
- 不改 repository。
- 不跑 520 全量。

小集 / 自测：

- scorer 单测：
  - movie query 命中 coffee：应是 `irrelevant_retrieval`，不是 stale。
  - superseded location 作为历史：应是 stale/history evidence，不等同普通 irrelevant。
  - sensitive restricted：应统计 restricted，不泄露内容。
  - ambiguous candidates：应统计 ambiguous candidate usage。
  - ordinary must_not distractor：只计 must_not，按对象状态决定是否 stale。
- Adapter smoke：
  - `l3_history_001`
  - `l3_multianswer_001`
  - `l3_multianswer_004`
  - `l3_noanswer_081`

验收：

- `irrelevant_retrieval_rate` 不再长期为 0。
- History case 能在 prediction 中看见 selected version refs。
- list_tasks/list_recent_episodes 的工具结果能被 Evaluator 看见。
- restricted case 不需要真实敏感文本进入 prediction。

回滚点：

- 新旧指标并行输出一轮；旧字段不删。

## 4. Stage 1：统一 Evidence 数据结构与评测接线

目标：让普通 memory、version、source、structured tool result、pending/conflict diagnostic 都能进入同一个 EvidenceBundle，供 AnswerDecision 和 Evaluator 使用。

最小修改范围：

- 修改 `agent/answer_models.py`
  - 新增或扩展 `RetrievalEvidence` / `EvidenceBundle`：
    - `kind`: `memory|version|source|tool_result|pending_review|access_denied`
    - `id`: 真实 DB id 或稳定生产 id。
    - `memory_id`
    - `version_id`
    - `source_ids`
    - `memory_type`
    - `status`
    - `task_status`
    - `score`
    - `rank`
    - `channel`
    - `tool`
    - `selected`
    - `role`: `current|history|stale_history|conflict|candidate|restricted|access_denied`
    - `logical_ref`: 可选，仅 Evaluator 填充或保留；生产逻辑不得读取该字段做判断。
- 修改 `agent/query_agent.py`
  - 所有工具执行后，把结果 normalize 成 EvidenceBundle。
  - `answer_question_result` 不再重新猜测一套 evidence，而是复用实际 answer path 的 selected evidence。
  - 如果 production path 没有 Evaluator logical_ref，也必须能正常工作。
- 修改 `eval/layer3/run_layer3_eval.py`
  - 将 `list_tasks/list_recent_episodes/profile_summary/memory_history` 的 evidence 映射进可评分 refs。
  - 仅在 Adapter 层把 DB id 映射为 `m1/v1/s1/pr1`。

不修改：

- 不改数据集 gold。
- 不让生产读取 logical ref。
- 不改 repository 核心排序权重。

兼容风险：

- `AnswerResult.to_dict()` schema 增字段，旧调用方应忽略新增字段。
- 需要保留旧 `selected_memory_ids/selected_version_ids/selected_source_ids`。

小集：

- `l3_history_001`：应输出 version evidence。
- `l3_multianswer_001`：应输出 list task evidence。
- `l3_multianswer_004`：应输出 episodic list evidence。
- `l3_noanswer_081`：restricted evidence 不泄露内容。

验收：

- `selected_context_refs` 不再 520/520 缺失。
- `selected_version_refs` 能覆盖 history 专用 case。
- structured tools 有 `executed_tools`，不再只依赖 raw hybrid `executed_channels`。

回滚点：

- feature flag：`SUIXINJI_QUERY_EVIDENCE_BUNDLE_ENABLED`。

## 5. Stage 2：Semantic Retrieval 专项

目标：独立确认 semantic/paraphrase/noise 的召回链路是否可靠，再处理命中后的回答问题。

检查范围：

- 数据 Seed：
  - `memory_vectors` 是否为 Layer3 seeded memories 正确创建。
  - vector `model/dimension/embedding_version/status` 是否与当前 `current_embedding_contract()` 一致。
  - query embedding 和 memory embedding 是否使用同一 normalize 策略。
- 原始相似度：
  - 对 semantic/paraphrase/noise 小集输出 raw cosine distance / similarity。
  - 记录 Top1/Top2 分差，不只看最终 fused score。
- 阈值：
  - 检查 `SUIXINJI_QUERY_MIN_SCORE=0.55` 与 `SUIXINJI_MEMORY_QUERY_MIN_SCORE=0.45` 对 semantic cases 的影响。
  - 区分“检索未达阈值”与“进入 retrieval 但 AnswerDecision 拒答”。
- Fusion 接线：
  - 检查 `hybrid_search_memory_hits` 中 exact/structured/fts/trigram/vector 是否都参与 RRF。
  - Evaluator raw diagnostic 不能因为 `query_embedding=None` 误判 vector 未执行；需要记录 production `memory_search` 真实是否执行 vector。
- 跨语言与 Query Rewrite：
  - 检查 mixed-language query（如 `focus`）是否进入 query rewrite 或 synonym expansion。
  - 检查 indirect_reference 是否被 history marker 抢路由。
  - 检查 typo/noise 是否主要由 trigram/structured 命中，还是 vector 真命中。

最小修改范围：

- 修改 `eval/layer3/run_layer3_eval.py`
  - semantic diagnostic 输出 raw vector/fusion channel 信息。
- 修改 `repositories/postgres/memory.py` 或 diagnostic helper
  - 暴露可选 debug 字段，不改变生产排序行为。
- 如确认 seed 缺 vector：
  - 修 Layer3 seed 或测试准备逻辑，确保 memory_vectors 符合真实生产契约。
- 如确认 query rewrite 缺失：
  - 后续再在 `agent/query_agent.py` 增加 query rewrite / synonym expansion，但本 Stage 先以诊断和最小修复为主。

不修改：

- 不为 semantic 小集写死关键词。
- 不用扩大 top_k 掩盖 vector/threshold 接线错误。
- 不把 answer no_answer 问题归咎于 retriever，必须用 evidence 证明。

小集：

- `l3_semantic_003`
- `l3_semantic_004`
- `l3_semantic_005`
- semantic coverage tag：
  - typo
  - noise
  - mixed_language
  - indirect_reference
  - paraphrase

验收：

- 每条 semantic diagnostic 能说明：
  - memory vector 是否存在。
  - query vector 是否生成。
  - vector model/dim/version 是否匹配。
  - raw vector rank / score。
  - fusion 后 rank / score。
  - answer path 是否使用了该 evidence。
- `l3_semantic_003/005` 这类 Top1 命中但拒答 case 被明确标记为 AnswerDecision 问题。
- `l3_semantic_004` 这类 indirect 当前事实被明确标记为 route/history marker 问题。

回滚点：

- diagnostic 字段只追加，不改变旧检索输出。

## 6. Stage 3：AnswerDecision 前置化

目标：先基于 EvidenceBundle 决定 answer_type，再生成答案；LLM 只负责表达，不负责决定是否可答。

最小修改范围：

- 修改 `agent/query_agent.py`
  - 新增 `decide_answer(question, route, evidence_bundle, access_context, query_time)`。
  - 决策类型：
    - `answered`
    - `no_answer`
    - `qualified_history_only`
    - `conflict`
    - `clarification`
    - `restricted`
    - `system_error`
  - 规则：
    - `selected current evidence` 且 relevance 合格 → `answered`。
    - 用户直接询问历史，且有 history/version evidence → `answered`，不是 `qualified_history_only`。
    - 用户询问当前，但只有 stale/history evidence → `qualified_history_only`。
    - 无 relevant evidence → `no_answer`，清空 selected context，不允许“但我知道...”。
    - unresolved conflict/pending evidence → `conflict`。
    - 多个近似 identity + 单数模糊指代 → `clarification`。
    - ACL 拒绝或 access-denied marker → `restricted` 或按安全策略统一拒答。
- 修改 answer 生成：
  - simple fact/list/history/conflict/clarification/restricted 用模板。
  - 复杂多 claim 可调用 LLM，但输入只允许 selected claims/evidence。

Restricted 安全契约：

- Repository 不得向上层返回敏感原文再让 Answer 层过滤。
- 二选一：
  - Repository 返回不含敏感内容的 `access_denied` marker，字段只包含对象类型、权限原因、安全可公开元数据。
  - 或 Repository 直接按安全策略不返回 evidence，由 AnswerDecision 根据安全 diagnostic 统一拒答。
- prediction/report 不得保存敏感内容。
- citation 不得引用敏感 source 内容。

不修改：

- 不调整底层 RRF 参数。
- 不把 case_id、对象名、gold refs 传入生产。

兼容风险：

- `/ask` 文案可能变化，但 answer_type 更稳定。
- `no_answer` 更严格后，弱相关旧问法可能拒答，需要日志观察。

小集：

- answered：`l3_current_001`、`l3_current_004`
- no_answer：`l3_noanswer_001`
- direct history answered：`l3_history_001`
- qualified_history_only：`l3_noanswer_041`
- conflict：`l3_noanswer_061`
- clarification：`l3_noanswer_091`
- restricted：`l3_noanswer_081`
- semantic hit：`l3_semantic_003/005`

验收：

- `l3_current_004` 从 no_answer 变为 answered。
- `l3_semantic_003/005` 命中后变为 answered。
- `l3_history_001` 为 answered，不再被误归为 qualified_history_only。
- `l3_noanswer_041` 为 qualified_history_only。
- `l3_noanswer_001` 为 no_answer，且无 citation。
- restricted case 不泄露敏感值。

回滚点：

- feature flag：`SUIXINJI_QUERY_STRUCTURED_DECISION_ENABLED`。

## 7. Stage 4：History、Complex History Synthesis、Stale-only 复用 Timeline

目标：只保留一个 timeline 能力入口，专用 history、复杂历史总结、stale-only 都复用它，但 answer_type 要区分“直接问历史”和“问当前但只有历史”。

最小修改范围：

- 修改 `agent/query_agent.py:_deterministic_route`
  - 将 history/list route 优先级拆开：
    - list episodic/list task 优先于“经历/记录” history marker。
    - “总结/归纳/从开始到完成/过程/变化” + 单一任务/主题 → `history_synthesis`，底层调用 `memory_history`。
  - “之前说的那个当前重点是什么”这类当前事实问句不能仅因“之前”进入 history。
- 修改 `agent/query_agent.py:memory_history`
  - 输出 normalized version evidence。
  - 返回每个 version 的 source 支持字段。
  - 不要求生产填 `logical_ref`。
- 修改 stale fallback：
  - current query active miss 后，按 topic/key 查 `include_inactive=True` 或 `get_memory_timeline`。
  - 若只发现 superseded/version：answer_type=`qualified_history_only`。
  - 禁止把 stale content 当 current claim。
- 如 repository 需要：
  - 扩展 `repositories/postgres/memory.py:get_memory_timeline` 支持 canonical topic / memory_key / predicate 查询。

Answer type 契约：

- 用户直接问“历史/变化/经历了哪些状态/从开始到完成”：
  - 有 version/history evidence → `answered`。
  - 答案可以说明历史 timeline。
- 用户问“现在/当前”：
  - 有 active current evidence → `answered`。
  - 没有 active current evidence，但有 superseded/version/history evidence → `qualified_history_only`。
  - 没有任何相关证据 → `no_answer`。

不修改：

- 不新建第三套 history 搜索。
- 不用 gold `version_refs` 反查。

兼容风险：

- include_inactive fallback 不能默认进入所有回答，否则 stale answer risk 上升。
- 必须标记 role=`stale_history`，AnswerDecision 只能生成 qualified claim。

小集：

- `l3_history_001`
- `l3_multianswer_003`
- `l3_noanswer_041`
- `l3_semantic_004`

验收：

- 直接历史问题为 answered，Version evidence 可评分。
- history_synthesis 不再召回 m4/m5/m6 干扰作为核心证据。
- stale-only Claim/Citation 命中历史 source，`stale_answer_usage=0`。

回滚点：

- `SUIXINJI_QUERY_STALE_HISTORY_FALLBACK_ENABLED` 独立开关。

## 8. Stage 5 实施前 List Contract Audit

在不读取 Gold 排除字段、也不让生产逻辑依赖 `logical_ref` 的前提下，先审计
`l3_multianswer_001` 的业务字段。下表中的 m1–m6 仅是评测报告中的可读标签，
生产排序使用 `scope.canonical_topic`、`task_status`、`object_value/current_value`、
source 关系与业务时间。

| 评测标签 | Topic | Status | 业务时间 | Source | Identity | 信息完整度 |
|---|---|---|---|---|---|---|
| m1 | 随心记评测 | todo | 2026-08-02 | 用户来源 s1 | canonical_topic=随心记评测 | content + current state + source |
| m2 | 检索质量优化 | blocked | 2026-08-01 | 用户来源 s2 | canonical_topic=检索质量优化 | content + current state + source |
| m3 | 上下文工程实验 | done | 2026-07-31 | 用户来源 s3 | canonical_topic=上下文工程实验 | content + current state + source |
| m4 | 论文发言稿 | todo | 2026-07-10 | 来源标记为无关 s4 | canonical_topic=论文发言稿 | 缺少 current state，且来源无关 |
| m5 | 思维导图连线修复 | todo | 2026-07-09 | 来源标记为无关 s5 | canonical_topic=思维导图连线修复 | 缺少 current state，且来源无关 |
| m6 | 随心记评测 | todo | 2026-07-08 | 来源标记为无关 s6 | 与 m1 同 canonical_topic | 缺少 current state，且为较弱重复 |

通用排序因此先保留具有明确 current state、source 支撑完整的记录，再按
`canonical_topic` 去重，最后按业务时间和稳定 ID 排序，能够选择前三个业务项目；
没有读取 Gold，也没有按 m1/m2/m3 或 case id 写特判。如果实际生产字段无法提供这些
区分，必须暂停并修复数据契约，不能用隐藏特判通过评测。

### Stage 5：List / Profile 范围控制

目标：结构化列表按用户表达的数量、状态、主题、时间、去重、排序返回。选择 m1/m2/m3 必须来自通用业务排序，不得根据 gold 排除 m4/m5/m6。

最小修改范围：

- 修改 `agent/query_agent.py`
  - 新增轻量 `parse_list_constraints(question)`：
    - 显式数量：一/二/两/三/四/五/N 个/项/件。
    - memory_type：task/episodic/profile。
    - status：todo/blocked/done/cancelled/current/all。
    - 时间：最近/今天/本周/明确日期。
    - topic terms。
  - `list_tasks`：
    - 默认 active task。
    - 按 canonical_topic / memory_key 去重。
    - 显式数量控制 limit，但不是硬编码 `3`。
    - 按业务排序选择候选：
      1. 与 query scope 匹配：用户问“当前项目/当前任务状态”时，优先结构化 `current_value` 或明确 `task_status` 的当前状态记录。
      2. Identity 去重：同一 canonical_topic/memory_key 多条记录，只保留当前状态置信度最高的一条。
      3. 证据权威：用户直接来源、明确当前状态表达、source relation 更强的记录优先。
      4. 业务时间：优先更近的业务时间；没有业务时间时再用 updated_at。
      5. 信息完整度：`canonical_topic + task_status/current_value + source` 完整的记录优先于只有泛化“待处理”的记录。
      6. 排序稳定性：同分时按业务时间 desc、updated_at desc、id asc。
    - 对 `l3_multianswer_001`，m1/m2/m3 应因“当前状态结构完整、topic 去重后代表三个当前项目、source 支撑更明确”胜出；m4/m5 是其他无关主题，m6 是同 topic 的旧/泛化重复记录。这个判断必须由上述业务排序产生，不得读取 gold。
  - `list_recent_episodes`：
    - 按事件发生时间排序，优先级：
      1. memory `event_time`。
      2. memory `valid_from`。
      3. content 中明确事件日期。
      4. source `observed_at`。
      5. memory `updated_at`。
      6. memory `created_at`。
    - `observed_at` 只表示记录/观察时间，不能默认等同事件发生时间。
    - 返回 source refs。
  - `profile_summary`：
    - slots 内也产出 evidence，不只产出自然语言。

不修改：

- 不改变 task status 枚举。
- 不写死“当前三个项目”。
- 不根据 `must_not_return_refs` 过滤生产候选。

兼容风险：

- 当前 list_memories 默认排序不明，改排序会影响旧 `/memory list` 风格答案；新排序应只作用于 query list 工具。

小集：

- `l3_multianswer_001`
- `l3_multianswer_004`
- profile summary 抽样 case

验收：

- task_list Claim Precision ≥ 95%，Citation F1 ≥ 95%。
- task_list 不因 gold 过滤，而是由通用排序选择正确 TopN。
- episodic_list 按 event_time/valid_from 优先排序，source citation 正确。
- structured list evidence 进入 selected refs。

回滚点：

- `SUIXINJI_QUERY_LIST_CONSTRAINTS_ENABLED`。

## 9. Stage 6：Conflict 与 Clarification

### 9.1 Conflict

目标：查询当前身份时发现 unresolved pending/conflict，不武断选择 active 一侧。

最小修改范围：

- 修改/新增 repository 查询：
  - 查询同 `memory_key/canonical_topic/entity+attribute` 下 `status in active,pending_review,conflicted` 的候选。
  - 查询 `MemoryDecisionRow.status="pending_review"` 或 relation `conflicts_with`。
- 修改 eval seed：
  - 必须使用项目真实 pending-review 持久化契约。
  - 如果真实契约是 `MemoryDecisionRow(status="pending_review") + result_memory_ids_json + relation/recommended_action`，就按该契约 seed。
  - 如果真实契约还需要 pending memory row、source、decision id、relation rows，也必须完整 seed。
  - 不允许为了评测临时把 `pending_reviews` 伪装为普通 pending Memory。
- 修改 `AnswerDecision`：
  - 若同一 identity 存在 active/pending_review polarity/value 冲突 → `conflict`。
  - `conflict_ids` 填入相关真实 memory/decision/review refs。
  - 不把 pending 对象当普通事实展示，只展示“存在冲突，需要确认”的摘要。

不修改：

- 不自动 approve/reject pending_review。
- 不改变真实用户数据。
- 不新增只服务评测的 pending-review 伪结构。

小集：

- `l3_noanswer_061`
- conflict coverage tag 20 条小集

验收：

- conflict 20 条 answer_type 不再全是 answered。
- forbidden claim “确定喜欢/不喜欢” 为 0。
- pending-review evidence 可追溯到真实持久化对象。

回滚点：

- pending conflict 查询单独 feature flag。

### 9.2 Clarification

目标：单数模糊指代 + 多个同类候选时触发澄清。

最小修改范围：

- 修改 `agent/query_agent.py`
  - 新增 `detect_ambiguous_reference(question, evidence)`：
    - 单数模糊代词：这个/那个/它/该项/那个评测/那个项目等。
    - 多个候选 identity 同类型、score 接近、均可回答。
    - 用户没有要求“都列出/全部/分别”。
  - 输出 `AnswerDecision("clarification", ..., clarification_options=[...])`。
- 修改 Evaluator：
  - 对 `expected.answer_type="clarification"`、`no_answer=true` 的 case 单独统计 clarification，不混入普通 no_answer。

不修改：

- 不针对“那个评测”写对象名特判。

小集：

- `l3_noanswer_091`
- 用户问“把两个评测都列出”的反例 synthetic smoke

验收：

- ambiguous_reference 10 条不再 answered。
- clarification options 有可读候选。
- 用户明确要求全列时不误触发 clarification。

回滚点：

- `SUIXINJI_QUERY_CLARIFICATION_ENABLED`。

## 10. Stage 7：Claim / Citation 结构化

目标：每个可独立验证事实生成一条 Claim，并逐条绑定 Memory/Version/Source。

最小修改范围：

- 修改 `agent/answer_models.py`
  - `SupportedClaim` 增强字段：
    - `text`
    - `claim_type`
    - `memory_ids`
    - `version_ids`
    - `source_ids`
    - `support_role`
    - `confidence`
    - `logical_refs`: 可选评测元数据；生产不得依赖。
- 修改 `agent/query_agent.py`
  - 在 answer generation 前由 selected evidence 构造 claims。
  - 模板答案直接由 claims 渲染。
  - LLM 只接收 claims，不允许引入新事实。
- 修改 citation builder：
  - 引用不再仅 regex `memory:` 文本。
  - 每条 claim 保留 source refs；最终文本展示可仍保持现有“来源”格式。
- 修改 `eval/layer3/run_layer3_eval.py`
  - 优先读取 `answer_result.claims` 评分。
  - 若 structured claims 缺失，再 fallback 到句子切分。

不修改：

- 不把所有历史版本标题当事实 claim。
- 不要求所有复杂答案都完全模板化；复杂表达可 LLM 润色，但事实边界由 claims 决定。

兼容风险：

- 旧 answer 文本格式可能变化；API 调用方若解析文本来源行需兼容。

小集：

- task list：3 claims，分别绑定真实 memory/source。
- direct history：timeline claims 绑定真实 version/source。
- stale-only：qualified claim 绑定 history version/source。
- absent/no_answer/restricted：claims 为空或非事实 claim，不计 unsupported fact。

验收：

- Claim Precision/Recall/F1 ≥ 95% 作为目标。
- Citation F1 ≥ 98% 作为目标。
- 每条事实 claim 都有 source 或明确无 source 的 reason。

回滚点：

- 保留旧 text citations，structured citations 增量启用。

## 11. Stage 8：分桶回归、520 全量与跨层回归

目标：所有小集通过后，才运行全量 520，并做 Layer1/Layer2/Worker smoke。

执行顺序：

1. Stage 0-7 每个 Stage 独立提交。
2. 每个 Stage 只跑对应小集。
3. 全部小集通过后，跑功能分桶：
   - episodic 当前问答
   - semantic typo/noise/indirect/paraphrase/mixed_language
   - history 专用
   - history_synthesis
   - task_list
   - episodic_list
   - absent
   - stale_only
   - conflict
   - ambiguous_reference
   - restricted/access_control
4. 分桶通过后，跑 520 全量。
5. 全量通过后，跑跨层回归：
   - Layer 1 核心回归：抽取、分类、note 落库。
   - Layer 2 PostgreSQL 回归：状态演化、版本、source、pending review。
   - Redis Worker smoke：ingest → memory/enrichment/outbox。
   - 真实 `/ask` smoke：当前事实、历史、列表、无答案、敏感权限。

全量验收门槛：

- Answer Availability：`answer_error_count=0`，system_error 不混入 no_answer。
- Sensitive/Cross-space/Business mutation 全部 0。
- Current State Recall@1 不回归，目标 ≥ 0.98。
- History Hit@3 ≥ 0.95。
- Episodic answered cases Claim F1 ≥ 0.95。
- Semantic 命中后 no_answer FP 接近 0。
- Task List 不返回业务排序下的干扰项。
- Stale-only 进入 qualified_history_only，Stale Answer Usage=0。
- Conflict 进入 conflict，不武断选择。
- Ambiguous 进入 clarification。
- Absent 不引用无关 Memory。
- Claim F1 ≥ 0.95。
- Citation F1 ≥ 0.98。
- stale/irrelevant/access/ambiguous/must_not 指标分别可解释。

## 12. 文件级修改清单

| 文件 | 修改目的 |
|---|---|
| `eval/layer3/run_layer3_eval.py` | Stage 0 指标拆分、逻辑映射、结构化工具接线、semantic diagnostic、pending-review seed。 |
| `agent/answer_models.py` | 扩展 AnswerResult / SupportedClaim / AnswerDecision / Evidence 数据结构。 |
| `agent/query_agent.py` | 统一工具 evidence、前置 AnswerDecision、修路由、semantic query rewrite、列表约束、history/stale/conflict/clarification。 |
| `memory/service.py` | 如有必要，暴露带 role/score/source 的 memory_search 结果；保持旧 API 兼容。 |
| `repositories/postgres/memory.py` | timeline 查询增强、pending/conflict 查询 primitive、source/version hydration、semantic diagnostic。 |
| `tests/` | 新增 Evaluator、Query Agent、Repository Contract、Semantic Retrieval 小集测试。 |

## 13. 回滚策略

- 所有生产行为变化用 feature flag 分段启用：
  - `SUIXINJI_QUERY_EVIDENCE_BUNDLE_ENABLED`
  - `SUIXINJI_QUERY_SEMANTIC_DIAGNOSTIC_ENABLED`
  - `SUIXINJI_QUERY_STRUCTURED_DECISION_ENABLED`
  - `SUIXINJI_QUERY_STALE_HISTORY_FALLBACK_ENABLED`
  - `SUIXINJI_QUERY_LIST_CONSTRAINTS_ENABLED`
  - `SUIXINJI_QUERY_CLARIFICATION_ENABLED`
- Stage 0 Evaluator 指标拆分先新旧并行，不删除旧字段。
- 保留旧 `answer_question` 字符串 API。
- 新增结构化字段不删除旧字段。
- 如果 Layer1/Layer2 回归失败，优先关闭 Query 层 feature flag，不回滚数据结构迁移。

## 14. 强制执行约束

1. Stage 0 只允许修评分器和映射当前已经真实暴露的数据。不得根据答案文本、Gold 或额外补查推断生产实际使用的 Evidence。当前生产没有暴露的 selected context/tool refs 必须标记为 `null` 或 `unavailable`，待 Stage 1 统一 Evidence 后再补齐。

2. Stage 2 不能只做 Semantic 诊断。在进入 Stage 3 前必须设置并达到出口门槛：
   - Vector Seed 完整率 100%
   - Embedding Contract 匹配率 100%
   - Query Embedding 成功率 100%
   - paraphrase Recall@3 ≥ 95%
   - typo/noise Recall@3 ≥ 95%
   - mixed_language Recall@3 ≥ 90%

   未达到时继续在 Stage 2 修复，不允许带着真实召回问题进入 AnswerDecision 阶段。

3. Stage 3 开始前冻结 Restricted 安全 ADR：明确是否允许向请求方暴露“存在受限记录”。Repository、AnswerDecision 和 Evaluator 只能使用一种统一契约。无论采用 `access_denied` marker 还是完全隐藏，都不得向 Answer、日志、Prediction 或 Citation 暴露敏感内容。

4. Answer Type 枚举固定为：
   - `answered`
   - `no_answer`
   - `qualified_history_only`
   - `conflict`
   - `clarification`
   - `restricted`
   - `system_error`

   不新增 `history_answered`。直接历史回答使用 `answer_type=answered`、`evidence_mode=history`、`reason_code=history_query`。

5. Stage 5 实施前先输出 List Contract Audit，列出 `l3_multianswer_001` 中 m1-m6 的 Topic、Status、业务时间、Source、Identity、信息完整度，证明通用业务排序在不读取 Gold 时能够选择正确 TopN。如果现有字段无法区分，不允许通过隐藏特判强行通过数据集。
