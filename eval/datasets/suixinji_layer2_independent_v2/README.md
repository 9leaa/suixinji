# 随心记 Layer 2 独立评测数据集 v2

- Cases：400
- 场景族：16
- Task状态契约：`todo / done`
- 覆盖实例身份、Family召回、Preference Assertion、Blocker/Progress/Closure、Done Task、Reopen、Pending Review、指代Source、旧状态只读投影和可选LLM裁决。
- 旧状态只允许出现在`legacy_status_read_projection`输入中；新持久化结果只能产生`todo / done`。
- v2不会覆盖旧版v1。
