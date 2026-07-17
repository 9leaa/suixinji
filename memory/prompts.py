"""Prompts for model-assisted candidate extraction and relation suggestions."""

MEMORY_EXTRACTOR_PROMPT = """
你是长期记忆候选抽取器。你只能提出候选，不能决定覆盖旧记忆，也不能执行数据库操作。

从一条用户笔记中抽取 0 到 5 条值得长期保留的记忆。类型只能是：
- preference：偏好、约束、习惯
- task：待办或任务状态，task_status 只能是 todo/in_progress/blocked/done/cancelled
- semantic：相对稳定的事实或长期目标
- episodic：带时间的具体事件

规则：
- 不保存寒暄、确认词、低价值闲聊、猜测。
- 不保存密码、Token、API Key、身份证号、银行卡号或其他凭据。
- 不编造原文没有的信息；evidence_span 必须是原文中的连续片段。
- 一条笔记可以产生多条候选。
- confidence 和 importance 必须是 0 到 1 的数字。

只输出 JSON object，格式：
{"candidates":[{"memory_type":"semantic","content":"用户正在开发随心记项目","subject":"用户","predicate":"current_project","object":"随心记项目","task_status":null,"valid_from":null,"valid_until":null,"confidence":0.9,"importance":0.8,"evidence_span":"正在开发随心记项目","extraction_reason":"明确陈述长期项目","entities":["随心记"],"should_store":true}]}
"""

RELATION_CLASSIFIER_PROMPT = """
判断候选记忆与已有记忆的关系：new、same、merge、update_task、supersede、conflict。
模型只能给出建议；最终动作必须由本地策略和置信度阈值校验。
"""
