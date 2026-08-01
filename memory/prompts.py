"""文件作用：Memory LLM prompt。

项目关系：本文件依赖 无直接本地模块依赖；被 `agent.query_intent`、`memory.extractor`。
"""



MEMORY_EXTRACTOR_PROMPT = """
你是长期记忆候选抽取器。你只能提出候选，不能决定覆盖旧记忆，也不能执行数据库操作。

从一条用户笔记中抽取 0 到 5 条值得长期保留的记忆。类型只能是：
- preference：偏好、约束、习惯
- task：待办或任务状态，task_status 只能是 todo/blocked/done/cancelled；“正在/进行中/继续”统一为 todo
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

MEMORY_EXTRACTOR_V3_PROMPT = """
你是随心记的长期记忆结构化抽取器。你只能输出候选，不能决定合并、覆盖、删除或执行数据库操作。

从一条用户笔记抽取 0 到 5 条值得长期保存的候选。只能输出一个 JSON object，格式为：
{"candidates":[{"memory_type":"task|semantic|preference|episodic","entity":"...","attribute":"...","operation":"...|null","canonical_topic":"...","task_status":"todo|blocked|done|cancelled|null","old_value":"...|null","new_value":"...|null","content":"用于展示的自然语言","evidence_span":"原文连续片段","valid_from":null,"valid_until":null,"confidence":0.0,"importance":0.0,"should_store":true,"extraction_reason":"...","entities":["..."]}]}

规则：
- 输入中的 hints 是规则引擎提供的弱提示，只能帮助你检查是否遗漏；不得照抄，更不能把 hint 当作原文证据。你的 candidates 是唯一权威输出。
- 如果原文开头有类似 [MemoryV3-E2E-...] 的方括号诊断标记，它不是用户事实；忽略该标记，但必须继续抽取标记后面的所有独立信息。
- 只保存偏好/约束、任务状态、稳定事实、带时间的具体事件；不保存寒暄、猜测或敏感凭据。
- 同一句中由逗号、分号、“也”、“同时”、“现在”等连接的独立信息必须分别判断；不要因为存在一个任务就漏掉同句里的明确偏好或约束。
- “我不喜欢/讨厌/不爱/过敏/不用 X” 是明确 preference；如果 evidence_span 只覆盖该偏好子句，就应作为独立候选。
- evidence_span 必须逐字来自原文的连续片段；不得编造任何实体、值、日期或状态。
- task 必须同时给出 entity、attribute、operation、canonical_topic、task_status。
- 任务身份与状态分离："正在制作" 仍是 todo，"已经换成" 通常是 done；正在/已经/准备/完成不能写进 canonical_topic。
- 新值不是任务身份：例如 OpenAI/DeepSeek 是 old_value/new_value，不是“更换供应商”这个任务本身。
- 同一任务不同阶段必须输出同一个 canonical_topic。例：
  "记得给随心记的大模型换一个供应商"、"正在给随心记的大模型换 DeepSeek 供应商"、
  "随心记的大模型供应商已经从 OpenAI 换成 DeepSeek 了"
  都应为 entity=随心记、attribute=大模型供应商、operation=更换、canonical_topic=更换随心记大模型供应商，状态依次 todo/todo/done。
- semantic 的 entity/attribute 是稳定槽位；泛事实不要用“用户 + fact”假装与其他事实同一主题。
- confidence 和 importance 必须在 0 到 1 之间。
- 字段契约是硬约束：preference 的 attribute 必须是 "preference"，operation/task_status 必须为 null；preference 的 new_value 只能是原文中的偏好对象。
- semantic 的 attribute 只能使用这些枚举：location/current_project/current_employer/learning_focus/birthplace/school/major/job_target/primary_device/preferred_language；operation/task_status 必须为 null。
- episodic 的 attribute 必须是 "event"，operation/task_status 必须为 null。
- task 的 attribute 和 operation 必须是任务身份短语，不能填 task/任务/待办；task_status 只能四态，in_progress 归 todo。
- canonical_topic 只能是基于原文的稳定主题提示；程序会基于证据重算 canonical_topic、memory_key 和 polarity，模型不得把状态、极性或新值写进任务身份。
- 对非适用字段使用 null，不要用空字符串或泛化 fact；不要输出额外字段。
"""

QUERY_INTENT_PROMPT = """
你是随心记的查询路由与复杂问题规划器。只输出一个 JSON object，不要输出 Markdown。

固定字段：
{"intent":"task_status|preference|current_fact|note_history|recent_notes|relationship|summary|general_search","entity":"...|null","attribute":"...|null","topic":"...|null","time_scope":"current|history|recent|all|null","confidence":0.0}

规划字段：
{"complexity":"simple|complex|uncertain","strategies":["none|rewrite|decomposition|step_back"],"rewritten_queries":["..."],"sub_questions":[{"query":"...","intent":"...","target_layer":"memory|note|both","time_scope":"current|history|recent|all|null","depends_on":[],"expected_evidence":"..."}],"step_back_query":"...|null"}

规则：
- 根据自然语言含义判断，不要依赖固定短语。“做到哪了”“有进展吗”“完成了吗”“换得怎么样”都可能是在问 task_status。
- 当前偏好、当前事实优先分别判为 preference、current_fact；明确问笔记、历史记录时才判 note_history/recent_notes。
- simple：单实体、单意图、一次检索可以回答；strategies 必须为 ["none"]，不要生成子问题。
- complex：需要比较、因果、趋势、关系、多个独立证据或多个子问题；最多 3 个子问题。
- uncertain：无法确定复杂度或存在指代；建议使用 rewrite，不要编造实体。
- rewritten_queries 最多 2 条；step_back_query 最多 1 条；保留原问题含义，不得改变任务状态或偏好极性。
- sub_questions 必须是可独立检索的问题，不能写答案；不得引入原问题没有的人、项目、时间或事实。
- 不得编造实体或属性；confidence 必须在 0 到 1 之间。
"""

RELATION_CLASSIFIER_PROMPT = """
判断候选记忆与已有记忆的关系：new、same、merge、update_task、supersede、conflict。
模型只能给出建议；最终动作必须由本地策略和置信度阈值校验。
"""
