# 随心记评测集覆盖矩阵

| 数据集 | 主要能力 | 数量 |
|---|---|---:|
| L1 | 四类记忆、task 二元状态、blocker、多偏好、多 Candidate、指代、猜测/敏感/噪声过滤 | 1000 |
| L2 | new/same/update/conflict、同 family 异 instance、歧义 pending-review、偏好反转 | 800 |
| L3 | current/history/no-answer/conflict/clarification/restricted/qualified-history、多证据、语义噪声、stale 防护 | 800 |
| L1→L2 | todo→progress_note→done、逐轮 relation/action、三版本三来源 | 300 |
| 全链路 | 多轮消息与 current/history ask checkpoint | 200 |

主题域为园艺、烘焙、旅行、摄影、健身、乐器、阅读、宠物照护、收纳、语言学习、理财、木工、绘画、自然观察、烹饪、露营、播客、社区志愿、家庭收纳、陶艺，不复用既有 RAG/数据库/咖啡/随心记主体。

Gold 冻结规则：只由每条记录的 `world_spec` 与确定性映射生成；失败样本导出为 `failed_cases.jsonl`，不得回写 Gold。
