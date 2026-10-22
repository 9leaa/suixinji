# 随心记评测数据集构造指南

日期：2026-08-13  
适用范围：Layer 1、Layer 2、Layer 3 独立评测，以及 L1→L2、L1→L2→L3 联合评测。

## 1. 核心原则

先生成结构化的 **世界状态与事件计划（world spec）**，再由大模型把它改写为自然语言消息或查询。Gold 应由 world spec 和确定性规则生成，不能由模型看着自然语言自由判断。

这样可以避免两类问题：

- Gold 标签不稳定，例如同一句完成表达被随机标成 Task 或 Episodic。
- 数据集的表达、答案与某个生成模型的习惯绑定，离开同一模板后失去泛化能力。

一个简化的 world spec 示例：

```json
{
  "entities": {
    "user": "用户",
    "project": "Layer3 评测报告"
  },
  "initial_state": {
    "tasks": [
      {
        "task_id": "task_report_round_1",
        "canonical_topic": "Layer3 评测报告",
        "task_family": "layer3-evaluation",
        "status": "todo"
      }
    ]
  },
  "events": [
    {
      "event_id": "e1",
      "kind": "task_progress",
      "target": "task_report_round_1",
      "new_status": "done",
      "source_fact": "用户完成了 Layer3 评测报告"
    }
  ]
}
```

大模型只负责将其自然表达为：

```text
Layer3 的评测报告终于收尾了。
```

而 Gold 仍然由 world spec 确定：

```text
task_report_round_1: todo → done
```

## 2. 五类数据集的边界

| 数据集 | 系统输入 | 测试目标 | 不测试的内容 |
|---|---|---|---|
| L1 独立集 | 原始消息 + 必要前文 | 抽取、分类、证据、字段、指代 | 旧记忆如何更新 |
| L2 独立集 | 已规范化 Candidate + 旧 Memory 快照 | Relation Guard、Adjudicator、版本、来源、待审 | LLM 是否抽对 Candidate |
| L3 独立集 | 已构造好的 Memory/Version/Source 快照 + `/ask` | 检索、历史、回答、引用、安全 | 消息如何形成记忆 |
| L1→L2 桥接集 | 多轮原始消息流 | 抽取是否导致正确记忆演化 | `/ask` 最终回答 |
| L1→L2→L3 全链路集 | 多轮原始消息流 + 最终/中途 ask | 从输入到回答的真实端到端效果 | 无 |

## 3. 共用的状态与身份规范

### 3.1 Task 仅使用二元状态

所有新数据必须统一：

```text
task_status ∈ {todo, done}
```

下列信息不是第三个任务状态：

| 信息 | 正确归属 |
|---|---|
| 卡住、等待 API、依赖他人 | `blocker` / `progress_note` |
| 正在做、完成一半 | `progress_note` |
| 返工、重新开始 | 明确的任务演化事件 |
| 取消、不做了 | closure/review 专项；不能随意扩充状态集合 |

例如：

```text
“报告卡在 API 限流，暂时没完成”
```

应标注为：

```json
{
  "memory_type": "task",
  "task_status": "todo",
  "blocker": "API 限流"
}
```

不能标注为 `task_status = blocked`。

### 3.2 家族、实例、操作分层

| 层 | 含义 | 示例 |
|---|---|---|
| `task_family_key` | 相近任务家族，仅用于召回/排序 | “RAG 评测报告” |
| `task_instance_id` | 某一轮具体任务，用于更新授权 | “RAG 评测报告第一轮” |
| `operation` | 用户对任务执行的动作，不是唯一身份 | 编写、修复、验收、复测 |

数据必须包含“同家族不同实例”干扰：

```text
旧任务：完成 RAG 评测报告第一轮
新消息：第二轮 RAG 评测报告完成了
```

预期：同家族不等于同一个具体任务，系统不得更新第一轮任务。

### 3.3 数据切分按 world，而不是按句子

不能让训练与测试仅仅共享主题、换一种说法：

```text
训练：“我完成了 RAG 报告”
测试：“RAG 报告终于做完了”
```

应按以下维度隔离 train/dev/test：

- 主题词：训练使用 RAG，测试使用 Agent、数据库、求职等。
- 任务结构：训练两轮任务，测试三轮并行任务。
- 表达风格：训练直接句，测试口语、省略、混合语言。
- 指代方式：训练“这个”，测试“上面那个/它/后续那件事”。

### 3.4 生成后的三层校验

```text
JSON/Schema 校验 → 规则一致性校验 → 独立模型或人工审校
```

| 校验层 | 要求 |
|---|---|
| JSON/Schema | 字段类型、source ref、version ref、ID 结构合法 |
| 规则一致性 | evidence span 必须为原文连续子串；版本不能倒序；引用来源必须存在 |
| 独立审校 | 第二个模型或人工确认自然语言没有改变 world spec 事实 |

生成模型不应同时承担最终 Gold 标注与自评工作。

## 4. Layer 1 独立数据集：消息到 Candidate

### 4.1 输入结构

```json
{
  "case_id": "l1_xxx",
  "note": {
    "note_id": "n1",
    "text": "Layer3 的评测报告终于收尾了，不过下周可能还要补一次压测。"
  },
  "previous_messages": [],
  "expected": {
    "should_store": true,
    "candidates": []
  }
}
```

### 4.2 Gold 应包含的字段

| 字段 | 用途 |
|---|---|
| `should_store` | 判断噪声、寒暄、纯猜测、敏感信息 |
| `memory_type` | task / preference / semantic / episodic |
| `content` | 用户可读的记忆表达 |
| `evidence_span` | 强制每条记忆都能回到原文证据 |
| `subject/predicate/object_value` | 结构化检索与状态管理 |
| `task_status` | 仅 task 使用，且只允许 todo/done |
| `polarity` | 仅 preference 使用，区分喜欢/不喜欢 |
| `canonical_topic` | 身份、召回、画像所需的规范主题 |
| `task_instance_id` / family 语义 | 验证实例与家族边界 |
| `blocker/progress_note/closure_reason` | 任务过程信息 |
| `reference_status/antecedent` | 仅存在指代时标注 |

### 4.3 必须覆盖的样本

| 类别 | 示例 |
|---|---|
| 单一稳定事实 | “我现在住在杭州。” |
| 单一偏好 | “我不喜欢工作日早上喝咖啡。” |
| 单一任务 todo | “这周要完成数据库迁移。” |
| 单一任务 done | “数据库迁移做完了。” |
| 无前置任务的完成事件 | “我完成了本科毕业论文答辩。” → episodic |
| 多事实拆分 | “我喜欢乌龙茶，也不喜欢早起。” |
| 任务 + 偏好混合 | “报告做完了，我还是喜欢简洁的汇报。” |
| 明确事实 + 猜测 | “报告完成了，可能下周再补压测。” |
| 纯猜测 | “我可能下周会去上海。” → 不存 |
| 指代完成 | “这个也做完了。” + 合法前文 |
| 指代后出现新事实 | “论文答辩结束了，接下来准备找工作。” |
| 同家族不同实例 | “第二轮压测报告也做完了。” |
| 敏感信息 | 密码、Token、身份证、银行卡等 → 不存 |
| 噪声与混合表达 | 口语、错别字、英文混杂、否定、转折 |

### 4.4 L1 构造禁忌

- 不要只生成短、标准、语法完整的句子。
- 不要所有任务只使用“我要/完成了”模板。
- 不要用业务关键词硬编码 Gold。
- 不要因“可能、也许”出现一次就把整条多事实消息标为不存。
- 不要把 blocker 设计成独立任务状态。

## 5. Layer 2 独立数据集：关系判断与记忆演化

L2 的输入必须是已正确抽取、已规范化的 Candidate，避免 L1 抽取误差污染 Relation Guard / Adjudicator 评测。

### 5.1 输入结构

```json
{
  "case_id": "l2_xxx",
  "candidate": {
    "memory_type": "task",
    "content": "Layer3 评测报告已完成",
    "task_status": "done",
    "canonical_topic": "Layer3 评测报告",
    "task_instance_id": "report_round_1"
  },
  "pre_state": {
    "memories": [],
    "versions": [],
    "sources": []
  },
  "expected": {
    "relation": "update_task",
    "action": "update_task",
    "target_memory_ref": "m1",
    "post_state": {}
  }
}
```

### 5.2 必须验证的结果

| 对象 | 必须验证的内容 |
|---|---|
| Relation | `new/same/merge/update_task/conflict/ambiguous` |
| Action | `insert/add_source/update_task/pending_review` |
| Target | 是否选择正确旧 Memory |
| Current State | 最终 active Memory 的字段 |
| Version Sequence | 新版本号、顺序、有效时间 |
| Source Link | 新消息应关联到的 Memory/Version |
| Pending Review | 不确定时不得影响 active state |
| Idempotence | 相同 Candidate 重放不能重复写入 |
| Isolation | 不同 space 不得互相读取或写入 |

### 5.3 场景配比建议

| 场景 | 建议占比 | 关键断言 |
|---|---:|---|
| 全新记忆 `new→insert` | 20% | 没有旧身份时正确新增 |
| 同事实重复 `same→add_source` | 15% | 不额外创建 Memory / Version |
| 同任务 todo→done | 15% | 创建新版本、更新当前状态 |
| 同任务补充进度 | 10% | 更新过程信息，不改变实例身份 |
| 同家族不同实例 | 10% | 禁止错误合并 |
| 偏好重复与反转 | 10% | add source / pending / supersede 边界 |
| 冲突或多目标歧义 | 10% | 必须 pending_review |
| 无前置完成→episodic | 5% | 不凭空创建 active done Task |
| 重复投递、过期快照、并发 | 5% | 验证锁、幂等和版本检查 |

### 5.4 L2 特别注意

1. Relation 与 Action 不能混用：Relation 描述新旧事实关系，Action 描述系统实际处理方式。
2. `task_family_key` 命中不能作为 update 的 Gold 依据；只有同一 task instance 才可更新。
3. Gold 必须包含完整 `post_state`，不能只标 action。
4. 并发 case 要定义允许结果集合，不能将具有时序竞争的结果强行标为唯一答案。

## 6. Layer 3 独立数据集：已有记忆后的 /ask

L3 保持“直接 seed 隔离数据库 Memory 快照，再调用真实 `/ask`”的形式。它测试查询与回答，不测试消息写入。

### 6.1 输入结构

```json
{
  "case_id": "l3_xxx",
  "input": {
    "memory_snapshot": {
      "memories": [],
      "versions": [],
      "sources": [],
      "pending_reviews": []
    },
    "query": "Layer3 评测报告经历了哪些变化？",
    "query_time": "2026-08-10T10:00:00Z",
    "access_context": {
      "requester": "owner",
      "allow_sensitive": true
    }
  },
  "expected": {}
}
```

### 6.2 Gold 应使用答案契约，而非唯一标准文本

| 字段 | 用途 |
|---|---|
| `answer_type` | answered / no_answer / conflict / clarification / restricted |
| `evidence_mode` | current / history / mixed / none |
| `relevant_current_refs` | 当前状态题需要的 active Memory |
| `relevant_history_refs` | 时间线题需要的 Version |
| `must_not_return_refs` | 过期、冲突、错误实例、无权限证据 |
| `required_citation_refs` | 必须引用的 source |
| `expected_claims` | 回答必须表达的事实命题 |
| `expected_claim_groups` | 时间线、对比、多记忆汇总等结构 |
| `forbidden_claims` | 不得输出的结论 |
| `access_context` | owner/non-owner 与敏感访问权限 |

### 6.3 L3 必须拆分的评测子集

| 子集 | 主指标 | 不应混入的题 |
|---|---|---|
| 当前事实/状态 | Hit@K、Recall@K、Current State Accuracy | no-answer、权限、历史版本 |
| 历史时间线 | Timeline Complete@K、Version/Source Exact、Sequence Exact | 当前状态题 |
| 安全与回答 | Claim F1、Citation F1、No-answer F1、Access Violation、Stale Answer Use | 单纯召回指标 |

### 6.4 必须覆盖的查询

| 类型 | 示例 |
|---|---|
| 当前偏好 | “我现在喜欢咖啡吗？” |
| 当前任务 | “我的 Layer3 报告现在什么状态？” |
| 多记忆汇总 | “我最近学习和简历进展分别如何？” |
| 关系型问题 | “RAG 学习和 Agent 简历有什么关系？” |
| 历史时间线 | “这个任务从开始到完成经历了什么？” |
| 历史事实 | “我以前住在哪里？” |
| 无证据拒答 | “我养的猫叫什么？” |
| 冲突 | “我到底喜不喜欢咖啡？” |
| 歧义指代 | “那个任务现在怎样？” |
| 过期防护 | 旧 todo 与当前 done 同时存在时查询当前状态 |
| 权限隔离 | 非 owner 查询敏感 Memory |
| 跨空间隔离 | A space 不得返回 B space 的 Memory |

### 6.5 历史任务的二元状态规范

```text
版本事实：
v1: todo
v2: todo + blocker="API 限流"
v3: done

用户可见生命周期：
todo → done
```

不要要求回答 `todo → blocked → done`。如果问题询问“为什么卡住”，则应额外检查 blocker 证据是否正确引用 v2。

## 7. L1→L2 桥接数据集：真实消息到记忆演化

每个 case 是一段消息序列，每一轮都同时有 L1 与 L2 Gold：

```json
{
  "case_id": "bridge_xxx",
  "turns": [
    {
      "note_id": "n1",
      "text": "这周要完成 Layer3 评测报告。",
      "expected_l1": {},
      "expected_l2": {}
    },
    {
      "note_id": "n2",
      "text": "报告卡在 API 限流，暂时没完成。",
      "expected_l1": {},
      "expected_l2": {}
    },
    {
      "note_id": "n3",
      "text": "Layer3 评测报告终于完成了。",
      "expected_l1": {},
      "expected_l2": {}
    }
  ],
  "expected_final_state": {}
}
```

该例最终应当只有一个 active Task，状态为 done；版本链和 n1/n2/n3 来源完整；blocker 为过程信息，不是第三状态。

桥接集必须包含：

- 新建→补充进度→完成；
- 同一消息重复投递；
- 同一 Note 多 Candidate；
- 同家族不同实例；
- 指代续接；
- 指代后出现明确的新任务；
- 偏好加强、反转、范围变化；
- 无前置 task 的完成事件转 episodic；
- 多候选目标导致 pending review；
- 并发或乱序消息的允许结果；
- 敏感信息不入记忆；
- previously empty Note 的强制重放回归。

每一轮需保存：原消息、L1 Gold Candidate、实际 Candidate、L2 Gold Decision、实际 Decision、pre/post Snapshot 与 Trace ID。这样失败可归因到抽取、身份判断、关系决策、持久化或来源/版本链路。

## 8. L1→L2→L3 全链路数据集：消息到最终回答

全链路 case 在桥接 case 上增加 checkpoint `/ask`：

```text
消息 n1：创建任务
消息 n2：补充 blocker
Ask q1：当前状态如何？

消息 n3：任务完成
Ask q2：当前状态如何？
Ask q3：经历了哪些变化？
```

每个 checkpoint 必须评分：

| 层 | 评分内容 |
|---|---|
| L1 | 每轮 Candidate、span、type、字段 |
| L2 | 每轮 relation/action、版本、source、pending |
| L3 Retrieval | `/ask` 是否找到正确 current/history evidence |
| L3 Answer | 回答、引用、拒答、权限与 stale 防护 |

关键 scenario：

| Scenario | 核心断言 |
|---|---|
| todo→done 后查询当前状态 | 不得回答旧 todo |
| todo→blocker→done 后查询历史 | 找全版本；用户状态摘要为 todo→done；需要时解释 blocker |
| 无前置完成 | 形成 episodic，不出现在当前任务 |
| 两个同家族任务 | 不得把第一轮状态套到第二轮 |
| pending review | 默认不进入画像与 ask 有效证据 |
| reject pending | archived/discarded 不得在 ask 中召回 |
| sensitive note | 非 owner 必须拒绝或 restricted |
| duplicate delivery | Memory、Version、Source 不重复 |
| 并发更新 | 不得静默覆盖；不确定时进入 pending |
| 指代完成 | 必须关联正确前序任务 |

## 9. 给数据生成 LLM 的核心指令

```text
你负责生成随心记评测数据的自然语言消息或查询，不负责决定最终 Gold。

先根据输入 world_spec 理解实体、任务实例、当前状态、历史版本、来源和访问权限。
随后生成自然、口语化、多样化的中文消息或查询。

严格要求：
1. 不改变 world_spec 中的事实、时序、实例身份和权限边界。
2. task 状态只表达 todo 或 done；阻塞、等待、进度必须表达为过程信息，不得构造第三状态。
3. 若消息包含多个独立事实，每个事实必须在原文中有可定位的连续证据片段。
4. 指代仅在给定前文可唯一消解时使用；若设计为歧义，应保留至少两个合理候选。
5. 同一 task_family 不代表同一 task_instance；不同轮次、不同对象、不同范围必须使用不同实例。
6. 不使用真实用户隐私、凭据、真实飞书群信息或真实 API Key。
7. 不将 Gold 标签、memory_key、relation、action 或内部字段写进用户消息。
8. 不反复使用“我要/完成了/我喜欢”模板；覆盖口语、省略、错别字、否定、转折、英文混杂和多句表达。
9. 对纯猜测、假设、预测，不得写成既成事实。
10. 生成结果必须能被 world_spec 确定性验证；若自然语言引入额外事实或歧义，必须重新生成。
```

## 10. 推荐规模与交付要求

建议先建立小而严的验证集，再扩展规模：

| 数据集 | 建议首版规模 |
|---|---:|
| L1 | 1,000 messages |
| L2 | 800–1,000 cases |
| L3 | 800 queries |
| L1→L2 | 300 条多轮 scenario |
| L1→L2→L3 | 200 条多轮 scenario，每条 2–4 个 ask checkpoint |

每次交付必须包含：

- `schema.json`：数据字段和约束；
- `manifest.json`：生成模型、随机种子、版本、文件 hash、规模与分布；
- `validation_report.json`：结构校验和一致性校验结果；
- `coverage_matrix.md`：能力点、场景、数量、数据集之间的映射；
- 冻结 Gold 与失败样本导出规则；
- 训练/开发/测试 world-level split 清单。

每轮扩容前，都应对分层随机样本进行人工审计，重点检查：任务实例身份、指代、时间线、pending review、敏感权限。
