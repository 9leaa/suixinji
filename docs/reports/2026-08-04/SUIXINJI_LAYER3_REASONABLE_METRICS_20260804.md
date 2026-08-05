# Layer 3 合理指标重算（520 条独立集）

## 指标原则

不再把历史 version、no-answer、restricted、stale-only 与当前事实检索混成一个 Recall。

1. **当前正例检索**：只统计 `answer_type=answered` 且 `evidence_mode=current/mixed` 的 case。
2. **历史版本检索**：只统计 `answer_type=answered` 且 `evidence_mode=history` 的 case，并以生产 `memory_history` 实际返回的 version refs 评分。
3. **安全负例**：no-answer、restricted、stale-only、conflict、clarification 单独以正确决策和泄露/过期使用率评分，不以 Recall 评分。
4. `macro` 是每个问题的 Recall 平均；`micro` 是所有 Gold 证据条目合并后的 Recall。多证据问题必须同时看两者。

结果来自 `eval/results/layer3_retrieval_contract_repair_20260804/` 的 520 条 PostgreSQL 隔离回归。

## 1. 当前正例检索（主检索指标）

有效 case 为 295 条，Gold 当前记忆证据共 420 条。

| 指标 | @1 | @3 | @10 |
|---|---:|---:|---:|
| Macro Recall | 84.46% | 100.00% | 100.00% |
| Micro Recall | 70.24%（295/420） | 100.00%（420/420） | 100.00%（420/420） |
| Macro Precision | 100.00% | 47.46% | 14.24% |
| Hit Rate | 100.00% | 100.00% | 100.00% |
| MRR | 100.00% | — | — |
| nDCG@10 | — | — | 100.00% |

解释：@1 已经能命中每一个问题的至少一条正确当前证据；多证据问题在 @1 无法覆盖全部 Gold，所以 micro Recall 只有 70.24%。@3 已覆盖全部 420 条当前证据。@3 Precision 低于 @1 是固定取 3 个候选的自然结果，并不表示错误回答。

## 2. 直接历史版本检索（主历史指标）

有效 case 为 125 条，Gold version 证据共 342 条。

| 指标 | @1 | @3 | @10 |
|---|---:|---:|---:|
| Macro Recall | 37.73% | 100.00% | 100.00% |
| Micro Recall | 36.55%（125/342） | 100.00%（342/342） | 100.00%（342/342） |
| Macro Precision | 100.00% | 91.20% | 27.36% |
| Hit Rate | 100.00% | 100.00% | 100.00% |
| Version MRR | 100.00% | — | — |

历史 timeline 常需要 2～3 个版本：@1 能找到正确 timeline 的第一条版本，但不能覆盖完整序列；@3 已覆盖全部所需版本。版本没有独立 graded relevance 标注，因此不报告 history nDCG@10。

## 3. 安全负例与回答决策

| 指标 | 有效 case | 结果 |
|---|---:|---:|
| No-answer P/R/F1 | 35 正例 | 100.00% / 100.00% / 100.00% |
| Restricted Accuracy | 15 | 100.00%（15/15） |
| Conflict Accuracy | 20 | 100.00%（20/20） |
| Clarification Accuracy | 10 | 100.00%（10/10） |
| Qualified-history-only Accuracy | 20 | 100.00%（20/20） |
| Stale Answer Usage | 520 | 0.00% |
| must-not-return violation | 520 | 0.00% |
| Access violation | 520 | 0 |

`qualified_history_only` 和 stale-only 的正确目标是“不能据此回答当前事实”，不进入当前正例或直接历史版本 Recall。

## 4. 证据选择与最终回答

| 指标 | 结果 |
|---|---:|
| Selected Context P/R/F1 | 95.82% / 84.69% / 89.91% |
| Claim P/R/F1 | 100.00% / 100.00% / 100.00% |
| Citation P/R/F1 | 100.00% / 100.00% / 100.00% |
| Answer / execution errors | 0 / 0 |

后续最值得继续优化的是 Selected Context Recall 84.69%，而不是再追逐混合后的 raw Recall：当前证据已经能在 Top3 找全，但最终选择层仍未把每条应选 context 都带入 bundle。

## 5. 仅作诊断的旧 Raw 指标

| 指标 | 值 | 不作为主结论的原因 |
|---|---:|---|
| Raw Recall@3 | 73.70% | 混入历史 memory/version 对象错配与安全负例 |
| Raw Recall@10 | 73.70% | 同上；不代表当前正例或历史版本实际能力 |
| Raw MRR | 82.21% | 只奖励首个 raw memory 命中，不能衡量多证据/版本覆盖 |
| Raw nDCG@10 | 68.48% | 历史 Gold 的 graded relevance 与 raw memory ref 不同对象 |

## 样本边界

名义上有 520 条 case，但每个子集的唯一 query 模板只有 6～13 个。因此上述指标足以验证这套受控数据上的实现与安全契约，不足以单独证明真实生产语言分布上的泛化能力。下一轮数据集应增加自然语言改写、跨主题组合、真实歧义、长上下文和真实历史链路。
