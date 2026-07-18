# 随心记当前项目审视与优化方案

> 仓库：`9leaa/suixinji`  
> 审查基线：`main` / `deda2b3a51fda260c10d93a84ebfb3b48d64cece`  
> 审查重点：长期记忆设计、性能、高并发、数据一致性、安全与工程可维护性  
> 文档用途：作为后续 Codex 改造任务的总方案，不直接要求一次性重写全部系统。

---

## 1. 总体结论

当前项目已经完成了从本地单进程 Demo 到分布式 Agent 原型的关键升级：

- PostgreSQL 作为共享事实存储；
- pgvector 保存 Note Embedding；
- Redis Streams 负责分布式任务分发；
- Transactional Inbox/Outbox 保证任务可恢复；
- Receiver、Relay、Ingest、Memory、Enrichment、Query、Summary、Delivery、Scheduler 已拆成独立角色；
- 长期记忆已经具备候选抽取、校验、旧记忆检索、六类关系审理、版本演化、人工审核和 Trace；
- 已完成 100 用户、1000 请求的多进程恢复性验收，最终任务、Pending、Stream lag 和 Memory gap 能够收敛。

但当前系统仍属于“**分布式正确性基本成立，记忆质量和容量能力尚未定型**”的阶段。最需要解决的不是继续堆功能，而是以下四件事：

1. **记忆判断过度依赖规则和字符相似度，存在漏记、误合并、误替代风险。**
2. **查询与记忆处理存在全量扫描、N+1 查询和同步写放大，数据增长后性能会明显下降。**
3. **同一空间的强顺序屏障范围过大，虽然保证正确性，但会把活跃用户的所有消息串行化。**
4. **多 Worker 缺少任务租约令牌和 fencing，旧 Worker 恢复后可能提交过期结果。**

建议按“记忆正确性 → 查询性能 → 并发所有权 → 安全与迁移”的顺序改造，不建议直接继续增加 Worker 数量。

---

## 2. 问题优先级总表

| 编号 | 优先级 | 领域 | 当前问题 | 主要影响 |
|---|---|---|---|---|
| M-01 | P0 | 记忆 | 候选检索只取最多 100 条 active Memory，在 Python 中做字符/规则相似度 | 记忆增长后漏召回，导致重复记忆或错误关系判断 |
| M-02 | P0 | 记忆 | `merge/supersede/conflict` 的主题判断和阈值较宽 | 错误覆盖用户偏好、事实和任务 |
| M-03 | P0 | 记忆 | 月度 consolidation 写死 Agent/RAG 领域语义 | 项目无法泛化为通用个人记忆系统 |
| C-01 | P0 | 并发 | Task 无 lease token / fencing token | 旧 Worker 可能覆盖新 Worker 的执行结果 |
| C-02 | P0 | 多租户 | tenant 未进入全部查询条件、唯一约束和 Redis Key | 不同租户可能互相碰撞或串数据 |
| P-01 | P0 | 性能 | Memory 列表存在 N+1 source 查询，Note 工具大量全量加载 | 用户数据增长后数据库请求急剧增加 |
| P-02 | P0 | 性能/并发 | 每条 Ingest 都等待关键 Memory 完成后才释放同空间下一条消息 | 单个活跃用户吞吐被 Memory 延迟锁死 |
| M-04 | P1 | 记忆 | 时间有效性只存字段，没有完整过期与回溯策略 | 过期事实仍可能保持 active |
| M-05 | P1 | 记忆 | Candidate/Decision 缺少完整可重放版本信息 | Prompt、模型、策略更新后难以审计与重放 |
| M-06 | P1 | 记忆 | pending/conflict 只有查看和批准，缺少拒绝、编辑、冲突解决 | 人工审理闭环不完整 |
| P-03 | P1 | 性能 | Memory 查询每次更新 access_count | 读请求变写请求，产生热点行和锁竞争 |
| P-04 | P1 | 性能 | Outbox 在数据库事务和行锁内调用 Redis | Redis 变慢时长期占用数据库连接和行锁 |
| C-03 | P1 | 并发 | 所有进程独立连接池，缺少全局连接预算 | 多进程下可能耗尽 PostgreSQL 连接数 |
| C-04 | P1 | Redis | 每次 Worker 循环优先从 `0-0` 执行 XAUTOCLAIM | Pending 较多时重复扫描 |
| C-05 | P1 | 降级 | Redis 限流故障时完全 fail-open | Redis 故障可能把流量直接压向 PostgreSQL/LLM |
| E-01 | P1 | 数据库 | Alembic baseline 使用 `Base.metadata.create_all()` | 历史迁移不稳定，新旧数据库路径可能不一致 |
| E-02 | P1 | 安全 | 测试 API 可由请求体指定 tenant/user/space，缺少鉴权 | 接口暴露后可伪造任意用户任务 |
| E-03 | P2 | Schema | 多个时间字段使用 String，部分引用没有外键 | 时间查询、清理和数据完整性较差 |
| E-04 | P2 | Embedding | 维度在配置中可改，但数据库与代码写死 1024 | 配置与真实能力不一致 |

---

# 3. 长期记忆设计问题

## 3.1 候选抽取规则过于脆弱和领域化

### 当前表现

`memory/extractor.py` 默认使用 `rules`，主要依赖固定关键词：

- 偏好：喜欢、不喜欢、偏好、习惯、过敏等；
- 任务：记得、需要、待办、修、改、实现、准备等；
- 语义：学习、研究、开发、负责、住在等；
- 实体抽取只内置少量词，例如咖啡、牛奶、北京、上海、Python、Agent、RAG。

这会造成：

- “以后咖啡都不要加糖”可能同时是偏好和约束，但规则未必稳定识别；
- “论文第三章还差实验分析”可能是任务，但不含当前 task marker；
- “我已经从北京搬走了”涉及事实失效和新事实，但可能只抽到一条粗粒度 semantic；
- 反问、否定范围、比较偏好、条件偏好容易判断错误；
- 默认规则对当前开发领域表现较好，但对饮食、健康、旅行、学习、工作等开放领域覆盖不足。

### 优化方案

引入“三层候选抽取”，但仍坚持 LLM 只提候选、不直接修改数据库：

```text
原始 Note
  → 轻量规则预筛选
  → 结构化 LLM/小模型候选抽取
  → 本地 Schema 校验和风险校验
  → 候选持久化
```

建议新增持久化表：

```text
memory_candidates
- id / candidate_id
- tenant_id / space_id / note_id
- memory_type
- subject / predicate / object_value
- polarity
- scope_json
- valid_from / valid_until
- confidence / importance
- evidence_span
- extractor_type
- extractor_version
- model
- prompt_hash
- status
- created_at
```

关键要求：

1. `candidate_id` 对同一 note、类型、规范化结构保持稳定；
2. 候选必须先落库，再进入审理；
3. 提取器版本和 Prompt hash 必须记录；
4. 不允许只保存最终 Memory 而丢失中间 Candidate；
5. 规则模式保留为降级路径，但不再作为长期唯一主路径。

### 需要修改的文件

- `memory/extractor.py`
- `memory/models.py`
- `memory/candidate_validator.py`
- `memory/prompts.py`
- `infrastructure/schema.py`
- `repositories/postgres/memory.py`
- 新增 Alembic migration

---

## 3.2 Candidate Retrieval 不是语义检索，且固定只看 100 条

### 当前表现

`memory/candidate_retriever.py`：

- 只读取相同 memory type 的 active Memory；
- 最多读取 100 条；
- 使用字符集合 Jaccard、subject/predicate、实体包含等规则评分；
- `memory_vectors` 虽然存在，但没有进入主链路。

当一个用户积累几百或几千条 Memory 后，可能出现：

- 真正相关的旧记忆不在最新 100 条中；
- 同义表达字符重合低，检索不到；
- 无关句子因为共享“学习”“工作”或某个英文实体被召回；
- 没召回旧记忆时，审理器只能判断为 `new`，最终产生重复 Memory。

### 优化方案：混合候选召回

```text
结构化召回：subject + predicate + memory_type + status
语义召回：memory embedding / pgvector
词法召回：PostgreSQL FTS 或 trigram
时间召回：最近变化、当前 active、尚未过期
                  ↓
合并去重 + rerank
                  ↓
Top K 进入关系审理
```

建议召回候选池：

- 结构化精确召回：10 条；
- pgvector 语义召回：20 条；
- trigram/FTS 词法召回：20 条；
- 合并后最多 30 条；
- 本地 reranker 选 8 条进入 adjudicator。

新增或完善索引：

```sql
CREATE INDEX ix_memories_space_type_status_predicate
ON memories(space_id, memory_type, status, predicate);

CREATE INDEX ix_memories_space_subject
ON memories(space_id, subject);

CREATE INDEX ix_memory_vectors_embedding_hnsw
ON memory_vectors USING hnsw (embedding vector_cosine_ops);
```

不要再通过 `list_memories(... limit=100)` 把 Memory 全部拉到 Python 中排序。

### 验收标准

- 100、1000、10000 条 Memory 三档测试；
- 候选召回 Recall@20 ≥ 0.95；
- 单次候选召回数据库查询次数固定，不随 Top K 线性增长；
- 不同表达的同一偏好能够召回；
- 相同实体但不同主题不应进入最终 Top K。

---

## 3.3 关系审理存在误合并和误覆盖风险

### 当前表现

当前 adjudicator 中存在较宽判断：

- 共享 subject、predicate、实体、学习/研究词或相似度达到较低阈值，即可认为 same topic；
- same topic 且相似度达到约 0.34，可能进入 merge；
- destructive action 主要靠综合 confidence 是否超过阈值决定自动执行。

风险示例：

```text
我正在学习日语。
我正在学习 Redis。
```

二者都可能出现“学习”，但不应 merge。

```text
我喜欢咖啡。
我喜欢在咖啡店学习。
```

共享“咖啡”，但一个是饮品偏好，一个是地点/场景偏好。

### 优化方案

把关系审理拆成两个阶段：

```text
阶段 A：是否为同一 Memory Key
阶段 B：在同一 Key 下判断 same/merge/update/supersede/conflict
```

建议为不同类型定义稳定的 `memory_key`：

```text
preference: subject + topic + scope
semantic:   subject + predicate
 task:      owner + task_identifier
 episodic:  event_type + entity + time_bucket
```

例如：

```text
用户喜欢咖啡
memory_key = preference:user:drink:coffee

用户喜欢在咖啡店学习
memory_key = preference:user:study_place:cafe
```

只有 `memory_key` 相同或达到高置信兼容时，才允许执行 destructive action。

进一步规则：

- `supersede` 必须检测明确时间变化、否定或替换语义；
- `conflict` 不应自动把双方全部从正常查询中移除，应保留冲突组和当前可用结论；
- `merge` 应生成结构化合并，不应简单用候选文本覆盖旧文本；
- destructive action 的阈值按类型分别配置，不应只有一个全局阈值；
- destructive action 误判成本高，应优化“精确率优先”，不追求自动化率。

建议阈值：

```text
new / same：可自动
merge：高精度，低置信进入 review
update_task：必须通过状态机和 task key
supersede：必须存在明确变化证据
conflict：默认进入 review，除非证据极强
```

---

## 3.4 时间、失效、衰减和“当前事实”定义不完整

### 当前表现

项目有 `valid_from`、`valid_until`、`expired`、`last_confirmed_at` 等字段，但主查询通常只筛选 `status=active`，没有形成完整的时间状态机。

可能出现：

- “本周住在上海”在数月后仍为 active；
- 已经过期的任务和临时偏好继续进入画像；
- 旧事实与新事实只靠 superseded 状态管理，缺少 as-of 查询；
- 长期未确认的低置信 Memory 永久积累。

### 优化方案

建立统一的 Memory Lifecycle：

```text
pending_review
    ↓ approve
active
    ├─ superseded
    ├─ conflicted
    ├─ expired
    ├─ forgotten
    └─ archived
```

增加：

1. `expires_at` 或统一使用 `valid_until TIMESTAMPTZ`；
2. 定时 expiry worker；
3. 查询默认条件：`status=active AND (valid_until IS NULL OR valid_until > now())`；
4. 支持 `as_of` 历史查询；
5. 对 episodic、临时偏好和已完成 task 设置不同保留策略；
6. 低置信、长期未确认 Memory 进入再验证或归档，而不是直接删除；
7. `access_count` 不参与无限强化，避免“错误记忆越查越重要”。

---

## 3.5 Monthly Consolidation 写死 Agent/RAG 内容

### 当前表现

`memory/consolidator.py` 的月度稳定语义生成只筛选 Agent、RAG、学习、开发、向量、ReAct，并固定生成：

```text
用户当前持续学习和开发 Agent/RAG 系统。
```

这不是通用 Memory Consolidation，而是针对当前开发者样例的硬编码。

### 优化方案

删除硬编码领域总结，改为通用主题聚类：

```text
近期 episodic/semantic Memory
  → 按 embedding + predicate + entity 聚类
  → 每个聚类计算来源数、时间跨度、一致性
  → 满足稳定条件后生成 semantic candidate
  → 仍走普通 adjudication/evolution
```

稳定语义生成条件建议：

- 至少 3 个不同来源 Note；
- 时间跨度至少 7 天；
- 聚类一致性超过阈值；
- 不包含未解决 conflict；
- 生成内容必须附来源列表；
- 生成器版本可审计；
- 相同主题重复月度运行保持幂等。

---

## 3.6 Candidate 级幂等和审计不完整

### 当前表现

- Candidate ID 稳定；
- Decision ID 每次随机生成；
- extraction 只在 Note 级记录 candidate_count 和 processed_count；
- partial 重试时会重新审理全部候选；
- 已成功候选可能再次形成新的 Decision 记录。

虽然数据库唯一约束和同内容判断可以减少重复 Memory，但审计记录仍可能重复，且无法精确知道哪一个 Candidate 已完成、失败或待审。

### 优化方案

新增 Candidate 状态：

```text
extracted → validated → adjudicated → applied
                         ├─ pending_review
                         ├─ discarded
                         └─ failed
```

增加唯一约束：

```text
UNIQUE(note_id, candidate_id)
UNIQUE(candidate_id, policy_version, adjudication_attempt)
```

重试只重试失败 Candidate，不重新执行已经 applied 的 Candidate。

Decision 中增加：

- extractor_version；
- adjudicator_version；
- policy_version；
- target_snapshot_version；
- prompt_hash / model；
- evidence_span；
- input_hash；
- retry_of_decision_id。

这样才能真正做到“可重放、可对比、可回滚”。

---

## 3.7 人工审核闭环不完整

### 当前能力

已有：

- pending list；
- approve；
- conflict list；
- correct / forget / purge。

缺少：

- reject pending；
- edit-and-approve；
- resolve conflict：选择 A、选择 B、合并成 C、均保留但限定 scope；
- 批量审核；
- pending 超时处理；
- 低风险自动批准、高风险必须人工批准的策略配置。

### 建议命令

```text
/memory reject <id> [reason]
/memory edit <id> <new_content>
/memory approve <id>
/memory resolve <conflict_id> choose=<memory_id>
/memory resolve <conflict_id> merge=<content>
/memory review stats
```

---

## 3.8 记忆评测仍不足以支持真实质量结论

当前 deterministic eval 的部分指标达到 1.0，但它主要证明规则样例通过，不能证明真实语言下的长期记忆质量。

需要建立真实反馈集：

```text
至少 300～500 条真实或人工构造对话
覆盖：
- 否定范围
- 条件偏好
- 比较偏好
- 时间变化
- 任务更新
- 多实体
- 指代
- 模糊表达
- 冲突
- 敏感内容
- 不应记忆的闲聊
```

评测维度：

| 模块 | 指标 |
|---|---|
| Candidate Extraction | Precision / Recall / F1，按四类 Memory 分组 |
| Candidate Retrieval | Recall@K / MRR |
| Relation Adjudication | 六类关系 Macro-F1、混淆矩阵 |
| Destructive Action | 错误 merge/supersede/conflict 比例 |
| Evolution | 状态、版本、来源、关系一致性 |
| End-to-End | 当前偏好/任务/事实问答正确率 |
| Forget/Correct | 删除与修正后不可错误召回 |

关键门槛：

- destructive action 错误执行率优先压到 1% 以下；
- 不应为了提高自动执行率降低安全阈值；
- dry-run 指标和真实模型指标必须分开记录。

---

# 4. 性能问题

## 4.1 Memory Repository 存在明显 N+1 查询

### 当前表现

`list_memories()` 先查 Memory 列表，然后每条 Memory 调用 `_sources()`；包含版本时还会调用 `_versions()`。

Candidate Retrieval 每个候选最多加载 100 条 Memory，可能产生：

```text
1 次 memories 查询
+ 100 次 memory_sources 查询
+ Python 中 100 次规则评分
```

一条 Note 若抽取 3～5 个候选，数据库往返会被进一步放大。通过 Mac SSH 反向隧道访问 PostgreSQL 时，这类小查询尤其昂贵。

### 优化方案

- 使用 `selectinload` / 批量查询；
- 或一次查 Memory，再通过 `WHERE memory_id IN (...)` 批量查询 sources；
- Candidate Retrieval 只返回 adjudication 所需轻量字段，不加载 sources/versions；
- 只有最终展示和 Trace 才加载 sources；
- 添加 SQL query count 测试。

目标：

```text
list_memories Top 100：最多 2～3 次 SQL
candidate retrieval：最多 3 次 SQL
```

---

## 4.2 Note 查询工具大量全量扫描

### 当前表现

`filter_notes`、`list_recent`、`follow_links`、`provisional_search` 会调用 `_safe_notes()`，而 PostgreSQL 后端的 `load_index()` 会读取该空间全部 Notes、Tags 和 Relations，再在 Python 中过滤。

当单用户有数万条 Note 时：

- `/type` 仍加载全部 Note；
- `/tag` 仍加载全部 Note；
- `list_recent` 仍加载全部 Note；
- `follow_links` 为找一条 Note 扫描全部 Note；
- 每次查询还需要构建大量 Python dict。

### 优化方案

在 Repository 增加专用 SQL：

```text
query_notes_by_type(...)
query_notes_by_tags(...)
list_recent_notes(...)
get_note_by_id(...)
get_note_relations(...)
list_provisional_notes(...)
```

对应索引：

```sql
CREATE INDEX ix_notes_space_type_created
ON notes(space_id, note_type, created_at DESC);

CREATE INDEX ix_notes_space_enrichment_created
ON notes(space_id, enrichment_status, created_at DESC);

CREATE INDEX ix_note_tags_tag_note
ON note_tags(tag, note_id);

CREATE INDEX ix_note_relations_target
ON note_relations(target_note_id);
```

Agent 层不再直接调用 `load_index()` 完成数据库查询。

---

## 4.3 Memory Search 的读请求会产生写放大

### 当前表现

每次命中 Memory 后，`mark_accessed()` 会：

- 对每个 Memory 加行锁；
- 更新 `last_accessed_at`；
- `access_count += 1`。

这使查询不再是纯读操作。热门 Memory 会成为数据库热点行，影响高并发查询。

### 优化方案

方案 A：Redis 异步聚合

```text
查询命中
→ Redis HINCRBY memory_access:{date}
→ 定时批量刷入 PostgreSQL
```

方案 B：采样写入

- 只有 1/N 查询更新；
- 或每个 Memory 每小时最多更新一次。

方案 C：从核心排序中弱化 access_count，避免它变成强反馈回路。

推荐 A + C。

---

## 4.4 当前同空间因果屏障过度串行

### 当前设计

同一空间只发布第一条 root task；Ingest 完成后激活关键 Memory task；关键 Memory 完成后才推进 `memory_watermark` 并释放下一条 root task。

优点：

- 保证“先修改偏好，再查询当前偏好”不会读到旧 Memory。

代价：

- 每条普通 Note 都要等待 Memory 抽取和演化；
- 同一用户连续发送 20 条无关笔记也完全串行；
- 一个慢 Memory 会阻塞该空间后续所有 Query、Summary 和 Ingest；
- 多加 Ingest Worker 对同一热点用户没有帮助。

### 优化方案：双 Watermark + 可选一致性

引入：

```text
note_watermark    = Note 已经可靠保存的最大 sequence
memory_watermark  = Memory 已经完成演化的最大 sequence
```

任务策略：

1. 普通 Note 写入完成后推进 `note_watermark`；
2. Memory 异步处理，完成后推进 `memory_watermark`；
3. Query 根据意图选择一致性：
   - Note 历史搜索：只要求 note watermark；
   - 当前偏好/任务/事实：要求 memory watermark；
   - 弱一致查询：直接执行，并附 provisional 证据；
4. 强一致等待设置超时，例如 1～3 秒；超时后返回“记忆仍在整理”或使用 Note 证据降级；
5. 不同 Memory Key 可并发，只对同一 `(space_id, memory_key)` 串行。

推荐流程：

```text
Ingest root
  → Note durable
  → note_watermark++
  → 发布 Memory Candidate task
  → 立即允许下一条普通 Ingest

Current-state Query
  → 检查 query_sequence <= memory_watermark
  → 未达到时短暂等待 / 使用 provisional route
```

这样保留关键 read-after-memory 语义，同时避免所有消息被 Memory 全局阻塞。

---

## 4.5 ReAct Query 的 LLM 调用需要 Fast Path

常见问题不需要完整 ReAct：

```text
/type 学习
/tag 饮食
最近一周记了什么
我现在喜欢什么
当前待办是什么
```

建议增加确定性 Router：

```text
命令/结构化条件 → 直接工具
明确 current preference/task → memory search + 一次 synthesis
普通自然语言 → hybrid retrieval
复杂多跳问题 → ReAct
```

目标：

- 70% 常见请求不进入多轮 ReAct；
- 限制最大 tool steps；
- 精确查询不调用 embedding；
- 相同标准化 Query 使用缓存；
- LLM synthesis 与 retrieval 延迟分开统计。

---

## 4.6 当前性能报告不能作为容量基线

Stage 4 最终收敛证明了恢复性，但记录的总延迟主要由队列等待、故障演练、进程重启和 SSH 数据库往返组成。

需要补充三类独立测试：

### A. 纯正确性测试

- 带 Chaos；
- 只看守恒、恢复、无重复和最终收敛；
- 不用于宣传性能。

### B. 干净容量测试

- 不执行 Chaos；
- 进程预热完成后再发流量；
- Fake LLM / Embedding；
- 100、500、1000 用户三档；
- 记录 Receiver、DB、Queue、Worker、Delivery 分段延迟。

### C. 真实外部依赖测试

- 小规模 10～20 用户；
- 真实 LLM / Embedding；
- 统计 token、成本、超时和限流；
- 与内部执行耗时分开。

---

# 5. 高并发与分布式问题

## 5.1 Task 缺少 Lease Token / Fencing Token

### 风险场景

```text
Worker A claim task
→ A 卡住超过 stale 时间
→ Worker B reclaim 并重新执行
→ A 恢复
→ A 调用 complete_task
→ A 的过期结果覆盖 B 当前状态
```

目前完成、失败、延迟接口只传 `task_id`，没有验证任务所有权。

### 优化方案

Task 增加：

```text
claimed_by
lease_token
lease_expires_at
claim_version
```

claim 返回：

```text
(task_payload, lease_token, claim_version)
```

所有状态变更必须带 token：

```sql
UPDATE tasks
SET status = 'completed'
WHERE id = :task_id
  AND status = 'running'
  AND lease_token = :lease_token
  AND claim_version = :claim_version;
```

更新 0 行代表过期 Worker，结果必须丢弃并写日志。

增加测试：

1. A claim；
2. 租约过期；
3. B claim；
4. A complete；
5. 断言 A 被拒绝；
6. B complete 成功。

这是进入真实多服务器前必须完成的 P0 问题。

---

## 5.2 Outbox Relay 在数据库事务内访问 Redis

### 当前风险

Relay 持有 `FOR UPDATE SKIP LOCKED` 行锁期间调用 Redis `XADD`。当 Redis 超时或网络抖动时：

- 数据库事务时间变长；
- 连接池被占用；
- Outbox 行长期锁定；
- 多 Relay 吞吐下降；
- 毒事件会被每轮重复处理。

### 优化方案

改为三段式：

```text
短事务 1：claim Outbox，写 lease_token / status=publishing
事务外：调用 Redis XADD
短事务 2：token 匹配后标记 published
```

Outbox 增加：

```text
status
lease_token
lease_expires_at
next_attempt_at
max_attempts
last_error
failed_at
```

重试使用指数退避，超过上限进入 `dead`，不阻塞正常事件。

---

## 5.3 PostgreSQL 连接池缺少全局预算

Stage 4 有约 26 个独立进程。即使每进程：

```text
pool_size=1
max_overflow=2
```

理论峰值仍可能达到约 78 个连接。

正式部署需要：

- 计算每类角色的连接预算；
- Receiver、Worker、Relay 使用不同池设置；
- 接入 PgBouncer transaction pooling；
- 为迁移和管理任务保留连接；
- 监控 active/idle/waiting connection；
- 防止大量进程同时重连造成 connection storm。

建议配置：

```text
Receiver：pool 2，overflow 1
Relay：pool 1，overflow 1
普通 Worker：pool 1，overflow 0
Query Worker：pool 2，overflow 1
Scheduler：pool 1，overflow 0
```

具体值必须根据数据库 max_connections 和压测重新确定。

---

## 5.4 Redis Streams Reclaim 扫描策略需要优化

当前 Worker 每次循环先 `XAUTOCLAIM`，且 start id 固定 `0-0`，无 Pending 才读取新消息。

高并发下可能导致：

- 反复扫描旧 Pending；
- 新消息处理被 reclaim 检查拖慢；
- 所有 Worker 同时扫描 Pending。

建议：

- 新消息读取作为主循环；
- reclaim 每 5～30 秒运行一次；
- 保存 `next_start_id` 游标；
- 只让部分 Worker 承担 reclaim；
- 单独记录 reclaim count、reclaim latency、stale age。

---

## 5.5 Redis 故障时限流完全 Fail-Open

普通 Inbox 以 PostgreSQL 唯一约束兜底是合理的，但所有流量 fail-open 会在 Redis 故障时把压力集中到：

- PostgreSQL；
- LLM；
- Embedding；
- Worker Queue。

建议按请求类型降级：

| 请求 | Redis 故障策略 |
|---|---|
| 普通 Ingest | 可 fail-open，但增加本地进程限流和 DB backpressure |
| Query | 本地限流，过载时返回稍后重试 |
| Summary | 延迟执行，不立即放行 |
| LLM | 必须受本地 semaphore 保护 |
| 管理/批量命令 | fail-closed 或仅管理员允许 |

增加系统级 overload 状态：

```text
normal → degraded → overload
```

依据 DB pool wait、queue depth、LLM slots 自动拒绝低优先级任务。

---

## 5.6 Redis 幂等 Fast Path 没有充分减少数据库压力

Receiver 检查 completed，但对 processing 状态没有直接返回，仍会访问 PostgreSQL。虽然数据库唯一约束保证最终正确，但重复流量高时会造成无意义事务。

建议区分：

```text
absent      → 获取 processing token，继续
processing  → 返回 accepted/in-progress，不重复入库
completed   → 返回 duplicate
failed      → 允许受控重试
```

Redis 仅作为快速层，PostgreSQL 唯一约束仍是最终事实。

---

## 5.7 多租户隔离没有贯穿全部层

需要统一改造：

- 数据库查询全部包含 tenant_id；
- `Space.id` 不直接等于平台 space id，改为内部 UUID；
- 唯一约束使用 `(tenant_id, source, source_space_id)`；
- Inbox 幂等使用 `(tenant_id, source, source_message_id)`；
- Redis Key 前缀包含 tenant_id；
- rate limit、session、cache、lock、idempotency 都按 tenant 隔离；
- API 不允许从普通请求体直接信任 tenant_id。

建议内部标识：

```text
tenant_id
internal_space_id
source
source_space_id
user_id
```

业务 Repository 统一接收 `TenantScope`，避免遗漏 tenant 条件。

---

# 6. 其他工程问题

## 6.1 Alembic Baseline 不应调用当前 Base.metadata

第一版 migration 使用当前 `Base.metadata.create_all()`，会导致 migration 历史随代码变化。

处理方式：

1. 当前尚未生产上线，尽快生成固定 baseline；
2. migration 内使用明确 `op.create_table()`；
3. 禁止 migration 导入运行时 Schema；
4. CI 增加：
   - 空库升级到 head；
   - 旧 revision 升到 head；
   - downgrade 一版再 upgrade；
   - schema diff 必须为空。

---

## 6.2 测试 API 必须增加环境开关和认证

`/v1/commands` 仅用于 Stage 4，不应默认成为公开入口。

增加：

```text
SUIXINJI_TEST_API_ENABLED=false
SUIXINJI_TEST_API_TOKEN=...
```

要求：

- 非 dev/stage4 环境禁用；
- 仅绑定 `127.0.0.1`；
- 使用 Bearer Token 或 HMAC；
- tenant/user 从认证上下文推导；
- 日志不能记录正文和 Token。

---

## 6.3 时间字段统一为 TIMESTAMPTZ

Memory、Version、Decision、Trace、Delivery 等部分时间字段仍使用 String。

建议迁移：

```text
created_at
updated_at
valid_from
valid_until
last_confirmed_at
last_accessed_at
started_at
completed_at
lease_expires_at
```

统一使用 timezone-aware `TIMESTAMPTZ`，API 边界再转换成 ISO 字符串。

---

## 6.4 补充外键或明确软引用策略

重点字段：

- `memory_sources.note_id`；
- `memory_decisions.note_id`；
- `memory_traces.note_id`；
- `note_relations.target_note_id`；
- `memory_relations.decision_id`。

真实外键与审计保留可能冲突，需明确：

- 业务数据使用外键；
- 审计数据可以保存 source snapshot/hash；
- Purge 时按隐私要求清理原文，但允许保留无原文统计记录。

---

## 6.5 Embedding 维度配置与实现不一致

当前配置允许设置 `EMBEDDING_DIMENSION`，但 Schema 和 Repository 写死 1024。

二选一：

### 简化方案

- 正式声明只支持 1024；
- 启动时校验；
- 删除“任意可配置”的错觉。

### 完整方案

- 按模型和维度拆表/分区；
- 每个索引只服务固定维度；
- 模型切换通过后台 re-embedding；
- 向量写入使用 upsert，而不是 conflict do nothing。

当前阶段推荐先采用简化方案。

---

## 6.6 Docker Compose 默认端口和密码需要收紧

仓库中的本地基础设施应默认：

```yaml
127.0.0.1:5432:5432
127.0.0.1:6379:6379
```

PostgreSQL 和 Redis 密码全部从 `.env` 获取，不保留固定密码或无密码示例。

---

# 7. 推荐目标架构

## 7.1 记忆主链路

```text
Note Durable
  ↓
Candidate Extraction
  - rules prefilter
  - model candidate extractor
  - extractor/prompt/model version
  ↓
Candidate Persistence
  - candidate-level state
  - evidence span/hash
  ↓
Hybrid Candidate Retrieval
  - structured key
  - pgvector
  - lexical/FTS
  ↓
Adjudication
  - type-specific policy
  - relation confidence
  - destructive precision guard
  ↓
Evolution Transaction
  - memory
  - version
  - source
  - relation
  - decision
  ↓
Review / Feedback / Evaluation
```

## 7.2 查询主链路

```text
Query Router
  ├─ structured fast path
  ├─ note retrieval path
  ├─ current-memory strong-consistency path
  └─ complex ReAct path

Repository
  ├─ indexed SQL filtering
  ├─ pgvector semantic retrieval
  └─ hybrid rerank

Answer
  ├─ evidence ids
  ├─ memory version
  └─ consistency status
```

## 7.3 并发主链路

```text
Inbox + Root Task + Outbox（同一 PG 事务）
                ↓
Outbox Lease → Redis Stream → Consumer Group
                ↓
Task Claim + Lease Token + Claim Version
                ↓
Handler
                ↓
Token-fenced Complete / Fail / Defer
```

---

# 8. 分阶段实施方案

## 阶段 0：建立可比较基线

### 任务

- [ ] 保存当前 commit 和测试报告；
- [ ] 新增 SQL query count 和分段 latency；
- [ ] 分开记录 Receiver、DB、Queue、Worker、LLM、Embedding；
- [ ] 跑一次无 Chaos、Fake External 的干净基线；
- [ ] 建立 300 条以上记忆质量集。

### 产物

```text
docs/metrics/baseline_clean.json
docs/memory_eval/baseline.json
```

---

## 阶段 1：记忆正确性修复

### 必做

- [ ] 持久化 memory_candidates；
- [ ] 记录 extractor/policy/model/prompt 版本；
- [ ] 引入 memory_key；
- [ ] 重写 Candidate Retrieval 为 hybrid；
- [ ] 收紧 merge/supersede/conflict；
- [ ] 删除 Agent/RAG 硬编码 consolidation；
- [ ] 增加 reject/edit/resolve 命令；
- [ ] Candidate 级状态与重试幂等；
- [ ] valid_until 过滤和 expiry worker。

### 验收

- destructive false positive < 1%；
- Candidate Recall@20 ≥ 0.95；
- 重复执行同一 Note 不新增 Memory/Version/Source；
- 失败 Candidate 重试不重跑已成功 Candidate；
- 任意业务领域均不依赖 Agent/RAG 关键词。

---

## 阶段 2：数据库和查询性能

### 必做

- [ ] 消除 Memory N+1；
- [ ] Note 工具改专用 SQL；
- [ ] 增加必要索引；
- [ ] access_count 改 Redis 批量回写；
- [ ] ReAct 增加 Fast Path；
- [ ] embedding query/result cache；
- [ ] 加入 `EXPLAIN ANALYZE` 基准。

### 验收

- filter/list_recent/get_note 不加载全空间 Notes；
- list 100 Memory SQL 次数 ≤ 3；
- Fake External 下干净测试 queue wait p95 显著下降；
- 数据从 1000 增至 10000 条时，常见查询延迟不线性增长 10 倍。

---

## 阶段 3：高并发和任务所有权

### 必做

- [ ] Task lease token / claim version；
- [ ] 所有 complete/fail/defer 加 fencing；
- [ ] Outbox lease + 事务外 Redis publish；
- [ ] reclaim 周期化和游标；
- [ ] Redis 降级 backpressure；
- [ ] 全局数据库连接预算；
- [ ] 双 watermark 和按意图一致性；
- [ ] `(space_id, memory_key)` 粒度锁。

### 验收

- 旧 Worker 的迟到结果无法修改任务；
- Redis/Worker 抖动后无重复业务副作用；
- 1000 跨空间并发最终守恒；
- 单空间连续消息保持必要顺序；
- 无关 Note 不再被 Memory 全局串行阻塞；
- 数据库连接峰值不超过预算。

---

## 阶段 4：多租户、安全和迁移

### 必做

- [ ] tenant 贯穿 DB/Redis/API；
- [ ] 重做固定 Alembic baseline；
- [ ] 测试 API 环境开关和鉴权；
- [ ] 时间字段迁移 TIMESTAMPTZ；
- [ ] 外键与清理策略；
- [ ] Compose 默认绑定本机并使用密码。

### 验收

- 两个 tenant 使用相同 source_space_id 不串数据；
- Redis key 不碰撞；
- 未鉴权无法访问测试 API；
- fresh upgrade / historical upgrade / downgrade-upgrade 全通过；
- 删除 tenant 后无业务孤儿数据。

---

# 9. 文件级修改清单

| 文件 | 主要修改 |
|---|---|
| `memory/models.py` | 增加 memory_key、polarity、scope、candidate state、版本字段 |
| `memory/extractor.py` | 通用结构抽取、版本化、减少硬编码实体 |
| `memory/candidate_retriever.py` | 混合召回，不再 list 100 后 Python 全扫 |
| `memory/adjudicator.py` | 基于 memory_key 的类型化审理，收紧破坏性动作 |
| `memory/consolidator.py` | 删除 Agent/RAG 硬编码，改通用聚类 consolidation |
| `memory/service.py` | Candidate 级状态、审核命令、过期处理 |
| `memory/retriever.py` | 结构化 + vector + lexical rerank，时间有效性 |
| `repositories/postgres/memory.py` | 批量预取、专用搜索 SQL、Candidate 表、无 N+1 |
| `repositories/postgres/notes.py` | type/tag/recent/relation 专用 SQL |
| `agent/query_agent.py` | Fast Path、一致性模式、不再 load_index 全扫 |
| `repositories/postgres/tasks.py` | lease token、claim version、fenced update |
| `runtime/streams/worker.py` | 传递 token，拒绝 stale completion |
| `runtime/streams/client.py` | reclaim 游标和周期策略 |
| `repositories/postgres/outbox.py` | Outbox lease、退避、dead 状态、事务外发布 |
| `apps/outbox_relay.py` | claim/publish/confirm 三段式 |
| `repositories/postgres/dispatch.py` | 双 watermark、tenant scoped idempotency |
| `infrastructure/redis_keys.py` | 所有 key 增加 tenant scope |
| `infrastructure/schema.py` | Candidate、lease、时间类型、索引、外键 |
| `apps/api.py` | 测试开关、认证、禁止信任请求体 tenant |
| `alembic/versions/*` | 固定 baseline 和后续增量迁移 |
| `runtime/distributed_metrics.py` | 分段延迟、连接池、query count、watermark lag |
| `tests/*` | 记忆真实样例、stale worker、tenant 隔离、性能回归 |

---

# 10. 必须新增的测试

## 10.1 记忆测试

- [ ] 条件偏好：“工作日喝咖啡，周末喝茶”；
- [ ] 比较偏好：“比起咖啡更喜欢茶”；
- [ ] 否定范围：“不是不喜欢咖啡，只是晚上不喝”；
- [ ] 事实变化：“住在北京” → “搬到上海”；
- [ ] 同实体不同主题：“喜欢咖啡”与“喜欢在咖啡店学习”；
- [ ] 任务同名不同上下文；
- [ ] 过期 Memory 不进入当前查询；
- [ ] partial retry 只重试失败 Candidate；
- [ ] consolidation 无领域硬编码；
- [ ] reject/edit/resolve review 全流程。

## 10.2 高并发测试

- [ ] stale Worker completion fencing；
- [ ] 同 task 重复 Stream 消息只执行一次副作用；
- [ ] Outbox publish 成功但确认失败后的重复发布；
- [ ] Redis 暂停后 backpressure 生效；
- [ ] PostgreSQL pool 耗尽时快速失败而不是无限挂起；
- [ ] 1000 个不同 space 并发；
- [ ] 单 space 100 条消息的顺序与吞吐；
- [ ] tenant A/B 相同 message_id、space_id 不碰撞。

## 10.3 性能回归测试

- [ ] 每个 Repository 操作 SQL 次数上限；
- [ ] 100、1000、10000 Notes 查询延迟；
- [ ] 100、1000、10000 Memories 候选召回延迟；
- [ ] p50/p95/p99 分段指标；
- [ ] 无 Chaos 和有 Chaos 报告分开。

---

# 11. 建议的验收指标

## 正确性

```text
conservation_delta = 0
pending = 0（测试结束后）
dead_letter = 0（正常场景）
memory_gap = 0（正常场景）
错误 destructive mutation < 1%
重复业务副作用 = 0
跨 tenant 数据泄漏 = 0
```

## 性能

开发环境使用 SSH 反向数据库，因此绝对延迟只作为开发参考。更重要的是改造前后相对变化。

建议目标：

```text
干净 Fake External 100 用户 / 1000 请求：
- 所有任务在 120 秒内收敛
- queue wait p95 < 5 秒
- filter/list_recent DB 部分 p95 < 200 ms
- memory candidate retrieval DB 部分 p95 < 300 ms
- Receiver acceptance p95 相比当前基线下降至少 70%
```

正式同机房环境再设更严格目标。

## 资源

```text
PostgreSQL 总连接数不超过预设预算
Redis reclaim 不持续占据主循环
DB query count 不随结果条数线性增加
单热点 space 不拖慢其他 space
```

---

# 12. 不建议现在做的事情

1. 不要在没有修复 lease fencing 前把 Worker 部署到多台真实服务器。
2. 不要通过继续增加 Worker 掩盖 N+1、全量扫描和强串行问题。
3. 不要把当前带 Chaos 的 Stage 4 延迟写成性能成绩。
4. 不要直接让 LLM 决定 Memory 数据库更新。
5. 不要降低 destructive action 阈值追求“自动记得更多”。
6. 不要在当前 Alembic baseline 上长期叠加更多迁移而不先修正历史迁移。
7. 不要把测试 API 暴露到公网。
8. 不要让 Redis 成为事实存储；PostgreSQL 仍应是最终事实来源。

---

# 13. 最终推荐顺序

```text
第一步：Candidate 持久化 + memory_key + 混合召回
第二步：收紧关系审理 + 通用 consolidation + 真实评测集
第三步：消除 Note 全扫和 Memory N+1
第四步：Task lease fencing + Outbox lease
第五步：双 Watermark，缩小同空间串行范围
第六步：tenant 全链路隔离
第七步：固定 Alembic baseline、安全和时间 Schema
第八步：重新跑干净容量测试与 Chaos 测试
```

完成前四步后，项目的“长期记忆 Agent”核心可信度会明显提升；完成前六步后，才适合把它描述为具备真实多用户、多进程扩展能力的 Agent 系统。
