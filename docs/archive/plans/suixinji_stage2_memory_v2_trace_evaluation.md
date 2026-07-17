# 随心记 Agent 第二阶段高级记忆系统设计方案

## 1. 阶段目标

将当前的“原始笔记 + Embedding 检索”升级为：

> 具备记忆提取、分层存储、去重合并、冲突更新、版本追踪、来源引用和生命周期管理的长期记忆系统。

核心链路：

```text
原始消息
→ 保存原始笔记
→ 提取候选记忆
→ 查找相似旧记忆
→ 判断新增、合并、更新、冲突或丢弃
→ 保存版本化记忆
→ 查询时混合召回
→ 返回带来源的回答
```

---

# 一、Memory V2

## 1.1 记忆分层

短期实现四类：

### Episodic Memory：情景记忆

记录具体发生的事件。

```text
2026-07-13 用户完成了随心记项目的 CI 改造。
```

特点：

- 带时间。
- 可能只在一段时间内有价值。
- 主要回答“发生过什么”。

### Semantic Memory：语义记忆

从多次记录中抽取的稳定事实。

```text
用户正在学习 Agent 工程开发。
```

特点：

- 相对稳定。
- 可以由多条情景记忆合并得到。
- 主要回答“用户长期在做什么”。

### Preference Memory：偏好记忆

记录用户的喜好、习惯和约束。

```text
用户更喜欢简洁、直接的项目评价。
```

特点：

- 会发生变化。
- 必须支持 supersede 和版本记录。
- 主要用于个性化回答。

### Task Memory：任务记忆

记录需要执行或跟踪的事项。

```text
完善随心记项目 README。
状态：进行中。
```

状态：

```text
todo
in_progress
blocked
done
cancelled
```

---

## 1.2 记忆数据模型

使用 SQLite：

```text
data/memory/memory.db
```

### memories 表

```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    normalized_content TEXT,
    importance REAL NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT,
    access_count INTEGER DEFAULT 0,
    current_version INTEGER DEFAULT 1
);
```

### memory_sources 表

```sql
CREATE TABLE memory_sources (
    memory_id TEXT NOT NULL,
    note_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

关系：

```text
created_from
supported_by
updated_by
contradicted_by
```

### memory_versions 表

```sql
CREATE TABLE memory_versions (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    source_note_id TEXT,
    created_at TEXT NOT NULL
);
```

### memory_vectors 表

初期可以继续使用现有 JSON 向量索引，后续迁移到 SQLite Vector、FAISS 或 Qdrant。

---

## 1.3 模块结构

```text
memory/
├── models.py
├── repository.py
├── extractor.py
├── candidate_retriever.py
├── relation_classifier.py
├── consolidator.py
├── lifecycle.py
├── retriever.py
├── service.py
└── prompts.py
```

---

## 1.4 Memory Extractor

输入：

```json
{
  "note_id": "note_xxx",
  "text": "我现在不想继续学习 Java，短期重点放在 Python Agent。"
}
```

输出：

```json
{
  "candidates": [
    {
      "memory_type": "preference",
      "content": "用户短期不打算重点学习 Java",
      "importance": 0.7,
      "confidence": 0.9,
      "entities": ["Java"],
      "should_store": true
    },
    {
      "memory_type": "semantic",
      "content": "用户短期重点学习 Python Agent",
      "importance": 0.9,
      "confidence": 0.95,
      "entities": ["Python", "Agent"],
      "should_store": true
    }
  ]
}
```

过滤规则：

不进入长期记忆：

- 普通寒暄。
- 没有上下文意义的单句。
- 低置信度猜测。
- 短期无意义状态。
- 敏感内容且用户没有明确要求保存。

---

## 1.5 记忆关系判断

每个候选记忆先检索同类型相似记忆，再判断关系：

```text
new
same
extend
update
contradict
unrelated
```

输出：

```json
{
  "relation": "update",
  "target_memory_id": "mem_123",
  "action": "supersede",
  "reason": "新记录明确改变了此前学习重点"
}
```

### 操作规则

| Relation | Action |
|---|---|
| new | insert |
| same | add_source |
| extend | merge |
| update | supersede |
| contradict | conflict 或 supersede |
| unrelated | insert |

---

## 1.6 冲突处理

示例：

```text
旧记忆：用户喜欢喝咖啡
新记录：最近胃不舒服，暂时不喝咖啡
```

处理后：

```text
旧记忆：
status = superseded
valid_until = 新记录时间

新记忆：
content = 用户目前暂时不喝咖啡
status = active
valid_from = 新记录时间
```

对不明确的冲突：

```text
旧记忆：用户喜欢远程工作
新记录：最近还是去办公室效率更高
```

不能直接删除旧偏好，应保存：

```text
status = conflicted
confidence = 0.6
```

必要时通过飞书询问用户确认。

---

## 1.7 Memory Consolidation

定时任务：

```text
每天：处理尚未提取记忆的笔记
每周：合并重复情景记忆
每月：生成稳定语义记忆
```

示例：

```text
多条 episodic：
- 阅读 RAG 论文
- 实现向量检索
- 调整 ReAct 查询
- 做 Agent 项目

合并为 semantic：
用户当前持续学习和开发 Agent/RAG 系统。
```

注意：

- 新语义记忆必须保留来源。
- 不能删除原始笔记。
- Consolidation 结果必须可撤销。

---

## 1.8 记忆检索

查询路由：

```text
具体事件 → episodic
用户事实 → semantic
偏好习惯 → preference
待办进度 → task
模糊问题 → 全类型混合检索
```

综合评分：

```text
final_score =
0.45 × semantic_similarity
+ 0.20 × importance
+ 0.15 × recency
+ 0.10 × confidence
+ 0.05 × entity_overlap
+ 0.05 × access_frequency
```

过滤：

- 默认只查询 `active`。
- `superseded` 只用于历史解释。
- `conflicted` 必须降低权重。
- `expired` 不用于当前事实回答。

---

## 1.9 用户控制命令

增加：

```text
/memory list
/memory show <id>
/memory search <内容>
/memory forget <id>
/memory correct <id> <新内容>
/memory conflicts
/memory stats
```

用户删除记忆时：

- 默认软删除。
- 保留审计记录。
- 查询时不再返回。
- 提供彻底删除选项。

---

# 二、Memory Trace

## 2.1 目标

解决两个问题：

1. 为什么系统记住了这件事？
2. 为什么系统用这条记忆回答？

## 2.2 Trace 数据结构

```json
{
  "trace_id": "trace_xxx",
  "trace_type": "memory_write",
  "space_id": "p_xxx",
  "note_id": "note_xxx",
  "started_at": "...",
  "finished_at": "...",
  "steps": []
}
```

### 写入 Trace

```text
note_saved
memory_extraction_started
candidate_extracted
similar_memories_retrieved
relation_classified
memory_inserted / merged / superseded / discarded
vector_written
trace_finished
```

每步记录：

```json
{
  "step": "relation_classified",
  "status": "success",
  "duration_ms": 341,
  "input_summary": {
    "candidate_memory_id": "candidate_1",
    "retrieved_count": 3
  },
  "output_summary": {
    "relation": "update",
    "target_memory_id": "mem_123"
  },
  "reason": "新记录明确改变了旧偏好",
  "error": null
}
```

### 查询 Trace

```text
query_received
query_routed
memory_search
note_search
rerank
evidence_selected
answer_generated
answer_returned
```

## 2.3 隐私原则

Trace 不默认记录：

- 完整原始消息。
- 完整 Prompt。
- API Key。
- 完整用户画像。

默认只记录：

- 长度。
- ID。
- 类型。
- 分数。
- 操作理由摘要。
- 错误信息。

开发模式可通过配置开启详细 Trace。

## 2.4 Trace 查看方式

```text
/trace latest
/trace <trace_id>
/trace memory <memory_id>
```

也可以提供 CLI：

```bash
python scripts/show_trace.py --trace-id xxx
```

## 2.5 可视化

后续可生成：

```text
docs/traces/example-memory-write.md
docs/traces/example-memory-query.md
```

用于 README 演示。

---

# 三、Memory Evaluation

## 3.1 评测目标

不能只测试“向量是否召回”，必须测试完整记忆生命周期。

## 3.2 数据集结构

```text
eval/memory/
├── extraction_cases.jsonl
├── filtering_cases.jsonl
├── relation_cases.jsonl
├── conflict_cases.jsonl
├── lifecycle_cases.jsonl
├── retrieval_cases.jsonl
└── end_to_end_cases.jsonl
```

---

## 3.3 评测模块

### A. 记忆提取评测

判断：

- 是否应该记忆。
- 记忆类型是否正确。
- 内容是否忠实。
- importance/confidence 是否合理。

指标：

```text
should_store precision
should_store recall
memory_type accuracy
content faithfulness
```

### B. 无价值记忆过滤

测试：

```text
你好
今天天气不错
哈哈
收到
```

不能全部保存为长期记忆。

指标：

```text
false memory rate
```

### C. 去重与合并评测

输入：

```text
我正在学习 Agent
最近主要学习 Agent
现在重点还是 Agent 系统
```

期望：

```text
1 条 active semantic memory
3 条 source
```

指标：

```text
duplicate merge accuracy
source preservation rate
```

### D. 冲突更新评测

输入：

```text
先说：喜欢咖啡
后说：暂时不喝咖啡
```

期望：

- 旧记忆 superseded。
- 新记忆 active。
- 当前查询返回新记忆。
- 查询历史变化时可以返回两条。

指标：

```text
conflict detection accuracy
supersede accuracy
stale memory usage rate
```

### E. 生命周期评测

测试：

- 任务创建。
- 任务状态更新。
- 任务完成。
- 偏好变化。
- 记忆过期。
- 用户删除和修正。

指标：

```text
state transition accuracy
expired memory leakage
deleted memory leakage
```

### F. 检索评测

分别评估：

```text
episodic recall@k
semantic recall@k
preference recall@k
task recall@k
mixed retrieval recall@k
```

### G. 端到端评测

完整场景：

```text
连续输入 5～10 条消息
→ 自动提取记忆
→ 发生重复或冲突
→ 提出问题
→ 判断最终回答是否正确
```

评分：

```text
事实正确性
是否使用最新有效记忆
是否引用正确来源
是否错误使用过期记忆
是否出现无依据内容
```

---

## 3.4 关键测试样例

必须包含：

```text
喜欢苹果 → 后来说苹果过敏
住在北京 → 后来说搬到上海
想学习 Java → 后来说短期只学 Python
创建任务 → 更新进度 → 完成任务
重复表达同一偏好
一句模糊玩笑是否被误记
低置信度内容是否进入长期记忆
用户主动删除记忆后是否仍被召回
```

---

## 3.5 评测输出

```text
eval/results/memory_extraction.json
eval/results/memory_relation.json
eval/results/memory_conflict.json
eval/results/memory_retrieval.json
eval/results/memory_e2e.json
```

汇总：

```json
{
  "extraction_f1": 0.0,
  "memory_type_accuracy": 0.0,
  "false_memory_rate": 0.0,
  "merge_accuracy": 0.0,
  "conflict_accuracy": 0.0,
  "stale_memory_usage_rate": 0.0,
  "retrieval_recall_at_5": 0.0,
  "source_attribution_rate": 0.0
}
```

---

# 四、阶段实施顺序

```text
M1：SQLite Schema + Repository
M2：Memory Extractor
M3：相似记忆检索
M4：关系判断与合并
M5：冲突与版本管理
M6：Memory Retriever
M7：用户记忆控制命令
M8：Memory Trace
M9：Memory Evaluation
M10：README 演示与指标
```

---

# 五、本阶段完成标准

完成后必须满足：

- 原始笔记与长期记忆分离。
- 支持四类记忆。
- 重复信息不会无限生成新记忆。
- 新旧偏好冲突可以正确更新。
- 每条记忆可以追溯到原始笔记。
- 每次记忆变更都有版本记录。
- 查询默认使用最新 active 记忆。
- 用户可以查看、修正和删除记忆。
- 可以通过 Trace 解释记忆写入和调用过程。
- 有独立 Memory Evaluation 数据集和结果报告。
