# 第一阶段：记忆正确性修复验收报告

## 结论

第一阶段通过验收。相同 360 条离线质量集上，关系审理准确率从
78.33% 提升到 100%，破坏性误判率从 32.50% 降到 0%，候选召回
Recall@20 从 83.33% 提升到 100%。本轮评测没有调用 LLM 或 Embedding，
因此指标不受外部 API 延迟、费用和随机性影响。

## 对比口径

- 基线提交：`deda2b3a51fda260c10d93a84ebfb3b48d64cece`
- 实现分支：`optimize/stage1-memory-correctness`
- 数据集：`eval/memory/quality_cases.jsonl`，360 条
- 数据集 SHA-256：`10cc2f950264b03b35f034775ebf4cc6696b7ca8d13ee40ff6e6ccddd670c240`
- 基线报告：`docs/memory_eval/baseline_v2.json`
- 阶段报告：`docs/memory_eval/stage1.json`
- 结构化对比：`docs/metrics/stage1_memory_correctness.json`

## 指标变化

| 指标 | 基线 | 第一阶段 | 变化 |
| --- | ---: | ---: | ---: |
| 抽取存储判断准确率 | 81.67% | 81.67% | 0.00 个百分点 |
| 抽取类型 Macro-F1 | 66.53% | 66.53% | 0.00 个百分点 |
| 关系审理准确率 | 78.33% | 100.00% | +21.67 个百分点，+27.66% |
| 关系审理 Macro-F1 | 76.10% | 100.00% | +23.90 个百分点，+31.41% |
| 破坏性误判率 | 32.50% | 0.00% | -32.50 个百分点，消除 100% |
| Candidate Recall@20 | 83.33% | 100.00% | +16.67 个百分点，+20.00% |
| Retrieval MRR | 77.50% | 94.17% | +16.67 个百分点，+21.51% |
| 端到端准确率 | 66.67% | 83.33% | +16.66 个百分点，+24.99% |

## 已完成内容

- 持久化 `memory_candidates`，补全 Candidate 生命周期、状态和重试幂等。
- 记录 extractor、policy、model、prompt、输入快照和审理版本信息。
- 引入 `memory_key`，实现候选混合召回并收紧 merge/supersede/conflict。
- 增加 `valid_until` 查询过滤、过期 worker 和领域无关的月度 consolidation。
- 增加 reject、edit/approve、conflict resolve 人工审核闭环和飞书命令。
- 增加 PostgreSQL 迁移 `20260718_0003`，完成降级再升级验证。

## 模型路由

运行时按任务难度分流，避免所有操作都使用最贵模型：

| 任务 | 模型角色 | 默认模型 |
| --- | --- | --- |
| 结构化候选抽取、低风险校验 | fast | `gpt-5.4-mini` |
| 复杂查询综合 | balanced | `gpt-5.4` |
| 高风险关系审理与冲突判断 | strong | `gpt-5.5` |

确定性规则仍是快速路径和降级路径。上述质量评测实际外部模型调用为 0，
所以本轮评测 API 花费为 0；生产调用费用需在真实流量阶段另行测量。

## 验证结果

- `python -m pytest tests/test_memory*.py -q`：97 passed。
- `python -m pytest tests/test_postgres_repositories.py -q`：7 passed。
- `ruff check .`：通过。
- `git diff --check`：通过。
- Alembic `0003 -> 0002 -> 0003`：通过。

## 剩余问题

抽取类型 Macro-F1 仍为 66.53%，端到端准确率仍为 83.33%。主要缺口是
episodic 事件抽取；它不影响本阶段的四项硬验收，但应在后续模型抽取质量
专项中继续处理。第二阶段只优化数据库和查询性能，不修改这套质量数据与
指标定义，以防性能优化掩盖正确性回归。
