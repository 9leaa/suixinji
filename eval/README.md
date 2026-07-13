# 离线评测

离线评测用于评估真实 LLM / embedding 效果，并为 CI 提供不调用外部 API 的 dry-run。

## 数据文件

```text
eval/data/classification_cases.jsonl
eval/data/retrieval_cases.jsonl
eval/data/summary_cases.jsonl
eval/data/query_cases.jsonl
```

## Dry-run

```bash
python eval/eval_classification.py --dry-run
python eval/eval_retrieval.py --dry-run
python eval/eval_summary.py --dry-run
python eval/eval_query_react.py --dry-run
```

Dry-run 只校验样例格式和脚本流程，不调用真实 LLM 或 embedding API。

## 真实评测

```bash
python eval/eval_classification.py
python eval/eval_retrieval.py
python eval/eval_summary.py
python eval/eval_query_react.py
```

结果写入：

```text
eval/results/classification_results.json
eval/results/retrieval_results.json
eval/results/summary_results.json
eval/results/query_react_results.json
```

## 最近指标

当前展示指标同步在 `docs/metrics/latest.json`：

| 模块 | 样例数 | 通过 | 失败 | 通过率 |
| --- | ---: | ---: | ---: | ---: |
| 分类 | 36 | 32 | 4 | 88.89% |
| 检索 | 160 | 152 | 8 | 95.00% |
| 总结 | 12 | 12 | 0 | 100.00% |
| ReAct 查询 | 40 | 36 | 4 | 90.00% |

已知失败样例包括部分边界 tag 命中不足、top3 检索排名偏后，以及少量语义查询未召回期望笔记。

## 如何扩展

真实使用中发现失败样例后，追加到对应 jsonl：

- 误分类：`classification_cases.jsonl`
- 搜不到：`retrieval_cases.jsonl`
- 总结遗漏或幻觉：`summary_cases.jsonl`
- ReAct 工具选择问题：`query_cases.jsonl`
