# 随心记 Memory/Ask 检索修复计划

日期：2026-08-28
范围：长期 Memory 检索、旧版 ReAct `/ask`、Ask V2；不引入 Cross-Encoder。

## 目标

1. 写入时生成的稳定身份字段能够真正用于查询。
2. 每个召回通路只计分一次，RRF 结果可以解释。
3. 任务状态和 Semantic 当前事实不会因为错误路由或“只取最新一条”而答错。
4. 查询改写和向量召回只能补充结果，不能挤掉原查询的强命中。

## P0：先修正确性

### 1. RRF 通路治理

- 将混在 `structured` 中的信号拆成 `structured_slot`、`type_hint`、`lexical`、`legacy_recent`。
- 同一条 Memory 在同一通路内只保留最好名次，只贡献一次 RRF 分数。
- `family` 纳入多通路命中奖励。
- 保存每个通路的 rank、RRF contribution 和最终分数。

### 2. 路由修复

- 旧版 ReAct 的明确任务状态问题统一调用 `task_status_search`。
- Ask V2 fallback 中，旧版 `semantic_search` 映射为 Note 查询，不能误映射成 Semantic 当前事实。
- 补齐 `task_status_search`、近期事件等确定性 fallback 映射。

### 3. Semantic 当前事实修复

- facet 只负责缩小范围，不能单独证明两条事实属于同一个具体问题。
- 优先使用 projection 标出的 current/uncertain 记录和查询真实命中记录。
- 不再确定性地只取第一条；需要时把多个当前或冲突证据交给回答层判断。

## P1：改善召回与排序

### 4. 结构化查询合同

- 新增 `MemoryQuerySpec`：`memory_type`、`canonical_topic`、`family_key`、`subject`、`predicate`、`entities`、`time_mode`。
- Ask V2 从 QueryUnit 构造；旧 ReAct 从已识别的类型和查询主题构造。
- Exact/Family/Structured 先召回，文本和向量负责兜底。

### 5. 多查询融合

- 原查询和 topic/改写查询分别召回，再做加权 RRF。
- 原查询保留至少一半候选名额；其他通路竞争剩余名额。
- 禁止第一组结果直接占满候选池。

### 6. 排序规则修正

- embedding 覆盖不足时自动降低 vector 权重。
- Structured 先按字段匹配强度排序，再按更新时间排序。
- 状态惩罚改为累乘，移除访问次数造成的“越搜越靠前”反馈。
- FTS 使用有界 OR 查询提高中文短词覆盖，最终仍由融合与规则排序控制。

## P2：验证与验收

- 单测：重复 structured、family 计数、任务状态路由、fallback 映射、Semantic 同 facet 不同事实、多查询融合、部分向量覆盖。
- 回归：现有 retrieval、Ask V2、task/semantic 测试全部通过。
- 保持 `SUIXINJI_ASK_CROSS_ENCODER_ENABLED=false`，只验证无 CE 路径。
- 后续单独建立长期 Memory 检索集，不能拿 Note-only LongMemEval 指标代替。

## 完成标准

- 同名通路不会重复贡献 RRF。
- 明确任务状态问题能够同时看到当前 Task 和有界 Episodic 证据。
- Semantic 当前问题不会仅因更新时间选中同 facet 的无关事实。
- 每次检索可以追溯候选来自哪一路、各路名次和最终得分。
- 新增测试和相关既有测试全部通过。

## 实施结果（2026-08-28）

已完成：

- 新增 `MemoryQuerySpec`，旧 ReAct 自动构造，Ask V2 从 QueryUnit 构造。
- RRF 按逻辑通路先去重，再融合；拆分 Structured 子通路并补入 Family 计数。
- Memory trace 新增每条候选的 `channel_ranks`、`channel_scores`、`rrf_score`、`policy_score` 和 `final_score`。
- 明确任务状态查询改走 `task_status_search`；Ask V2 fallback 映射已修正。
- Semantic 当前事实只补 projection 选中的记录，不再把整个 facet 当成同一身份，也不再只给回答层一条证据。
- Ask V2 原查询保留半数名额，每个附加查询至少有一个候选进入融合，再用加权 RRF 填满剩余位置。
- Vector 根据实际候选覆盖率自动降权；FTS 改为有界 OR；Structured 先按匹配强度排序。
- 状态惩罚改成累乘；访问次数退出相关性评分。

验证：

- Ruff：通过。
- Memory/Query/Ask 无 CE 相关回归：`241 passed`。
- 首轮重点回归：`65 passed`。
- 新增真实 PostgreSQL Structured + Family 集成测试：`1 passed`。
- PostgreSQL 仓库套件首轮为 `10 passed, 1 failed`；失败是 6 并发用例遇到连接池 `3 + 2` 容量的瞬时超时，单独复跑后通过。

未执行：

- 没有开启 CE。
- 没有重启飞书进程。
- 没有用 Note-only LongMemEval 指标冒充长期 Memory 检索指标。
