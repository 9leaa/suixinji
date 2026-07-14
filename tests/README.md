# 测试总览

当前测试分三类：

1. `1阶段测试/`：确定性单元测试。
2. `2阶段测试/`：mock 流程测试。
3. `3阶段测试/`：离线评测指标测试。

统一运行命令：

```bash
python -m pytest tests
```

CI 会额外执行 coverage、Ruff 和 dry-run 评测。

## 当前覆盖

- taxonomy 固定 type/tags 规则。
- `/type`、`/tag`、`/filter` 底层筛选。
- `/summary` 时间范围计算和订阅读写。
- 自动总结 scheduler 到点触发规则。
- feedback 反馈记录的 JSONL 落盘。
- worker 从 WAL record 到 note/vector/mark_processed 的 mock 流程。
- 已存在笔记缺向量时的 vector backfill。
- `process_pending()` 对重复 message_id 的跳过逻辑。
- `/ask` ReAct 工具选择、observation 传递和 semantic_search fallback。
- 有界任务执行器的队列拒绝、ingest 执行和状态统计。
- PendingDrainer 的 pending 重提交流程、inflight 跳过、batch size 和队列满停止。
- DeliveryStore 的租约、reserve/sent/failed/unknown 幂等规则和最大尝试次数。
- 自动总结 delivery/subscription 对账，覆盖 sent 修复、unknown 跳过和 failed 重提交流程。
- Summary scheduler 异常韧性，覆盖对账失败跳过当前订阅、下一轮恢复、一个订阅失败不影响其他订阅、tick 级异常保护。
- Memory V2 extraction state，覆盖 completed、empty、partial、failed、attempt_count、retryable 状态和 stale processing 恢复。
- Memory V2 SQLite WAL/locked 重试和并发写入，覆盖 sources、versions 不丢失。
- Memory V2 consolidation run 幂等，覆盖 daily/weekly/monthly period_key、completed 跳过、failed 重试、running 租约。
- Memory V2 查询阈值，覆盖低相关结果过滤和自定义 `min_score`。
- 任务级重试边界，确认 runner 失败不会整体重跑。
- 同一 `space_id` ingest 串行、不同 `space_id` 可并行、压力提交受 worker/queue 限制。
- TaskRegistry 的历史裁剪和累计计数保留。
- 离线评测评分函数。

## Dry-run 评测

```bash
python eval/eval_classification.py --dry-run
python eval/eval_retrieval.py --dry-run
python eval/eval_summary.py --dry-run
python eval/eval_query_react.py --dry-run
python eval/eval_memory.py --dry-run
```

去掉 `--dry-run` 后才会调用真实 LLM / embedding API。

## 当前没有覆盖

- 飞书 SDK 真实收发消息集成测试。
- worker 真实文件链路集成测试。
- 总结内容质量人工/半自动评测。

## 当前结果

最近一次结果以 CI 为准；本地可用 `python -m pytest tests` 复现。

最近一次真实 LLM / embedding 离线评测结果见 `docs/metrics/latest.json` 和 `eval/README.md`。
