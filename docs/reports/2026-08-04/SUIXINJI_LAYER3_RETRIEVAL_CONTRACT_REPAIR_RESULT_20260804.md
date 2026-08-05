# Layer 3 检索契约修复与 520 条复跑结果（2026-08-04）

## 范围

本次针对 Layer 3 独立评测中“raw Recall 混合了当前事实、历史版本和安全负例”的问题完成四项修改，并使用原有 `eval/layer3/data_v2` 的 520 条数据、PostgreSQL 隔离空间重新执行生产查询入口。

| 基线 | 修复后运行 |
|---|---|
| `eval/results/layer3_v2_final_20260804_122146/` | `eval/results/layer3_retrieval_contract_repair_20260804/` |

本次没有读取 Gold 或逻辑 Ref 来改变生产行为；`m1/v1/s1` 只在评测器读取生产已暴露 id 后做对齐。每条评测空间执行后均清理，未修改用户 PostgreSQL 数据。

## 样本量与有效分母

520 是执行 case 数，不等于 520 个彼此独立的 query 模板。各数据集的名义样本与 query 多样性如下：

| 数据集 | Case 数 | 唯一 query 数 | 普通/专项有效 case |
|---|---:|---:|---:|
| `current_state_retrieval` | 120 | 12 | 当前正例 120 |
| `history_and_temporal` | 100 | 13 | 历史版本 100 |
| `multi_memory_answer_and_citation` | 100 | 8 | 当前多证据 75；历史 synthesis 25 |
| `no_answer_conflict_and_stale` | 100 | 10 | 普通 Recall 不纳入；安全诊断中有 40 条带相关/历史元数据 |
| `semantic_paraphrase_and_noise` | 100 | 6 | 当前正例 100 |

因此普通当前检索的有效分母是 295 条，raw Recall 的非空相关证据分母是 460 条；其余 case 是 no-answer、restricted、stale-only 或历史专用契约。样本重复会让置信区间偏乐观，尤其 `multi_memory` 只有 75 条当前正例和 8 个 query 模板，100% 不能直接等同于生产泛化能力。

## 实施内容

1. 在 [memory/service.py](/home/zcj/suixinji/memory/service.py) 的 `memory_search` 增加库存型查询的覆盖式 rerank。
   - 仅对“列出、概括、分别”等显式清单/汇总请求启用；普通事实查询保持仓库排序。
   - 候选先由仓库执行 access control 和敏感内容过滤；rerank 仅重排已授权候选，不改变安全边界。
   - 使用 query、memory type、canonical topic、task status、object value、内容中是否存在结构化状态来提升不同主题与结构化状态记录的覆盖；不使用评测 case id、Gold 或逻辑 Ref。
2. 在 [run_layer3_eval.py](/home/zcj/suixinji/eval/layer3/run_layer3_eval.py) 增加历史版本专项。
   - 对直接历史问题调用生产 `memory_history`，记录其实际返回的 version refs。
   - `history_version_retrieval` 只统计 `answer_type=answered` 且 `evidence_mode=history` 的直接历史请求。
   - `qualified_history_only` / stale-only 继续在安全指标中衡量，不冒充历史版本检索成功。
3. 评测输出新增两个分开的召回契约。
   - `ordinary_current_retrieval`：只统计应由当前事实回答的正例（295 条）。
   - `history_version_retrieval`：只统计直接历史版本请求（125 条）。
   - 原顶层 `recall@K` 保留为全 case raw 检索诊断，以便与旧基线比较；它不再被解释为普通事实检索质量。
4. 保留 no-answer、restricted、stale-only、conflict、clarification 的独立安全指标；它们不进入普通当前事实 Recall。

## 回归验证

- 单元/契约测试：18 passed。
- PostgreSQL 小样本：10/10，answer error 0，execution error 0。
- PostgreSQL 全量：520/520，answer error 0，execution error 0。

## 结果

### 1. Raw retrieval 诊断（全量，可与旧基线比较）

| 指标 | 旧基线 | 修复后 | 变化 |
|---|---:|---:|---:|
| Recall@3 | 70.43% | 73.70% | +3.27pp |
| Recall@10 | 73.70% | 73.70% | 0.00pp |
| MRR | 79.26% | 82.21% | +2.95pp |
| nDCG@10 | 66.12% | 68.48% | +2.36pp |

`Recall@10` 不变而 `Recall@3` 上升，说明本次是把原本已在候选池后段的相关证据提前，而不是凭空增加候选。这正是覆盖式 rerank 的预期作用。

### 2. 多证据问题

| 数据集 | 旧 Recall@3 | 旧 Recall@10 | 新 Recall@3 | 新 Recall@10 |
|---|---:|---:|---:|---:|
| `multi_memory_answer_and_citation`（100 条，raw） | 66.25% | 81.25% | 81.25% | 81.25% |
| 其中普通当前多证据（75 条） | 未单列 | 未单列 | 100.00% | 100.00% |

这表明此前的主要问题是多条相关证据排在 Top3 之后；修复后已经全部进入 Top3。历史 synthesis 的 25 条不计入“普通当前多证据”，而由下节的版本召回衡量。

### 3. 分离后的正式检索指标

| 指标 | Case 数 | Recall@1 | Recall@3 | Recall@10 | Hit@3 |
|---|---:|---:|---:|---:|---:|
| Ordinary Current Retrieval | 295 | 84.46% | 100.00% | 100.00% | 100.00% |
| History Version Retrieval | 125 | 37.73% | 100.00% | 100.00% | 100.00% |

历史版本 Recall@1 低于 @3 是正常的：一个 timeline 可能要求两个或三个版本，Top1 无法覆盖完整序列；@3 已能覆盖所需版本。此前历史 raw Recall@3 的 27.75% 是 current memory refs 与 version refs 的对象错配，不再作为历史能力结论。

### 4. 回答与安全回归

| 指标 | 修复后 |
|---|---:|
| Claim P/R/F1 | 100.00% / 100.00% / 100.00%（565/0/0） |
| Citation P/R/F1 | 100.00% / 100.00% / 100.00%（782/0/0） |
| No-answer P/R/F1 | 100.00% / 100.00% / 100.00%（35/0/0） |
| Stale Answer Usage | 0.00% |
| must-not-return violation | 0.00% |
| Forbidden Claim | 0.00% |
| Access violation / Cross-space violation | 0 / 0 |
| Restricted expected / predicted | 15 / 15 |
| Answer / execution error | 0 / 0 |
| Selected Context P/R/F1 | 95.82% / 84.69% / 89.91%（不变） |

raw stale retrieval rate 仍为 10.38%，其中一部分来自直接历史查询和 stale-only 安全 case；它没有进入答案。后续若要继续收紧，可将“当前问题中的 stale candidate”与“历史查询允许的历史版本”进一步分开统计，不能以此驱动放宽安全过滤。

## 结论

本次完成了多证据候选的 Top3 覆盖修复，并将历史版本、普通当前事实和安全负例的评测契约拆开。当前下一项真实可优化点不再是 Top3 多证据覆盖，而是 Selected Context Recall 84.69%：生产回答最终选择时仍遗漏 145 个应选 context ref，尽管现有答案与引用在该受控数据集上没有回归。
