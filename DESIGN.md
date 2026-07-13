# 随心记 Agent · 设计方案

> 本文件是项目的**长期记忆**。每次对话开始时可以让 AI 助手先读一下这份文件，
> 以便快速进入语境。

---

## 0. 项目目标

### 首要目标
**通过实现这个项目来系统性地学习 agent。** 实用性是次要的。

### 次要目标
做一个"随心记"agent：用户在 IM 里随手发文字/语音 → agent 自动分类、归档、
建立历史关联 → 用户提问时 agent 能准确召回 → 定期生成总结。

### 学习路径与所学概念的对应

| 项目阶段 | 对应学过的章节 | 学到的核心概念 |
|---|---|---|
| P1 Ingestion 链路 | ch-4 (tools)、ch-7 (my_llm) | Structured LLM output、WAL、async worker、写入路径与理解路径解耦 |
| P2 RAG 增强 | ch-8 (RAG / 语义分块) | Embedding、向量库、写入时关联历史 |
| P3 查询 ReAct | ch-4 (ReAct)、ch-7 (my_react_agent) | ReAct 循环、工具路由、何时该用 agent / 何时不该 |
| P4 定时总结 | ch-4 (Plan-and-Solve、Reflection) | 任务分解、自我反思、复杂输出的质量提升 |

---

## 1. 用户与场景（已确认）

- **第一版用户**：自己 + 身边几个朋友（不做正式注册/付费体系）
- **入口**：飞书自建应用机器人
  - 开发期优先使用飞书 Python SDK 的长连接方式接收事件，避免本地调试时必须暴露公网回调地址。
  - 后续部署到稳定服务器时，可切换为飞书事件订阅 Webhook。
- **附件支持**：MVP 只支持纯文本 + 语音转文字；图片/PDF/视频后置
- **存储**：本地 markdown 文件 + JSON 索引（看得见摸得着、便于调试和迁移）
- **多用户隔离**：不直接把平台字段写死为业务主键，统一抽象成 `space_id`
  - 单聊：`space_id = p_{open_id}`
  - 群聊：`space_id = g_{chat_id}`
  - 落盘前可再做安全化/哈希，避免平台 ID 中的特殊字符污染路径。

---

## 2. 整体架构

```
[飞书消息事件]
   │
   ▼
┌─────────────────┐
│ Feishu Receiver │  快路径：只做两件事
│ 长连接/Webhook   │  1. 追加到 WAL (cache/{space_id}.jsonl)
└────┬────────────┘  2. 立刻回 ack 给用户
     │
     │ asyncio.create_task(...)
     ▼
┌──────────────────┐
│ Background Worker│  慢路径，异步处理
│  ① LLM 分类打标签 │
│  ② 生成 embedding│
│  ③ 找相关历史笔记  │
│  ④ 落盘 markdown │
│  ⑤ 更新向量库     │
│  ⑥ 标记 WAL 已处理│
└──────────────────┘
     │
     ▼
┌──────────────────────────────────────┐
│ 持久层 notes/{space_id}/              │
│   ├── 2026-05-27.md   人能读的笔记     │
│   ├── index.json      元数据 + 链接    │
│   └── vectors/        向量库          │
└──────────────────────────────────────┘

──────────────────────────────────────────

[飞书里提问 "上次说的那家咖啡店"]
   │
   ▼
┌─────────────────┐
│ ReAct Agent     │  复用 ch-7 的 MyReActAgent
│ (查询路径)       │  工具：semantic_search /
└─────────────────┘        by_tag / get_note / follow_links
```

### 关键设计洞察

**写入路径不需要 ReAct。** ReAct 是给"需要多步推理决定调哪个工具"的场景用的；
打标签是单次结构化输出就够了，杀鸡不用牛刀。**只有查询路径才真正需要 ReAct**。

学习意义：**不是所有 LLM 调用都该是 agent**。强行所有路径都走 ReAct
反而学不到何时该用、何时不该用。

---

## 3. 阶段划分

### P1：Ingestion 链路（先做）
**目标**：发消息 → bot 立刻 ack → 后台落盘成 markdown，带 LLM 打的标签。

**做：**
- 飞书自建应用开启机器人能力
- 订阅接收消息事件 `im.message.receive_v1`
- 开发期用飞书 Python SDK 长连接接收事件；部署期可改成 Webhook
- 只处理 `message_type = text` 的消息；群聊里先只处理 @ 机器人的文本
- WAL 写入（JSONL append-only）
- 后台 worker 消费 WAL：调 LLM 给笔记打标签 + 生成标题/摘要
- 落盘到 `notes/{space_id}/{date}.md`
- 更新 `index.json`
- 标记 WAL 行为 processed
- 崩溃恢复：worker 启动时扫一遍 pending 行重做

**不做：**
- 语音
- RAG / 向量库 / 历史关联（P2）
- 查询 / 总结（P3、P4）
- 多进程、消息队列等生产级设施

### P2：RAG 增强
**目标**：每条新笔记自动带"相关的过去 3 条"链接。

**做：**
- 引入 OpenAI-compatible embedding 模型
- 使用本地 JSON 向量索引 `notes/{space_id}/vectors/index.json`
- worker 在落盘前先做一次语义检索找相关历史
- 把相关链接写进当前笔记的 metadata
- index.json 加 `related: [note_id, ...]` 字段
- 以 `message_id` / `note_id` 防止向量重复写入

**当前实现：**
- `core/llm_client.py::embed_text()` 读取 `EmbeddingConfig` 并调用 embeddings API。
- `storage/vector_store.py` 保存 `VectorItem`，用余弦相似度做本地 top-k 检索。
- `core/worker.py::process_record()` 的顺序是：分类 → embedding → 搜索相关历史 → 保存笔记 → 写向量 → 标记 WAL processed。
- 当前 `top_k=3`，`min_score=0.7`，默认 embedding 维度为 1024。

**配置：**
- `DASHSCOPE_API_KEY`：embedding 专用 key；为空时回退到 `OPENAI_API_KEY`。
- `EMBEDDING_BASE_URL`：embedding 专用 OpenAI-compatible base url；为空时回退到 `OPENAI_BASE_URL`。
- `EMBEDDING_MODEL`：默认 `text-embedding-v3`。
- `EMBEDDING_DIMENSION`：默认 `1024`。

**流程**
飞书收到消息
  -> 写入 WAL pending
  -> worker 处理 pending 记录
  -> classify_text() 做标题/标签/摘要
  -> embed_text() 生成当前文本 embedding
  -> search_related_note_ids() 搜索历史向量库
  -> NoteMetadata.related 写入相关 note_id
  -> save_note() 保存 markdown + index.json
  -> add_vector_item() 把当前笔记写入 vectors/index.json
  -> mark_processed() 标记 WAL 完成


### P3：查询与筛选
**目标**：用户既可以用固定 type/tags 直接筛选笔记，也可以在忘记分类条件时用 `/ask` 进行自然语言语义查询。

**当前实现：**
- 固定筛选路线不走 LLM，不生成 embedding，直接读取 `data/notes/{space_id}/index.json`：
  - `/type 生活`：按固定 type 精确筛选。
  - `/tag 饮食`：按固定 tag 精确筛选。
  - `/filter type=生活 tags=饮食,日常`：按 type + tags 组合筛选。
  - `/filter ... match=any`：多个 tags 任一命中即可；默认 `match=all`。
- 自然语言路线走 `/ask`，由 ReAct 判断是否调用工具：
  - `filter_notes(type, tags, match_all_tags, limit)`：明确 type/tags 时使用。
  - `semantic_search(query, top_k, min_score)`：用户忘记分类或自然语言描述时使用。
  - `list_recent(days, limit)`：查看最近笔记。
  - `get_note(note_id)`：读取完整笔记。
  - `follow_links(note_id, limit)`：查 related 的双向关联。
- `related_notes()` 保留为代码接口，但不再作为主要 ReAct 工具暴露；相关笔记问题由 `semantic_search -> follow_links` 两步完成。
- worker 增加向量回填：如果重跑 WAL 时发现 `index.json` 已有笔记但向量库缺失，会用笔记原文补写 `vectors/index.json`，避免 `semantic_search` 搜不到。

**入口：**
- 单聊：`/ask 上次说的那家咖啡店在哪`
- 群聊：`@随心记 /ask 上次说的那家咖啡店在哪`
- 直接筛选：`/type 生活`、`/tag 饮食`、`/filter type=生活 tags=饮食,日常`

### P4：总结与自动推送
**目标**：用户可以手动生成今天/昨天/一周/一个月/半年/一年的随心记总结，也可以开启每天固定时间自动总结并推送回飞书。

**当前实现：**
- 手动总结命令：
  - `/summary 今天`
  - `/summary 昨天`
  - `/summary 一周`
  - `/summary 一个月`
  - `/summary 半年`
  - `/summary 一年`
- 自动总结订阅命令：
  - `/summary_auto on`：为当前飞书单聊或群聊开启自动总结。
  - `/summary_auto off`：关闭当前会话自动总结。
  - `/summary_auto status`：查看开启状态、推送时间和最近发送日期。
  - `/summary_auto time 22:00`：修改每天推送时间。
- `summary/daily_summary.py` 负责读取时间范围内的 `index.json` 笔记，生成总结，并保存到 `summaries/`。
- 总结流程采用两段式 LLM 调用：先生成总结草稿，再 Reflection 自检并修订。
- `summary/subscription.py` 用 `data/summary_subscriptions.json` 保存每个 `space_id` 的自动总结订阅。
- `summary/scheduler.py` 使用标准库 `threading.Thread + sleep` 启动后台调度器，每分钟扫描一次订阅。
- 当天当前时间超过订阅时间，且 `last_sent_date` 不是今天，就自动生成并推送“今天”的总结。
- 发送失败时不更新 `last_sent_date`，下一轮继续重试。

**当前边界：**
- 自动总结只支持每天推送“今天”的总结，暂不支持自动周报/月报。
- 同一天内错过设定时间会补发；如果程序跨天后才恢复，不自动补发昨天，需要用户手动 `/summary 昨天`。
- 调度器是单进程内线程调度，不是分布式任务系统；未来多进程部署时需要引入文件锁、数据库锁或独立调度服务。

---

## 4. P1 的具体设计决定

### 决定 0：飞书接入方式 → 自建应用机器人 + 长连接优先
第一版用飞书企业自建应用，不做商店应用和多租户授权。

开发期采用 SDK 长连接接收事件：
- 不要求本机有公网 HTTPS 地址。
- 适合学习项目快速跑通消息接收、WAL、worker。

后续如果部署到服务器：
- 可切换为事件订阅 Webhook。
- 业务层保持不变，只替换 `FeishuReceiver` 的事件来源。

### 决定 1：WAL 格式 → JSONL append-only
每行一个 JSON：
```json
{
  "id": "uuid4",
  "source": "feishu",
  "event_id": "5e3702a84e847582be8db7fb73283c02",
  "message_id": "om_xxx",
  "space_id": "p_ou_xxx",
  "chat_id": "oc_xxx",
  "chat_type": "p2p",
  "sender": {
    "open_id": "ou_xxx",
    "user_id": "u_xxx",
    "union_id": "on_xxx"
  },
  "ts": "2026-05-27T14:52:33+08:00",
  "text": "...",
  "status": "pending"
}
```
状态从 `pending` → `processed`。崩溃后 worker 启动扫一遍把 pending 的重做。
**理由**：可 `cat` / `tail -f` 调试，没有第三方依赖。

幂等策略：
- 以飞书 `message_id` 去重，不依赖 `event_id`。
- 如果同一条消息重复推送，WAL 不重复写入或 worker 跳过已处理记录。

### 决定 2：进程模型 → 单进程 asyncio
飞书事件接收器 + worker 同一个 Python 进程，worker 是后台协程。
**理由**：学习项目最少活动部件；后续要拆进程很容易。

### 决定 3：Worker 触发方式 → asyncio.create_task
飞书事件接收器写完 WAL 后立刻 `asyncio.create_task(process_note(...))`，
不依赖轮询，延迟最低。
**理由**：和单进程 asyncio 模型最契合；天然学到"主路径快、副路径慢"的解耦感。

### 决定 4：飞书消息解析边界
P1 只解析文本消息：
- 从飞书事件体拿 `message.message_type`，只处理 `text`。
- `message.content` 按 JSON 解析，提取 `text` 字段。
- 群聊中去掉 @ 机器人的前缀，再进入分类链路。
- 非文本消息先回复"暂不支持此类型"，不进入 WAL。

### 决定 5：LLM 分类输出 schema
worker 调 LLM 给笔记打结构化标签：
```json
{
  "title": "在京都看到的咖啡店设计",
  "tags": ["旅行", "灵感", "设计"],
  "type": "灵感",
  "summary": "咖啡店用了原木和清水混凝土的对比……"
}
```
- `tags` 多标签（一条笔记可同时是"旅行"和"灵感"）
- `type` 单一主类型（用于归档目录可选）
- 标签由 LLM 自由生成，**不预设固定列表**（允许新概念涌现）
- 后续可以做"标签合并/规范化"功能（P3 之后）

### 决定 6：LLM 调用层独立出来
`classifier.py` 不直接读取环境变量、不直接创建 OpenAI client，也不绑定某一种 API 形态。

新的分工：
- `core/config.py`：读取 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`。
- `core/llm_client.py`：封装 OpenAI / OpenAI-compatible 调用，提供 `complete_json(...)`。
- `core/classifier.py`：只定义 `NoteClassification`、分类 prompt、`classify_text(...)`。

P1 暂时采用 Chat Completions + JSON 文本解析 + Pydantic 校验，而不是
`responses.parse(...)`。理由：当前本地 OpenAI-compatible 代理可能返回 HTTP 200，
但不完整支持 Responses API structured parse，导致 SDK 解析 `response.output` 为空。
Chat Completions + 本地 JSON 校验对代理更稳，也更适合后续 P3/P4 复用同一个 LLM 适配层。

---

## 5. 项目目录结构（计划）

```
/home/zcj/suixinji/
├── DESIGN.md                # 本文件
├── README.md                # 用户向说明（怎么跑起来）
├── requirements.txt
├── .env.example             # FEISHU_APP_ID, OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL 等
├── .gitignore
├── main.py                  # 启动飞书接收器 + worker
│
├── bot/
│   └── feishu_bot.py        # 飞书事件接收、消息 handler
│
├── core/
│   ├── config.py            # 环境配置读取
│   ├── llm_client.py        # LLM 调用适配层（OpenAI-compatible）
│   ├── wal.py               # WAL 读写、状态变更
│   ├── worker.py            # 后台 worker 主体
│   └── classifier.py        # 笔记分类 schema + prompt + 校验
│
├── storage/
│   ├── note_storage.py      # markdown + index.json 读写
│   └── vector_store.py      # P2 加：向量库封装
│
├── agent/                   # P3 加
│   ├── query_agent.py       # 基于 MyReActAgent
│   └── tools/               # ReAct 工具
│
├── summary/                 # P4 加
│   ├── daily_summary.py     # Plan-and-Solve + Reflection 总结
│   ├── subscription.py      # 自动总结订阅读写
│   └── scheduler.py         # 自动总结后台调度器
│
├── data/                    # 运行时生成（gitignore）
│   ├── cache/
│   │   └── {space_id}.jsonl # WAL
│   ├── summary_subscriptions.json # P4 加：自动总结订阅
│   └── notes/
│       └── {space_id}/
│           ├── 2026-05-27.md
│           ├── index.json
│           ├── vectors/     # P2 加
│           └── summaries/   # P4 加：总结 markdown + index.json
│
└── tests/
```

---

## 6. 技术栈（已定）

- **语言**：Python 3.10+
- **Bot / IM 接入**：飞书开放平台自建应用机器人
- **飞书 SDK**：`lark-oapi` / 飞书 Python SDK，开发期优先长连接事件接收，部署期可选 Webhook
- **LLM 配置层**：`core/config.py` 统一读取 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
- **LLM 调用层**：`core/llm_client.py` 使用 OpenAI SDK 的 Chat Completions，兼容 OpenAI-compatible 代理
- **LLM 输出约束**：模型返回 JSON object，本地用 Pydantic 校验为 `NoteClassification`
- **LLM**：OpenAI 或 OpenAI-compatible 服务（用户指定）
- **ReAct 框架**：复用 ch-7 的 `MyReActAgent`，**手写**而非 LangGraph
- **向量库（P2）**：本地 JSON 向量索引，文件位于 `data/notes/{space_id}/vectors/index.json`
- **嵌入模型（P2）**：OpenAI-compatible embeddings API，配置项为 `DASHSCOPE_API_KEY` / `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION`
- **定时调度（P4）**：标准库 `threading.Thread + time.sleep(60)`，每分钟扫描自动总结订阅

---

## 7. 不做清单（明确划界，避免范围蔓延）

- ❌ 账号系统、登录、付费
- ❌ 多端同步（云数据库）
- ❌ Web/移动 UI（飞书就是 UI）
- ❌ 生产级容错（重试队列、死信队列、监控告警）
- ❌ 微信个人号机器人（封号风险）
- ❌ 视频/PDF/Office 文件解析
- ❌ 协作功能（共享笔记、多人评论）

如果未来想做，单独立项。

---

## 8. 当前进度

- [x] P0：需求与架构设计（本文件）
- [x] P1：Ingestion 链路
- [x] P2：RAG 增强
- [x] P3：查询与筛选
- [x] P4：总结与自动推送

---

## 9. 飞书接入清单（P1 前置）

1. 在飞书开放平台创建企业自建应用。
2. 开启机器人能力，把机器人添加到自己的单聊测试环境或测试群。
3. 配置事件订阅：
   - 开发期：启用长连接事件接收。
   - 部署期：可切换到 Webhook，并配置 Encrypt Key / Verification Token。
4. 订阅消息事件：`im.message.receive_v1`。
5. 按最小权限申请消息相关权限，P1 需要能接收消息、读取消息文本、发送回复。
6. `.env` 中配置：
   - `FEISHU_APP_ID`
   - `FEISHU_APP_SECRET`
   - `FEISHU_VERIFICATION_TOKEN`（Webhook 时使用）
   - `FEISHU_ENCRYPT_KEY`（Webhook 加密时使用）
   - `OPENAI_API_KEY`
   - `OPENAI_BASE_URL`（可选；使用本地或第三方 OpenAI-compatible 代理时配置）
   - `OPENAI_MODEL`

## 9.1 P2 embedding 配置清单

1. 如果聊天模型和 embedding 模型走同一个 OpenAI-compatible 服务，只需要配置 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`，再确认 `EMBEDDING_MODEL` 和 `EMBEDDING_DIMENSION` 适配该服务。
2. 如果 embedding 单独走阿里云百炼或其他服务，配置：
   - `DASHSCOPE_API_KEY`
   - `EMBEDDING_BASE_URL`
   - `EMBEDDING_MODEL`
   - `EMBEDDING_DIMENSION`
3. 新消息处理成功后，应看到：
   - 当日 markdown 里出现 `related: ...`
   - `data/notes/{space_id}/index.json` 中有 `related` 字段
   - `data/notes/{space_id}/vectors/index.json` 中新增一条 1024 维向量记录

## 9.2 P4 总结功能使用清单

1. 手动总结命令：
   - `/summary 今天`
   - `/summary 昨天`
   - `/summary 一周`
   - `/summary 一个月`
   - `/summary 半年`
   - `/summary 一年`
2. 自动总结订阅命令：
   - `/summary_auto on`
   - `/summary_auto off`
   - `/summary_auto status`
   - `/summary_auto time 22:00`
3. 自动总结数据文件：
   - `data/summary_subscriptions.json` 保存当前会话的 `space_id`、`chat_id`、开启状态、推送时间和 `last_sent_date`。
   - `data/notes/{space_id}/summaries/` 保存生成后的总结 markdown 和总结索引。
4. 调度规则：
   - bot 启动时调用 `start_summary_scheduler(safe_send_text)`。
   - scheduler 每分钟扫描一次订阅。
   - 如果当前时间已经超过订阅时间，且当天未发送过，则生成并推送“今天”的总结。
   - 发送成功后更新 `last_sent_date`；发送失败则保留未发送状态，下一轮重试。

## 9.3 P5 可观测性 MVP 使用清单

1. 结构化日志文件：
   - 路径：`data/logs/app-YYYY-MM-DD.jsonl`。
   - 每行是一条 JSON event，方便后续按 `space_id`、`message_id`、`record_id`、`action` 检索。
   - `data/logs/` 已加入 `.gitignore`，不会提交运行日志。
2. 统一日志字段：
   - `ts`：事件时间。
   - `level`：`info` / `warning` / `error`。
   - `action`：业务动作，例如 `worker.process_record`。
   - `status`：`start` / `success` / `failed` / `skipped`。
   - `space_id`、`message_id`、`record_id`：用于串起一条消息。
   - `duration_ms`：动作耗时。
   - `error`：失败原因。
   - `extra`：动作相关的补充信息。
3. 当前已接入的 action：
   - 飞书入口：`feishu.message.received`。
   - 飞书命令：`feishu.command.summary`、`feishu.command.ask`、`feishu.command.status`。
   - worker：`worker.process_record`、`worker.classify`、`worker.write_note`、`worker.write_vector`、`worker.mark_processed`。
   - 查询：`query.answer_question`、`query.tool_call`、`query.final_answer`。
   - 自动总结：`summary.scheduler.tick`、`summary.auto.trigger`、`summary.auto.send`。
4. 飞书状态命令：
   - `/status` 返回当前会话 pending 数、自动总结状态、最近成功事件、最近 3 条错误和日志目录。
5. 隐私边界：
   - 默认不把完整消息正文或完整 `/ask` 问题写入日志。
   - 查询相关日志只记录 `question_len`、工具名、`top_k`、`min_score`、type/tags 等必要上下文。
6. 后续可继续补：
   - 日志轮转和保留天数。
   - 按 `message_id` 聚合的排障命令。
   - summary 发送历史文件。
   - Prometheus/metrics 或轻量告警。

---

## 9.4 Dogfooding 反馈收集 MVP 使用清单

1. 飞书反馈命令：
   - `/feedback 这次总结漏了健身计划`
2. 反馈数据文件：
   - 路径：`data/feedback/{space_id}.jsonl`。
   - 每行是一条 JSON feedback record。
3. 反馈字段：
   - `id`：反馈记录 ID。
   - `ts`：反馈时间。
   - `space_id`：反馈所属会话。
   - `message_id`：触发反馈的飞书消息 ID。
   - `text`：用户反馈内容。
   - `status`：当前默认 `open`。
4. 飞书日志：
   - 成功记录反馈时写入 `feishu.command.feedback`。
   - 空反馈会记录为 `skipped`，并提示用法。
5. 后续整理方式：
   - 每周人工查看 `data/feedback/`。
   - 误分类反馈整理进 `eval/data/classification_cases.jsonl`。
   - 搜不到反馈整理进 `eval/data/retrieval_cases.jsonl` 或 `eval/data/query_cases.jsonl`。
   - 总结遗漏/幻觉反馈整理进 `eval/data/summary_cases.jsonl`。

---

## 9.5 部署包装 MVP 使用清单

1. 配置检查：
   - `scripts/check_config.py` 检查 `.env` 是否存在。
   - 检查飞书、LLM、embedding 必要环境变量。
   - 检查 `data/` 目录是否可写。
2. 本地后台运行脚本：
   - `scripts/start.sh`：先检查配置，再用 `nohup` 后台启动 `python -m bot.feishu_bot`。
   - `scripts/stop.sh`：读取 `data/suixinji.pid` 并停止进程。
   - `scripts/status.sh`：检查 pid 文件和进程存活状态。
   - `scripts/logs.sh`：跟踪 `data/logs/runtime.log`。
3. 数据备份：
   - `scripts/backup_data.sh` 将整个 `data/` 打包到 `backups/suixinji-data-YYYYmmdd-HHMMSS.tar.gz`。
   - 当前备份包含 WAL、notes、vectors、logs、feedback 和 summary subscription 等运行数据。
4. 运行产物忽略规则：
   - `data/suixinji.pid`
   - `data/logs/runtime.log`
   - `backups/`
5. 已验证：
   - `scripts/check_config.py` 通过。
   - shell 脚本 `bash -n` 语法检查通过。
   - 未启动时 `scripts/status.sh` 返回 no pid file，符合预期。
   - `scripts/backup_data.sh` 能成功生成备份包。
   - 全量 pytest：`53 passed`。
6. 后续仍需补：
   - systemd unit 模板。
   - `docs/DEPLOY.md` 部署文档。
   - 数据恢复说明。
   - Docker / docker-compose 可选部署方式。

---

## 9.6 后续质量与生产化问题清单

P0-P5 已经形成完整功能闭环，并已补充本地部署脚本，但当前项目仍处于学习型 MVP。下面这些问题先记录下来，后续逐项解决。

### 1. 评测不足

**当前问题：**
- 分类、tag/type 选择、语义检索、ReAct 查询、总结质量主要靠人工试用判断。
- 没有固定样例集，改 prompt 或代码后很难判断是否退化。
- 自动总结是否漏掉重要笔记，目前缺少系统性检查。

**解决方法：**
- 建立 `tests/fixtures/`，放固定 WAL、index.json、vectors/index.json 和 summary 输入样例。
- 为 `classifier`、`filter_notes`、`semantic_search`、`summary` 建 pytest 单元测试。
- 建立离线评测脚本，例如 `eval/eval_query.py`、`eval/eval_summary.py`。
- 设计少量人工标注样例：问题、期望命中 note_id、期望 type/tags、总结必须包含的要点。
- 指标先简单化：分类准确率、检索 hit@k、总结要点覆盖率、自动总结触发是否正确。

**建议优先级：** 高。每次大改 prompt、分类规则、查询逻辑前都应先能跑基本评测。

### 2. 可观测性不足

**当前状态：**
- 已完成第一版 JSONL 结构化日志，覆盖飞书入口、关键命令、worker、查询和自动总结 scheduler。
- 已新增 `/status`，可查看当前会话 pending 数、自动总结状态、最近成功事件和最近错误。
- 仍缺少日志轮转、保留策略、metrics、告警和按 message_id 聚合的排障视图。

**后续解决方法：**
- 补日志轮转和保留天数，避免 `data/logs/` 长期增长。
- 对自动总结记录更细的发送历史：发送时间、note_count、summary path、是否成功。
- 增加按 `message_id` 查询整条链路的排障命令。
- 后续部署时可接入简单 metrics 和告警。

**建议优先级：** 高。可观测性会直接影响后续调试效率。

### 3. 部署包装不足

**当前状态：**
- 已新增 `scripts/check_config.py`、`start.sh`、`stop.sh`、`status.sh`、`logs.sh`、`backup_data.sh`。
- 已明确本地运行环境为 `zcj_hello`，普通运行日志为 `data/logs/runtime.log`，结构化业务日志为 `data/logs/app-YYYY-MM-DD.jsonl`。
- 已能一键备份 `data/` 到 `backups/`。
- 仍缺少 systemd / Docker、完整部署文档和恢复演练。

**后续解决方法：**
- 补 systemd unit，使服务支持开机自启和崩溃自动重启。
- 或提供 Dockerfile / docker-compose，用 volume 挂载 `data/` 和 `.env`。
- 写一份部署文档：首次启动、重启、查看日志、恢复 pending、备份/迁移数据。
- 做一次备份恢复演练，确认 `data/` 可以迁移到新目录继续运行。

**建议优先级：** 中。功能稳定后再做，但在长期运行前必须补。

### 4. 生产安全不足

**当前问题：**
- 本地 JSON 文件适合学习和调试，但并发、损坏恢复、跨进程锁都不是生产级。
- 飞书命令目前缺少权限控制，群聊中任何成员理论上都可能触发查询或总结。
- LLM 输入直接来自用户和笔记，存在 prompt injection、敏感信息外发和误操作风险。
- `.env`、缓存、笔记数据中可能含隐私内容，需要更明确的保护边界。

**解决方法：**
- 加命令权限控制：允许配置管理员 open_id / 群聊白名单。
- 对高影响命令做限制，例如自动总结订阅、未来删除/导出命令。
- 加速率限制，避免刷屏、刷 LLM API 或重复触发总结。
- 加数据备份和恢复策略，避免 JSON 写坏后不可恢复。
- 多进程部署前引入 OS 文件锁、SQLite 或更正式的数据库。
- 明确隐私策略：哪些内容会发给 LLM，哪些文件不进 git，如何清理本地数据。

**建议优先级：** 中到高。只给自己用可以后置，给朋友长期用前要补。

### 5. 缺真实使用数据

**当前状态：**
- 已新增 `/feedback` 作为 dogfooding 反馈入口。
- 反馈落盘到 `data/feedback/{space_id}.jsonl`，默认状态为 `open`。
- 仍需要持续真实使用，定期把反馈整理进 eval 样例。

**后续解决方法：**
- 先进行 1-2 周 dogfooding，只给自己和少量朋友使用。
- 收集真实失败样例：误分类、标签不合适、搜不到、总结遗漏、自动总结打扰。
- 每周人工复盘一次真实反馈和运行日志，更新 eval fixtures。
- 用真实数据决定下一阶段优先级，而不是只按想象扩功能。

**建议优先级：** 高。真实使用数据会反过来校正评测集和产品方向。

### 建议推进顺序

1. 可观测性 MVP 已完成；后续继续补日志轮转、按 message_id 排障和发送历史。
2. 部署包装脚本已完成；后续补 systemd / Docker / `docs/DEPLOY.md`。
3. 继续 dogfooding：用 `/feedback` 收集真实失败样例。
4. 根据真实问题补生产安全：权限、速率限制、备份恢复。
5. 持续补评测：把反馈沉淀为 eval 样例，防止后续改坏。

---

## 10. 变更记录

- 2026-06-07：完成部署包装 MVP 的脚本部分。新增 `scripts/check_config.py`、`start.sh`、`stop.sh`、`status.sh`、`logs.sh`、`backup_data.sh`；补充 `.gitignore` 忽略 pid、runtime log 和 backups；验证配置检查、脚本语法、备份脚本和全量测试 `53 passed`。
- 2026-06-07：新增 dogfooding 反馈收集 MVP。飞书支持 `/feedback ...`，反馈写入 `data/feedback/{space_id}.jsonl`，并记录 `feishu.command.feedback` 日志；后续用于整理真实失败样例到 eval 数据集。
- 2026-06-07：完成 P5 可观测性 MVP。新增 `core/observability.py`，结构化日志写入 `data/logs/app-YYYY-MM-DD.jsonl`；接入飞书消息和命令、worker、query agent、summary scheduler 关键 action；新增飞书 `/status` 状态命令。
- 2026-06-07：记录 P4 后的五类后续问题和解决路线：评测不足、可观测性不足、部署包装不足、生产安全不足、缺真实使用数据。
- 2026-06-07：完成 P4 总结功能文档同步。新增 `/summary` 手动总结；新增 `/summary_auto on/off/status/time` 自动总结订阅；总结会落盘到 `summaries/`，自动订阅保存到 `data/summary_subscriptions.json`，scheduler 按“同日超过设定时间即补发”的规则推送。
- 2026-06-06：完成 P3 查询与筛选文档同步。飞书新增 `/type`、`/tag`、`/filter` 直接查询命令；`/ask` 保留 ReAct 自然语言查询；worker 增加已存在笔记的向量缺失回填。
- 2026-06-06：补齐 P2 文档和配置说明。P2 当前采用 OpenAI-compatible embeddings API + 本地 JSON 向量索引，worker 在写入笔记前检索相关历史，并把 related note_id 写入 markdown 与 index.json。
- 2026-06-01：将 LLM 配置和调用从 `classifier.py` 拆到 `core/config.py` 与 `core/llm_client.py`。分类器只保留 schema、prompt 和校验；P1 改用 Chat Completions + JSON 解析 + Pydantic 校验，以兼容本地 OpenAI-compatible 代理不完整支持 Responses API structured parse 的情况。
- 2026-05-27：入口从 Telegram bot 改为飞书自建应用机器人。P1 默认采用飞书 SDK 长连接接收事件，并将存储主键从 Telegram `chat_id` 抽象为 `space_id`，以同时兼容飞书单聊和群聊。

---

## 11. 协作约定（用户给 AI 助手）

1. **用户写代码，AI 指导**。AI 不主动写业务代码，只在用户卡住时给提示、审查代码、解释概念。
2. **每完成一个阶段，更新本文件的"当前进度"。**
3. **遇到设计变更**（推翻原方案）时，在文件末尾加 "## 变更记录" 说明何时为什么改了什么。
4. AI 助手在长对话中如果上下文丢失，应主动让用户提示读这个文件。
