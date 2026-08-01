# Layer 1 评测口径

本口径用于 `should_store_basic`、`single_candidate_clean`、`key_fields_and_status` 以及后续扩展数据集。评测只读数据集和抽取结果，不写入长期记忆。

## 阶段

1. `raw_llm`：模型返回的 JSON 候选。
2. `schema_validated`：证据连续性、类型和字段契约校验后的候选。
3. `normalized`：确定性字段归一化后的候选；canonical topic、memory key、polarity 不接受模型直接决定。
4. `final_candidate`：准入/安全校验后的最终候选。若候选因敏感信息或 `should_store=false` 被拒绝，按 Gold 候选的 FN 计入。

报告至少同时展示 raw→schema、schema→normalized、normalized→final 的数量变化，以及最终候选指标。

## 候选匹配

Gold 与预测候选按一对一匹配：先匹配 `evidence_span` 完全相同，再匹配 `memory_type + canonical_topic`，再匹配 `memory_type + memory_key`；仍无法匹配时按 `memory_type` 和原文位置的稳定顺序配对。未匹配 Gold 是 FN，未匹配预测是 FP。

## 指标

- Should-store F1：`TP/(TP+FP)` 与 `TP/(TP+FN)` 的调和平均。只在有 Gold should-store 标签的数据集上统计。
- Candidate F1：一对一候选匹配的 F1。
- Memory Type Macro-F1：四类 `preference/task/semantic/episodic` 的 F1 算术平均；没有该类 Gold 的数据集仍保留 0，避免被多数类掩盖。
- Field Accuracy：在全部 Gold 候选上逐字段比较；未匹配预测的字段全部算错，`null == null` 算对。
- Key-field Accuracy：`entity、attribute、operation、canonical_topic、task_status、old_value、new_value` 七个字段的正确比较数 ÷（Gold 候选数 × 7）。这是召回敏感口径，不只统计成功匹配的候选。
- All-fields Exact：11 个字段（再加 `valid_from、valid_until、polarity、memory_key`）全部正确的 Gold 候选数 ÷ Gold 候选数。
- Per-type Key-field Accuracy：在每个 Memory 类型内使用同一分母定义计算，便于定位类型契约问题。

空值策略：只有 Gold 和预测都为 `null` 时相等；空字符串先规范成 `null`。日期按 ISO 字符串比较，比较前去掉时区格式差异以外的文本空白。

## 错误样本

每个字段输出一个 JSONL，保留 `case_id、原文、Gold、预测、memory_type、evidence_span、raw_llm_output`。敏感内容不得写入报告；测试数据若含凭据应先脱敏并计入安全拒绝。
