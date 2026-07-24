# 随心记优化第一阶段设计书：记忆正确性与真实混合检索

## 1. 文档信息

- 项目：Suixinji
- 阶段：第一阶段
- 主题：记忆正确性、真实混合检索、语义 Consolidation
- 当前基线：`main@9977ac5`
- 前置状态：
  - Candidate 持久化、`memory_key`、人工审核、版本/来源/Decision/Trace 已实现；
  - 关系审理离线集准确率达到 100%，破坏性误判率为 0%；
  - 抽取类型 Macro-F1 仍为 66.53%，端到端准确率为 83.33%；
  - 当前 Candidate Retrieval 和用户 Memory Search 仍主要依赖固定条数查询及 Python 规则评分；
  - `memory_vectors` 表已存在，但尚未成为主检索链路。

---

## 2. 阶段目标

第一阶段解决“系统记得对不对、能不能在大量记忆中找到正确内容”的问题。

### 2.1 核心目标

1. 将 Candidate Retrieval 和用户 Memory Search 改为真正的混合检索：
   - `memory_key` 结构化召回；
   - PostgreSQL 全文检索；
   - pgvector 语义召回；
   - 统一合并与重排。

2. 提升候选记忆抽取质量，重点修复：
   - episodic 事件漏抽取；
   - 普通 semantic 事实过度归入 `semantic:用户:fact`；
   - 复杂否定、状态变化、比较偏好的错误抽取。

3. 细化 `memory_key`，降低不相关 Memory 进入同一审理池的概率。

4. 将 Monthly Consolidation 改造成真正的语义聚类，而不是按 predicate 粗分组并拼接文本。

5. 消除 Rules 模式下 Candidate 重复提取。

6. 建立规则、LLM、Hybrid 三种模式的真实质量、延迟和成本基线。

### 2.2 非目标

本阶段不以大规模吞吐量为主要验收目标，不大幅修改 Redis Streams、Worker 拓扑和 Receiver。性能优化仅限于避免记忆检索产生新的明显退化。

---

## 3. 当前问题

### 3.1 Candidate Retrieval 不是真正混合召回

当前关键位置：

- `memory/candidate_retriever.py::retrieve_candidates`
- `repositories/postgres/memory.py::list_adjudication_candidates`

当前流程：

```text
同 space、active、同 memory_type
→ memory_key 相同优先
→ 最多取 200 条
→ Python 字符重合与规则评分
→ Top K
```

问题：

- 超过 200 条后的 Memory 永远无法参与审理；
- 语义相近但词面差异大的内容召回困难；
- `memory_vectors` 未进入主链路；
- 无全文索引和统一融合策略。

### 3.2 用户 Memory Search 固定扫描 100 条

当前关键位置：

- `repositories/postgres/memory.py::search_memories`
- `memory/retriever.py::score_memory`

问题：

- 只对最多 100 条 Memory 做 Python 评分；
- 真实用户 Memory 增长后会发生系统性漏召回；
- 当前 Recall@20=100% 只能代表固定离线集。

### 3.3 抽取质量不足

当前关键位置：

- `memory/extractor.py`
- `memory/prompts.py`
- `memory/candidate_validator.py`

问题：

- 规则实体和关键词覆盖有限；
- episodic 只有在没有其他 Candidate 时才容易生成；
- 复杂句可同时包含任务、偏好和事件，但当前规则可能漏掉其中一类；
- 真实 LLM/Embedding 质量尚未测量。

### 3.4 Semantic Key 过粗

当前关键位置：

- `memory/models.py::memory_key_for`

当前一般 semantic key：

```text
semantic:{subject}:{predicate}
```

普通事实容易退化为：

```text
semantic:用户:fact
```

导致无关事实进入同一审理池。

### 3.5 Monthly Consolidation 仍是假聚类

当前关键位置：

- `memory/consolidator.py::generate_stable_semantic`

当前只是：

```text
按 predicate 分组
→ 取最大组
→ 拼接前几条内容
→ 生成 stable_theme
```

尚未验证语义一致性、时间跨度、独立来源数和冲突情况。

### 3.6 Candidate 重复提取

当前关键位置：

- `apps/handlers.py::handle_ingest`
- `memory/service.py::_process_note_memory_impl`

Rules 模式中 Ingest 为判断是否需要创建 Memory Task 先抽取一次，Memory 流程再次抽取。

---

## 4. 总体设计

```text
Note
  │
  ▼
Extraction
Rules / LLM / Hybrid
  │
  ▼
Candidate Persistence
状态、证据、模型、Prompt Hash
  │
  ▼
Candidate Validation
  │
  ▼
Hybrid Candidate Retrieval
┌──────────────┬──────────────┬──────────────┐
│ memory_key   │ PostgreSQL   │ pgvector     │
│ exact/slot   │ FTS/lexical  │ semantic     │
└──────────────┴──────────────┴──────────────┘
                 │
                 ▼
          Result Fusion
        RRF + policy rerank
                 │
                 ▼
           Adjudication
                 │
                 ▼
       Evolution Transaction
                 │
                 ▼
Memory / Source / Version / Decision / Relation
```

用户查询链路：

```text
Query Router
  │
  ├─ 当前偏好/任务/事实 → Hybrid Memory Search
  ├─ 历史原话/时间问题 → Note Search
  └─ 复杂多跳问题       → ReAct
```

---

## 5. 数据模型设计

### 5.1 `memory_vectors`

当前已有：

```text
memory_id
embedding Vector(1024)
model
created_at
updated_at
```

调整建议：

1. 增加：
   - `dimension`
   - `content_hash`
   - `embedding_version`
   - `status`
   - `last_error`

2. 不直接依赖固定 1024 维。两种可选方案：

方案 A，当前阶段优先：

- 固定一个生产 Embedding 模型；
- 保持单一维度；
- 迁移中明确写死模型和维度；
- 切换模型时执行全量重建。

方案 B，后续扩展：

- 不同模型分表；
- 或用不同列保存不同维度；
- 不建议在同一 pgvector 列中混合维度。

### 5.2 Memory 全文检索字段

新增 PostgreSQL 生成列或表达式索引：

```text
search_document =
  setweight(to_tsvector(content), 'A')
  + setweight(to_tsvector(subject), 'B')
  + setweight(to_tsvector(predicate), 'B')
  + setweight(to_tsvector(object_value), 'B')
```

中文分词需要明确选型：

- 第一版可使用 `simple` 配置加 trigram；
- 若部署允许，再评估 `zhparser`；
- 不把额外 PostgreSQL 扩展作为第一阶段硬依赖。

推荐索引：

```sql
GIN (to_tsvector('simple', content))
GIN (content gin_trgm_ops)
BTREE (space_id, memory_type, status, memory_key)
BTREE (space_id, status, updated_at DESC)
HNSW / IVFFlat (embedding vector_cosine_ops)
```

### 5.3 `memory_key` 版本化

新增：

```text
memory_key_version
```

建议值：

```text
memory-key-v2
```

用于区分旧 key 和新 key，避免算法升级后无法解释历史 Decision。

---

## 6. Memory Key V2

### 6.1 Preference

```text
preference:{subject}:{topic}:{scope}
```

不包含正负 polarity，使正面和负面偏好进入同一槽位。

示例：

```text
用户喜欢咖啡
用户不喝咖啡
→ preference:user:coffee:global
```

### 6.2 Task

```text
task:{subject}:{normalized_identifier}
```

任务状态不进入 key。

示例：

```text
修改 README
README 已完成
→ task:user:readme
```

### 6.3 Semantic

按 predicate 类型分别定义。

槽位型 predicate：

```text
location
current_project
current_employer
learning_focus
```

Key：

```text
semantic:{subject}:{predicate}
```

实体型或开放事实：

```text
semantic:{subject}:{predicate}:{topic_or_entity}
```

示例：

```text
用户养了一只猫
→ semantic:user:pet:cat

用户出生于杭州
→ semantic:user:birthplace
```

禁止所有普通事实统一退化为：

```text
semantic:user:fact
```

无法结构化时，使用稳定主题摘要或实体哈希：

```text
semantic:user:fact:{topic_hash}
```

### 6.4 Episodic

Episodic 不作为可覆盖槽位，Key 应包含事件和时间：

```text
episodic:{subject}:{event_type}:{date_bucket}:{event_hash}
```

---

## 7. 混合召回设计

### 7.1 Candidate Retrieval

输入：

```text
space_id
MemoryCandidate
memory_type
memory_key
content
entities
valid time
```

四路召回：

#### A. Exact Key Recall

```sql
WHERE space_id = ?
  AND memory_type = ?
  AND status = 'active'
  AND memory_key = ?
```

Top 20。

#### B. Structured Recall

按：

- subject；
- predicate；
- entity；
- task identifier；
- preference topic；
- valid time。

Top 30。

#### C. Full-Text Recall

使用 FTS 和 trigram。

Top 30。

#### D. Vector Recall

对 Candidate 内容生成 Embedding，pgvector cosine Top 30。

### 7.2 结果融合

使用 Reciprocal Rank Fusion：

```text
RRF score = Σ 1 / (k + rank_i)
```

建议 `k=60`。

增加规则加权：

- exact memory_key：额外加权；
- 同 subject/predicate：加权；
- valid_until 已过期：过滤；
- preference scope 不兼容：过滤；
- task identifier 不兼容：过滤。

最终进入 Adjudicator 的数量：

```text
Top 20
```

### 7.3 用户 Memory Search

用户查询同样执行：

- 结构化意图路由；
- FTS；
- Vector；
- 类型过滤；
- active/时间过滤；
- RRF；
- 最终业务评分。

禁止先 `list_memories(limit=100)` 再纯 Python 排序。

### 7.4 降级策略

Embedding API 不可用时：

```text
Exact Key + Structured + FTS
```

FTS 不可用时：

```text
Exact Key + Structured + trigram
```

所有外部依赖失败都不能阻塞确定性审理主流程。

---

## 8. Candidate Extraction V2

### 8.1 输出契约

每个 Candidate 必须包含：

```text
memory_type
content
subject
predicate
object_value
evidence_span
confidence
importance
valid_from
valid_until
polarity
scope
extraction_reason
```

### 8.2 多候选支持

一条 Note 可同时抽取多类 Candidate。

示例：

```text
今天参加了日语交流会，发现我更喜欢小班练习，
下周要继续报名。
```

应生成：

1. episodic：今天参加日语交流会；
2. preference：更喜欢小班练习；
3. task：下周继续报名。

### 8.3 Rules 模式

规则只承担：

- 明确偏好；
- 明确任务状态；
- 明确位置变化；
- 明确低价值过滤；
- LLM失败时降级。

不继续无限扩展硬编码实体表。

### 8.4 LLM 模式

- fast 模型用于 Candidate Extraction；
- 必须返回 JSON；
- evidence_span 必须是 Note 原文子串；
- 不满足契约的 Candidate 丢弃；
- 输出最多 5 条。

### 8.5 Hybrid 模式

建议执行顺序：

```text
Rules high-confidence
+ LLM supplemental extraction
→ candidate dedupe
→ validator
```

规则高置信 Candidate 不需要被 LLM 覆盖。

---

## 9. 消除重复提取

### 9.1 推荐方案

Ingest 阶段不再完整调用 `extract_candidates()`。

新增轻量函数：

```python
memory/extractor.py::may_contain_memory(text, classification) -> bool
```

它只做：

- 空文本；
- 低价值文本；
- 敏感信息；
- 少量显式标记判断。

若返回 False：

```text
直接写 extraction_state=empty
不创建 Memory Task
```

若返回 True 或不确定：

```text
创建 Memory Task
由 Memory Worker 只提取一次
```

### 9.2 备选方案

Ingest 完整提取后立即持久化 Candidate，Memory Worker 直接读取 Candidate。

不推荐把完整 Candidate 放进 Redis Payload，因为 Candidate 是业务事实，应以 PostgreSQL 为准。

---

## 10. Semantic Consolidation V2

### 10.1 Daily

直接查询：

```text
memory_extraction_states in pending/failed/partial
或没有 extraction_state 的 Note
```

不再 `load_index(space_id)` 后扫描。

### 10.2 Weekly Episodic Dedup

流程：

```text
按时间桶分组
→ Vector Top K
→ FTS/实体过滤
→ 近重复判断
→ 合并来源
```

避免 100 条两层循环。

### 10.3 Monthly Stable Semantic

流程：

```text
取时间窗口内 active episodic
→ 生成/读取 embedding
→ 聚类
→ 检查独立日期数
→ 检查独立来源数
→ 检查主题一致性
→ 检查冲突
→ 生成 Semantic Candidate
→ 正常进入 Hybrid Retrieval 和 Adjudication
```

最低门槛建议：

```text
独立来源 >= 3
独立日期 >= 2
聚类平均相似度 >= 0.75
冲突率 <= 0.1
```

禁止直接拼接五条事件作为稳定事实。

---

## 11. 代码改造位置

### 核心文件

| 文件 | 改造内容 |
|---|---|
| `memory/models.py` | Memory Key V2、key version、候选字段 |
| `memory/extractor.py` | Extraction V2、轻量预筛选、单次提取 |
| `memory/candidate_retriever.py` | 四路召回、RRF、降级 |
| `memory/retriever.py` | 用户查询混合召回后的业务重排 |
| `memory/adjudicator.py` | 使用融合结果，保留类型策略 |
| `memory/consolidator.py` | Daily SQL恢复、Weekly向量去重、Monthly聚类 |
| `memory/service.py` | Candidate生命周期、单次提取、Trace |
| `repositories/postgres/memory.py` | FTS、Vector、结构化查询、批量读取 |
| `infrastructure/schema.py` | FTS/Vector元数据、索引 |
| `core/llm_client.py` | Embedding版本和模型信息 |
| `eval/eval_memory.py` | 真实三模式评测 |
| `apps/handlers.py` | 移除完整预提取 |

### 新增建议

```text
memory/hybrid_retriever.py
memory/embedding_service.py
memory/key_builder.py
memory/clustering.py
eval/memory/realistic_cases.jsonl
docs/metrics/stage6_memory_quality.json
```

### Migration

新增：

```text
20260718_0007_memory_hybrid_retrieval
```

包含：

- `memory_key_version`；
- Memory全文索引；
- Vector索引；
- Vector元数据字段；
- 必要的回填状态字段。

---

## 12. 测试设计

### 12.1 单元测试

1. Memory Key V2：
   - 同一偏好正负句 key 一致；
   - 不同 semantic 事实 key 不冲突；
   - task 状态变化 key 不变。

2. Hybrid Retrieval：
   - exact key 一定进入 Top K；
   - 词面不同、语义相近可被向量召回；
   - Embedding失败可降级；
   - 过期 Memory 不返回。

3. Extraction：
   - 一条 Note 多 Candidate；
   - evidence_span 验证；
   - episodic；
   - 复杂否定；
   - 敏感信息。

4. Consolidation：
   - 不同主题不聚类；
   - 来源不足不生成；
   - 有冲突不生成；
   - 重跑幂等。

### 12.2 集成测试

- PostgreSQL FTS；
- pgvector Top K；
- RRF稳定性；
- Candidate写入、Decision、Evolution事务；
- Candidate失败后仅重试该 Candidate；
- 人工审核闭环。

### 12.3 真实质量集

至少扩展到 1000 条：

```text
Extraction 350
Relation 250
Retrieval 200
End-to-End 200
```

覆盖：

- 口语；
- 错别字；
- 否定；
- 比较；
- 时间变化；
- 指代；
- 多意图；
- 跨 Note 演化。

分别运行：

```text
rules
llm
hybrid
```

---

## 13. 指标和验收标准

### 13.1 正确性硬指标

| 指标 | 当前 | 第一阶段目标 |
|---|---:|---:|
| Extraction Type Macro-F1 | 66.53% | ≥ 82% |
| Candidate Storage F1 | 未完整测 | ≥ 90% |
| Relation Macro-F1 | 100% 固定集 | 新扩展集 ≥ 92% |
| Destructive False Positive | 0% 固定集 | 新扩展集 ≤ 2% |
| Candidate Recall@20 | 100% 固定集 | 10K Memory下 ≥ 95% |
| Retrieval MRR | 94.17% | ≥ 90% |
| End-to-End Accuracy | 83.33% | ≥ 90% |

### 13.2 性能保护指标

在 10K Memory 单 Space：

```text
Hybrid Retrieval p95 <= 500 ms，不含外部 Embedding首次调用
缓存命中 p95 <= 150 ms
SQL次数 <= 6
```

### 13.3 成本指标

记录：

```text
每1000条Note的LLM请求数
每1000条Note的Embedding请求数
Token输入/输出
缓存命中率
估算费用
```

不以“费用下降百分比”作为本阶段硬验收，但必须形成真实基线。

---

## 14. 实施顺序

### 任务 1：Memory Key V2

- 新 Key Builder；
- 回填策略；
- 版本字段；
- 兼容旧 Key。

### 任务 2：Memory FTS 和 Vector Repository

- 索引；
- Vector写入；
- FTS查询；
- 批量接口。

### 任务 3：Hybrid Retrieval

- Candidate Retrieval；
- Query Retrieval；
- RRF；
- 降级。

### 任务 4：Extraction V2

- 多候选；
- Episodic；
- 真实 LLM契约；
- 去掉重复提取。

### 任务 5：Consolidation V2

- Daily SQL；
- Weekly近重复；
- Monthly聚类。

### 任务 6：评测和报告

- 1000条质量集；
- 三模式对比；
- 成本和延迟报告。

---

## 15. 风险和回滚

### 风险

1. Embedding模型切换导致向量维度不兼容；
2. 新 Key 使旧 Memory 无法正确匹配；
3. FTS中文效果有限；
4. LLM抽取增加费用和随机性；
5. Hybrid Retrieval 增加查询延迟。

### 回滚

- 保留 `SUIXINJI_MEMORY_RETRIEVAL_MODE=legacy|hybrid`；
- 保留 `memory_key_version`；
- 新索引和向量表可独立停用；
- LLM失败自动走 Rules；
- Migration必须支持 downgrade；
- 旧质量集继续作为回归集。

---

## 16. 阶段完成定义

同时满足以下条件才算完成：

1. Candidate和Query两条主链路均使用真实Hybrid Retrieval；
2. 不再固定扫描100/200条Memory；
3. Rules模式不重复完整提取Candidate；
4. Monthly Consolidation使用语义一致性和时间/来源门槛；
5. 真实扩展质量集达到验收目标；
6. 真实LLM/Embedding成本和延迟被记录；
7. 全仓测试、Ruff和Migration Round Trip通过。
