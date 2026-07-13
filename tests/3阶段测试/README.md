# 3阶段测试：评测指标单元测试

这里不跑真实 LLM，也不跑真实 embedding。这里只测试 `eval/common.py` 里的评分函数，确保离线评测脚本的计分逻辑本身是可靠的。

真正调用模型的离线评测脚本在 `eval/` 目录中。

最近一次真实 LLM / embedding 离线评测结果已记录在 `eval/README.md`，完整 JSON 结果在 `eval/results/`。
