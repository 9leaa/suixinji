# 单元测试说明

当前 `tests/` 目录先覆盖项目里最稳定、最适合自动化回归的确定性逻辑。这一批测试不调用真实 LLM，不生成真实 embedding，也不连接飞书。

运行命令固定使用 `zcj_hello` 环境：

```bash
/usr/local/anaconda3/envs/zcj_hello/bin/python -m pytest tests
```

## 当前测试范围

### 1. `test_taxonomy.py`

测试固定分类体系是否稳定。

覆盖点：

- `is_valid_type()` 能识别固定 6 类：任务、学习、灵感、资料、生活、情绪。
- `is_valid_tag()` 只接受全局 tag 和对应类型 tag，并支持去掉 `#`。
- `normalize_type()` 对非法 type 回退到 `资料`。
- `normalize_tags()` 会丢弃自由标签、重复标签、等于 type 的标签。
- 当 LLM 没给出可用 tags 时，会从该 type 的推荐池里补足至少 2 个。
- `normalize_classification_data()` 会统一修正 LLM 分类结果。

测试方法：直接调用 taxonomy 函数并断言返回值，不读写文件。

### 2. `test_query_filter.py`

测试 P3 中不依赖 LLM 的查询筛选逻辑。

覆盖点：

- `filter_notes(note_type="生活")` 只返回生活类笔记，并按时间倒序排列。
- `filter_notes(tags=[...], match_all_tags=True)` 要求所有 tags 都命中。
- `filter_notes(tags=[...], match_all_tags=False)` 任一 tag 命中即可。
- 非法 type 或非法 tag 会直接返回空列表。
- `by_type()`、`by_tag()` 是对 `filter_notes()` 的薄封装。
- `get_note()` 能按 note_id 读取单条笔记。
- `follow_links()` 能返回 outbound related 和 inbound related。
- `_run_tool("filter_notes", ...)` 能把 ReAct 工具参数转成筛选调用。

测试方法：用 `monkeypatch` 替换 `query_agent.load_index()`，让它返回测试文件里的固定 `NOTES`，避免读取真实 `data/notes`。

### 3. `test_summary_range.py`

测试 P4 手动总结的时间范围计算。

覆盖点：

- `parse_summary_range()` 支持今天、昨天、一周、一个月、半年、一年等中文别名。
- `build_time_range()` 对固定当前时间 `2026-06-07 15:30 +08:00` 计算：
  - 今天：`2026-06-07 00:00` 到 `2026-06-08 00:00`
  - 昨天：`2026-06-06 00:00` 到 `2026-06-07 00:00`
  - 一周：`2026-06-01 00:00` 到 `2026-06-08 00:00`
  - 一个月、半年、一年也按当前实现的天数窗口计算。
- 未知 range 会抛出 `ValueError`。
- `load_notes_in_range()` 会过滤区间外、结束边界、非法时间戳，并按时间升序返回。

测试方法：传入固定 `now`，并用 `monkeypatch` 替换 `daily_summary.load_index()`。

### 4. `test_summary_subscription.py`

测试 P4 自动总结订阅文件的读写逻辑。

覆盖点：

- `parse_summary_time()` 只接受严格的 `HH:MM`，例如 `22:00`、`00:05`。
- `/summary_auto on` 背后的 `enable_summary_subscription()` 会创建默认订阅。
- `/summary_auto off` 背后的 `disable_summary_subscription()` 会关闭订阅。
- `list_enabled_summary_subscriptions()` 只返回 enabled 为 true 的订阅。
- `update_summary_time()` 会更新时间，并保留已有的 `last_sent_date`。
- 非法时间会抛出 `ValueError`。
- 对不存在订阅调用 `mark_summary_sent()` 不会创建文件。

测试方法：用 pytest 的 `tmp_path` 建临时目录，并用 `monkeypatch` 把 `SUBSCRIPTIONS_PATH` 指向临时文件，避免污染真实 `data/summary_subscriptions.json`。

### 5. `test_summary_scheduler.py`

测试 P4 自动总结 scheduler 的触发规则。

覆盖点：

- 设定 `22:00` 时：
  - `21:59` 不触发。
  - `22:00` 触发。
  - `23:30` 也触发。
  - 今天已经发送过则不触发。
- `run_summary_scheduler_once()` 对到期订阅会调用 `generate_summary()` 和发送函数。
- 发送成功后会调用 `mark_summary_sent()`。
- 发送失败时不会更新 `last_sent_date`，下一轮可以继续重试。

测试方法：用 `monkeypatch` 替换：

- `list_enabled_summary_subscriptions()`
- `generate_summary()`
- `mark_summary_sent()`
- `send_text` 回调

这样只测试调度逻辑本身，不调用真实 LLM 和飞书 API。

### 6. `test_feedback.py`

测试 dogfooding 反馈记录的本地落盘逻辑。

覆盖点：

- `save_feedback()` 会写入 JSONL。
- `list_feedback()` 能读回同一条反馈。
- 反馈字段包含 `id`、`message_id`、`text` 和默认 `status=open`。
- `create_feedback_record()` 会去掉反馈文本首尾空白。

测试方法：用 `tmp_path` 和 `monkeypatch` 把 `feedback.FEEDBACK_DIR` 指向临时目录，避免污染真实 `data/feedback`。

## 当前没有覆盖的内容

这一批测试是第一层单元测试，暂时不覆盖：

- 真实 LLM 分类质量。
- 真实 embedding 语义检索质量。
- `/ask` ReAct 多轮工具选择质量。
- 飞书 SDK 真实收发消息。
- worker 从 WAL 到 markdown、index、vector 的完整集成链路。
- 总结生成内容是否真的高质量。

这些后续适合分别用 mock 流程测试、离线评测和少量真实集成测试来补。

## 当前测试结果

最近一次运行结果：

```text
32 passed
```
