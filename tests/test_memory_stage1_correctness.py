"""文件作用：偏好否定、冲突和基础正确性回归。

项目关系：本文件依赖 `core.config`、`memory.consolidator`、`memory.expiry`、`memory.models` 等 6 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from core.config import get_chat_config
from memory.consolidator import generate_stable_semantic
from memory.expiry import run_expiry_once
from memory.models import MemoryCandidate, MemoryDecision, candidate_id_for
from memory.repository import (
    apply_memory_decision,
    edit_pending_memory,
    get_memory_candidate_status,
    insert_memory,
    list_memories,
    list_retryable_memory_candidates,
    reject_pending_memory,
    resolve_memory_conflict,
    search_memories,
)
from memory.service import process_note_memory


def test_candidate_is_persisted_and_terminal_retry_is_idempotent() -> None:
    """函数功能：`test_candidate_is_persisted_and_terminal_retry_is_idempotent` 负责验证 candidate is persisted and terminal retry is idempotent 场景，服务于本文件职责：偏好否定、冲突和基础正确性回归。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    note = {"id": "note-candidate", "space_id": "space-1", "text": "我喜欢喝牛奶"}
    first = process_note_memory(note)
    second = process_note_memory(note)
    candidate_id = candidate_id_for(note["id"], "preference", "用户喜欢喝牛奶")

    assert first["extraction_status"] == "completed"
    assert second["idempotent"] is True
    assert get_memory_candidate_status(candidate_id) == "applied"
    assert list_retryable_memory_candidates("space-1") == []
    assert len(list_memories("space-1", memory_type="preference")) == 1


def test_model_roles_are_configurable(monkeypatch) -> None:
    """函数功能：`test_model_roles_are_configurable` 负责验证 model roles are configurable 场景，服务于本文件职责：偏好否定、冲突和基础正确性回归。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setenv("SUIXINJI_FAST_MODEL", "fast-test")
    monkeypatch.setenv("SUIXINJI_BALANCED_MODEL", "balanced-test")
    monkeypatch.setenv("SUIXINJI_STRONG_MODEL", "strong-test")

    assert get_chat_config("fast").model == "fast-test"
    assert get_chat_config("balanced").model == "balanced-test"
    assert get_chat_config("strong").model == "strong-test"


def test_partial_retry_skips_applied_candidate(monkeypatch) -> None:
    """函数功能：`test_partial_retry_skips_applied_candidate` 负责验证 partial retry skips applied candidate 场景，服务于本文件职责：偏好否定、冲突和基础正确性回归。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    import memory.service as service

    first = MemoryCandidate("semantic", "用户正在学习数据库", 0.8, 0.9)
    second = MemoryCandidate("preference", "用户喜欢绿茶", 0.8, 0.9)
    calls: list[str] = []
    failed_once = {"value": False}

    monkeypatch.setattr(service, "may_contain_memory", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(service, "extract_candidates", lambda *args, **kwargs: [first, second])

    def consolidate(space_id, note_id, candidate, trace=None):
        """函数功能：`consolidate` 负责合并长期记忆，服务于本文件职责：偏好否定、冲突和基础正确性回归。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
            note_id: Note 标识，用于定位原始记录。
            candidate: candidate 参数，由调用方传入。
            trace: trace 参数，由调用方传入，默认值为 `None`。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        calls.append(candidate.memory_type)
        if candidate.memory_type == "preference" and not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError("temporary")
        return {"candidate_id": candidate.candidate_id, "decision_id": f"decision-{candidate.memory_type}", "action": "insert"}

    monkeypatch.setattr(service, "consolidate_candidate", consolidate)
    note = {"id": "note-partial", "space_id": "space-1", "text": "组合候选输入"}

    assert process_note_memory(note)["extraction_status"] == "partial"
    assert process_note_memory(note)["extraction_status"] == "completed"
    assert calls == ["semantic", "preference", "preference"]


def test_negative_preference_does_not_answer_positive_preference_query() -> None:
    """函数功能：`test_negative_preference_does_not_answer_positive_preference_query` 负责验证 negative preference does not answer positive preference query 场景，服务于本文件职责：偏好否定、冲突和基础正确性回归。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    process_note_memory({"id": "note-like", "space_id": "space-1", "text": "我喜欢喝牛奶"})
    process_note_memory({"id": "note-dislike", "space_id": "space-1", "text": "我讨厌喝牛奶"})

    assert search_memories("space-1", "我喜欢喝什么", mark_access=False) == []
    negative = search_memories("space-1", "我不喜欢喝什么", mark_access=False)
    assert negative and "讨厌喝牛奶" in negative[0][0].content


def test_valid_until_is_filtered_and_expiry_worker_versions_state() -> None:
    """函数功能：`test_valid_until_is_filtered_and_expiry_worker_versions_state` 负责验证 valid until is filtered and expiry worker versions state 场景，服务于本文件职责：偏好否定、冲突和基础正确性回归。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    expired_at = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")
    memory = insert_memory(
        "space-1",
        MemoryCandidate("semantic", "用户本周住在上海", 0.8, 0.9, valid_until=expired_at),
        source_note_id="note-expired",
    )

    assert list_memories("space-1", memory_type="semantic") == []
    report = run_expiry_once(space_id="space-1")
    expired = list_memories("space-1", status="expired", memory_type="semantic")

    assert report["expired_count"] == 1
    assert expired[0].id == memory.id
    assert expired[0].current_version == 2


def test_monthly_consolidation_is_domain_neutral() -> None:
    """函数功能：`test_monthly_consolidation_is_domain_neutral` 负责验证 monthly consolidation is domain neutral 场景，服务于本文件职责：偏好否定、冲突和基础正确性回归。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    for index, text in enumerate(("周末练习烘焙面包", "记录一次跑步训练", "整理旅行照片")):
        insert_memory("space-1", MemoryCandidate("episodic", text, 0.7, 0.8), source_note_id=f"note-{index}")

    report = generate_stable_semantic("space-1", min_sources=3)
    semantic = list_memories("space-1", memory_type="semantic")

    assert report["created"] is True
    assert semantic
    assert "Agent/RAG" not in semantic[0].content
    assert all(text in semantic[0].content for text in ("烘焙", "跑步", "旅行"))


def _pending_memory(note_id: str, content: str) -> str:
    """函数功能：`_pending_memory` 负责处理 pending memory，服务于本文件职责：偏好否定、冲突和基础正确性回归。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    candidate = replace(
        MemoryCandidate("preference", content, 0.8, 0.9),
        note_id=note_id,
        candidate_id=candidate_id_for(note_id, "preference", content),
    )
    decision = MemoryDecision(
        candidate_id=candidate.candidate_id,
        relation="new",
        target_memory_ids=[],
        confidence=0.7,
        reason="manual_review_test",
        evidence=[f"note:{note_id}"],
        recommended_action="pending_review",
    )
    return str(apply_memory_decision("space-1", note_id, candidate, decision)["memory_id"])


def test_review_reject_edit_and_conflict_resolution() -> None:
    """函数功能：`test_review_reject_edit_and_conflict_resolution` 负责验证 review reject edit and conflict resolution 场景，服务于本文件职责：偏好否定、冲突和基础正确性回归。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    rejected_id = _pending_memory("note-reject", "用户喜欢咖啡")
    rejected = reject_pending_memory(rejected_id, reason="not_user_fact")
    assert rejected is not None and rejected.status == "archived"

    edited_id = _pending_memory("note-edit", "用户喜欢茶")
    edited = edit_pending_memory(edited_id, "用户喜欢绿茶")
    assert edited is not None and edited.status == "active" and "绿茶" in edited.content

    conflict = insert_memory("space-1", MemoryCandidate("semantic", "用户住在杭州", 0.8, 0.9), source_note_id="note-conflict", status="conflicted")
    resolved = resolve_memory_conflict(conflict.id, resolution="keep")
    assert resolved is not None and resolved.status == "active"
