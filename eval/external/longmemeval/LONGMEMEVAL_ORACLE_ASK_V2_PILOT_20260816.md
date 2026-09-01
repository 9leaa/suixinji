# LongMemEval Oracle × Suixinji Ask V2：30 条隔离试验

## 范围

- 数据：官方 `longmemeval-cleaned / longmemeval_oracle.json`，500 条中的分层 30 条；六类题型各 5 条。
- 执行：每个 case 写入独立 PostgreSQL `eval-longmemeval-oracle-*` space，调用真实 `answer_question_v2`，结束即删除该 space。
- 边界：只写官方证据会话为 Note；不写真实飞书 space、不创建长期 Memory、不触发真实用户画像。
- 产物：`eval/external/longmemeval/results/longmemeval_oracle_ask_v2_20260816T025210Z.jsonl`。

## 指标

| 指标 | 结果 | 正确解读 |
|---|---:|---|
| 完成率 | 30 / 30 | 没有运行时失败 |
| Evidence Recall@1 / @3 / @5 | 100% / 100% / 100% | **不代表真实检索能力**；Oracle 只保留 Gold 证据会话，没有干扰项 |
| Gold substring match | 3 / 30（10%） | 透明的严格字符串代理，非官方 LLM Judge Accuracy |
| 平均端到端耗时 | 4.062 s | 包含 Planner、领域检索/补证与 Answer LLM |
| Resolution | resolved 1；partial 35 | 大多数英文问答最终走到 Note 补证，但回答器对证据的抽取/裁决不足 |

## 各题型 Gold substring match

| 题型 | 样本 | 命中 |
|---|---:|---:|
| knowledge-update | 5 | 1 / 5 |
| multi-session | 5 | 0 / 5 |
| single-session-assistant | 5 | 1 / 5 |
| single-session-preference | 5 | 0 / 5 |
| single-session-user | 5 | 0 / 5 |
| temporal-reasoning | 5 | 1 / 5 |

## 结论与问题定位

1. 这轮证明了 V2 链路可在隔离 PostgreSQL space 中完整运行：AskPlan → 检索/补证 → 来源 Note 展开 → Answer，且不会污染真实数据。
2. 不能用 Oracle 的 100% Evidence Recall 展示项目能力；它没有 distractor。要测 Note Hybrid Retrieval，下一轮必须跑 `longmemeval_s_cleaned.json`。
3. 10% 的严格答案代理表明主要瓶颈在“证据已取到后的信息抽取与时间/数量裁决”，不是 Oracle 的会话命中。
4. 典型失败：问“5K 个人最佳成绩”时，回答器拿到了 `25:50`，却把“目标成绩”误读成“没有具体记录”；这说明 Answer Synthesizer 不能只看摘要式证据，需要对选中原文做更精确的 span 定位，并将数量、时间、更新类问题交给专门的 evidence resolver。

## 不能得出的结论

- 不能称为 LongMemEval 官方 Accuracy：官方评测使用独立 LLM Judge，而本轮使用的是严格 Gold 子串代理。
- 不能称为长上下文/有干扰项 Recall：Oracle 不含干扰会话。
- 不能证明英文 Memory Extraction：本轮刻意未创建 Candidate/Memory，只测试 Ask V2 对原始 Note 证据的使用。

## 下一轮

1. 下载并运行 `longmemeval_s_cleaned.json`，用 40 个含干扰 session 的 case 测 Note Hybrid Recall@1/3/5。
2. 为 Answer Evidence Bundle 增加可定位的原文 span，并为时间、数量、知识更新启用确定性/受控裁决。
3. 用官方兼容的 LLM Judge 或人工抽样审计替代 Gold substring 代理。
