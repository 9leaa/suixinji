# 随心记 Agent

一个用于学习 agent 的飞书随手记项目。当前主线已经跑到 P4：文本消息进入 WAL 后，由 worker 做 LLM 分类、生成 embedding、检索相关历史笔记，并落盘为 markdown、index.json 和本地向量索引；查询侧支持固定 type/tags 的直接筛选和 `/ask` 自然语言语义问答；总结侧支持手动总结和每天定时自动总结。

## 配置

基础配置见 `.env.example`。P2/P3 的 embedding 配置为：

```env
DASHSCOPE_API_KEY=
EMBEDDING_BASE_URL=
EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_DIMENSION=1024
```

如果 embedding 专用配置为空，代码会回退使用 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`。聊天分类和 `/ask` 的 ReAct 判断仍使用 `OPENAI_MODEL`。

运行飞书 bot 的 Python 环境需要安装 `requirements.txt`，尤其是 `lark-oapi`。

## 运行后产物

- WAL：`data/cache/{space_id}.jsonl`
- 笔记 markdown：`data/notes/{space_id}/{YYYY-MM-DD}.md`
- 笔记索引：`data/notes/{space_id}/index.json`
- 向量索引：`data/notes/{space_id}/vectors/index.json`
- 总结 markdown：`data/notes/{space_id}/summaries/{start}_{end}_{range}.md`
- 总结索引：`data/notes/{space_id}/summaries/index.json`
- 自动总结订阅：`data/summary_subscriptions.json`

每条新笔记会先用当前文本 embedding 搜索历史向量，最多写入 3 条 `related` 笔记 ID，然后再把当前笔记加入向量索引。

## 飞书用户功能

### 1. 随手记录

用户直接给机器人发送普通文本，机器人会把它当作一条新笔记：

```text
今天看了一篇关于 RAG 语义分块的文章，感觉按标题层级切分不一定适合小说。
```

处理流程：写入 WAL -> LLM 分类生成 `title/type/tags/summary` -> 生成 embedding -> 查找 related 历史笔记 -> 写入 markdown、`index.json`、`vectors/index.json`。

群聊里使用时，先 @ 机器人再发送内容；代码会去掉 @ 前缀后再归档。

### 2. 自然语言查询

用户用 `/ask` 提问。适合忘记了 type/tags，或者想用自然语言描述要找的内容：

```text
/ask 上次我说吃馅饼是什么时候？
/ask 和 P2 功能测试相关的笔记有哪些？
/ask 最近我记录过什么学习内容？
```

`/ask` 会进入 ReAct 查询流程，必要时使用 embedding 语义检索和 LLM 总结回答。

### 3. 按 type 直接筛选

用户明确知道大类时，用 `/type`。这个命令不调用 LLM，不生成 embedding，只筛选 `index.json`：

```text
/type 生活
/type 学习 20
```

第二个参数可选，表示最多显示多少条，默认 30。

### 4. 按 tag 直接筛选

用户明确知道标签时，用 `/tag`。这个命令只做固定 tag 精确匹配：

```text
/tag 饮食
/tag 提醒 20
```

第二个参数可选，表示最多显示多少条，默认 10。

### 5. 组合筛选

用户同时知道 type 和 tags 时，用 `/filter`：

```text
/filter type=生活 tags=饮食,日常
/filter type=生活 tags=饮食,日常 match=any limit=30
/filter tags=提醒,待处理 match=any
```

参数说明：

```text
type=生活              可选，固定 type 精确匹配
tags=饮食,日常         可选，多个 tag 用逗号、中文逗号或顿号分隔
match=all              默认，笔记必须包含所有 tags
match=any              笔记包含任意一个 tag 即可
limit=30               可选，最多显示多少条
```

### 6. 手动总结

用户可以用 `/summary` 手动生成某个时间范围的随心记总结：

```text
/summary 今天
/summary 昨天
/summary 一周
/summary 一个月
/summary 半年
/summary 一年
```

`/summary` 会读取当前会话 `space_id` 下对应时间范围的 `index.json`，用 LLM 生成总结，再做一次 Reflection 自检。总结会发回飞书，也会保存到 `data/notes/{space_id}/summaries/`。

### 7. 自动总结

用户可以为当前飞书单聊或群聊开启每天自动总结：

```text
/summary_auto on
/summary_auto off
/summary_auto status
/summary_auto time 22:00
```

命令说明：

```text
/summary_auto on          开启自动总结，默认每天 22:00 推送“今天”的总结
/summary_auto off         关闭当前会话的自动总结
/summary_auto status      查看当前会话是否开启、推送时间和最近发送日期
/summary_auto time 22:00  修改每天自动总结的推送时间，格式为 HH:MM
```

bot 启动后会启动一个后台 scheduler，每分钟检查一次订阅。当天超过设定时间且今天还没发送过时，会自动生成并推送总结；如果发送失败，不会更新 `last_sent_date`，下一轮会继续重试。

当前自动总结只做“同一天超过设定时间补发”。如果程序跨天后才恢复，不会自动补发昨天的总结；需要时可以手动发送 `/summary 昨天`。

### 8. 反馈收集

如果发现分类不准、搜不到、总结遗漏或自动总结打扰，可以在飞书里发送：

```text
/feedback 这次总结漏了健身计划
```

反馈会保存到：

```text
data/feedback/{space_id}.jsonl
```

每条反馈包含 `id`、`ts`、`space_id`、`message_id`、`text` 和 `status=open`。后续可以把这些真实失败样例整理进 `eval/data/`，用于改进分类、检索和总结评测。

### 9. 运行状态与日志

用户可以在飞书里发送：

```text
/status
```

当前 `/status` 会返回：

```text
- 当前会话 pending 数
- 当前会话自动总结状态
- 最近一次成功处理事件
- 最近 3 条错误事件
- 日志目录
```

系统会把关键运行事件写入 JSONL 结构化日志：

```text
data/logs/app-YYYY-MM-DD.jsonl
```

当前已接入的主要 action：

```text
feishu.message.received
feishu.command.summary
feishu.command.ask
feishu.command.status
feishu.command.feedback
worker.process_record
worker.classify
worker.write_note
worker.write_vector
worker.mark_processed
query.answer_question
query.tool_call
query.final_answer
summary.scheduler.tick
summary.auto.trigger
summary.auto.send
```

日志字段统一包含 `ts`、`level`、`action`、`status`、`space_id`、`message_id`、`record_id`、`duration_ms`、`error` 和 `extra`。为了减少隐私暴露，默认不把完整消息正文或完整 `/ask` 问题写入日志，只记录长度和必要上下文。

### 10. 暂不支持的消息

当前只支持文本消息。语音、图片、文件会回复“暂时只支持文本消息”，不会进入 WAL。

## 本地部署脚本

当前已提供一组轻量脚本，统一使用 `zcj_hello` 环境：

```text
scripts/check_config.py   启动前检查 .env、必要环境变量和 data/ 可写性
scripts/start.sh          后台启动飞书 bot，写入 data/suixinji.pid
scripts/stop.sh           根据 data/suixinji.pid 停止 bot
scripts/status.sh         检查 pid 对应进程是否存活
scripts/logs.sh           跟踪 data/logs/runtime.log
scripts/backup_data.sh    将 data/ 打包到 backups/
```

常用命令：

```bash
/usr/local/anaconda3/envs/zcj_hello/bin/python scripts/check_config.py
bash scripts/start.sh
bash scripts/status.sh
bash scripts/logs.sh
bash scripts/stop.sh
bash scripts/backup_data.sh
```

运行产物会被 `.gitignore` 忽略：

```text
data/suixinji.pid
data/logs/runtime.log
backups/
```

## 当前边界与后续计划

项目已经具备从记录、检索到总结的完整学习闭环，但还没有进入生产化状态。后续需要重点补齐：

- **评测不足**：目前主要靠人工试用判断效果。后续应建立固定测试样例和离线评测脚本，覆盖分类、tag/type 筛选、语义检索、总结质量和自动总结触发。
- **可观测性不足**：已接入第一版 JSONL 结构化日志和 `/status`，可以定位消息、worker、查询和自动总结的关键步骤。后续还需要日志轮转、metrics、告警和更完整的发送历史。
- **部署包装不足**：已补充本地脚本版部署包装，支持配置检查、启动、停止、状态、日志和备份。后续还需要 systemd 或 Docker、完整部署文档和恢复说明。
- **生产安全不足**：当前本地 JSON 存储和进程内锁适合学习项目，但还不是生产级。后续应考虑权限控制、命令白名单、敏感信息保护、速率限制、跨进程文件锁或数据库迁移。
- **缺真实使用数据**：已新增 `/feedback` 反馈入口，真实反馈会落盘到 `data/feedback/{space_id}.jsonl`。后续应进行一段时间 dogfooding，把误分类、搜不到、总结遗漏、误触发等反馈整理进 eval 样例。

## 运行单元测试

本项目当前统一使用 `zcj_hello` 环境运行测试：

```bash
/usr/local/anaconda3/envs/zcj_hello/bin/python -m pytest tests
```

第一批单元测试只覆盖确定性逻辑，不调用真实 LLM，不连接飞书：

- 固定 type/tags taxonomy 规范化
- `/type`、`/tag`、`/filter` 底层筛选
- summary 时间范围计算
- 自动总结订阅读写
- 自动总结 scheduler 到点触发规则

## 本地模拟写入

```bash
python main.py "今天看了一篇关于 RAG 语义分块的文章"
```

成功后可以检查当天 markdown、`index.json` 和 `vectors/index.json` 是否都新增了记录。
