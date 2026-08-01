# Layer 1 阶段对比

## 当前实现

| 阶段 | 责任 | 是否允许模型决定身份/状态 |
|---|---|---|
| raw_llm | 接收 JSON 候选 | 允许提出，不能直接落库 |
| schema_validated | 校验类型、证据连续性、跨类型字段 | 不允许；不适配字段被清空/归一化 |
| normalized | 根据证据重算主题、槽位、任务身份、key、polarity | 不允许模型覆盖 |
| final_candidate | 敏感信息、should_store、关系裁决和状态演化 | 只能由确定性规则决定 |

当前仓库已具备三套冻结集：`should_store_basic`、`single_candidate_clean`、`key_fields_and_status`。方案要求的 `multi_candidate` 和 `hard_language_and_noise` 压缩包中不存在，因此没有伪造阶段指标。

## 现有规则回归（新统一口径）

| 数据集 | Cases | Should-store F1 | Candidate F1 | Type Macro-F1 | Key-field Accuracy | All-fields Exact |
|---|---:|---:|---:|---:|---:|---:|
| should_store_basic | 120 | 74.58% | 62.90% | 62.38% | 47.62% | 10.00% |
| single_candidate_clean | 160 | 79.70% | 68.59% | 68.12% | 45.89% | 13.12% |
| key_fields_and_status | 180 | 81.19% | 70.55% | 69.59% | 49.52% | 24.44% |

这些是规则候选的回归基线，不是 LLM 结果；运行 LLM 版本时使用同一匹配和字段分母。
