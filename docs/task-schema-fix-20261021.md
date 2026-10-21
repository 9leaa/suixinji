# 任务抽取结构契约修复

## 原因与改动

原实现把新任务规则追加到含旧 task/entity/entities 结构的通用模板后；模型可能混用字段。任务写入采用 `extra=forbid`，因此一个额外字段就会令整条任务进入待确认。

现在启用任务卡片时，使用独立的 `extraction_contract` 构建按类型区分的输出 Schema。TaskInput/TaskDetailInput 是任务字段的唯一来源，非任务分支不再包含旧 task 类型。业务语义规则与字段定义分离，原有非任务解析模式保持兼容。

- `complete_json` 增加可选 `response_schema`，传递到 provider 请求。当前 `api.deepseek.com` 的 `deepseek-flash` 实测拒绝 json_schema（HTTP 400，This response_format type is unavailable now）。对此明确使用 JSON object 模式并在提示中携带同一 Schema，由本地 Pydantic 校验；不能称为服务端严格 Schema。非 DeepSeek 调用传入 Schema 时会请求 strict json_schema，本次仅测试了请求传递，未对其他服务商实测。
- 仅额外字段错误可触发一次格式修正请求。成功修正必须通过 TaskInput 校验，且所有原有合法字段、状态、身份、证据和细节保持等价；不能利用格式重试改写业务事实。
- 修正失败或接口失败，保留原候选及其校验错误，继续进入待确认；不丢掉同消息中的有效候选。原输出、校验错误、修正输出和结果保存在候选 scope.task_schema_repair，已测试持久化。
- 业务错误、无法对应原文的证据不会借格式重试绕过检查。输出字段合法也不代表语义正确。
- 更新问题追踪的 prompt hash，使其对应实际新模板。

## 验证结果

- 本地 187 项测试通过，包含字段分支隔离、嵌套额外字段、最多一次修正、非法修改状态被拒绝、重试失败保留候选、混合消息保留其他事实、审计落库、SDK Schema 请求传递，以及任务、偏好、主体关联回归。
- 原 #12 的 entities 和 #146 的 details[0].description_note 错误输出均用真实 deepseek-flash 成功修正，原有合法字段没有变化。记录：`eval/reports/task-schema-original-output-repair-20261021.json`。
- 从第一条消息完整重放两个问题及上一类身份关联案例：9 场景、23 消息全部通过，#12、#146 和上一类的 9 条目标记录均通过。记录：`eval/results/task-schema-fix-20261021-r2/`，摘要：`eval/reports/task-schema-fix-20261021.json`。
- 首轮保留在 `eval/results/task-schema-fix-20261021/`，其中两条语义失败分别为复制前文旧细节、因缺少会话前文提前待确认。补充规则后再跑完整 9 场景：当前 details 只能来自当前消息；主体和任务已明确但缺少轮次时交给旧卡召回判断，不在抽取阶段提前拒绝。

本次仅使用已验证可用密钥，模型为 deepseek-flash。没有修改生产接口配置、生产卡片或原消息，也未做完整 100 场景复测。
