# 随心记第二阶段评测数据集 v1

## 范围

只评测：`validated MemoryCandidate -> consolidate_candidate -> Relation/Action -> Memory/Version/Source`。第一阶段抽取器不在本数据集中评测。

## 文件

| 文件 | Cases | 核心能力 |
|---|---:|---|
| relation_and_action_core.jsonl | 120 | Task Identity、六类 Relation、四类 Action |
| task_state_transition.jsonl | 144 | Task 状态演化与 Current State |
| orphan_done_resolution.jsonl | 100 | 无历史 Done、转 Episodic、Pending Review |
| version_source_idempotency.jsonl | 80 | Version、Source、乱序、重复消费、并发 |
| non_task_consolidation.jsonl | 120 | Preference、Semantic、Episodic 演化 |

总计 **564 Cases**。

## Relation
`new / same / merge / update / supersede / conflict`

## Action
`insert / add_source / update / pending_review`

## 主要指标
- Task Identity Precision / Recall / F1
- Relation Macro-F1
- Action Accuracy
- Current State Accuracy
- Task Transition Accuracy
- Duplicate Active Rate
- Stale Active Rate
- Version Sequence Accuracy
- Source Link Accuracy
- Pending-review Precision
- Orphan Done Task Rate（目标必须为 0）

## 孤立 Done 规则
- 匹配 todo/blocked：更新为 done
- 匹配 done：same/add_source
- 匹配 cancelled：pending_review
- 无历史普通完成事件：转 Episodic
- 无历史强任务完成：pending_review
- 禁止无历史直接插入 Task(done)

## 适配说明
`memory_ref`（如 `m1`、`new:<candidate_id>`）是逻辑引用，不要求等于数据库 UUID。评测适配器应把实际持久化结果映射为逻辑引用。数据集中的 `memory_key` 表示稳定逻辑身份；若生产编码格式不同，可做等价映射，但不能改变身份语义。
