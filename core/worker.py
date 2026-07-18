"""Background worker for classifying notes and writing them to storage."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.classifier import classify_text, classify_text_local
from core.sensitive import assess_sensitive_text
from core.observability import observe
from core.settings import ENRICHMENT_MAX_ATTEMPTS, RELATED_MIN_SCORE, RELATED_TOP_K
from core.wal import load_pending_records, mark_processed, mark_sensitive_blocked
from storage.note_storage import (
    NoteMetadata,
    find_note,
    is_note_queryable,
    load_index,
    note_exists,
    save_note,
    update_note_metadata,
)
from core.llm_client import embed_text
from infrastructure.redis_cache import invalidate_space_cache
from memory.repository import get_extraction_state
from memory.service import process_note_memory
from storage.vector_store import (
    VectorItem,
    add_vector_item,
    search_related_note_ids,
    vector_item_exists,
)

LOGGER = logging.getLogger(__name__)


def _find_note_by_message_id(space_id: str, message_id: str) -> dict[str, Any] | None:
    for note in load_index(space_id):
        if note.get("message_id") == message_id:
            return note
    return None


def backfill_vector_if_missing(space_id: str, message_id: str) -> bool:
    """补写已存在笔记缺失的向量记录。

    如果 worker 在 save_note 成功后、add_vector_item 成功前崩溃，
    重跑时 index.json 已经有笔记，但 vectors/index.json 可能缺记录。
    这个函数用 index.json 中的笔记内容补写向量，恢复 semantic_search 能力。
    """
    note = _find_note_by_message_id(space_id, message_id)
    if note is None or not is_note_queryable(note):
        return False

    note_id = str(note.get("id") or "")
    if not note_id:
        return False

    if vector_item_exists(space_id, note_id, message_id):
        return False

    text = str(note.get("text") or "").strip()
    if not text:
        return False

    with observe(
        "worker.write_vector",
        space_id=space_id,
        message_id=message_id,
        record_id=note_id,
        extra={"note_id": note_id, "reason": "backfill"},
    ):
        embedding = embed_text(text)
        return add_vector_item(
            space_id,
            VectorItem(
                note_id=note_id,
                message_id=message_id,
                text=text,
                embedding=embedding,
                metadata={
                    "title": note.get("title"),
                    "tags": note.get("tags", []),
                    "type": note.get("type"),
                    "summary": note.get("summary"),
                    "ts": note.get("ts"),
                },
            ),
        )


def process_record(record: dict[str, Any], *, defer_memory: bool = False) -> NoteMetadata | dict[str, Any] | None:
    """处理单条 pending WAL 记录。

    功能说明:
        调用分类器生成结构化信息，将结果保存为 markdown 笔记和 index.json 索引，
        最后把对应 WAL 记录标记为 processed。如果该 message_id 已经存在于
        index.json，则跳过重复保存并直接把 WAL 标记为 processed。

    传参说明:
        record: 从 WAL 中读取出的单条消息记录字典。

    返回类型说明:
        None: 处理完成后通过文件系统产生副作用，不返回业务结果。
    """
    space_id = record["space_id"]
    record_id = record["id"]
    message_id = record["message_id"]
    ctx = {"space_id": space_id, "message_id": message_id, "record_id": record_id}

    with observe("worker.process_record", **ctx):
        sensitive = assess_sensitive_text(str(record.get("text") or ""))
        if sensitive.blocks_storage:
            mark_sensitive_blocked(space_id, record_id, str(sensitive.category or "sensitive"))
            LOGGER.warning(
                "Blocked sensitive WAL record before note storage: space_id=%s message_id=%s record_id=%s category=%s",
                space_id,
                message_id,
                record_id,
                sensitive.category,
            )
            return None

        if note_exists(space_id, message_id):
            note = _find_note_by_message_id(space_id, message_id)
            recovered_memory = False
            if not defer_memory and note is not None and is_note_queryable(note):
                note_id = str(note.get("id") or "")
                state = get_extraction_state(note_id) if note_id else None
                if state is None or state.status in {"pending", "failed", "partial"}:
                    try:
                        with observe("worker.recover_memory", extra={"note_id": note_id}, **ctx):
                            process_note_memory(note)
                        recovered_memory = True
                    except Exception:
                        LOGGER.exception(
                            "Memory recovery failed for existing note: space_id=%s message_id=%s record_id=%s note_id=%s",
                            space_id,
                            message_id,
                            record_id,
                            note_id,
                        )
            LOGGER.info(
                "Skip WAL record because note already exists: space_id=%s message_id=%s record_id=%s recovered_memory=%s",
                space_id,
                message_id,
                record_id,
                recovered_memory,
            )
            with observe(
                "worker.mark_processed",
                extra={"reason": "note_exists", "recovered_memory": recovered_memory},
                **ctx,
            ):
                mark_processed(space_id, record_id)
            return note

        with observe("worker.classify_local", **ctx):
            classification = classify_text_local(record["text"])

        note = NoteMetadata(
            id=record_id,
            message_id=message_id,
            space_id=space_id,
            ts=record["ts"],
            title=classification.title,
            tags=classification.tags,
            type=classification.type,
            summary=classification.summary,
            text=record["text"],
            related=[],
            enrichment_status="provisional",
            enrichment_attempts=0,
            sensitivity="normal",
            tenant_id=str(record.get("tenant_id") or "default"),
        )

        with observe("worker.write_note", extra={"note_id": record_id}, **ctx):
            save_note(note)
        invalidate_space_cache(space_id)

        if not defer_memory:
            try:
                with observe("worker.write_memory", extra={"note_id": record_id}, **ctx):
                    process_note_memory(note, classification={
                        "title": classification.title,
                        "tags": classification.tags,
                        "type": classification.type,
                        "summary": classification.summary,
                    })
            except Exception:
                LOGGER.exception(
                    "Memory processing failed after note was saved: space_id=%s message_id=%s record_id=%s",
                    space_id,
                    message_id,
                    record_id,
                )

        with observe("worker.mark_processed", **ctx):
            mark_processed(space_id, record_id)
        return note


def enrich_note(space_id: str, note_id: str) -> bool:
    """Run slow LLM classification and embedding after the note is queryable."""
    note = find_note(space_id, note_id)
    if note is None or not is_note_queryable(note):
        return False

    status = str(note.get("enrichment_status") or "ready")
    attempts = int(note.get("enrichment_attempts") or 0)
    message_id = str(note.get("message_id") or "")
    if status == "ready" and vector_item_exists(space_id, note_id, message_id):
        return False
    if status in {"failed", "enriching"} and attempts >= ENRICHMENT_MAX_ATTEMPTS:
        return False

    now = datetime.now().astimezone().isoformat()
    update_note_metadata(
        space_id,
        note_id,
        enrichment_status="enriching",
        enrichment_attempts=attempts + 1,
        enrichment_error=None,
        enrichment_started_at=now,
        enrichment_updated_at=now,
    )
    ctx = {"space_id": space_id, "message_id": message_id, "record_id": note_id}
    try:
        with observe("worker.enrich_classify", extra={"attempt": attempts + 1}, **ctx):
            classification = classify_text(str(note.get("text") or ""))
        with observe("worker.enrich_embed", extra={"attempt": attempts + 1}, **ctx):
            embedding = embed_text(str(note.get("text") or ""))
        related = search_related_note_ids(
            space_id,
            embedding,
            top_k=RELATED_TOP_K,
            exclude_note_id=note_id,
            min_score=RELATED_MIN_SCORE,
        )
        with observe("worker.write_vector", extra={"note_id": note_id, "reason": "background_enrichment"}, **ctx):
            add_vector_item(
                space_id,
                VectorItem(
                    note_id=note_id,
                    message_id=message_id,
                    text=str(note.get("text") or ""),
                    embedding=embedding,
                    metadata={
                        "title": classification.title,
                        "tags": classification.tags,
                        "type": classification.type,
                        "summary": classification.summary,
                        "ts": note.get("ts"),
                        "sensitivity": "normal",
                    },
                ),
            )
        finished_at = datetime.now().astimezone().isoformat()
        update_note_metadata(
            space_id,
            note_id,
            title=classification.title,
            tags=classification.tags,
            type=classification.type,
            summary=classification.summary,
            related=related,
            enrichment_status="ready",
            enrichment_error=None,
            enrichment_updated_at=finished_at,
        )
        invalidate_space_cache(space_id)
        return True
    except Exception:
        update_note_metadata(
            space_id,
            note_id,
            enrichment_status="failed",
            enrichment_error="background_enrichment_failed",
            enrichment_updated_at=datetime.now().astimezone().isoformat(),
        )
        invalidate_space_cache(space_id)
        raise


def process_pending(space_id: str) -> int:
    """处理指定 space_id 下所有 pending WAL 记录。

    功能说明:
        读取当前 space_id 下所有 pending 记录，并逐条调用 process_record 完成分类、
        保存和状态更新。恢复过程中会按 message_id 跳过重复 pending 记录，
        且单条记录失败不会中断整个 space_id 的恢复。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        int: 本次成功处理或成功跳过的 pending 记录数量。
    """
    records = load_pending_records(space_id)
    count = 0
    seen_message_ids: set[str] = set()

    for record in records:
        record_id = record.get("id")
        message_id = record.get("message_id")

        if message_id in seen_message_ids:
            LOGGER.warning(
                "Skip duplicate pending WAL record in same recovery batch: space_id=%s message_id=%s record_id=%s",
                space_id,
                message_id,
                record_id,
            )
            if record_id:
                with observe(
                    "worker.mark_processed",
                    space_id=space_id,
                    message_id=message_id,
                    record_id=record_id,
                    extra={"reason": "duplicate_pending"},
                ):
                    mark_processed(space_id, record_id)
                count += 1
            continue

        if message_id:
            seen_message_ids.add(message_id)

        try:
            process_record(record)
            count += 1
        except Exception:
            LOGGER.exception(
                "Failed to process pending WAL record: space_id=%s message_id=%s record_id=%s",
                space_id,
                message_id,
                record_id,
            )

    return count
