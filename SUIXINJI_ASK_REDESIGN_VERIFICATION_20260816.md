# Ask 改造验证结果（2026-08-16）

本次只采用下列“修复后”结果。所有测试均使用隔离 PostgreSQL tenant/space；不调用飞书，结束后已清理测试数据。Ask V2 仍是灰度：本次通过直接调用工作流验证，未打开用户可见开关。

## 1. LongMemEval-S：真实 Note Hybrid 检索

- 数据：LongMemEval-S cleaned 的分层 60 case；平均历史会话数 **48.33**。
- 路径：保存真实 Note → 写入真实 embedding → 项目 `hybrid_search_notes`。不进行 Memory 抽取，不调用回答 LLM。
- 完成：**60/60**，失败 0。

| 指标 | 结果 |
| --- | ---: |
| Answer-session Recall@1 / @3 / @5 / @10 | 40.00% / 58.33% / 70.00% / 90.00% |
| Answer-session Coverage@1 / @3 / @5 / @10 | 28.19% / 43.61% / 56.53% / 79.03% |
| 检索延迟 mean / p50 / p95 | 0.460 / 0.436 / 0.715 s |

结论：Top-10 召回达到 90%，但 Top-1 只有 40%，说明当前候选召回够用、首位排序仍是主要短板。最弱桶是 `single-session-assistant`（Recall@1 20%）；不能把本结果表述成回答准确率或官方 LongMemEval 总分。

原始工件：

- `eval/external/longmemeval/results/lme_s_hybrid_60_r2_20260816/longmemeval_s_note_hybrid_20260816T062052Z.jsonl`
- `eval/external/longmemeval/results/lme_s_hybrid_60_r2_20260816/longmemeval_s_note_hybrid_20260816T062052Z.summary.json`

## 2. 真实 Ingest → Memory → Ask V2

路径：生产 receiver 合约 → 真实 Redis Stream → 现有 ingest/memory worker → `answer_question_v2`。四个 case 为 task 状态、语义最新地址、偏好覆盖、episodic 回忆。

| 指标 | 结果 |
| --- | ---: |
| worker 主链路完成 | 4/4（100%） |
| 预期 Memory 类型出现 | 4/4（100%） |
| 回答包含预期证据 | 3/4（75%） |
| task 持久化状态正确 | 0/1（0%） |
| 严格端到端通过 | 2/4（50%） |
| Ask 延迟 | 4.49–5.78 s |
| Stream ACK 后清理 | true |

失败不能忽略：

1. `环境部署文档已经完成` 的回答说完成，但对应 task Memory 仍为 `todo`，新消息被抽成 episodic。因此展示层“像正确”不等于状态演进正确。
2. `北京 → 上海` 两条 semantic 都被保存，但 Ask 返回北京，说明 semantic 的当前值解析没有把时间新近性和冲突证据可靠地带入选择。

另一个工程发现：PostgreSQL Task 完成与 Redis ACK 是两个操作。若在二者之间删除测试空间，会留下“已删 task 的 pending Stream 消息”并堵住单 memory worker。本次评测器已改为确认本 tenant 的所有 Stream 消息已 ACK 后才清理；最终 run 的该项为 `true`。

原始工件：

- `eval/external/ask_v2/results/full_lifecycle_20260816_r4/results.jsonl`
- `eval/external/ask_v2/results/full_lifecycle_20260816_r4/metrics.json`
- `eval/external/ask_v2/results/full_lifecycle_20260816_r4/summary.md`

## 3. 当前结论与下一步

这次验证证明 Ask V2 的计划、证据 span 和事实层能完整运行，但尚不应切换用户可见流量。优先级如下：

1. 修 task 的“同任务完成”识别和 Relation Guard 的状态演进，避免误降级为 episodic。
2. 给 semantic 查询增加基于证据时间与冲突的 current-value resolver；它只能选择/说明证据，不能改写历史 Note。
3. 针对 LongMemEval 的 Top-1 引入排序消融和难例分析，再考虑 cross-encoder 或更强确定性排序。

## 4. 修复后全链路与 60 条配对 A/B（补充）

修复 task 身份锚点和 semantic current-value resolver 后，`full_lifecycle_20260816_r7` 的四个真实 worker case 均严格通过：task 只保留一条 `done`，北京→上海回答上海，且 Stream ACK 后清理为 true。

随后在 LongMemEval Oracle 的分层 60 case 上，对同一隔离证据 Note 分别执行旧 ReAct 与 Ask V2：

| 指标 | Ask V2 | 旧 ReAct |
| --- | ---: | ---: |
| 完成 case | 60/60 | 60/60 |
| Oracle Evidence Recall@1 | 100.00% | 91.67% |
| 平均延迟 | 5.876 s | 4.570 s |
| 独立 fast-LLM 盲评 | 16/56 = 28.57%（4 次评审错误） | 18/60 = 30.00% |

该 Oracle 没有干扰会话，Evidence Recall 只说明证据使用链路；它与 LongMemEval-S 的含干扰检索结果不能混合。盲评中 V2 在 `single-session-assistant` 与 `single-session-user` 较好，但在 `knowledge-update`、`multi-session`、`temporal-reasoning` 与尤其 `single-session-preference` 落后。因此 P5 继续保持 Shadow：不能切流或删除旧 ReAct。

对应工件：

- `eval/external/ask_v2/results/full_lifecycle_20260816_r7/`
- `eval/external/longmemeval/results/longmemeval_oracle_react_vs_ask_v2_60_r2_20260816/`


## Layer3 Ask V2 全量回归（2026-08-16）

- 数据：`eval/layer3/data_v2`，520 case，PostgreSQL + hybrid，`--ask-engine v2`，并发 4。
- 旧 ReAct 基线：Answer Claim F1 **95.58%**，Citation F1 **100%**，端到端成功率 **79.81%**，p95 **10.02s**。
- Ask V2：Answer Claim F1 **48.53%**，Citation F1 **61.10%**，端到端成功率 **26.73%**，p95 **11.07s**；无执行/模型错误、无访问越权、无业务状态写入。
- 结论：保持 Shadow，**不进入灰度**。
- 主因：计划器没有稳定路由历史/时间线、任务清单/聚合、"当前主要在忙什么"等泛任务语义；这些 unit 被分配到缺少对应检索能力的工具，产生 `not_found`。下一轮应先补 `task_inventory`、timeline 强制路由与 planner-vs-executor 路由契约测试，再重新评测。
- 产物：`eval/results/layer3_ask_v2_full_20260816/`；旧链路对照：`eval/results/layer3_ask_v2_r7_20260816/`。


## Layer3 Ask V2 修复后全量回归 r3（2026-08-16）

- 数据：`eval/layer3/data_v2`，520 case；隔离 PostgreSQL space、Hybrid 检索、`--ask-engine v2`、并发 4。
- 本轮修复：多目标兜底拆分、Task inventory 的“先过滤来源再截断”、历史版本的结构化 Claim Group、敏感和显式无关证据过滤、评测 Source Note 的 `run_id` 隔离。

| 指标 | 修复后 r3 |
| --- | ---: |
| Retrieval Precision@1 / Recall@1 / MRR | 79.81% / 62.37% / 82.21% |
| Answer Claim F1 | 75.67% |
| Citation F1 | 100.00% |
| 端到端成功率 | 65.38% |
| 总延迟 p50 / p95 | 4.66 s / 5.57 s |
| 回答/执行错误 | 0 / 0 |
| must-not-return 泄露 | 0 |

分桶结果：`current_state_retrieval` 和 `semantic_paraphrase_and_noise` 的端到端、Claim F1、Citation F1 均为 100%；`multi_memory_answer_and_citation` 端到端 100%、Claim F1 90%、Citation F1 100%。100 条多记忆专项也单独复测通过：端到端 100%、Citation P/R/F1 100%、无执行错误、无 must-not 泄露。

仍不能切流：`history_and_temporal` 的端到端评分仍为 0%，尽管其 Citation F1 为 100%；该桶暴露的是更广泛的历史版本聚合/claim-group 覆盖缺口，而非当前态检索。`no_answer_conflict_and_stale` 的端到端 20% 主要受非事实回答的统一评分口径影响，不能用 Claim F1 替代安全正确性。故保持 **Shadow**，不启用 `SUIXINJI_ASK_V2_ENABLED`，也不退役旧 ReAct。

工件：

- `eval/results/layer3_ask_v2_full_r3_20260816/`
- `eval/results/layer3_ask_v2_multi_r12_20260816/`

## Layer3 Ask V2 最终只读回归 r7（2026-08-17）

本轮以全新 run-id 运行 520 条 `eval/layer3/data_v2`：PostgreSQL、Hybrid 检索、`--ask-engine v2`、并发 4。V2 仍为 Shadow；本评测显式调用 V2，不改变真实飞书用户的旧 ReAct 回答路径。

| 指标 | r7 结果 |
| --- | ---: |
| 当前态检索 Hit@1 / Recall@1 | 100.00% / 84.46%（295 条可比 case） |
| 历史版本检索 Hit@3 / Recall@3 | 100.00% / 100.00%（125 条可比 case） |
| Answer Claim P / R / F1 | 81.30% / 100.00% / 89.68% |
| Citation P / R / F1 | 100.00% / 100.00% / 100.00% |
| 端到端成功率 | 84.62% |
| No-answer P / R / F1 | 100.00% / 100.00% / 100.00%（TP=35, FP=0, FN=0） |
| 回答 / 执行错误 | 0 / 0 |
| 访问越权 / 跨空间返回 / 业务状态写入 | 0 / 0 / 0 |
| must-not-return / stale answer / forbidden claim | 0 / 0 / 0 |
| 总延迟 p50 / p95 | 4.50 s / 5.54 s |

### 本轮额外修复：评测空间与后台调度隔离

r5/r6 出现的状态写入不是 Ask V2 查询函数写入；根因是常驻 distributed scheduler 把 `l3_eval_*` 临时空间的真实 Source Note 当作待处理用户笔记，入队后被 memory worker 再抽取一次。修复在两个入口完成：

1. `apps/scheduler.py` 在入队前跳过 `l1_eval_*`、`l2_eval_*`、`l3_eval_*`；
2. `memory/scheduler.py` 即使收到这类空间的 consolidation task 也返回 `synthetic_evaluation_space`，不执行抽取。

重启 scheduler 后，原始失败 case `l3_multianswer_030` 单独复测为 0 写入；最终 r7 全量也为 0。新增两条单测覆盖“分布式入队不触发”和“worker 侧兜底不处理”，与 Ask V2 契约回归共 54 条通过。

这消除了评测环境与常驻后台任务的交叉副作用，但不代表可以切流。外部 LongMemEval 含干扰会话的 Top-1 排序与真实生命周期边界仍需继续验证，因此 `SUIXINJI_ASK_V2_ENABLED=false`、`SUIXINJI_ASK_V2_SHADOW=true` 保持不变。

最终工件：

- `eval/results/layer3_ask_v2_full_r7_20260817/`
- `eval/results/layer3_ask_v2_isolation_r7_20260817/`

## Layer3 Ask V2 最终只读回归 r8（2026-08-17）

r8 在 r7 通过后加入了 planner / executor / answer 的独立超时预算和阶段 Trace，并以全新 run-id 重跑同一份 520 case。配置为 PostgreSQL、Hybrid、`--ask-engine v2`、并发 4；评测直接调用 V2，线上仍保持 Shadow。

| 指标 | r8 结果 |
| --- | ---: |
| 当前态检索 Hit@1 / Recall@1 | 100.00% / 84.46%（295 条可比 case） |
| 历史版本检索 Hit@3 / Recall@3 | 100.00% / 100.00%（125 条可比 case） |
| Answer Claim P / R / F1 | 81.30% / 100.00% / 89.68% |
| Citation P / R / F1 | 100.00% / 100.00% / 100.00% |
| 端到端成功率 | 84.62% |
| No-answer P / R / F1 | 100.00% / 100.00% / 100.00%（TP=35, FP=0, FN=0） |
| 回答 / 执行错误 | 0 / 0 |
| 访问越权 / 跨空间返回 / 业务状态写入 | 0 / 0 / 0 |
| must-not-return / stale answer / forbidden claim | 0 / 0 / 0 |
| 总延迟 p50 / p95 | 4.48 s / 5.38 s |

本轮说明新增预算与 Trace 没有改变评测正确性或只读边界。它解决了阶段配置存在但未真正传入 LLM / executor 的工程缺口；不等价于解除 P5 的 Shadow 门槛。

最终工件：

## Layer3 Ask fair A/B: Legacy ReAct vs V2 (2026-08-17)

Both engines ran the same 520 cases with PostgreSQL, Hybrid retrieval, top-k 10, concurrency 4, and fresh isolated evaluation spaces. Raw retrieval metrics are intentionally reported as a shared diagnostic; they are not attributed to either answer engine.

| Metric | Legacy ReAct | Ask V2 |
| --- | ---: | ---: |
| Raw Hit@1 / Recall@1 | 79.81% / 62.37% | 79.81% / 62.37% |
| Raw MRR / NDCG@10 | 82.21% / 68.27% | 82.21% / 68.27% |
| Final selected Evidence P / R / F1 (420 eligible) | 94.36% / 94.36% / 94.36% | 100.00% / 100.00% / 100.00% |
| Answer Claim P / R / F1 | 100.00% / 100.00% / 100.00% | 81.30% / 100.00% / 89.68% |
| Citation P / R / F1 | 100.00% / 100.00% / 100.00% | 100.00% / 100.00% / 100.00% |
| End-to-end success | 84.62% | 84.62% |
| No-answer P / R / F1 | 100.00% / 100.00% / 100.00% | 100.00% / 100.00% / 100.00% |
| Total latency p50 / p95 | 4.72s / 5.80s | 4.48s / 5.43s |
| Answer / execution errors | 0 / 0 | 0 / 0 |
| Access violation / business writes | 0 / 0 | 0 / 0 |

Interpretation: the new evaluator separates raw candidate retrieval from the engine-selected memory/version evidence. V2 selects all eligible Gold evidence in this run, but its answer precision is lower because it emits 130 extra claims. The next V2 fix should constrain claim generation to evidence-supported and question-required facts; do not tune raw RRF based on the mixed overall score.

Artifacts:
- eval/results/layer3_ask_ab_legacy_r9_20260817/
- eval/results/layer3_ask_ab_v2_r9_20260817/
