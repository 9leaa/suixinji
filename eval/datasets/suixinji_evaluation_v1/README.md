# suixinji_evaluation_v1

按 `SUIXINJI_EVALUATION_DATASET_CONSTRUCTION_GUIDE.md` 构造的大规模 world-spec-first 数据集。运行 `python eval/datasets/build_suixinji_eval_v1.py` 可确定性重建，再运行 `python eval/datasets/validate_suixinji_eval_v1.py` 校验。L1/L2 对应 independent v2 输入，L3 对应 retrieval_answer v2 五分片。数据均为合成 world，不包含 PostgreSQL 或真实用户数据。Gold 冻结后，模型只可负责自然语言改写，不能决定标签。
