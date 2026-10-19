# 偏好卡片与消息流程控制台联调

验证日期：2026-10-18。运行目录：`/Users/zhangchengjie/Projects/suixinji/server`。

## 合并与运行

当前分支为 `codex/preference-console`。以 `feat/message-lifecycle-console` 工作目录为基础，整合 `codex/preference-cards` 的增量。两边此前功能都未提交，因此此次为工作区代码整合，未生成 Git merge commit，也未推送。整合前比对了控制台原有改动与开发基线，确认没有覆盖后续修改。原偏好 worktree 保留。

- 后端：`http://127.0.0.1:18000/health`，HTTP 200。
- 控制台：`http://127.0.0.1:18080/console`。
- 本地 PostgreSQL 数据库 `suixinji_v2` 已从 `20261018_0015` 升级到 `20261018_0016`；Redis 使用现有 16379 服务。
- 本地 `.env` 已开启 `SUIXINJI_PREFERENCE_CARDS_ENABLED=true`；沿用真实 DeepSeek 模型、DashScope 向量服务、hybrid 抽取与 Ask V1 配置。
- 12 个分布式角色和独立控制台进程均已启动。PID 在 `data/pids/`，日志在 `data/logs/`。进程通过独立会话启动，工具命令退出后继续运行。
- 控制台访问令牌仍在 `data/console.token`，未更换；页面连接后会显示“偏好卡片 ●”。

备份目录：`/Users/zhangchengjie/Projects/suixinji/workspace/backups/preference-console-20261018-170040`。包含整合前 diff、被修改文件、受限权限配置备份、PostgreSQL custom-format 全库备份 `database.dump`、测试 trace 和截图。备份中含私人数据，不要提交 Git。

## 本次联调修复

控制台入口此前只带 `reply_channel=console`，没有 `chat_id`，新记忆工作流因此无法保存通知回传地址。现在每条消息使用 `console:<trace_id>`，pending 与忘记结果回到对应页面，不进入飞书投递队列。新增两项回归覆盖消息路由和 pending 结果回传。

控制台配置状态和每次运行快照加入 `preference_cards`；偏好解析、关联检索、合并决策、写入、忘记均加入真实流程节点。

## 验证结果

`python -m pytest -q tests/test_preference_cards.py tests/test_lifecycle_console.py`：57 passed。新偏好模块 Ruff、控制台改动 F 类检查与 `git diff --check` 通过；控制台原有 E701/E702 紧凑写法未改动。本次未重复全量测试，先前全量结果见 `preference-cards-implementation.md`。

测试空间 `console-preference-1018`、用户 `console-user`，使用实际 HTTP 入口、PostgreSQL、Redis Streams、Worker、真实模型和向量服务：

| 输入 | 已验证结果 |
|---|---|
| 我喜欢喝咖啡 | 创建“用户＋咖啡”卡片 |
| 我不喜欢在早上喝咖啡 | 同一卡片追加早上不喜欢，保留总体喜欢 |
| 我喜欢在晚上喝咖啡 | 同一卡片追加晚上喜欢，合计三项有效细节 |
| /ask 我喜欢咖啡吗？早上和晚上分别怎么样？ | 正确回答总体喜欢、早上不喜欢、晚上喜欢；引用卡片和三条笔记，证据含原文与消息时间 |
| 他喜欢喝茶 | 进入 pending；页面显示原文、指代不明原因与补充命令 |
| /pending resolve … 同事小明 | 原 pending resolved，创建同事小明的茶偏好 |
| 请忘记同事小明喜欢喝茶 | 对应细节 forgotten，页面收到确认，咖啡卡片仍保留三项有效细节 |

查询 trace：`9a6df4d0266f4c80914f836246bf65b3`。浏览器实际打开页面并检查了完成状态、流程节点和回答。上述测试没有创建飞书 delivery 任务。后台 `preference_maintenance` 任务已实际执行成功；没有等到 24 小时或凑够 30 条来验证定时/数量阈值，其逻辑由自动化测试覆盖。

历史偏好未批量迁移，保持原结构且仍可检索；新输入走卡片流程。若要转换某个旧空间，按实现说明执行 migrate 预览与迁移。此次不修改大量历史评测空间。飞书接收进程已重启，但未发送真实飞书消息做端到端验收。

## 下次启动

在运行目录执行：

```bash
PYTHON="$PWD/.venv/bin/python" bash scripts/start_distributed.sh
.venv/bin/python scripts/run_console.py
```

第二条以前台方式运行控制台，适合本机终端使用；当前控制台已经在后台运行，不要重复启动同一端口。停止脚本会处理 `data/pids/` 中的控制台 PID；启动分布式脚本本身不包含控制台，需要单独启动。

## 2026-10-18 指代回归修复

用户在 console 空间输入“我好喜欢成龙大哥啊。”“我喜欢他的电影《十二生肖》”后查询电影，暴露了前一次咖啡测试没有覆盖的问题：`extract_llm_candidates` 的前文发送条件没有识别“他”，实际模型输入的 previous_messages 为空；service 又在卡片解析前把旧候选的 unresolved 拦成 pending。

已补充人物和作品指代信号、主语与对象定语指代示例。偏好候选由卡片解析最终判断指代是否确定，其他类型仍沿用原校验。解析结果中的 antecedent_note_id 必须属于提供的前文；“我喜欢他的电影”的主体仍为用户。最终卡片写入节点输出 preference_card，候选节点说明中间协议与最终结构的区别。目前仍是四类记忆共用候选提取、偏好再转卡片的两阶段实现。

同时修复总体态度依赖 description 字面匹配的问题：模型返回内部 is_general 标记，程度、称呼和原因不再改变总体态度判断；具体电影、低糖品类与时间条件不被扩大为对整个对象的喜好。已有规则模式和旧数据保留兼容判断。

新增四项回归后，记忆抽取、偏好卡片、控制台相关测试共 103 项通过，相关 Python 文件 Ruff 与 diff 检查通过。独立空间 console-reference-1018 的三条真实请求已得到《十二生肖》的正确回答。

原 console 数据已备份后修复：第一条卡片总体态度为喜欢；第二条按原笔记和原消息时间写入电影卡片，保存 antecedent_note_id=inbox_071f6e029994，原 pending 已解决。修复 trace 为 fc99768e79124c5187028cca6d91f05b；原空间重新查询 trace 为 54ce8a6e0d2846d6b09e5365829e4420，真实回答“你提到喜欢成龙的电影《十二生肖》”。历史失败 trace 保留当时证据。备份和回放记录在 workspace/backups/pronoun-fix-20261018。

问题案例 71e8678a469bfba7827b1a04919a04bb 已收集为 suspected，附实际失败、前文、拟定预期和修复 trace；没有伪称已完成人工标准答案审核，也未对外导出。相关 Worker 和控制台已重启。

## 2026-10-18 第一阶段直接提取偏好字段

替代上文的过渡实现：开启偏好卡片时，一次模型提取返回混合 `candidates` 数组。偏好直接使用 `subject / preference_object / attitude / condition / description`，另带 `is_general`、原文证据、指代来源、未决原因和质量分数；任务、稳定事实、事件仍使用原 V3 字段。

- `memory/prompts.py`：独立的混合类型提示词，偏好不再输出 entity/attribute/polarity/qualifiers 等旧协议字段。
- `memory/preference_schema.py`：第一阶段偏好契约与卡片细节契约共享字段；第一阶段必须明确 condition、is_general。
- `memory/extractor.py`：新偏好字段装入内部通用候选的 `scope.preference_detail`，仅作为传输适配。旧 canonicalizer 不再重算它的主体、对象、条件或极性。hybrid 保留其他类型的规则补全；疑似遗漏的偏好进入 pending，不再暗中调用第二次模型。
- `memory/preference_cards.py`：普通写入只校验第一阶段结果，再关联检索、决定追加/合并、写入。已有卡片的合并决策仍可调用模型；这是合并，不是重复抽取。用户明确发起的忘记、pending 补充说明仍可单独解释新指令。
- `memory/candidate_validator.py`、`memory/service.py`：未决结果即使被模型标成 should_store=false，也必须生成 pending 和原文/原因通知；一般不值得存储的消息仍正常丢弃。问题样本保留结构化输出；`memory/workflow.py` 记录新提示词版本。
- 控制台将解析节点改为“偏好卡片 · 校验提取结果”，更新候选说明。历史 trace 保留当时输出，只有新请求使用新字段。

验证：新增 15 项直接提取测试，抽取/偏好/控制台专项共 118 passed；Ruff 与 `git diff --check` 通过。隔离开发 `.env` 的卡片开关后，全量离线 `DATABASE_URL='' REDIS_URL='' .venv/bin/python -m pytest -q` 为 **618 passed、38 skipped、2 failed**。两项失败为既有记录中的 seed vector promotion 和 memory handler classification 测试，本次没有改动相应功能。测试默认关闭卡片开关，新功能测试显式开启，避免开发环境配置改变旧协议测试含义。最初未隔离的全量运行受到本机 feature flag 和在运行队列影响，其结果不作为离线基线。

真实 HTTP → PostgreSQL/Redis → Worker → 模型验证均在独立 console 测试空间：

| 场景 | 结果与 trace |
|---|---|
| 我喜欢孙燕姿 → 我喜欢她的《我不难过》这首歌 | 指代来源可核验；主体仍为用户；新模型输出直接为歌曲对象与完整描述。第二条 `56fece4ec0b94f99b72e7a4c31020e26` |
| 查询孙燕姿歌曲 | 正确回答《我不难过》，`95d78aaa350c48058141c6d22995fc90` |
| 喜欢咖啡 → 早上不喜欢 → 晚上喜欢 | 同一卡片三条细节，保留总体喜欢；第三条 `4209f76d5aec45cda1d3a95512f103b1` |
| 查询早晚咖啡偏好 | 正确分场景回答，引用卡片、原文与消息时间；`c3db4050b59645e198c6076c2b37c69c` |
| 无前文“他喜欢喝茶” | pending，控制台收到原文与指代不明原因；`252b3d5d1ddf4f9fac7955c63799228c` |
| 补充“同事小明”与忘记 | 补充后可创建该主体的偏好，随后成功忘记；`a92b80591f204c8ebafcf260225b0ade` / `f6826c7d7f444daca3ea535e3c0fb57b` |

逐项检查了真实模型节点 JSON：普通偏好每条只有一次候选提取调用，偏好输出没有旧协议字段。首轮真实模糊指代测试发现 should_store=false 会跳过提醒，已补回归并通过上表的第二轮验证。原失败 trace 保留。

真实 trace 与全量测试日志保存在 `../workspace/backups/direct-preference-20261018/`，含本地测试数据，不提交。memory/query Worker 和 18080 控制台已重启，18000 健康接口正常。无数据库结构变更，无批量重写用户历史记忆；仍在 `codex/preference-console` 工作区，未提交或推送。
