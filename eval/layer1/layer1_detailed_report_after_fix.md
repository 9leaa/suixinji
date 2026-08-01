# Layer 1 修复后详细报告

本报告对应远程运行的 `eval/layer1/run_regression.py --mode rules`，输出文件：
`eval/results/layer1_regression_rules_20260801_195219.json`。

## 结论

- 字段契约已集中到 `memory/field_contracts.py`。
- preference 的主题和 polarity、semantic 的稳定槽位、episodic 的 event 槽位、task 的身份和四态状态均由确定性归一化重算。
- `canonical_topic` 和 `memory_key` 不再接受模型自由改写。
- 泛化 semantic `fact` 不会自动合并；明确的当前槽位更新由规则处理。
- 任务画像在同秒写入时，done/cancelled 优先于 todo，避免旧任务遮蔽最新状态。

## 字段分母

Key-field Accuracy 的分母是全部 Gold 候选数 × 7；漏抽候选的七个字段全部算错。All-fields Exact 的分母是 Gold 候选数。详细定义见 `metric_definition.md` 和 JSON 版本。

## 现阶段限制

当前报告是 rules 模式，不能替代 LLM 端到端指标。LLM 版命令：

```bash
cd /home/zcj/suixinji
/usr/local/anaconda3/envs/zcj_hello/bin/python eval/layer1/run_regression.py --mode llm
```

两套扩展数据集缺失，需补齐后才能声称覆盖多候选和复杂噪声场景。
