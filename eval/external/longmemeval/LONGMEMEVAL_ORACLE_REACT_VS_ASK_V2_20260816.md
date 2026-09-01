# LongMemEval Oracle：旧 ReAct 与 Ask V2 的 30 条 A/B

## 对照方式

- 同一份官方 Oracle 分层样本：30 条，六类题型各 5 条。
- 每条 case 写入同一个隔离 PostgreSQL space 的同一组官方证据 Note。
- 旧路径：纯 ReAct；评测进程内关闭 V2 与 Shadow，避免观测开销干扰延迟。
- 新路径：直接运行 `answer_question_v2`。
- 每条完成后删除 space；最终确认剩余测试 space 为 0。

## 可比较结果

| 指标 | 旧 ReAct | Ask V2 | 解释 |
|---|---:|---:|---|
| 运行完成率 | 30 / 30 | 30 / 30 | 两条链路均无运行异常 |
| 非官方语义 Judge | 7 / 30（23.33%） | 6 / 30（20.00%） | 同一项目 fast LLM Judge；用于 A/B，不是官方 GPT-4o Judge |
| 平均端到端耗时 | 4.281 s | 3.420 s | V2 快约 20.1% |
| 来源 Note 显示命中@1 | 90% | 100% | Oracle 没有干扰会话，不能代表真实检索 Recall |

### 按题型的 Judge 正确数

| 类型 | ReAct | Ask V2 |
|---|---:|---:|
| knowledge-update | 2 / 5 | 1 / 5 |
| multi-session | 2 / 5 | 1 / 5 |
| single-session-assistant | 0 / 5 | 0 / 5 |
| single-session-preference | 1 / 5 | 1 / 5 |
| single-session-user | 0 / 5 | 0 / 5 |
| temporal-reasoning | 2 / 5 | 3 / 5 |

## 结论

1. 当前 V2 的速度更好，但 30 条小样本下回答正确率未超过 ReAct；不能据此开启正式 V2 用户回答。
2. V2 在时间推理小桶更好（3 / 5 vs 2 / 5），但知识更新和多会话计数更弱（各少 1 条）。这符合当前 V2 的短板：证据取到后仍缺少专门的数量、更新和多证据聚合 Resolver。
3. 两条链路共同的主要短板仍是 Note 原文截断、没有精确证据 span，以及回答模型直接承担事实裁决。
4. 旧 ReAct 的来源 Note 显示命中 90% 不能说明其真实召回低于 V2：它是从最终文本来源区块解析得到的可观测代理；V2 则直接返回结构化 selected evidence。

## 指标边界

- 早先的 `Gold substring match` 已确认不可靠：来源 Note ID 或题干复述会误包含数字/短语，因此不作为本报告结论。
- 本轮 LLM Judge 使用随心记当前 fast 路由的模型，和回答模型存在同源偏差；正式外部对比应改用独立模型或人工双盲抽检。
- Oracle 只有 Gold evidence session。下一轮 `longmemeval_s_cleaned.json` 含干扰会话，才能报告真正的 Note Hybrid Retrieval Recall@K。

## 可复现产物

- 原始 A/B：`results/longmemeval_oracle_react_vs_ask_v2_20260816T030243Z.jsonl`
- 修正后的严格代理：`results/longmemeval_oracle_react_vs_ask_v2_20260816T030243Z_rescored.jsonl`
- 语义 Judge：`results/longmemeval_oracle_react_vs_ask_v2_20260816T030243Z_rescored_llm_judged.jsonl`
