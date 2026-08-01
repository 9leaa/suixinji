# Layer 1 Before / After

旧报告（2026-08-01 16:31）使用的是旧的候选计数/匹配口径，记录为参考：

| 数据集 | 旧 Should-store F1 | 旧 Candidate F1 | 旧 Type Macro-F1 | 旧 Key-field |
|---|---:|---:|---:|---:|
| should_store_basic | 89.60% | 87.50% | 79.26% | 57.14% |
| single_candidate_clean | 94.74% | 94.43% | 83.14% | 51.34% |
| key_fields_and_status | 95.95% | 93.00% | 86.05% | 55.79% |

修复后规则回归使用统一的一对一匹配和召回敏感字段分母，不能与旧数字直接做百分点比较。旧数字来自 strict hybrid LLM，修复后数字来自 rules；要做严格 before/after，必须在同一 LLM、同一数据集、同一口径下重跑。

本次修复的可验证变化是契约和行为：偏好主题不再落成“饮品偏好”等泛标签；任务 key 不随状态变化；semantic fact 不自动合并；用户画像只保留最新任务状态；错误日志有稳定 retry category。
