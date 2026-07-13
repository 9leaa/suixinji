# 2阶段测试：mock 流程测试

这一阶段测试带 LLM、embedding、存储、发送副作用的业务流程，但不调用真实外部服务。核心方法是使用 pytest 的 `monkeypatch` 替换外部依赖。

## `test_worker_flow.py`

测试 worker 处理 WAL 记录的流程。

覆盖点：

- 新笔记流程：
  - `note_exists()` 返回 false。
  - mock `classify_text()` 返回固定标题、type、tags、summary。
  - mock `embed_text()` 返回固定向量。
  - mock `search_related_note_ids()` 返回固定 related note_id。
  - 断言 `save_note()` 收到的 `NoteMetadata` 字段正确。
  - 断言 `add_vector_item()` 收到的 `VectorItem` 字段正确。
  - 断言最后调用 `mark_processed()`。
- 已存在笔记流程：
  - `note_exists()` 返回 true。
  - 断言不再分类、不再重新保存笔记。
  - 断言会调用 `backfill_vector_if_missing()` 和 `mark_processed()`。
- 向量回填流程：
  - 从 mock `load_index()` 中找到已有笔记。
  - 当向量不存在时，用笔记正文生成 embedding 并写入 vector store。
  - 当向量已存在时，不重复 embed。
- pending 批处理去重：
  - 同一批 pending 中相同 `message_id` 只处理第一条。
  - 重复记录直接 `mark_processed()`。

## `test_query_agent_react.py`

测试 `/ask` 背后的 ReAct 查询流程。

覆盖点：

- 空问题不会调用 LLM，直接返回用法提示。
- LLM 第一步决定调用 `filter_notes` 时：
  - query agent 会执行筛选工具。
  - 第二步 prompt 中会带上 observations。
  - 如果 LLM 第二步给出 final answer，则直接返回。
- 如果 LLM 没给 action：
  - query agent 会默认走 `semantic_search`。
  - 默认参数使用 `top_k=5` 和 `DEFAULT_QUERY_MIN_SCORE`。
- fallback answer 能从 observations 中提取标题和摘要。

## `test_daily_summary_flow.py`

测试 P4 summary 生成主流程。

覆盖点：

- 有笔记时：
  - 第一次 mock `complete_json()` 返回草稿总结。
  - 第二次 mock `complete_json()` 返回 Reflection 后的最终总结。
  - 断言最终 markdown 使用修订版。
  - 断言总结 markdown 和 summaries/index.json 已保存到临时目录。
- LLM 抛异常时：
  - 使用 `_fallback_summary()` 生成保底总结。
  - 不让一次 LLM 失败导致 summary 命令完全失败。
- 没有笔记时：
  - 不调用 LLM。
  - 直接返回“今天没有记录到随心记笔记”。

## 为什么这样测

这些测试关注“流程是否走对”，不是测试模型聪不聪明。真实模型质量后续应该放到离线评测里做，例如分类样例集、query hit@k、summary 要点覆盖率。
