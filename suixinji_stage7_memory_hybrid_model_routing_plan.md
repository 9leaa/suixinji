# 随心记 Stage 7 完善计划：记忆系统、混合检索与多模型任务路由

## 0. 文档信息

- 仓库：`9leaa/suixinji`
- 审阅基线：`main@4132180ee0380de7ec4331242a09159d4d592f06`
- Stage 6 混合检索基线：`8e46d748dbc3655062b989d699ce905ed0c38211`
- 当前数据库迁移头：`20260718_0007`
- 当前部署：PostgreSQL + pgvector、Redis Streams、多个独立 Worker
- 当前数据策略：不迁移旧版历史库；只维护当前 `suixinji_v2` 中的新数据
- 本计划范围：长期记忆质量、Memory Vector 生命周期、混合检索、多模型任务分工、评测与上线。

当前稳定性修复已经合入 `main`，包括数据库连接路径、Redis 阻塞客户端、锁异常边界、任务恢复日志、飞书重复事件静默处理和 API 地址参数化。本计划不重复处理这些问题。

---

# 1. 当前项目状态

## 1.1 已完成

当前主链路可以完成：

```text
飞书消息
→ Inbox
→ Note
→ Memory Candidate
→ Candidate 校验
→ 旧 Memory 检索
→ 确定性关系审理
→ Memory 演化
→ Source / Version / Decision / Relation
```

已具备：

- Memory Key V2；
- `new / same / merge / update_task / supersede / conflict`；
- `pending_review`；
- Memory Source、Version、Decision、Relation；
- Memory Extraction State；
- Daily/Weekly/Monthly Consolidation 任务框架；
- 查询 Trace；
- PostgreSQL 混合召回框架；
- Fast/Balanced/Strong 三档模型配置。

## 1.2 当前混合检索

PostgreSQL 路径已有：

```text
Exact Memory Key
+ Structured Fields
+ PostgreSQL FTS
+ ILIKE Lexical Fallback
+ Optional pgvector
+ RRF Fusion
```

并支持：

```dotenv
SUIXINJI_MEMORY_RETRIEVAL_MODE=legacy|hybrid
SUIXINJI_MEMORY_HYBRID_VECTOR_ENABLED=true|false
```

## 1.3 当前多模型使用

| 任务 | 当前状态 |
|---|---|
| Memory LLM Candidate 抽取 | 已使用 `fast` |
| Query ReAct 与普通回答合成 | 主要使用 `balanced` |
| Note LLM 分类 | 未明确传角色，仍依赖 `OPENAI_MODEL` |
| Summary 草稿 | 未明确传角色 |
| Summary Review | 未明确传角色 |
| Memory 冲突/替代判断 | 主要为本地规则 |
| `strong` | 核心流程基本未正式使用 |
| Embedding | 独立 `EMBEDDING_MODEL` |

---

# 2. 当前核心问题

## P0：Memory Vector 生命周期不完整

数据库已有 `memory_vectors`、状态字段和 HNSW 索引，检索代码也能查询 `ready` 向量。

但 Memory 创建或更新后，没有完整保证：

```text
Memory insert/update
→ 标记向量 pending/stale
→ 持久化异步任务
→ 生成 embedding
→ 校验维度和版本
→ memory_vectors=ready
→ 失败重试
```

因此当前 `hybrid` 很可能主要依赖 Exact Key、Structured、FTS 和 ILIKE，向量路径只有在数据库中已经存在 `ready` Memory Vector 时才工作。

## P0：融合结果会被旧字符评分再次过滤

当前 Candidate 检索大致为：

```text
Repository 多路召回并 RRF
→ 返回 MemoryRecord
→ candidate_similarity() 重新打分
→ 低于固定阈值的结果删除
```

结果是：向量找回了语义相近的 Memory，但因为字面重合较低，仍可能被旧字符评分丢弃。

用户查询也有类似问题：最终 Memory 评分仍高度依赖字符重合和少量固定意图规则。

## P0：中文 FTS 和词法召回较弱

当前主要使用 PostgreSQL `simple` FTS 和 `ILIKE`，存在：

- 中文没有空格；
- 查询词难以拆分；
- 整句可能成为一个 token；
- FTS 索引表达式与查询表达式不完全一致；
- 缺少 `pg_trgm` 作为中文、错别字和专有名词召回通道。

## P0：Candidate 仍不是真正的子句级事实

示例：

```text
今天参加了交流会，我更喜欢小班练习，下周还要报名。
```

应拆成：

```text
Episodic：今天参加了交流会
Preference：更喜欢小班练习
Task：下周报名
```

当前多个 Candidate 可能仍携带整句内容，造成 Memory 内容不纯、Memory Key 不准确、向量主题混杂和关系审理困难。

## P1：短事实 Gate 容易漏掉

可能漏掉：

```text
我是杭州人
我养了一只猫
我会弹吉他
我有两个姐姐
我姓张
```

## P1：多模型插槽存在，但缺少统一策略层

当前问题不是不能选模型，而是缺少统一的任务策略：

- 任务属于哪一类；
- 为什么使用该模型；
- 是否允许升级 Strong；
- 失败时降级到哪个模型；
- 成本和时延预算；
- 模型结果是否允许直接改变 Memory。

因此 `gpt-5.5` 基本没有明确职责，分类和总结等任务也未完整按角色分配。

## P1：Monthly Consolidation 缺少语义聚类

月度整理还没有形成完整的：

```text
Vector clustering
→ 主题识别
→ 重复/冲突组识别
→ Source/日期/范围校验
→ 建议 merge/supersede
→ 确定性安全门
```

## P1：缺少真实质量、性能和成本基线

尚未完整测量：

- Memory Extraction F1；
- Gate Recall；
- Candidate Storage Precision/Recall；
- Destructive False Positive Rate；
- Retrieval Recall@K；
- MRR/NDCG；
- Answer Grounded Accuracy；
- Vector 覆盖率与新鲜度；
- 每 1000 条 Note 的成本；
- Strong 升级比例；
- 10K Memory p95。

---

# 3. 目标架构

## 3.1 记忆写入

```text
Note
│
├─ 本地安全过滤
├─ 轻量 Memory Admission Gate
├─ 子句切分
├─ Rules 高置信度抽取
├─ Fast Model 补充抽取
├─ Candidate 标准化与去重
├─ Candidate Validator
├─ Hybrid Candidate Retrieval
├─ 确定性 Adjudicator
├─ 可选 Strong Advisory Review
├─ 确定性 Safety Gate
├─ Memory Mutation
└─ Transactional Outbox
    └─ Memory Embedding Task
        └─ memory_vectors ready
```

原则：模型负责理解、提议和结构化；本地规则负责最终数据库状态变化。

`gpt-5.5` 不得直接执行删除、覆盖、merge 或 supersede。

## 3.2 查询

```text
用户问题
│
├─ Deterministic Router
│   ├─ 明确命令/type/tag → 直接 SQL
│   ├─ 当前偏好/任务/事实 → Memory Search
│   ├─ 普通单跳问题 → Hybrid Search
│   └─ 复杂多跳问题 → ReAct
│
├─ Hybrid Retrieval
│   ├─ Exact Key
│   ├─ Structured Slot
│   ├─ FTS
│   ├─ Trigram
│   └─ Vector
│
├─ RRF / Weighted Fusion
├─ Policy-aware Rerank
├─ Evidence Selection
├─ Balanced Synthesis
└─ Strong Escalation（仅复杂或低置信度）
```

---

# 4. 多模型任务分工

| 角色 | 默认模型 | 任务 |
|---|---|---|
| `fast` | `gpt-5.4-mini` | Note 分类、意图识别、子句结构化、Memory Candidate 初步抽取、低成本 JSON 规范化 |
| `balanced` | `gpt-5.4` | 普通查询合成、普通总结草稿、复杂 Candidate 补充理解、一般语义判断 |
| `strong` | `gpt-5.5` | 复杂多步查询、长周期 Summary Review、低置信度冲突/替代建议、Monthly Cluster 审阅 |
| `embedding` | 独立 Embedding Model | Note Vector、Memory Vector、语义召回、聚类和近重复检测 |
| `rules` | 本地代码 | 安全、幂等、Memory Key、状态迁移、最终数据库演化、破坏性操作安全门 |

## 4.1 Strong 升级条件

### 查询

- 需要两个以上工具调用；
- 需要跨时间比较；
- 多个证据相互矛盾；
- Balanced 输出自评置信度低；
- 用户明确要求深入综合；
- 证据较多，需要复杂压缩推理。

### Memory

- 可能为 `conflict`；
- 可能发生 `supersede`；
- 多个相关 Memory 分数接近；
- Candidate 置信度处于灰区；
- Rules 与 Balanced 建议不一致；
- Monthly Consolidation 需要主题审阅。

### Summary

- 月度、半年、年度总结；
- Note 数量超过阈值；
- 存在多个任务状态变化或 Memory Conflict；
- Balanced 草稿遗漏高重要度证据。

## 4.2 Strong 禁止事项

Strong 只能返回建议：

```json
{
  "recommended_relation": "conflict",
  "confidence": 0.83,
  "reason": "...",
  "evidence_ids": ["..."]
}
```

不能直接更新 `memories.status`、删除 Memory、执行 merge/supersede/purge。

---

# 5. 实施阶段

## Phase 0：固定基线和功能开关

新增：

```dotenv
SUIXINJI_MODEL_ROUTING_ENABLED=true
SUIXINJI_STRONG_ESCALATION_ENABLED=false
SUIXINJI_MEMORY_VECTOR_LIFECYCLE_ENABLED=false
SUIXINJI_MEMORY_TRIGRAM_ENABLED=false
SUIXINJI_MEMORY_UNIFIED_RERANK_ENABLED=false
SUIXINJI_MEMORY_CLAUSE_EXTRACTION_ENABLED=false
SUIXINJI_MONTHLY_SEMANTIC_CONSOLIDATION_ENABLED=false
```

要求：所有新开关关闭时行为与当前 `main` 一致。

---

## Phase 1：统一 Model Router

新增：

```text
core/model_router.py
core/model_policy.py
```

建议定义：

```python
class ModelRole(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    STRONG = "strong"

class LLMTask(str, Enum):
    NOTE_CLASSIFICATION = "note_classification"
    MEMORY_EXTRACTION = "memory_extraction"
    QUERY_ROUTING = "query_routing"
    QUERY_SYNTHESIS = "query_synthesis"
    QUERY_COMPLEX_REASONING = "query_complex_reasoning"
    SUMMARY_DRAFT = "summary_draft"
    SUMMARY_REVIEW = "summary_review"
    MEMORY_CONFLICT_ADVISORY = "memory_conflict_advisory"
    CONSOLIDATION_CLUSTER_REVIEW = "consolidation_cluster_review"
```

默认策略：

```text
note_classification              → fast
memory_extraction                → fast
query_routing                    → fast
query_synthesis                  → balanced
query_complex_reasoning          → strong
summary_draft                    → balanced
summary_review(day/week)         → balanced
summary_review(month/half/year)  → strong
memory_conflict_advisory         → strong
consolidation_cluster_review     → strong
```

修改：

```text
core/config.py
core/llm_client.py
core/llm_classifier.py
agent/query_agent.py
summary/generator.py
memory/extractor.py
memory/service.py
```

日志必须记录 `llm_task/model_role/model/route_reason/latency/tokens/fallback`。

---

## Phase 2：改进 Admission Gate 和子句级抽取

### Gate 三层

```text
Layer 1：安全/低价值过滤
Layer 2：确定性短事实模式
Layer 3：Fast Model 轻量判断（规则不确定时）
```

覆盖模式：

```text
我是...
我姓...
我来自...
我有...
我养了...
我会...
我不会...
我在...工作
我的...是...
...是我的...
```

新增：

```text
memory/clause_splitter.py
```

抽取结果必须包含 `evidence_span` 和 `clause_index`。

去重键：

```text
note_id + clause_index + memory_type + memory_key + evidence_span
```

修改：

```text
memory/extractor.py
memory/models.py
memory/candidate_validator.py
memory/service.py
memory/prompts.py
apps/handlers.py
```

验收：短事实 Admission Recall ≥ 95%；多事实句 Candidate Recall ≥ 90%。

---

## Phase 3：完成 Memory Vector 生命周期

统一状态：

```text
pending → processing → ready
                   ↘ failed
ready → stale → pending
```

维护字段：

```text
memory_id
embedding
model
dimension
content_hash
embedding_version
status
attempt_count
next_retry_at
last_error
updated_at
```

内容哈希建议包含：

```text
memory_type + subject + predicate + object_value + content
+ embedding_model + dimension + embedding_version
```

Memory 事务中只做：

```text
Memory mutation
→ upsert vector state pending/stale
→ enqueue durable outbox task
→ commit
```

禁止在 Memory 数据库事务中调用 Embedding API。

新增任务类型：

```text
memory_embedding
```

Worker：

1. 读取 Memory；
2. 重算 hash；
3. 过期任务直接 skip；
4. 状态改为 processing；
5. 调用 Embedding；
6. 校验维度；
7. 原子更新 ready；
8. 失败写 failed 并重试。

不迁移旧系统，只对 `suixinji_v2` 当前 active Memory 幂等创建任务：

```bash
python scripts/backfill_memory_vectors.py --status active --dry-run
python scripts/backfill_memory_vectors.py --status active --execute
```

修改：

```text
infrastructure/schema.py
alembic/versions/202607xx_0008_memory_vector_lifecycle.py
repositories/postgres/memory.py
repositories/postgres/dispatch.py
apps/handlers.py
runtime/streams/*
scripts/backfill_memory_vectors.py
```

验收：active Vector 覆盖率 ≥ 99%，Freshness p95 < 60 秒，stale Vector 查询率为 0。

---

## Phase 4：重构 Hybrid Retrieval

新增：

```python
@dataclass
class MemoryRetrievalHit:
    memory: MemoryRecord
    exact_rank: int | None
    structured_rank: int | None
    fts_rank: int | None
    trigram_rank: int | None
    vector_rank: int | None
    exact_score: float
    structured_score: float
    fts_score: float
    trigram_score: float
    vector_score: float
    rrf_score: float
    policy_score: float
    final_score: float
    reasons: list[str]
```

Repository 不再只返回裸 `MemoryRecord`。

### Structured

禁止只用 `subject=用户`，改为组合：

```text
memory_type + predicate
subject + predicate
predicate + object_value
entity + memory_type
exact memory_key
```

### FTS

新增生成列：

```sql
search_document tsvector
GENERATED ALWAYS AS (
  to_tsvector(
    'simple',
    coalesce(content, '') || ' ' ||
    coalesce(subject, '') || ' ' ||
    coalesce(predicate, '') || ' ' ||
    coalesce(object_value, '')
  )
) STORED
```

索引和查询统一使用 `search_document`。

### Trigram

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ... ON memories USING gin (content gin_trgm_ops);
CREATE INDEX ... ON memories USING gin (object_value gin_trgm_ops);
```

### Vector

- 维度读取 `EMBEDDING_DIMENSION`，不写死 1024；
- 只检索 ready、hash 新鲜、model/version/dimension 匹配的向量。

### 融合

```text
final_retrieval_score =
RRF(exact, structured, fts, trigram, vector)
+ exact_key_boost
+ policy_compatibility
```

删除融合后的字符硬阈值。`candidate_similarity()` 只能作为特征，不能作为最终硬过滤器。

修改：

```text
memory/retrieval_models.py
memory/candidate_retriever.py
memory/retrieval.py
memory/adjudicator.py
memory/service.py
memory/repository.py
repositories/postgres/memory.py
infrastructure/schema.py
alembic/versions/202607xx_0009_memory_search_document_trgm.py
```

---

## Phase 5：Query 与 Summary 模型升级

### Query

```text
Fast：意图和复杂度判断
Balanced：普通查询合成
Strong：复杂多步、跨时间、冲突证据综合
```

路由输出：

```json
{
  "route": "memory_search",
  "complexity": "simple",
  "model_role": "balanced",
  "strong_escalation": false,
  "reason": "current_preference"
}
```

### Summary

```text
今日/昨日：balanced draft
周总结：balanced draft + balanced review
月/半年/年：balanced draft + strong review
```

### Memory Advisory

只有 `possible_conflict / possible_supersede / ambiguous_merge / multiple_close_targets` 才调用 Strong。

修改：

```text
agent/query_agent.py
summary/generator.py
memory/adjudicator.py
memory/service.py
core/model_router.py
core/model_policy.py
```

---

## Phase 6：Monthly Semantic Consolidation

```text
Active Memory
→ 按 memory_type 初分
→ Vector KNN / clustering
→ cluster candidates
→ Source、日期、scope、polarity 校验
→ Strong 仅做命名和关系建议
→ 确定性 Policy Gate
→ merge / pending_review / no-op
```

自动 merge 必须满足同类型、无 polarity 冲突、scope 兼容、来源充分、主题一致和严格相似度门槛。

自动 supersede 必须有明确时间顺序、明确替代表达、相同 slot/key 和高置信度，否则进入 `pending_review`。

---

## Phase 7：评测与上线

先建立 150 条 Golden Set，再扩展到 1000 条。

### Memory 指标

| 指标 | 目标 |
|---|---:|
| Admission Recall | ≥ 95% |
| Candidate Type Macro-F1 | ≥ 90% |
| Candidate Storage Precision | ≥ 95% |
| Destructive False Positive Rate | ≤ 0.5% |
| Evidence Span Validity | 100% |

### Retrieval 指标

| 指标 | 目标 |
|---|---:|
| Recall@20 | ≥ 95% |
| MRR | ≥ 0.85 |
| Exact Key Recall | 100% |
| Broad Profile Recall | ≥ 90% |

### Vector 指标

| 指标 | 目标 |
|---|---:|
| Active Vector Coverage | ≥ 99% |
| Freshness p95 | < 60 秒 |
| Dimension Mismatch Silent Failure | 0 |
| Stale Vector Query Rate | 0 |

### Model Routing 指标

| 指标 | 目标 |
|---|---:|
| 普通 Note 使用 Fast | ≥ 95% |
| 普通查询使用 Strong | ≤ 10% |
| 月/半年/年 Summary Strong Review | 100% |
| 路由日志完整率 | 100% |
| 模型直接破坏 Memory | 0 |

保留模式：

```dotenv
SUIXINJI_MEMORY_RETRIEVAL_MODE=legacy|hybrid_v1|hybrid_v2
SUIXINJI_MODEL_ROUTING_ENABLED=true|false
SUIXINJI_STRONG_ESCALATION_ENABLED=true|false
```

上线顺序：

```text
1. Model Router（不改变默认模型）
2. Clause Extraction + Gate
3. Memory Vector Lifecycle
4. Hybrid Retrieval V2 Shadow
5. Hybrid Retrieval V2 Read
6. Strong Query Escalation
7. Strong Summary Review
8. Monthly Semantic Consolidation
```

---

# 6. 推荐提交拆分

```text
feat(model-router): centralize task-to-model policy
test(model-router): cover routing fallback and observability

feat(memory): add clause-level candidate extraction
test(memory): cover short facts and multi-clause notes

feat(memory-vector): add durable embedding lifecycle
feat(worker): process memory embedding tasks
feat(memory-vector): add idempotent backfill command
test(memory-vector): cover stale refresh retries and dimension checks

feat(memory-retrieval): preserve channel scores in retrieval hits
feat(memory-retrieval): add generated FTS document and pg_trgm
feat(memory-retrieval): replace post-fusion character hard filter
test(memory-retrieval): add Chinese synonym and typo retrieval cases

feat(query): add complexity-aware strong escalation
feat(summary): route long-range reflection to strong model
test(model-routing): assert model role by task

feat(memory): implement semantic monthly consolidation
test(memory): enforce destructive-action safety gates

feat(eval): add memory/retrieval/model-routing benchmark suite
docs: publish measured quality latency and cost report
```

---

# 7. 重点文件

```text
.env.example
core/config.py
core/settings.py
core/llm_client.py
core/llm_classifier.py
core/model_router.py
core/model_policy.py
agent/query_agent.py
summary/generator.py
memory/clause_splitter.py
memory/extractor.py
memory/models.py
memory/prompts.py
memory/candidate_validator.py
memory/candidate_retriever.py
memory/retrieval.py
memory/retrieval_models.py
memory/adjudicator.py
memory/service.py
memory/consolidation.py
memory/scheduler.py
memory/policies/*
memory/repository.py
repositories/postgres/memory.py
repositories/postgres/dispatch.py
infrastructure/schema.py
apps/handlers.py
runtime/streams/*
scripts/backfill_memory_vectors.py
tests/test_model_router.py
tests/test_memory_clause_extraction.py
tests/test_memory_vector_lifecycle.py
tests/test_memory_hybrid_retrieval_v2.py
tests/test_memory_model_escalation.py
tests/test_monthly_semantic_consolidation.py
eval/memory/*
docs/metrics/*
```

---

# 8. Codex 执行要求

## 第一步：只读确认

先输出：

1. 当前 Memory Vector 的所有写入位置；
2. 所有 `complete_json()` 调用位置及实际模型角色；
3. Candidate 与 User Query 的完整检索调用链；
4. 当前 Consolidation 实现；
5. 上述问题是否与当前 `main` 一致；
6. 不修改代码。

## 第二步：按 Phase 实施

每个 Phase：

- 独立分支；
- 独立测试；
- 独立报告；
- 不混入无关重构；
- 不删除当前稳定性修复；
- 不清空 PostgreSQL/Redis；
- 不同步批量调用 Embedding；
- Backfill 默认 `--dry-run`。

## 第三步：每阶段输出

- 根因；
- 设计；
- Diff；
- 测试；
- 迁移；
- 部署；
- 回滚；
- 指标；
- 未完成风险。

---

# 9. 最终验收标准

1. 每条 active Memory 都能获得可追踪、可刷新的 Vector。
2. 新 Memory 和更新后的 Memory 不会长期使用 stale Vector。
3. Hybrid Retrieval 保留各通道分数，不再被旧字符阈值误删。
4. Exact、Structured、FTS、Trigram、Vector 五路召回可解释。
5. 中文短语、同义表达和少量错别字能正常召回。
6. 多事实消息能生成子句级 Candidate。
7. 短事实不会因长度短而被 Gate 漏掉。
8. `gpt-5.4-mini`、`gpt-5.4`、`gpt-5.5` 有明确职责。
9. 普通低风险任务不会滥用 `gpt-5.5`。
10. `gpt-5.5` 不能直接执行破坏性 Memory 操作。
11. Monthly Consolidation 使用语义聚类和严格安全门。
12. 质量、延迟、成本和模型路由有真实可重复指标。
13. 所有改造可通过功能开关独立回滚。
