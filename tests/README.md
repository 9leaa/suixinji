# 测试总览

当前测试分三类：

1. `1阶段测试/`：确定性单元测试。
2. `2阶段测试/`：mock 流程测试。
3. `3阶段测试/`：离线评测指标测试。

统一使用 `zcj_hello` 环境运行：

```bash
/usr/local/anaconda3/envs/zcj_hello/bin/python -m pytest tests
```

## 1阶段测试：确定性单元测试

目标：只测不依赖 LLM、embedding、飞书网络的纯逻辑。

当前覆盖：

- taxonomy 固定 type/tags 规则。
- `/type`、`/tag`、`/filter` 底层筛选。
- `/summary` 时间范围计算。
- `/summary_auto` 订阅读写。
- 自动总结 scheduler 到点触发规则。
- feedback 反馈记录的 JSONL 落盘。

详细说明见：`tests/1阶段测试/README.md`。

## 2阶段测试：mock 流程测试

目标：测试带 LLM/embedding/外部发送接口的主流程，但把这些外部依赖都 mock 掉。

当前覆盖：

- worker 从 WAL record 到 `NoteMetadata`、`VectorItem`、`mark_processed` 的流程。
- 已存在笔记但缺向量时的 vector backfill 流程。
- `process_pending()` 对同一批 pending 中重复 message_id 的跳过逻辑。
- `/ask` ReAct 的工具选择、observation 传递、默认 semantic_search fallback。
- P4 summary 先生成草稿、再 Reflection 修订、最后落盘的流程。
- summary 生成失败时 fallback 总结是否生效。

详细说明见：`tests/2阶段测试/README.md`。

## 3阶段测试：离线评测指标测试

目标：建立真实 LLM / embedding 离线评测框架，并测试评分函数本身是否可靠。

当前覆盖：

- 分类评测指标：type 是否命中 `acceptable_types`、tags 是否至少命中 2 个合理标签、必选 tags 是否完整。
- 检索评测指标：`hit@k`、`recall@k`、多答案召回、无结果样例的 `min_score`。
- ReAct 查询评测指标：工具选择是否符合预期、查询结果是否覆盖期望笔记。
- 总结评测指标：必须包含的要点是否覆盖、禁止出现的内容是否未出现。
- JSONL 样例读取和总分统计。

真正调用模型的脚本在 `eval/` 目录：

```bash
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_classification.py --dry-run
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_retrieval.py --dry-run
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_summary.py --dry-run
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_query_react.py --dry-run
```

去掉 `--dry-run` 后才会调用真实 LLM / embedding API。

详细说明见：`tests/3阶段测试/README.md` 和 `eval/README.md`。

## 当前没有覆盖

后续还需要补：

- 飞书 SDK 真实收发消息集成测试。
- worker 真实文件链路集成测试。
- 总结内容质量人工/半自动评测。

## 当前结果

最近一次 pytest 运行结果：

```text
53 passed
```

最近一次真实 LLM / embedding 离线评测结果，运行时间：2026-06-07，环境：`zcj_hello`。

| 模块 | 样例数 | 通过 | 失败 | 通过率 |
| --- | ---: | ---: | ---: | ---: |
| 分类 | 36 | 32 | 4 | 88.89% |
| 检索 | 160 | 152 | 8 | 95.00% |
| 总结 | 12 | 12 | 0 | 100.00% |
| ReAct 查询 | 40 | 36 | 4 | 90.00% |

详细失败项和结果文件见：`eval/README.md` 与 `eval/results/`。
