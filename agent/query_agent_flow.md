# query_agent.py 流程梳理

当前 P3 查询被分成两层：

```text
飞书直接命令：/type /tag /filter
  -> 直接调用 by_type / by_tag / filter_notes
  -> 只读 index.json
  -> 不调用 LLM，不生成 embedding

自然语言问答：/ask 问题
  -> answer_question()
  -> ReAct 选择 filter_notes / semantic_search / list_recent / get_note / follow_links
  -> 需要时使用 embedding + LLM
```

---

## 1. 确定性筛选接口

`filter_notes(space_id, note_type=None, tags=None, match_all_tags=True, limit=30)` 是底层筛选接口。

规则：

```text
note_type 不为空时，必须精确等于 note["type"]
tags 不为空时：
  match_all_tags=True  -> 笔记必须包含所有 tags
  match_all_tags=False -> 笔记包含任意 tag 即可
非法 type/tag 直接返回空列表
结果按 ts 倒序返回
```

`by_type()` 和 `by_tag()` 是薄封装：

```python
by_type(space_id, "生活")
by_tag(space_id, "饮食")
```

它们不搜索标题、摘要、原文，只做固定 type/tags 的精确筛选。

---

## 2. 飞书直接命令

`bot/feishu_bot.py` 提供 3 个直接查询命令：

```text
/type 生活
/tag 饮食
/filter type=生活 tags=饮食,日常
```

这些命令在飞书入口直接调用确定性筛选接口，不进入 `answer_question()`。

可选参数：

```text
/type 生活 20
/tag 饮食 20
/filter type=生活 tags=饮食,日常 match=all limit=30
/filter type=生活 tags=饮食,日常 match=any limit=30
```

---

## 3. ReAct 工具

`/ask` 仍然走 `answer_question()`。为了避免工具重复，prompt 只暴露 5 个工具：

```text
filter_notes(type, tags, match_all_tags, limit)
semantic_search(query, top_k, min_score)
list_recent(days, limit)
get_note(note_id)
follow_links(note_id, limit)
```

选择原则：

```text
用户明确给出 type/tags -> filter_notes
用户自然语言描述，或忘记分类条件 -> semantic_search
用户问最近内容 -> list_recent
用户已经有 note_id 或从结果里拿到 note_id -> get_note / follow_links
```

`related_notes()` 仍保留为代码接口，但不再作为主要 ReAct 工具暴露；相关笔记问题由 `semantic_search -> follow_links` 两步完成。

---

## 4. 语义查询

`semantic_search()` 仍使用 P2 向量索引：

```text
embed_text(query)
search_related(space_id, embedding, top_k, min_score)
```

`min_score` 默认是 `0.55`，用于避免 top_k 返回弱相关结果。

---

## 5. 向量回填

`core/worker.py` 已补充回填逻辑：

```text
如果 WAL 重跑时发现 index.json 已有该 message_id：
  -> 检查 vectors/index.json 是否已有该 note_id/message_id
  -> 如果缺失，用 index.json 中的 text 重新 embed_text
  -> add_vector_item 补写向量
  -> mark_processed
```

这避免了“笔记已保存但向量缺失，semantic_search 搜不到”的情况。
