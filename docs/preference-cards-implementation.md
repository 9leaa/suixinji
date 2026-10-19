# 偏好卡片实现与验证

分支：`codex/preference-cards`。实现目录：`/Users/zhangchengjie/Projects/suixinji/preference-cards`。

## 行为

同一空间、同一账号的“主体＋对象”定位一张卡片，极性不参与定位。每个细节使用自然语言保存条件、对象限定、原因，并携带原文、笔记 ID、消息时间。总体喜欢、早上不喜欢、晚上喜欢可同时存在；不确定更新目标时允许追加，确有歧义时进入 pending。

例：`我喜欢咖啡`、`我不喜欢早上喝咖啡`、`我喜欢晚上喝咖啡` 写入一张卡片的三条细节。`我现在早上也喜欢咖啡了` 只改变相应场景，旧细节成为历史。`我喜欢拿铁咖啡` 进入咖啡卡片，拿铁保留在描述；`我喜欢拿铁` 另建拿铁卡片，两张卡片可关联检索。

主体默认用户，明确第三人时保留“同事小明”等称谓。指代利用前三条用户消息；无法唯一确定时通知用户原文和原因。在线模型使用统一 JSON 输出及 Pydantic 校验；规则模式用于离线验证，仅覆盖常见表达和少量中英文别名，不代表所有自然语言都能用规则准确解析。

## 存储与入口

| 文件 | 职责 |
|---|---|
| `memory/preference_cards.py` | 抽取、先检索后写入、卡片合并决策、证据输出、整理、忘记 |
| `memory/service.py` | 接入现有四类记忆写入、revision 幂等、查询扩展 |
| `memory/workflow_state.py` | SQLite/PostgreSQL 持久化 pending、通知、问题、作业与遗忘来源 |
| `memory/workflow.py` | pending 生命周期、问题审核、JSONL 导出 |
| `memory/runtime_workflow.py` | 飞书/控制台反馈、失败重试、仅新增问题的日报 |
| `memory/preference_migration.py` | 从原始笔记迁移旧偏好，保留旧记录用于审计 |
| `scripts/memory_workflow.py` | 运维和问题复核 CLI |

卡片复用 `memories` 的 `scope_json.preference_card`，避免另建第二套记忆主表。`memory_key_version=preference-card-v1`；部分唯一索引保证身份唯一，版本比较和重试防止并发覆盖。内部操作 ID、版本、作业时间用于一致性，不作为偏好的生效时间。

查询复用精确、关键词、向量等现有检索渠道，补入关联卡片；卡片不按极性过滤。原文与消息时间进入查询结果及 Ask 证据模型，历史有独立标记。咖啡/拿铁有双向关联种子，其他对象通过现有混合召回建立关联；本版本没有独立的通用概念分类树。

## 自动整理

写入只保存 dirty 状态，后台每分钟检查。触发条件：卡片达到 10 条、空间累计新增细节达到 30 条、有变化且超过 24 小时，或者有变化且超过 7 天需要刷新关联。单卡片最多 20 条有效细节；历史和来源不计入有效条数，不删除事实强行凑数。

整理只能合并同态度、同条件的等价表达，来源合并且保留历史；跨态度、跨条件的模型合并会被拒绝并形成问题记录。超过上限的新内容进入 `card_capacity` pending，原消息保留；整理腾出空间后自动补写。仍不能合并则继续 pending。整理自身不增加业务变更计数。

作业和通知可重启恢复，卡片写入后、dirty 标记保存前中断也可通过变更计数恢复。并发更新依据当前版本重新读取，不覆盖其他消息的新内容。PostgreSQL 通知交给现有持久化 delivery 队列，外部平台最终送达仍遵循原有重试和去重语义。

## pending 与忘记

用户收到“原文＋pending 原因＋处理命令”。可使用：

```text
/pending resolve <pending_id> 同事小明
/pending cancel <pending_id>
```

忘记范围不清楚时，提示候选细节 ID；可回复具体 ID 或“全部”。任务运行失败由原任务队列重试，成功后通知解决；达到原任务最大重试次数后，需在任务控制台处理，取消 pending 仅取消提醒事项。

`请忘记我喜欢喝咖啡` 是管理操作，不写成负向偏好。匹配多项时 pending；匹配明确时停用对应细节及同一断言的历史，记录遗忘来源。查询、时间线、笔记证据读取、总结和重新抽取均避开这些证据。原始笔记物理文件仍保留；用户再次发送新的明确偏好可以重新记录。

## 问题数据集

自动收集处理异常、抽取/定位歧义、整理校验失败，`/feedback` 可附带最近查询证据与回答快照。疑似问题不是标准答案；必须人工确认并提供 expected 才能导出。数据包含结构版本、Prompt hash、抽取模式、来源/候选及可用的操作现场。外部模型的实际表现尚需在线评测；现有 trace 可供进一步定位。

```bash
python -m scripts.memory_workflow --space <space_id> pending
python -m scripts.memory_workflow --space <space_id> issues
python -m scripts.memory_workflow --space <space_id> review <issue_id> --status confirmed --reviewer owner --expected expected.json
python -m scripts.memory_workflow --space <space_id> export cases.jsonl
python -m scripts.memory_workflow --space <space_id> export shared-cases.jsonl --replacements replacements.json
```

`expected.json` 为人工标注 JSON；`replacements.json` 为一致脱敏映射，如 `{"小明":"人物A"}`。文件权限为 `0600`，默认导出供自己复核，不能认为已经自动去除了全部个人信息。数据按空间分组确定 development/test，防止同一空间的案例进入两侧；可在人工整理数据集时进一步细分场景。当前提供 CLI JSONL 导出，没有新增数据集网页。

## 启用与迁移

默认关闭新功能，避免在生产库未迁移时自动改变行为。在测试部署或正式启用时：

1. 备份数据库与原始笔记。
2. PostgreSQL 执行 `python -m alembic upgrade head`，升级至 `20261018_0016`；SQLite 首次使用自动建表。
3. 配置 `SUIXINJI_PREFERENCE_CARDS_ENABLED=true`，并重启 API/worker/scheduler。正式使用沿用现有模型配置；离线规则测试使用 `SUIXINJI_MEMORY_EXTRACTOR_MODE=rules`。
4. 预览旧偏好：`python -m scripts.memory_workflow --space <space_id> migrate`。
5. 执行迁移：`python -m scripts.memory_workflow --space <space_id> migrate --apply`。原文缺失或抽取不确定则保留旧记忆并记录 pending；已迁移的旧记录归档，重复运行不重复迁移。

可配置项：`SUIXINJI_PREFERENCE_CARD_SOFT_LIMIT`、`SUIXINJI_PREFERENCE_CARD_HARD_LIMIT`、`SUIXINJI_PREFERENCE_CARD_CHANGE_LIMIT`、`SUIXINJI_PREFERENCE_CARD_INTERVAL_SECONDS`、`SUIXINJI_PREFERENCE_CARD_RELATED_SECONDS`。以 `core/settings.py` 的实际变量读取为准。

新卡片一旦写入，关闭开关不会自动还原为旧的偏好结构。若要完全回退数据，应恢复启用前备份；不要把“关开关”当成数据迁移回滚。

## 验证

- 新增测试：`python -m pytest -q tests/test_preference_cards.py`。
- PostgreSQL：使用独立本地 `preference_test*` 数据库，迁移后执行 `python -m scripts.check_preference_postgres`。脚本验证原文时间、并发不丢写、整理不自循环、pending 持久化、忘记后阻止重放和重新表达。
- 完整测试已有两项基线失败：`test_complete_seed_memory_vectors_promotes_claimed_vectors` 和 `test_memory_handler_preserves_note_classification_for_model_extraction`。已在原目录复现，均为旧测试数据库依赖/fixture 问题。
- 本次未部署线上、未向真实飞书用户发送测试消息、未用真实模型验证自然语言效果。

分支保留了原目录已有的控制台等未提交修改作为开发基线；本次实现没有修改原工作目录的代码，已比较原目录 tracked diff 与开始前备份，完全一致。改动尚未提交或推送。

2026-10-18 最终验证：

| 检查 | 结果 |
|---|---|
| 新增偏好功能测试 | 30 项全部通过 |
| 新功能及记忆相关专项回归 | 88 项通过 |
| 全量 pytest | 597 通过、38 跳过、2 项原有失败（已在原工作目录复现） |
| 独立 PostgreSQL 迁移与集成 | 迁移到 0016；6 项集成场景通过 |
| 变更文件 Ruff、git diff --check | 通过 |

PostgreSQL 集成使用临时 Docker 测试库，完成后已停止并删除容器。以上是离线规则/模型契约测试与真实数据库验证，不等同于真实 LLM、向量模型和飞书的端到端验收。
