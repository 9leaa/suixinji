# 单元测试说明

当前目录覆盖最稳定、最适合自动化回归的确定性逻辑。这一批测试不调用真实 LLM，不生成真实 embedding，也不连接飞书。

运行命令：

```bash
python -m pytest tests
```

## 当前测试范围

- `test_taxonomy.py`：固定分类体系、type/tag 校验和规范化。
- `test_query_filter.py`：不依赖 LLM 的查询筛选逻辑。
- `test_summary_range.py`：手动总结时间范围计算。
- `test_summary_subscription.py`：自动总结订阅文件读写。
- `test_summary_scheduler.py`：自动总结 scheduler 的触发规则。
- `test_feedback.py`：dogfooding 反馈记录的本地落盘逻辑。

## 当前没有覆盖

- 真实 LLM 分类质量。
- 真实 embedding 语义检索质量。
- `/ask` ReAct 多轮工具选择质量。
- 飞书 SDK 真实收发消息。
- worker 从 WAL 到 markdown、index、vector 的完整集成链路。
- 总结生成内容是否真的高质量。
