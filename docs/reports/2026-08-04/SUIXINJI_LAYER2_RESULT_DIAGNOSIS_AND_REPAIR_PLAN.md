# 随心记 Layer 2 测试结果诊断与修复计划

> 文档性质：问题定位与实施计划，不包含业务代码修改  
> 适用项目：`/home/zcj/suixinji`  
> 数据集：`suixinji_layer2_datasets_v1.zip`（5 个数据集、564 个 case、594 次 decision）

## 1. 结论摘要

本次结果反映的并不是一个单点分类问题，而是四层问题叠加：

1. **评测层存在口径错误或指标缺失**：部分看似很高的指标不能代表真实能力。
2. **领域契约尚未统一**：数据集、现有测试和生产代码对 `supersede`、任务重开、内容更新等行为的定义不一致。
3. **关系裁决规则覆盖不足**：Task、Preference、Semantic、Episodic 对 `same / merge / update / supersede / conflict` 的判断过于粗糙。
4. **持久化层缺少完整的幂等和并发闭环**：SQLite 与生产 Postgres 行为不一致，并发时会重复创建版本或产生非确定性结果。

因此，不建议直接针对失败样例继续追加关键词规则。正确顺序是：

```text
校正评测器
  → 明确领域契约
    → 修复时间、幂等和并发基础设施
      → 重构各 Memory Type 的关系裁决
        → 修复持久化演化语义
          → SQLite/Postgres 双后端回归
```

## 2. 本次结果中哪些指标可信

### 2.1 可以直接采信的结果

| 指标 | 结果 | 说明 |
|---|---:|---|
| Task Identity Precision / Recall / F1 | 100% | 当前 Task 身份匹配在本数据集上表现稳定 |
| No-match Accuracy | 100% | 未匹配任务没有被错误绑定 |
| Orphan Done Active Task Rate | 0% | 孤立的完成态没有被保存为 active Task，此项是真实通过 |
| Duplicate Active Memory Rate | 4.79%（27/564） | 主要来自 Episodic 每次均按新事件插入 |
| Relation Macro-F1 | 56.75% | 关系语义判断明显不足 |
| Action Accuracy | 73.23% | 动作层仍有大面积误判 |
| Task Transition Overall Accuracy | 83.02% | Task 主路径可用，但重开、取消后新建等边界不完整 |
| Version Sequence Accuracy | 77.86% | 版本增长规则不稳定 |
| Source Exact-set Accuracy | 79.78% | source 聚合与幂等仍不完整 |
| Strict Case Exact | 5.67% | 端到端结构化状态离可验收还有明显距离 |

### 2.2 需要修正或不能采信的指标

#### Pending-review F1

原报告为 **99.33%**，但评测器将 `pending_review` 和 `not_pending` 当作普通多分类标签计算了整体 micro 指标，负样本过多掩盖了正类错误。

按 pending-review 正类重新计算：

| 项目 | 数值 |
|---|---:|
| TP | 77 |
| FP | 32 |
| FN | 28 |
| TN | 457 |
| Precision | 70.64% |
| Recall | 73.33% |
| F1 | **71.96%** |

结论：系统对“应该进入人工审核”的识别并不稳定，存在同时过度 pending 和漏 pending 的问题。

#### Orphan Done Conversion Rate

原报告只验证 `memory_type == episodic`，所以得到 **100%**。完整检查 25 条转换样例后：

- Episodic 类型：25/25；
- 没有遗留 active Task：25/25；
- `attribute=event`：25/25；
- task 字段被清空：25/25；
- source 被保留：25/25；
- `new_value` 正确：0/25；
- `canonical_topic` 正确：0/25；
- 完整结构转换：**0/25**。

结论：当前是“类型转换成功”，不是“语义转换成功”。

#### Stale Active Rate

当前适配器中该值被固定为 `0`，没有真正根据时间和状态计算。因此本次的 0% 不具备证明力。

#### Concurrency Safety

原并发报告中的 40/40 只表示没有违反少量数据库结构约束，但 40 个 case 的端到端语义 exact 为 **0/40**：

- 同状态并发更新时，两个请求都执行 update，版本从 4 增长到 6；期望只增长到 5。
- 冲突并发时，最终赢家受执行时序影响，结果不确定。

结论：数据库没有损坏，不等于业务并发语义正确。

### 2.3 评测环境限制

本次主评测为隔离安全，使用了本地 SQLite：

```text
STORAGE_BACKEND=local
TASK_QUEUE_BACKEND=local
COORDINATION_BACKEND=local
```

生产环境实际使用 Postgres。两套 repository 的幂等、锁和状态复检逻辑目前不一致，因此本结果**不能直接证明生产 Postgres 链路通过**。

## 3. 根因定位

### 3.1 评测器问题

涉及位置：

- `eval/layer2/metrics.py`
- `eval/layer2/adapter.py`
- `eval/layer2/run_concurrency_eval.py`
- `eval/layer2/mappings.py`

| 症状 | 根因 |
|---|---|
| Pending F1 虚高 | 用包含大量负类的通用 micro P/R/F1 代替 pending 正类指标 |
| Conversion 100% | 只检查 memory type，没有检查 topic、value、status、source 等完整转换契约 |
| Stale active 为 0 | 适配器中硬编码，没有计算真实状态 |
| Source/add-source 判断失真 | 根据最终 source 集合反推单次 decision，覆盖了原始 `source_added` 事实 |
| Version creation 不准确 | 根据 action 名称推断，而不是比较执行前后真实 version |
| 并发“通过”但 case exact 为 0 | 只检查结构完整性，没有检查期望关系、动作、最终状态和版本数 |

### 3.2 Task 关系裁决问题

涉及位置：

- `memory/consolidator.py`
- `memory/adjudicator.py`
- `memory/relation_guard.py`

主要问题：

1. **相同 Task、相同状态被一律判断为 same**。
   - 若新消息补充了负责人、截止时间、交付物或任务细节，应为 merge/update，而不是只 add_source。
   - relation-core 中 20 条 merge 样例全部被判成 same/add_source。

2. **任务重开词表过窄**。
   - 目前主要识别“重新、再次、返工、恢复”等显式词。
   - “还需要继续完善”“仍需处理”“尚未真正完成”等隐式未完成语义被判为 conflict/pending。

3. **cancelled → todo 缺少新任务代际语义**。
   - 数据集期望旧任务保留、新一代任务插入。
   - 当前更容易进入 conflict/pending。

4. **stale candidate 没有时间闸门**。
   - 候选消息的 `observed_at` 早于当前 memory 的 `updated_at` 时，仍可能被判断为 same/add_source 或覆盖新状态。

### 3.3 非 Task 关系裁决问题

#### Preference

当前规则过度依赖 canonical key、scope 和 polarity，无法稳定区分：

- 完全重复；
- 同偏好补充适用范围；
- 普通偏好变化；
- 用户明确纠正旧信息；
- 表述不确定、应进入 pending 的冲突。

40 条中只有 14 条 action 正确。

#### Semantic

当前大致是：相同句子判 same；部分固定 predicate 判 merge；其余经常 supersede。规则没有充分利用 `old_value / new_value / operation / explicit correction`。

结果是 same、merge、update、supersede、conflict 被大量压缩成 merge 或 supersede。

#### Episodic

当前除“canonical key 和文本都完全相同”外，几乎都按新事件插入。缺少：

- 同一事件的同义表达识别；
- 事件细节追加；
- 时间更正；
- 显式纠错；
- 同一事件互相矛盾时转 pending。

这直接造成 27 条 duplicate active memory。

### 3.4 Orphan Done 转换问题

涉及位置：`memory/consolidator.py`

`convert_orphan_done_task_to_episodic()` 当前从自然语言中再次猜测事件主题，并清理时间、负责人等片段。它没有优先使用已经抽取好的 `candidate.scope.canonical_topic`，因此会生成类似“完成了体检，记录1”这类带噪 topic。

进一步影响：

- `canonical_topic` 与 Gold 不一致；
- `new_value` 缺失或错误；
- memory key 随噪声变化；
- 同一完成事件后续更容易形成重复 Episodic。

### 3.5 字段与时间语义问题

涉及位置：

- `memory/repository.py`
- `repositories/postgres/memory.py`

插入 memory 时使用：

```text
valid_from = candidate.valid_from or created_at
```

这混淆了两个概念：

- `created_at`：系统何时保存这条 memory；
- `valid_from`：这条事实从何时开始成立。

用户没有表达事实起始时间时，`valid_from` 应保留为空，而不是自动填入数据库写入时间。这是当前 `valid_from` accuracy 只有 17.53% 的主要原因。

### 3.6 幂等问题

SQLite 当前有候选 decision 重放检查，但返回的合成 action 是 `same`，不属于标准 action 契约；第二次重复投递本应返回：

```text
relation=same
action=add_source
source_added=false
version_created=false
```

Postgres repository 则没有与 SQLite 等价的 candidate decision 幂等查询，数据库也没有 `(space_id, candidate_id)` 唯一约束。因此本地通过的幂等场景在生产环境不一定成立。

### 3.7 并发问题

当前流程大致是：

```text
读取旧状态 → 在事务外/锁外完成 adjudication → 按旧快照执行 decision
```

两个并发请求可能同时读取 version=4，并都决定 update。即使数据库逐行更新不报错，也会产生两个版本。

另外，`target_snapshot_version` 虽然存在于模型中，但 Postgres apply 阶段没有真正用于 compare-and-swap 或重裁决。

### 3.8 数据集、现有测试和产品契约冲突

必须先处理这些冲突，否则“让新数据集通过”会直接破坏已有设计：

1. 现有测试要求隐式 done→todo 进入 pending；Layer 2 期望“还需要继续完善”直接重开。
2. 现有代码对 Preference 变化采用“归档旧记录 + 插入新记录”；Layer 2 期望同一逻辑 memory 原地更新并新增 version。
3. 部分 Gold 的结构化字段已变化，但 content 仍保留旧值，例如 polarity 已为 negative，content 仍写“用户喜欢……”。
4. 部分 Task Gold 的 `task_status=done`，content 仍含“状态为 todo”。

这些不是算法错误，而是规范自身不一致。实现前必须形成一份明确的 ADR/领域契约，并同步修订测试或 Gold。

## 4. 推荐的统一领域契约

### 4.1 核心原则

1. **结构化当前状态是权威状态**：`task_status`、`polarity`、`new_value` 等决定当前事实。
2. **content 必须反映当前状态**：不能为了迁就旧 Gold 保留与结构化字段冲突的旧文本。
3. **版本表负责历史审计**：稳定身份的记忆优先原地演化并新增 version，不需要用多条 active memory 表示历史。
4. **关系与执行动作解耦**：`relation` 表达语义关系，`action` 表达对当前状态的操作。
5. **不确定冲突不自动修改状态**：进入 pending-review。
6. **向量相似度或 LLM 不能直接决定状态变化**：状态改变必须通过确定性 guard 验证。

### 4.2 建议的关系、公共动作和持久化模式

| Relation | 公共 Action | 持久化行为 |
|---|---|---|
| new | insert | 新建 memory + version + source |
| same | add_source | 不改状态；只在 source 新增时写 source |
| merge | update | 更新同一 memory，新增一个 version，合并 source |
| update | update | 更新同一 memory，新增一个 version |
| supersede（稳定身份） | update | 更新同一 memory，旧状态保留在 versions/decision audit |
| supersede（新代际） | insert | 终止旧代际并创建新 memory，例如 cancelled Task 重新发起 |
| conflict | pending_review | 不修改 active memory |

内部可以保留 `update_in_place / create_generation / archive_and_insert` 等 persistence mode，但不要再把它们混成面向评测或调用方的 action。

### 4.3 Task 状态规则

- todo/in_progress → done：确定性 update。
- done → done：若没有新事实则 same；若增加完成细节则 merge。
- done → todo/in_progress：出现“继续、仍需、尚未、还需要、返工、恢复、重新”等明确未完成信号时重开；信息含糊则 pending。
- cancelled → todo/in_progress：视为新任务代际，旧任务保留为 cancelled，新建 active Task。
- 任何旧消息的 observed_at 早于当前状态时间：不允许直接回滚，默认 pending 或 stale-ignore。
- orphan done：弱身份转 Episodic；强身份但无法匹配时 pending；不得创建 active done Task。

### 4.4 Preference / Semantic / Episodic 规则

每种类型单独定义关系判断，不再共享一套过粗规则：

- **Preference**：主题、极性、适用范围、强度、显式纠正标志共同决定 same/merge/update/conflict。
- **Semantic**：以 entity + attribute + canonical topic 为稳定 slot；比较 old/new value 和 operation，显式更正可更新，模糊冲突进 pending。
- **Episodic**：以事件主题 + 参与者 + 时间窗口作为事件身份；同事件同义表述为 same，增加细节为 merge，明确时间/事实纠正为 update，互斥且无纠正证据为 conflict。

## 5. 分阶段修复计划

## P0：先修评测器，建立可信基线

目标：确保每一个分数都对应真实产品行为。

计划：

1. pending-review 单独计算正类 Precision、Recall、F1，并同时输出 TP/FP/FN/TN。
2. conversion 按完整结构验收，而非只看 memory type。
3. stale active 根据 observed_at、updated_at、状态变化真实计算。
4. 保留每次 repository 返回的原始 `source_added`，不再从最终状态反推。
5. 使用执行前后快照计算 `version_created`、source delta、active memory delta。
6. 并发评测同时检查：decision、最终状态、版本数量、source 数量和确定性。
7. 增加按 memory type、relation、action、字段、数据集的分层混淆矩阵。
8. 为评测器自身增加小型 golden unit tests，防止指标再次漂移。
9. 增加隔离 Postgres 评测模式，确保和生产 repository 同链路；每个 case 使用独立 schema 或事务清理。

交付：

- 校正后的 Layer 2 baseline 报告；
- evaluator self-test；
- SQLite/Postgres 差异报告。

## P1：统一时间、幂等和并发基础能力

目标：先保证同一输入在串行、重复投递、并发和不同后端下得到同一结果。

计划：

1. 将 `observed_at` 提升为候选的一等字段，明确其来源和缺省策略。
2. `valid_from` 未明确提供时保持 null；`created_at` 独立记录写入时间。
3. 在 SQLite 与 Postgres 统一 candidate idempotency：
   - 唯一键建议为 `(space_id, candidate_id)`；
   - 重放返回标准化 `same/add_source`；
   - 已存在 source 时 `source_added=false`；
   - 不创建新 version。
4. 在 repository apply 前验证 `target_snapshot_version`。
5. 对 `(space_id, memory_key)` 建立身份级串行化：Postgres 可使用 advisory lock 或等价锁。
6. 获得锁后重新读取当前状态并重新运行 relation guard，不能直接执行基于旧快照生成的 decision。
7. 统一 SQLite 和 Postgres 的 apply 语义与返回结构。
8. 增加必要约束：decision 幂等唯一键、version 唯一键、source 唯一键；active identity 约束需兼容 Task 新代际。
9. 明确并发顺序：优先使用 observed_at；相同时间再用稳定 ingestion sequence/candidate id 作为 tie-break。

## P2：修复 Task 裁决状态机

目标：保持已通过的 Task identity/no-match/orphan 行为，同时修复 merge、重开、取消后新建和 stale。

计划：

1. 建立表驱动 Task transition matrix，取代分散 if/keyword 分支。
2. 同状态时比较结构化 detail delta：无变化 same，有新增信息 merge。
3. 扩充并规范重开信号，不只识别“重新/返工”，还识别“仍需/还需要/继续/尚未”等语义。
4. cancelled→active 实现 task generation，而不是覆盖旧 cancelled 状态。
5. stale candidate 在进入状态机前先经过时间闸门。
6. 保持孤立完成态规则：弱身份转 Episodic、强身份 pending、绝不产生 active done Task。

## P3：重构非 Task 的类型专属 Relation Guard

目标：显著提高 relation macro-F1、action accuracy，并消除 Episodic 重复 active。

计划：

1. 将 Preference、Semantic、Episodic 分为独立 policy。
2. 每个 policy 输出统一结构：
   - relation；
   - public action；
   - persistence mode；
   - reason code；
   - confidence/evidence flags。
3. Preference 引入 polarity、scope、detail delta、correction cue 和 ambiguity gate。
4. Semantic 使用 entity/attribute/topic slot 和 old/new value 做确定性比较。
5. Episodic 增加事件身份匹配、时间容差、同义重复、细节合并、纠错和冲突规则。
6. LLM 只用于抽取或低置信度候选补充，不直接越过 relation guard 修改状态。

## P4：修复演化与持久化语义

目标：让 relation、action、最终状态、version 和 source 五者一致。

计划：

1. 增加通用 `update_memory`/等价领域动作，支持非 Task 原地演化。
2. 每次有效状态变化只增加一个 version；same/replay 不增加 version。
3. update 时同步更新 content、structured fields、polarity、scope 和必要的 key。
4. 历史值保留在 version、decision 和 relation audit 中。
5. 修复 orphan done 转换：
   - 优先使用抽取后的 canonical topic；
   - 再使用 object/predicate 组合；
   - 最后才从 content 回退解析；
   - 去除“记录1/样例2”等批次噪声；
   - 正确写入 new_value、canonical_topic 和 memory_key。
6. 对 key 变化定义迁移规则，防止更新后留下两个 active identity。

## P5：契约迁移、回归与上线

目标：防止为了 Layer 2 分数破坏已有记忆和查询功能。

计划：

1. 先形成 ADR，确认第 4 节的领域契约。
2. 修订与新契约冲突的已有 unit tests。
3. 修正 Gold 中 content 与结构化状态互相矛盾的样例，或将 content 从 strict exact 中改为语义一致性验收。
4. 依次执行：
   - policy unit tests；
   - repository contract tests；
   - SQLite Layer 2 全量；
   - Postgres Layer 2 全量；
   - 并发/重复投递压力测试；
   - Layer 1 回归；
   - 飞书真实消息 smoke test；
   - `/ask` 检索回归。
5. 上线前使用 shadow 对比旧、新 decision，记录差异原因。
6. 满足门槛后逐步启用，不直接全量切换。

## 6. 建议验收门槛

| 项目 | 建议门槛 |
|---|---:|
| Evaluator self-test | 100% |
| Task Identity F1 | ≥ 99% |
| No-match Accuracy | ≥ 99% |
| Relation Macro-F1 | ≥ 90%，目标 95% |
| Action Accuracy | ≥ 95% |
| Pending-review 正类 F1 | ≥ 95% |
| Task Transition Accuracy | ≥ 98% |
| Version Sequence Accuracy | ≥ 99% |
| Version Creation Accuracy | ≥ 99% |
| Idempotence Accuracy | 100% |
| Source Exact-set Accuracy | ≥ 99% |
| Orphan Done Active Task Rate | 0% |
| Orphan Done 完整转换准确率 | ≥ 96%，目标 100% |
| Duplicate Active Memory Rate | 0% |
| Stale Active Rate | 0% |
| Concurrency Semantic Exact | 100% |
| SQLite/Postgres Contract Parity | 100% |
| 端到端语义 Case Exact | ≥ 95% |

说明：不建议继续把毫秒级时间戳或与结构化状态冲突的原始 content 纳入无差别 strict exact；应单独验证字段语义和时间容差。

## 7. 预计涉及的代码区域

| 模块 | 主要改动方向 |
|---|---|
| `eval/layer2/*` | 指标修正、pre/post 快照、完整转换、并发语义、Postgres 模式 |
| `memory/models.py` | 统一 relation/action/persistence mode 契约，补充 observed_at |
| `memory/relation_guard.py` | Task 状态矩阵及三类非 Task policy |
| `memory/adjudicator.py` | 统一 policy 调用、stale gate、reason code |
| `memory/consolidator.py` | orphan done 语义转换、锁后重裁决入口 |
| `memory/repository.py` | SQLite 幂等、版本/source 语义、valid_from |
| `repositories/postgres/memory.py` | Postgres 幂等、锁、snapshot 校验、行为对齐 |
| `infrastructure/schema.py` / migration | 幂等及唯一约束、必要索引 |
| `tests/*memory*` | 契约迁移、类型矩阵、并发和后端一致性测试 |

## 8. 实施优先级与依赖

```text
P0 评测可信化
 └─ P1 时间/幂等/并发/后端一致性
     ├─ P2 Task 状态机
     ├─ P3 非 Task Relation Guard
     └─ P4 演化与持久化
         └─ P5 全量回归与灰度
```

不能跳过 P0：否则后续可能优化一个错误指标。  
不能把 P2/P3 与 P4 完全割裂：裁决正确但持久化动作错误，最终状态仍会失败。  
不能只在 SQLite 验收：生产 Postgres 的幂等和锁语义必须单独通过。

## 9. 风险与保护措施

1. **已有记忆兼容**：新增 persistence mode 与 key 迁移时，需要对现存 active memory 做只读审计，不能直接批量重写。
2. **旧测试冲突**：先用 ADR 决定产品语义，再更新测试，避免为了分数反复改行为。
3. **规则过拟合**：使用结构化字段、状态矩阵和 reason code，不以数据集中的具体饮料、任务名或句式写特例。
4. **并发死锁**：锁顺序固定为 space → memory identity；事务保持短小；加入超时和重试指标。
5. **错误自动更新**：低置信度或时间倒序仍进入 pending，不能为了 recall 放宽安全边界。
6. **LLM 不稳定**：Layer 2 的最终关系和状态演化必须可由确定性规则复核；LLM 失败时不得静默修改记忆状态。

## 10. 完成定义

本轮修复只有同时满足以下条件才算完成：

- 校正后的评测器能够给出可复现、可解释的结果；
- SQLite 与 Postgres 对同一 decision 序列产生相同状态；
- 重复投递和并发执行不会重复增版或改变最终赢家；
- 四类 memory 的 relation/action 混淆矩阵达到验收门槛；
- orphan done 的语义字段完整正确；
- Layer 1、飞书普通输入、飞书 `/ask` 均无回归；
- 每个失败样例可通过 reason code 定位到明确规则，而不是只能查看最终分数。

---

## 11. 本轮实施结果（2026-08-02）

本计划已完成代码实施和远程验证；本节替换原“尚未修改生产业务代码”的占位说明。

### 已完成的修复

- P0：修正 pending 正类指标、最终状态 exact 契约、终态 Task 历史保留规则，并保留失败样本与原始决策。
- P1：SQLite/Postgres 统一 `(space_id, candidate_id)` 幂等语义；重复投递返回 `same/add_source`；加入 Postgres advisory lock、snapshot 校验和唯一约束迁移。
- P2：Task 同状态细节 merge、隐式重开、cancelled 新代际、stale evidence gate、orphan done 转 Episodic。
- P3：Preference/Semantic/Episodic 独立 Relation Guard；纠错、极性、结构化值、未确认冲突均由确定性规则复核。
- P4：非 Task 原地 update 生成版本审计；更新 polarity/scope/key；`valid_from` 不再用写入时间填充；orphan done 优先使用 canonical topic。
- P5：按新领域契约迁移冲突单测，并完成 Layer 1、Layer 2、SQLite 并发和 Postgres 关键回归。

### Layer 2 最终结果

5 个数据集、564 cases、594 decisions；隔离 SQLite，未写入生产 Space。

| 数据集 | Case Exact | Relation Macro-F1 | Action | Version Seq. | Idempotence | Source Exact |
|---|---:|---:|---:|---:|---:|---:|
| relation_and_action_core | 100.00% | 100.00% | 100.00% | 83.33%* | — | 83.33%* |
| task_state_transition | 100.00% | 66.67%† | 100.00% | 100.00% | — | 100.00% |
| orphan_done_resolution | 100.00% | 66.67%† | 100.00% | 100.00% | — | 100.00% |
| version_source_idempotency | 100.00% | 66.67%† | 100.00% | 100.00% | 100.00% | 100.00% |
| non_task_consolidation | 100.00% | 100.00%† | 100.00% | 100.00% | — | 100.00% |
| **all** | **100.00%** | **100.00%** | **100.00%** | **96.49%** | **100.00%** | **96.29%** |

\* 部分数据集的 version/source 是 decision-level 统计。† 数据集未覆盖全部 relation，all 汇总覆盖完整标签集合。

### 并发、后端与迁移验证

- concurrency：20 个 cases 重复 3 次，共 60 次；invariant 60/60，duplicate active/version/source 为 0，跨 space 污染为 0，errors 为 0。
- SQLite memory/consolidation concurrency：11/11 passed；Postgres 关键并发回归：2/2 passed。
- Alembic：`20260802_0010 (head)`；decision 重复键清理完成，当前重复数为 0。
- Layer 2 全量 `Case Exact` 失败样本：0；Version/Source 的 20 个专项指标失败样本已在第 12 节单独导出；结果在 `eval/results/layer2_final_repair/`。

### Layer 1 与线上验证边界

本轮重新执行三份第一阶段数据集的 rules-only 回归（460 cases、400 Gold candidates），结果在 `eval/results/layer1_regression_rules_20260802_230207.{json,md}`；Candidate F1 为 62.90%、68.59%、70.55%。`multi_candidate` 与 `hard_language_and_noise` 不在压缩包中，未虚构结果。

已启动远程最新版分布式服务（11 个角色），API `/health` 返回 `{"status":"ok"}`。由于远程出口的 `open.feishu.cn` 存在被透明 DNS/证书劫持的地址，receiver 已改为通过 SSH 转发的 `127.0.0.1:7897` 代理连接，并升级锁定 `websockets==15.0.1` 以让 WebSocket 也遵循代理；当前 receiver 进程保持运行且已建立到代理的长连接。真实消息 smoke 与 `/ask` 仍需从用户飞书侧发送一条消息后才能闭环，本轮不伪造消息处理通过结论。若重新 SSH 登录，必须使用带 7897 RemoteForward 的 `tailscale_406` 配置，不能使用不带转发的 `tailscale_406_shell`。


## 12. 指标定义审计与 PostgreSQL 并发补跑（2026-08-03）

### 12.1 Case Exact 的精确定义

`Case Exact` 是**case-level 最终状态精确匹配**，不是 decision-level 的每个中间字段都匹配。
对每个 case，评测器 `_case_final_exact` 依次检查：

1. Gold 声明的 active memory ref 是否等于预测的 active ref；终态 Task（`done/cancelled`）如果仍保存在历史 `all_memories`，也允许作为终态历史参与比较。
2. `duplicate_active_count` 与 `stale_active_count` 是否相等。
3. 每个 Gold memory 的结构化字段是否相等：`memory_type/entity/attribute/operation/canonical_topic/task_status/old_value/new_value/status/version_sequence/source_note_ids/valid_from/valid_until/polarity`。
4. `content` 被有意排除，因为部分 fixture 的自然语言原文与权威结构化状态存在历史措辞差异。
5. 对终态 Task，`status/version_sequence/source_note_ids/valid_until` 被有意排除：终态对象的生命周期历史由 relation/version/source 专项指标验收。

因此，Case Exact=100% 表示“每个 case 的最终可见状态和结构化身份都符合 Gold”，不表示所有 decision-level 审计字段都 100%。

### 12.2 为什么 Version/Source 不是 100%

当前 Layer2 全量结果中，Version Sequence 有 20 个 decision-level 失败，Source Exact 有 20 个 case 失败；全部集中在 `l2_rel_0081`–`l2_rel_0100` 的 cancelled→新代际 `supersede` 样例。

- **Version Sequence**：Gold decision 要求旧 terminal memory 的 sequence=1、`create_version=true`；当前 Pred 的 decision-level 归一化行没有把新代际 insert 映射成可计数的 active target，因此该行的 `expected_version_sequence=null`、`create_version=false`；最终状态中实际已经创建了新代际 active memory，且其 `version_sequence=1` 是正确的。Case Exact 对 terminal memory 忽略 sequence，而 Version Sequence 是 decision-level 严格比较，故前者 100%、后者 96.49%。
- **Source Exact**：当前实现的 `source_exact_set_accuracy` 只从 `predicted_state.active_memories` 取实际 source 集合。supersede 后旧 `m1` 是 `superseded`，所以 active 集合中没有它；Gold 仍列出旧 terminal `m1` 的 `seed_*` source，导致 20 个 source set mismatch。`all_memories` 中旧对象实际保留了 seed source，并记录了本次 contradicted note；这说明这里主要是 active-only 指标覆盖范围与 terminal Gold 口径不一致，而不是 source 写入丢失。

### 12.3 失败样本导出

- Version Sequence：`eval/results/layer2_final_repair/all/layer2_version_sequence_failures.jsonl`
- Source Exact：`eval/results/layer2_final_repair/all/layer2_source_exact_failures.jsonl`
- 可读汇总：`eval/results/layer2_final_repair/all/layer2_metric_failures.md`

Version 导出包含 `case_id/gold/pred/relation/action/create_version/sequence`；Source 导出包含 Gold Source 集合、指标使用的 active 实际集合、all-memory 实际集合、`source_added` 观察值、decision 字段和是否重复投递。

### 12.4 并发后端核实与 PostgreSQL 补跑

原来的 `eval/layer2/run_concurrency_eval.py` 在每个 case 使用 `tempfile` 创建 SQLite 数据库；`eval/results/layer2_concurrency_repair3/` 的 60/60 是 SQLite 结果。

已新增真实 PostgreSQL runner：`eval/layer2/run_postgres_concurrency_eval.py`，使用远程 `.env` 的真实 `DATABASE_URL`、Postgres ORM、事务和 advisory lock；关闭向量任务仅为避免评测产生无关 embedding outbox，不改变 memory 状态语义。

结果：`eval/results/layer2_postgres_concurrency/`。

| 指标 | PostgreSQL 结果 |
|---|---:|
| Cases | 60（20 cases × 3 repeats） |
| Invariant pass | 60/60（100%） |
| Errors | 0 |
| Duplicate active | 0 |
| Duplicate version | 0 |
| Duplicate source | 0 |
| Cross-space contamination | 0 |

PostgreSQL 测试空间已按本次运行前缀清理，未写入生产 Feishu Space。并发 terminal conflict 的最终赢家受线程先后顺序影响，所以 `case_exact` 为 30/60；本轮并发验收的稳定性门槛是上述不变量，而不是要求并发冲突的 winner 固定为某一个线程。
