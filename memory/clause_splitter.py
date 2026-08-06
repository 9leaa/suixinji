"""文件作用：消息子句切分。

项目关系：本文件依赖 无直接本地模块依赖；被 `memory.extractor`、`tests.test_stage7_model_routing_and_clause_extraction`。
"""



from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Clause:
    """类功能：`Clause` 封装与“消息子句切分”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    index: int
    text: str
    start: int
    end: int


# In memory extraction a comma frequently separates independently writable
# facts (preference, device, task state, ...).  Restricting comma boundaries
# to a small hand-written set silently collapsed long mixed messages into one
# candidate and lost facts.  Sentence-final punctuation still works as before.
_BOUNDARY_RE = re.compile(r"[。！？!?；;，,](?!(?:(?:嗯|呃)[，,]\s*)?(?:还在|还没|仍在|仍然))")
_LEADING_CONNECTOR_RE = re.compile(r"^(?:并且|而且|但是|不过|同时|然后|接着|另外|还|以及|，|,)\s*")


def split_clauses(text: str, *, max_clauses: int = 64) -> list[Clause]:
    """函数功能：`split_clauses` 负责切分 clauses，服务于本文件职责：消息子句切分。
    传参：
        text: 输入文本内容，类型为 `str`。
        max_clauses: max clauses 参数，由调用方传入，类型为 `int`，默认值为 `8`。
    返回结果说明：
        返回 `list[Clause]`，表示按条件筛选、构造或查询得到的列表。
    """
    raw = str(text or "")
    clauses: list[Clause] = []
    start = 0
    for match in _BOUNDARY_RE.finditer(raw):
        end = match.start()
        _append_clause(clauses, raw, start, end, max_clauses)
        start = match.end()
        if len(clauses) >= max_clauses:
            break
    if len(clauses) < max_clauses:
        _append_clause(clauses, raw, start, len(raw), max_clauses)
    return clauses


def _append_clause(clauses: list[Clause], raw: str, start: int, end: int, max_clauses: int) -> None:
    """函数功能：`_append_clause` 负责追加 clause，服务于本文件职责：消息子句切分。
    传参：
        clauses: clauses 参数，由调用方传入，类型为 `list[Clause]`。
        raw: raw 参数，由调用方传入，类型为 `str`。
        start: start 参数，由调用方传入，类型为 `int`。
        end: end 参数，由调用方传入，类型为 `int`。
        max_clauses: max clauses 参数，由调用方传入，类型为 `int`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    if len(clauses) >= max_clauses:
        return
    segment = raw[start:end].strip()
    segment = _LEADING_CONNECTOR_RE.sub("", segment).strip()
    if not segment:
        return
    if len(segment) <= 2:
        return
    clauses.append(Clause(index=len(clauses), text=segment, start=start, end=end))
