# Layer 3 诊断计划修复记录

日期：2026-08-03

## 已完成

### 1. 访问控制前置过滤

- 新增 memory/access.py 统一 AccessContext 与 ACL predicate。
- Postgres 的 exact、structured、FTS、trigram、vector 通道在 RRF 融合前统一过滤。
- ORM 行读取 scope_json，修复此前只过滤 MemoryRecord.scope 导致 owner_only/high 数据漏出的缺陷。
- 未带 scope 的历史生产记忆保持兼容；显式 owner_only/private/restricted 记录按 requester、owner 和 allow flags 判定。
- 查询服务和历史时间线都传递访问上下文。

### 2. 多 API Key 与限流

- 保留 core/llm_key_pool.py 的多 key 轮换和单 key 冷却。
- 429 使用 Retry-After，并增加小幅随机抖动，避免多个 worker 同步重试。
- 兼容旧的单参数 client builder；不影响现有多 key 生产路径。
- LLM hook 的全局并发/预算限制继续生效。

### 3. 历史版本检索

- Postgres/SQLite 增加 get_memory_timeline。
- 新增 memory_history 查询工具；历史问题优先走 canonical memory，再展开有序版本。
- 历史回答使用确定性版本模板，避免 LLM 只返回当前版本或把多个候选混成一条。

### 4. 列表与画像查询

- 增加 list_tasks、list_recent_episodes、profile_summary 工具。
- 任务清单和近期事件使用确定性模板，减少简单问题的 LLM 调用。
- 显式“历史/列表/画像”路由优先于不确定的 QueryIntent LLM 路由。

### 5. 结构化回答契约

- 新增 AnswerResult、AnswerDecision、SupportedClaim。
- answer_question_result 保持旧 answer_question -> str 兼容，同时输出：
  - answered
  - no_answer
  - qualified_history_only
  - conflict
  - clarification
  - restricted
  - system_error
- 评估器同时记录 answer availability、成功调用质量和端到端质量，避免把 LLM 调用失败与检索质量混为一谈。
- 受限回答不返回具体内容；无答案不再只靠评估器关键词猜测。

### 6. Layer 3 评估诊断

评估输出新增/扩展：

- layer3_answer_availability.json
- layer3_answer_quality.json
- executed_channels
- stale retrieval、irrelevant retrieval、ambiguous candidate 分开统计
- 原始通道命中仍保留，仅用于诊断，不改变生产答案路径

## 验证

- 现有回归：9 passed
  - tests/test_llm_key_pool.py
  - tests/test_llm_client_memory_extraction_retry.py
  - tests/test_stage2_query_performance.py
  - tests/test_layer2_metrics.py
- Layer 3 五 case smoke：
  - execution/seed errors：0
  - answer errors：0
  - access violations：0
  - stale retrieval：0
  - business state mutation：0
- 历史 case 验证为 qualified_history_only，输出 v1/v2/v3 顺序。
- external_app 访问 owner_only/high 记录返回空结果，owner 请求可见。
- 服务重启后 /health 返回 status ok；当前聊天 key pool 为 2 个 key。

## 兼容性说明

飞书现有入口继续调用字符串接口 answer_question，无需修改消息格式。需要结构化结果的评估、API 或后续 UI 使用 answer_question_result。

## 2026-08-04 Stage 8：520 条全量回归结果

### 结果位置与可复现性

- 本次 Run ID：`stage8_full_20260804`。
- 原始结果目录：`/tmp/suixinji-layer3-stage8-full/`。这是系统临时目录，不在项目根目录下；其中 `layer3_summary.md` 是总览、`layer3_metrics.json` 是完整指标、`layer3_predictions.jsonl` 是逐 case 预测、`layer3_failed_cases.jsonl` 是失败清单。
- 该目录可能被系统清理。本报告记录可长期保留的结论；在全量通过前，不启动 Layer 1、Layer 2 或 Worker smoke。

### 已通过的安全与运行性门槛

| 项目 | 结果 |
|---|---:|
| Cases | 520 |
| answer / execution error | 0 / 0 |
| 访问越权 | 0 |
| 业务状态被评测写入 | 0 |
| stale answer usage | 0 |
| forbidden claim | 0 |
| restricted 识别 | 15 / 15 |
| 当前状态检索与 Claim / Citation | 1.000 / 1.000 |

这说明本轮的隔离 seed、运行链路、权限拒绝和已选证据的安全边界可用；没有触碰用户原有 PostgreSQL 数据。

### 未通过门槛与本次暴露的问题

全量尚未通过，不能进入跨层回归：总体 Claim F1 为 **0.547850**（门槛 `>= 0.95`），Citation F1 为 **0.962229**（门槛 `>= 0.98`）。问题分为生产行为、生产与评测契约对齐、以及仅诊断指标三类，不能混为一谈。

1. **历史 / 状态迁移的 Claim 粒度与评测契约不一致。**
   - `history_and_temporal`、`timeline`、`task_transition` 的 Claim F1 均为 0。
   - 生产侧已正确选择版本与来源，Citation F1 为 1.0；但它按时间线版本产生多个原子 Claim（例如 todo、blocked、done），数据集期望的是一个覆盖整条时间线的复合 Claim。因此产生 267 个 FP、100 个 FN，不代表历史事实或引用错误。
   - 后续必须为 `history_query` 和 `qualified_history_only` 定义“时间线摘要 Claim”：使用最终历史回答的事实文本，绑定全部已选 version/source，并保持 `support_role=history`。非事实 answer（`no_answer`、`clarification`、`restricted`、`conflict`）必须保持空 Claim，不能回退按答案文本切句。

2. **No-answer 的主题相关性约束不足，存在真实生产问题。**
   - 35 个“最喜欢的电影”缺失 case 中，16 个把无关的“喜欢咖啡”当作已选证据并回答；No-answer Recall 仅为 0.316667。
   - 后续在 AnswerDecision 中修复“候选与问题槽位/主题不匹配即不可答”的判断，不能通过读取 Gold 或 case id 特判。

3. **混合语言语义召回 / Query Rewrite 仍有真实缺口。**
   - `semantic_paraphrase_and_noise` 有 20 个本应 answered 的 mixed-language + hybrid case 返回 `no_answer`，缺少应有的 `s1` citation；该分桶 Citation F1 为 0.888889。
   - Stage 2 的定向门槛不能替代全量分桶结论。需要回到 Semantic Retrieval，检查 mixed-language rewrite 是否实际接入 hybrid 路径、rewrite 后 query embedding 是否成功，以及融合阈值是否把正确向量结果排除。

4. **Profile summary 选择了事实但遗漏第三条来源。**
   - 25 个 `multi_memory + profile_summary` case 仅选到 `s1/s2`，遗漏要求的 `s3`；该分桶 Citation F1 为 0.952381。
   - 后续修 EvidenceBundle/列表-画像选择的 source 完整性：每条被回答的事实必须带齐实际支持它的 source，不能为追求引用指标额外拼接未选证据。

5. **评测的 answer-type 预期与冻结后的安全/冲突契约存在历史不兼容。**
   - 15 个敏感身份号码 case 的旧期望是 `no_answer`，当前统一安全 ADR 的实际输出是 `restricted`；内容未泄露，属于契约升级后的预期差异，不应将生产降级为普通 no-answer。
   - 20 个真实 pending-review case 的旧期望是 `qualified_history_only`，当前按真实 pending-review 持久化契约输出 `conflict`；同样不能为了旧 Gold 把冲突伪装成普通历史回答。
   - 数据集 zip/jsonl 和 Gold 不在本轮修改范围。若要让这两类计入最终门槛，需要先由需求方确认新的评测契约并发布新版数据集，而非在生产代码中隐藏特判。

6. **原始检索干扰率是诊断信号，当前不可作为答案质量失败率。**
   - `must_not_return_violation_rate=0.894231`、`irrelevant_retrieval_rate=0.875` 来自 `raw_channel_hits`：候选通道中包含干扰项，但最终 selected Evidence / answer 未使用它们。例如当前状态分桶的最终 Claim、Citation 均为 1.0，原始指标仍为 1.0。
   - 应保留这些指标用于检索精度优化，但报告和验收必须区分“raw 候选命中”与“最终选中/用于回答”。不得为压低该数字将 Gold、must-not 集合注入生产过滤。

7. **Evidence 暴露覆盖仍不完整。**
   - `selected_context_unavailable_rate=0.075`；`selected_tool_refs_unavailable_rate=0.276923`。
   - 这表示部分路由没有把已执行工具或最终选择上下文完整暴露给 Evaluator。后续应补齐真实生产输出的 EvidenceBundle 字段；不能由答案文本、Gold 或额外查询倒推。

### 下一步（停在 Stage 7 / 检索修复）

按既定门禁，下一轮先分别修复并运行对应小集：时间线 Claim、no-answer 相关性、mixed-language semantic、profile source 完整性、真实 Evidence 暴露。只有这些小集通过后才重跑 520；在 Claim F1 `>= 0.95`、Citation F1 `>= 0.98` 前，不进入跨层 smoke。

## 2026-08-04 Stage 7/8 更新（Layer 3 v2 最终结果与跨层回归）

上面的 Stage 8 旧记录对应修复前基线。修复后的最终结果保存在：
`eval/results/layer3_v2_final_20260804_122146/`。

### Layer 3 v2 最终结果

| 指标 | 结果 |
|---|---:|
| Cases | 520 |
| Answer Claim F1 | 1.000000 |
| Citation F1 | 1.000000 |
| No-answer F1 | 1.000000 |
| Selected context unavailable | 0 |
| Selected tool refs unavailable | 0 |
| Must-not violation | 0 |
| Forbidden claim | 0 |
| Access violation | 0 |
| Answer / execution errors | 0 / 0 |

最终 answer type 计数为 `answered=420`、`no_answer=35`、`qualified_history_only=20`、`conflict=20`、`restricted=15`、`clarification=10`；未新增 `history_answered`。

### Layer 1 / Layer 2 回归

- Layer 1 rules 回归已完成。
- Layer 1 hybrid 全部 730 cases 已完成，结果在
  `eval/results/layer1_stage8_hybrid_20260804/`。整体 should-store F1 为 94.69%，Candidate F1 为 84.62%；`multi_candidate` Candidate F1 为 80.07%，`hard_language_and_noise` 为 76.42%。LLM 成功率分别按数据集记录在 `metrics.json`，不将失败调用计为成功。
- Layer 2 PostgreSQL、并发（60/60）及数据集校验已通过；最终报告在
  `eval/results/layer2_version_final_repair/version_source_idempotency/layer2_summary.md`。

### Redis Worker smoke 暴露的问题

结果在 `eval/results/redis_worker_chain_stage8_20260804/`：

- 60 条正常 ingest 消息全部完成（100%），10 次重复投递未造成重复业务处理，跨 space 隔离通过。
- poison memory 任务未观察到 retry/dead-letter，最终保持 queued；因此 Worker 的 retry/dead-letter 门槛尚未通过。
- 根因是分布式启动时 `outbox-relay` 曾因 PostgreSQL `127.0.0.1:15432` 连接失败退出；恢复数据库后，Outbox 仍有约万条历史未发布事件，relay 按时间顺序处理旧积压，未及时发布本次评测产生的下游 enrichment/memory 事件。
- 该问题属于运行环境/Outbox 运维与积压治理，不是 Layer 3 AnswerDecision 逻辑问题；不得通过修改用户 PostgreSQL 数据绕过。后续应先处理 relay 启动健康检查、积压监控和评测隔离队列，再重跑 Worker retry/dead-letter smoke。

积压清理后已用独立 poison-only smoke 复核：失败任务成功经历 retry，并最终进入 `dead_letter`；该项 Worker 逻辑本身通过。原先的 queued 结果是 Outbox 积压造成的运行环境假失败。

### 当前结论

Layer 3 v2 的契约、检索、回答和安全门槛已通过；Layer 2 回归已通过。Layer 1 hybrid 的字段质量仍需作为抽取质量改进项跟踪。跨层 Worker smoke 目前仅证明正常 ingest 与幂等路径，retry/dead-letter 因 Outbox 积压未完成，不能宣称全链路通过。

### Outbox 积压清理记录

后续核查发现积压并非一万条用户业务事件：其中 6,910 条是 `memory_embedding` 类型、已找不到对应 Task 的孤儿 `task.requested` 事件，创建时间集中在 2026-08-04 12:09–12:28（北京时间），来源为历史评测/任务清理残留。已精准删除这 6,910 条孤儿 Outbox 行；未删除任何 Task、Note、Memory 或用户数据。

清理后曾保留 24 条有对应 queued `memory` Task 的 Outbox 事件（`tenant_id=default`）。进一步核查确认它们不是用户任务，而是两次 Redis Worker 评测产生的 daily consolidation 任务：每次 12 个隔离评测 space，分别在北京时间 12:25 和 14:34 创建，payload 为 `operation=consolidate`、`period_key=2026-08-04`。这些评测 space 没有 Note 或 Memory；由于评测 space 被错误地落在 `default` tenant，原有按评测 tenant 清理的逻辑没有删除它们。用户确认后，已精准删除这 24 条 Task 及其 24 条 Outbox 事件；评测 space 本身保留，未删除 Note、Memory 或其他任务。
