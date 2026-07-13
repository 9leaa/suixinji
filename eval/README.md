# 3阶段测试：离线评测

第三阶段不是普通单元测试，而是用于评估真实 LLM / embedding 效果的离线评测。

## 目标

回答这些问题：

- 分类模型是否把笔记分到正确 type？
- tags 是否至少命中 2 个合理标签？
- embedding 检索能否把相关笔记排到 top-k？
- summary 是否覆盖必须提到的要点，并避免明显幻觉？

## 数据文件

```text
eval/data/classification_cases.jsonl  分类评测样例
eval/data/retrieval_cases.jsonl       embedding 检索评测样例
eval/data/summary_cases.jsonl         总结覆盖率评测样例
eval/data/query_cases.jsonl           完整 ReAct 查询评测样例
```

每一行是一个 JSON object，方便后续持续追加真实失败样例。

## 当前样例规模

- 分类评测：36 条
- 检索评测：160 条
- ReAct 查询评测：40 条
- 总结评测：12 条

## 运行方式

所有命令都使用 `zcj_hello` 环境。

只校验样例格式，不调用真实 API：

```bash
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_classification.py --dry-run
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_retrieval.py --dry-run
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_summary.py --dry-run
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_query_react.py --dry-run
```

调用真实 LLM / embedding：

```bash
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_classification.py
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_retrieval.py
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_summary.py
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_query_react.py
```

结果会写到：

```text
eval/results/classification_results.json
eval/results/retrieval_results.json
eval/results/summary_results.json
eval/results/query_react_results.json
```

## 最近一次真实评测结果

运行时间：2026-06-07，环境：`zcj_hello`。

```text
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_classification.py
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_retrieval.py
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_summary.py
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/eval_query_react.py
```

结果摘要：

| 模块 | 样例数 | 通过 | 失败 | 通过率 |
| --- | ---: | ---: | ---: | ---: |
| 分类 | 36 | 32 | 4 | 88.89% |
| 检索 | 160 | 152 | 8 | 95.00% |
| 总结 | 12 | 12 | 0 | 100.00% |
| ReAct 查询 | 40 | 36 | 4 | 90.00% |

失败项摘要：

- 分类失败：`cls_resource_tool`、`cls_resource_data`、`cls_life_expense`、`cls_life_social`。前三个 type 可接受，但 tags 只命中 1 个；最后一个被分成 `灵感`，期望为 `生活`。
- 检索失败：`ret_auto_summary_02`、`ret_auto_summary_06` 的正确笔记排第 4，当前单答案规则要求进入 top3。
- 检索多答案失败：`ret_multi_testing`、`ret_multi_production_safety`、`ret_multi_deploy_all`、`ret_multi_health_life`、`ret_multi_test_layers`、`ret_multi_ops_monitor`。这些样例大多命中 2/3，但当前 `min_recall=0.67`，`2/3=0.6667` 被判失败。
- ReAct 查询失败：`qry_semantic_observability`、`qry_semantic_deploy`、`qry_extra_observability_02`、`qry_extra_deploy_06`。工具选择正确，均调用了 `semantic_search`，但没有召回期望笔记。

结果文件：

```text
eval/results/classification_results.json
eval/results/retrieval_results.json
eval/results/summary_results.json
eval/results/query_react_results.json
```

## 当前评分规则

### 分类

通过条件：

- `pred_type` 命中 `acceptable_types`；如果没有配置 `acceptable_types`，再看 `expected_type`
- 如果配置了 `expected_tags_any`，预测 tags 默认至少命中 2 个，可用 `min_tag_hits` 调整
- 如果配置了 `expected_tags_all`，预测 tags 必须全部包含

### 检索

通过条件：

- 单答案默认看 `recall@3 == 1.0`
- 多答案默认看 `recall@5`，样例可用 `pass_k` 和 `min_recall` 调整
- 无相关样例用 `expected_no_result=true` 和 `min_score` 检查最高分是否低于阈值

### 总结

通过条件：

- `must_include` 中的词都出现在总结中
- `must_not_include` 中的词都不能出现在总结中

这个规则很朴素，但适合 MVP 阶段快速发现退化。

## 如何扩展

每次真实使用中发现失败样例，就追加到对应 jsonl：

- 误分类：加到 `classification_cases.jsonl`
- 搜不到：加到 `retrieval_cases.jsonl`
- 总结遗漏或幻觉：加到 `summary_cases.jsonl`

这样评测集会随着真实使用逐步变强。
