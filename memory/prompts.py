"""Prompt placeholders for future LLM-backed memory extraction."""

MEMORY_EXTRACTOR_PROMPT = """
从用户笔记中提取长期记忆候选。不要保存寒暄、低价值状态或敏感凭据。
输出 JSON，包含 memory_type、content、importance、confidence、entities、should_store。
"""

RELATION_CLASSIFIER_PROMPT = """
判断候选记忆与已有记忆的关系：new、same、extend、update、contradict、unrelated。
"""

