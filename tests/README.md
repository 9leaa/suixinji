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
- 离线评测评分函数。

## Dry-run 评测

```bash
python eval/eval_classification.py --dry-run
python eval/eval_retrieval.py --dry-run
python eval/eval_summary.py --dry-run
python eval/eval_query_react.py --dry-run
```

去掉 `--dry-run` 后才会调用真实 LLM / embedding API。

## 当前没有覆盖

- 飞书 SDK 真实收发消息集成测试。
- worker 真实文件链路集成测试。
- 总结内容质量人工/半自动评测。

## 当前结果

最近一次真实 LLM / embedding 离线评测结果见 `docs/metrics/latest.json` 和 `eval/README.md`。
