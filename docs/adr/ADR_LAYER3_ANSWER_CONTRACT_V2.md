# ADR：Layer 3 Answer Contract v2

- 状态：Accepted
- 日期：2026-08-04
- 范围：Layer 3 查询路径、`AnswerResult`、`EvidenceBundle` 与 Layer 3 Evaluator

## 决定

Layer 3 v1 数据集保持不可变。Layer 3 v2 是独立的数据与评分契约，使用
`suixinji.layer3.retrieval_answer.v2` 标识；生产代码永远不读取 v1/v2 Gold、
`case_id` 或 `m1/v1/s1/pr1` 等评测逻辑引用。

Answer Type 固定为：

```text
answered | no_answer | qualified_history_only | conflict |
clarification | restricted | system_error
```

回答模式由 `evidence_mode=current|history|mixed|none` 和 `reason_code` 补充。
直接历史回答是 `answered + history + history_query`；只有“询问当前而仅有历史
证据”才是 `qualified_history_only`。

受限记录采用 access-denied marker 契约。它只能暴露：

```text
kind=access_denied
reason=insufficient_permission
resource_type=memory
```

它不得暴露敏感内容、当前值、来源文本或真实敏感 memory id。Conflict 使用真实
pending-review 持久化语义，不能被降级成 no-answer 或 qualified history。

## Claim 契约

生产继续输出每个可验证事实一条 `SupportedClaim`（原子 Claim），其中必须有
`memory_ids`、`version_ids`、`source_ids` 和 `support_role`。历史时间线另增加
`ClaimGroup(group_type=timeline)`：它包含顺序化原子成员和一个 Summary Claim，
并绑定整条时间线的 version/source 集合。评分器优先按 Group 比较顺序、状态、
version 与 source；Summary 文本仅辅助匹配。非事实响应（no_answer、restricted、
conflict、clarification）不得产生事实 Claim。

## 三层评测

1. Raw Candidate：仅诊断通道召回和噪声，不判回答失败。
2. Selected Context：检查真正暴露的 selected evidence、must-not、stale、access 与工具覆盖。
3. Final Answer：检查 Claim、Citation、No-answer、Conflict、Clarification、Restricted 和安全语义。

Evaluator 只能映射生产已暴露的真实 ID。若 selected context/tool evidence 未暴露，
必须报告 `unavailable`，不得由答案文本、Gold、路由推断或额外查询倒推。

## 兼容性与回滚

`answer_question -> str` 保持不变，结构化调用使用 `answer_question_result`。v2 字段只增不删。
后续生产行为开关分别为：

```text
SUIXINJI_LAYER3_CONTRACT_V2_ENABLED
SUIXINJI_QUERY_TIMELINE_CLAIM_GROUP_ENABLED
SUIXINJI_QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED
SUIXINJI_QUERY_MIXED_LANGUAGE_REWRITE_ENABLED
SUIXINJI_QUERY_PROFILE_SOURCE_STRICT_ENABLED
SUIXINJI_QUERY_EVIDENCE_V2_ENABLED
```

关闭任一生产开关只回退 Query 行为，不删除 v2 契约、评测数据或用户 Memory 数据。
