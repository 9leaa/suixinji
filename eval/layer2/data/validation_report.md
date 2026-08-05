# 第二阶段数据集校验报告

- 总 Cases：564
- 全部通过：`True`
- 核心约束：`No-history Task(done) must never insert a new Task(done).`

## relation_and_action_core.jsonl

- Cases：120
- Decisions：120
- Relations：`{'new': 20, 'same': 20, 'merge': 20, 'update': 20, 'supersede': 20, 'conflict': 20}`
- Actions：`{'insert': 40, 'add_source': 20, 'update': 40, 'pending_review': 20}`
- Memory Types：`{'task': 120}`
- 结果：`PASS`

## task_state_transition.jsonl

- Cases：144
- Decisions：144
- Relations：`{'new': 24, 'same': 24, 'update': 84, 'conflict': 12}`
- Actions：`{'insert': 24, 'add_source': 24, 'update': 84, 'pending_review': 12}`
- Memory Types：`{'task': 144}`
- 结果：`PASS`

## orphan_done_resolution.jsonl

- Cases：100
- Decisions：100
- Relations：`{'new': 25, 'conflict': 35, 'update': 30, 'same': 10}`
- Actions：`{'insert': 25, 'pending_review': 35, 'update': 30, 'add_source': 10}`
- Memory Types：`{'task': 100}`
- 结果：`PASS`

## version_source_idempotency.jsonl

- Cases：80
- Decisions：110
- Relations：`{'same': 40, 'update': 40, 'conflict': 20, 'new': 10}`
- Actions：`{'add_source': 40, 'update': 40, 'pending_review': 20, 'insert': 10}`
- Memory Types：`{'task': 110}`
- 结果：`PASS`

## non_task_consolidation.jsonl

- Cases：120
- Decisions：120
- Relations：`{'new': 21, 'same': 21, 'merge': 21, 'update': 21, 'supersede': 18, 'conflict': 18}`
- Actions：`{'insert': 21, 'add_source': 21, 'update': 60, 'pending_review': 18}`
- Memory Types：`{'preference': 40, 'semantic': 40, 'episodic': 40}`
- 结果：`PASS`
