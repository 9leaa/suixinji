# 随心记三层独立评测指标汇总（2026-08-04）

## 结论与范围

本文件汇总三次**彼此独立**的最终回归运行，未混用不同层的样本，也未把 Layer 2 的 80 条 version 子集当成全量结果：

| 层 | 最终运行 | 样本数 | 执行入口 / 后端 |
|---|---|---:|---|
| Layer 1：候选提取 | `layer1_stage8_hybrid_20260804` | 730 | Hybrid（规则 + LLM） |
| Layer 2：记忆归并与状态 | `layer2_stage8_postgres_20260804/all` | 564 | PostgreSQL 隔离评测空间 |
| Layer 3：检索、证据与回答 | `layer3_v2_final_20260804_122146` | 520 | PostgreSQL 隔离评测空间、真实查询入口 |

百分比均四舍五入至两位。`—` 表示当前最终产物没有可被诚实换算成该指标的独立分子/分母；不将其填成 0 或 100。

## 口径与边界

1. Layer 1 的 **Candidate** 是项目评测器的官方宽松候选匹配：预测和 Gold 的 `memory_type` 一致即可候选配对，`evidence_span`、topic、key 仅决定优先配对顺序。因此 Candidate F1 不等同于证据片段正确率。
2. 本报告补算的 Layer 1 **Evidence Span** 是严格口径：先按上述官方 type-aware 配对；只有配对后 span 完全一致才记 TP，span 不同记 1 FP + 1 FN，漏候选记 FN，多余候选记 FP。
3. Layer 3 的 `m1/v1/s1` 只作为评测数据的逻辑引用，用于评测对齐；生产答案逻辑不依赖这些逻辑 Ref。Selected Context 指标只使用生产入口已暴露的 `selected_context_refs`，不从答案文本或 Gold 反推。
4. Layer 3 的 Recall/Hit/MRR/nDCG 是原始 retrieval 候选诊断；历史问题的实际证据由 `memory_history` 返回版本记录，因而 raw `history_hit@K` 不能单独代表历史回答质量。

## Layer 1：Should-store 与候选提取（730 条，Hybrid）

| 指标 | TP | FP | FN | Precision | Recall | F1 / Accuracy |
|---|---:|---:|---:|---:|---:|---:|
| Should-store | 588 | 14 | 52 | 97.67% | 91.88% | 94.69% |
| Candidate（官方宽松匹配） | 748 | 90 | 182 | 89.26% | 80.43% | 84.62% |
| Evidence Span（严格，补算） | 385 | 453 | 545 | 45.94% | 41.40% | 43.55% |
| Memory Type Macro-F1 | — | — | — | — | — | 84.19% |
| Task Status Accuracy | 739 | — | 930 个 Gold 字段 | — | — | 79.46% |
| Memory Key Accuracy | 255 | — | 930 个 Gold 字段 | — | — | 27.42% |
| Polarity Accuracy | 741 | — | 930 个 Gold 字段 | — | — | 79.68% |
| Multi-candidate Recall（补算） | 317 | — | 470 个 Gold 候选 | — | 67.45% | 67.45% |

Multi-candidate Recall 只在含两个及以上 Gold 候选的 180 个 case 上统计，仍使用官方 type-aware 候选配对。它反映一条原文含多段记忆时，有多少 Gold 候选被找出。

### Memory Type 混淆矩阵（辅助诊断）

为展示类型误判，先按 evidence span、canonical topic、memory key 依次对齐，**忽略 memory type**；未能按这三项任何一项对齐的候选不放入矩阵。这不是官方 Candidate P/R/F1 的口径。

Gold / Predict | preference | task | semantic | episodic |
|---|---:|---:|---:|---:|
| preference | 155 | 0 | 0 | 0 |
| task | 2 | 145 | 1 | 2 |
| semantic | 6 | 1 | 194 | 0 |
| episodic | 0 | 20 | 0 | 76 |

未对齐 Gold 328 条、未对齐预测 236 条。最显著的已对齐类型混淆是 episodic → task（20 条）；更大的问题仍是内容/topic/key/span 对不齐，而非纯类型分类。

补充说明：本次 Hybrid 运行的 LLM 调用为 705 次，成功 664 次、失败 41 次（94.18%）。这会影响召回，但不是评分器把失败伪装为正确。

## Layer 2：记忆归并、任务状态与版本（564 条全量 PostgreSQL）

| 指标 | 结果 | 统计口径 |
|---|---:|---|
| Task Identity F1 | 100.00%（TP 370 / FP 0 / FN 0） | 被识别为同一任务/记忆的实体身份 |
| Relation Macro-F1 | 100.00% | `new/same/merge/update/supersede/conflict` 宏平均 |
| Action Accuracy | 100.00% | `insert/add_source/update/pending_review` |
| Current State Accuracy | 96.49%（549/569） | 可评分决策的最终 memory type、task status、版本序列字段；三者均为该值 |
| Task Transition Accuracy | 95.28%（101/106） | 有效任务状态转移 case |
| Duplicate Active Rate | 0.00% | 不应产生重复 active 记忆 |
| Stale Active Rate | 0.00% | 不应保留已经过期/被替代的 active 记忆 |
| Version Sequence Accuracy | 96.49%（549/569） | 版本链顺序与预期一致 |
| Source Link F1 | 98.89%（TP 888 / FP 0 / FN 20） | 来源链接集合 |
| Pending-review F1 | 100.00%（TP 105 / FP 0 / FN 0 / TN 489） | 真实 pending-review 持久化契约 |
| Orphan Done Task Rate | 0.00% | done task 不应无有效归属/来源 |
| Idempotence | 100.00% | 重放同一输入不产生额外业务写入 |
| Concurrency Invariant | 100.00%（60/60） | PostgreSQL 并发专项；错误 0 |

附：SQLite 并发扩展集是 200/200 invariant pass、错误 0，但生产后端主值仍以上表 PostgreSQL 60/60 为准。

本次 564 条的 Case Exact 为 92.91%（524/564，40 条失败），它不是本表的 Current State 指标。已核对这 40 条均在 `non_task_consolidation`，主要是同一时间点的 `+08:00` 与 `+00:00` 文本表示差异；不能直接归因于归并业务决策错误。

### Relation 混淆矩阵

Gold / Predict | new | same | merge | update | supersede | conflict | other |
|---|---:|---:|---:|---:|---:|---:|---:|
| new | 100 | 0 | 0 | 0 | 0 | 0 | 0 |
| same | 0 | 115 | 0 | 0 | 0 | 0 | 0 |
| merge | 0 | 0 | 41 | 0 | 0 | 0 | 0 |
| update | 0 | 0 | 0 | 195 | 0 | 0 | 0 |
| supersede | 0 | 0 | 0 | 0 | 38 | 0 | 0 |
| conflict | 0 | 0 | 0 | 0 | 0 | 105 | 0 |

### Action 混淆矩阵

Gold / Predict | insert | add_source | update | pending_review | other |
|---|---:|---:|---:|---:|---:|
| insert | 120 | 0 | 0 | 0 | 0 |
| add_source | 0 | 115 | 0 | 0 | 0 |
| update | 0 | 0 | 254 | 0 | 0 |
| pending_review | 0 | 0 | 0 | 105 | 0 |

### 任务最终状态对齐矩阵（辅助诊断）

下表遍历所有含 `gold.final_task_status` 的决策字段；它的分母不同于只筛有效“状态转移”的 Task Transition Accuracy，因此不能用表中总数反推 101/106。

Gold / Predict | todo | blocked | done | cancelled | other/缺失 |
|---|---:|---:|---:|---:|---:|
| todo | 148 | 0 | 0 | 0 | 20 |
| blocked | 0 | 94 | 0 | 0 | 0 |
| done | 0 | 0 | 116 | 0 | 0 |
| cancelled | 0 | 0 | 0 | 46 | 0 |

## Layer 3：检索、证据与回答（520 条 v2）

### 检索指标

| 指标 | @1 | @3 | @5 | @10 |
|---|---:|---:|---:|---:|
| Recall@K | 59.95% | 70.43% | 72.61% | 73.70% |
| Precision@K | 74.23% | 33.33% | 21.15% | 10.87% |
| Hit Rate@K | 74.23% | 84.62% | 84.62% | 84.62% |
| Current Hit Rate@K | 74.23% | 84.62% | 84.62% | 84.62% |
| History Hit Rate@K（raw retrieval） | 0.00% | 0.00% | 0.00% | 0.00% |

| MRR | nDCG@10 |
|---:|---:|
| 79.26% | 66.12% |

`History Hit Rate=0` 的原因是历史问题转由 `memory_history` 读取 version refs，raw retrieval 列表不承载历史版本；历史回答本身由下文 Timeline Claim Group 和路由矩阵核验，不能把该 0% 误读为历史回答均失败。

语义检索专项（130 条）已达到向后续回答决策交付的门槛：vector seed ready 720/720（100%）、embedding contract 720/720（100%）、query embedding 130/130（100%）；paraphrase Recall@3 20/20、typo 20/20、mixed-language 40/40、noise 20/20，均为 100%。

### 回答、证据与安全指标

| 指标 | TP | FP | FN | Precision | Recall | F1 / Accuracy |
|---|---:|---:|---:|---:|---:|---:|
| Selected Context（补算） | 802 | 35 | 145 | 95.82% | 84.69% | 89.91% |
| Claim | 565 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| Timeline Claim Group | 125 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| No-answer | 35 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| Citation | 782 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| Conflict Accuracy | 20/20 | — | — | — | — | 100.00% |
| Clarification Accuracy | 10/10 | — | — | — | — | 100.00% |
| Restricted Accuracy | 15/15 | — | — | — | — | 100.00% |
| Qualified-history Accuracy | 20/20 | — | — | — | — | 100.00% |
| Stale Answer Usage | 0/520 | — | — | — | — | 0.00% |
| Evidence Coverage（暴露可用性） | 520/520 | — | — | — | — | 100.00% |
| Answer Availability | 520/520 无 `system_error` | — | — | — | — | 100.00% |

Selected Context 的 Gold 是 `relevant_current_refs ∪ relevant_history_refs`，预测是生产回答结果公开的 `selected_context_refs`。Evidence Coverage 仅表示这三项 evidence contract（selected context、selected tool、executed tools）均已真实暴露且可评分，三项 unavailable rate 均为 0%；内容选择质量由 Selected Context P/R/F1 单列，不能以“已暴露”冒充“选对”。

Answer Availability 的具体返回类型：`answered` 420、`no_answer` 35、`qualified_history_only` 20、`conflict` 20、`restricted` 15、`clarification` 10、`system_error` 0。这里的 100% 表示 520 条都返回了契约内的结构化结果，不表示所有 case 都应该给出事实性答案。

安全补充：访问违规 0、跨空间违规 0、业务状态变更 0、must-not-return 违规率 0.00%、禁止性断言率 0.00%。restricted 契约使用不含敏感正文的 `access_denied` marker；答案、日志化预测和 citation 不包含敏感内容。

### Answer Type 混淆矩阵

Gold / Predict | answered | no_answer | qualified_history_only | conflict | clarification | restricted | system_error |
|---|---:|---:|---:|---:|---:|---:|---:|
| answered | 420 | 0 | 0 | 0 | 0 | 0 | 0 |
| no_answer | 0 | 35 | 0 | 0 | 0 | 0 | 0 |
| qualified_history_only | 0 | 0 | 20 | 0 | 0 | 0 | 0 |
| conflict | 0 | 0 | 0 | 20 | 0 | 0 | 0 |
| clarification | 0 | 0 | 0 | 0 | 10 | 0 | 0 |
| restricted | 0 | 0 | 0 | 0 | 0 | 15 | 0 |

### 延迟

| 阶段 | P50 | P95 | P99 |
|---|---:|---:|---:|
| Retrieval | 322.183 ms | 480.557 ms | 629.529 ms |
| Answer | 1,666.570 ms | 4,491.472 ms | 10,223.209 ms |
| Total | 3,939.423 ms | 7,166.059 ms | 11,978.621 ms |

### 路由诊断混淆矩阵（非 Answer Type）

这是“测试期望路由”与生产 observed route diagnostic 的对照，路由标签不要求一一相等，不能将其作为回答正确率；例如 125 个 complex 历史问题由 history 专用路径完成。

Expected / Observed | history | structured | complex | vector |
|---|---:|---:|---:|---:|
| complex | 125 | 75 | 0 | 0 |
| exact | 0 | 30 | 0 | 0 |
| fulltext | 0 | 30 | 0 | 0 |
| hybrid | 0 | 95 | 10 | 50 |
| structured | 0 | 50 | 0 | 15 |
| trigram | 0 | 0 | 0 | 20 |
| vector | 0 | 0 | 0 | 20 |

## 原始产物索引

- Layer 1：`eval/results/layer1_stage8_hybrid_20260804/metrics.json`、`cases.jsonl`
- Layer 2：`eval/results/layer2_stage8_postgres_20260804/all/metrics.json`、`predictions.jsonl`；并发：`eval/results/layer2_postgres_concurrency/layer2_postgres_concurrency_metrics.json`
- Layer 3：`eval/results/layer3_v2_final_20260804_122146/layer3_metrics.json`、`layer3_predictions.jsonl`、`layer3_latency_report.json`、`layer3_route_confusion.json`、`layer3_access_control_report.json`

本报告新增的补算项只有：Layer 1 Evidence Span P/R/F1、Layer 1 Multi-candidate Recall 和类型辅助混淆矩阵、Layer 3 Selected Context P/R/F1、Layer 3 Timeline Claim Group P/R/F1 与 Answer Type 混淆矩阵。补算均直接读取保存的逐 case 预测与 Gold 元数据；没有重跑生产代码、没有写入或修改用户 PostgreSQL 数据。
