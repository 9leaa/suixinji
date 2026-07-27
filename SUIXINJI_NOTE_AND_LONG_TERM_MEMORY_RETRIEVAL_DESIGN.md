# 随心记 Note 与长期 Memory 双层检索设计

> 状态：设计提案  
> 版本：V1.0  
> 日期：2026-07-25  
> 适用项目：`/home/zcj/suixinji`

## 0. 结论

随心记不应把“普通笔记”和“长期记忆”当成两套相似的文本库。

- **Note（普通笔记）是证据层**：保存用户真实说过的话、发生过的事情及上下文，主要目标是高召回、可追溯、不丢历史。
- **Long-term Memory（长期记忆）是派生状态层**：从 Note 中抽取任务、偏好、稳定事实和重要事件，主要目标是当前状态正确、身份稳定、演化可控。

两者应共享一个 Query Planner，但使用不同的 Retriever 和 Reranker：

```text
用户问题
  ↓
QueryIntent + 时间范围 + 实体/主题识别
  ↓
┌──────────────────┬────────────────────┐
│ 长期状态问题       │ 历史/上下文问题      │
│ Memory First      │ Note First          │
└──────────────────┴────────────────────┘
  ↓                         ↓
Memory Retriever       Note Retriever
  ↓                         ↓
规则与状态排序          RRF + 可选 Cross-Encoder
  └──────────────┬──────────┘
                 ↓
       基于证据生成答案并附来源
```

核心原则是：

1. **Note 负责“用户说过什么”**。
2. **Memory 负责“系统当前相信什么”**。
3. **Memory 的状态正确性不能交给相似度模型决定**。
4. **Note 的语义召回不能只依赖精确关键词**。
5. **检索只能决定看哪些候选，不能越权修改 Memory**。

---

## 1. 当前实现快照

截至 2026-07-25，当前飞书测试空间的实际状态为：

| 项目 | 当前状态 |
|---|---:|
| Note 数量 | 50 |
| 已有 Note Embedding | 49 |
| Active Memory | 24 |
| Memory Vector | 0 |
| QueryIntent | 已启用 |
| Memory Barrier | 已启用 |
| Memory Retrieval Mode | `hybrid` |
| Memory Hybrid Vector 开关 | 已启用 |
| Memory Vector Lifecycle | 未启用 |
| Trigram | 未启用 |
| Unified Rerank | 未启用 |

这意味着当前真实能力是：

### Note

- 普通语义查询主要使用向量余弦相似度；
- type、tag、recent 使用结构化过滤；
- Memory 查询无结果时，Note 有 FTS/词法回退；
- 最新未增强 Note 有本地词法 read-after-write；
- 没有统一的关键词 + 向量 + RRF 主链路；
- 没有真正的 Cross-Encoder 或 LLM Reranker。

### Long-term Memory

- 使用结构化字段、关键词、PostgreSQL FTS 多路召回；
- 使用 RRF 合并已有召回通道；
- 最终使用文本重合、类型、状态、时效、置信度等规则计分；
- 虽然 Hybrid Vector 开关已开，但 Vector Lifecycle 关闭且没有 ready vector，因此实际没有向量通道；
- 当前代码中的 `rerank` 主要是流程名称或规则排序，不是模型重排。

---

## 2. 双层数据职责

### 2.1 Note：不可替代的证据层

Note 保存：

- 飞书原始消息或安全处理后的消息；
- 时间、标题、分类、标签；
- 摘要和正文；
- 与其他 Note 的关联；
- Embedding；
- enrichment 状态；
- 敏感性状态。

Note 应尽量保持原始含义，不因为后来观点变化而覆盖旧内容。

例如：

```text
7 月 1 日：我住在上海
7 月 20 日：我已经搬到北京
```

两条 Note 都应保留，因为它们是历史证据。

### 2.2 Long-term Memory：版本化的当前状态层

Memory 保存：

- `memory_type`；
- canonical identity；
- 当前内容和状态；
- task status 或 preference polarity；
- valid_from / valid_until；
- version；
- source Note；
- conflict / pending_review；
- 决策理由和 Trace。

上例在 Memory 中应表示为：

```text
当前居住地：北京
来源：7 月 20 日的 Note
旧版本：上海
```

Memory 不是 Note 的压缩副本，而是带身份、状态、版本和证据的派生视图。

### 2.3 写入关系

```text
飞书消息
  ↓
敏感信息与命令入口校验
  ↓
WAL / Inbox
  ↓
Note 持久化（证据先落库）
  ↓
Note Enrichment + Note Embedding
  ↓
Memory Candidate 抽取
  ↓
Canonicalizer
  ↓
Relation Guard + State Machine
  ↓
Insert / Add Source / Update Version / Pending Review
```

必须始终满足：

- Memory 可以从 Note 派生；
- Memory 必须能回到来源 Note；
- 删除或修正 Memory 不应篡改原始 Note；
- Note 暂未完成 LLM 增强时，也必须能够被即时查询；
- 敏感消息不能进入 Note、Embedding 或 Memory。

---

## 3. 统一查询路由

### 3.1 QueryIntent

查询入口先生成结构化意图：

```python
class QueryIntent:
    intent: Literal[
        "task_status",
        "preference",
        "current_fact",
        "note_history",
        "recent_notes",
        "relationship",
        "summary",
        "general_search",
    ]
    entity: str | None
    attribute: str | None
    topic: str | None
    time_scope: Literal["current", "history", "recent", "all"] | None
    complexity: Literal["simple", "multi_hop", "summary"] | None
    confidence: float
```

QueryIntent 负责路由，不负责决定答案。

### 3.2 路由表

| 用户问题 | 主检索 | 辅助检索 |
|---|---|---|
| 当前任务做到哪了 | Memory First | 无结果再查 Note |
| 我喜欢喝什么 | Memory First | 无结果再查 Note |
| 我现在住哪里 | Memory First | 无结果再查 Note |
| 我上个月说过什么 | Note First | Memory 作为主题提示 |
| 找我提过 Agent 简历的消息 | Note First | Memory 作为 query expansion |
| 最近有哪些笔记 | Note metadata | 不需要向量 |
| 这几个月的 RAG 学习有什么变化 | Note + Memory | 子问题分解 |
| 两件事情有什么关系 | Note graph + hybrid | Memory 提供当前背景 |

### 3.3 Read-after-write

当用户刚发送消息，Note 已落库但 LLM、Embedding 或 Memory 尚未完成时：

```text
问题
  ↓
先查 provisional / enriching Note
  ↓
本地词法命中
  ↓
立即基于原文回答
  ↓
明确提示后台仍在完善分类或长期记忆
```

这一条路径不能等待 QueryIntent LLM、Embedding 或 Memory Worker。

---

## 4. Long-term Memory 检索设计

### 4.1 目标

Long-term Memory 检索优先保证：

1. 当前有效状态正确；
2. 不返回已 superseded / forgotten 的值；
3. 不把 `pending_review` 当成已确认事实；
4. 同一任务的身份与版本稳定；
5. 能处理用户换一种说法后的查询；
6. 查询结果能解释为什么命中。

### 4.2 召回通道

#### 通道 A：精确结构化召回

适用于 task、preference、stable semantic：

```text
memory_type
+ canonical key
+ entity
+ attribute
+ operation
+ scope
+ active status
```

精确 key 命中不需要与模糊候选竞争，直接进入候选集首位。

#### 通道 B：结构化宽召回

当 QueryIntent 的槽位不完整时：

```text
同 memory_type
+ entity / attribute / operation 任意强信号
+ 当前有效时间范围
```

结构化宽召回只用于用户查询；写入审理仍由 Relation Guard 决定能否修改目标。

#### 通道 C：Sparse / Lexical

组合：

- canonical topic；
- content；
- subject；
- predicate；
- object value；
- ASCII 标识符；
- PostgreSQL FTS；
- 中文 2～4 字片段；
- 可选 pg_trgm。

这一通道负责：

- 项目名；
- 人名；
- Ticket、版本号；
- 英文缩写；
- 用户省略“的”等连接词；
- 拼写或空格轻微变化。

#### 通道 D：Dense Vector

为每条 active Memory 建立独立 Memory Embedding。

建议 Embedding 文本包含：

```text
类型：task
实体：用户
属性：RAG 学习
动作：学习
状态：完成
内容：已经完成 RAG 检索部分的学习
```

不要只向量化自然语言 `content`，否则状态词和身份字段容易被稀释。

Memory 更新版本后：

- 原 vector 标记 stale；
- 新版本异步生成 vector；
- Embedding 失败不阻断 Memory 写入；
- 查询时自动降级到结构化和 Sparse 通道；
- 只检索与当前 embedding contract 一致的 ready vector。

### 4.3 候选融合

精确 canonical key 先进入固定高优先级区，其余通道使用 Weighted RRF：

```text
score(d) = Σ channel_weight / (rrf_k + rank_channel(d))
```

建议初始权重：

| 通道 | 权重 |
|---|---:|
| 精确 canonical key | 不参与普通 RRF，固定最高优先级 |
| 结构化身份 | 1.4 |
| Sparse / Lexical | 1.0 |
| Dense Vector | 1.0 |
| Trigram | 0.6 |

权重最终必须由评测集确定，不能长期使用拍脑袋常量。

### 4.4 Memory 排序

Long-term Memory 不默认使用 Cross-Encoder。

RRF 后使用确定性 Policy Reranker：

```text
identity_match
+ active/current
+ time_scope_match
+ task_status compatibility
+ preference polarity compatibility
+ source quality
+ confidence
+ freshness
- conflict penalty
- pending_review penalty
- expired/superseded exclusion
```

硬规则：

- `forgotten` 永不参与普通回答；
- `superseded` 只在 history 查询中出现；
- `pending_review` 只能以“存在待确认冲突”的形式展示；
- 当前事实优先当前有效版本；
- 任务清单默认排除 done/cancelled，除非用户询问历史或状态；
- 相似度不能覆盖状态机结论。

### 4.5 为什么 Memory 不默认用 Cross-Encoder

Memory 数量通常少，且有明确结构和状态。Cross-Encoder 更擅长判断“文本是否回答问题”，但不能可靠决定：

- 哪个版本当前有效；
- task 是否已经完成；
- preference 是正向还是负向；
- conflict 是否已经人工解决；
- 某条 Memory 是否允许自动覆盖另一条。

因此 Memory 的首要排序器必须是结构化策略。只有在大量泛化 semantic Memory 的搜索中，才可以将 Cross-Encoder 作为低权重辅助信号。

---

## 5. Note 检索设计

### 5.1 目标

Note 检索优先保证：

1. 用户换一种说法仍能找到原文；
2. 精确项目名、数字、缩写不会被向量检索漏掉；
3. 能根据时间、分类和标签过滤；
4. 长笔记能定位到相关段落；
5. 返回结果能引用真实 Note；
6. 复杂问题能够组合多个证据。

### 5.2 Note 索引单位

#### 短飞书消息

保持一条消息一个 Note，不切块。

原因：

- 飞书消息本身短；
- 切块会丢失时间、发送者和上下文；
- 小片段容易造成无意义高相似。

#### 长笔记

超过设定长度后按语义段落切块：

```text
Note
  ├─ Chunk 1
  ├─ Chunk 2
  └─ Chunk 3
```

每个 Chunk 必须携带：

- note_id；
- chunk_id；
- title；
- time；
- type / tags；
- section path；
- message_id；
- sensitivity；
- embedding version。

检索得到 Chunk 后，在回答前回读所属 Note 的相邻 Chunk，避免断章取义。

### 5.3 Note 召回通道

#### 通道 A：Metadata

- type；
- tags；
- time range；
- sender；
- enrichment status；
- exact note_id / message_id。

显式条件优先走数据库过滤，不调用 Embedding。

#### 通道 B：Sparse / Keyword

建议组合：

- PostgreSQL FTS；
- title / summary / text 的关键词；
- ASCII identifier；
- 中文 n-gram；
- pg_trgm；
- 精确短语加权。

Sparse 通道特别适合：

- `PROJ-123`；
- `README`；
- `Agent`；
- 版本号；
- 错别字或轻微拼写差异；
- 用户记得原文中的某个词。

#### 通道 C：Dense Vector

使用 Query Embedding 与 Note/Chunk Embedding 做余弦检索。

这是普通 Note 的主语义召回通道，适合：

- 同义改写；
- 用户只记得大意；
- 自然语言问题；
- 跨语言或中英文混合表达；
- 不知道标签和标题的查询。

Dense Retrieval 在功能上属于 bi-encoder 召回：

- Query 单独编码；
- Note/Chunk 预先编码；
- 通过向量相似度快速召回。

#### 通道 D：Note Relation Graph

只有当用户询问“相关内容”“前因后果”“还有哪些关联笔记”时启用：

1. 先用 Hybrid Search 找种子 Note；
2. 再扩展 inbound/outbound related Note；
3. 图扩展结果单独标注来源；
4. 不允许图距离自动替代文本相关性。

### 5.4 RRF 融合

Note 使用 RRF 融合：

```text
Metadata / exact
+ Sparse
+ Dense
+ Graph（按需）
→ Weighted RRF
```

建议初始权重：

| 通道 | 权重 |
|---|---:|
| 显式 metadata / exact phrase | 1.5 |
| Sparse | 1.0 |
| Dense | 1.0 |
| Graph expansion | 0.5 |

不要直接把 BM25、cosine similarity 和 trigram similarity 相加，因为它们的分数分布不同。RRF 只依赖排名，更稳定。

### 5.5 Cross-Encoder Reranker

Cross-Encoder 只用于满足以下条件的 Note 查询：

- RRF 候选数量大于最终返回数；
- 查询属于 general_search、relationship、summary 或 multi_hop；
- 候选之间主题接近但回答性不同；
- 延迟预算允许；
- Reranker 服务健康。

推荐流程：

```text
Hybrid Recall top 20～30
  ↓
Cross-Encoder(query, candidate text)
  ↓
top 5～8
  ↓
回读完整 Note / 相邻 Chunk
```

简单 type/tag/recent 查询、精确 ID 查询、read-after-write 查询不调用 Cross-Encoder。

Cross-Encoder 超时或失败时直接使用 RRF 顺序，不能让查询失败。

### 5.6 LLM Reranker

不建议作为默认排序器。

可选场景：

- 少量复杂候选的最终证据选择；
- 多篇长 Note 的总结；
- Cross-Encoder 无法覆盖的特殊语言或任务；
- 离线评测/标注辅助。

限制：

- 最多处理少量候选；
- 必须输出结构化排序和理由；
- 不得生成新证据；
- 超时立即回退；
- 不能用于决定 Memory 当前状态。

---

## 6. 查询改写、HyDE、子问题分解与 Step-back

这些能力不应默认串行执行，而应由 QueryIntent 和检索质量触发。

### 6.1 基础查询扩展：默认启用

使用已经识别的槽位扩展：

```text
原问题
+ topic
+ entity
+ attribute
+ ASCII identifiers
+ time scope
```

例如：

```text
“RAG 学到哪里了”
→ 原问题 + RAG + 学习任务 + 当前状态
```

这是可控的结构化 query expansion，不让模型自由编造同义词。

### 6.2 生成式 Query Rewrite：条件启用

触发条件：

- 第一轮 Hybrid Recall 结果为空；
- top score 低；
- QueryIntent 置信度低；
- 用户使用大量指代词，如“那个”“之前说的”；
- 问题包含多个主题但没有明确范围。

最多生成 2～3 条改写：

```text
原始 Query
+ 实体化 Query
+ 关键词 Query
+ 必要时一条语义改写
```

每条改写分别召回，最后使用 RRF 合并。必须保留原始 Query 通道，防止改写偏离。

### 6.3 HyDE：默认关闭

HyDE 先生成“可能的答案/文档”，再用其向量检索。它在开放知识库中可能有用，但对个人事实库有明显风险：

- 生成用户从未说过的事实；
- 把假设答案中的实体带入召回；
- 对当前任务状态和偏好产生方向性污染；
- 增加一次 LLM 延迟。

如果未来评测证明有收益，只允许：

- 用于探索性 Note 搜索；
- 作为低权重独立召回通道；
- 不用于 task/preference/current_fact；
- 不直接展示 HyDE 文本；
- 不参与 Memory 写入和关系判断。

### 6.4 子问题分解：复杂查询启用

适用于：

- 比较；
- 趋势；
- 多时间段总结；
- 多实体关系；
- “结合 A 和 B 回答”。

示例：

```text
“我最近 RAG 学习进度如何，遇到过哪些问题？”

子问题 1：当前 RAG 学习任务状态是什么？
子问题 2：最近关于 RAG 的 Note 有哪些？
子问题 3：这些 Note 中提到过哪些问题？
```

每个子问题独立检索，保留来源，再统一综合。最多拆成 3～4 个子问题，避免搜索爆炸。

### 6.5 Step-back Prompting：总结/关系问题启用

Step-back 用于先确定更高层的检索框架，例如：

```text
具体问题：最近几次 RAG 测试为什么效果不稳定？
Step-back：需要按时间找测试记录、配置变化和结果指标。
```

它适合决定“需要哪些证据”，不适合替代检索或生成事实。

---

## 7. Answer Composer

Retriever 返回候选后，答案层必须区分两类证据：

```json
{
  "memory_evidence": [
    {
      "memory_id": "...",
      "status": "active",
      "version": 3,
      "source_note_ids": ["..."]
    }
  ],
  "note_evidence": [
    {
      "note_id": "...",
      "chunk_id": null,
      "time": "...",
      "text": "..."
    }
  ]
}
```

回答规则：

- 当前状态问题以 active Memory 为主；
- 历史问题以 Note 为主；
- Memory 与 Note 冲突时说明时间与状态，不静默选择；
- pending_review 必须显式说明待确认；
- 每个关键结论至少有一个可追溯来源；
- LLM 只能基于已选 Evidence 回答；
- 没有证据时明确说未找到。

---

## 8. 降级与容错

| 故障 | 降级路径 |
|---|---|
| QueryIntent LLM 失败 | 规则路由 + Memory prefetch |
| Query Embedding 失败 | Sparse / metadata |
| Memory Vector 尚未 ready | 结构化 + Sparse |
| PostgreSQL FTS 中文效果差 | CJK n-gram / trigram |
| Cross-Encoder 超时 | RRF 顺序 |
| Query Rewrite 失败 | 原始 Query |
| 子问题分解失败 | 原问题单路检索 |
| Memory Barrier 超时 | provisional Note 回答 |
| LLM 最终生成失败 | 确定性列出候选和来源 |

任何可选模型失败都不能导致用户完全查不到已经持久化的数据。

---

## 9. 评测体系

### 9.1 Long-term Memory 指标

| 指标 | 含义 |
|---|---|
| Current-state Accuracy | 当前任务/偏好/事实回答是否正确 |
| Canonical-key Hit Rate | 同一身份能否命中同一 Memory |
| Conflict Recall | 应进入 pending_review 的冲突是否被发现 |
| False Merge Rate | 不相关 Memory 被错误合并的比例 |
| Active-only Precision | 普通查询是否误返回旧版本 |
| Memory Recall@5 | 正确 Memory 是否在前 5 |
| Evidence Precision | Memory 来源 Note 是否真实支持结论 |

硬门槛：

- False Merge Rate 必须接近 0；
- task/preference/current_fact 不能以召回率为由牺牲状态正确性；
- pending_review 不得伪装成已确认事实。

### 9.2 Note 指标

| 指标 | 含义 |
|---|---|
| Recall@10 | 相关 Note 是否进入前 10 |
| MRR@10 | 第一条正确 Note 的平均排名 |
| nDCG@10 | 多条相关 Note 的排序质量 |
| Identifier Recall | 项目名、编号、英文缩写的召回率 |
| Paraphrase Recall | 换一种表达后的语义召回率 |
| Citation Precision | 引用 Note 是否支持回答 |
| Answer Groundedness | 最终回答是否完全基于证据 |

### 9.3 工程指标

- P50 / P95 总延迟；
- 各召回通道耗时；
- Embedding 调用量与缓存命中率；
- Cross-Encoder 调用率、超时率；
- Query Rewrite 触发率；
- Barrier timeout 率；
- 无结果率；
- 降级路径使用率。

建议初始延迟目标：

| 查询类型 | P95 目标 |
|---|---:|
| 精确 Memory / metadata | 500 ms 内 |
| 普通 Hybrid Note | 1.2 s 内 |
| Cross-Encoder 查询 | 2.5 s 内 |
| 多跳总结 | 5 s 内，并给出处理中反馈 |

---

## 10. 分阶段实施

### Phase 0：固定基线

- 冻结一组真实飞书问题和正确答案；
- 分开标注 Memory 查询和 Note 查询；
- 记录当前 Recall@K、MRR、正确率、耗时和模型调用；
- 将测试数据放入独立 space，避免污染用户记忆。

验收条件：

- 所有后续优化都能与基线直接对比；
- 不再只用主观示例评价检索效果。

### Phase 1：Memory Vector Lifecycle

- 开启 Memory Vector Lifecycle；
- 为 active Memory 回填 embedding；
- Memory 变更后刷新向量；
- 建立 pending / processing / ready / failed 状态；
- 验证 Embedding 失败时 Sparse 降级；
- 将 Vector 通道加入现有 RRF。

验收条件：

- active Memory 向量覆盖率 ≥ 99%；
- paraphrase Recall@5 提升；
- current-state Accuracy 不下降；
- False Merge Rate 不受影响。

### Phase 2：Note 真正 Hybrid Search

- 新增统一 Note Hybrid Retriever；
- metadata、Sparse、Dense 分通道召回；
- 使用 RRF 融合；
- 普通 semantic_search 迁移到 Hybrid Search；
- 保留 type/tag/recent 的直接路径；
- 保留 provisional Note 快速路径。

验收条件：

- Identifier Recall 与 Paraphrase Recall 同时提升；
- 无结果率下降；
- P95 在目标范围内。

### Phase 3：统一 Retrieval Trace

每个 Hit 记录：

```text
channel ranks
RRF score
exact/metadata signals
vector model/version
policy boosts/penalties
reranker score
final rank
```

验收条件：

- 能解释每条结果为什么出现；
- 能区分召回失败、融合失败和重排失败。

### Phase 4：条件 Cross-Encoder

- 只接入 Note 复杂查询；
- top 20～30 重排到 top 5～8；
- 超时回退；
- Feature Flag 灰度；
- 与纯 RRF 做 A/B 评测。

验收条件：

- MRR/nDCG 有显著提升；
- Recall 不下降；
- 延迟和资源使用可接受。

### Phase 5：复杂 Query Planner

- 低召回时 Query Rewrite；
- multi_hop / summary 使用子问题分解；
- relationship / summary 使用 Step-back；
- HyDE 保持关闭，除非离线评测证明明确收益。

验收条件：

- 复杂问答 groundedness 提升；
- 每个子答案都有来源；
- 不增加简单查询延迟。

---

## 11. 建议 Feature Flags

```text
SUIXINJI_MEMORY_VECTOR_LIFECYCLE_ENABLED
SUIXINJI_MEMORY_HYBRID_VECTOR_ENABLED
SUIXINJI_MEMORY_TRIGRAM_ENABLED

SUIXINJI_NOTE_HYBRID_RETRIEVAL_ENABLED
SUIXINJI_RETRIEVAL_WEIGHTED_RRF_ENABLED
SUIXINJI_NOTE_CROSS_ENCODER_RERANK_ENABLED

SUIXINJI_QUERY_REWRITE_ENABLED
SUIXINJI_QUERY_DECOMPOSITION_ENABLED
SUIXINJI_QUERY_STEP_BACK_ENABLED
SUIXINJI_QUERY_HYDE_ENABLED
```

注意：

- 当前 `SUIXINJI_MEMORY_UNIFIED_RERANK_ENABLED` 并不代表已经接入 Cross-Encoder；
- 新模型重排应使用含义明确的独立开关；
- 所有模型能力必须保留无模型降级路径；
- Feature Flag 开启前必须有离线指标和 Trace。

---

## 12. 需要新增或调整的模块

建议新增：

```text
agent/query_planner.py
retrieval/models.py
retrieval/rrf.py
retrieval/note_retriever.py
retrieval/memory_retriever.py
retrieval/reranker.py
retrieval/query_rewriter.py
retrieval/evaluator.py
```

现有模块调整：

```text
agent/query_agent.py
agent/query_intent.py
repositories/postgres/notes.py
repositories/postgres/memory.py
repositories/postgres/vectors.py
memory/retriever.py
memory/vector_lifecycle.py
core/settings.py
core/model_policy.py
```

接口建议：

```python
class RetrievalRequest:
    space_id: str
    query: str
    intent: QueryIntent
    filters: dict
    top_k: int


class RetrievalHit:
    source_type: Literal["note", "memory"]
    source_id: str
    content: str
    channel_ranks: dict[str, int]
    rrf_score: float
    rerank_score: float | None
    final_score: float
    reasons: list[str]
    metadata: dict
```

统一返回结构，但 Note 和 Memory 的召回与排序策略保持分离。

---

## 13. 必须通过的验收场景

### 场景 1：当前任务

```text
Note 1：我需要学习 RAG
Note 2：我正在学习 RAG
Note 3：我已经学完 RAG
问题：RAG 学到哪里了？
```

预期：

- Memory First；
- 命中同一 task Memory；
- 返回 done；
- 可展开 3 条来源；
- 不返回旧状态为当前答案。

### 场景 2：状态冲突

```text
Note 1：我已经完成测试报告
Note 2：我正在完成测试报告
```

预期：

- 当前 Memory 保持 done；
- Note 2 进入 pending_review；
- 查询明确显示存在待确认冲突。

### 场景 3：普通 Note 改写召回

```text
原文：今天复盘了向量召回不足的问题
问题：之前什么时候讨论过语义检索漏召回？
```

预期：

- Dense 通道召回原文；
- Sparse 通道可能不命中，但不影响最终结果；
- 返回 Note 时间和来源。

### 场景 4：精确标识符

```text
原文：修复 PROJ-123 的 Redis 超时
问题：PROJ-123 发生过什么？
```

预期：

- Sparse/identifier 通道强命中；
- 不依赖向量是否理解 Ticket；
- 正确 Note 排在前列。

### 场景 5：复杂总结

```text
问题：比较我前后两轮 RAG 测试的召回效果，并总结改进原因。
```

预期：

- Step-back 确定需要时间、两轮记录和指标；
- 分解为多轮检索；
- Hybrid + Cross-Encoder 选证据；
- 最终答案逐项引用 Note；
- 不引用无关的长期偏好或任务。

### 场景 6：Embedding 故障

预期：

- Note 仍可通过 Sparse/metadata 查询；
- Memory 仍可通过 canonical/structured 查询；
- 用户不会因为 Embedding 服务失败而得到系统错误。

---

## 14. 最终架构决策

最终推荐架构为：

```text
Note：
Metadata + Sparse + Dense + Optional Graph
→ Weighted RRF
→ Conditional Cross-Encoder
→ Evidence-grounded Answer

Long-term Memory：
Canonical/Structured + Sparse + Dense
→ Exact-first + Weighted RRF
→ Deterministic Policy Reranker
→ Current-state Answer
```

明确不采用：

- 所有查询都运行 Query Rewrite；
- 所有查询都运行 Cross-Encoder；
- 默认运行 LLM Reranker；
- 默认运行 HyDE；
- 用向量相似度决定 Memory 合并；
- 用 LLM 排序结果覆盖 task 状态机或 preference polarity；
- 将 pending_review 当成当前事实；
- 为追求召回率而牺牲来源证据和状态正确性。

这套设计的本质不是堆叠更多 RAG 技术，而是让每种技术承担适合它的职责：

- 结构化字段负责身份和状态；
- Sparse 负责精确词和标识符；
- Dense 负责语义改写；
- RRF 负责多路融合；
- Cross-Encoder 负责 Note 候选的回答相关性；
- Query Planner 负责复杂问题的检索计划；
- LLM 负责理解和表达；
- Relation Guard 与状态机负责长期记忆安全。

## 15. 本次落地记录（2026-07-25）

本方案已在 `/home/zcj/suixinji` 落地以下可运行部分：

- Note 查询已接入 `exact + FTS/sparse + lexical/CJK + dense` 多路召回，并通过加权 RRF 融合；Embedding 不可用时自动保留 Sparse/Metadata 降级路径。
- Memory 查询已加入 exact-first 通道和加权 RRF；最终仍由确定性状态/偏好规则排序，向量不会改变 task 状态或偏好极性。
- Memory 向量生命周期已开启：写入/更新会进入 `pending → processing → ready/failed`，新增 `worker-memory-embedding` 执行异步向量任务；历史 active Memory 已完成一次幂等回填。
- Query Rewrite、有限子问题分解和 Step-back 已接入复杂查询的低召回补偿路径；简单查询不触发额外变体查询。HyDE 和 LLM Reranker 仍默认关闭。
- 复杂查询规划只产生检索变体，不写入 Note/Memory，不改变任何任务状态或冲突结论。

验证结果：V3/敏感笔记/读后写测试 `20 passed`，PostgreSQL/Streams 回归测试 `27 passed`；当前测试空间 Memory 向量 `24/24 ready`，Note 向量 `52`，分布式服务和 API 健康检查通过。
