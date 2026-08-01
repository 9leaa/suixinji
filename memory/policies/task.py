"""文件作用：任务策略。

项目关系：本文件依赖 无直接本地模块依赖；被 `memory.repository`、`repositories.postgres.memory`。
"""



from __future__ import annotations

import re


ALLOWED_TRANSITIONS = {
    "todo": {"blocked", "done", "cancelled"},
    "blocked": {"todo", "done", "cancelled"},
    "done": {"todo"},
    "cancelled": {"todo"},
}


_IDENTIFIER_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*|\d+(?:[._-]\d+)*")


def task_identifiers(text: str) -> frozenset[str]:
    """函数功能：`task_identifiers` 负责处理 task identifiers，服务于本文件职责：任务策略。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `frozenset[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    identifiers: set[str] = set()
    for token in _IDENTIFIER_TOKEN_RE.findall(str(text or "")):
        has_code_punctuation = any(character in "#._-" for character in token)
        if any(character.isdigit() for character in token) or has_code_punctuation or token.isupper():
            identifiers.add(token.casefold())
    return frozenset(identifiers)


def identifiers_compatible(left_content: str, right_content: str) -> bool:
    """函数功能：`identifiers_compatible` 负责处理 identifiers compatible，服务于本文件职责：任务策略。
    传参：
        left_content: left content 参数，由调用方传入，类型为 `str`。
        right_content: right content 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    left = task_identifiers(left_content)
    right = task_identifiers(right_content)
    return not left or not right or left == right


def can_transition(old_status: str | None, new_status: str | None) -> bool:
    """函数功能：`can_transition` 负责处理 can transition，服务于本文件职责：任务策略。
    传参：
        old_status: old status 参数，由调用方传入，类型为 `str | None`。
        new_status: new status 参数，由调用方传入，类型为 `str | None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    # 已持久化的旧记录可以在不写库的前提下继续参与后续状态演化。
    # 新候选和新写入均不能再产生 in_progress。
    if old_status == "in_progress":
        old_status = "todo"
    if new_status is None or old_status == new_status:
        return True
    if old_status is None:
        return True
    return new_status in ALLOWED_TRANSITIONS.get(old_status, set())


def is_terminal(status: str | None) -> bool:
    """函数功能：`is_terminal` 负责判断是否为 terminal，服务于本文件职责：任务策略。
    传参：
        status: status 参数，由调用方传入，类型为 `str | None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return status in {"done", "cancelled"}
